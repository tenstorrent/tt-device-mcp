# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Clear the device of readers before a reset (spec 04 I92).

A reset takes chips off the bus. A process that still maps one and reads it then issues a
non-posted read that gets no completion: the core stalls to the root port's completion timeout,
and under firmware-first error handling every such read is a system error the firmware handles.
A reader in a loop makes thousands of them, and that is how a reset ends in a host reset. The
broker quiesces its own pollers for every reset (``reset_with_quiesce``) and fences a reaped job's
leftover (01 I16), but nothing else looked for a reader first: a telemetry or tt-smi run outside
the broker, or a stale process, kept its mappings through the reset.

So before every reset: scan the holders. A tenant blocks an automatic reset (it is named, never
signalled). Every other holder is a reader: SIGTERM, up to ``PRE_RESET_TERM_GRACE_SEC`` for it to
let go, SIGKILL, then up to ``HOLDER_RELEASE_WAIT_SEC`` for the driver's release (01 I67). One
still holding after that, stuck in the kernel, is an unkillable holder (04 I70); any other refuses
the reset with its name. Nothing holding costs one scan.

A chip already off the bus at scan time is the exception to "kill first". When a holder's fd on a
chip closes, the driver itself sends that chip a power-state message unless the chip's endpoint is
detached (removed from the kernel, the only operation that marks it so). Killing a reader of an
off-bus chip that is still bound would make the kernel read that chip: the very read the scan
exists to prevent. So such chips are detached first (``isolate_chip``, the broker's own valve), and
only then are readers signalled. If one cannot be detached, nothing is signalled and the reset is
refused: the hold stays and the bounded recovery (the 10-min ladder, then the power cycle) runs.
The detach is a sysfs write that can block in the kernel, so it runs off the event loop as the one
in-flight PCI write (spec 04 I76) with one deadline (``PRE_RESET_DETACH_TIMEOUT_SEC``) for all the
chips; a detach that does not return in time counts as not detached and latches PCI-wedged.

Two more refusals come before any signal. A holder inside a reset (a process in a reset scope, or
one the broker launched as a reset and registered here) is never signalled: killing a reset partway
through the chips is worse than any reader, so the reset is refused while another is in flight. And
an automatic reset is refused while the scan cannot see a holder the driver names (after one rescan):
an unseen reader is the one the scan exists to remove. A zombie the driver still names (a killed
job's leader) is classified rather than read as unseen. One whose whole thread group has exited holds
no fd and no mapping (``device_holders.zombie_info``), so it is no holder and the reset goes ahead at
once, unless the file it opened is still open in another process. A zombie leader whose live threads
show a device fd or mapping is a holder: refused. One whose live threads cannot be read either way is
rescanned until it settles or the caller's bound passes, once per zombie: the release may be running
in one of those threads, and a killed job's exit is often that close to allowing the reset.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from tt_device_mcp import device_holders, job_reap
from tt_device_mcp.constants import (
    HOLDER_RELEASE_WAIT_SEC,
    PRE_RESET_DETACH_TIMEOUT_SEC,
    PRE_RESET_RESCAN_SETTLE_SEC,
    PRE_RESET_TERM_GRACE_SEC,
    PRE_RESET_ZOMBIE_WAIT_SEC,
)
from tt_device_mcp.device_holders import SCOPE_PROC_DIR, DeviceHolder, is_tenant
from tt_device_mcp.holder_identity import holder_identity, read_holder_identities
from tt_device_mcp.peercred import username_for_uid

RESET_SCOPE_PREFIX = "ttdev-reset-"

# Set while the broker runs the ladder or sweep that comes right before a power cycle it may request
# (spec 04 I103): a holder outside every broker job or exec scope is then a reader to clear, never a
# tenant that refuses the reset. The power cycle would end that process anyway; only a broker job holds.
_foreign_holders_yield: contextvars.ContextVar[bool] = contextvars.ContextVar("foreign_holders_yield", default=False)


@contextlib.contextmanager
def foreign_holders_yield(on: bool = True):
    """Run the block with foreign (unscoped) holders yielding to automatic resets (spec 04 I103)."""
    token = _foreign_holders_yield.set(bool(on) or _foreign_holders_yield.get())
    try:
        yield
    finally:
        _foreign_holders_yield.reset(token)


def foreign_holders_yielding() -> bool:
    """Whether the current task runs a pre-power-cycle ladder or sweep (:func:`foreign_holders_yield`)."""
    return _foreign_holders_yield.get()


# Pids of resets the broker launched outside a reset scope (the no-systemd local reset), live while
# they run. A holder that is one of them, or below one, is a reset in flight, never a reader.
_reset_pids: set[int] = set()

# Zombies (pid, starttime) the scan already waited for: each costs its wait once, so a zombie that
# outlives it adds nothing to the later rungs of the bounded recovery (the 10-min ladder included).
_zombies_waited: set[tuple] = set()


def note_reset_pid(pid: int) -> None:
    _reset_pids.add(int(pid))


def forget_reset_pid(pid: int) -> None:
    _reset_pids.discard(int(pid))


def in_reset(pid: int) -> bool:
    """True when ``pid`` is part of a reset in flight: in a reset scope, or a registered reset or its
    child. /proc/<pid>/cgroup is world-readable, so only an exited pid reads as False."""
    if any(device_holders.descends_from(pid, p) for p in list(_reset_pids)):
        return True
    try:
        with open(f"{SCOPE_PROC_DIR}/{pid}/cgroup") as f:
            raw = f.read()
    except OSError:
        return False
    for line in raw.splitlines():
        for part in line.rsplit(":", 1)[-1].split("/"):
            if part.endswith(".scope") and part.startswith(RESET_SCOPE_PREFIX):
                return True
    return False


def _describe(h: DeviceHolder, state: Optional[str] = None, **extra) -> dict:
    out = {
        "pid": h.pid,
        "uid": h.uid,
        "user": username_for_uid(h.uid),
        "comm": device_holders.holder_comm(h.pid),
        "scoped": bool(getattr(h, "scoped", False)),
    }
    if state is not None:
        out["state"] = state
    out.update(extra)
    return out


@dataclass
class PreResetScan:
    """What one pre-reset scan found and did. Lists hold ``_describe`` dicts."""

    found: list = field(default_factory=list)  # every holder seen, the broker itself excluded
    # ``holder_identity`` of each found holder, read before any signal (logging only)
    identities: list = field(default_factory=list)
    tenants: list = field(default_factory=list)  # tenants (never signalled)
    killed: list = field(default_factory=list)  # readers signalled, with the last signal sent
    unkillable: list = field(default_factory=list)  # readers left holding, stuck in the kernel
    survivors: list = field(default_factory=list)  # readers left holding for any other reason
    detached: list = field(default_factory=list)  # off-bus chips detached before any signal
    undetached: list = field(default_factory=list)  # off-bus chips that could not be: nothing signalled
    readers_left: list = field(default_factory=list)  # readers left unsignalled (off-bus refusal)
    detach_timed_out: bool = False  # the detach deadline passed before every off-bus chip was detached
    reset_in_flight: str = ""  # a reset already running (its unit, or the holder that is one)
    gaps: list = field(default_factory=list)  # holders the driver names that the scan could not read
    zombies: list = field(default_factory=list)  # zombie gap pids, classified (ZombieInfo.row, waited_sec)
    tenants_block: bool = True  # whether a tenant refuses this reset (automatic resets)
    scan_complete: bool = True
    source: str = "proc"
    seconds: float = 0.0
    error: str = ""

    @property
    def outcome(self) -> str:
        if self.error:
            return "scan_error"
        if self.reset_in_flight:
            return "reset_in_flight"
        if self.tenants and self.tenants_block:
            return "tenant_blocked"
        if self.gaps and self.tenants_block:
            return "scan_incomplete"
        if self.undetached:
            return "offbus_undetached"
        if self.unkillable:
            return "unkillable"
        if self.survivors:
            return "survivor"
        if self.killed:
            return "killed"
        return "clear"

    def refusal(self) -> str:
        """Why the reset must not run, or '' when it may."""

        def names(rows: list) -> str:
            return "; ".join(f"pid {r['pid']} ({r['user']}) comm {r['comm']} state {r.get('state', '?')}" for r in rows)

        if self.reset_in_flight:
            return f"a reset is already in flight ({self.reset_in_flight}); its processes are never signalled"
        if self.tenants and self.tenants_block:
            return f"a tenant holds the device: {names(self.tenants)}; no automatic reset runs under a tenant"
        if self.gaps and self.tenants_block:
            return (
                f"the holder scan could not see every holder ({'; '.join(self.gaps)}); "
                "no automatic reset runs past a reader it cannot see"
            )
        if self.undetached:
            return (
                f"chips {', '.join(self.undetached)} are off the bus and could not be detached from the "
                f"driver, so their readers were not killed (closing them would make the driver touch "
                f"those chips): {names(self.readers_left)}"
            )
        if self.unkillable:
            return (
                f"a device reader survived SIGKILL, stuck in the kernel: {names(self.unkillable)}; "
                "no reset runs while it maps the device, only a host reboot clears it"
            )
        if self.survivors:
            return f"a device reader would not let go: {names(self.survivors)}; no reset runs while it maps the device"
        return ""


def _offbus_bound_chips() -> list:
    """Chips still bound to the driver whose ARC heartbeat reads all-ones: off the bus, not detached.
    A chip the broker already marked lost is not read again (spec 03 I66); it reports all-ones."""
    from tt_device_mcp.health.monitors.heartbeat import dead_chips, read_heartbeats

    return dead_chips(read_heartbeats())


def _detach_chip(idx: str) -> bool:
    from tt_device_mcp.health.monitors.pci import isolate_chip

    return isolate_chip(idx)


def _unkillable(pid: int) -> bool:
    state, _ = job_reap._leftover_state(pid)
    return state == "D" or bool(job_reap._sigkill_pending(pid))


def pid_gap_rows(scan) -> list:
    """(why, pid) for each gap of ``scan`` on a pid the driver names, the broker's own work discounted."""
    scan = device_holders.discount_broker_internal(scan)
    if scan.complete:
        return []
    pids = list(getattr(scan, "gap_pids", None) or [])
    return [(why, pid) for why, pid in zip(scan.gaps, pids) if pid is not None]


@dataclass
class GapVerdicts:
    """What ``classify_gap_pids`` made of the pids a holder scan could not attribute."""

    left: list = field(default_factory=list)  # pids that still count as holders
    unknown: list = field(default_factory=list)  # the left ones: zombies whose live threads cannot settle it
    cleared: list = field(default_factory=list)  # exited zombies: no holder
    zombies: dict = field(default_factory=dict)  # pid -> ZombieInfo.row(), verdict as ruled here


def classify_gap_pids(
    pids: Iterable[int],
    named: Iterable[int],
    zombie_of: Optional[Callable[[int], Optional[device_holders.ZombieInfo]]] = None,
    users_outside: Optional[Callable[[set], tuple]] = None,
) -> GapVerdicts:
    """Spec 04 I92: settle each pid the driver names that the scan could not show holding the device.
    A zombie whose whole thread group has exited is no holder (``cleared``), unless ``users_outside``
    finds the device open in a process outside ``named`` (every pid the driver names) or cannot walk
    every process: then its verdict is ``open_elsewhere`` or ``walk_incomplete`` and it stays. A zombie
    leader whose live threads hold, or cannot show whether they hold (``unknown``), stays; so does any
    pid that is no zombie (live, or gone from /proc)."""
    zombie_of = zombie_of or device_holders.zombie_info
    users_outside = users_outside or device_holders.device_users_outside
    pids = list(pids)
    out = GapVerdicts()
    exited = []
    for pid in pids:
        z = zombie_of(pid)
        if z is None:
            out.left.append(pid)
            continue
        out.zombies[pid] = z.row()
        if z.verdict == "exited":
            exited.append(pid)
        else:
            out.left.append(pid)
            if z.verdict == "unknown":
                out.unknown.append(pid)
    if exited:
        others, complete = users_outside(set(named) | set(pids))
        for pid in exited:
            if others or not complete:
                out.zombies[pid]["verdict"] = "open_elsewhere" if others else "walk_incomplete"
                out.zombies[pid]["open_in"] = list(others)
                out.left.append(pid)
            else:
                out.cleared.append(pid)
    return out


async def clear_device_readers(
    *,
    tenants_block: bool,
    foreign_yield: bool = False,
    protected: Iterable[int] = (),
    grace_sec: float = PRE_RESET_TERM_GRACE_SEC,
    release_sec: float = HOLDER_RELEASE_WAIT_SEC,
    kill: Callable[[int, int], None] = os.kill,
    getpid: Callable[[], int] = os.getpid,
    getpgid: Callable[[int], int] = os.getpgid,
    read_starttime: Optional[Callable[[int], Optional[str]]] = None,
    unkillable: Optional[Callable[[int], bool]] = None,
    clock: Callable[[], float] = time.monotonic,
    offbus_chips: Optional[Callable[[], list]] = None,
    detach_chip: Optional[Callable[[str], bool]] = None,
    detach_timeout_sec: float = PRE_RESET_DETACH_TIMEOUT_SEC,
    reset_active: Optional[Callable[[], Optional[str]]] = None,
    holder_in_reset: Optional[Callable[[int], bool]] = None,
    rescan_settle_sec: float = PRE_RESET_RESCAN_SETTLE_SEC,
    zombie_wait_sec: float = PRE_RESET_ZOMBIE_WAIT_SEC,
    zombie_of: Optional[Callable[[int], Optional[device_holders.ZombieInfo]]] = None,
    users_outside: Optional[Callable[[set], tuple]] = None,
) -> PreResetScan:
    """Scan the device holders and remove every reader before a reset (spec 04 I92). Never raises.

    ``tenants_block``: a tenant (``is_tenant``, or a process in ``protected`` or below one, which is
    the broker's running job) refuses the reset and nothing is signalled. An operator reset passes
    False: its own reset gate (04 I6/I7) already ruled on tenants, ``force`` included, and they are
    only recorded here. The broker itself and its process group are never signalled. A broker-internal
    holder (one the broker already killed, still mid-exit) is a reader whatever its uid.

    ``foreign_yield`` (the ladder and sweep before a power cycle, spec 04 I103): a tenant outside every
    broker job or exec scope, and not in ``protected``, is a reader like any other and is cleared; only a
    broker job's process still refuses the reset.

    Before any signal, every chip still bound but off the bus (``offbus_chips``) is detached
    (``detach_chip``, the one in-flight PCI write, all within ``detach_timeout_sec``); if one fails or the deadline
    passes, nothing is signalled and the reset is refused (module docstring).

    ``reset_active`` (the mechanism's ``scope_active``) names a reset already running; with one, or
    with a holder ``holder_in_reset``, nothing is signalled and the reset is refused. A scan that
    raises proceeds (a scan bug must not stop a recovery; ``error`` records it). A scan left
    incomplete by a pid the driver names (after one rescan ``rescan_settle_sec`` later) refuses an
    automatic reset; an operator reset records it, its own gate having ruled (04 I6/I7). A gap no
    pid explains (an unprivileged /proc walk) is recorded only: no pid is there to signal or wait for.
    A gap whose pid is a zombie (``zombie_of``) is classified first, with no wait: one whose whole
    thread group has exited is no holder and leaves the gaps, unless ``users_outside`` finds the device
    open in a process the driver does not name or cannot walk every process (then it stays a gap). A
    zombie leader whose live threads show a device fd or mapping stays a gap: refused after the one
    rescan. When every gap left is a zombie whose live threads cannot settle it, the scan looks again
    every ``rescan_settle_sec`` until they do or ``zombie_wait_sec`` passes (the caller's bound: an
    automatic reset's ends before the hold's 10-min ladder), once per (pid, start time). A gap on a live
    or unreadable pid gets the one rescan. Nothing is signalled for any of this.
    """
    read_starttime = read_starttime or device_holders._read_proc_starttime
    unkillable = unkillable or _unkillable
    offbus_chips = offbus_chips or _offbus_bound_chips
    detach_chip = detach_chip or _detach_chip
    holder_in_reset = holder_in_reset or in_reset
    t0 = clock()
    result = PreResetScan(tenants_block=tenants_block)
    loop = asyncio.get_running_loop()

    def _pid_gaps(scan) -> list:
        return [why for why, _ in pid_gap_rows(scan)]

    cleared: set = set()  # exited zombies: no holder, out of the gaps
    zombie_rows: dict = {}

    def _classify(scan) -> tuple:
        """Settle each gap pid that is a zombie (``classify_gap_pids``). Returns (gap pids left, the left
        ones that are unknown zombies)."""
        v = classify_gap_pids(
            (pid for _, pid in pid_gap_rows(scan)), (h.pid for h in scan.holders), zombie_of, users_outside
        )
        for pid, row in v.zombies.items():
            zombie_rows[pid] = {**row, "waited_sec": 0.0}
        cleared.update(v.cleared)
        return v.left, v.unknown

    try:
        if reset_active is not None:
            unit = await loop.run_in_executor(None, reset_active)
            if unit:
                result.reset_in_flight = str(unit)
                result.seconds = round(clock() - t0, 3)
                return result
        # The executor, not asyncio.to_thread: a reset step's own seams may stand in for to_thread.
        scan = await loop.run_in_executor(None, device_holders.enumerate_device_holders)
        left, unknown = await loop.run_in_executor(None, _classify, scan) if _pid_gaps(scan) else ([], [])
        if left:
            # A pid the driver names but /proc could not show is most often one mid-exit: look again.
            await asyncio.sleep(rescan_settle_sec)
            scan = await loop.run_in_executor(None, device_holders.enumerate_device_holders)
            cleared.clear()
            left, unknown = await loop.run_in_executor(None, _classify, scan)
            waited: set = set()
            deadline = clock() + zombie_wait_sec
            started = clock()
            while left and len(unknown) == len(left) and clock() < deadline:
                # Every gap left is a zombie whose live threads cannot settle it: its release may be
                # running in one of them. Look again, bounded, once per zombie.
                keys = {(pid, read_starttime(pid)) for pid in unknown}
                if keys & _zombies_waited:
                    break
                waited |= keys
                await asyncio.sleep(rescan_settle_sec)
                scan = await loop.run_in_executor(None, device_holders.enumerate_device_holders)
                cleared.clear()
                left, unknown = await loop.run_in_executor(None, _classify, scan)
            for row in zombie_rows.values():
                row["waited_sec"] = round(clock() - started, 3) if waited else 0.0
            if len(_zombies_waited) > 256:
                _zombies_waited.clear()
            _zombies_waited.update(waited)
    except Exception as e:  # noqa: BLE001 - a scan bug must not stop a recovery
        result.error = str(e) or type(e).__name__
        result.seconds = round(clock() - t0, 3)
        return result
    result.scan_complete, result.source = scan.complete, scan.source
    result.gaps = [why for why, pid in pid_gap_rows(scan) if pid not in cleared]
    result.zombies = list(zombie_rows.values())
    own_pid = getpid()
    try:
        own_pgid = getpgid(own_pid)
    except OSError:
        own_pgid = None
    protected = [int(p) for p in protected]

    def _is_self(pid: int) -> bool:
        if pid == own_pid:
            return True
        try:
            return own_pgid is not None and getpgid(pid) == own_pgid
        except OSError:
            return False

    def _is_tenant(h: DeviceHolder) -> bool:
        if h.pid in protected or any(device_holders.descends_from(h.pid, p) for p in protected):
            return True
        if not getattr(h, "scoped", False) and (foreign_yield or device_holders.is_broker_internal(h.pid)):
            return False
        return is_tenant(h)

    others = [h for h in scan.holders if not _is_self(h.pid)]
    # Logging only, before any signal: off the loop, all within one fixed bound (a /proc read of a
    # holder stuck in the kernel can block); one left unread at the bound changes nothing here.
    pids = list(dict.fromkeys(h.pid for h in others))
    rows = await loop.run_in_executor(None, lambda: read_holder_identities(pids, read=holder_identity)) if pids else []
    by_pid = dict(zip(pids, rows))
    readers: list[DeviceHolder] = []
    for h in others:
        result.found.append(_describe(h))
        result.identities.append(by_pid[h.pid])
        if holder_in_reset(h.pid):
            result.reset_in_flight = f"pid {h.pid} comm {device_holders.holder_comm(h.pid)}"
        elif _is_tenant(h):
            result.tenants.append(_describe(h))
        else:
            readers.append(h)
    if result.reset_in_flight or (result.tenants and tenants_block) or (result.gaps and tenants_block) or not readers:
        result.seconds = round(clock() - t0, 3)
        return result

    # Readers are only killed while every chip they may read is on the bus or detached (tt-kmd's
    # release path sends a bound chip a power-state message the moment the fd closes).
    try:
        off = [str(i) for i in await loop.run_in_executor(None, offbus_chips)]
    except Exception:  # noqa: BLE001 - unreadable: treat as on the bus, as the reset's own gates do
        off = []
    # A sysfs remove can block in the kernel: off the loop, one deadline for all the chips, each the
    # one in-flight PCI write (spec 04 I76). A detach still running at the deadline, or refused behind
    # an earlier blocked write, latches PCI-wedged; this reset is refused either way.
    from tt_device_mcp.health.pci_writer import PCI_WRITER

    deadline = clock() + detach_timeout_sec
    for idx in off:
        left_sec = deadline - clock()
        ok = False
        if left_sec > 0 and not result.detach_timed_out:
            try:
                ok = bool(await PCI_WRITER.write(f"pre-reset detach of chip {idx}", detach_chip, idx, timeout=left_sec))
            except asyncio.TimeoutError:  # PciWedged included
                result.detach_timed_out = True
            except Exception:  # noqa: BLE001
                ok = False
        else:
            result.detach_timed_out = True
        (result.detached if ok else result.undetached).append(idx)
    if result.undetached:
        result.readers_left = [_describe(h, device_holders.holder_state(h.pid)) for h in readers]
        result.seconds = round(clock() - t0, 3)
        return result

    # pid + starttime is the identity each signal re-checks: across the grace window the kernel can
    # hand a gone reader's pid to a new process, which must never get its SIGKILL.
    starts = {h.pid: read_starttime(h.pid) for h in readers}
    last_sig: dict[int, str] = {}

    def _signal(h: DeviceHolder, sig: int) -> None:
        start = starts.get(h.pid)
        if start is None or read_starttime(h.pid) != start:
            return
        try:
            kill(h.pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            return
        last_sig[h.pid] = signal.Signals(sig).name

    for h in readers:
        # Mid-exit after our signal, the driver can still name it with its fd table gone; the next
        # scan must not read that as a tenant (spec 04 I59).
        device_holders.note_broker_internal(h.pid)
        _signal(h, signal.SIGTERM)
    left = await device_holders.wait_device_released([h.pid for h in readers], grace_sec)
    if left:
        for h in readers:
            if h.pid in left:
                _signal(h, signal.SIGKILL)
        left = await device_holders.wait_device_released(left, release_sec)
    for h in readers:
        if h.pid in last_sig:
            result.killed.append(_describe(h, signal=last_sig[h.pid], released=h.pid not in left))
        if h.pid not in left:
            continue
        state = device_holders.holder_state(h.pid)
        if unkillable(h.pid):
            result.unkillable.append(_describe(h, state, starttime=starts.get(h.pid) or ""))
        else:
            result.survivors.append(_describe(h, state, signalled=h.pid in last_sig))
    result.seconds = round(clock() - t0, 3)
    return result
