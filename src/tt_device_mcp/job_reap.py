# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Sweep up the processes a finished job left behind, and name any that will not die.

The runner's termination ladder (``server._terminate_job``) signals the job's process group,
or its systemd scope under privsep. A child can still outlive it: one that started its own
session or process group (``setsid``, ``timeout``, a daemonizing helper) is out of the group's
reach, and a process stuck in the kernel (state D, most often inside the device driver)
ignores every signal including SIGKILL. Either one keeps the device open, and the next job
inherits a device someone else still holds.

So after a job is finalized the runner sweeps its leftovers here: SIGTERM, a short grace,
SIGKILL, and then a re-scan. Whatever is still alive is returned with its state and whether it
holds a ``/dev/tenstorrent`` node, so the runner can log it and keep the device from the next
job until it is gone.

A process belongs to the job when it shares the job's process group or session (``pid`` is
both: jobs start under ``os.setsid``), sits in the job's systemd scope cgroup, carries the job's
``JOB_TAG_ENV`` value in its environment, or descends from any of those. The cgroup is the one
membership a child cannot leave on its own; the tag is what still finds a child that started
its own session and whose parent has since exited (``setsid cmd &``, ``nohup``) on a host
without privsep, where there is no scope.
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from tt_device_mcp import device_holders
from tt_device_mcp.constants import SURVIVOR_KILL_WAIT_SEC, SURVIVOR_TERM_GRACE_SEC

logger = logging.getLogger("tt-device-mcp")

PROC_DIR = "/proc"
# Exported into every job's shell with a value unique to that run; children inherit it.
JOB_TAG_ENV = "TT_DEVICE_MCP_JOB_TAG"
_POLL_SEC = 0.1


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    sid: int
    state: str
    starttime: str


@dataclass(frozen=True)
class Survivor:
    """A job process still alive after SIGKILL."""

    pid: int
    state: str
    cmdline: str
    holds_device: Optional[bool]  # None: its fds could not be read, so it may hold the device
    starttime: str = ""  # with pid, the process identity a later holder scan matches against

    def describe(self) -> str:
        held = {True: "holds the device", False: "does not hold the device", None: "fds unreadable"}
        return f"pid {self.pid} state {self.state} ({held[self.holds_device]}): {self.cmdline}"


def _read_stat(pid: int) -> Optional[ProcInfo]:
    try:
        with open(f"{PROC_DIR}/{pid}/stat") as f:
            raw = f.read()
    except OSError:
        return None
    # comm (field 2) may hold spaces or ')'; index the fields after its LAST ')'.
    try:
        rest = raw.rsplit(")", 1)[1].split()
        return ProcInfo(
            pid=pid,
            state=rest[0],
            ppid=int(rest[1]),
            pgid=int(rest[2]),
            sid=int(rest[3]),
            starttime=rest[19],
        )
    except (IndexError, ValueError):
        return None


def _in_scope(pid: int, scope: str) -> bool:
    try:
        with open(f"{PROC_DIR}/{pid}/cgroup") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    for line in lines:
        path = line.rsplit(":", 1)[-1]
        if path.endswith("/" + scope) or f"/{scope}/" in path:
            return True
    return False


def _has_tag(pid: int, tag: str) -> bool:
    try:
        with open(f"{PROC_DIR}/{pid}/environ", "rb") as f:
            entries = f.read().split(b"\0")
    except OSError:
        return False
    return f"{JOB_TAG_ENV}={tag}".encode() in entries


