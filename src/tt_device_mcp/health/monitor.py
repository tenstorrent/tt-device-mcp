# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""HealthMonitor: the aggregate that owns one probe pass and the readings blackboard.

Every check that decides whether the mesh is usable — the tt-smi enumeration snapshot, the
fabric traffic pass, the passive eth-core heartbeat read, and the board-type cache the reset
command needs — used to live as free functions and module globals on ``server.py``. That left
their state (the board-type cache, the last-real-fabric-verdict latch, the in-flight fabric
check handle a dropping chip must be able to kill) scattered across the module with no single
owner. ``HealthMonitor`` is that owner: constructed once as a module-level singleton (see
``server.health_monitor``), never per gate pass — a fresh instance would lose the fabric latch
that stops a recovered-on-ARC hold from lifting onto an unverified fabric.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from datetime import datetime
from typing import Callable, Optional

from tt_device_mcp import aio, alert, metrics
from tt_device_mcp.constants import (
    ETH_POST_JOB_TIMEOUT_SEC,
    ETH_REAP_SEC,
    FABRIC_CHECK_CANNOT_CHECK_RC,
    FABRIC_CHECK_HARDWARE_FAULT_RC,
    FABRIC_CHECK_TIMEOUT_SEC,
)
from tt_device_mcp.device_holders import note_broker_internal
from tt_device_mcp.health import chip_state
from tt_device_mcp.health.aiclk_ceiling import CEILING
from tt_device_mcp.health.core import HealthState, Observation, Verdict
from tt_device_mcp.health.evidence import capture_incident, health_dir, health_event
from tt_device_mcp.health.monitors import eth, fabric, hostpci, pci
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.shutdown import CheckInterruptedByShutdown, interrupted_by_shutdown

# How much of a fabric run's output the action-log row keeps for a run that got no healthy verdict:
# all of it, since the validator's first error line (the cause) sits near the head and a wrapper's
# verdict at the tail. Only past this ceiling, so a chatty validator cannot fill the disk, is the
# middle cut, keeping both ends.
ACTION_LOG_OUTPUT_CHARS = 256 * 1024


def _action_log_output(text: str) -> str:
    if len(text) <= ACTION_LOG_OUTPUT_CHARS:
        return text
    half = ACTION_LOG_OUTPUT_CHARS // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} chars cut ...]\n{text[-half:]}"


# How much of a fabric run that reached no verdict (a 77) its incident bundle keeps: the tail, where
# the validator's abort and the wrapper's verdict sit. The action-log row rotates with the jobs list;
# the bundle rotates with the incidents (MAX_INCIDENTS), so the cause outlives the row.
FABRIC_SKIP_OUTPUT_TAIL_BYTES = 64 * 1024
# A no-verdict run with the same reason as the last one bundled this recently reuses that bundle:
# the idle relift reruns a 77-only hold, and a bundle per rerun would rotate the bundles that explain
# a reboot out of the bounded set.
FABRIC_SKIP_INCIDENT_REUSE_SEC = 3600.0


def _output_tail(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    return f"[... {len(raw) - max_bytes} bytes cut ...]\n" + raw[-max_bytes:].decode("utf-8", "replace")


ASIC_BUS_FILE = "fabric_ok_asic_bus.json"
# The validator's tray id is the PCI bus's top two bits (its UBB_TRAY_BUS_IDS).
_TRAY_BY_BUS_GROUP = {0x00: 1, 0x40: 2, 0xC0: 3, 0x80: 4}


def _asic_bus_map() -> dict[str, int]:
    """``{tt_asic_id: PCI bus}`` for every chip sysfs names both for. A file read per chip."""
    out: dict[str, int] = {}
    for rec in pci.chip_snapshot().values():
        bus = pci.pci_bus_number(rec.get("pci", ""))
        if rec.get("tt_asic_id") is not None and bus is not None:
            out[str(rec["tt_asic_id"])] = bus
    return out


def _save_asic_bus_map() -> None:
    """Remember which bus each chip sat on at the last clean fabric pass; kept across reboots so a
    later mismatch can name the trays that moved."""
    m = _asic_bus_map()
    if not m:
        return
    try:
        p = health_dir() / ASIC_BUS_FILE
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(m))
        os.replace(tmp, p)
    except OSError:
        pass


