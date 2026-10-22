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
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from tt_device_mcp import device_holders
from tt_device_mcp.constants import HOLDER_RELEASE_WAIT_SEC, SURVIVOR_KILL_WAIT_SEC, SURVIVOR_TERM_GRACE_SEC

logger = logging.getLogger("tt-device-mcp")

PROC_DIR = "/proc"
# Exported into every job's shell with a value unique to that run; children inherit it.
JOB_TAG_ENV = "TT_DEVICE_MCP_JOB_TAG"
# An empty /proc/<pid>/environ does not prove the tag absent: a process inside execve reads empty
# until the new image's environment is mapped, and one past exit_mm reads empty for good. Such a
# process (not a kernel thread or zombie, which never have one) is re-read, bounded for the whole
# scan, before it is ruled out.
TAG_REREAD_SEC = 0.1
_TAG_REREAD_POLL_SEC = 0.002
_PF_KTHREAD = 0x00200000
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


def _read_tag(pid: int, tag: str) -> Optional[bool]:
    """Whether ``pid`` carries ``tag``; None when its environ reads empty but it is a live user
    process, so the read cannot tell (mid-execve, or exiting)."""
    try:
        with open(f"{PROC_DIR}/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return False
    if raw:
        return f"{JOB_TAG_ENV}={tag}".encode() in raw.split(b"\0")
    try:
        with open(f"{PROC_DIR}/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        if rest[0] in ("Z", "X") or int(rest[6]) & _PF_KTHREAD:
            return False
    except (OSError, IndexError, ValueError):
        return False
    return None


def _tagged(pids: Iterable[int], tag: str) -> set[int]:
    """The pids that carry ``tag``. One whose read cannot tell is re-read until it can, for at
    most ``TAG_REREAD_SEC`` across all of them; still undecided then, it is ruled out."""
    found, unsure = set(), []
    for p in pids:
        has = _read_tag(p, tag)
        if has:
            found.add(p)
        elif has is None:
            unsure.append(p)
    deadline = time.monotonic() + TAG_REREAD_SEC
    while unsure and time.monotonic() < deadline:
        time.sleep(_TAG_REREAD_POLL_SEC)
        still = []
        for p in unsure:
            has = _read_tag(p, tag)
            if has:
                found.add(p)
            elif has is None:
                still.append(p)
        unsure = still
    return found


def _has_tag(pid: int, tag: str) -> bool:
    return pid in _tagged((pid,), tag)


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
        if (pid and (info.pgid == pid or info.sid == pid or p == pid)) or (scope and _in_scope(p, scope))
    }
    if tag:
        members |= _tagged((p for p in procs if p not in exclude and p not in members), tag)
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
    release_wait_sec: float = HOLDER_RELEASE_WAIT_SEC,
    log: Callable[[str], None] = logger.warning,
) -> list[Survivor]:
    """End every process the job left behind: SIGTERM, ``term_grace_sec``, SIGKILL.

    Returns the processes still alive ``kill_wait_sec`` after the SIGKILL (empty in the
    normal case, where the job left nothing behind and this costs one /proc scan). One that
    may still hold the device then gets until ``release_wait_sec`` after the SIGKILL to drop
    out of the holder scan before it is returned (spec 01 I67). Each process found is logged
    through ``log``. Never raises."""
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
            left = await _wait_gone(left, kill_wait_sec)
            # Alive after SIGKILL is not yet a leftover: an exit can sit in the driver's release
            # for seconds. Poll the holder scan, bounded, before the rescan names it (01 I67).
            holding = [p for p in left if _holds_device(p) is not False]
            if holding and release_wait_sec > kill_wait_sec:
                log(
                    f"REAP job {job_id}: pid {', '.join(map(str, holding))} still alive {kill_wait_sec:g}s "
                    f"after SIGKILL; waiting up to {release_wait_sec - kill_wait_sec:g}s more for it to release the device"
                )
                await device_holders.wait_device_released(holding, release_wait_sec - kill_wait_sec)
        # Re-scan: anything the job forked during the sweep is a survivor too.
        rescan = await asyncio.to_thread(job_processes, pid, scope, tag)
        # A process the first scan found stays the job's while it lives (_alive checks the
        # starttime): past exit_mm, stuck in the driver's release, its tag no longer reads.
        for p, i in found.items():
            rescan.setdefault(p, i)
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
# The job_id of a leftover the dead-chip path killed (spec 04 I77): "dead-chip:<chip,...>".
DEAD_CHIP_LABEL_PREFIX = "dead-chip:"
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
        owner = (
            f"killed holder of chip(s) {self.job_id[len(DEAD_CHIP_LABEL_PREFIX) :]}"
            if self.job_id.startswith(DEAD_CHIP_LABEL_PREFIX)
            else f"job {self.job_id}"
        )
        return (
            f"{owner} pid {self.pid} ({device_holders.username_for_uid(self.uid)}) "
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


# Spec 04 I77: how long one read of /proc/<pid>/maps may take. A process stuck in the kernel can
# hold its mmap lock, and a maps read waits on that lock, so the read runs on its own thread and an
# unanswered read counts as unreadable (fail closed: the holder is kept as stuck).
STUCK_MAPS_READ_SEC = 2.0
# The bounded reads still out, per pid. One stuck behind the mmap lock never returns, nor does its
# thread: while it is out, a further read of that pid starts no thread and counts as unanswered at
# once, so a long hold does not leave one thread behind per scan.
_outstanding_reads: dict[int, threading.Thread] = {}
_outstanding_lock = threading.Lock()


def _bounded_read(pid: int, name: str, timeout: float) -> Optional[str]:
    """``_read_leftover`` on a daemon thread, or None when it has not answered in ``timeout``, or at
    once while an earlier read of ``pid`` is still out (at most one reader thread per pid)."""
    box: list = []

    def read() -> None:
        try:
            box.append(_read_leftover(pid, name))
        finally:
            with _outstanding_lock:
                if _outstanding_reads.get(pid) is threading.current_thread():
                    del _outstanding_reads[pid]

    with _outstanding_lock:
        if pid in _outstanding_reads:
            return None
        t = threading.Thread(target=read, daemon=True, name=f"bounded-read-{pid}")
        _outstanding_reads[pid] = t
        t.start()
    t.join(timeout)
    return box[0] if box else None


def device_refs(pid: int, bdfs: Iterable[str], *, maps_timeout: float = STUCK_MAPS_READ_SEC) -> Optional[list[str]]:
    """What of the device ``pid`` still references (spec 04 I77): open fds on a /dev/tenstorrent node
    and mappings of a device node or of one of ``bdfs``' PCI BARs (``.../<bdf>/resource<n>``),
    deleted ones included. [] when both fds and maps were read and name nothing; None when the fds
    name nothing and the maps could not be read or did not answer in time (a BAR mapping behind it
    cannot be ruled out: the caller fails closed)."""
    bars = [f"/{b}/resource" for b in bdfs if b]
    node = device_holders.DEVICE_DIR + "/"
    refs: list[str] = []
    try:
        fds = os.listdir(f"{LEFTOVER_PROC_DIR}/{pid}/fd")
    except OSError:
        fds = []
    for fd in fds:
        try:
            target = os.readlink(f"{LEFTOVER_PROC_DIR}/{pid}/fd/{fd}")
        except OSError:
            continue
        if target.startswith(node) or any(b in target for b in bars):
            refs.append(f"fd {fd} -> {target}")
    maps = _bounded_read(pid, "maps", maps_timeout)
    if maps is None:
        return refs or None
    for line in maps.splitlines():
        parts = line.split(None, 5)
        path = parts[5].strip() if len(parts) == 6 else ""
        if path and (path.startswith(node) or any(b in path for b in bars)) and f"map {path}" not in refs:
            refs.append(f"map {path}")
    return refs


def still_stuck(pid: int, starttime: str) -> bool:
    """Whether ``pid`` (the process with ``starttime``, when given) is still stuck in the kernel: state
    D or SIGKILL pending. /proc stat and status only: no maps read, so it never waits on a lock."""
    state, now = _leftover_state(pid)
    if state in ("?", "Z", "X") or (starttime and now != starttime):
        return False
    return state == "D" or bool(_sigkill_pending(pid))


def stuck_device_holder(pid: int, label: str, uid: int, bdfs: Iterable[str]) -> Optional[ReapedLeftover]:
    """``pid`` as a leftover named ``label`` when it survived its SIGKILL stuck in the kernel with a
    reference on the device (spec 04 I77), else None. Stuck: state D, or SIGKILL queued and not yet
    acted on. A zombie or a gone process holds nothing. Refs that cannot be read count as held."""
    state, _ = _leftover_state(pid)
    if state in ("?", "Z", "X"):
        return None
    pending = _sigkill_pending(pid)
    if state != "D" and not pending:
        return None
    refs = device_refs(pid, bdfs)
    if refs == []:
        return None
    wchan = (_read_leftover(pid, "wchan") or "").strip() or "?"
    return ReapedLeftover(job_id=label, pid=pid, uid=uid, state=state, wchan=wchan, sigkill_pending=pending)
