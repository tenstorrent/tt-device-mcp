# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Enumerate processes holding /dev/tenstorrent/* and gate device resets.

A reset is a board-level reset of *all* chips: resetting while another tenant
holds the device loses their run mid-op and can wedge the mesh. The reset gate
therefore refuses unless every current device holder belongs to the caller's
uid (resetting your own wedged holders is fine).

Holder enumeration is a best-effort open-fd scan via /proc. Full cross-user
visibility requires CAP_DAC_READ_SEARCH/root; without it we can only see our
own processes' fds. We degrade gracefully and report whether the scan was
complete so the gate can decide how much to trust it.
"""

import logging
import os
import signal
import time
from dataclasses import dataclass, field

from tt_device_mcp.peercred import username_for_uid

logger = logging.getLogger("tt-device-mcp")

DEVICE_DIR = "/dev/tenstorrent"
# tt-kmd's own per-device record of who holds it open. Preferred over the fd walk because it is
# world-readable and complete by construction; see _driver_holder_pids.
DRIVER_PROC_DIR = "/proc/driver/tenstorrent"

# uids below this are system/service accounts (root, and daemons like
# tt_telemetry_server which permanently hold the device). They are infrastructure,
# not competing tenants, and survive a board reset — so the reset gate ignores them.
# Only real logged-in users (uid >= this) count as foreign holders to protect.
MIN_TENANT_UID = 1000


@dataclass(frozen=True)
class DeviceHolder:
    """A process holding an open fd to a Tenstorrent device node."""

    pid: int
    uid: int

    @property
    def username(self) -> str:
        return username_for_uid(self.uid)


@dataclass
class HolderScan:
    """Result of a device-holder enumeration."""

    holders: list[DeviceHolder] = field(default_factory=list)
    # True if every holder that exists was enumerated and attributed. When False,
    # foreign-uid holders may exist that we simply could not see, and every unforced
    # caller must treat that as a tenant (spec 04 I6/I7).
    complete: bool = True
    # Which route answered: "driver" for tt-kmd's own record, "proc" for the fd walk. Recorded
    # because the two fail differently, and "the gate refused" is otherwise indistinguishable
    # between a device that is genuinely busy and a scan that had no privilege to look.
    source: str = "proc"

    def foreign_holders(self, caller_uid: int | None) -> list[DeviceHolder]:
        """Other real tenants holding the device — excludes the caller and system
        accounts (root/daemons like tt_telemetry_server, which aren't tenants).

        ``caller_uid=None`` is a caller with no peer identity (an HTTP reset): no
        holder can be claimed as its own, so every real tenant counts as foreign.
        """
        return [h for h in self.holders if h.uid != caller_uid and h.uid >= MIN_TENANT_UID]


def _proc_uid(pid: int) -> int | None:
    """Read the real uid that owns a /proc/<pid> directory."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _process_holds_device(pid: int) -> bool:
    """Return True if /proc/<pid> has an open fd to a Tenstorrent device node.

    Raises PermissionError when the fd directory cannot be read (other uid,
    no CAP_DAC_READ_SEARCH) so the caller can mark the scan incomplete.
    """
    fd_dir = f"/proc/{pid}/fd"
    for entry in os.listdir(fd_dir):  # may raise PermissionError / FileNotFoundError
        try:
            target = os.readlink(f"{fd_dir}/{entry}")
        except OSError:
            continue
        # A stale fd to a since-recreated node reads as "/dev/tenstorrent/N (deleted)".
        # It no longer touches live hardware, so it must not count as holding the device
        # (else a hung process keeps the reset gate shut forever after a node recreate).
        if target.endswith(" (deleted)"):
            continue
        if target.startswith(DEVICE_DIR + "/") or target == DEVICE_DIR:
            return True
    return False


def _iter_pids() -> list[int]:
    """List numeric pids under /proc."""
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def _driver_holder_pids() -> "tuple[set[int], bool] | None":
    """The pids tt-kmd itself reports holding a device, or None when it publishes no such record.

    ``/proc/driver/tenstorrent/<n>/pids`` is the driver's own answer to the question the fd walk
    below can only infer: one pid per line, no header, empty while the device is free. It is
    world-readable, which is the whole point — the walk needs CAP_DAC_READ_SEARCH to read another
    uid's fd table, so an unprivileged scan is blind to exactly the tenants the reset gate exists
    to protect, and fails closed for lack of privilege rather than for anything about the device.

    Returns ``(pids, complete)``. A process holding two devices is listed under both, so the pids
    are unioned. ``complete`` is False when some device's file could not be read at all: the
    driver knows of holders we could not enumerate, which is a real blind spot. None (rather than
    an empty result) when the directory or its per-device files are absent, so a host whose driver
    predates this interface falls back to the walk instead of reading "no holders".
    """
    try:
        indices = [name for name in os.listdir(DRIVER_PROC_DIR) if name.isdigit()]
    except OSError:
        return None
    if not indices:
        return None

    pids: set[int] = set()
    complete = True
    published = False
    for index in indices:
        try:
            with open(f"{DRIVER_PROC_DIR}/{index}/pids") as fh:
                raw = fh.read()
        except OSError:
            complete = False
            continue
        published = True
        for line in raw.split():
            if line.isdigit():
                pids.add(int(line))

    if not published:
        # Every per-device file was unreadable, so nothing distinguishes "the driver does not
        # publish this" from "we could not read it". Decline rather than report a blind scan the
        # gate would then fail closed on: the walk can still answer, and its blind spots are the
        # ones the gate's rules were written against.
        return None
    return pids, complete


def _scan_from_driver_pids(pids: "set[int]", complete: bool) -> HolderScan:
    """Attribute driver-reported holder pids to uids.

    The driver names the holders; ``/proc/<pid>`` names their owner, and stat on it is
    world-readable even where the fd table is not, so both halves work unprivileged.

    A pid the driver names but that holds no device node in our own view is not attributed. That
    combination means we are reading the driver's pids — always the host's — through a /proc in a
    different pid namespace, where the same number is an unrelated process. Charging its uid with
    holding the device would let the gate mistake a stranger for the caller's own holder, or name
    the wrong tenant in an audit.
    """
    scan = HolderScan(complete=complete, source="driver")
    for pid in sorted(pids):
        uid = _proc_uid(pid)
        if uid is None:
            # A holder the driver can see and we cannot: another pid namespace, or it exited
            # between the driver's read and ours. Either way it is unattributed, not absent.
            scan.complete = False
            continue
        try:
            confirmed = _process_holds_device(pid)
        except PermissionError:
            # The ordinary case for another uid's process: unconfirmable here, but the driver
            # already confirmed it, and the uid from stat is authoritative for this pid.
            confirmed = True
        except OSError:
            scan.complete = False
            continue
        if not confirmed:
            scan.complete = False
            continue
        scan.holders.append(DeviceHolder(pid=pid, uid=uid))
    return scan


def enumerate_device_holders() -> HolderScan:
    """Who currently holds a Tenstorrent device, by whichever route can actually answer.

    The driver's own record first (`_driver_holder_pids`); the /proc fd walk only when the driver
    publishes nothing. The two differ in what a blind spot means: the driver's list is complete by
    construction, while the walk is complete only for a caller privileged enough to read every
    process's fd table.
    """
    driver = _driver_holder_pids()
    if driver is not None:
        return _scan_from_driver_pids(*driver)
    return _scan_by_walking_proc()


def _scan_by_walking_proc() -> HolderScan:
    """Infer holders by walking every process's fd table.

    Best-effort: if we cannot read another process's fd table (it belongs to a
    different uid and we lack CAP_DAC_READ_SEARCH) we skip it and mark the scan
    incomplete rather than failing.
    """
    scan = HolderScan(source="proc")

    try:
        pids = _iter_pids()
    except OSError as exc:
        logger.warning("device-holder scan: cannot list /proc: %s", exc)
        scan.complete = False
        return scan

    blocked = 0
    for pid in pids:
        try:
            holds = _process_holds_device(pid)
        except PermissionError:
            # Another uid's process we cannot inspect -> visibility gap.
            blocked += 1
            scan.complete = False
            continue
        except FileNotFoundError:
            # Process exited mid-scan; ignore.
            continue
        except OSError:
            continue

        if holds:
            uid = _proc_uid(pid)
            if uid is None:
                scan.complete = False
                continue
            scan.holders.append(DeviceHolder(pid=pid, uid=uid))

    if blocked:
        logger.warning(
            "device-holder scan incomplete: %d processes unreadable "
            "(need CAP_DAC_READ_SEARCH/root for full cross-user visibility, or a tt-kmd that "
            "publishes %s)",
            blocked,
            DRIVER_PROC_DIR,
        )

    return scan


@dataclass
class ResetDecision:
    """Outcome of the reset gate."""

    allowed: bool
    reason: str
    foreign_holders: list[DeviceHolder] = field(default_factory=list)
    scan_complete: bool = True


def evaluate_reset_gate(
    caller_uid: int | None,
    scan: HolderScan,
    force: bool = False,
) -> ResetDecision:
    """Decide whether ``caller_uid`` may reset, given a holder scan.

    Allow iff no holder has a uid != caller AND the scan was complete. An
    incomplete scan (a cross-uid holder was unreadable) fails closed: it cannot
    prove the device is free of foreign holders, so it is denied unless
    ``force``. ``force=True`` overrides both foreign holders and a blind spot,
    but the decision still carries the foreign holders so the caller can log
    what it is about to nuke. Queued jobs are irrelevant here (not on the device).

    ``caller_uid=None`` is an anonymous caller with no peer identity (an HTTP
    reset on a privsep host): it owns no holder, so every real tenant is foreign
    and the same fail-closed rules apply — a reset over an unruled-out foreign
    holder is refused, not run un-scoped.
    """
    foreign = scan.foreign_holders(caller_uid)

    if force:
        return ResetDecision(
            allowed=True,
            reason=(
                "forced reset"
                + (f" over {len(foreign)} foreign holder(s)" if foreign else "")
                + ("; scan incomplete (foreign holders may be invisible)" if not scan.complete else "")
            ),
            foreign_holders=foreign,
            scan_complete=scan.complete,
        )

    if foreign:
        return ResetDecision(
            allowed=False,
            reason=f"{len(foreign)} foreign-uid holder(s) on device",
            foreign_holders=foreign,
            scan_complete=scan.complete,
        )

    if not scan.complete:
        # Fail closed: an unreadable cross-uid holder means we cannot rule out a
        # foreign tenant on the device. Refuse rather than reset over an
        # invisible holder; an explicit force still overrides.
        return ResetDecision(
            allowed=False,
            reason="holder scan incomplete; cannot rule out foreign holders (pass force to override)",
            foreign_holders=[],
            scan_complete=scan.complete,
        )

    return ResetDecision(
        allowed=True,
        reason="no foreign holders",
        foreign_holders=[],
        scan_complete=scan.complete,
    )


def _read_proc_starttime(pid: int) -> str | None:
    """Read field 22 (starttime, in clock ticks since boot) from /proc/<pid>/stat.

    A pid alone is not a stable process identity across the several-second grace window a
    reclaim waits between rounds: the original holder can exit and the kernel can hand its pid
    to an unrelated new process before the next rescan runs. starttime is fixed for the life of
    a process, so pid+starttime is what actually identifies "the same process" across rounds.

    Field 2 (comm) is parenthesized and may itself contain spaces or a ')' (a process can be
    renamed via prctl to nearly anything), so the line cannot be split on whitespace alone --
    split on the LAST ')' and index the remaining fields from there. Returns None if the field
    cannot be read (process gone, /proc unreadable, unexpected format) so a caller treats
    "unknown" as "not the same process" rather than assuming identity.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        # Fields 3.. begin right after the comm field's closing ')'; field N maps to index
        # N-3 in that remainder, so field 22 (starttime) is index 19.
        return raw.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


def _read_proc_ppid(pid: int) -> int | None:
    """Read field 4 (ppid) from /proc/<pid>/stat; None if the process is gone or unreadable.

    Same parsing rule as `_read_proc_starttime`: comm may contain ')' or spaces, so split on
    the last ')'. Field 4 is index 1 of the remainder.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        return int(raw.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


# Bound on the parent-chain walk: real process trees are a few levels deep, and the bound
# keeps a pathological or racing /proc from looping.
_MAX_PARENT_DEPTH = 64


def descends_from(pid: int, ancestor: int) -> bool:
    """True if ``pid`` is ``ancestor`` or its parent chain reaches ``ancestor``.

    Walks /proc/<pid>/stat ppid links up to pid 1, bounded by _MAX_PARENT_DEPTH. A process
    that exits mid-walk (or a chain that cannot be read) is not a descendant: a leftover
    that daemonized and was reparented away from ``ancestor`` does not count as its child.
    """
    for _ in range(_MAX_PARENT_DEPTH):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        ppid = _read_proc_ppid(pid)
        if ppid is None or ppid == pid:
            return False
        pid = ppid
    return False


@dataclass
class ReclaimResult:
    """What a reclaim signalled, and what refused to let go."""

    signalled: list[DeviceHolder] = field(default_factory=list)
    survivors: list[DeviceHolder] = field(default_factory=list)
    scan_complete: bool = True


def reclaim_foreign_holders(
    *,
    grace_sec: float = 10.0,
    kill=os.kill,
    sleep=time.sleep,
    rescan=None,
    getpid=os.getpid,
    getpgid=os.getpgid,
    read_starttime=None,
) -> ReclaimResult:
    """Signal every tenant process still holding the device, then re-scan.

    This does NOT relax the reset gate: it removes the tenant so the gate's ordinary rule
    (04 I6/I7) passes on its own. A survivor or a blind re-scan leaves the gate's fail-closed
    refusal exactly where it was — nothing here may be read as license to reset over a holder.
    Only a caller with the authority to declare the allocation over may run it (spec 05).

    Two exclusions are structural, enforced here regardless of what the scan reports:
    infrastructure below MIN_TENANT_UID (it survives a board reset and is not a competing
    tenant), and this process itself along with its own process group (a defensive belt in
    case a scan or a uid check upstream ever miscounted the broker as a holder — the caller
    already runs as this process, so signalling it would be self-inflicted). Neither the uid
    floor nor self-exclusion has any notion of "a job the broker submitted" — a broker-owned
    job runs as its submitter's uid under privsep and looks like any other tenant holder here.
    Keeping this reclaim off a live broker job is the in-flight guard's job (server.py), not
    this function's.
    """
    rescan = rescan or enumerate_device_holders
    # Late-bound the same way `rescan` is, not a direct default-argument reference: a default of
    # `read_starttime=_read_proc_starttime` would bind to that function object once, at import
    # time, and a test's `monkeypatch.setattr(device_holders, "_read_proc_starttime", ...)` would
    # never be seen by a caller that does not pass `read_starttime=` itself. Looking the name up
    # here, at call time, is what lets tests/conftest.py sandbox the default the same way it
    # already sandboxes `rescan`'s default (`enumerate_device_holders`) and `TT_DEV_DIR`.
    read_starttime = read_starttime or _read_proc_starttime
    own_pid = getpid()
    try:
        own_pgid = getpgid(own_pid)
    except OSError:
        own_pgid = None

    def _is_self(h: DeviceHolder) -> bool:
        if h.pid == own_pid:
            return True
        if own_pgid is None:
            return False
        try:
            return getpgid(h.pid) == own_pgid
        except OSError:
            return False  # pid already gone; not the broker's group either way

    scan = rescan()
    targets = [h for h in scan.holders if h.uid >= MIN_TENANT_UID and not _is_self(h)]
    if not targets:
        return ReclaimResult(signalled=[], survivors=[], scan_complete=scan.complete)

    # Captured once, when each target is first selected: the grace window before EVEN THE FIRST
    # signal (let alone between rounds) is long enough for the original process to exit and the
    # kernel to hand its pid to an unrelated new holder before the kill call runs. Every signal —
    # the first one included, see the per-signal revalidation below — must therefore confirm the
    # SAME starttime as this initial read, not just the same pid number. An unreadable starttime
    # here means this target can never be confirmed at all: it is never signalled, on round 1 or
    # any later round (fail-safe: treated as a stranger throughout, never falling back to
    # pid-only tracking).
    target_starttimes = {h.pid: read_starttime(h.pid) for h in targets}

    # `targets` narrows every round (a survivor of round N's signal is the only thing eligible
    # for round N+1's escalation) so that a bystander who opens the device mid-reclaim never
    # gets retargeted onto (see the note below). But that means `targets` at the end of the loop
    # is only the *last* round's residue — anyone who died to an earlier round's signal has
    # already dropped out of it. `signalled_by_pid` is the union: every pid this call actually
    # sent a signal to, across every round, so a pid that died to SIGTERM still shows up in the
    # result even though it is gone by the time SIGKILL's round runs.
    signalled_by_pid: dict[int, DeviceHolder] = {}
    vanished = 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for h in targets:
            # Revalidated immediately before EVERY signal, including the first — not just when
            # narrowing `targets` between rounds. The between-round check alone left the FIRST
            # round unguarded: every pid the initial scan selected was signalled unconditionally,
            # including one whose starttime read had already come back None at selection. An
            # unreadable starttime is supposed to be absolute ("never signal this identity again"),
            # not merely a bar to RE-matching after round 1 — reusing a target's OWN captured
            # starttime here (rather than skipping the check on round 1) is what makes it so: a
            # None value can never equal itself, so a target selected with no readable identity is
            # never signalled at all, and one whose pid was reused since selection fails the match
            # right here instead of only at the next round's rescan.
            expected_starttime = target_starttimes.get(h.pid)
            if expected_starttime is None or read_starttime(h.pid) != expected_starttime:
                continue
            try:
                kill(h.pid, sig)
            except ProcessLookupError:
                # A pid read from a scan and signalled later is a normal race, but nothing was
                # actually signalled -- the audit must not claim a kill that never happened.
                vanished += 1
                continue
            except PermissionError:
                logger.warning("device-holder reclaim: no permission to signal pid %d (uid %d)", h.pid, h.uid)
                continue
            # Recorded only once the kill call itself succeeded: an audit that claims a signal
            # was sent to a process that was already gone, or that we lacked permission to
            # touch, is worse than one that under-reports, since it asserts a broker action that
            # never occurred.
            signalled_by_pid[h.pid] = h
        if grace_sec:
            sleep(grace_sec)
        after = rescan()
        still = [h for h in after.holders if h.uid >= MIN_TENANT_UID]
        if not still:
            return ReclaimResult(signalled=list(signalled_by_pid.values()), survivors=[], scan_complete=after.complete)
        # No `or still` fallback: a pid that shows up here but was never a target is a NEW
        # holder that opened the device after the reclaim began (a different tenant's job, not
        # a straggler of the one that just ended). Retargeting the escalation onto it would
        # SIGKILL a bystander with no SIGTERM and no authority over its allocation. Matching pid
        # alone is not enough either -- the kernel can hand a dead target's pid to exactly such a
        # bystander during the grace window -- so a match also requires the SAME starttime as
        # when this target was first selected. Anything that fails to match (including a target
        # whose starttime could not be read at all) survives this reclaim untouched and is
        # reported as a survivor below.
        targets = [
            h
            for h in still
            if target_starttimes.get(h.pid) is not None and read_starttime(h.pid) == target_starttimes[h.pid]
        ]

    final = rescan()
    if vanished:
        logger.debug("device-holder reclaim: %d signal(s) targeted an already-gone process", vanished)
    return ReclaimResult(
        signalled=list(signalled_by_pid.values()),
        survivors=[h for h in final.holders if h.uid >= MIN_TENANT_UID],
        scan_complete=final.complete,
    )
