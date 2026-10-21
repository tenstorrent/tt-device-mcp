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
an unseen reader is the one the scan exists to remove.
"""

from __future__ import annotations

import asyncio
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
)
from tt_device_mcp.device_holders import SCOPE_PROC_DIR, DeviceHolder, is_tenant
from tt_device_mcp.peercred import username_for_uid

RESET_SCOPE_PREFIX = "ttdev-reset-"

# Pids of resets the broker launched outside a reset scope (the no-systemd local reset), live while
# they run. A holder that is one of them, or below one, is a reset in flight, never a reader.
_reset_pids: set[int] = set()


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


async def clear_device_readers(
    *,
    tenants_block: bool,
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
) -> PreResetScan:
    """Scan the device holders and remove every reader before a reset (spec 04 I92). Never raises.

    ``tenants_block``: a tenant (``is_tenant``, or a process in ``protected`` or below one, which is
    the broker's running job) refuses the reset and nothing is signalled. An operator reset passes
    False: its own reset gate (04 I6/I7) already ruled on tenants, ``force`` included, and they are
    only recorded here. The broker itself and its process group are never signalled. A broker-internal
    holder (one the broker already killed, still mid-exit) is a reader whatever its uid.

    Before any signal, every chip still bound but off the bus (``offbus_chips``) is detached
    (``detach_chip``, the one in-flight PCI write, all within ``detach_timeout_sec``); if one fails or the deadline
    passes, nothing is signalled and the reset is refused (module docstring).

    ``reset_active`` (the mechanism's ``scope_active``) names a reset already running; with one, or
    with a holder ``holder_in_reset``, nothing is signalled and the reset is refused. A scan that
    raises proceeds (a scan bug must not stop a recovery; ``error`` records it). A scan left
    incomplete by a pid the driver names (after one rescan ``rescan_settle_sec`` later) refuses an
    automatic reset; an operator reset records it, its own gate having ruled (04 I6/I7). A gap no
    pid explains (an unprivileged /proc walk) is recorded only: no pid is there to signal or wait for.
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
        scan = device_holders.discount_broker_internal(scan)
        if scan.complete:
            return []
        pids = list(getattr(scan, "gap_pids", None) or [])
        return [why for why, pid in zip(scan.gaps, pids) if pid is not None]

    try:
        if reset_active is not None:
            unit = await loop.run_in_executor(None, reset_active)
            if unit:
                result.reset_in_flight = str(unit)
                result.seconds = round(clock() - t0, 3)
                return result
        # The executor, not asyncio.to_thread: a reset step's own seams may stand in for to_thread.
        scan = await loop.run_in_executor(None, device_holders.enumerate_device_holders)
        if _pid_gaps(scan):
            # A pid the driver names but /proc could not show is most often one mid-exit: look again.
            await asyncio.sleep(rescan_settle_sec)
            scan = await loop.run_in_executor(None, device_holders.enumerate_device_holders)
    except Exception as e:  # noqa: BLE001 - a scan bug must not stop a recovery
        result.error = str(e) or type(e).__name__
        result.seconds = round(clock() - t0, 3)
        return result
    result.scan_complete, result.source = scan.complete, scan.source
    result.gaps = _pid_gaps(scan)
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
        if not getattr(h, "scoped", False) and device_holders.is_broker_internal(h.pid):
            return False
        return is_tenant(h)

    readers: list[DeviceHolder] = []
    for h in scan.holders:
        if _is_self(h.pid):
            continue
        result.found.append(_describe(h))
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