def _cmdline(pid: int) -> str:
    try:
        with open(f"{PROC_DIR}/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        raw = b""
    text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    if not text:
        try:
            with open(f"{PROC_DIR}/{pid}/comm") as f:
                text = f"[{f.read().strip()}]"
        except OSError:
            text = "?"
    return text[:200]


def _holds_device(pid: int) -> Optional[bool]:
    try:
        return device_holders._process_holds_device(pid)
    except (PermissionError, FileNotFoundError):
        return None
    except OSError:
        return None


def _all_procs() -> dict[int, ProcInfo]:
    procs = {}
    try:
        names = os.listdir(PROC_DIR)
    except OSError:
        return procs
    for name in names:
        if name.isdigit():
            info = _read_stat(int(name))
            if info is not None:
                procs[info.pid] = info
    return procs


def job_processes(pid: Optional[int], scope: Optional[str] = None, tag: Optional[str] = None) -> dict[int, ProcInfo]:
    """Live (non-zombie) processes that belong to the job rooted at ``pid`` / ``scope`` / ``tag``.

    Never includes the broker itself or pid 1, whatever /proc says."""
    procs = _all_procs()
    exclude = {os.getpid(), 1, 0}
    members = {
        p
        for p, info in procs.items()
        if (pid and (info.pgid == pid or info.sid == pid or p == pid))
        or (scope and _in_scope(p, scope))
        or (tag and p not in exclude and _has_tag(p, tag))
    }
    # Descendants of any member: a child that left the group or session but whose parent
    # chain is still intact.
    children: dict[int, list[int]] = {}
    for p, info in procs.items():
        children.setdefault(info.ppid, []).append(p)
    stack = list(members)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in members:
                members.add(child)
                stack.append(child)
    return {p: procs[p] for p in members if p not in exclude and procs[p].state != "Z"}


def _alive(info: ProcInfo) -> bool:
    now = _read_stat(info.pid)
    # Same pid AND same start time is the same process; a zombie holds no fds.
    return now is not None and now.starttime == info.starttime and now.state != "Z"


def _signal(targets: Iterable[ProcInfo], sig: int, pgid: Optional[int]) -> None:
    if pgid:
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
    for info in targets:
        if _alive(info):
            try:
                os.kill(info.pid, sig)
            except OSError:
                pass


async def _wait_gone(targets: dict[int, ProcInfo], window: float) -> dict[int, ProcInfo]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    while True:
        left = {p: i for p, i in targets.items() if _alive(i)}
        if not left or loop.time() >= deadline:
            return left
        await asyncio.sleep(_POLL_SEC)


async def reap_job_survivors(
    job_id: str,
    pid: Optional[int],
    scope: Optional[str] = None,
    tag: Optional[str] = None,
    *,
    term_grace_sec: float = SURVIVOR_TERM_GRACE_SEC,
    kill_wait_sec: float = SURVIVOR_KILL_WAIT_SEC,
    log: Callable[[str], None] = logger.warning,
) -> list[Survivor]:
    """End every process the job left behind: SIGTERM, ``term_grace_sec``, SIGKILL.

    Returns the processes still alive ``kill_wait_sec`` after the SIGKILL (empty in the
    normal case, where the job left nothing behind and this costs one /proc scan). Each
    process found is logged through ``log``. Never raises."""
    try:
        found = await asyncio.to_thread(job_processes, pid, scope, tag)
        if not found:
            # Nothing visible; the pgid kill is still the cheap, idempotent backstop.
            _signal((), signal.SIGKILL, pid)
            return []
        log(
            f"REAP job {job_id}: {len(found)} process(es) outlived the job: "
            + "; ".join(f"pid {p} {_cmdline(p)[:80]}" for p in sorted(found))
            + f" — SIGTERM, then SIGKILL after {term_grace_sec:g}s"
        )
        _signal(found.values(), signal.SIGTERM, pid)
        left = await _wait_gone(found, term_grace_sec)
        if left:
            _signal(left.values(), signal.SIGKILL, pid)
            await _wait_gone(left, kill_wait_sec)
        # Re-scan: anything the job forked during the sweep is a survivor too.
        rescan = await asyncio.to_thread(job_processes, pid, scope, tag)
        survivors = [
            Survivor(pid=p, state=i.state, cmdline=_cmdline(p), holds_device=_holds_device(p), starttime=i.starttime)
            for p, i in sorted(rescan.items())
            if _alive(i)
        ]
        for s in survivors:
            log(f"REAP job {job_id}: survived SIGKILL: {s.describe()}")
        return survivors
    except Exception as e:  # bookkeeping must never take the runner down
        log(f"REAP job {job_id}: survivor sweep failed: {e}")
        return []


# --- Leftovers of reaped jobs that still hold the device (spec 01 I16) ---------------------------
#
# Read through their own root, not PROC_DIR: the sweep above is exercised against real
# processes, while the fence below is tested against staged /proc trees.
LEFTOVER_PROC_DIR = "/proc"
JOB_SCOPE_PREFIX = "ttdev-job-"
_SIGKILL_BIT = 1 << (signal.SIGKILL - 1)


@dataclass(frozen=True)
class ReapedLeftover:
    """A device holder that belongs to a job the runner already finished and swept."""

    job_id: str
    pid: int
    uid: int
    state: str  # /proc/<pid>/stat field 3; "?" if unreadable
    wchan: str  # the kernel function it sleeps in; "?" if unreadable
    sigkill_pending: Optional[bool]  # None: /proc/<pid>/status unreadable

    def describe(self) -> str:
        kill = {True: "SIGKILL pending", False: "no SIGKILL pending", None: "signals unreadable"}
        return (
            f"job {self.job_id} pid {self.pid} ({device_holders.username_for_uid(self.uid)}) "
            f"state {self.state} wchan {self.wchan}, {kill[self.sigkill_pending]}"
        )

    @property
    def unkillable(self) -> bool:
        """Stuck in the kernel: SIGKILL is already queued and the process cannot act on it."""
        return self.state == "D" or bool(self.sigkill_pending)


def _read_leftover(pid: int, name: str) -> Optional[str]:
    try:
        with open(f"{LEFTOVER_PROC_DIR}/{pid}/{name}") as f:
            return f.read()
    except OSError:
        return None


def _job_scope_id(pid: int) -> Optional[str]:
    """The job id of the ttdev-job-<id>.scope cgroup ``pid`` sits in, or None."""
    raw = _read_leftover(pid, "cgroup")
    for line in (raw or "").splitlines():
        for part in line.rsplit(":", 1)[-1].split("/"):
            if part.startswith(JOB_SCOPE_PREFIX) and part.endswith(".scope"):
                return part[len(JOB_SCOPE_PREFIX) : -len(".scope")] or None
    return None


def _leftover_state(pid: int) -> tuple[str, str]:
    """(state, starttime) from /proc/<pid>/stat; ("?", "") if unreadable."""
    raw = _read_leftover(pid, "stat")
    try:
        rest = (raw or "").rsplit(")", 1)[1].split()
        return rest[0], rest[19]
    except IndexError:
        return "?", ""


def _sigkill_pending(pid: int) -> Optional[bool]:
    """SIGKILL queued on the thread (SigPnd) or the process (ShdPnd) but not yet acted on."""
    raw = _read_leftover(pid, "status")
    if raw is None:
        return None
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        if key in ("SigPnd", "ShdPnd"):
            try:
                if int(value.strip(), 16) & _SIGKILL_BIT:
                    return True
            except ValueError:
                continue
    return False


def find_reaped_leftovers(
    holders: Iterable["device_holders.DeviceHolder"],
    live_job_ids: Iterable[str],
    survivors: Optional[dict[int, tuple[str, str]]] = None,
) -> list[ReapedLeftover]:
    """The device holders that belong to a job no longer running.

    A holder belongs to a job when it sits in that job's ttdev-job-<id>.scope cgroup (privsep; a
    process cannot leave it, and it outlives a broker restart), or when it is a survivor the sweep
    named for that job (``survivors``: pid -> (job id, starttime); the same starttime, so a reused
    pid is not mistaken for it). A job in ``live_job_ids`` (running, being torn down, re-adopted)
    owns the device by right and is never a leftover."""
    live = set(live_job_ids)
    known = survivors or {}
    found = []
    for h in holders:
        state, starttime = _leftover_state(h.pid)
        job_id = _job_scope_id(h.pid)
        if job_id is None and h.pid in known and starttime and known[h.pid][1] == starttime:
            job_id = known[h.pid][0]
        if job_id is None or job_id in live:
            continue
        wchan = (_read_leftover(h.pid, "wchan") or "").strip() or "?"
        found.append(
            ReapedLeftover(
                job_id=job_id,
                pid=h.pid,
                uid=h.uid,
                state=state,
                wchan=wchan,
                sigkill_pending=_sigkill_pending(h.pid),
            )
        )
    return found