def _load_asic_bus_map() -> dict[str, int]:
    try:
        data = json.loads((health_dir() / ASIC_BUS_FILE).read_text())
        return {str(k): int(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


# A topology mismatch remembered per chip-to-bus map: while the map is unchanged, the descriptor
# run that would only throw the same mismatch again (~19 s) is skipped and the gate goes straight
# to the traffic-only rerun. Spec 03 I45.
TOPOLOGY_CACHE_FILE = "fabric_topology_mismatch.json"


def _write_json(name: str, data: dict) -> None:
    try:
        p = health_dir() / name
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, p)
    except OSError:
        pass  # state we cannot persist must not break the gate


def _remember_topology_mismatch(desc: str, marker: str) -> None:
    m = _asic_bus_map()
    if m:
        _write_json(TOPOLOGY_CACHE_FILE, {"desc": desc, "asic_bus": m, "marker": marker})


def _forget_topology_mismatch() -> None:
    try:
        (health_dir() / TOPOLOGY_CACHE_FILE).unlink()
    except OSError:
        pass


# Written when a fabric check starts and removed when it returns, so a broker that is stopped or
# killed mid-check (systemd SIGTERMs the unit's cgroup, the validator with it, and SIGKILLs it at
# the stop timeout) leaves a mark the next startup journals as `fabric_check_interrupted`.
FABRIC_INFLIGHT_FILE = "fabric_check_inflight.json"


def _read_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _fabric_inflight_begin(cmd: str) -> None:
    _write_json(FABRIC_INFLIGHT_FILE, {"ts": time.time(), "pid": os.getpid(), "boot_id": _read_boot_id(), "cmd": cmd})


def _fabric_inflight_end() -> None:
    try:
        (health_dir() / FABRIC_INFLIGHT_FILE).unlink()
    except OSError:
        pass


def _journal_fabric_interrupted(rec: dict, how: str) -> dict:
    started = rec.get("ts")
    boot = rec.get("boot_id") or ""
    now_boot = _read_boot_id()
    event = {
        "how": how,
        "started": started,
        "ran_s": round(time.time() - started, 1) if isinstance(started, (int, float)) else None,
        "broker_pid": rec.get("pid"),
        "same_boot": (boot == now_boot) if boot and now_boot else None,
        "cmd": str(rec.get("cmd", ""))[:400],
    }
    health_event("fabric_check_interrupted", **event)
    return event


def journal_interrupted_fabric_check() -> Optional[dict]:
    """At startup, before any pass: a fabric check the previous broker left in flight was cut off
    by its stop, kill or the host going down. Journal it once and clear the mark. Nothing is
    trusted from it: the startup gate runs its own full pass regardless (03 I4)."""
    p = health_dir() / FABRIC_INFLIGHT_FILE
    try:
        rec = json.loads(p.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        rec = {}
    _fabric_inflight_end()
    return _journal_fabric_interrupted(rec if isinstance(rec, dict) else {}, "broker_stopped")


def _cached_topology_mismatch(desc: str) -> Optional[str]:
    """The cached mismatch marker when it was seen with this descriptor on this chip-to-bus map."""
    try:
        data = json.loads((health_dir() / TOPOLOGY_CACHE_FILE).read_text())
        cached = {str(k): int(v) for k, v in data["asic_bus"].items()}
        marker = str(data["marker"])
        same_desc = data.get("desc") == desc
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return marker if same_desc and cached and cached == _asic_bus_map() else None


def _tray_name(bus: int) -> str:
    tray = _TRAY_BY_BUS_GROUP.get(bus & 0xC0)
    return f"tray {tray}" if tray else f"bus 0x{bus:02x}"


def moved_trays(before: dict[str, int], now: dict[str, int]) -> list[str]:
    """``['tray 1 -> tray 3 (8 chips)', ...]``: the chips whose tray changed since ``before``."""
    moves: dict[tuple[str, str], int] = {}
    for asic, bus in now.items():
        if asic in before and _tray_name(before[asic]) != _tray_name(bus):
            k = (_tray_name(before[asic]), _tray_name(bus))
            moves[k] = moves.get(k, 0) + 1
    return [f"{a} -> {b} ({n} chips)" for (a, b), n in sorted(moves.items())]


def _override_reason(text: str) -> str:
    """The reason an operator's fabric wrapper gives for its exit code, as one line.

    The validator's first error line wins (see :func:`fabric.first_reason`: what() before the
    'terminate called' banner above it): the wrapper prints its verdict AFTER the validator's own
    output, and a multi-line verdict put the cause on a line above the last, so the last line alone
    said "no link was tested" and dropped why. With no such line, the last line is the wrapper's
    own verdict."""
    reason = fabric.first_reason(text)
    if reason:
        return reason
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "(no output)"


def _env_int(name: str, default: int, floor: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(floor, int(raw)) if raw else default
    except ValueError:
        return default


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _verdict_label(ok: Optional[bool]) -> str:
    """The tri-state ``(ok, unhealthy, could-not-check)`` every probe here returns, in
    :data:`metrics.VERDICTS` terms."""
    return "healthy" if ok is True else "skipped" if ok is None else "unhealthy"


# The detail prefix of an eth read whose cores all advanced but whose measured link count fell
# below this host's high-water mark (see HealthMonitor.eth_link_drop). update() tells this skip
# apart from a read that reached no verdict: the drop persists until an operator re-baselines.
ETH_LINK_DROP_SKIP = "skipped (eth links unverified)"
# The detail prefix of an eth read that reached no verdict at all (a timeout, a crash, the probe's 77).
ETH_COULD_NOT_CHECK = "skipped (eth-heartbeat read could not check)"
# The detail of a traffic pass that passed with no cabling descriptor while the eth read counted
# fewer links than the high-water mark (see HealthMonitor.links_unseen_by_traffic, spec 03 I28).
FABRIC_LINKS_UNSEEN = "eth links below the high-water mark, which a traffic pass with no cabling descriptor cannot see"


def eth_read_timed_out(detail: str) -> bool:
    """Whether an eth read's detail is a could-not-check whose cause was the read's own timeout —
    the stuck read the post-job present-mesh reset answers (spec 03 I61), never a crash or a 77."""
    return (detail or "").startswith(ETH_COULD_NOT_CHECK) and eth.READ_TIMED_OUT in detail


# Every board type tt-smi cannot identify, including the literal it emits when an ARC read fails.
# Reading these as "not a Galaxy" is how a degraded Galaxy gets the reset that cannot recover it.
_UNIDENTIFIED_BOARDS = ("", "n/a")


def _normalized_board(raw) -> str:
    """A snapshot board type reduced to what tt-smi itself compares: case-folded, without the
    " L"/" R" suffix tt-smi appends to its log copy only. Whitespace comes off before the suffix as
    well as after, or a padded value keeps the suffix and reads as some other board entirely."""
    board = str(raw).strip()
    if board.endswith((" L", " R")):
        board = board[:-2].strip()
    return board.lower()


# The detail every fabric pass the dead-chip isolation killed starts with (spec 03 I82): the gate reads
# it off its own evidence to keep the off-bus hold dirty rather than hold it as a plain 77.
FABRIC_KILLED_BY_ISOLATION = "fabric check KILLED by dead-chip isolation"
SNAPSHOT_KILLED_BY_ISOLATION = "tt-smi snapshot STOPPED by dead-chip isolation"


class HealthMonitor:
    """Runs the probe pass and holds its state: the board-type snapshot cache, the fabric probe's
    last-real-verdict latch, the in-flight fabric-check process handle, and the last ``readings``
    a pass produced. ``deps`` is the same late-bound accessor bag :class:`Recovery` uses (see
    ``tt_device_mcp.health.recovery.RecoveryDeps``) — ``update()`` is what reaches into it for the
    heartbeat probe (``heartbeat_supported``/``heartbeat_verdict``) and ``set_device_op_detail``,
    the same seam :class:`Recovery` has always used, so a test that monkeypatches a server-owned
    dep controls this probe pass too.
    """

    CHIP_BASELINE_FILE = "chip_baseline.json"
    ETH_LINK_BASELINE_FILE = "eth_link_baseline.json"

    def __init__(self, deps) -> None:
        self._deps = deps
        self._begin_action_row = deps.begin_action_row
        self._write_action_log = deps.write_action_log
        self._terminate_process_group = deps.terminate_process_group
        self._logger = deps.logger
        # The SAME /dev/tenstorrent node enumeration the gate, the idle escalators, and the reset
        # routes already use (server._present_chip_indices) — update()'s present-chip count must
        # agree with theirs on every host, including one whose driver exposes no ARC heartbeat at
        # all (heartbeat_supported() permanently False), where a heartbeat-derived count would
        # silently read 0 present chips and neuter expected()'s enumeration-count check.
        self._present_chip_indices = deps.present_chip_indices

        # Board types from the health gate's own tt-smi snapshot. None until one has been read:
        # the reset argv must never issue a snapshot of its own — that would be a device init on
        # the event loop, and on the reset path it would land on silicon the gate has just
        # declared unhealthy.
        self._board_types: Optional[list] = None
        # Per-chip PCI bus ids from the same snapshot, indexed by chip id. Banked for the same
        # reason and with the same lifetime as the board types, but for a stricter one: a chip
        # that has left the bus is absent from every later snapshot AND from sysfs, so the tray
        # rung that needs its bus (spec 04 I16) can only ever read one taken before the drop.
        self._bus_ids: Optional[list] = None
        # {chip index: PCI address} read from sysfs while every chip was on the bus — the map
        # tray placement actually keys on (spec 04 I16). tt-smi lists chips in PCI order and the kernel
        # numbers them in its own order, so ``_bus_ids`` read by position names the wrong chip.
        # Re-banked by any later trusted full read that differs: the kernel can re-number chips.
        self._chip_buses: Optional[dict] = None
        self._glx_cache: Optional[tuple] = None

        # The once-per-process dedup for no-verdict skip journaling: the fabric and eth-heartbeat
        # checks run on every gate and every idle relift, so a skip that reflects a STATIC
        # condition — the command unset, the tool not installed — must journal ONCE, not on every
        # verify until the record is buried under its own repeats.
        self._skip_events_journaled: set[tuple[str, str]] = set()

        # The last REAL fabric verdict (True healthy / False unhealthy); None until one runs. Kept
        # across checks so the read-only relift can refuse to lift a recovered-on-ARC hold back
        # onto a fabric whose last verdict was unhealthy — enum+ARC recover before the eth cores
        # do, and lifting there admits the next tenant onto a fabric that still cannot move data.
        # A skip (None) never overwrites it: not knowing is not the same as knowing it is fit.
        self.last_fabric_ok: Optional[bool] = None
        # The last fabric check's verdict as every hold-lift and release event carries it (spec 03 I78):
        # see fabric_verdict(). Empty until a check runs.
        self._fabric_verdict: dict = {}
        # (monotonic end, outcome) of the last fabric pass that returned: see fabric_outcome_since().
        self._fabric_outcome: tuple[float, str] = (0.0, "")
        self.last_fabric_output: str = ""
        # When the run that produced last_fabric_output started (local naive datetime), None until
        # one runs. An incident bundle keeps that output only when this pass produced it (spec 03 I57).
        self.last_fabric_output_at: Optional[datetime] = None
        # (reason, monotonic, bundle path) of the last no-verdict fabric run kept in an incident bundle.
        self._fabric_skip_incident: Optional[tuple[str, float, str]] = None
        # A topology mismatch (the discovered links are not the descriptor's) passed on a
        # traffic-only rerun: the first job after it is watched, because the links that pass
        # skipped are the ones a mesh-wide workload routes over (see note_job_after_topology_lift).
        self.topology_lift_pending: bool = False
        # Set once that first job failed on the fabric. The check still runs, but a mismatch now
        # FAILS it, so the hold takes the ladder (a reset, then a power cycle); cleared only by a
        # clean fabric pass on the full topology, never by an operator reset or the 10-min bound (I42).
        self.topology_hw_fault: str = ""
        self._topology_alerted: set[str] = set()
        # The last pass's port connections against the cabling descriptor, by name, for /health
        # (spec 03 I71): ``{"verdict": "failed"|"lift", "missing_ports", "extra_ports", "accepted_ports",
        # "marker"}``, ``accepted_ports`` naming the missing ports the accept list excused on a lift;
        # empty after a clean pass on the full topology.
        self.fabric_topology: dict = {}
        # Whether the last fabric pass that ran reached no verdict (a 77). A job that then dies on the
        # fabric's routing ran on a mesh no pass cleared; consecutive ones are a known fabric fault
        # that no reset ends (see note_job_fabric_route_fault, spec 03 I46).
        self.last_fabric_skipped: bool = False
        self.fabric_known_fault: str = ""
        self._fabric_known_fault_at: float = 0.0
        # The last measured eth link count's drop below the high-water mark ("" when it met the mark).
        # A 77 on a mesh with links measured down is a fault a reset can fix, not a missing verdict.
        self.last_eth_link_drop: str = ""
        self._eth_drop_measured: bool = False
        self._cable_fault_alerted: set[str] = set()
        self._fabric_route_fault_streak: int = 0
        # The in-flight fabric traffic pass. It maps every chip's BARs, so when a chip leaves the
        # bus it is the likeliest thing still reading a dead address — and `pci remove` does not
        # revoke its mapping. Tracked so a dead chip can kill it (see server._kill_device_holders).
        self.fabric_check_proc: asyncio.subprocess.Process | None = None
        # Why the broker's dead-chip isolation killed the in-flight fabric pass ("" when it did not):
        # that run's exit is no verdict (spec 03 I82). Set by note_fabric_check_killed, read once.
        self._fabric_killed: str = ""
        # The in-flight full check's tt-smi snapshot, in its own session so a dead chip can stop and
        # kill its group like the fabric pass's (spec 03 I86); and why the isolation did ("" if not).
        self.snapshot_proc: subprocess.Popen | None = None
        self._snapshot_killed: str = ""

        # The last pass's readings blackboard. None until update() runs once.
        self._readings: Optional[HealthState] = None

    def status(self) -> Optional[HealthState]:
        """The last probe pass, or None before one has ever run. Never blocks, never probes —
        the read side of :meth:`update`, for the FSM and Recovery to consult at will."""
        return self._readings

    async def update(
        self,
        phase: str,
        *,
        run_fabric: bool,
        force_fabric: bool = False,
        run_eth: bool = False,
        fabric_stale: bool = True,
        eth_timeout_reset: bool = False,
        indices: Optional[list] = None,
        expected: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> HealthState:
        """Run one probe pass — heartbeat+pci always, then eth, then the fabric traffic pass
        when ``run_fabric``/``force_fabric`` — and store it as the new ``readings`` blackboard.

        ``run_eth`` asks for the passive eth read WITHOUT the traffic pass: the clean post-job gate
        on a host whose eth rung is armed (spec 03 I30). The read is bounded to
        ``ETH_POST_JOB_TIMEOUT_SEC`` there. A frozen verdict stops the pass as usual; a read that
        reached no verdict (its own timeout, a crash, a could-not-check) runs the fabric traffic
        pass in this same pass, because on an armed host that read has answered before and a
        read that now cannot is the stuck-read shape a fabric failure follows. Pass it only when
        the rung is armed: a disarmed reader always skips, so ``run_eth`` there would buy the
        traffic pass on every call. A ``run_eth`` read skipped for a link-count drop (``eth_link_drop``)
        runs that traffic pass only when ``fabric_stale`` (no pass fresher than the caller's
        interval): the drop persists until an operator re-baselines, so running the pass on it
        would charge every clean job ~45-100s. Otherwise the skip is recorded and the pass ends.

        ``eth_timeout_reset`` says the post-job gate can still grant this hold episode's one
        present-mesh reset (spec 03 I61). A ``run_eth`` read that then TIMED OUT with no verdict, on
        a pass whose host-pci read is OK and whose snapshot found every chip with a healthy ARC,
        skips the fabric pass: the gate resets next, and the full check after the reset runs the
        pass. Any guard that fails keeps the pass.

        Mirrors ``Recovery._verify_device``'s gentlest-first short-circuiting (a frozen heartbeat
        or a failed snapshot skips every heavier, more perturbing check below it — the traffic
        pass is only safe to run once nothing is known to be frozen), but records every probe
        that DID run as an :class:`Observation` instead of collapsing straight to a bool. Each
        probe's existing ``(Optional[bool], detail)`` tri-state maps onto an Observation the same
        way: True -> HEALTHY, False -> UNHEALTHY, None -> SKIPPED.

        ``indices``/``expected`` let a caller that already read the mesh this pass (the gate reads
        both before it ever reaches a probe) hand them in instead of this method re-reading
        ``present_chip_indices()`` and re-deriving ``expected()`` itself — the latter WRITES the
        chip-baseline high-water mark, so a second, independent read/ratchet mid-pass can disagree
        with the caller's own. Omit both only where no such read exists yet (e.g. a bare test).

        ``log``, when given, gets the same per-probe line ``Recovery._verify_device`` used to
        write to the job log, and each probe's own ``set_device_op_detail`` string — both are what
        ``Recovery._verify_device`` now delegates here for (see below); dropping them here would
        silently blank the job log and the queue's "what's it doing" detail for every caller.

        A probe the host shutdown or the broker's own stop killed raises
        ``CheckInterruptedByShutdown`` out of here, logged once: no readings are stored, and the
        caller unwinds as from a cancellation, counting nothing (I63).
        """
        try:
            return await self._update(
                phase,
                run_fabric=run_fabric,
                force_fabric=force_fabric,
                run_eth=run_eth,
                fabric_stale=fabric_stale,
                eth_timeout_reset=eth_timeout_reset,
                indices=indices,
                expected=expected,
                log=log,
            )
        except CheckInterruptedByShutdown as e:
            line = f"{e.probe}: INTERRUPTED BY SHUTDOWN — exit {e.rc}, {e.why}; not a verdict, device left as is"
            if log:
                log(line)
            broker_log = self._logger()
            if broker_log and broker_log is not log:
                broker_log.warning(f"HEALTH {line}")
            raise

    async def _update(
        self,
        phase: str,
        *,
        run_fabric: bool,
        force_fabric: bool,
        run_eth: bool,
        fabric_stale: bool,
        eth_timeout_reset: bool,
        indices: Optional[list],
        expected: Optional[int],
        log: Optional[Callable[[str], None]],
    ) -> HealthState:
        if expected is None:
            if indices is None:
                indices = await asyncio.to_thread(self._present_chip_indices)
            expected = self.expected(len(indices))
        observations: list[Observation] = []

        def _record(monitor: str, ok: Optional[bool], detail: str, evidence: Optional[dict] = None) -> None:
            verdict = Verdict.HEALTHY if ok is True else Verdict.UNHEALTHY if ok is False else Verdict.SKIPPED
            observations.append(
                Observation(monitor=monitor, verdict=verdict, detail=detail, evidence=evidence or {}, phase=phase)
            )

        def _store() -> HealthState:
            self._readings = HealthState(
                phase=phase, at=datetime.now(), expected=expected, observations=tuple(observations)
            )
            return self._readings

        # Ahead of the heartbeat, and ahead of the snapshot, because it is the only probe that
        # needs nothing to have worked yet: pure sysfs bus reads, no driver attribute, no device
        # open, no UMD. A chip the driver never bound has no /sys/class/tenstorrent entry at all,
        # so the heartbeat below is simply absent for it and the tt-smi snapshot fails for a
        # reason it cannot name — this says which chip and why. UNHEALTHY rather than evidence
        # because both faults it judges make the device unusable AND are the kind the gentlest
        # rung actually repairs: a PCI rescan re-binds a driver that failed to attach, and an
        # unassigned BAR after hotplug is exactly what a rescan places.
        # Record which root ports carry the chips while they are on the bus, so a tray that drops
        # later still gets its own port masked for its re-power (spec 04 I23). Sysfs reads only.
        from tt_device_mcp.health.recovery import pcie_guard  # recovery imports this module

        await asyncio.to_thread(pcie_guard.record_topology)

        self._deps.set_device_op_detail("health check: host PCI (driver binding, BARs)")
        _t0 = time.monotonic()
        hp_ok, hp_detail, hp_evidence = await asyncio.to_thread(hostpci.host_pci_verdict)
        metrics.probe_observed("hostpci", _verdict_label(hp_ok), time.monotonic() - _t0)
        if log:
            log(f"host-pci: {'SKIPPED' if hp_ok is None else ('OK' if hp_ok else 'UNHEALTHY')} — {hp_detail}")
        _record("hostpci", hp_ok, hp_detail, hp_evidence)
        # A host-hang hold (spec 04 I25) lifts itself once every chip is back on the bus and host-pci
        # is OK, or past its ceiling with every chip back: one file read while no hold is latched.
        await asyncio.to_thread(pcie_guard.expire_offbus_reset_latch, expected, hp_ok, log or (lambda m: None))
        if hp_ok is False:
            return _store()

        # Skipped only where the driver never exposed the attribute — not as a way to excuse a
        # device that failed the probe. See health.monitors.heartbeat.heartbeat_supported(). Routed
        # through deps, not the module-level function, so a test that monkeypatches
        # srv.heartbeat_supported()/heartbeat_verdict (the seam Recovery._verify_device has always
        # used) still controls this probe now that this is the one place it runs.
        if self._deps.heartbeat_supported():
            self._deps.set_device_op_detail("health check: chip heartbeat")
            hb, hb_detail, hb_evidence = await asyncio.to_thread(self._deps.heartbeat_verdict, expected)
            if log:
                log(f"heartbeat: {hb.value.upper()} — {hb_detail}")
            observations.append(
                Observation(monitor="heartbeat", verdict=hb, detail=hb_detail, evidence=hb_evidence, phase=phase)
            )
            if hb is Verdict.UNHEALTHY:
                return _store()

        self._deps.set_device_op_detail("health check: chip enumeration (tt-smi)")
        ok, detail = await self._verify_device(expected)
        if log:
            log(f"snapshot: {'OK' if ok else 'UNHEALTHY'} — {detail}")
        _record("pci", ok, detail)
        # The operator's AICLK ceiling, re-proven (and re-applied where a chip lost it) on every
        # pass once the snapshot showed the chips present — a reset, power cycle, boot or job exit
        # may have cleared it — and before the eth read and the traffic pass, so no load the broker
        # drives runs above it. ~2 ms per chip. Off (no step, no observation) when unconfigured.
        if ok and CEILING.armed():
            self._deps.set_device_op_detail("health check: AICLK ceiling")
            cok, cdetail, cevidence = await CEILING.apply(phase, log=log, terminate=self._terminate_process_group)
            if cok is not None:
                _record("aiclk_ceiling", cok, cdetail, cevidence)
            if cok is False:
                return _store()
        fabric_asked = run_fabric or force_fabric
        if ok and (fabric_asked or run_eth):
            # A full pass re-arms a rung a failed self-test left off before it reads (spec 03 I28). A
            # self-test that hung during an open fault episode is the frozen core itself: FROZEN.
            hung = ""
            if fabric_asked and self._deps.eth_rearm is not None:
                hung = await self._deps.eth_rearm()
            self._deps.set_device_op_detail("health check: eth-core heartbeat (passive)")
            if hung:
                eok, edetail = False, hung
            elif fabric_asked:
                eok, edetail = await self.verify_eth_heartbeat()
            else:
                eok, edetail = await self.verify_eth_heartbeat(timeout_sec=ETH_POST_JOB_TIMEOUT_SEC)
            if log:
                log(f"eth-heartbeat: {'SKIPPED' if eok is None else ('OK' if eok else 'FROZEN')} — {edetail}")
            _record("eth_heartbeat", eok, edetail)
            link_drop = eok is None and edetail.startswith(ETH_LINK_DROP_SKIP)
            # With no cabling descriptor this read is the only measurement of the drop and no traffic
            # pass can see it (I28): a clean exit's check fails here, without the ~45s pass and
            # whatever the rate limit, so it holds as the post-reset check does. An asked-for pass
            # still runs (it may name another fault) and fails on the same drop.
            unseen = self.links_unseen_by_traffic() if link_drop and not fabric_asked else ""
            # A link-drop skip is a standing condition, not a stuck read: rate-limit its pass (a
            # descriptor run, whose validator names the missing links itself). Only a recent PASSING
            # pass covers it: a failed or unknown last verdict is no cover (03 I30).
            link_drop_held = (
                not fabric_asked and link_drop and not unseen and not fabric_stale and self.last_fabric_ok is True
            )
            # Spec 03 I61: a timed-out read on a present mesh goes straight to the gate's one
            # present-mesh reset. The pass here failed or skipped after every such timeout measured.
            reset_due = (
                eth_timeout_reset and not fabric_asked and eok is None and eth_read_timed_out(edetail) and hp_ok is True
            )
            if link_drop_held and log and self.last_fabric_skipped:
                # A 77 since the green pass (it leaves last_fabric_ok alone) reached no verdict: no cover.
                log(
                    "eth links unverified after a clean exit — the last fabric pass reached no verdict, so "
                    "the fabric stays unverified; any fabric-unverified hold stands until a pass returns healthy"
                )
            elif link_drop_held and log:
                log(
                    "eth links unverified after a clean exit — a recent passing fabric pass covers it, not repeating it"
                )
            elif reset_due and log:
                log(
                    "eth-heartbeat read timed out after a clean exit on a present mesh — skipping the "
                    "pre-reset fabric pass; the full check runs after the present-mesh reset"
                )
            elif not fabric_asked and eok is None and log:
                log("eth-heartbeat reached no verdict after a clean exit — running the fabric traffic pass")
            if unseen:
                fok, fdetail = self._fail_links_unseen(unseen, 0.0)
                if log:
                    log(f"fabric: UNHEALTHY — {fdetail}")
                _record("fabric", fok, fdetail)
            elif eok is not False and (fabric_asked or eok is None) and not link_drop_held and not reset_due:
                self._deps.set_device_op_detail("health check: fabric traffic pass across all links (~45s)")
                fok, fdetail = await self.verify_fabric_health()
                if log:
                    log(f"fabric: {'SKIPPED' if fok is None else ('OK' if fok else 'UNHEALTHY')} — {fdetail}")
                _record("fabric", fok, fdetail)

        return _store()

    async def _verify_device(self, expected: int) -> tuple[bool, str]:
        """The tt-smi snapshot probe, off the event loop. A thin seam ``update()`` calls through,
        separated out so a test can replace the whole probe without shelling out to tt-smi."""
        return await asyncio.to_thread(self.verify_device_health, expected)

    def expected(self, present: int) -> int:
        """How many chips this host SHOULD have — never the survivor count.

        Every check took ``expected`` from the /dev nodes that were present, so a mesh that lost a
        tray silently lowered its own bar: a 24-chip box verified 24-of-24 and reported HEALTHY,
        while a job needing the full mesh could not build one. The device was fine about whoever
        was left. The truth comes from an explicit per-host override, else a high-water mark of
        what this host has actually shown — chips disappear from a wedge, not from the design, so
        the bar only ever ratchets up.
        """
        env = os.environ.get("TT_DEVICE_MCP_EXPECTED_CHIPS", "").strip()
        if env.isdigit() and int(env) > 0:
            return int(env)
        path = health_dir() / self.CHIP_BASELINE_FILE
        try:
            baseline = int(json.loads(path.read_text()).get("chips", 0))
        except FileNotFoundError:
            # No baseline was ever written: this host has never shown a chip, so it is
            # device-less until one enumerates. present (0 on the all-off-bus path) is the
            # honest floor, and the gate's empty-/dev branch is allowed to release on it.
            baseline = 0
        except (OSError, ValueError, TypeError, AttributeError):
            # The baseline file EXISTS but is unreadable or corrupt. Its existence is proof
            # this host has shown chips, so it is never a device-less box — collapsing it to 0
            # is the F1 inversion: with present==0 that returns 0, and the gate reads a baseline
            # it merely could not read as "expects no chips" and releases the hold on a mesh that
            # dropped every chip. Absence of a readable count is not absence of chips; fail closed
            # to a nonzero expectation so the empty-/dev branch stays held and escalates.
            return max(present, 1)
        if present > baseline:
            try:
                path.write_text(json.dumps({"chips": present}))
            except OSError:
                pass  # a baseline we cannot persist must not break the gate; present is the floor
            return present
        return baseline or present

    def eth_link_drop(self, measured: int) -> str:
        """Judge the built-in eth probe's measured link count against this host's high-water mark.

        The probe reads only links that are up, so a link that went down is simply one core fewer
        and every remaining core still reads advancing. The count is the only place that loss
        shows. Returns "" when ``measured`` is at least the mark (and ratchets the mark up), else
        the reason the read cannot vouch for the fabric. Like the chip baseline, the mark only
        rises: links go down from a wedge, not from the design. An operator re-baselines a host
        whose links really changed by deleting the file. An unreadable file fails closed for this
        read and is rewritten with the current count, so a file lost that way loses its mark.
        Writes go through a temp file and ``os.replace``, so a crash mid-write cannot tear it.
        """
        self._eth_drop_measured = True
        path = health_dir() / self.ETH_LINK_BASELINE_FILE
        try:
            data = json.loads(path.read_text())
            baseline = int(data.get("links", 0))
            corrupt = False
        except FileNotFoundError:
            data, baseline, corrupt = {}, 0, False
        except (OSError, ValueError, TypeError, AttributeError):
            data, baseline, corrupt = {}, 0, True
        topo = self._topology_baseline(data)
        if topo is not None:
            # A topology-mismatch lift re-baselined this host (I45): the mark belongs to the
            # chip-to-bus map it was taken on, and the pre-lift mark is kept as ``high_water``.
            high, keyed, pending = topo
            accepted = bool(data.get("accepted"))
            if keyed != _asic_bus_map():
                # Trays moved again since: the re-baseline no longer describes this mesh.
                data, baseline = {"links": high}, high
                _write_json(self.ETH_LINK_BASELINE_FILE, data)
            elif pending and (accepted or measured >= high):
                # The first read after the lift sets the mark at the links it discovered.
                _write_json(
                    self.ETH_LINK_BASELINE_FILE,
                    {"links": measured, "high_water": max(high, measured), "asic_bus": keyed, "accepted": accepted},
                )
                health_event("eth_link_rebaselined", links=measured, high_water=high)
                self.last_eth_link_drop = ""
                return ""
            elif baseline < high and not accepted:
                # Never down to a degraded count (I71): only a lift over ports the operator accepted
                # as dead lowers the mark, so a lost link stays a drop for every later read. A mark a
                # broker before I71 lowered reads at the high-water mark again.
                data, baseline = {**data, "links": high}, high
                _write_json(self.ETH_LINK_BASELINE_FILE, data)
            elif measured != baseline:
                # The cabling changed under the re-baseline: the next pass runs the descriptor again.
                _forget_topology_mismatch()
        if measured > baseline or corrupt:
            data = {"links": measured} if corrupt else {**data, "links": measured}
            if "high_water" in data:
                data["high_water"] = max(int(data["high_water"]), measured)
            _write_json(self.ETH_LINK_BASELINE_FILE, data)
            if corrupt or not baseline:
                # A new mark (first read, an operator's delete to re-baseline, a rewritten file):
                # the only ways it can come down, so each is journaled.
                health_event("eth_link_baseline_seeded", links=measured, corrupt=corrupt)
        self.last_eth_link_drop = ""
        if corrupt:
            return f"eth link baseline unreadable; re-baselined at {measured} up link(s)"
        if measured < baseline:
            self.last_eth_link_drop = (
                f"{measured} of {baseline} eth link(s) up — a link went down since the high-water mark"
            )
            return self.last_eth_link_drop
        return ""

    @staticmethod
    def _topology_baseline(data: dict) -> Optional[tuple[int, dict[str, int], bool]]:
        """``(high_water, asic_bus, pending)`` of a topology re-baseline, or None for a plain mark."""
        try:
            keyed = {str(k): int(v) for k, v in data["asic_bus"].items()}
            high = max(int(data.get("high_water", 0)), 0)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return high, keyed, bool(data.get("rebaseline"))

    def _arm_eth_rebaseline(self, accepted: bool = False) -> None:
        """A topology-mismatch lift: key the link mark to this chip-to-bus map and let the next read
        set it at the links the traffic-only pass found, never below the high-water mark unless
        ``accepted`` (a lift over ports the operator accepted as dead, I71). A map already
        re-baselined the same way keeps its mark, so a link lost since then still reads as a drop."""
        now = _asic_bus_map()
        if not now:
            return
        try:
            data = json.loads((health_dir() / self.ETH_LINK_BASELINE_FILE).read_text())
            topo = self._topology_baseline(data)
            links = int(data.get("links", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            data, topo, links = {}, None, 0
        if topo is not None and topo[1] == now and bool(data.get("accepted")) == accepted:
            return
        high = max(links, topo[0] if topo else 0)
        _write_json(
            self.ETH_LINK_BASELINE_FILE,
            {"links": high, "high_water": high, "asic_bus": now, "rebaseline": True, "accepted": accepted},
        )

    def _restore_eth_high_water(self) -> None:
        """A clean pass without the mismatch marker: the descriptor's links are all there again, so
        the pre-lift high-water mark is the bar again, and the cached mismatch is gone."""
        _forget_topology_mismatch()
        try:
            data = json.loads((health_dir() / self.ETH_LINK_BASELINE_FILE).read_text())
            topo = self._topology_baseline(data)
            links = int(data.get("links", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            return
        if topo is not None:
            _write_json(self.ETH_LINK_BASELINE_FILE, {"links": max(links, topo[0])})
            health_event("eth_link_baseline_restored", links=max(links, topo[0]))

    def _health_check_enabled(self) -> bool:
        """Whether the tt-smi snapshot health check runs around jobs. On by default;
        set TT_DEVICE_MCP_HEALTH_CHECK=0 to disable (e.g. a host without tt-smi)."""
        return os.environ.get("TT_DEVICE_MCP_HEALTH_CHECK", "1").strip() != "0"

    def _fabric_check_cmd(self) -> str:
        """The configured operator override for the fabric traffic check (e.g. a custom wrapper
        around run_cluster_validation, or deploy/tt-device-fabric-check.sh). Empty => no
        override; verify_fabric_health falls to the built-in validator path instead (see
        health.monitors.fabric.build_command). Set via TT_DEVICE_MCP_FABRIC_CHECK_CMD. The
        command must exit 0 when the fabric is healthy, 77 when it could not check at all (no
        verdict either way — ignored, never a reset trigger), and non-zero only when the fabric
        is genuinely unhealthy. Its stdout is never interpreted; only its exit code is."""
        return os.environ.get("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "").strip()

    def _eth_heartbeat_cmd(self) -> str:
        """The operator's own non-perturbing eth-core heartbeat check. Empty => not overridden;
        ``verify_eth_heartbeat`` falls to the built-in probe instead (see ``health.monitors.eth``).

        Set via TT_DEVICE_MCP_ETH_HEARTBEAT_CMD. It reads each active-ethernet-core's firmware
        heartbeat register (a passive MMIO read, no traffic pushed) and exits 0 when every core's
        heartbeat is advancing, non-zero when one is frozen, 77 when it could not check. A frozen
        core is the wedge itself; catching it by reading — not by pushing traffic across it, which
        is what knocks the frozen chip off the PCIe bus — is the whole point of running this first."""
        return os.environ.get("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "").strip()

    def _journal_skip_once(self, kind: str, reason: str, **fields) -> None:
        """Record, at most once per process, that a health check produced NO verdict.

        A no-verdict skip means the mesh went unvalidated on that axis, which doctrine forbids passing
        silently. But these skips are a fixed condition seen on every gate, so the first is the whole
        signal — deduping on (kind, reason) keeps the loud record from becoming the noise an operator
        learns to scroll past."""
        key = (kind, reason)
        if key in self._skip_events_journaled:
            return
        self._skip_events_journaled.add(key)
        health_event(kind, reason=reason, **fields)

    def _bank_board_types_once(self, types: list) -> None:
        """Record the board types seen in a tt-smi snapshot. The boards cannot change under a running
        broker, so the first identified read stands; a later short or empty one is not allowed to erase
        it, because losing the machine type is what silently downgrades a Galaxy's reset.

        A snapshot tt-smi could not identify is not a read of the machine type. Caching it would pin
        this process to "unknown" for its whole life and lock out the identified snapshot that follows a
        recovery — so it is left unset instead, and the next snapshot gets to fill it.
        """
        if self._board_types is not None or not types:
            return
        named = [t for t in types if _normalized_board(t) not in _UNIDENTIFIED_BOARDS]
        if len(named) != len(types):
            self._journal_skip_once(
                "board_types_unidentified",
                f"tt-smi identified {len(named)}/{len(types)} boards — machine type "
                f"stays unknown until a snapshot identifies all of them",
            )
            return
        self._board_types = list(types)

    def _bank_bus_ids_once(self, bus_ids: list, expected_count: int) -> None:
        """Record every chip's PCI bus id from a tt-smi snapshot, first complete read standing.

        Complete or not at all: a snapshot taken while chips were already off the bus is short, and
        a short list read positionally would attribute one chip's bus to another's id. The trays
        derived from that (I16) would name healthy silicon, so a partial read is discarded and the
        next full snapshot gets to fill it. Both halves of "complete" are enforced here rather than
        left to the caller: ``expected_count == 0`` (the boot platform probe reads only board types
        and does not know the count) and any snapshot shorter than the mesh are both refused, and
        so is a chip tt-smi enumerated without a bus id.
        """
        if self._bus_ids is not None:
            return
        if expected_count <= 0 or len(bus_ids) < expected_count:
            return
        if not bus_ids or not all(bus_ids):
            return
        self._bus_ids = list(bus_ids)

    def _bank_chip_buses(self, bus_ids: list, expected_count: int) -> None:
        """Keep every chip index's PCI address, read from sysfs, as of the latest trusted full read.

        The index is the kernel's (/dev, sysfs, heartbeat), so the tray of an off-bus chip comes from
        its own address, not from where tt-smi happened to list it (spec 04 I16). Trusted and full
        means the same "complete or not at all" rule as ``_bus_ids``, read in the same pass, and the
        two agreeing on the set of buses: a disagreement means the bus moved under one of the reads,
        so both are journaled and the next full pass tries again.

        Unlike ``_bus_ids`` the map is not first-read-stands. tt-kmd can give a chip another index
        after a drop, a rescan or a tray reset (it falls back to a free index when the chip's own is
        still taken), and a map naming the old index re-powers the wrong tray. So a trusted full read
        that differs re-banks it here, in the gate's pass before any rung reads it, and journals the
        old and the new map. A short or disagreeing read never touches a banked map: a chip off the
        bus has no node, so that read cannot place it.
        """
        if expected_count <= 0 or len(bus_ids) < expected_count or not all(bus_ids):
            return
        chips = pci.chip_pci_bdfs()
        smi_buses = [pci.pci_bus_number(b) for b in bus_ids]
        if None in smi_buses or sorted(smi_buses) != sorted(pci.pci_bus_number(a) for a in chips.values()):
            self._journal_skip_once(
                "chip_bus_map_mismatch",
                f"sysfs and tt-smi disagree on the chips' PCI buses ({len(chips)} vs {len(bus_ids)} chips) — "
                + ("the banked tray map stands" if self._chip_buses is not None else "no tray map")
                + " until they agree (I16)",
                sysfs={str(k): v for k, v in sorted(chips.items())},
                tt_smi=list(bus_ids),
            )
            return
        if self._chip_buses is None:
            self._chip_buses = dict(chips)
            return
        if chips == self._chip_buses:
            return
        old, self._chip_buses = self._chip_buses, dict(chips)
        # Every change, not once per process: each one moves the tray a rung would re-power.
        health_event(
            "chip_bus_map_drift",
            reason="the kernel re-numbered chips since the map was banked (I16); re-banked from this read",
            old={str(k): v for k, v in sorted(old.items())},
            new={str(k): v for k, v in sorted(chips.items())},
        )

    def _bus_id_cache_expected_count(self, expected_count: int) -> int:
        """The smallest snapshot size that is allowed to freeze ``_bus_ids``.

        The raw ``expected_count`` is not always enough. On a fresh broker a degraded first pass can
        seed the chip baseline from its survivor count; caching a Galaxy's bus ids against that lower
        bar would permanently bank a partial tray map until restart. The board type therefore
        tightens the count when it can: a known Galaxy needs all 32 chips, and an unidentified
        machine type means "no map yet" rather than freezing a positional guess for a later
        identified snapshot to inherit.
        """
        board_types = {_normalized_board(t) for t in (self._board_types or ())}
        if len(board_types) != 1:
            return 0
        if board_types.pop() in self._glx_board_types():
            return max(expected_count, 32)
        return expected_count

    def _glx_board_types(self) -> tuple:
        """tt-smi's own Galaxy board types — the single source of truth, never copied here. Empty when
        tt-smi cannot be imported, which leaves the machine type unknown rather than guessed.

        Imported here, not at module scope: `tt_smi` pulls in a native extension and a TUI stack to
        reach a two-element list, and importability differs from tt-smi being on PATH.
        """
        if self._glx_cache is None:
            try:
                from tt_smi.constants import GLX_BOARD_TYPES

                self._glx_cache = tuple(GLX_BOARD_TYPES)
            except Exception as e:  # noqa: BLE001 - a native extension failing here must not kill boot
                self._journal_skip_once("glx_types", f"tt-smi board-type list unavailable: {e}")
                self._glx_cache = ()
        return self._glx_cache

    def verify_device_health(self, expected_count: int, *, timeout_sec: float = 90.0) -> tuple[bool, str]:
        """Verify the mesh is actually usable after a reset, via a read-only tt-smi
        snapshot. A reset exiting 0 only means the command ran — it does NOT prove the
        chips came back. This is the check that turns "reset ran" into "device healthy".

        Returns ``(ok, detail)``. ``ok`` is True only when ALL of:
          * the snapshot itself succeeds (ARC mailbox answered within ``timeout_sec``),
          * at least ``expected_count`` chips enumerate (none dropped off the mesh — the
            UBB-tray-down signature is a short count here), and
          * every chip reports a ``board_id`` and non-empty telemetry (its ARC is alive,
            not wedged returning sentinels).

        Scope: this proves chips enumerate + ARC is responsive. True inter-chip fabric
        (ethernet) liveness only manifests under a collective, so a green result means
        "all chips present and answering", which is the strongest signal obtainable
        without running a CCL workload in the between-jobs gate. Never raises — any
        failure is returned as ``(False, reason)`` so callers stay non-blocking.

        Note: ``tt-smi -s`` dumps the snapshot JSON to STDOUT and exits before any
        ``-f`` file is written, so we parse stdout (``--snapshot_no_tty`` forces the
        machine-readable JSON form even when attached to a tty).

        Wrapped by a thin timer that reports the "pci" probe metric (see
        :func:`_verify_device_health_body`) — every caller of this method (the gate,
        ``Recovery._verify_device``, and ``HealthMonitor.update()``) is covered from this one
        instrumentation point.
        """
        _t0 = time.monotonic()
        ok, detail = self._verify_device_health_body(expected_count, timeout_sec=timeout_sec)
        metrics.probe_observed("pci", _verdict_label(ok), time.monotonic() - _t0)
        return ok, detail

    def note_snapshot_killed(self, reason: str, journal: Optional[Callable[..., None]] = None) -> None:
        """The dead-chip isolation stopped or killed the in-flight tt-smi snapshot (spec 03 I86): its
        exit or timeout says nothing about the chips. That check is no verdict, never a pass, and
        a full pass with fabric is owed before any release (04 I32)."""
        self._snapshot_killed = reason or "dead-chip isolation"
        FABRIC_DEBT.note_fabric_killed(f"tt-smi snapshot killed ({self._snapshot_killed})", journal)

    def _run_snapshot(self, argv: list[str], timeout_sec: float) -> subprocess.CompletedProcess:
        """``subprocess.run`` for the snapshot, but in its own session and tracked on
        ``snapshot_proc`` while it runs, so the dead-chip valve can SIGSTOP its group before a PCI
        remove and SIGKILL it after (spec 03 I86). Raises TimeoutExpired as run does, after killing
        the group."""
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        # Its own session takes it out of the broker's process group: register it, or a holder scan
        # reads the broker's own snapshot as a tenant (spec 04 I59).
        note_broker_internal(proc.pid)
        self.snapshot_proc = proc
        try:
            try:
                out, err = proc.communicate(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                # Held by an AER stop: no kill until it is settled, bounded (spec 03 I92).
                chip_state.wait_reader_release(proc.pid)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.communicate(timeout=5)
                except Exception:  # noqa: BLE001 - a reader stuck past SIGKILL is the holder scan's
                    pass
                raise
        finally:
            self.snapshot_proc = None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def _verify_device_health_body(self, expected_count: int, *, timeout_sec: float = 90.0) -> tuple[bool, str]:
        self._snapshot_killed = ""
        try:
            try:
                proc = self._run_snapshot(["tt-smi", "-s", "--snapshot_no_tty"], timeout_sec)
            except subprocess.TimeoutExpired:
                if self._snapshot_killed:
                    return False, self._snapshot_no_verdict()
                return False, f"tt-smi snapshot timed out after {timeout_sec:.0f}s (ARC unresponsive)"
            if self._snapshot_killed:
                # Stopped or killed by the dead-chip valve: whatever it printed or returned is no
                # verdict (spec 03 I86). Never a pass; the hold stays and a full pass is owed.
                return False, self._snapshot_no_verdict()
            if proc.returncode != 0:
                interrupted = interrupted_by_shutdown("tt-smi", proc.returncode)
                if interrupted is not None:
                    raise interrupted  # the shutdown killed it, not the chips (I63)
                tail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[-200:]
                return False, f"tt-smi snapshot exited {proc.returncode}: {tail}"
            try:
                snap = json.loads(proc.stdout)
            except ValueError as e:
                tail = (proc.stderr or "").strip().replace("\n", " ")[-200:]
                return False, f"could not parse tt-smi snapshot JSON: {e}" + (f" (stderr: {tail})" if tail else "")
        except Exception as e:  # noqa: BLE001 - health check must never raise into the gate
            return False, f"snapshot error: {e}"

        devs = snap.get("device_info") or []
        # The board types ride along: this snapshot is the only tt-smi call the broker makes, and the
        # reset argv needs the machine type. Taking it here means no second device init — least of all
        # one aimed at silicon this check has just declared unhealthy.
        self._bank_board_types_once([(d.get("board_info") or {}).get("board_type") or "" for d in devs])
        # A short count is a drop; an over-count is not. ``expected_count`` can be a stale
        # high-water mark baked while the mesh was degraded, so a reset that brings back MORE
        # chips than that bar is a recovery — scoring it a failure would escalate a healthy box.
        if len(devs) < expected_count:
            return False, (
                f"{len(devs)} chip(s) enumerated, expected {expected_count} "
                f"(chips dropped off the mesh — likely a tray down)"
            )
        # The count guard lives inside _bank_bus_ids_once, but the count itself has to be tightened
        # here: a boot platform probe passes expected_count=0, and a fresh degraded first pass can
        # hand us the survivor count before the baseline ever saw the whole mesh. Neither is allowed
        # to freeze a positional bus map (I16).
        fresh_bus_ids = [(d.get("board_info") or {}).get("bus_id") or "" for d in devs]
        bus_id_expected_count = self._bus_id_cache_expected_count(expected_count)
        self._bank_bus_ids_once(fresh_bus_ids, bus_id_expected_count)
        self._bank_chip_buses(fresh_bus_ids, bus_id_expected_count)
        # Topology-drift observability for tt-smi's own list: it is banked once for the process, and
        # the driver's bus enumeration after a warm reset is not guaranteed to match boot. Journal a
        # mismatch so a drift is visible. The list stands: no tray is read from it (the chip map just
        # re-banked is). Only compares full-count reads — anything shorter is an already-degraded
        # mesh whose absent chips look like drift.
        if (
            self._bus_ids is not None
            and bus_id_expected_count > 0
            and len(fresh_bus_ids) >= bus_id_expected_count
            and all(fresh_bus_ids)
            and list(fresh_bus_ids) != self._bus_ids
        ):
            self._journal_skip_once(
                "bus_map_drift",
                "cached tt-smi bus list differs from the current snapshot — a warm reset may have "
                "re-enumerated chips (I16); no tray is read from this list, so it stands",
            )
        silent = []
        for i, d in enumerate(devs):
            binfo = d.get("board_info") or {}
            telem = d.get("telemetry") or {}
            if not binfo.get("board_id") or not telem:
                silent.append(str(i))
        if silent:
            return False, f"chip(s) [{','.join(silent)}] returned no board_id/telemetry (ARC wedged)"
        return True, f"all {len(devs)} chips enumerated with live ARC telemetry"

    async def verify_fabric_health(self, timeout_sec: float = FABRIC_CHECK_TIMEOUT_SEC) -> tuple[Optional[bool], str]:
        """Run the fabric traffic check (see ``health.monitors.fabric``), which pushes packets
        across every inter-chip ethernet link and reports non-zero if any link is unhealthy.
        This is the only check that proves the FABRIC actually moves data — the tt-smi snapshot
        only proves chips enumerate + ARC is alive.

        Returns ``(ok, detail)``:
          * ``None`` => SKIPPED (not configured, no built-in validator installed, the command
            isn't runnable, or the check ran but reached no verdict) — the caller must NOT
            reset on a skip.
          * ``True``  => fabric healthy.
          * ``False`` => fabric unhealthy, including a broker-side timeout — the caller should
            reset.
        Never raises.

        Wrapped by a thin timer that reports the "fabric" probe metric — the pass whose duration
        is the one worth alerting on, since it is the closest to its own timeout of the four."""
        _t0 = time.monotonic()
        _fabric_inflight_begin(self._fabric_check_cmd() or "built-in")
        try:
            ok, detail = await self._verify_fabric_health_body(timeout_sec)
        except asyncio.CancelledError as e:
            # Cancelled under us (a shutdown that unwinds the loop), or the validator itself killed
            # by the shutdown (I63): say so now, the mark goes.
            _journal_fabric_interrupted(
                {"ts": time.time() - (time.monotonic() - _t0), "pid": os.getpid(), "boot_id": _read_boot_id()},
                "shutdown" if isinstance(e, CheckInterruptedByShutdown) else "cancelled",
            )
            raise
        finally:
            _fabric_inflight_end()
        metrics.probe_observed("fabric", _verdict_label(ok), time.monotonic() - _t0)
        self._note_fabric_verdict(ok, detail)
        return ok, detail

    def _note_fabric_verdict(self, ok: Optional[bool], detail: str) -> None:
        """Keep what fabric_verdict() reports: pass/fail/skipped, and how far the links the check found
        fall short of the cabling descriptor (a topology marker in its detail or kept output)."""
        text = detail or ""
        missing, extra = fabric.marker_counts(text)
        if not (missing or extra) and ok is False:
            text = self.last_fabric_output or ""
            missing, extra = fabric.marker_counts(text)
        self._fabric_verdict = {
            "result": "pass" if ok is True else "fail" if ok is False else "skipped",
            "missing_vs_cabling": missing,
            "extra_vs_cabling": extra,
            "detail": (detail or "")[:200],
            "_marker": fabric.find_topology_marker(text) or "",
            "_at": time.monotonic(),
        }
        killed = (detail or "").startswith(FABRIC_KILLED_BY_ISOLATION)
        self._fabric_outcome = (time.monotonic(), "killed" if killed else self._fabric_verdict["result"])

    def fabric_outcome_since(self, since: float) -> str:
        """How the last fabric pass that returned at or after ``since`` (monotonic) ended: "pass",
        "fail", "skipped" (no verdict) or "killed" (dead-chip isolation stopped it, I82); "" when
        none returned since. A record for attribution only (spec 03 B-post-job clock check)."""
        at, outcome = self._fabric_outcome
        return outcome if outcome and at >= since else ""

    def fabric_verdict(self) -> dict:
        """The last fabric verdict, for the detail of every hold-lift and release event (spec 03 I78):
        ``result`` (pass/fail/skipped/none), ``missing_vs_cabling``/``extra_vs_cabling`` (port connections
        the links found lack, or add, against the cabling descriptor: 0/0 on a clean pass means every
        cabled link was found), ``links_short`` (True when more are missing than extra and the operator's
        accept list does not name every missing port, which FAILS the check, I71), ``excused_ports``
        (only when that list turned a shortfall into a pass: the ports it excused) and ``age_s``. Only
        the shortfall is logged: the validator prints no link totals. ``result`` "none" once a reset
        voided it or before any check ran."""
        v = dict(self._fabric_verdict)
        if not v or self.last_fabric_ok is None and v["result"] != "skipped":
            return {"result": "none"}
        marker = v.pop("_marker", "")
        v["links_short"] = bool(fabric.links_missing(marker, fabric.accepted_missing_ports()))
        if v["missing_vs_cabling"] > v["extra_vs_cabling"] and not v["links_short"]:
            v["excused_ports"] = sorted(fabric.marker_ports(marker)[0])
        v["age_s"] = round(time.monotonic() - v.pop("_at"), 1)
        return v

    def note_fabric_check_killed(self, reason: str, journal: Optional[Callable[..., None]] = None) -> None:
        """The broker's dead-chip isolation SIGKILLed the in-flight fabric pass (spec 03 I82). Its exit
        (-9) then says nothing about the links: the run is no verdict, the fabric unknown, and a full
        pass is owed before any release (04 I32)."""
        self._fabric_killed = reason or "dead-chip isolation"
        FABRIC_DEBT.note_fabric_killed(f"fabric check killed ({self._fabric_killed})", journal)

    async def stop_check_readers(self, reason: str, grace_sec: float) -> list:
        """Spec 04 I97: end the full check's own readers at its deadline. SIGTERM the process group of
        the fabric pass, the tt-smi snapshot and the AICLK ceiling, SIGKILL any group still alive
        ``grace_sec`` later, and mark each killed check no verdict (never a pass). Returns the pids
        signalled. Never raises."""
        stopped = []
        for proc, note in (
            (self.fabric_check_proc, self.note_fabric_check_killed),
            (self.snapshot_proc, self.note_snapshot_killed),
            (CEILING.proc, None),
        ):
            if proc is None or getattr(proc, "returncode", None) is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                continue
            stopped.append(proc.pid)
            if note is not None:
                note(reason)
        loop = asyncio.get_running_loop()
        end = loop.time() + grace_sec
        alive = list(stopped)
        while alive and loop.time() < end:
            await asyncio.sleep(0.2)
            alive = [pid for pid in alive if _group_alive(pid)]
        for pid in alive:
            # A group an AER stop holds SIGSTOPped acts on neither signal: its SIGKILL waits for the
            # stop to be settled, at most to the hold's bound (spec 03 I92).
            await chip_state.await_reader_release(pid)
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        return stopped

    def _snapshot_no_verdict(self) -> str:
        why, self._snapshot_killed = self._snapshot_killed, ""
        health_event("snapshot_no_verdict", cause="killed_by_dead_chip_isolation", reason=why[:400])
        return f"{SNAPSHOT_KILLED_BY_ISOLATION}: {why}; no verdict, a full pass is owed after recovery"

    async def _verify_fabric_health_body(self, timeout_sec: float) -> tuple[Optional[bool], str]:
        self._fabric_killed = ""
        override = self._fabric_check_cmd()
        built = fabric.build_command()
        if built is None:
            # The authoritative fabric verdict, and preflight hard-requires it — so reaching here means
            # the mesh goes entirely unvalidated on the one check that proves it moves data. Journal it.
            self._journal_skip_once("fabric_check_unavailable", "not_configured")
            return None, "skipped (TT_DEVICE_MCP_FABRIC_CHECK_CMD not set, and no built-in validator installed)"
        argv, env = built
        cmd = override or " ".join(argv)
        # The runtime-root cwd is a built-in-path concern only: run_cluster_validation resolves
        # its kernels from it, but an operator's override runs wherever the broker itself runs,
        # exactly as before this module existed — env for that path is just os.environ, and
        # deriving cwd from whatever TT_METAL_RUNTIME_ROOT happens to be ambient there would
        # silently relocate the override's cwd too.
        cwd = None if override else env.get("TT_METAL_RUNTIME_ROOT")
        _t0 = datetime.now()
        # Open the jobs-list row now, not on completion: the traffic pass holds the device for
        # ~45-100s, and a row written only at the end leaves that window a blank in the audit
        # trail with no id an operator can follow.
        self._begin_action_row("[broker]fabric-check", cmd)

        def _track(proc: Optional[asyncio.subprocess.Process]) -> None:
            # Tracked so a dead chip can kill it (see server._kill_device_holders). This process
            # maps every chip's BARs, so when one leaves the bus it is the likeliest thing still
            # reading a dead address — and removing the endpoint does not revoke its mapping. It
            # has killed a host twice.
            self.fabric_check_proc = proc

        desc = "" if override else fabric._validator_paths()[2]
        cached = _cached_topology_mismatch(desc) if desc else None
        if cached:
            # This chip-to-bus map already mismatched the descriptor: that run would only throw the
            # same mismatch again, so go straight to the traffic-only rerun (I45).
            rc, text, ok, reason = await self._fabric_traffic_only_rerun(
                desc, "", timeout_sec=timeout_sec, track=_track, cwd=cwd, marker=cached
            )
        else:
            try:
                rc, text = await fabric.check(
                    argv, env, timeout_sec=timeout_sec, track=_track, terminate=self._terminate_process_group, cwd=cwd
                )
            except Exception as e:  # noqa: BLE001 - configured but could not spawn; never raise into the gate
                self._journal_skip_once("fabric_check_unavailable", "not_runnable", detail=str(e)[:400])
                return None, f"skipped (fabric check not runnable: {e})"

        dt = (datetime.now() - _t0).total_seconds()
        # Keep the whole thing: the validator names the failing links, and the 400-char detail
        # the journal carries truncates exactly the part naming them.
        self.last_fabric_output = text
        self.last_fabric_output_at = _t0
        if self._fabric_killed:
            return self._fabric_killed_no_verdict(cmd, rc, text, dt)
        last = text.splitlines()[-1][:200] if text else "(no output)"
        if override:
            # An operator's own wrapper has already done its own interpretation and reports it
            # purely through its exit code — its stdout is never run through classify(), which
            # means something only for the built-in validator's own output signatures.
            ok = None if rc in (FABRIC_CHECK_CANNOT_CHECK_RC, FABRIC_CHECK_HARDWARE_FAULT_RC) else rc == 0
            reason = _override_reason(text)
            if rc == FABRIC_CHECK_HARDWARE_FAULT_RC:
                # The wrapper's own verdict line names the missing cable; the validator's what() does not.
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                reason = f"{fabric.CABLE_FAULT} (fabric check exit {rc}): {lines[-1] if lines else '(no output)'}"
        elif not cached:
            ok, reason = fabric.classify(rc, text)
            if ok is None and desc and fabric.is_topology_mismatch(text):
                rc, text, ok, reason = await self._fabric_traffic_only_rerun(
                    desc, text, timeout_sec=timeout_sec, track=_track, cwd=cwd
                )
                self.last_fabric_output = text
                self.last_fabric_output_at = _t0
        if self._fabric_killed:
            # Read again after the traffic-only rerun (I45): a kill there is no verdict either (I82),
            # never a link fault or a missing-cable verdict (I47) from whatever the killed run printed.
            return self._fabric_killed_no_verdict(cmd, rc, text, (datetime.now() - _t0).total_seconds())
        # Traffic crossed every discovered link but ports of the descriptor are missing: a FAILED
        # check, never a lift (I71). The cached mismatch goes, so the next pass runs the descriptor
        # again and a reset that brought the link back reads clean.
        marker = fabric.find_topology_marker(text) if ok is True else None
        marker = fabric.with_ports(marker, text) if marker else None
        lost = fabric.links_missing(marker, fabric.accepted_missing_ports()) if marker else ""
        if marker and not lost and self.topology_hw_fault:
            # The lift's first job already failed on these links (I42): a mismatch now FAILS, so the
            # ladder (a reset, then a power cycle) runs instead of a second lift.
            lost = f"{fabric.LINKS_MISSING} after the lift's first job failed on the fabric (I42): {marker}"
        if lost:
            ok, reason = False, lost
            text = f"{text}\n{lost}"
            self.last_fabric_output = text
            _forget_topology_mismatch()
        # Derived from ok, not rc: on the built-in path a measured-bad verdict can exit 77 (the
        # collision guard classify() applies), and a did-not-measure run can exit some other
        # code entirely — status must agree with the verdict actually being returned below, not
        # with the raw exit code that verdict may have been remapped away from. A broker-side
        # timeout (rc is None) is its own thing: a live run that ran out of time, not a skip.
        cable = ok is None and reason.startswith(fabric.CABLE_FAULT)
        status = "timeout" if rc is None else "completed" if ok is True else "skipped" if ok is None else "failed"
        if cable:
            status = "hardware_fault"
        if ok is True:
            self._write_action_log("[broker]fabric-check", cmd, dt, status, rc)
        else:
            # A run that got no healthy verdict is the one an operator opens the row for, and the
            # broker journal keeps only a line of it — so the row carries the output itself.
            self._write_action_log("[broker]fabric-check", cmd, dt, status, rc, output=_action_log_output(text))

        self.last_fabric_skipped = ok is None
        if ok is True:
            if marker:
                self._note_fabric_ok()
                self._on_topology_mismatch(marker)
                return True, f"fabric links healthy on discovered links ({dt:.0f}s); {marker}"
            unseen = self.adopt_fabric_pass(text)
            if unseen:
                return self._fail_links_unseen(unseen, dt)
            return True, f"fabric links healthy ({dt:.0f}s)"
        if cable:
            # A missing cable is a hardware verdict, not a skip (I47): held as a known fabric fault,
            # outside the 77 bound and never reset or climbed for (I40, 04 I30), paged once.
            self._declare_cable_fault(reason)
            if not override:
                self._arm_eth_rebaseline()  # as a topology skip does: the mark is this mesh's (I45)
            bundle = await self._keep_skip_output(rc, text, reason, cmd) if rc is not None else None
            kept = f"; exit {rc}, output in {bundle}/fabric.log" if bundle else ""
            return None, f"fabric HARDWARE FAULT ({dt:.0f}s{kept}): {reason[:400]}"
        if ok is None:
            # The check could not run — it learned nothing about the fabric, so resetting
            # on it would be resetting on our own missing install. Loud, because a host
            # in this state has no fabric coverage at all and that must not pass silently.
            log = self._logger()
            if log:
                log.warning(f"FABRIC-CHECK not runnable on this host — no fabric coverage: {reason[:400]}")
            health_event("fabric_check_unavailable", detail=reason[:400], cmd=cmd)
            if reason.startswith(fabric.TOPOLOGY_MISMATCH):
                # The rerun could not measure: the bounded fallback (I40) judges the eth read, so it
                # needs the mark for this mesh, not the descriptor's (I45).
                self._arm_eth_rebaseline()
            # The run's output is the only record of why it could not check: keep its tail and exit
            # code where a rerun is not needed to read them, and name the bundle in the detail.
            bundle = await self._keep_skip_output(rc, text, reason, cmd) if rc is not None else None
            kept = f"; exit {rc}, output in {bundle}/fabric.log" if bundle else ""
            return None, f"fabric check COULD NOT RUN ({dt:.0f}s{kept}): {reason[:400]}"
        self.last_fabric_ok = False
        if lost:
            missing, extra = fabric.marker_ports(marker)
            self.fabric_topology = {
                "verdict": "failed",
                "missing_ports": missing,
                "extra_ports": extra,
                "accepted_ports": [],
                "marker": marker,
            }
            health_event("fabric_links_missing", detail=lost[:400], missing_ports=missing, extra_ports=extra)
            log = self._logger()
            if log:
                log.error(f"FABRIC {lost}")
            return False, f"fabric check FAILED ({dt:.0f}s): {lost[:400]}"
        if rc is None:
            return False, f"fabric check timed out after {timeout_sec:.0f}s; last line: {last}"
        return False, f"fabric check exited {rc} ({dt:.0f}s): {reason[:400]}"

    def _fabric_killed_no_verdict(self, cmd: str, rc: Optional[int], text: str, dt: float) -> tuple[None, str]:
        """A pass the dead-chip isolation killed (spec 03 I82): neither a pass nor a link fault, nor a
        77. No failed verdict, no 77 streak (I46) and no cover from an earlier pass: the fabric is
        unknown until a later pass measures it."""
        why, self._fabric_killed = self._fabric_killed, ""
        self._write_action_log("[broker]fabric-check", cmd, dt, "killed", rc, output=_action_log_output(text))
        self.last_fabric_ok = None
        self.last_fabric_skipped = False
        detail = (
            f"{FABRIC_KILLED_BY_ISOLATION} ({dt:.0f}s, exit {rc}): {why}; no verdict, "
            "fabric unknown, a full pass is owed after recovery"
        )
        health_event("fabric_check_no_verdict", cause="killed_by_dead_chip_isolation", reason=why[:400], rc=rc)
        log = self._logger()
        if log:
            log.warning(f"FABRIC-CHECK {detail}")
        return None, detail

    async def _keep_skip_output(self, rc: int, text: str, reason: str, cmd: str) -> Optional[str]:
        """Freeze a no-verdict fabric run's exit code and output tail (``FABRIC_SKIP_OUTPUT_TAIL_BYTES``)
        in an incident bundle; return its path, or ``None`` when it could not be written. A run with
        the reason of the last bundle written within ``FABRIC_SKIP_INCIDENT_REUSE_SEC`` reuses it."""
        now = time.monotonic()
        last = self._fabric_skip_incident
        if last and last[0] == reason and now - last[1] < FABRIC_SKIP_INCIDENT_REUSE_SEC and os.path.isdir(last[2]):
            return last[2]
        nbytes = len(text.encode("utf-8", "replace"))
        bundle = await asyncio.to_thread(
            capture_incident,
            "fabric_cannot_check",
            fabric_output=_output_tail(text, FABRIC_SKIP_OUTPUT_TAIL_BYTES) or "(no output)",
            evidence={"rc": rc, "reason": reason[:400], "cmd": cmd[:400], "output_bytes": nbytes},
        )
        if bundle is None:
            return None
        self._fabric_skip_incident = (reason, now, str(bundle))
        return str(bundle)

    async def _fabric_traffic_only_rerun(
        self,
        desc: str,
        first: str,
        *,
        timeout_sec: float,
        track,
        cwd: Optional[str],
        marker: Optional[str] = None,
    ) -> tuple[Optional[int], str, Optional[bool], str]:
        """The validator stopped on its descriptor comparison: rerun it once without the descriptor
        so traffic still crosses every link it discovered. Same verdict table as the first run; a
        pass carries the mismatch marker onto its OK, anything not measured stays a topology skip.
        ``marker`` set is a mismatch cached for this chip-to-bus map: no descriptor run preceded."""
        log = self._logger()
        if marker is None:
            missing, extra = fabric.topology_counts(first)
            marker = fabric.topology_marker(desc, missing, extra, fabric.topology_ports(first))
            _remember_topology_mismatch(desc, marker)
            if log:
                log.warning(f"fabric-check: {marker}; rerunning without the descriptor")
        elif log:
            log.warning(f"fabric-check: {marker} (cached for this chip-to-bus map); descriptor run skipped")
        cables = fabric.missing_cables(marker)

        def no_verdict(why: str) -> str:
            # Missing cables with none extra are a hardware verdict whatever the rerun came to (I47).
            return fabric.cable_fault(cables, desc, why) if cables else f"{fabric.TOPOLOGY_MISMATCH}; {why}"

        rebuilt = fabric.build_command(descriptor=False)
        if rebuilt is None:
            return None, first, None, no_verdict("traffic-only rerun unavailable")
        try:
            rc, text = await fabric.check(
                rebuilt[0],
                rebuilt[1],
                timeout_sec=timeout_sec,
                track=track,
                terminate=self._terminate_process_group,
                cwd=cwd,
            )
        except Exception as e:  # noqa: BLE001 - never raise into the gate
            return None, first, None, no_verdict(f"traffic-only rerun not runnable: {e}")
        ok, reason = fabric.classify(rc, text)
        combined = f"{first}\n{marker}\n{text}"
        if ok is True:
            return rc, f"{combined}\n{marker}", True, "fabric healthy on discovered links; " + marker
        if ok is None:
            reason = no_verdict(f"traffic-only rerun: {reason}")
        return rc, combined, ok, reason

    def _note_fabric_ok(self) -> None:
        """What every healthy fabric verdict settles: the post-reset debt (04 I32) and a known fabric
        fault (I46)."""
        self.last_fabric_ok = True
        FABRIC_DEBT.note_fabric(True)  # the one verdict that settles a post-reset debt (04 I32)
        if self.fabric_known_fault:
            health_event("fabric_known_fault_cleared", detail=self.fabric_known_fault)
        self.fabric_known_fault, self._fabric_route_fault_streak = "", 0

    def adopt_fabric_pass(self, output: str) -> str:
        """Fold in a healthy fabric pass on the full topology, as a clean pass of the monitor's own
        does, also one run outside the monitor (an operator verification job, spec 01 I21). A pass
        whose output carries a topology marker ran on the discovered links only: it is adopted for
        nothing and its marker returned, so the caller leaves the lift to the monitor's own pass
        (I45). Nor is a pass the eth read's link count says missed a link (``links_unseen_by_traffic``):
        its reason is returned. Returns "" once adopted."""
        marker = fabric.find_topology_marker(output)
        if marker:
            return marker
        unseen = self.links_unseen_by_traffic()
        if unseen:
            return unseen
        self._note_fabric_ok()
        self._note_fabric_verdict(True, "a clean fabric pass on the full cabling")
        self.topology_lift_pending = False
        self.clear_topology_hw_fault("a clean fabric pass")
        self.fabric_topology = {}
        _save_asic_bus_map()
        self._restore_eth_high_water()
        return ""

    def _fail_links_unseen(self, unseen: str, dt: float) -> tuple[bool, str]:
        """Fail the fabric check on a link drop no traffic pass could see (spec 03 I28): a measured
        fault, so the ladder runs, then the 10-min ceiling and the host rung; never a release."""
        self.last_fabric_ok = False
        health_event("fabric_links_unseen", detail=unseen[:400])
        log = self._logger()
        if log:
            log.error(f"FABRIC {unseen}")
        return False, f"fabric check FAILED ({dt:.0f}s): {unseen[:400]}"

    def links_unseen_by_traffic(self) -> str:
        """The eth link drop a passing traffic pass could not have seen, or "" (spec 03 I28).

        With no cabling descriptor the validator builds its mesh from the links it discovers, so a
        link that went down is simply left out and the traffic over the rest passes. The last eth
        read's count against the high-water mark is then the only measurement of the loss, so the
        pass cannot vouch for the fabric: it fails the check rather than release a mesh short a link.
        With a descriptor the validator compares the links itself and names what is missing."""
        if not self.last_eth_link_drop or fabric._validator_paths()[2]:
            return ""
        return f"{FABRIC_LINKS_UNSEEN}: {self.last_eth_link_drop}"

    def _on_topology_mismatch(self, marker: str) -> None:
        """An OK verdict that carries a topology mismatch: journal it, name the trays that moved
        since the last clean pass, page the operator once per distinct mismatch, and watch the
        next job (the lift is bounded by that one job, see note_job_after_topology_lift)."""
        moved = moved_trays(_load_asic_bus_map(), _asic_bus_map())
        missing, extra = fabric.marker_ports(marker)
        # A lift that would have FAILED without the accept list: the ports it excused, by name (I71).
        excused = bool(fabric.links_missing(marker))
        accepted = sorted(set(missing) & fabric.accepted_missing_ports()) if excused else []
        self.fabric_topology = {
            "verdict": "lift",
            "missing_ports": missing,
            "extra_ports": extra,
            "accepted_ports": accepted,
            "marker": marker,
        }
        health_event(
            "fabric_topology_mismatch",
            marker=marker,
            moved_trays=moved,
            missing_ports=missing,
            extra_ports=extra,
            accepted_ports=accepted,
        )
        self.topology_lift_pending = True
        # Only a lift over ports the operator accepted as dead may lower the link mark (I71).
        self._arm_eth_rebaseline(accepted=excused)
        log = self._logger()
        if log:
            log.warning(
                f"FABRIC TOPOLOGY MISMATCH: {marker}"
                + (f"; moved: {', '.join(moved)}" if moved else "")
                + (f"; excused by TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS: {', '.join(accepted)}" if accepted else "")
            )
        key = marker + "|" + ",".join(moved)
        if key not in self._topology_alerted:
            self._topology_alerted.add(key)
            alert.send_alert(
                {"kind": "fabric_topology_mismatch", "detail": marker, "moved_trays": moved},
                log,
            )

    def note_job_after_topology_lift(self, failed: bool, log_tail: Optional[str]) -> Optional[str]:
        """Called once per finished job. Only the first job after a topology-mismatch lift
        counts: if it failed on the fabric, the skipped links are a fault. The fabric reads
        measured-bad and the cached mismatch goes, so the next pass runs the descriptor again, and
        while this latch stands a mismatch FAILS the check instead of lifting (I42). The hold this
        makes takes the ladder like any measured fabric fault: a reset, then a power cycle. The
        operator is paged by the ladder's own end, never by this latch. Returns the hold reason,
        or None."""
        if not self.topology_lift_pending:
            return None
        self.topology_lift_pending = False
        sig = fabric.job_fabric_error(log_tail or "") if failed else None
        if not sig:
            return None
        self.topology_hw_fault = (
            f"hardware fault: the first job after a topology-mismatch lift failed on the fabric ({sig}); "
            "a mismatch now fails the check, so a reset and then a power cycle run"
        )
        self.last_fabric_ok = False
        _forget_topology_mismatch()
        health_event("fabric_topology_hw_fault", detail=self.topology_hw_fault)
        log = self._logger()
        if log:
            log.error(f"FABRIC {self.topology_hw_fault}")
        return self.topology_hw_fault

    def clear_topology_hw_fault(self, why: str) -> None:
        """End the I42 latch. Only a clean fabric pass on the full topology (no mismatch, no link the
        traffic missed) calls this; an operator reset, a ladder rung or the 10-min bound keeps it, so
        a post-reset mismatch still FAILS the check."""
        if not self.topology_hw_fault:
            return
        health_event("fabric_topology_hw_fault_cleared", detail=self.topology_hw_fault, why=why)
        log = self._logger()
        if log:
            log.info(f"FABRIC topology hardware-fault latch cleared ({why})")
        self.topology_hw_fault = ""

    def note_job_fabric_route_fault(self, failed: bool, log_tail: Optional[str]) -> Optional[str]:
        """Called once per finished job (spec 03 I46). A job that dies on the fabric's routing right
        after a fabric pass that could not run (a 77) ran on a mesh no pass cleared. After
        ``TT_DEVICE_MCP_FABRIC_ROUTE_FAULT_JOBS`` (default 2) such jobs in a row, resetting is shown
        futile (each reset ends in another 77 and the next job dies the same way), so this declares a
        known fabric fault: journal it, page once, and return the hold reason. Any other job
        outcome, or a fabric pass that measured, breaks the streak. Returns the reason, or None."""
        sig = fabric.job_fabric_error(log_tail or "") if failed else None
        if not sig or not self.last_fabric_skipped:
            self._fabric_route_fault_streak = 0
            return None
        self._fabric_route_fault_streak += 1
        limit = _env_int("TT_DEVICE_MCP_FABRIC_ROUTE_FAULT_JOBS", 2, 1)
        if self._fabric_route_fault_streak < limit:
            return None
        self.fabric_known_fault = (
            f"known fabric fault: {self._fabric_route_fault_streak} jobs in a row failed on the fabric ({sig}) "
            "after fabric checks that could not run (77); held without a reset, needs an operator"
        )
        self._fabric_known_fault_at = time.monotonic()
        health_event("fabric_known_fault", detail=self.fabric_known_fault)
        log = self._logger()
        if log:
            log.error(f"FABRIC {self.fabric_known_fault}")
        alert.send_alert({"kind": "fabric_known_fault", "detail": self.fabric_known_fault}, log)
        return self.fabric_known_fault

    def _declare_cable_fault(self, detail: str) -> None:
        """A fabric pass found cables missing (spec 03 I47): open the known fabric fault (I46) at once,
        journal it, page once per distinct fault. A fault already open is left as it is, so its
        bound still runs from the first declaration."""
        if self.fabric_known_fault == detail:
            return
        self.fabric_known_fault = detail
        self._fabric_known_fault_at = time.monotonic()
        health_event("fabric_known_fault", detail=detail, cause="missing_cable")
        log = self._logger()
        if log:
            log.error(f"FABRIC HARDWARE FAULT: {detail}")
        if detail not in self._cable_fault_alerted:
            self._cable_fault_alerted.add(detail)
            alert.send_alert({"kind": "fabric_known_fault", "cause": "missing_cable", "detail": detail}, log)

    def known_fabric_fault(self) -> str:
        """The open known fabric fault, or "". Bounded: past ``TT_DEVICE_MCP_FABRIC_KNOWN_FAULT_HOLD_SEC``
        (default 21600 s) it expires and the usual 77 path (03 I40) gets one more try; the streak is
        left one short, so the next job that dies the same way declares it again at once."""
        if self.fabric_known_fault and (
            time.monotonic() - self._fabric_known_fault_at
            >= _env_int("TT_DEVICE_MCP_FABRIC_KNOWN_FAULT_HOLD_SEC", 21600, 60)
        ):
            health_event("fabric_known_fault_expired", detail=self.fabric_known_fault)
            self.fabric_known_fault = ""
            self._fabric_route_fault_streak = _env_int("TT_DEVICE_MCP_FABRIC_ROUTE_FAULT_JOBS", 2, 1) - 1
        return self.fabric_known_fault

    async def verify_eth_heartbeat(self, timeout_sec: float = 60.0) -> tuple[Optional[bool], str]:
        """Read the active-ethernet-core firmware heartbeats — a passive check that never pushes
        traffic — and say whether any core is frozen. Same override/built-in split as
        ``verify_fabric_health`` (see ``health.monitors.eth``): ``TT_DEVICE_MCP_ETH_HEARTBEAT_CMD``
        set names an operator's own command, judged on exit code alone; unset, this runs the
        built-in probe. Both self-block (see ``eth.build``) until the startup self-test has timed
        the read on this box and armed the rung — a disarmed reader skips, never holds.

        Returns ``(ok, detail)`` on the same contract as the fabric check:
          * ``None``  => SKIPPED (not configured/available, exit 77, or could not spawn) — say
            nothing, and fall through to the traffic pass.
          * ``True``  => every active-eth-core heartbeat is advancing.
          * ``False`` => a core's heartbeat is frozen, OR the read itself hung past ``timeout_sec``.
            A frozen core can hang the read, so THIS timeout is evidence of the wedge, not a skip —
            distinct from the built-in probe's OWN, tighter bound (``eth.probe_timeout_sec``, kept
            below ``timeout_sec`` on purpose), whose expiry is "the read did not finish in its own
            budget" and folds to a skip via ``classify_exit(None)``, mirroring the old wrapper's
            124-under-``timeout(1)`` case. The caller must route a ``False`` to HOLD and must NOT
            run the traffic pass, which would push the frozen chip off the PCIe bus. This matches
            verify_fabric_health, whose own timeout is also False.
        Never raises.

        Wrapped by a thin timer that reports the "eth_heartbeat" probe metric — the label
        HealthMonitor.update() already uses for this Observation (see ``_record`` calls below)."""
        _t0 = time.monotonic()
        # The drop belongs to the read that measured it: a read that counted no links (a crash,
        # the probe's own timeout, a 77, a disarmed reader, an override) leaves none behind, or an
        # old drop would fail a pass on a mesh the reset since healed (I28).
        self._eth_drop_measured = False
        try:
            ok, detail = await self._verify_eth_heartbeat_body(timeout_sec)
        finally:
            if not self._eth_drop_measured:
                self.last_eth_link_drop = ""
        metrics.probe_observed("eth_heartbeat", _verdict_label(ok), time.monotonic() - _t0)
        return ok, detail

    async def _verify_eth_heartbeat_body(self, timeout_sec: float) -> tuple[Optional[bool], str]:
        # Spec 03 I66: the reader maps every chip, so a chip lost and still attached keeps it from
        # running at all (outside this broker's own reset step). Learned nothing, never a pass: the
        # heartbeat check already fails on that chip.
        lost = await asyncio.to_thread(chip_state.unreadable_lost)
        if lost:
            return None, f"skipped (chip(s) {','.join(lost)} lost and still attached: no eth read of the mesh)"
        override = self._eth_heartbeat_cmd()
        # build() can shell out up to three candidate pythons (health.monitors.eth.resolve_python)
        # on a cache miss once armed — off the event loop, or a health gate stalls the job queue,
        # MCP requests, and the sd_notify watchdog ping for as long as those subprocess.run calls
        # take.
        built = await asyncio.to_thread(eth.build)
        if built is None:
            # Nothing to check with: the rung is disarmed (override or built-in alike — the
            # startup self-test's disarm must keep an untimed reader from delivering a HOLD),
            # or no reader is wired (no python imports ttexalens, or the probe is missing). All
            # collapse to the same "learned nothing" signal, but NOT the same journal key:
            # unavailable() (off-loop — it re-resolves) keys the skip not_armed vs not_configured
            # with the actionable cause in the detail, so a host whose cause crosses that boundary
            # over the process lifetime journals both sides.
            key, reason = await asyncio.to_thread(eth.unavailable)
            self._journal_skip_once("eth_heartbeat_unavailable", key, detail=reason[:400])
            return None, f"skipped (no runnable eth-heartbeat read: {reason})"
        argv, env = built
        # The runtime-root cwd is a built-in-path concern only, mirroring verify_fabric_health:
        # ttexalens resolves relative paths off the tree its python was built from, but an
        # operator's override runs wherever the broker itself runs, exactly as before this module
        # existed.
        cwd = None if override else env.get("TT_METAL_RUNTIME_ROOT")
        # The override has no separate probe-timeout knob (never did): it uses the caller's own
        # bound outright. The built-in path's own knob (eth.probe_timeout_sec) is an operator
        # value from /etc/default, not something this code can trust to stay below timeout_sec on
        # its own — enforced here so a misconfigured TTDEV_ETH_CHECK_TIMEOUT can never collapse the
        # two distinct timeout outcomes (a merely-slow read skipping vs. the caller's own hang
        # evidence) into one.
        probe_timeout = timeout_sec if override else min(eth.probe_timeout_sec(), timeout_sec * 0.9)
        # A reader killed on its own timeout is waited on until it has exited (bounded), so the
        # holder scan the idle relift's escalation runs next does not find its pid still listed by
        # the driver and defer the reset as an incomplete scan. Capped to half the slack under the
        # outer bound, so the wait never turns a skip into the outer bound's FROZEN verdict.
        reap_sec = min(ETH_REAP_SEC, max(0.0, (timeout_sec - probe_timeout) / 2))

        # Survives track(None): run_probe's own `finally` clears fabric_check_proc on every exit
        # path, INCLUDING when the outer wait_for below cancels this call — a CancelledError
        # unwinds through run_probe without ever reaching its own `except TimeoutError`, so
        # run_probe never kills the group in that case. Without an independent holder, the except
        # block below would have nothing left to killpg, and the reader would outlive a HOLD
        # verdict, still attached to the wedged mesh.
        held: dict[str, Optional[asyncio.subprocess.Process]] = {"proc": None}

        def _track(proc: Optional[asyncio.subprocess.Process]) -> None:
            # Tracked so a dead chip can kill it (see server._kill_device_holders). It touches
            # chip memory, so a chip leaving the bus mid-read is the likeliest thing still reading
            # a dead address.
            self.fabric_check_proc = proc
            if proc is not None:
                held["proc"] = proc

        _t0 = datetime.now()
        try:
            rc, text = await aio.wait_for(
                eth.check(argv, env, timeout_sec=probe_timeout, track=_track, cwd=cwd, reap_sec=reap_sec),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            # eth.check() bounds itself to probe_timeout (< timeout_sec for the built-in path)
            # and always returns rather than raising; reaching HERE means even that bound plus its
            # own killpg cleanup did not complete inside timeout_sec, which is itself frozen-core
            # evidence, not a skip. The process itself is still alive at this point (see `held`
            # above) — kill it now, or a HOLD verdict leaves a reader attached to a wedged mesh.
            proc = held["proc"]
            if proc is not None:
                await chip_state.await_reader_release(proc.pid)  # held by an AER stop (spec 03 I92)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            return False, f"eth-heartbeat read hung past {timeout_sec:.0f}s (a frozen core can hang the read)"
        except Exception as e:  # noqa: BLE001 - never raise into the gate
            # Wired but could not spawn — a configured rung silently producing no verdict is the
            # degrade worth a loud record, unlike the sanctioned unavailable default above.
            self._journal_skip_once("eth_heartbeat_unavailable", "not_runnable", detail=str(e)[:400])
            if not override:
                eth.forget_python()  # the cached python may be what cannot spawn
            return None, f"skipped (eth-heartbeat read not runnable: {e})"

        dt = (datetime.now() - _t0).total_seconds()
        last = text.splitlines()[-1][:200] if text else "(no output)"
        if override:
            # An operator's own command has already done its own interpretation and reports it
            # purely through its exit code — its stdout is never run through classify_exit(),
            # which means something only for the built-in probe's own sentinel (3).
            ok = True if rc == 0 else None if rc == FABRIC_CHECK_CANNOT_CHECK_RC else False
            reason = last
        else:
            ok, reason = eth.classify_exit(rc)
            links = eth.parse_link_count(text)
            # A down link is invisible to the verdict above (the probe skips it), so a drop in the
            # measured count turns an all-advancing read into "could not vouch": the caller then
            # runs the traffic pass, which is what tests every link. A frozen verdict stays frozen.
            drop = self.eth_link_drop(links) if links else ""
            if ok is True and drop:
                self._journal_skip_once("eth_heartbeat_unavailable", "link_count_drop", detail=drop[:400])
                return None, f"{ETH_LINK_DROP_SKIP}: {drop}"

        if ok is None:
            # The check could not run — it learned nothing about the eth cores, so falling
            # through to the traffic pass is correct, but a configured rung that got no verdict
            # deserves a loud record, mirroring the fabric check's own rc-77 path.
            self._journal_skip_once("eth_heartbeat_unavailable", "could_not_check", detail=last[:400])
            if not override and rc == 1:
                # A crash, which a python that lost ttexalens in place causes: re-resolve next time.
                # Not on a timeout or the probe's own 77: that python imported and ran the probe.
                eth.forget_python()
            return None, f"{ETH_COULD_NOT_CHECK}: {reason}"
        if ok is False:
            return False, f"a frozen active-eth core ({dt:.0f}s): {reason}"
        return True, f"all active-eth cores advancing ({dt:.0f}s)"
