# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Galaxy recovery: the mesh-wide ``tt-smi -glx_reset``, its gentlest-first routing (a per-chip
bridge reset before the mesh-wide reset; a host power cycle only once a reset has already
run and failed), and the explicit cascade router that names the rung each degradation justifies —
the one the between-job health gate now steers on.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp import alert, metrics
from tt_device_mcp.device_holders import discount_broker_internal, holder_comm, is_tenant, username_for_uid
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitor import _UNIDENTIFIED_BOARDS, _normalized_board
from tt_device_mcp.health.monitors.heartbeat import offbus_chip_ids, offbus_count, unreadable_on_bus_chips
from tt_device_mcp.health.monitors.hostpci import link_down
from tt_device_mcp.health.monitors.pci import chip_node_present, chip_pci_bdfs, pci_bus_number, removed_chip_ids
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import (
    BELOW_FLOOR,
    BLOCKED,
    DEFER,
    HOLD_FABRIC_UNVERIFIED,
    OUTCOME_RECOVERED,
    OUTCOME_TERMINAL,
    OUTCOME_WAITING,
    RELEASE,
    STAGE_BRIDGE_RESET,
    STAGE_POWER_CYCLE,
    STAGE_SMI_RESET,
    WAIT,
    Evidence,
    Recovery,
    offbus_power_cycle,
    pcie_guard,
)
from tt_device_mcp.health.recovery.base import (
    CLASS_A_LADDER_AFTER_SEC,
    CYCLE_LAUNCH_REPAGE_SEC,
    HOLD_ESCALATION_REARM_SEC,
    drop_returned_isolated,
    refuse_mesh_reset,
)
from tt_device_mcp.health.recovery.stages.bridge_reset import gone_chip_bridge_reset_enabled
from tt_device_mcp.health.recovery.stages.power_cycle import run_pre_power_cycle_hook

_logger = logging.getLogger("tt-device-mcp")


def _is_galaxy(board_types: Optional[list], glx_board_types: tuple) -> Optional[bool]:
    """Whether this is a Galaxy, or None when it cannot be told, from an already-cached tt-smi
    snapshot. Never probes: ``board_types``/``glx_board_types`` are injected by the caller (see
    ``select_recovery``), which reads only what a health snapshot already cached — safe to call
    from the event loop.

    Unanimous or unknown. tt-smi does NOT validate the flag — ``-glx_reset`` runs
    ``glx_6u_trays_reset`` before any backend exists and never consults its own Galaxy check — so a
    wrong verdict here fires a tray reset on hardware that cannot take one, with nothing to refuse
    it. Any board tt-smi could not identify (it prints "N/A" whenever a board-id or ARC read fails,
    i.e. exactly on a degraded Galaxy) and any disagreement between boards therefore yield None,
    which keeps the loud reset_mode_unknown guard armed.

    Matching is on the normalized value (see ``_normalized_board``), because the snapshot suffixes
    wormhole types " L"/" R" while tt-smi compares the unsuffixed name.
    """
    if not glx_board_types or not board_types:
        return None
    verdicts = set()
    for raw in board_types:
        board = _normalized_board(raw)
        if board in _UNIDENTIFIED_BOARDS:
            return None
        verdicts.add(board in glx_board_types)
    return verdicts.pop() if len(verdicts) == 1 else None


def _reboot_min_dead_chips(expected: int) -> int:
    """Fewest dead chips that justifies the destructive power-cycle rung (the only host rung).

    Below this the wedge is a single/few-chip active-eth-core freeze that clears itself while the holder-kill covers the dead-BAR-read MCE path — so a power cycle there kills
    every in-flight tenant/agent for a fault that heals on its own. At or above it the mesh is
    mostly gone, which self-heal does not recover. Floored at 2 so one dead chip never power-cycles the host;
    the fraction is per-host tunable and a malformed value falls back rather than crashing start."""
    try:
        frac = float(os.environ.get("TT_DEVICE_MCP_REBOOT_MIN_DEAD_FRAC", "0.5"))
    except ValueError:
        frac = 0.5
    if not (0.0 < frac <= 1.0):
        frac = 0.5
    return max(2, math.ceil(expected * frac))


def _hold_gate_liftable() -> bool:
    """The gate refuses off-bus resets only because ``TT_DEVICE_MCP_HOST_RESET_GATE=hold`` is configured:
    no I25 host-hang latch and no ``TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC=off``. Only such a hold's
    operator hand-off ends in the broker's own ladder (spec 04 I33)."""
    return (
        pcie_guard.offbus_reset_latched() is None
        and pcie_guard.gate_mode() == pcie_guard.GATE_HOLD
        and offbus_power_cycle.hold_handoff_sec() is not None
    )


def _galaxy_reset_min_dead_chips(expected: int) -> Optional[int]:
    """Fewest off-bus chips that justifies the mesh-wide galaxy reset. Defaults to the mass-drop
    floor; None only when an operator explicitly disables it (frac <= 0), restoring the old
    reset-any-unhealthy behavior.

    The galaxy reset is the measured all-chip drop: run against a single/few-chip off-bus wedge it
    takes the mesh with it — one chip off the bus, one -glx_reset, and the other 31 go to
    0xFFFFFFFF until a power cycle. That wedge is an active-eth-core freeze that self-heals on its own; only a mass drop, which self-heal does not clear, is worth the reset. So
    a below-floor drop HOLDS (self-heal works, no tenant runs on the held mesh) and only a mass drop
    resets. This floor gates the reset itself.

    Default-safe: unset -> the mass-drop floor (frac 0.5), because a single
    galaxy reset on a single-chip wedge is the measured way the whole mesh is lost, and the idle
    relift now lifts a below-floor hold on its own so holding costs no stuck device. The old
    reset-any-unhealthy-device behavior is an explicit opt-out — TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC
    set to 0 (or negative) reads as disabled. A value above 1.0 is not a valid fraction — almost
    always a chip COUNT typed in its place — so it fails safe to the default floor rather than
    silently disabling it. Floored at 2 so one dead chip never trips it; a malformed value likewise
    falls back to the default rather than crashing the broker at start."""
    raw = os.environ.get("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "").strip()
    if not raw:
        frac = 0.5
    else:
        try:
            frac = float(raw)
        except ValueError:
            frac = 0.5
        else:
            if math.isnan(frac):
                frac = 0.5  # a NaN is as malformed as an unparseable value — fail safe, never ceil(nan)
            elif frac <= 0.0:
                return None  # 0 or negative is the operator opt-out to the old reset-any behavior
            elif frac > 1.0:
                # A fraction cannot exceed 1.0, so a value like 16 is a chip COUNT typed where a
                # fraction belongs. Disabling the floor on it would silently restore the reset-any
                # hazard the floor guards against, so fail safe to the default instead of opting out.
                logging.getLogger("tt-device-mcp").warning(
                    "TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC=%s is not a fraction in (0,1]; using the "
                    "default 0.5 floor (set 0 to disable the floor).",
                    raw,
                )
                frac = 0.5
    return max(2, math.ceil(expected * frac))


def _eth_freeze_holds() -> bool:
    """Whether a frozen active-eth-core heartbeat routes the gate to HOLD rather than a reset.
    ON by default: enum + ARC + snapshot have all passed by the time a frozen verdict exists, so
    it is a single-chip eth-firmware freeze that self-heals, and the galaxy reset the
    unhealthy path would run instead is the measured drop that takes all 32 chips to 0xFFFFFFFF. The
    kill switch (TT_DEVICE_MCP_ETH_FREEZE_HOLD=0) restores that reset for an operator who needs it."""
    return os.environ.get("TT_DEVICE_MCP_ETH_FREEZE_HOLD", "1").strip() != "0"


# --- idle/forced hold-escalation predicates -----------------------------------------------------
#
# Pure env reads and pure functions of their own arguments — nothing server-side to inject, so
# these travel with the escalation bodies that call them (GalaxyRecovery.escalate() and its
# siblings below) rather than staying behind as injected deps.


def _stuck_hold_reset_enabled() -> bool:
    """Whether an idle hold that has stood past the ceiling may escalate to the galaxy reset the
    gate would run at the next job. ON by default; TT_DEVICE_MCP_STUCK_HOLD_RESET=0 is the operator
    kill-switch.

    The read-only relift lifts a hold the moment the mesh proves fit, but it cannot lift a
    present-mesh eth/fault hold no read the broker owns can clear (no eth reader), so that hold
    stands until the next job's gate resets it — and an idle box has no next job, so read-only it
    stands indefinitely. This trigger moves that gate reset to the idle timeout so no hold outlives
    the ceiling. It is on by default because an unbounded hold is the failure it exists to prevent;
    a healthy mesh lifts in the first relift and never reaches it, the escalation fires only on an
    idle, tenant-free, present mesh past the ceiling, is rate-limited to one reset per episode, and
    climbs to the power cycle if the reset does not recover rather than looping or sitting."""
    return os.environ.get("TT_DEVICE_MCP_STUCK_HOLD_RESET", "1").strip() == "1"


def _offbus_hold_escalate_enabled() -> bool:
    """Whether an idle OFF-BUS hold that has stood past the ceiling may escalate to the recovery
    cascade. ON by default; TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE=0 is the operator kill-switch. The
    present-mesh sibling (_stuck_hold_reset_enabled) closes the eth/fault strand; this closes the
    twin an off-bus drop leaves — a chip that left the bus, held below the reset floor, which the
    read-only relift cannot lift (still off the bus) and which an idle box has no next-job gate to
    run the gone-chip cascade on, so read-only it stands forever. On by default because that hold is
    unbounded otherwise; it fires only on an idle, tenant-free drop past the ceiling, is rate-limited
    to one recovery per episode, and climbs to the power cycle if the reset does not recover."""
    return os.environ.get("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "1").strip() == "1"


def _stuck_hold_ceiling_sec() -> int:
    """Seconds a GENERIC hold may stand before the ladder climbs through it to a power cycle. The hard
    ceiling on any hold. Default 600 (10 min): a reset brings a recoverable drop back immediately, so a
    hold still standing at the ceiling has already had every gentler rung; the box is idle and
    tenant-refused, so climbing then costs nothing a longer wait would save. Parsed defensively — a
    malformed or non-positive value reads as the default, never 0 (which would escalate the instant
    a hold latched). Never above 600 (spec 04 I76): the env may shorten the bound, never lengthen it."""
    raw = os.environ.get("TT_DEVICE_MCP_STUCK_HOLD_SEC", "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 600
    return v if 0 < v <= 600 else 600


def _offbus_hold_ceiling_sec() -> int:
    """Seconds an OFF-BUS hold waits before its first forced escalation. Default 120, an order
    below the general ceiling, because the two faults differ in kind: the 600s ceiling is
    calibrated for a present-mesh ARC wedge, which does self-heal given time, whereas a chip
    that has LEFT the PCIe bus never returns by waiting — its endpoint is gone until something
    re-enumerates it. Measured on this loudbox: a 2-chip off-bus hold sat the full 1200s doing
    nothing, and the bridge-reset + PCI rescan it then ran brought both chips back in 3 seconds.
    Waiting only buys the tenants a 20-minute outage. Parsed defensively like the general
    ceiling — malformed or non-positive reads as the default, never 0."""
    raw = os.environ.get("TT_DEVICE_MCP_OFFBUS_HOLD_SEC", "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 120
    return v if v > 0 else 120


def _settle_before_host_rung_sec() -> int:
    """Seconds to let the mesh re-enumerate after the last reset in a branch before the ONE verify
    that decides a power cycle — the tray branch's single settle, and the settle GENERIC runs before
    it hands the mesh to a host rung. NOT a self-heal wait: a reset brings a recoverable drop back
    at once; this only covers PCIe re-enumeration after the pulse. Env-overridable, parsed
    defensively — a malformed or non-positive value reads as the default (60)."""
    raw = os.environ.get("TT_DEVICE_MCP_SETTLE_BEFORE_HOST_RUNG_SEC", "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 60
    return v if v > 0 else 60


# The two off-bus fault classes that go to the power cycle with no further reset (spec 04 I37). A: one
# or more whole UBB trays off the bus (no whole-tray drop has healed without a power cycle: 0/50 on one
# Galaxy host). C: every chip still off the bus after a mesh reset already ran and failed this episode
# (one mesh reset has healed an all-off bus, so the first one still runs, at startup too; a repeat on
# the unchanged set ran 130-262 s and exited 1). Neither gets a sweep.
FAULT_CLASS_WHOLE_TRAY = "A"
FAULT_CLASS_ALL_OFF = "C"


def _power_cycle_direct_class(
    offbus_chips: set,
    expected: int,
    tray_map: Optional[dict],
    *,
    mesh_reset_failed: bool,
    present_buses: Optional[Mapping] = None,
    tray_groups=(),
) -> Optional[str]:
    """The I37 fault class of an off-bus set, or None when the ordinary ladder applies.

    ``FAULT_CLASS_ALL_OFF`` when every chip is off the bus and ``mesh_reset_failed``: a mesh reset
    already ran and failed on it, or the broker's own reboot or power cycle for this fault did not
    bring it back (the callers pass both, spec 04 I37). A whole-bus drop with no failed reset gets
    its one mesh reset first, at the startup gate as at run time: the startup gate also runs after a
    broker restart, and one mesh reset has brought a whole bus back there. ``FAULT_CLASS_WHOLE_TRAY``
    when the set is exactly one or more whole trays and at least one chip of the mesh is still on the
    bus. With no tray map (a broker that never read a full mesh, as on a boot that came up short),
    ``present_buses`` (``{chip id: PCI bus}`` of the chips still on the bus) and ``tray_groups`` (the
    UBB bus groups) decide it instead, see :func:`_whole_trays_absent`. Anything else — a
    partial-tray drop, a single chip, a host with fewer than two trays, or neither a map nor a
    presence read that says which chips share a tray — is None. Pure and side-effect-free."""
    if expected < 2 * UBB_CHIP_COUNT or not offbus_chips:
        return None
    if len(offbus_chips) >= expected:
        return FAULT_CLASS_ALL_OFF if mesh_reset_failed else None
    if _whole_tray_drop_trays(offbus_chips, expected, tray_map) is not None:
        return FAULT_CLASS_WHOLE_TRAY
    if tray_map is None and _whole_trays_absent(present_buses, expected, offbus_chips, tray_groups):
        return FAULT_CLASS_WHOLE_TRAY
    return None


def _whole_trays_absent(present_buses: Optional[Mapping], expected: int, offbus_chips, tray_groups) -> bool:
    """Whether the chips still on the bus fill whole trays and every chip off the bus is accounted for
    by trays with none left on it: class A read from presence alone (spec 04 I37), for a broker with
    no I16 tray map. See :func:`_absent_tray_groups`. Pure and side-effect-free."""
    return bool(_absent_tray_groups(present_buses, expected, offbus_chips, tray_groups))


def _absent_tray_groups(present_buses: Optional[Mapping], expected: int, offbus_chips, tray_groups) -> list:
    """The sorted UBB bus groups with no chip on the bus when the presence read is a clean whole-tray
    drop, else []. A tray's chips share one PCI bus group (``bus & 0xF0``, the key tt-smi's UBB tables
    use), so this needs no board type and no chip numbering, only the live buses.

    Non-empty only when the host has exactly ``len(tray_groups)`` trays of ``UBB_CHIP_COUNT`` chips,
    the chip ids missing from ``present_buses`` are exactly ``offbus_chips`` (a heartbeat read and a
    sysfs read that raced can never pair the wrong chips), every present chip sits in a known group,
    each group holds all or none of its chips, at least one group is empty and one full, and the empty
    groups' chips are exactly the off-bus count. Anything unreadable or out of shape is []: the
    ladder decides, as before. Pure and side-effect-free."""
    groups = set(tray_groups or ())
    off = {str(i) for i in offbus_chips or ()}
    if not present_buses or not groups or not off or len(groups) * UBB_CHIP_COUNT != expected:
        return []
    present_ids = {str(i) for i in present_buses}
    if len(present_ids) + len(off) != expected or {str(i) for i in range(expected)} - present_ids != off:
        return []
    counts = dict.fromkeys(groups, 0)
    for bus in present_buses.values():
        group = bus & 0xF0 if isinstance(bus, int) else None
        if group not in counts:
            return []
        counts[group] += 1
    if any(n not in (0, UBB_CHIP_COUNT) for n in counts.values()):
        return []
    empty = sorted(g for g, n in counts.items() if n == 0)
    return empty if 0 < len(empty) < len(groups) and len(empty) * UBB_CHIP_COUNT == len(off) else []


def _hold_age_sec(hold_since: str) -> Optional[float]:
    """Age of the hold episode in seconds, or None when the stamp is missing/unparseable — callers
    decide which way None fails (the deadline watchdog cannot re-drive a hold with no clock, so
    gates that defer on age must treat None as 'do not defer')."""
    if not hold_since:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(hold_since)).total_seconds()
    except ValueError:
        return None


def _present_mesh_reset_grace_sec() -> int:
    """Seconds a PRESENT-mesh eth/fabric-unverified hold may stand before the idle escalation
    galaxy-resets it, and the minimum spacing after any earlier reset — deliberately far SHORTER than
    the hold ceiling. A galaxy reset re-inits the eth cores in a ~60s recoverable pass and is the ONLY
    cure for a wedge enum+ARC cannot see (it does not self-heal by waiting); eth links re-train in
    seconds, so a wedge still present at the grace is genuine, not a training window. The escalation
    runs the hold's one mesh reset; once it failed, the hold climbs to the power cycle and the reset
    never runs again in the episode (spec 04 I87). Default
    120 (2 min). Parsed defensively — a malformed or non-positive value reads as the default,
    never 0."""
    raw = os.environ.get("TT_DEVICE_MCP_PRESENT_MESH_RESET_GRACE_SEC", "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 120
    return v if v > 0 else 120


def _stuck_hold_reset_due(
    hold_since: str, *, off_bus: int, now: Optional[datetime] = None, measured_fault: bool = False
) -> bool:
    """Whether an idle PRESENT-mesh hold has stood past the present-mesh reset grace and its class
    admits the gate's galaxy reset.

    True only for a PRESENT mesh (``off_bus == 0``). There a galaxy reset usually re-inits every eth
    core in a ~60s recoverable pass, clearing a frozen-but-present core, and resets a falsely-held
    healthy mesh needlessly but recoverably. This is NOT the gate's default for this class: the gate
    HOLDS a present-mesh eth/fault hold (it is below the reset floor) rather than reset it, because a
    galaxy reset on a present-but-eth-unverifiable mesh can itself drop every chip off the bus. The
    escalation accepts that bounded risk as the cure for an otherwise-unbounded idle strand — at the
    grace, not the hard ceiling, because an eth wedge does not self-heal by waiting longer. One reset
    per hold: a failed one climbs to the power cycle (spec 04 I87). An off-bus drop (``off_bus > 0``)
    is EXCLUDED: it self-heals and a galaxy reset there is the measured all-chip drop
    that inverts the mesh (1 off-bus -> 31), so it keeps holding to the ceiling. Age is read off the
    idle-latched episode clock; a missing or unparseable stamp fails closed (not due). The caller
    applies the arm switch, retry-spacing, no-tenant and reset-scope guards on top.

    ``measured_fault`` (spec 03 I56): the hold's last fabric pass MEASURED a failure on this present
    mesh (not a 77 skip), so the fault is already proven and the grace has nothing left to wait out
    — the reset is due at once. A post-job hold on a measured fabric FAIL is exactly the class the
    galaxy reset cures, and waiting the grace first only stretches its time-to-recover."""
    if off_bus != 0:
        return False
    if not hold_since:
        return False
    try:
        started = datetime.fromisoformat(hold_since)
    except ValueError:
        return False
    if measured_fault:
        return True
    now = now or datetime.now()
    return (now - started).total_seconds() >= _present_mesh_reset_grace_sec()


def _offbus_stuck_hold_due(hold_since: str, *, off_bus: int, now: Optional[datetime] = None) -> bool:
    """Whether an idle OFF-BUS hold has stood past the ceiling — the mirror image of
    ``_stuck_hold_reset_due``. True only for a drop (``off_bus > 0``): the chip left the bus and did
    not self-return within the window, so it is escalated to the recovery cascade. A PRESENT mesh
    (``off_bus == 0``) is EXCLUDED here — that is the eth/fault strand ``_stuck_hold_reset_due``
    owns, and routing it through this off-bus path would double-arm it. Age is read off the same
    idle-latched episode clock; a missing or unparseable stamp fails closed."""
    if off_bus <= 0:
        return False
    if not hold_since:
        return False
    try:
        started = datetime.fromisoformat(hold_since)
    except ValueError:
        return False
    now = now or datetime.now()
    return (now - started).total_seconds() >= _stuck_hold_ceiling_sec()


def _choose_recovery_escalation(*, auto_power_cycle_enabled: Callable[[], bool]) -> Optional[str]:
    """The host-level recovery rung to attempt after a reset failed to revive the mesh, or None if
    the host has none armed. The power cycle is the only host rung: it brings the host OS down
    before power goes and re-powers the UBBs. There is no warm reboot rung: it never recovered a
    device a power cycle would not have, and a Galaxy's UBBs stay unpowered-through across it.

    ``auto_power_cycle_enabled`` is injected (server.py's own predicate) rather than read from a
    module global, so this stays free of a server<->health import cycle."""
    return "power-cycle" if auto_power_cycle_enabled() else None


def _host_escalation_for_drop(
    off_bus: int,
    expected: int,
    *,
    auto_power_cycle_enabled: Callable[[], bool],
) -> tuple[Optional[str], bool]:
    """The host recovery rung to fire for an ``off_bus``/``expected`` drop, plus whether no host rung
    can run, so the caller must page and keep holding (spec 04 I71). Every host-rung path calls this.

    The power cycle is the one host rung. Armed, it is returned for every drop (a present mesh that
    survived the reset ladder included). Not armed, ``(None, True)``: the caller MUST emit the loud
    actionable event and KEEP HOLDING; nothing else may stand in for the cycle. A power cycle the
    rate limits or the I65 cooldown deny, or one that fails to launch, is held and asked again when
    the wait ends or on the next pass (I10, I65, I76). Pure and side-effect-free; the caller acts on
    the result."""
    if auto_power_cycle_enabled():
        return "power-cycle", False
    return None, True


# The ledger/auto_recovery_allowed vocabulary — what record_auto_recovery(action=...) and every
# auto_recovery_* event have always written, so it is durable and never translated. These two maps
# are the ONE place a host rung crosses between it and the stage-name vocabulary `_route` returns.
CASCADE_POWER_CYCLE = "power-cycle"  # a chassis power cycle, host OS brought down first
_HOST_ESCALATION_STAGE = {CASCADE_POWER_CYCLE: STAGE_POWER_CYCLE}
# What the failed-reset climb returns when no host rung (the power cycle) is available to it.
NO_HOST_RUNG = "no_host_rung"
# And back, for the gate: once the router has named a host rung it needs the ledger's own word for
# it again — `auto_recovery_allowed(action)`, `_journal_auto_recovery_denied(action, ...)`.
_HOST_ESCALATION_ACTION = {stage: action for action, stage in _HOST_ESCALATION_STAGE.items()}


def _route(
    *,
    healthy: bool,
    fault_reported: bool,
    fabric_ok: Optional[bool],
    fabric_ran: bool,
    dirty: bool,
    fabric_forced: bool,
    off_bus: int,
    frozen_chips: int,
    expected: int,
    sbr_candidates: int,
    galaxy_floor: Optional[int],
    reboot_floor: int,
    eth_frozen: bool,
    cooling: bool,
    reset_scope_active: bool,
    was_failing: bool,
    last_action: Optional[str],
    last_action_recovered: Optional[bool],
    host_escalation: Optional[str],
    reboot_blocked: bool,
    holding_fabric_unverified: bool = False,
    dispatch_reset_due: bool = False,
    eth_timeout_reset_due: bool = False,
    kill_reset_due: bool = False,
) -> str:
    """The single gentlest-first recovery rung justified by the current degradation — THE escalation
    policy, reached through :class:`GalaxyRecovery`'s ``next_stage``, which folds the host-rung
    inputs and derives this platform's own floors before calling it.

    The gate is a two-phase machine — it picks an action, runs it, then asks again about the
    outcome — and this router names the rung for each phase, selected by ``last_action``/
    ``last_action_recovered``. A stateless "what is the next rung" cannot represent it faithfully,
    because the host rung is a decision reachable only AFTER a galaxy reset has already run
    and failed, and a bridge-reset recovery is a release with its own outcome semantics,
    distinct from a fresh pass's release.

    ``last_action_recovered is True`` — whichever rung ran (bridge reset, galaxy reset, or per-tray
    BMC reset) just recovered the mesh -> ``RELEASE``. Which fault bookkeeping accompanies the
    release (retiring a runtime-reported fault or leaving it set) is gate-local, not a router
    concern — every recovering rung reads the same here.

    ``last_action == STAGE_SMI_RESET and last_action_recovered is False`` — the galaxy reset this
    pass ALREADY ran and did not recover; only now is a host rung reachable:

    * ``reboot_blocked`` and no reset scope is live -> ``BLOCKED``: no opted-in rung can recover
      this drop (a whole-bus drop, or a reset that regressed/hard-failed a partial one, with no
      power cycle opted in to catch it) — distinct from
      ``WAIT``, which means the wedge may still self-heal. A live scope's reads are not a verdict,
      so it waits on the scope first.
    * a host rung is opted in (``host_escalation``), a prior cycle failed too (``was_failing`` — one
      bad pass is not "clearly won't recover"), no reset is still cycling, and the drop is a mass
      drop OR a present mesh that survived the reset still unhealthy (``off_bus == 0 or
      off_bus >= reboot_floor``) -> that rung: ``STAGE_POWER_CYCLE``, the only host rung (I71).
      ``off_bus == 0`` climbs on its own because the reboot floor is floored at 2 and could never
      otherwise be satisfied by a present mesh — an eth/fabric wedge the reset could not clear,
      which self-heal will not either since it already survived the deepest reset.
    * anything short holds (``WAIT``). The gate applies its no-tenant and rate-limit guards on top
      before acting; this only names the rung the ladder selects.

    Anything else — ``last_action is None`` (nothing ran yet this pass), or ``STAGE_BRIDGE_RESET``
    that ran and did NOT recover (falls through to the same floor check a fresh pass would reach) —
    runs the pre-action ladder:

    * ``healthy and not fault_reported`` — every check the host can run agrees the mesh is fine and
      the runtime named no fault:

      * ``fabric_ok is None and (dirty or (fabric_forced and expected > 1))`` -> the fabric pass RAN
        but returned no verdict (a 77) on a dirty or multi-chip host — enum+ARC agreeing is not
        proof against a wedge they cannot see, so hold fabric-unverified rather than release or
        reset (``HOLD_FABRIC_UNVERIFIED``).
      * otherwise -> ``RELEASE``.
    * ``eth_frozen`` -> ``WAIT``: a stuck active-eth-core heartbeat holds ahead of the off-bus
      count (the gate's first check) — a galaxy reset can neither see nor safely clear it.
    * ``cooling`` -> ``WAIT``: a prior reset failed and its cooldown has not elapsed; hammering a
      dead endpoint is what escalates a PCIe error to fatal, so the gate sits it out.
    * ``reset_scope_active`` -> ``DEFER``: a reset is cycling in its own PID-1 scope; the gate
      skips the surgical and floor rungs and adopts+verifies that reset, not a second one.
    * ``sbr_candidates > 0`` (and a bridge reset did not JUST fail) -> ``STAGE_BRIDGE_RESET``: chips
      eligible for a per-chip bridge reset (all-ones-isolated, or a below-floor gone chip when
      opted in) get that surgical rung first.
    * ``dispatch_reset_due`` with every chip present and none frozen -> ``STAGE_SMI_RESET``: the
      fabric passed traffic but mesh dispatch failed, which no self-heal clears; the caller grants
      this once per hold episode, and the full check, fabric included, decides the release
      (spec 03 I55). ``eth_timeout_reset_due`` takes the same rung: a clean post-job eth read timed
      out with no verdict on a present mesh (spec 03 I61), and so does ``kill_reset_due``: a job the
      broker stopped mid-run, by its timeout or a requested kill (spec 03 I69).
    * below the galaxy floor with nothing to surgically reset -> ``BELOW_FLOOR``: not a rung. The
      gate classifies the drop — a whole tray off the bus goes straight to the bounded power cycle
      (spec 04 I37), anything else holds for the 10-min bound's full ladder (I76) — because a
      single/few-chip wedge self-heals otherwise, and a galaxy reset there is the measured
      all-chip drop. There is no BMC tray re-power rung (spec 04 I23). The floor is keyed on every WEDGED chip (``off_bus + frozen_chips``), not the
      off-bus count alone — an all-present, all-ARC-frozen mesh has nothing off the bus to invert,
      so a reset is the only remedy, and counted by off-bus alone it would read as 0 and hold below
      the floor forever. This is the branch that must NOT climb to a mesh-wide reset over a
      single-chip wedge.
    * otherwise -> ``STAGE_SMI_RESET``: the hold's one mesh reset. Once it ran and failed, the gate
      skips it for the rest of the episode and reports the failed rung, so the escalation above
      climbs (spec 04 I87).

    ``galaxy_floor`` is None only when an operator opts out to the old reset-any-unhealthy behavior;
    then any off-bus chip resets. ``host_escalation``/``reboot_blocked`` are the caller's already-
    folded verdict — :func:`_host_escalation_for_drop`'s result (``GalaxyRecovery.next_stage``
    derives it from ``off_bus_before``/``reset_exit_nonzero``, never this function, which stays pure and
    side-effect-free: the gate does the acting, this only names the rung)."""
    if last_action_recovered:
        return RELEASE
    if last_action == STAGE_SMI_RESET and last_action_recovered is False:
        if reboot_blocked and not reset_scope_active:
            return BLOCKED
        if (
            host_escalation is not None
            and was_failing
            and not reset_scope_active
            and (off_bus == 0 or off_bus >= reboot_floor)
        ):
            return _HOST_ESCALATION_STAGE.get(host_escalation, host_escalation)
        return WAIT
    if healthy and not fault_reported:
        if fabric_ok is None and (dirty or holding_fabric_unverified or (fabric_forced and expected > 1)):
            return HOLD_FABRIC_UNVERIFIED
        return RELEASE
    if eth_frozen:
        return WAIT
    if cooling:
        return WAIT
    if reset_scope_active:
        return DEFER
    if sbr_candidates > 0 and last_action != STAGE_BRIDGE_RESET:
        return STAGE_BRIDGE_RESET
    if (dispatch_reset_due or eth_timeout_reset_due or kill_reset_due) and off_bus == 0 and frozen_chips == 0:
        return STAGE_SMI_RESET
    if galaxy_floor is not None and off_bus + frozen_chips < galaxy_floor:
        return BELOW_FLOOR
    return STAGE_SMI_RESET


# --- UBB trays -------------------------------------------------------------------------------
#
# A Galaxy's 32 ASICs sit on 4 UBB trays of 8: a tray-level power/link failure drops its whole tray
# off the PCIe bus at once, the measured failure that took chips 0-7 down together. There is no
# per-tray BMC re-power rung (spec 04 I23): its pulse dropped every other tray and devices outside
# the mesh. A whole-tray drop goes to the power cycle (I37).
#
# Which chips belong to which tray is NOT the chip index divided by the width (spec 04 I16) — that
# ordering disagrees with the hardware's. UBB_CHIP_COUNT is the tray WIDTH, used for the topology
# guards; tray IDENTITY comes from _tray_map.
UBB_CHIP_COUNT = 8


_UBB_TABLES_CACHE: Optional[dict] = None
_UBB_TABLES_MISSING_JOURNALED: bool = False
_UBB_TABLES_UNKNOWN_JOURNALED: set = set()


def _ubb_bus_id_tables() -> Optional[dict]:
    """tt-smi's own tray-number -> PCI bus-group tables, keyed by the board type that selects them.

    Imported, never copied: the numbering differs per architecture and is tt-smi's to define, the
    same rule ``HealthMonitor._glx_board_types`` follows for the board-type list. None when tt-smi
    cannot be imported, which leaves the tray unknown rather than guessed.

    Imported here rather than at module scope because ``tt_smi`` pulls a native extension and a TUI
    stack in to reach two dicts, and this is only ever needed on a Galaxy that is already degraded.
    Memoized after the first success: the tables cannot change under a running process, so a rung
    that fires per gate pass never pays the native import twice. The import-failure event journals
    once as well, so a broker where tt-smi is broken does not flood the log.
    """
    global _UBB_TABLES_CACHE, _UBB_TABLES_MISSING_JOURNALED
    if _UBB_TABLES_CACHE is not None:
        return _UBB_TABLES_CACHE
    try:
        from tt_smi.constants import BH_UBB_BUS_IDS, GLX_BOARD_TYPES, WH_UBB_BUS_IDS
    except Exception as e:  # noqa: BLE001 - a native extension failing here must not kill the gate
        if not _UBB_TABLES_MISSING_JOURNALED:
            _UBB_TABLES_MISSING_JOURNALED = True
            health_event("ubb_tray_tables_unavailable", reason=str(e))
        return None
    tables = {}
    for board in GLX_BOARD_TYPES:
        if board.endswith("-bh"):
            tables[board] = dict(BH_UBB_BUS_IDS)
        elif board.endswith("-wh"):
            tables[board] = dict(WH_UBB_BUS_IDS)
        else:
            # A new arch suffix in tt-smi's list this build does not know how to map. The rung
            # would silently decline on that architecture without this event — the spec is explicit
            # that a BMC or tt-smi change must reach us as a loud "rung stopped firing", not as a
            # release note (spec 04 I16 scope).
            if board not in _UBB_TABLES_UNKNOWN_JOURNALED:
                _UBB_TABLES_UNKNOWN_JOURNALED.add(board)
                health_event("ubb_tray_table_missing", board=board)
    if tables:
        _UBB_TABLES_CACHE = tables
    return tables or None


def _tray_map(chip_buses, board_type) -> Optional[dict]:
    """``{tray number: [chip ids]}`` for a Galaxy, or None when the trays cannot be known.

    ``chip_buses`` is ``{chip id: PCI address}``, the kernel's chip index (the one /dev, sysfs and
    the heartbeat read use) mapped to that chip's own address, banked from sysfs while every chip
    was on the bus. The tray is the bus masked to its group (``& 0xf0``) looked up in tt-smi's
    table for this board type — the same derivation ``tt-smi -glx_list_tray_to_device`` prints, so
    the trays the broker names are the trays an operator reads there.

    Never a list read by position (I16): tt-smi lists chips in PCI order, the kernel numbers them in
    its own, and on a Blackhole Galaxy the two disagree for chips 16-31 — reading one as the other
    swaps trays 3 and 4. A list is therefore refused outright, and a chip missing from the map
    shifts no other chip's tray.

    All-or-nothing on what it is given. A chip with no readable bus, a bus outside the four known
    groups, or a board type with no table leaves a map that misplaces silicon, and the rung declines
    on None; it never falls back to arithmetic. That every chip is present is the bank's guarantee
    (``HealthMonitor._bank_chip_buses``), and a drop on a chip the map does not place declines
    in :func:`_offbus_ids_on_trays`.
    """
    if not isinstance(chip_buses, Mapping) or not chip_buses:
        return None
    tables = _ubb_bus_id_tables()
    if not tables:
        return None
    # Normalize here, not at every call site: a snapshot's board type carries the " L"/" R" suffix
    # tt-smi appends (see _normalized_board), and tables are keyed by the unsuffixed value.
    table = tables.get(_normalized_board(board_type))
    if table is None:
        return None
    tray_of_group = {group: tray for tray, group in table.items()}
    trays: dict = {}
    try:
        chips = sorted((int(c), addr) for c, addr in chip_buses.items())
    except (TypeError, ValueError):
        return None
    for chip_id, address in chips:
        bus = pci_bus_number(address)
        if bus is None:
            return None
        tray = tray_of_group.get(bus & 0xF0)
        if tray is None:
            return None
        trays.setdefault(tray, []).append(chip_id)
    return trays or None


def _tray_label(tray: int, tray_map: dict) -> str:
    """``tray 4 (BMC mask 0x08, chips 24-31)``: the tray number as tt-smi prints it, with the BMC bit
    the reset pulses and the chip ids it holds, so a log line can be checked against both without
    knowing the numbering convention."""
    chips = sorted(tray_map.get(tray) or [])
    if chips and chips == list(range(chips[0], chips[-1] + 1)):
        span = f"{chips[0]}-{chips[-1]}" if len(chips) > 1 else str(chips[0])
    else:
        span = ",".join(str(c) for c in chips)
    return f"tray {tray} (BMC mask 0x{1 << (tray - 1):02x}, chips {span})"


def _offbus_chip_ids(beats: dict, expected: int) -> set:
    """The index set of chips off the PCIe bus: every expected chip id that is missing from the
    heartbeat read (its node left the bus) or present but reading all-ones. A chip missing only
    because its ARC is not ready (ENODATA on a bound endpoint) is on the bus and not in it, so no
    tray or bridge rung is planned around it (spec 04 I24). Its cardinality matches the off_bus
    count the rungs use (:func:`offbus_count`); the id SET is what a rung needs to map a drop onto
    physical trays."""
    return offbus_chip_ids(beats, expected)


def _offbus_ids_on_trays(offbus_chips: set, expected: int, tray_map: Optional[dict]) -> Optional[set]:
    """The off-bus chip set as ints, or None when no tray decision can rest on it: a host without
    multi-tray topology, an empty drop, a malformed or out-of-range id, no tray map at all, or a
    chip the map does not place. The shared front half of every planner below, so all three decline
    on exactly the same conditions and a drop can never be planned by one and refused by another."""
    if expected < 2 * UBB_CHIP_COUNT or expected % UBB_CHIP_COUNT != 0:
        return None
    if not offbus_chips or not tray_map:
        return None
    try:
        ids = {int(c) for c in offbus_chips}
    except (TypeError, ValueError):
        return None
    if any(i < 0 or i >= expected for i in ids):
        return None
    # A chip the map cannot place has no tray, and planning around it would report the drop as
    # whole trays while leaving that chip unaccounted for (I16).
    if not ids <= {c for chips in tray_map.values() for c in chips}:
        return None
    return ids


def _whole_tray_drop_trays(offbus_chips: set, expected: int, tray_map: Optional[dict]) -> Optional[list]:
    """The sorted whole trays an off-bus chip set is made of, or None when the drop is not a clean
    tray-down: every off-bus chip's whole tray is off the bus, and nothing off-bus lies outside those
    trays. Tray numbers come from ``tray_map`` and are 1-based (I16). Pure and side-effect-free."""
    ids = _offbus_ids_on_trays(offbus_chips, expected, tray_map)
    if ids is None:
        return None
    down = set()
    covered: set = set()
    for tray, chips in tray_map.items():
        if set(chips) <= ids:
            down.add(tray)
            covered |= set(chips)
    if not down or covered != ids:
        return None
    return sorted(down)


def _affected_trays(offbus_chips: set, expected: int, tray_map: Optional[dict]) -> Optional[list]:
    """The sorted UBB trays that CONTAIN any off-bus chip, for journal lines and events. Unlike
    :func:`_whole_tray_drop_trays` this does NOT require the whole tray to be down. None on every
    condition :func:`_offbus_ids_on_trays` refuses. Pure and side-effect-free."""
    ids = _offbus_ids_on_trays(offbus_chips, expected, tray_map)
    if ids is None:
        return None
    return sorted({tray for tray, chips in tray_map.items() if ids & set(chips)})


def _offbus_placement(offbus_chips, chip_buses, tray_map: Optional[dict]) -> list:
    """Each off-bus chip a sweep plans around, as ``{"chip", "pci", "tray"}`` sorted by chip: the
    PCI address from the banked map and the tray from ``tray_map`` (None where either is unknown).
    The sweep's line and event carry it so a reader can check the trays it names against the
    chips that drove them; without it a chip that came back earlier in the hold reads as the one
    the sweep was for. Pure."""
    tray_of = {c: t for t, chips in (tray_map or {}).items() for c in chips}
    buses = chip_buses if isinstance(chip_buses, Mapping) else {}
    ids = sorted({int(c) for c in offbus_chips or () if str(c).isdigit()})
    return [{"chip": i, "pci": buses.get(i), "tray": tray_of.get(i)} for i in ids]


def _placement_text(placement: list) -> str:
    """``chip 27 (0000:84:00.0, tray 4)``, comma-joined; ``?`` for an unknown address or tray."""
    return ", ".join(f"chip {p['chip']} ({p['pci'] or '?'}, tray {p['tray'] or '?'})" for p in placement) or "none"


# Issue #26 (spec 04 I19): a tray with this many chips off the bus at the FIRST sighting is a tray that
# has lost power, not a chip that dropped. Such an onset gets one PCI rescan and a read-only capture of
# the BMC/CPLD/PCIe state in front of the ladder; the ladder itself, and the power cycle only when the
# ladder fails, are unchanged.
TRAY_DOWN_MIN_CHIPS = 4
TRAY_DOWN_RESCAN_SETTLE_SEC = 3
TRAY_DOWN_ONSET = "TRAY_DOWN"


def _pci_rescan() -> None:
    """One bare PCI rescan — the same write the bridge rung's rescan makes. Its own function so the
    suite replaces it (conftest) and no test writes to the host's sysfs."""
    Path("/sys/bus/pci/rescan").write_text("1")


def tray_down_capture_enabled() -> bool:
    """``TT_DEVICE_MCP_TRAY_DOWN_CAPTURE``: the rescan and capture in front of the ladder for a
    tray-down onset. ON by default; ``0`` turns it off and every drop goes straight to the ladder."""
    return os.environ.get("TT_DEVICE_MCP_TRAY_DOWN_CAPTURE", "1").strip() != "0"


def _off_per_tray(off_ids, tray_map: Optional[dict]) -> Optional[dict]:
    """``{tray: chips off}`` for every tray with a chip off, or None when the map is missing or does
    not place every off chip — no tray decision rests on a partial picture (I16)."""
    if not tray_map or not off_ids:
        return None
    try:
        ids = {int(c) for c in off_ids}
    except (TypeError, ValueError):
        return None
    tray_of = {c: t for t, chips in tray_map.items() for c in chips}
    if not ids <= set(tray_of):
        return None
    per: dict = {}
    for i in ids:
        per[tray_of[i]] = per.get(tray_of[i], 0) + 1
    return dict(sorted(per.items()))


def _tray_down_counts(off_ids, tray_map: Optional[dict]) -> Optional[dict]:
    """The per-tray off counts when some tray has at least TRAY_DOWN_MIN_CHIPS off, else None."""
    per = _off_per_tray(off_ids, tray_map)
    if per is None or not any(n >= TRAY_DOWN_MIN_CHIPS for n in per.values()):
        return None
    return per


def tray_down_onset(off_ids, chip_buses, board_type) -> Optional[dict]:
    """``{tray: chips off}`` when the off-bus set puts TRAY_DOWN_MIN_CHIPS or more chips off ONE
    physical tray (by the bus-derived map, I16), else None — also None for no ids, no map, or a chip
    the map does not place. Pure: the classification the tray-down latch takes at the first sighting."""
    return _tray_down_counts(off_ids, _tray_map(chip_buses, board_type))


def _whole_trays_off(offbus_chips: set, expected: int, tray_map: Optional[dict]) -> list:
    """The sorted trays every chip of which is off the bus (others may be partly off too), or [] on
    every condition :func:`_offbus_ids_on_trays` refuses. Pure and side-effect-free."""
    ids = _offbus_ids_on_trays(offbus_chips, expected, tray_map)
    if ids is None:
        return []
    return sorted(tray for tray, chips in tray_map.items() if chips and set(chips) <= ids)


def _offbus_fault_class(off_bus: int, expected: int, whole: bool) -> str:
    """The time-to-recover class an off-bus hold is measured in (spec 04 I27): ``whole_tray`` (a whole UBB
    tray off the bus), ``all_off``, ``tray_partial`` (4 or more chips, no whole tray) or ``partial_offbus``
    (1-3 chips). Carried on the hand-off's journal events so per-class sweeps need not guess it."""
    if whole:
        return "whole_tray"
    if expected > 0 and off_bus >= expected:
        return "all_off"
    return "tray_partial" if off_bus >= 4 else "partial_offbus"


# The recorded reason of the I29 host rung on a 77 (spec 04 I29): the boot it brings up reads it from the
# reboot ledger, so a 77 that survives the reboot is handed to an operator instead of climbing again.
FABRIC_77_HOST_RUNG_CAUSE = "idle escalation: fabric unverified (77) after the reset and the last-chance sweep"
# How long a 77 the host rung did not end stands for an operator (the act-by time on its page).
FABRIC_77_HANDOFF_SEC = 1200
# What _climb_to_host_recovery_after_failed_reset did besides NO_HOST_RUNG: requested the reboot or power
# cycle; only waited (the hold is younger than the ladder ceiling, a reset still cycles in its scope, the
# sweep recovered or could not run in full), so a later escalation retries; or auto_recovery_allowed denied
# the opted-in rung (rate limit, tenant). NO_HOST_RUNG and a denial are the two ways it cannot run.
HOST_RUNG_FIRED = "fired"
HOST_RUNG_DEFERRED = "deferred"
HOST_RUNG_DENIED = "denied"
HOST_RUNG_CANNOT_RUN = (NO_HOST_RUNG, HOST_RUNG_DENIED)


class GalaxyRecovery(Recovery):
    """Recovery for a 6U Galaxy: the mesh-wide reset, gentlest-first routing ahead of it (a
    per-chip bridge reset when a chip is surgically isolable), and the host power-cycle
    escalation once a reset has already run and failed to revive the mesh."""

    # Whether the last sweep after an UNKNOWN reset read a fabric 77 (no verdict), and the episode whose
    # no-host-rung hold already paged (spec 04 I29).
    _sweep_read_77: bool = False
    _host_rung_77_paged_since: Optional[str] = None
    # The operator hand-off of a 77 the host rung did not end (spec 04 I29): its episode and deadline.
    _fabric_77_handoff: Optional[dict] = None
    # Why auto_recovery_allowed last held a host rung off (a cooldown, the rate limit, a tenant).
    _host_rung_denied_why: str = ""
    # The (episode, until) of the last I65 cooldown a 77 climb waited on without a page (spec 04 I79).
    _fabric_77_cooldown_logged: Optional[tuple[str, float]] = None
    # Whether the last idle-ladder escalate() stood down on a live tenant (spec 04 I7). The hold
    # watchdog reads it to re-arm the window it spent, so the ladder runs once the tenant is gone
    # rather than at the next window (spec 03 I23).
    tenant_deferred: bool = False
    # Spec 03 I75: the caller's stand-down for the escalate() now running (True while the startup gate
    # is still to run on a startup hold), re-read at every tenant check; and whether that run stood down
    # on it. The caller reads its predicate once before the device-op lock, and the re-adopted job can
    # end in any read after that, so the last read is the one right before a rung acts.
    _stand_down: Optional[Callable[[], bool]] = None
    startup_verify_deferred: bool = False
    # Spec 04 I71: the (episode, monotonic time) of the last page for a power cycle that did not launch.
    # One page per hold episode, then at most one every CYCLE_LAUNCH_REPAGE_SEC while it keeps failing.
    _cycle_launch_paged: Optional[tuple[str, float]] = None
    # Spec 04 I89: whether the escalate() now running is the I76 10-min bound's ladder, and the
    # (episode, off-bus set) whose below-floor gate hold already ran its rescan and SBR.
    _bound: bool = False
    _below_floor_rungs_key: Optional[tuple] = None

    def tenant_holds(self) -> bool:
        """The forced ladders' own tenant check, alone: a running broker job or a readable tenant fd."""
        return self._tenant_active(self.deps.enumerate_device_holders(), force=True)

    async def _offbus_ids_now(self, expected: int) -> Optional[set]:
        """The chip ids off the bus now, or None when the heartbeats cannot be read."""
        try:
            beats = await asyncio.to_thread(self.deps.read_heartbeats)
        except Exception:  # noqa: BLE001 - an unreadable read decides nothing here
            return None
        return _offbus_chip_ids(beats or {}, expected)

    def _tray_map_now(self) -> Optional[dict]:
        """This host's tray -> chip-ids map, or None when it cannot be known (spec 04 I16).

        Read through the injected providers rather than off a monitor handle for the same reason
        the board-type derivation is: the caches live on the one HealthMonitor the FSM builds, and
        a test aims its own bag at its own monitor.
        """
        if self.deps.chip_buses_provider is None:
            return None
        # Normalized so a WH snapshot's " L"/" R" pair reads as one board type — the same rule
        # _is_galaxy applies for its unanimity check, and the reason the tests seed suffixed values.
        board_types = {_normalized_board(t) for t in (self.deps.board_types_provider() or ())}
        if len(board_types) != 1:
            # Unanimous or nothing, as everywhere the board type decides a reset: a mesh reporting
            # two board types is not a topology either UBB table describes.
            return None
        return _tray_map(self.deps.chip_buses_provider() or {}, board_types.pop())

    def _chip_buses_now(self) -> dict:
        """The banked ``{chip id: PCI address}`` map, or ``{}`` when none is banked or wired."""
        if self.deps.chip_buses_provider is None:
            return {}
        return self.deps.chip_buses_provider() or {}

    def next_stage(self, ev: Evidence) -> str:
        # host_escalation/reboot_blocked are folded here, not carried on Evidence (see its own
        # docstring): only the post-galaxy-reset phase ever needs them.
        host_escalation, reboot_blocked = None, False
        if ev.last_action == STAGE_SMI_RESET and ev.last_action_recovered is False:
            # The fold picks the power cycle whenever it is armed (I71). Without it, a reset proven
            # unable to clear the drop holds loudly.
            host_escalation, reboot_blocked = _host_escalation_for_drop(
                ev.off_bus,
                ev.expected,
                auto_power_cycle_enabled=self.deps.auto_power_cycle_enabled,
            )
        return _route(
            healthy=ev.healthy,
            fault_reported=ev.fault_reported,
            fabric_ok=ev.fabric_ok,
            fabric_ran=ev.fabric_ran,
            dirty=ev.dirty,
            fabric_forced=ev.fabric_forced,
            off_bus=ev.off_bus,
            frozen_chips=ev.frozen_chips,
            expected=ev.expected,
            sbr_candidates=ev.sbr_candidates,
            galaxy_floor=_galaxy_reset_min_dead_chips(ev.expected),
            reboot_floor=_reboot_min_dead_chips(ev.expected),
            eth_frozen=ev.eth_frozen,
            cooling=ev.cooling,
            reset_scope_active=ev.scope_active,
            was_failing=ev.was_failing,
            last_action=ev.last_action,
            last_action_recovered=ev.last_action_recovered,
            host_escalation=host_escalation,
            reboot_blocked=reboot_blocked,
            holding_fabric_unverified=ev.holding_fabric_unverified,
            dispatch_reset_due=ev.dispatch_reset_due,
            eth_timeout_reset_due=ev.eth_timeout_reset_due,
            kill_reset_due=ev.kill_reset_due,
        )

    def _hold_after_cycle_launch_failure(self, log, cause: str) -> None:
        """A power cycle that did not launch (spec 04 I71): an unreachable BMC or a failing ipmitool.
        Nothing stands in for it — there is no warm reboot rung. The hold stays, every failure is
        journaled (log line plus a /health event), and the next watchdog or gate pass asks for the
        cycle again under the I76 bound, unpaced, so the cycle fires on the first pass the BMC answers.
        The page goes out once per hold episode and again at most every CYCLE_LAUNCH_REPAGE_SEC while
        the launch keeps failing: the I76 bound asks every watchdog tick, and a failed launch retracts
        its ledger entry, so nothing else rate-limits it. A cycle that could not start with the OS
        brought down first (I88) has just paged power_cycle_not_launched for this episode: that is the
        episode's one page, and only the re-page follows."""
        log(f"the power cycle did not launch ({cause}); holding the device and retrying the cycle on the next pass")
        episode = self.deps.device_hold_episode_since() or ""
        mono = time.monotonic()
        last = self._cycle_launch_paged
        repage = last is not None and last[0] == episode and mono - last[1] >= CYCLE_LAUNCH_REPAGE_SEC
        page = last is None or last[0] != episode or repage
        if page and not repage and self.mechanism.power_cycle_not_launched_paged_for(episode):
            self._cycle_launch_paged = (episode, mono)
            page = False
        health_event("power_cycle_launch_failed_holding", cause=cause, paged=page, held_since=episode or None)
        if not page:
            return
        event = {"kind": "power_cycle_launch_failed_holding", "cause": cause, "held_since": episode or None}
        if alert.send_alert(event, _logger) is not None or alert.alert_argv() is None:
            self._cycle_launch_paged = (episode, mono)

    def _platform_reset_argv(self, indices: list) -> list:
        # All ASICs, no targets: a 6U Galaxy needs -glx_reset — per-PCIe-target -r does not
        # recover it (the other trays never take part in a per-target reset).
        return ["tt-smi", "-glx_reset"]

    # ---- idle/forced hold-escalation ladder --------------------------------------------------
    #
    # This whole ladder is Galaxy-flavored in practice: it fires the mesh-wide galaxy reset (once per
    # hold episode, I87) and the host power-cycle escalation, neither of which a per-target
    # host has a rung for. Its two callers (the idle relift and the hold-deadline watchdog's
    # forced escalation, both still in server.py) reach it through select_recovery's per-pass
    # platform choice, exactly like the gate's own reset command: on a per-target host that
    # resolves to PerTargetRecovery instead, whose inherited escalate() (base.py) always reports
    # WAITING — there is no ladder to run, not a failed one.

    async def escalate(
        self,
        phase: str,
        indices: list,
        expected: int,
        log,
        *,
        stage: Optional[str] = None,
        ev: Optional[Evidence] = None,
        beats: Optional[dict] = None,
        measured_fault: bool = False,
        stand_down: Optional[Callable[[], bool]] = None,
        bound: bool = False,
    ) -> str:
        """Run the next justified rung of this ladder and report the outcome, replacing the
        direct ``_escalate_stuck_hold``/``_escalate_offbus_stuck_hold`` calls the idle relift and
        the hold-deadline watchdog's forced escalation used to make.

        ``phase`` names which ladder to try — ``"present"`` (the present-mesh galaxy-reset ladder)
        or ``"offbus"`` (the off-bus cascade) — with a ``"-forced"`` suffix for the watchdog's
        forced pass (bypasses each ladder's guarded defers — due/retry-pacing on the present ladder,
        the once-per-episode latch and cooldown on the off-bus one, and the unreadable-tenant read
        on both — exactly as ``force=True`` already did on the direct calls it replaces), or
        ``"gate/<gate phase>"`` for
        the between-job gate, whose ladder fires ONE rung its own ``next_stage`` already chose and
        so takes ``stage``/``ev``/``beats`` with it (see :meth:`_fire_gate_rung`; the two idle
        ladders choose their own rungs and ignore all three). ``indices``/``expected`` are the
        caller's OWN single read of the mesh, passed through rather than re-derived here: the
        caller already used that same read to decide off_bus and journal it, and a second,
        independent read here could hand the reset a different chip set than the one the decision
        was made against.

        Mapping for the two idle ladders, kept to one rule: nothing ran -> WAITING; something ran
        and the mesh went from degraded to not-degraded -> RECOVERED; something ran and the mesh is
        still degraded (climbed to the host rung, or found no rung left to climb) -> TERMINAL. The
        RECOVERED/TERMINAL split is a TRANSITION — ``device_degraded()`` read once before dispatch
        and once after — not a bare post-call read: a caller reaching escalate() on an
        already-clean mesh must not have a rung that merely RAN (e.g. a below-floor suppression's
        own ``return True``) misreported as having recovered something. Deliberately not
        ``self.mechanism.last_reset_failed``: two of the three rungs this dispatches into (a
        surgical bridge reset, a per-tray UBB reset) can recover the mesh without ever calling
        ``_reset_and_verify_device``, so that flag can still carry an unrelated FAILED verdict from
        an earlier, different reset. The gate rung reports its own action's verdict instead and
        never TERMINAL — see :meth:`_fire_gate_rung`.

        Every caller passes here first, so this is where a tray-down onset (spec 04 I19) is latched
        and, before its first rung, given one PCI rescan and the read-only capture. Then the ladder
        runs exactly as it would have, with the caller's own evidence and stage: the prelude never
        picks a rung, skips one or fires a power cycle. Only a rescan that brought every chip back
        onto a mesh that passes the full verify ends the episode before the ladder.

        ``measured_fault`` reaches the present ladder only: the hold stands on a MEASURED fabric
        failure, so its reset is due without the grace (spec 03 I56; see ``_stuck_hold_reset_due``).

        A host reboot or power cycle still pending (spec 04 I34) stops every caller here, before
        the prelude: nothing runs, WAITING, one log line per request.

        ``stand_down`` (spec 03 I75) is re-read at every tenant check of this run: while it reads True
        no rung acts, the run reports WAITING with ``tenant_deferred`` and ``startup_verify_deferred``
        set, and the caller leaves the hold to the startup gate.

        ``bound`` (spec 04 I89) marks the I76 10-min bound's own ladder: a below-floor off-bus hold
        then runs its full kept ladder even when an earlier forced run already failed its mesh reset
        on the same chips."""
        self._stand_down = stand_down
        self._bound = bound
        self.startup_verify_deferred = False
        try:
            return await self._escalate_phase(
                phase, indices, expected, log, stage=stage, ev=ev, beats=beats, measured_fault=measured_fault
            )
        finally:
            self._stand_down = None
            self._bound = False

    async def _escalate_phase(
        self,
        phase: str,
        indices: list,
        expected: int,
        log,
        *,
        stage: Optional[str],
        ev: Optional[Evidence],
        beats: Optional[dict],
        measured_fault: bool,
    ) -> str:
        """:meth:`escalate`'s body, run with its ``stand_down`` in place."""
        if self.mechanism.host_action_blocks_reset(log, context=f"escalate({phase})"):
            return OUTCOME_WAITING
        rung, _, gate_phase = phase.partition("/")
        if await self._tray_down_latch(expected, beats) == TRAY_DOWN_ONSET and not self._td["prelude_done"]:
            if await self._tray_down_prelude(expected, log, phase=gate_phase or rung):
                return OUTCOME_RECOVERED
        direct = await self._power_cycle_direct_from_escalate(rung, gate_phase, stage, expected, beats, log)
        if direct is not None:
            return direct
        # Anything that reaches a ladder from here may reset: an onset first sighted after this is
        # not an onset any more (the ladder can turn a 1-chip drop into a whole tray off).
        self._td_reset_seen = True
        if rung == "gate":
            return await self._fire_gate_rung(gate_phase, stage, indices, expected, log, ev=ev, beats=beats)
        was_degraded = self.deps.device_degraded()
        if rung.endswith("-forced"):
            force, rung = True, rung[: -len("-forced")]
        else:
            force = False
        self.tenant_deferred = False
        if rung == "offbus":
            ran = await self._escalate_offbus_stuck_hold(indices, expected, log, force=force)
        elif rung == "present":
            ran = await self._escalate_stuck_hold(indices, expected, log, force=force, measured_fault=measured_fault)
        else:
            raise ValueError(f"escalate(): unknown phase {phase!r}")
        if not ran:
            return OUTCOME_WAITING
        return OUTCOME_RECOVERED if was_degraded and not self.deps.device_degraded() else OUTCOME_TERMINAL

    def _presence_read(self, offbus: set, expected: int) -> tuple:
        """``(present_buses, tray_groups)`` for :func:`_absent_tray_groups`: tt-smi's UBB bus groups
        and ``{chip id: PCI bus}`` of the chips sysfs lists that are not in ``offbus``. A chip the
        heartbeat calls off the bus is not present even if its node lingers (an all-ones endpoint the
        kernel still lists), nor is one whose link reads down (:func:`hostpci.link_down`), so neither
        can fill a tray it left. ``(None, ())`` with no tables."""
        if not 0 < len(offbus) < expected:
            return None, ()
        groups = {g for table in (_ubb_bus_id_tables() or {}).values() for g in table.values()}
        if not groups:
            return None, ()
        off = {str(i) for i in offbus}
        return {
            i: pci_bus_number(a) for i, a in chip_pci_bdfs().items() if str(i) not in off and not link_down(a)
        }, groups

    def _power_cycle_direct_class_now(self, offbus: set, expected: int, tray_map: Optional[dict], log) -> Optional[str]:
        """:func:`_power_cycle_direct_class` on this broker's live state. With no tray map, the chips
        still on the bus are read from sysfs and grouped by tt-smi's UBB bus groups, so a whole-tray
        drop the broker never saw a full mesh before (a short boot, a restart into a drop) is still
        class A at its first gate pass, not after the hold ceiling and a mesh reset (spec 04 I37).

        When the set as read is not class A, the chips in it that are enumerated and bound but whose
        heartbeat read fails (EIO, a missing attribute: :func:`unreadable_on_bus_chips`) are taken as
        on the bus and the set is classified again, for class A only: survivors of a whole-tray drop
        that read like this would otherwise make it look all-off (one mesh reset, then class C) or
        partial (the full ladder). Every other answer is the one the set as read gives."""
        mesh_reset_failed = (
            self.mechanism.mesh_reset_failed_this_boot(offbus) or self.mechanism.broker_cycled_this_episode()
        )

        def classify(off: set) -> tuple:
            present_buses, groups = (None, ()) if tray_map is not None else self._presence_read(off, expected)
            return (
                _power_cycle_direct_class(
                    off,
                    expected,
                    tray_map,
                    mesh_reset_failed=mesh_reset_failed,
                    present_buses=present_buses,
                    tray_groups=groups,
                ),
                present_buses,
            )

        fault_class, present_buses = classify(offbus)
        unreadable: set = set()
        if fault_class != FAULT_CLASS_WHOLE_TRAY and offbus and expected >= 2 * UBB_CHIP_COUNT:
            unreadable = unreadable_on_bus_chips(offbus) & {str(i) for i in offbus}
            if unreadable:
                on_bus_class, on_bus_present = classify({str(i) for i in offbus} - unreadable)
                if on_bus_class == FAULT_CLASS_WHOLE_TRAY:
                    fault_class, present_buses = on_bus_class, on_bus_present
                    log(
                        f"chip(s) [{','.join(sorted(unreadable, key=int))}] are enumerated and bound but their "
                        f"heartbeat read fails — on the bus, unhealthy; the rest of the off-bus set is whole "
                        f"tray(s): class A"
                    )
                    health_event(
                        "class_a_unreadable_survivors",
                        unreadable=sorted(unreadable, key=int),
                        off_bus=sorted({str(i) for i in offbus} - unreadable, key=int),
                        expected=expected,
                    )
        # Spec 04 I73: the governor counts the broker cycles for this tray, and flags the AC-needed
        # state, only while a whole tray is what is off; any other read clears it.
        self.mechanism.whole_tray_off = sorted(offbus, key=int) if fault_class == FAULT_CLASS_WHOLE_TRAY else None
        if fault_class == FAULT_CLASS_WHOLE_TRAY and present_buses is not None:
            log(
                f"no tray map banked: the {len(present_buses)} chip(s) on the bus fill whole trays and "
                f"{len(offbus) - len(unreadable)} chip(s) are off — class A from bus presence"
            )
        return fault_class

    async def _power_cycle_direct_from_escalate(
        self, rung: str, gate_phase: str, stage: Optional[str], expected: int, beats: Optional[dict], log
    ) -> Optional[str]:
        """Spec 04 I37 at the ladder's entry: an off-bus set of class A or C goes to the power cycle
        before any rung fires. None when it does not apply (the ladder runs as before); else the
        outcome — WAITING while it holds (a tenant, a reset in scope, a stand-back, a rate limit) or
        on the gate after the cycle was requested, TERMINAL on an idle ladder that requested it.

        The gate's host-rung stages keep their own path (the last-chance sweep there skips itself
        for these classes, see :meth:`_settle_and_verify_before_host_rung`), and a reset already
        cycling in its scope is adopted and verified first. The present-mesh ladder has no chip off
        the bus, and an idle off-bus hold whose escalation is switched off stays read-only."""
        if rung == "gate":
            if stage in (STAGE_POWER_CYCLE, DEFER):
                return None
        elif rung == "offbus-forced":
            pass
        elif rung != "offbus" or not _offbus_hold_escalate_enabled():
            return None
        if beats is None:
            try:
                beats = await asyncio.to_thread(self.deps.read_heartbeats)
            except Exception:  # noqa: BLE001 - unreadable: no class, the ladder decides as before
                return None
        offbus = _offbus_chip_ids(beats or {}, expected)
        fault_class = self._power_cycle_direct_class_now(offbus, expected, self._tray_map_now(), log)
        if fault_class is None:
            return None
        fired = await self._power_cycle_direct(
            fault_class, offbus, expected, log, context=gate_phase or rung, force=rung == "offbus-forced"
        )
        if fired is None:
            return None
        if (
            fired is False
            and rung == "offbus-forced"
            and fault_class == FAULT_CLASS_WHOLE_TRAY
            and self.mechanism.class_a_ladder_due(consume=False)
        ):
            # Spec 04 I73: the cooldown refused the cycle, and the hold has stood 10 min since its last
            # cycle or boot. The sweep runs once now (SBR, then the mesh reset), then the full health
            # check, fabric included; the cycle still fires the moment the cooldown ends.
            return await self._class_a_cooldown_sweep(offbus, expected, log)
        if fired and rung != "gate":
            return OUTCOME_TERMINAL
        return OUTCOME_WAITING

    async def _power_cycle_direct(
        self, fault_class: str, offbus_chips: set, expected: int, log, *, context: str, force: bool = False
    ) -> Optional[bool]:
        """Request the power cycle for an I37 class with no reset or sweep first.

        None when no power cycle is opted in: the caller runs its ordinary ladder (I36 still stops a
        repeat of a failed reset). False while it holds, firing nothing: a reset is cycling in its own
        scope, a tenant holds the device or the scan is unreadable, an operator stand-back, or the
        power-cycle rate limits deny it (journaled; the next window asks again). True once requested.
        ``force`` (the hold-deadline climb) waives an unreadable scan, never a
        readable tenant or a running job.
        The boot that comes back runs the startup gate, which waits for the hugepage pool and passes
        the full health check, fabric included, before it releases anything (goal 8)."""
        off_bus = len(offbus_chips)
        name = {FAULT_CLASS_WHOLE_TRAY: "whole tray(s) off the bus", FAULT_CLASS_ALL_OFF: "every chip off the bus"}
        what = f"class {fault_class} ({name.get(fault_class, fault_class)}): {off_bus}/{expected} chip(s) off"
        escalation, _ = _host_escalation_for_drop(off_bus, expected, **self.deps.host_escalation_kwargs())
        if escalation != "power-cycle":
            health_event("power_cycle_direct", phase="not_opted_in", fault_class=fault_class, context=context)
            return None
        if await asyncio.to_thread(self.mechanism.scope_active):
            log(f"{what}: a reset is cycling in its own scope — holding before the power cycle")
            return False
        if self._tenant_active(await asyncio.to_thread(self.deps.enumerate_device_holders), force=force):
            log(f"{what}: a tenant holds the device or the scan is unreadable — holding before the power cycle")
            return False
        # The operator's stand-back marker and command still hold it; a missing manual-recovery check
        # does not, as on the ladder's own host rung this cycle replaces (holding there would leave the
        # class with no recovery at all).
        standback = await asyncio.to_thread(offbus_power_cycle.standback_reason, require_manual_check=False)
        if standback:
            log(f"{what}: standing back from the power cycle — {standback}")
            return False
        allowed, why = self.mechanism.auto_recovery_allowed("power-cycle", tenant_active=False)
        if not allowed:
            log(f"{what}: power cycle held off: {why}")
            self.mechanism._journal_auto_recovery_denied("power-cycle", why)
            metrics.stage_fired("power_cycle", "blocked")
            return False
        health_event(
            "power_cycle_direct",
            phase="firing",
            fault_class=fault_class,
            off_bus=sorted(offbus_chips, key=int),
            expected=expected,
            context=context,
            host_at_risk=True,
        )
        log(f"{what}: no further reset heals this class — power-cycling the host now (spec 04 I37)")
        await self.deps.auto_power_cycle_host(log, f"{what}; no further reset heals this class")
        return True

    async def leftover_host_rung(self, leftover_pids, expected: int, log, *, what: str) -> tuple[str, str]:
        """Take the host rung for an unkillable reaped leftover past its budget (spec 04 I70).

        Every reset is fenced while the leftover stands (I21) and none would free it, so no ladder or
        sweep runs first (the I22 exception). Returns ``(stage, detail)``: ``requested`` with the
        action once fired, ``held`` with why while a guard stands (the caller asks again on its next
        try), ``gone`` when no leftover pid is still in the scan (nothing fires; the caller re-scans,
        which closes the episode), ``no-host-rung`` when the host opted into neither rung. The guards
        are the forced climb's: a reset cycling in its own scope, a running broker job or a readable
        tenant other than the leftovers themselves (an unreadable scan is waived), the operator
        stand-back for the power cycle, and the governor (rate limits, the I65 cooldown). The scan
        guards run again on a fresh scan after the stand-back and the governor, just before firing.
        The boot that comes back runs the startup gate, which waits for the hugepage pool and passes
        the full health check, fabric included, before it releases anything (goal 8)."""
        # Read under the caller's device lock: a try spawned before another path's request (the I76
        # bound's host rung) reaches here after it. A launch that timed out retracted its ledger record,
        # so the governor alone would let a second cycle through (I34).
        pending = self.mechanism.host_action_pending()
        if pending is not None:
            return "held", f"a host {pending[0]} requested {int(pending[1])}s ago is still pending"
        leftovers = {int(p) for p in leftover_pids}
        guard = await self._leftover_scan_guard(leftovers)
        if guard is not None:
            return guard
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = len(_offbus_chip_ids(beats, expected)) if expected > 1 else 0
        # The power cycle is the only host rung (I71): it ends every process, a leftover holder included.
        escalation, _ = _host_escalation_for_drop(off_bus, expected, **self.deps.host_escalation_kwargs())
        if escalation is None:
            return "no-host-rung", (
                f"no host rung is opted in ({off_bus}/{expected} chip(s) off the bus); "
                "an operator must power-cycle the host"
            )
        standback = await asyncio.to_thread(offbus_power_cycle.standback_reason, require_manual_check=False)
        if standback:
            return "held", f"standing back from the power cycle: {standback}"
        allowed, why = self.mechanism.auto_recovery_allowed(escalation, tenant_active=False)
        if not allowed:
            return "held", f"{escalation} held off: {why}"
        # The stand-back command can run for its whole timeout: the leftover may have exited, a tenant
        # arrived or a reset scope started meanwhile. Fire only on a fresh scan that still says go.
        guard = await self._leftover_scan_guard(leftovers)
        if guard is not None:
            return guard
        reason = f"{what}; no reset can free it (spec 04 I70)"
        log(f"{what}: past its budget, no reset can free it — requesting the host {escalation} (spec 04 I70)")
        await self.deps.auto_power_cycle_host(log, reason)
        return "requested", f"{escalation} ({off_bus}/{expected} chip(s) off the bus)"

    async def _leftover_scan_guard(self, leftovers: set) -> Optional[tuple[str, str]]:
        """The I70 rung's scan guards on a fresh scan, or None to go on: ``gone`` once none of
        ``leftovers`` still holds the device, ``held`` while a reset cycles in its own scope or a
        running broker job or a readable tenant other than the leftovers holds it."""
        if await asyncio.to_thread(self.mechanism.scope_active):
            return "held", "a reset is cycling in its own scope"
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        if not leftovers & {int(h.pid) for h in scan.holders}:
            return "gone", "the leftover no longer holds the device; nothing fired"
        others = dataclasses.replace(scan, holders=[h for h in scan.holders if int(h.pid) not in leftovers])
        blocker = self._tenant_blocker(others, force=True)
        if blocker is not None:
            return "held", self._describe_blocker(blocker)
        return None

    # ---- the between-job gate's action rungs --------------------------------------------------
    #
    # The gate (server.py) DECIDES: it builds the Evidence, asks next_stage for the one rung that
    # evidence justifies, and keeps every guard, hold flavour and journal line of its own. This is
    # the other half — firing the named rung, and the release bookkeeping each rung's own outcome
    # semantics call for.
    #
    # Both halves apply on EVERY platform. The gate's ladder has always been one ladder: the
    # galaxy-reset floor, the surgical rung, the mesh reset and the host rungs are the policy at a
    # job boundary whatever the board is, and only the reset argv is platform-specific — resolved
    # inside _reset_and_verify_device, which re-derives select_recovery per call precisely so a
    # rung reached through a fixed platform reference still fires the command that fits the host
    # actually detected. So the gate reaches this through the module-global Galaxy instance,
    # not through its per-pass platform choice.

    async def _fire_gate_rung(
        self,
        gate_phase: str,
        stage: Optional[str],
        indices: list,
        expected: int,
        log,
        *,
        ev: Optional[Evidence],
        beats: Optional[dict],
    ) -> str:
        """Fire the one rung ``stage`` names and report whether it recovered the mesh.

        RECOVERED or WAITING only, never TERMINAL. Unlike the idle ladders — the last resort on a
        box with no next job — a gate pass that could not recover the mesh has not exhausted the
        ladder: the next job's gate, the idle relift and the hold-deadline watchdog all still own
        this mesh, so the episode stays exactly as it stood rather than closing into DOWN. That is
        also what keeps a per-target host, whose gate runs this same ladder, out of DOWN entirely —
        a state it can reach only through the Galaxy idle ladder.

        The verdict reported is the ACTION's own (the re-verify, the bridge reset, the mesh reset), not a
        degraded->fit transition: the gate reaches its ladder whenever THIS pass's probe read
        unhealthy, which happens on a mesh the FSM still records HEALTHY (a post-job pass on a clean
        episode), and a rung that verified such a mesh back must still read as recovered or the gate
        climbs straight past it to a heavier one.
        """
        if stage == STAGE_BRIDGE_RESET:
            if not await self._recover_isolated_chips(log):
                return OUTCOME_WAITING
            # Re-verify with the traffic pass: a chip back on the bus is not yet a mesh that moves
            # data across it.
            healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase=gate_phase)
            FABRIC_DEBT.note_evidence(evidence)
            if not healthy:
                return OUTCOME_WAITING
            log("chip(s) recovered and the mesh verified — no galaxy reset needed")
            health_event(
                "gate",
                phase=gate_phase,
                healthy=True,
                dirty=bool(ev is not None and ev.dirty),
                recovered_via="bridge-reset",
                evidence=evidence,
            )
            # The device reads clean again — the isolated chip is back on the bus — but, unlike the
            # galaxy-reset and tray-reset siblings below, do NOT retire a runtime-reported fault
            # here. A bridge reset is surgical: it re-inits only the isolated chip and leaves the
            # other 31 untouched, and the re-verify above is eth-blind (a traffic pass with a second
            # erisc disabled cannot see a stuck eth core). So a runtime-named fault that is a stuck
            # eth core on a NON-isolated chip survives this "verified" verdict unseen — retiring it
            # would let the next enum+ARC-healthy gate skip its clear and admit tenants to a
            # still-wedged mesh. Left set, the fault escalates to the galaxy reset that re-inits
            # every eth core and so earns the retire its siblings do. Cost in the common case (the
            # fault WAS the recovered chip): one needless ~60s galaxy reset of a healthy mesh at the
            # next gate — recoverable, and the safe default.
            self.deps.clear_device_dirty(verified=True, why=f"gate/{gate_phase}: bridge reset recovered the mesh")
            return OUTCOME_RECOVERED
        if stage == BELOW_FLOOR:
            # Below the galaxy floor there is no tray rung: no BMC tray re-power exists (spec 04 I23), and
            # no back-to-back reset sweep either. A whole-tray drop goes to the bounded power cycle now,
            # with no grace (I37); the I65 cooldown is its only wait. Anything else gets the I89 rungs
            # below and holds; the I76 bound runs the ladder at 10 min.
            offbus_set = _offbus_chip_ids(beats or {}, expected)
            self._note_whole_tray_drop(beats or {}, ev.off_bus if ev else 0, expected, log)
            hold_since = self.deps.device_hold_episode_since()
            if offbus_power_cycle.whole_tray_down_age_sec(hold_since) is not None:
                # A whole tray is down: the bounded power cycle now, not at the next idle window. It
                # runs no reset and holds the device; the post-boot startup gate re-proves the mesh.
                await self._offbus_gated_power_cycle(len(offbus_set), expected, hold_since, log)
            elif offbus_set and (ev is None or ev.last_action != STAGE_BRIDGE_RESET):
                # Spec 04 I89: a generic below-floor drop gets its kept rungs now (PCI rescan, then SBR
                # of the chips the defaults allow), not at the 10-min bound. A broker restart empties
                # isolated_chips, so without this a chip off the bus at startup waited for the bound.
                return await self._below_floor_gate_rungs(offbus_set, expected, log, gate_phase, hold_since)
            return OUTCOME_WAITING
        if stage in (STAGE_SMI_RESET, DEFER):
            # DEFER lands here too: a reset already cycling in its own scope is not a second reset
            # to start but one to ADOPT — _reset_and_verify_device waits that scope out
            # (await_foreign_scope) and verifies its result instead of issuing anything.
            ids_before = await self._offbus_ids_now(expected)
            hold_since = self.deps.device_hold_episode_since() or ""
            if stage == STAGE_SMI_RESET and not await asyncio.to_thread(self.mechanism.scope_active):
                failed = self.mechanism.failed_unchanged_entry("", "mesh_reset", ids_before) if ids_before else None
                if failed is None and self.mechanism.mesh_reset_spent(hold_since):
                    # Spec 04 I87: this hold's one mesh reset already ran and failed. The gate never runs a
                    # second one in the episode; it reports the failed rung, so the routing below climbs to
                    # the power cycle (whose last-chance sweep is the one re-run allowed) or hands off.
                    log(f"gate/{gate_phase}: this hold's mesh reset already ran and failed — not repeating it")
                    health_event(
                        "reset_rung_skipped_spent",
                        rung="mesh_reset",
                        path=f"gate/{gate_phase}",
                        held_since=hold_since,
                        off_bus=len(ids_before or ()),
                    )
                    metrics.stage_fired("smi_reset", "blocked")
                    return OUTCOME_WAITING
                if failed is not None:
                    # Spec 04 I58: this boot's mesh reset already failed on exactly these chips, in this
                    # process or before a broker restart. Never fire it again on that set: report it as
                    # the failed rung it is, so the caller's routing takes the host rung or hands off.
                    log(
                        f"gate/{gate_phase}: {len(ids_before)}/{expected} off-bus and a mesh reset already "
                        f"failed on exactly these chips this boot (rc {failed.get('rc')}) — not repeating it"
                    )
                    health_event(
                        "reset_rung_skipped_failed_unchanged",
                        rung="mesh_reset",
                        path=f"gate/{gate_phase}",
                        off_bus=len(ids_before),
                        rc=failed.get("rc"),
                    )
                    self.mechanism.last_reset_exit_nonzero = failed.get("rc") not in (None, 0)
                    self.mechanism.mark_mesh_reset_failed()
                    metrics.stage_fired("smi_reset", "blocked")
                    return OUTCOME_WAITING
            recovered = await self._reset_and_verify_device(indices, log)
            await self._note_mesh_reset_failed(recovered, hold_since, ids_before, expected)
            if not recovered:
                return OUTCOME_WAITING
            self.deps.clear_device_reported_fault(f"gate/{gate_phase}: reset recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why=f"gate/{gate_phase}: reset recovered the mesh")
            return OUTCOME_RECOVERED
        if stage == STAGE_POWER_CYCLE:
            # The caller has already confirmed the opt-in, the two-strikes precondition, that no
            # tenant holds the device and that the durable rate limits pass. Before taking the box
            # down, give every reset type one last back-to-back run, then ONE settle and ONE verify
            # (ladder-v2: the last-chance sweep gates every host rung): a drop that came back needs
            # no reboot or power cycle.
            # No heartbeats passed: the sweep reads them itself, as the I37 hook does, rather than
            # count every chip off and take a partial drop for class C.
            last_chance = await self._settle_and_verify_before_host_rung(
                expected,
                log,
                gate_phase,
                offbus_chips=None if beats is None else _offbus_chip_ids(beats, expected),
                context="gate ladder",
            )
            if last_chance:
                return OUTCOME_RECOVERED
            if last_chance is None:
                return OUTCOME_WAITING
            # A fired host rung takes the box with it, so there is no verdict to report: WAITING
            # leaves the episode as it stands for the boot that comes back to verify it.
            if stage == STAGE_POWER_CYCLE:
                cause = "the reset ladder could not recover the device"
                if await self.deps.auto_power_cycle_host(log, cause) is False:
                    self._hold_after_cycle_launch_failure(log, cause)
            return OUTCOME_WAITING
        raise ValueError(f"escalate(): {stage!r} is not a gate action rung")

    # ---- the tray-down prelude (spec 04 I19, issue #26) -----------------------------------------
    #
    # The episode latch lives on this instance: set at the first off-bus sighting of an episode,
    # before any reset, and never re-derived while the episode is open, because a reset can itself
    # turn a 1-chip drop into a whole tray off. tray_down_episode_end() clears it when the mesh is
    # released. Deliberately not persisted: a broker restart or a reboot starts with none and latches
    # again at its first sighting.
    _td: Optional[dict] = None
    _td_reset_seen: bool = False

    def tray_down_episode_end(self) -> None:
        """The episode closed (the gate released the mesh, or the prelude's rescan recovered it): the
        next off-bus sighting is a fresh onset, and a reset before this point no longer counts."""
        self._td = None
        self._td_reset_seen = False
        self.mechanism.reset_since_release = False
        self.mechanism.mesh_reset_failed_since_release = False

    async def _tray_down_latch(self, expected: int, beats: Optional[dict]) -> Optional[str]:
        """``TRAY_DOWN_ONSET``/``"PARTIAL"`` for the open episode, latching it at the first off-bus
        sighting; None when nothing is off the bus yet, the prelude is switched off, or this is not
        a Galaxy-sized mesh. An off set first seen after any reset (the ladder's own, or a manual
        ``reset_with_quiesce`` since the last release) or while one is cycling is never an onset."""
        if not tray_down_capture_enabled() or expected < 2 * UBB_CHIP_COUNT:
            return None
        if self._td is not None:
            return self._td["path"]
        if beats is None:
            beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off = _offbus_chip_ids(beats or {}, expected)
        if not off:
            return None
        after_reset = (
            self._td_reset_seen
            or self.mechanism.reset_since_release
            or self.mechanism.reset_in_flight
            or bool(await asyncio.to_thread(self.mechanism.scope_active))
        )
        counts = None
        # The whole bus off keeps its own route (_host_escalation_for_drop) with nothing to capture
        # per tray; a reset already fired means this is not a first sighting.
        if len(off) < expected and not after_reset:
            counts = _tray_down_counts(off, self._tray_map_now())
        path = TRAY_DOWN_ONSET if counts else "PARTIAL"
        self._td = {
            "path": path,
            "onset": counts or _off_per_tray(off, self._tray_map_now()) or {"count": len(off)},
            "off": sorted(int(c) for c in off),
            "epoch": time.time(),
            "rescanned": False,
            "captured": False,
            "prelude_done": path != TRAY_DOWN_ONSET,
        }
        health_event(
            "tray_down_latched", path=path, off_per_tray=self._td["onset"], expected=expected, after_reset=after_reset
        )
        return path

    def _tray_down_capture(self, onset: dict, expected: int) -> dict:
        """The onset evidence, read-only, under its deadline, fsync'd into an incident bundle."""
        from tt_device_mcp.health import bmc_capture

        bundle = None
        if self.deps.capture_incident is not None:
            try:
                bundle = self.deps.capture_incident("tray_down", evidence={"onset": onset, "expected": expected})
            except Exception:  # noqa: BLE001 - a failed bundle must not stop the capture or the ladder
                bundle = None
        if not isinstance(bundle, Path):
            # No incident bundle (capture off, or it failed): the BMC reads still get a directory
            # of their own, so the capture is never lost for want of one.
            bundle = bmc_capture.fallback_bundle()
        buses = (self.deps.chip_buses_provider() if self.deps.chip_buses_provider else None) or {}
        tray_map = self._tray_map_now() or {}
        trays = sorted(int(t) for t in onset.get("off_per_tray", {}))
        # lspci on the bridge above every off chip, found from the chip's banked PCI address: the
        # bridge stays enumerated when the endpoint drops, so its link status survives, and the
        # first reset is about to erase it. The endpoint too while the kernel still lists it.
        off = set(onset.get("off") or [])
        addrs = [buses.get(c) or buses.get(str(c)) for t in trays for c in sorted(tray_map.get(t, [])) if c in off]
        targets = bmc_capture.lspci_targets([a for a in addrs if a])
        return bmc_capture.capture_tray_down(
            bundle, onset, trays=trays, all_trays=sorted(tray_map), pci_targets=targets
        )

    async def _tray_down_prelude(self, expected: int, log, *, phase: str) -> bool:
        """A tray-down onset, before the ladder's first rung: one PCI rescan, then the read-only
        capture, then a re-read of the bus. True only when every chip came back and the full verify
        (fabric included) passed — the ladder is not needed; a runtime-reported fault is kept, as by
        the bridge rung. Otherwise False and the caller runs the ladder exactly as it would have;
        this never resets and never power-cycles.

        The rescan writes to the PCI subsystem, so it waits while a tenant holds the device (or the
        holder scan is unreadable); the capture is read-only and never waits. A rescan that waited
        runs at the first later ladder entry with no tenant, still before any rung."""
        td = self._td
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        tenant_active = self._tenant_active(scan)
        rescan = "not run"
        t0 = time.monotonic()
        # Spec 04 I69: this rescan resets nothing, so it must not re-enumerate a chip the broker removed
        # while hung. Withheld once for the episode, with no wait: the ladder's resets follow at once.
        removed = removed_chip_ids()
        if not td["rescanned"]:
            if tenant_active:
                rescan = "waiting (a tenant holds the device)"
            elif removed:
                td["rescanned"] = True
                rescan = "withheld (removed chip not reset)"
                health_event("pci_rescan_withheld", chips=removed, unreset=removed, reason="removed_chip_not_reset")
                log(f"tray-down: PCI rescan withheld, chip(s) {removed} were removed and not reset — to the ladder")
            else:
                td["rescanned"] = True
                try:
                    FABRIC_DEBT.note_reset("PCI rescan")
                    await asyncio.to_thread(_pci_rescan)
                    rescan = "ran"
                except OSError as e:
                    rescan = f"failed ({e})"
        capture: dict = {}
        if not td["captured"]:
            td["captured"] = True
            onset = {"off_per_tray": td["onset"], "off": td["off"], "expected": expected, "epoch": td["epoch"]}
            capture = await asyncio.to_thread(self._tray_down_capture, onset, expected)
            if str(capture.get("cpld", "")).startswith("skipped"):
                log("tray-down capture: no TT_DEVICE_MCP_TRAY_CPLD_* config — CPLD registers not read")
        td["prelude_done"] = td["rescanned"] and td["captured"]
        trays = sorted(k for k in td["onset"] if isinstance(k, int))
        if rescan != "ran":
            off = td["off"]
        else:
            # The capture usually outlasts the settle; wait only for what is left of it.
            await asyncio.sleep(max(0.0, TRAY_DOWN_RESCAN_SETTLE_SEC - (time.monotonic() - t0)))
            beats = await asyncio.to_thread(self.deps.read_heartbeats)
            off = sorted(int(c) for c in _offbus_chip_ids(beats or {}, expected))
            await asyncio.to_thread(drop_returned_isolated, self.deps.isolated_chips(), "tray-down PCI rescan", log)
            if not off:
                healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase=phase)
                FABRIC_DEBT.note_evidence(evidence)
                health_event("tray_down_prelude", rescan=rescan, off=0, healthy=healthy, capture=capture)
                if healthy:
                    log("tray-down: every chip came back on the PCI rescan and the mesh verified — no reset needed")
                    # Back on the bus, so no longer isolated: the next gate must not bridge-reset them.
                    isolated = self.deps.isolated_chips()
                    for c in td["off"]:
                        isolated.discard(str(c))
                    # Keep a runtime-reported fault, as the bridge rung does: a rescan re-inits no eth
                    # core and the verify cannot see a stuck one, so only a galaxy reset earns the retire.
                    self.deps.clear_device_dirty(verified=True, why=f"{phase}: tray-down chips back after a PCI rescan")
                    self.tray_down_episode_end()
                    return True
        _logger.error(
            "TRAY-DOWN: trays %s, chips off %s (%d/%d), rescan %s, capture %s — the full reset ladder follows",
            trays,
            off,
            len(off),
            expected,
            rescan,
            "done" if capture else ("done earlier" if td["captured"] else "not run"),
        )
        health_event("tray_down_prelude", rescan=rescan, off=len(off), trays=trays, capture=capture)
        return False

    async def _below_floor_kept_rungs(
        self,
        offbus_chips: set,
        expected: int,
        log,
        *,
        context: str,
        at_bound: bool = False,
        sbr_tried: frozenset = frozenset(),
    ) -> Optional[set]:
        """The kept rungs below the galaxy floor (spec 04 I89): one PCI rescan, then an SBR of the
        bridged off-bus chips through their parent bridges (:meth:`_recover_isolated_chips`). Never a
        mesh reset, and never a release: the caller runs the full check, fabric included.

        SBR targets are the chips with a bridge the rung can write to: the chips already isolated
        (reading all-ones), the chips the broker removed (their recorded bridge, I69), and a chip absent
        from both heartbeat reads whose own PCIe node is gone, only when TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET
        is set (its bridge is found by the secondary bus). The bound widens nothing: without the opt-in a
        gone chip has no bridge, so its SBR is skipped and journaled, and the chip is not queued. A chip
        still enumerated is on the bus (an ARC stall): never SBR'd; the bound's mesh reset covers it.
        A gone chip this run queued and the rung could not fire on is un-queued again, so no later gate
        pass reads it as an SBR candidate. ``sbr_tried``: chips the caller's own bridge rung already
        reset this pass; with no other target, no second SBR runs.

        Returns the chips still off after the rungs, or None when nothing ran (a reset is cycling in
        its own scope, or a tenant holds the device). A chip the broker removed withholds the bare
        rescan (I69); the SBR resets it first and rescans itself."""
        if await asyncio.to_thread(self.mechanism.scope_active):
            return None
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        if self._tenant_active(scan):
            log(f"below-floor rungs ({context}): a tenant holds the device — not rescanning or resetting")
            return None
        rescan = "withheld (removed chip not reset)" if removed_chip_ids() else "ran"
        if rescan == "ran":
            try:
                FABRIC_DEBT.note_reset("PCI rescan")
                await asyncio.to_thread(_pci_rescan)
            except OSError as e:
                rescan = f"failed ({e})"
        if rescan == "ran":
            await asyncio.sleep(TRAY_DOWN_RESCAN_SETTLE_SEC)
        isolated = self.deps.isolated_chips()
        if rescan == "ran":
            await asyncio.to_thread(drop_returned_isolated, isolated, f"{context} PCI rescan", log)

        async def _still_off() -> set:
            try:
                beats = await asyncio.to_thread(self.deps.read_heartbeats)
            except Exception as exc:  # noqa: BLE001 - an unreadable heartbeat counts every chip off, as the gate does
                log(f"could not read the heartbeats after the below-floor rungs: {exc!r}")
                beats = {}
            return _offbus_chip_ids(beats or {}, expected)

        still_off = await _still_off()
        gone = []
        pci_map = self.deps.device_pci_map()
        for c in sorted(set(still_off) & set(offbus_chips), key=int):
            bdf = pci_map.get(c)
            if bdf and c not in isolated and c not in removed_chip_ids():
                if not await asyncio.to_thread(chip_node_present, bdf):
                    gone.append(c)
        queued = gone if gone_chip_bridge_reset_enabled() else []
        if gone and not queued:
            log(
                f"below-floor rungs ({context}): chip(s) {gone} left the bus with no bridge to reset through "
                "(TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET is off) — SBR skipped"
            )
            health_event("below_floor_sbr_skipped", context=context, chips=gone, reason="no_bridge_without_opt_in")
        isolated.update(queued)
        targets = (set(isolated) | set(removed_chip_ids())) - set(sbr_tried)
        fired = sorted(set(isolated) | set(removed_chip_ids()), key=int) if targets else []
        unqueued = []
        if fired:
            await self._recover_isolated_chips(log)
            reasons = getattr(self, "last_bridge_reset_reasons", {}) or {}
            # Spec 04 I89: a queued chip the rung could not fire on (no bridge, gated, no privilege,
            # withheld) is not an SBR candidate: left isolated, every later gate pass would route to a
            # no-op bridge rung. A chip still gone is re-detected at the next run.
            unqueued = [c for c in queued if c in isolated and c in reasons]
            for c in unqueued:
                isolated.discard(c)
            if unqueued:
                health_event("gone_chip_bridge_reset_unqueued", chips=unqueued, reason="sbr_not_fired")
            still_off = await _still_off()
        log(
            f"below-floor rungs ({context}): PCI rescan {rescan}; chip(s) {sorted(offbus_chips, key=int)} were off, "
            f"{sorted(still_off, key=int) or 'none'} still off" + (f"; SBR tried on {fired}" if fired else "")
        )
        health_event(
            "below_floor_kept_rungs",
            context=context,
            at_bound=at_bound,
            rescan=rescan,
            off_before=sorted(offbus_chips, key=int),
            queued=queued,
            sbr=fired,
            sbr_skipped=[c for c in gone if c not in queued],
            unqueued=unqueued,
            still_off=sorted(still_off, key=int),
            expected=expected,
        )
        return still_off

    async def _below_floor_gate_rungs(
        self, offbus_set: set, expected: int, log, gate_phase: str, hold_since: Optional[str]
    ) -> str:
        """The gate's below-floor kept rungs (spec 04 I89), once per hold episode and off-bus set, then
        the full check with fabric. RECOVERED only on that check's pass; otherwise the hold stands and
        the I76 bound runs the full ladder at 10 min. Like the bridge rung, a runtime-reported fault is
        kept: a rescan and an SBR re-init no eth core on the other chips."""
        key = (hold_since, frozenset(offbus_set))
        prev = self._below_floor_rungs_key
        if prev == key:  # the same episode and off-bus set
            return OUTCOME_WAITING
        self._below_floor_rungs_key = key
        still_off = await self._below_floor_kept_rungs(offbus_set, expected, log, context=f"gate/{gate_phase}")
        if still_off is None:
            self._below_floor_rungs_key = None  # nothing ran: the next pass tries again
            return OUTCOME_WAITING
        if still_off:
            return OUTCOME_WAITING
        healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase=gate_phase)
        FABRIC_DEBT.note_evidence(evidence)
        if not healthy:
            log("every chip is back after the below-floor rescan/SBR but the full check failed — held")
            health_event("below_floor_kept_rungs_unverified", phase=gate_phase, evidence=evidence)
            return OUTCOME_WAITING
        log("every chip is back after the below-floor rescan/SBR and the full check passed — no mesh reset needed")
        health_event("gate", phase=gate_phase, healthy=True, recovered_via="below-floor-rescan-sbr", evidence=evidence)
        self.deps.clear_device_dirty(verified=True, why=f"gate/{gate_phase}: below-floor rescan/SBR recovered the mesh")
        return OUTCOME_RECOVERED

    def _tenant_active(self, scan, *, force: bool = False) -> bool:
        """Is anyone using the device? TWO independent sources, because either one alone lies:

        * the holder scan — open ``/dev/tenstorrent`` fds. Blind while a running job is off the
          device (compiling, loading weights, between opens), which is minutes of a real run.
        * the broker's own queue — a job in RUNNING state. Authoritative about work the broker
          itself dispatched, and it does not care whether that job currently holds an fd.

        Measured failure this exists to stop (box B, 2026-09-17 10:07:09Z): the stuck-hold
        escalation read an empty holder scan 106 s into a running job, fired a mesh reset, and the
        job died of SIGPIPE at 10:09:18Z the moment the reset released the device. The broker knew
        the job was RUNNING the whole time; nothing asked it.

        ``force`` (the hold-deadline backstop) still waives an UNREADABLE scan — that guard protects
        against blindness, not against a known tenant — but never waives a real one, fd-held or
        queue-known: resetting out from under a live job is the thing the guard is for."""
        return self._tenant_blocker(scan, force=force) is not None

    def _tenant_blocker(self, scan, *, force: bool = False) -> Optional[dict]:
        """Which check of :meth:`_tenant_active` blocks, as health_event fields, or None when none does:
        ``blocker`` is ``job_running``, ``startup_verify`` (the running escalate's I75 stand-down), ``tenant``
        (with the first tenant's pid, uid, user and comm) or ``scan_incomplete`` (with the scan's source
        and gaps). A deferral that names its blocker tells a
        real tenant apart from a scan that merely could not see."""
        if self.deps.job_running():
            return {"blocker": "job_running"}
        if self._stand_down is not None and self._stand_down():
            self.startup_verify_deferred = True
            return {"blocker": "startup_verify"}
        # The broker's own probes and the holders it killed for a dead chip are not tenants, nor is
        # a gap they leave while exiting (spec 04 I59).
        scan = discount_broker_internal(scan)
        tenants = [h for h in scan.holders if is_tenant(h)]
        if tenants:
            h = tenants[0]
            return {
                "blocker": "tenant",
                "pid": h.pid,
                "uid": h.uid,
                "user": username_for_uid(h.uid),
                "comm": holder_comm(h.pid),
                "tenants": len(tenants),
            }
        if not force and not scan.complete:
            return {"blocker": "scan_incomplete", "source": scan.source, "gaps": list(scan.gaps)}
        return None

    @staticmethod
    def _describe_blocker(blocker: dict) -> str:
        """The log-line form of a :meth:`_tenant_blocker` result."""
        kind = blocker["blocker"]
        if kind == "job_running":
            return "a broker job is running"
        if kind == "startup_verify":
            return "the startup gate (full check, fabric included) runs next on this startup hold"
        if kind == "tenant":
            more = f" (+{blocker['tenants'] - 1} more)" if blocker["tenants"] > 1 else ""
            return (
                f"tenant pid {blocker['pid']} uid {blocker['uid']} ({blocker['user']}) "
                f"comm {blocker['comm']} holds the device{more}"
            )
        gaps = "; ".join(blocker["gaps"]) or "no detail"
        return f"holder scan incomplete (source={blocker['source']}: {gaps})"

    def _sweep_phase(self) -> Optional[tuple]:
        """The hold phase a last-chance sweep counts for (spec 04 I87): (episode, at the 10-min bound).
        None without an episode clock, where nothing can key a repeat."""
        hold_since = self.deps.device_hold_episode_since()
        if not hold_since:
            return None
        age = _hold_age_sec(hold_since)
        return (hold_since, age is not None and age >= _stuck_hold_ceiling_sec())

    async def _issue_all_resets_back_to_back(
        self, trays: list, expected: int, log, *, do_sbr: bool = True, mesh_reset: bool = True
    ) -> bool:
        """Fire EVERY reset type once, back-to-back, with no waiting or verifying between them: SBR on
        any chip still bridged, then the mesh-wide reset —
        each issued as soon as the previous command returns. The single implementation of "all rungs",
        used only by the last-chance sweep that gates the power cycle. Never verifies and never raises:
        a rung that fails to launch is logged and the next one is issued anyway — before a power cycle
        there is nothing left to lose. The caller owns the guards (tenant, scope), the settle and the
        verify.

        Returns whether the sweep was COMPLETE: False when the per-host gate (spec 04 I24) or the
        outage hold (I28) refused it. An incomplete sweep is not the full ladder,
        so the caller must not climb to a power cycle on it (I22).

        ``mesh_reset=False`` fires SBR only, so the last-chance sweep can re-probe the bus before the
        mesh reset (spec 04 I86); the caller then fires the mesh reset with ``do_sbr=False``."""
        off_bus = pcie_guard.chips_off_bus(expected)
        allowed, why = pcie_guard.host_reset_gate(off_bus)
        if not allowed:
            log(f"back-to-back reset sweep NOT fired: {why}; holding — no host rung without the full ladder")
            health_event("host_reset_gated", context="reset_sweep", reason=why, trays=trays, host_at_risk=True)
            return False
        # The operator's outage hold (spec 04 I28) refuses the whole sweep: its last step is the mesh reset.
        if refuse_mesh_reset("reset_sweep", self.reset_argv([str(i) for i in range(expected)]), log, off_bus):
            return False
        # The caller ends it after its settle and verify (the host-hang latch, I25).
        pcie_guard.begin_offbus_reset("reset sweep", off_bus)
        # Hold reset_in_flight across the whole sweep so the dead-chip sampler defers instead of
        # isolating a tray mid-reset. Cleared on every exit.
        self.mechanism.reset_in_flight = True
        self.mechanism.reset_since_release = True
        try:
            # 1) SBR — only when the caller has something surgical to fire at. The tray branch always
            # does (the dead tray's own chips); the last-chance sweep passes do_sbr only when a chip is
            # actually isolated, because a mass drop must never be nibbled at with per-chip bridge
            # resets — that is the galaxy reset's case, not the gentle rung's.
            if do_sbr:
                await self._recover_isolated_chips(log)
            if not mesh_reset:
                return True
            # 2) mesh-wide reset, no verify (the ONE verify belongs to the caller). Lazy import:
            # select_recovery lives in the package __init__ that imports THIS module, so a top-level
            # import would be circular; by call time the package is fully loaded.
            from tt_device_mcp.health.recovery import select_recovery

            argv = select_recovery(self.monitor, self.mechanism, self.deps).reset_argv(
                [str(i) for i in range(expected)]
            )
            aer_mask = pcie_guard.mask_for_mesh_reset(log)
            try:
                await self.mechanism.reset_with_quiesce(argv, log)
            finally:
                if aer_mask is not None:
                    await asyncio.to_thread(aer_mask.restore)
        except Exception as exc:  # noqa: BLE001 - the host rung above must fire even if a reset blew up
            log(f"the back-to-back reset sweep raised {exc!r} — continuing to the host rung")
            health_event("last_chance_reset_sweep_error", error=repr(exc))
        finally:
            self.mechanism.reset_in_flight = False
        return True

    async def _settle_and_verify_before_host_rung(
        self,
        expected: int,
        log,
        gate_phase: str,
        *,
        offbus_chips: Optional[set] = None,
        context: str = "",
        fabric_77_unverified: bool = False,
    ) -> Optional[bool]:
        """The LAST-CHANCE gate the power cycle passes through: fire every reset type once more,
        back-to-back, then ONE settle and ONE verify — and only if the mesh is still bad does the
        caller power-cycle. True iff the mesh came back, in which case the fault/dirty are retired and
        the door reopens, so the caller skips the power cycle entirely.

        Why the resets and not just a settle: a power cycle takes the whole box down and costs
        minutes, so before paying that there is nothing to lose by re-issuing SBR and the mesh reset
        once ("we always need to give the resets one last chance before power cycle"). It runs at
        most once per hold phase (spec 04 I87): once below the 10-min bound, and once more as part of
        the bound's own ladder.

        Spec 04 I86: when SBR fires, the bus is re-probed before the mesh reset. Once every expected
        chip is back, the full check (fabric included) runs first and the mesh reset only on its
        failure, followed by one more settle and verify. The probe never lifts a hold by itself.

        ``offbus_chips`` names the off-bus chips the journal lines place (read from the heartbeats when
        not given). The sweep is SKIPPED — the
        settle and verify still run — when a tenant holds the mesh or a reset is already cycling in its
        own scope: those are the two guards a destructive rung never crosses.

        Returns True when the mesh came back, False when the FULL sweep ran and it did not (the caller
        climbs), and None when the sweep was skipped or incomplete and the mesh is still bad: the
        full ladder has not run, so the caller holds and fires no host rung this pass (I22)."""
        where = f" ({context})" if context else ""
        tray_map = self._tray_map_now()
        if offbus_chips is None:
            try:
                beats = await asyncio.to_thread(self.deps.read_heartbeats)
            except Exception as exc:  # noqa: BLE001 - an unreadable heartbeat counts every chip off, as the gate does
                log(f"could not read the heartbeats for the last-chance sweep: {exc!r}")
                beats = {}
            offbus_chips = _offbus_chip_ids(beats or {}, expected)
        fault_class = self._power_cycle_direct_class_now(offbus_chips, expected, tray_map, log)
        if fault_class is not None and not await asyncio.to_thread(self.mechanism.scope_active):
            # Spec 04 I37: the sweep's mesh reset has not healed this class (a
            # whole tray, or an all-off bus whose mesh reset already failed). The caller climbs at once.
            log(
                f"last chance before the host rung{where}: class {fault_class} — the sweep does not heal it, skipping it"
            )
            health_event(
                "last_chance_sweep_skipped",
                context=context or gate_phase,
                fault_class=fault_class,
                off_bus=len(offbus_chips),
                expected=expected,
            )
            return False
        if (
            offbus_chips
            and self.mechanism.rung_failed_unchanged("", "mesh_reset", offbus_chips)
            and not await asyncio.to_thread(self.mechanism.scope_active)
        ):
            # Spec 04 I58: this boot's mesh reset already ran and failed on exactly these chips, so the
            # sweep would end in the same reset again, which measures nothing new. Climb.
            log(
                f"last chance before the host rung{where}: the mesh reset already failed on these chips this boot — skipping the sweep"
            )
            health_event(
                "last_chance_sweep_skipped",
                context=context or gate_phase,
                reason="mesh_reset_failed_unchanged",
                off_bus=len(offbus_chips),
                expected=expected,
            )
            return False
        sweep_phase = self._sweep_phase()
        if (
            sweep_phase is not None
            and self.mechanism.sweep_spent == sweep_phase
            and not await asyncio.to_thread(self.mechanism.scope_active)
        ):
            # Spec 04 I87: the full sweep already ran in this phase of the hold (below the 10-min bound,
            # or as the bound's own ladder) and the mesh did not verify. Another would only repeat its
            # mesh reset; the caller goes on to the power cycle.
            log(
                f"last chance before the host rung{where}: the sweep already ran in full this hold phase — not repeating it"
            )
            health_event(
                "last_chance_sweep_skipped",
                context=context or gate_phase,
                reason="sweep_spent",
                at_bound=sweep_phase[1],
                off_bus=len(offbus_chips),
                expected=expected,
            )
            return False
        trays = _affected_trays(offbus_chips, expected, tray_map) or []
        placement = _offbus_placement(offbus_chips, self._chip_buses_now(), tray_map)
        swept = False
        mesh_reset_left = False
        if await asyncio.to_thread(self.mechanism.scope_active):
            log(f"last chance before the host rung{where}: a reset is already cycling in its own scope — adopting it")
        else:
            scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
            if self._tenant_active(scan):
                log(f"last chance before the host rung{where}: a tenant holds the mesh — not resetting over it")
            else:
                swept = True
                health_event(
                    "last_chance_reset_sweep",
                    context=context or gate_phase,
                    trays=trays,
                    off_bus=len(offbus_chips or set()),
                    off_bus_chips=placement,
                    expected=expected,
                    host_at_risk=True,
                )
                log(
                    f"last chance before the host rung{where}: firing every reset type back-to-back "
                    f"(SBR -> bus re-probe -> mesh reset) — nothing to lose before the "
                    f"power cycle; off the bus now: {_placement_text(placement)}"
                )
                if self.deps.isolated_chips():
                    # Spec 04 I86: SBR first, then a cheap re-probe of the bus. Once every expected chip
                    # is back, no further rung fires before the full check: the mesh reset runs only if
                    # that check fails.
                    swept = await self._issue_all_resets_back_to_back(
                        trays, expected, log, do_sbr=True, mesh_reset=False
                    )
                    if swept and await asyncio.to_thread(self._bus_whole_now, expected, log):
                        mesh_reset_left = True
                        log(
                            "every expected chip is back on the bus after SBR — the full check runs before the mesh reset"
                        )
                        health_event(
                            "last_chance_sweep_bus_whole", context=context or gate_phase, after="sbr", expected=expected
                        )
                    elif swept:
                        swept = await self._issue_all_resets_back_to_back(trays, expected, log, do_sbr=False)
                else:
                    swept = await self._issue_all_resets_back_to_back(trays, expected, log, do_sbr=False)
        # The bus is already whole: no re-enumeration left to settle for (the bridge-reset rung verifies
        # at once too).
        settle = 0 if mesh_reset_left else _settle_before_host_rung_sec()
        healthy, evidence = await self._settle_and_verify_sweep(
            expected, log, gate_phase, settle, context=context, fabric_77_unverified=fabric_77_unverified
        )
        if not healthy and mesh_reset_left:
            # The bus probe alone never lifts (I86): the full check failed, so the remaining rung runs and
            # the caller climbs to the power cycle if the mesh still does not verify.
            log("the full check failed on a whole bus after SBR — firing the remaining rung (mesh reset)")
            health_event("last_chance_sweep_resumed", context=context or gate_phase, rung="mesh_reset")
            swept = await self._issue_all_resets_back_to_back(trays, expected, log, do_sbr=False)
            mesh_reset_left = False
            self._sweep_read_77 = False  # the last verify decides, as with one verify
            healthy, evidence = await self._settle_and_verify_sweep(
                expected,
                log,
                gate_phase,
                _settle_before_host_rung_sec(),
                context=context,
                fabric_77_unverified=fabric_77_unverified,
            )
        if not healthy:
            if not swept:
                log(f"the full reset ladder did not run{where} — holding; no power cycle this pass")
                health_event(
                    "host_rung_held", context=context or gate_phase, reason="reset sweep skipped or incomplete"
                )
                return None
            self.mechanism.sweep_spent = sweep_phase
            return False
        via = "last-chance-reset-sweep" if swept else "settle-before-host-rung"
        if swept and mesh_reset_left:
            via = "last-chance-reset-sweep (SBR, bus whole)"
        self.deps.clear_device_reported_fault(f"gate/{gate_phase}: mesh recovered before the host rung ({via})")
        released = self.deps.clear_device_dirty(
            verified=True, why=f"gate/{gate_phase}: mesh recovered before the host rung ({via})"
        )
        if released is False:
            # enum+ARC passed but the resets still owe a passing fabric pass (spec 04 I32): the funnel
            # held the device fabric-unverified. No host rung on a verify that passed; the fabric relift
            # owns the hold, bounded by 03 I40/I48.
            log(f"the mesh passed enum+ARC after the {via}, but no fabric pass has passed since — held")
            health_event("host_rung_sweep_unverified", context=context or gate_phase, via=via, evidence=evidence)
            return True
        log(f"the mesh verified after the {via} — no power cycle needed")
        health_event("gate", phase=gate_phase, healthy=True, recovered_via=via, evidence=evidence)
        return True

    def _bus_whole_now(self, expected: int, log) -> bool:
        """The cheap re-probe between sweep rungs (spec 04 I86): every expected chip is in sysfs with a
        live heartbeat and none is still isolated. Only decides whether to skip ahead to the full check;
        never a verdict on its own. An unreadable bus reads as not whole, so the next rung fires."""
        try:
            off_bus = pcie_guard.chips_off_bus(expected)
            isolated = self.deps.isolated_chips()
        except Exception as exc:  # noqa: BLE001 - a failed probe must never skip a rung
            log(f"could not re-probe the bus between sweep rungs: {exc!r} — firing the next rung")
            return False
        return off_bus == 0 and not isolated

    async def _settle_and_verify_sweep(
        self, expected: int, log, gate_phase: str, settle: int, *, context: str, fabric_77_unverified: bool
    ) -> tuple:
        """One settle and one full verify (fabric included) after sweep rungs; ends the off-bus reset
        intent. A fabric 77 after an UNKNOWN reset reads as unhealthy (spec 04 I29)."""
        if settle:
            log(f"settling {settle}s for re-enumeration, then one verify before the host rung")
        try:
            await asyncio.sleep(settle)
            healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase=gate_phase)
            FABRIC_DEBT.note_evidence(evidence)
        finally:
            pcie_guard.end_offbus_reset()
        if healthy and fabric_77_unverified and expected > 1 and self._fabric_ran_but_unverified(evidence):
            # The caller is past an UNKNOWN reset whose I40 fallback refused (spec 04 I29): a 77 here is
            # the same no-verdict the reset already got, never a recovered fabric.
            log("the sweep's verify is still a fabric 77 — the fabric stays unverified, not recovered")
            health_event("host_rung_sweep_unverified", context=context or gate_phase, evidence=evidence)
            self._sweep_read_77 = True
            healthy = False
        return healthy, evidence

    async def _class_a_cooldown_sweep(self, offbus: set, expected: int, log) -> str:
        """Spec 04 I73 at the 10-min mark: a whole-tray (class A) hold whose power cycle the boot or
        post-cycle cooldown refused has stood CLASS_A_LADDER_AFTER_SEC since its last cycle or boot. Fire
        the resets that remain (:meth:`_issue_all_resets_back_to_back`: SBR on any chip still bridged,
        then the mesh-wide reset; no tray step exists, I23), then ONE settle and ONE full health check,
        fabric included. Only a passing check releases the device; a failed one keeps it held, and the
        power cycle fires the moment the cooldown ends (I65). Never under a live tenant or over a reset
        already cycling in its own scope: a guard that holds it leaves the once-per-cycle-or-boot mark
        unspent, so the next forced pass inside the cooldown runs it.

        OUTCOME_RECOVERED on a passing check, OUTCOME_TERMINAL when the sweep ran and the check failed,
        OUTCOME_WAITING when a guard held it or the sweep was refused (nothing ran)."""
        if await asyncio.to_thread(self.mechanism.scope_active):
            log("class A 10-min sweep: a reset is already cycling in its own scope — holding")
            return OUTCOME_WAITING
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        blocker = self._tenant_blocker(scan, force=True)
        if blocker is not None:
            self.tenant_deferred = True
            log(f"class A 10-min sweep: {self._describe_blocker(blocker)} — holding; sweeps once idle")
            health_event("stuck_hold_deferred", mesh="offbus", forced=True, **blocker)
            return OUTCOME_WAITING
        if not self.mechanism.class_a_ladder_due():
            return OUTCOME_WAITING  # once per cycle or boot: taken only now that the guards let it run
        trays = _affected_trays(offbus, expected, self._tray_map_now()) or []
        settle = _settle_before_host_rung_sec()
        log(
            f"whole tray(s) {trays} off and the power cycle in cooldown {CLASS_A_LADDER_AFTER_SEC}s after the "
            f"last cycle or boot — SBR then the mesh reset, one {settle}s settle and one full check; the "
            f"cycle follows at the cooldown's end if it fails"
        )
        health_event("class_a_cooldown_ladder", off_bus=sorted(offbus, key=int), expected=expected, trays=trays)
        self._td_reset_seen = True  # an onset first sighted after a reset is not an onset (I19)
        complete = await self._issue_all_resets_back_to_back(trays, expected, log)
        if not complete:
            log(
                "class A 10-min sweep refused (reset gate or outage hold) — holding; the cycle follows at the cooldown's end"
            )
            return OUTCOME_WAITING
        try:
            await asyncio.sleep(settle)
            healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase="class-a-10min")
            FABRIC_DEBT.note_evidence(evidence)
        finally:
            pcie_guard.end_offbus_reset()
        health_event("class_a_cooldown_ladder_verified", healthy=bool(healthy), trays=trays, evidence=evidence)
        if healthy:
            log("class A 10-min sweep recovered the mesh: the full check passed, fabric included")
            self.deps.clear_device_reported_fault("class A 10-min sweep recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why="class A 10-min sweep recovered the mesh")
            return OUTCOME_RECOVERED
        log("class A 10-min sweep did not recover the mesh — holding; the power cycle fires at the cooldown's end")
        return OUTCOME_TERMINAL

    async def _escalate_stuck_hold(
        self, indices: list, expected: int, log, *, force: bool = False, measured_fault: bool = False
    ) -> bool:
        """Escalate a present-mesh hold the read-only relift cannot clear to the gate's galaxy reset
        once it has stood past the ceiling. Returns True iff a reset ran (recovered or not); False
        leaves the hold standing for the next window.

        This is the gate's OWN ``_reset_and_verify_device`` — only the trigger is new (the idle timeout,
        not the next job), so an idle box stops stranding on a hold no read-only check can lift. Called
        only from the relift's under-lock frozen/unreadable-eth branch, where a healthy enum+ARC pass has
        already proven every chip present, so a galaxy reset usually re-inits every eth core. An eth
        wedge does not self-heal by waiting and the box is idle and tenant-refused, so the escalation runs
        the hold's ONE mesh reset; once that has failed, every later escalation of the episode climbs
        (the last-chance sweep once, then the power cycle) and never repeats it (spec 04 I87).

        Every guard fails closed: armed by opt-in only; a PRESENT mesh past the grace
        (``_stuck_hold_reset_due``, re-reading off-bus now under the lock); at least one grace window
        since the last reset attempt (so it never stacks on a reset+verify still settling); no reset
        already cycling in its own scope; and no foreign tenant (a scan we
        cannot fully read counts as a tenant, so the reset never lands under a job it merely could not
        see). A recovered mesh retires the fault and clears the hold exactly as the gate does."""
        # force = the hold-deadline backstop firing on a HELD (tenant-refused) box past the ceiling. It
        # bypasses the fail-closed defers that let such a box sit forever — the due check, the retry
        # spacing, and an UNREADABLE holder scan counted as a tenant — because none of them protects a
        # running job (there is none) once the hold outlived the ceiling. It keeps the guards that are
        # still real: a reset already cycling, and a genuine READABLE tenant.
        if not _stuck_hold_reset_enabled() and not force:
            return False
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = offbus_count(beats, expected)
        hold_since = self.deps.device_hold_episode_since()
        # Spec 04 I87: this hold's one mesh reset already ran and failed, so there is nothing to pace or
        # wait out — the escalation climbs (sweep once, then the power cycle). Unforced, it climbs once
        # per episode; a later try is the watchdog's (the I65 cooldown retry, the I76 bound).
        spent = self.mechanism.mesh_reset_spent(hold_since)
        if spent and not force and hold_since and self.mechanism.spent_climb_since == hold_since:
            return False
        if (
            not force
            and not spent
            and not _stuck_hold_reset_due(hold_since, off_bus=off_bus, measured_fault=measured_fault)
        ):
            return False
        # Retry pacing, not a cap: the grace window between attempts replaces the failed-reset
        # cooldown here — that cooldown guards against repeat MMIO at a DEAD endpoint, but this mesh
        # is fully present (live endpoints), so the only thing to pace is the reset+verify cycle
        # itself.
        since_last_reset = time.monotonic() - self.mechanism.last_reset_monotonic
        if (
            not force
            and not spent
            and self.mechanism.last_reset_monotonic
            and since_last_reset < _present_mesh_reset_grace_sec()
        ):
            log(
                f"present-mesh escalation: a reset ran {int(since_last_reset)}s ago — waiting the "
                f"{_present_mesh_reset_grace_sec()}s grace before this hold's first reset"
            )
            metrics.stage_fired("smi_reset", "blocked")
            return False
        if await asyncio.to_thread(self.mechanism.scope_active):
            return False
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        blocker = self._tenant_blocker(scan, force=force)
        if blocker is not None:
            self.tenant_deferred = True
            log(f"hold past the ceiling but {self._describe_blocker(blocker)} — holding; escalates once idle")
            health_event("stuck_hold_deferred", mesh="present", forced=force, **blocker)
            return False
        if measured_fault and not force:
            log(
                "present-mesh hold stands on a measured fabric failure with no tenant — escalating "
                "to the galaxy reset the next gate would run, no grace"
            )
        else:
            log(
                f"present-mesh hold stood past the {_present_mesh_reset_grace_sec()}s grace with no tenant — "
                f"escalating to the galaxy reset the next gate would run"
            )
        health_event(
            "stuck_hold_escalation",
            held_since=hold_since,
            reason=self.deps.device_hold_episode_reason(),
            off_bus=off_bus,
            measured_fault=measured_fault,
        )
        # Latched for visibility (the deadline watchdog journals it); the one-reset rule (I87) is the cap.
        self.deps.set_device_hold_episode_escalated(True)
        # Baseline the off-bus count BEFORE the reset so the climb can tell a reset that regressed the
        # mesh (dropped more chips off the bus) from one that merely did not recover.
        beats_before = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus_before = offbus_count(beats_before, expected)
        if hold_since and self.mechanism.unverified_reset_since == hold_since:
            # This episode's reset already came back UNKNOWN (enum+ARC healthy, the fabric a persistent
            # 77) and the I40 fallback still refused, so it escalated again. Another identical reset
            # measures nothing new — one Galaxy host ran eight of them, ten minutes apart, before the sweep
            # finally ran. Go straight to the last-chance sweep, then the host climb (spec 04 I29).
            await self._sweep_after_unverified_reset(expected, off_bus_before, hold_since, log)
            return True
        if spent:
            self.mechanism.spent_climb_since = hold_since or None
            log("this hold's mesh reset already ran and failed — not repeating it; climbing to the next rung")
            health_event("reset_rung_skipped_spent", rung="mesh_reset", held_since=hold_since, off_bus=off_bus_before)
            await self._climb_or_hand_off(
                expected, off_bus_before, hold_since, log, present_reset_failed=off_bus_before == 0
            )
            return True
        ids_before = await self._offbus_ids_now(expected)
        if force and self.mechanism.rung_failed_unchanged(hold_since, "mesh_reset", ids_before):
            # The deadline watchdog's window: a mesh reset already failed on this same off-bus set this
            # boot. A forced window takes the next stronger rung, never the same reset (spec 04 I36, I58).
            await self._climb_past_failed_rung(expected, ids_before, hold_since, log)
            return True
        recovered = await self._reset_and_verify_device(indices, log)
        await self._note_mesh_reset_failed(recovered, hold_since, ids_before, expected)
        if recovered is None:
            # The fabric is UNKNOWN after the reset (a persistent 77): held fabric-unverified, and its
            # passes already count toward the relift's bound (spec 03 I40), so the next relift gates on
            # the startup checks at once. A refused fallback that escalates again on this episode goes to
            # the sweep, not another reset (spec 04 I29). Nothing measured a failure to climb on.
            return True
        if recovered:
            self.deps.clear_device_reported_fault("idle escalation: galaxy reset recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why="idle escalation: galaxy reset recovered the mesh")
            return True
        # A reset that did not recover must climb, never sit. The failure mode that must not sit is a
        # galaxy reset that dropped this present mesh to all-0xFFFFFFFF — a mass drop a power cycle
        # recovers and self-heal never will. The reset is now spent for the episode (I87), so the climb
        # does not wait for the ceiling: the last-chance sweep once, then the power cycle, or an
        # operator when there is none (I36). Unforced, that climb runs once per episode.
        self.mechanism.spent_climb_since = hold_since or None
        await self._climb_or_hand_off(
            expected, off_bus_before, hold_since, log, present_reset_failed=off_bus_before == 0
        )
        return True

    async def _offbus_gated_power_cycle(self, off_bus: int, expected: int, hold_since: str, log) -> Optional[bool]:
        """The bounded end of an off-bus hold the host reset gate refuses to reset (spec 04 I27).

        Returns None when it does not apply or hands back to the plain hold (the gate allows resets,
        the ladder's safe rungs have not run yet this episode, the command is unset, no
        manual-recovery check is configured, or it is rate-limited — those page first), False while
        it holds (grace, a tenant, a reset in its own scope, a stand-back; past the grace plus the
        blocked-page bound those page once), True once the power cycle was requested. The idle relift and
        the hold watchdog reach it, and the between-jobs gate's below-floor branch does for a whole-tray drop
        (:meth:`escalate`): the device is already held there, the call runs no reset, and a fire hands the
        host to the power cycle, so it adds no time to a job that could have run.

        A gate that allows resets hands back to the ladder. A below-floor drop holding a whole tray (:func:`offbus_power_cycle.note_whole_tray_down`) fires with
        no grace. It still waits for the ladder to have run once, and it runs no reset itself."""
        if off_bus <= 0 or not hold_since:
            return None
        allowed, why = pcie_guard.host_reset_gate(off_bus)
        whole_age = offbus_power_cycle.whole_tray_down_age_sec(hold_since)
        if allowed and whole_age is None:
            return None
        if allowed:
            why = f"a whole tray is off the bus: {offbus_power_cycle.whole_tray_detail(hold_since)}"
            age, grace, handoff = whole_age, 0, "whole_tray"
        else:
            offbus_power_cycle.arm(hold_since)
            age, grace = offbus_power_cycle.episode_age_sec(hold_since) or 0.0, offbus_power_cycle.grace_sec()
            handoff = "gate_refused"
            if offbus_power_cycle.power_cycle_argv() is None:
                # No host rung to end it: hand the hold to an operator with a deadline (spec 04 I33).
                offbus_power_cycle.note_hold_handoff(
                    hold_since, off_bus, expected, why, log, latched="" if _hold_gate_liftable() else why
                )
                return None
        if not offbus_power_cycle.ladder_ran(hold_since):
            return None  # the safe rungs run once first; none is skipped before a power cycle
        argv = offbus_power_cycle.power_cycle_argv()
        if argv is None:
            offbus_power_cycle.page_once(
                "offbus_gated_hold_page",
                log,
                f"{off_bus}/{expected} chip(s) off the bus and no reset can end the hold ({why}); "
                f"TT_DEVICE_MCP_POWER_CYCLE_CMD is not set, so the hold stands until an operator "
                f"power-cycles the host",
                held_since=hold_since,
                off_bus=off_bus,
                expected=expected,
            )
            return None
        if offbus_power_cycle.manual_recovery_argv() is None:
            # No way to tell whether an operator is recovering the host: never read that as "no".
            offbus_power_cycle.page_once(
                "offbus_power_cycle_unguarded",
                log,
                f"{off_bus}/{expected} chip(s) off the bus and every reset is refused ({why}); "
                f"TT_DEVICE_MCP_POWER_CYCLE_CMD is set but no manual-recovery check is "
                f"(TT_DEVICE_MCP_MANUAL_RECOVERY_CMD unset or unparseable and none installed), so the "
                f"power cycle never fires and the hold stands until an operator power-cycles the host",
                held_since=hold_since,
                off_bus=off_bus,
                expected=expected,
            )
            return None
        if age < grace:
            log(f"off-bus hold and no reset can end it ({why}): {int(age)}/{grace}s of grace before the power cycle")
            return False
        if await asyncio.to_thread(self.mechanism.scope_active):
            log("off-bus hold past its grace but a reset is cycling in its own scope — holding")
            offbus_power_cycle.page_if_blocked(
                hold_since, age, log, "a reset is cycling in its own scope", bound=grace, off_bus=off_bus
            )
            return False
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        if self._tenant_active(scan):
            log("off-bus hold past its grace but a tenant holds the device or the scan is unreadable — holding")
            offbus_power_cycle.page_if_blocked(
                hold_since,
                age,
                log,
                "a tenant holds the device or the holder scan is unreadable",
                bound=grace,
                off_bus=off_bus,
            )
            return False
        # The stand-back checks include the operator's own power-cycle tooling (e.g. a "cycle already in
        # progress" check as TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD): never request a second cycle.
        standback = await asyncio.to_thread(offbus_power_cycle.standback_reason)
        if standback:
            log(f"off-bus hold past its grace: standing back from the power cycle — {standback}")
            offbus_power_cycle.note_standback(hold_since, standback)
            offbus_power_cycle.page_if_blocked(hold_since, age, log, standback, bound=grace, off_bus=off_bus)
            return False
        # The stand-back checks can take seconds each: a tenant or a reset that arrived meanwhile
        # still holds the cycle.
        if await asyncio.to_thread(self.mechanism.scope_active) or self._tenant_active(
            await asyncio.to_thread(self.deps.enumerate_device_holders)
        ):
            log("off-bus hold: a tenant or a reset arrived during the stand-back checks — holding")
            return False
        # Re-read the whole box, not just the dropped tray, right before the cycle: an external power
        # event can take other trays off the bus (or the box can come back on its own) between the caller's read
        # and here. A box with nothing off the bus any more needs no cycle.
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = offbus_count(beats, expected)
        if off_bus <= 0:
            log("off-bus hold: every chip is back on the bus — no power cycle")
            health_event("offbus_gated_hold", phase="back_on_bus", held_since=hold_since)
            return None
        offbus_now = _offbus_chip_ids(beats, expected)
        tray_map = self._tray_map_now()
        whole_now = bool(_whole_trays_off(offbus_now, expected, tray_map))
        if (
            handoff == "whole_tray"
            and not whole_now
            and _offbus_ids_on_trays(offbus_now, expected, tray_map)
            and not offbus_power_cycle.whole_tray_deferred(hold_since)
        ):
            # The no-grace cycle is for a whole tray off the bus. One that came back in part since the
            # decline is a partial drop: hold, and the next pass hands it to the ladder (the walk) or to
            # its own bounded grace, never to a grace-0 cycle. A read the tray map cannot place keeps the
            # verdict: a whole-tray drop goes to the power cycle. This deferral runs once per episode: a
            # whole tray seen off again in it (a flapping read) fires on that pass without the re-check.
            fault_class = _offbus_fault_class(off_bus, expected, whole=False)
            log(
                f"off-bus hold: no whole tray is off the bus any more ({off_bus}/{expected} chip(s) off) — "
                f"no power cycle at grace 0; the ladder owns the partial drop"
            )
            offbus_power_cycle.clear_whole_tray_down(
                hold_since, f"{off_bus}/{expected} off the bus, no whole tray", fault_class
            )
            return False
        if handoff == "whole_tray" and not whole_now:
            log(
                f"off-bus hold: a whole tray was seen off again after this episode's one partial re-read "
                f"({off_bus}/{expected} chip(s) off now) — the tray flaps; power cycle at grace 0"
            )
        fault_class = _offbus_fault_class(off_bus, expected, whole=whole_now)
        state = offbus_power_cycle.load_state()
        now = offbus_power_cycle.now_epoch()
        interval = offbus_power_cycle.min_interval_sec()
        last = (
            offbus_power_cycle.last_attempt_within(state, now, interval, healed=self.mechanism.power_cycle_healed)
            if interval > 0
            else None
        )
        if last is not None:
            # Spec 04 I65: an attempt spacing is a stated, bounded wait like the cooldown — on /health,
            # asked again by the watchdog at its end — and the hold runs no reset meanwhile.
            until = last + interval
            _, why = self.mechanism._deny_power_cycle(
                {
                    "until": until,
                    "last": last,
                    "cause": "off-bus power-cycle attempt",
                    "interval_sec": interval,
                    "why": f"off-bus power cycle last attempted {int(now - last)}s ago (spacing {interval}s): "
                    f"held until {datetime.fromtimestamp(until).isoformat(sep=' ', timespec='seconds')}",
                }
            )
            offbus_power_cycle.page_once(
                "offbus_power_cycle_rate_limited",
                log,
                f"{off_bus}/{expected} chip(s) still off the bus {int(age)}s into the hold; {why}",
                held_since=hold_since,
                off_bus=off_bus,
                last_attempt_epoch=last,
                last_error=state.get("last_error", ""),
            )
            return False
        allowed, deny = self.mechanism.auto_recovery_allowed("power-cycle", tenant_active=False)
        if not allowed:
            offbus_power_cycle.page_once(
                "offbus_power_cycle_denied",
                log,
                f"off-bus hold past its grace but the power cycle is denied: {deny} — paging instead",
                held_since=hold_since,
                off_bus=off_bus,
            )
            # A cooldown denial (I65) is a stated wait the watchdog ends: hold, no reset meanwhile.
            # Any other denial hands back to the plain hold as before.
            cooldown = self.mechanism.power_cycle_cooldown_state
            return False if cooldown is not None and cooldown.get("why") == deny else None
        reason = (
            f"{off_bus}/{expected} chip(s) off the bus, {int(age)}s past the last rung, no reset can end it ({why})"
        )
        # On disk before anything runs: a cycle that takes the broker down with it still counts
        # against the rate limit on the boot that comes back.
        try:
            offbus_power_cycle.record_attempt(state, now, hold_since, reason, self.mechanism._current_boot_id() or "")
        except OSError as exc:
            log(f"off-bus power cycle NOT fired: could not record the attempt ({exc!r}); holding")
            health_event("offbus_gated_hold", phase="record_failed", held_since=hold_since, error=repr(exc))
            return False
        health_event(
            "offbus_gated_hold",
            phase="firing",
            held_since=hold_since,
            off_bus=off_bus,
            reason=reason,
            fault_class=fault_class,
            handoff=handoff,
            hold_age_sec=offbus_power_cycle.episode_age_sec(hold_since),
        )
        await self.mechanism._fire_recovery_escalation(
            "power-cycle",
            row_owner="[broker]power-cycle-request",
            label="off-bus power cycle",
            detail="chips off the bus and every reset refused past the grace",
            fire=lambda: offbus_power_cycle.fire_and_record(argv, reason, state),
            log=log,
            reason=reason,
            pre_fire=run_pre_power_cycle_hook,
        )
        return True

    async def _escalate_offbus_stuck_hold(self, indices: list, expected: int, log, *, force: bool = False) -> bool:
        """One escalation pass over an idle off-bus hold (:meth:`_climb_offbus_stuck_hold`). Once a hold a
        configured ``hold`` gate refuses, with no power cycle to end it, has outlived its operator
        hand-off, the pass runs with the gate lifted (spec 04 I33): the ladder's own rungs (the bridge
        reset, then the mesh reset) may fire, each followed by the full health check, fabric included,
        before anything releases the device. An operator latch (the I25 host-hang latch,
        ``TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC=off``) or a manual-recovery check that says an operator
        is acting keeps the hold."""
        lift = await self._offbus_hold_lift(expected, log)
        if not lift:
            return await self._climb_offbus_stuck_hold(indices, expected, log, force=force)
        with pcie_guard.hold_lifted(lift), offbus_power_cycle.partial_pass(self.deps.device_hold_episode_since()):
            return await self._climb_offbus_stuck_hold(indices, expected, log, force=force)

    async def _offbus_hold_lift(self, expected: int, log) -> str:
        """Why this pass may lift the ``hold`` gate, or "" to keep it: with no power cycle configured,
        once the operator hand-off ran out (spec 04 I33); with one configured, once per episode past
        the grace for a partial off-bus drop, so the mesh reset is tried before the power cycle. A
        whole tray down (every off-bus chip's tray fully off, :func:`_whole_tray_drop_trays`) keeps the gate
        shut and goes to the power cycle (I27)."""
        hold_since = self.deps.device_hold_episode_since()
        cycle = offbus_power_cycle.power_cycle_argv() is not None
        if cycle:
            if not offbus_power_cycle.partial_reset_due(hold_since):
                return ""
        elif not offbus_power_cycle.hold_handoff_due(hold_since):
            return ""
        if not _hold_gate_liftable():
            return ""
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = offbus_count(beats, expected)
        if off_bus <= 0:
            return ""
        if (
            cycle
            and _whole_tray_drop_trays(_offbus_chip_ids(beats, expected), expected, self._tray_map_now()) is not None
        ):
            log(
                f"{off_bus}/{expected} chip(s) off the bus fill whole trays — no reset first; straight to the power cycle"
            )
            return ""
        standback = await asyncio.to_thread(offbus_power_cycle.standback_reason, require_manual_check=False)
        if standback:
            log(
                f"off-bus hold past its {'grace' if cycle else 'operator hand-off'}, but {standback} — holding; "
                f"the gate stays shut"
            )
            offbus_power_cycle.page_if_blocked(
                hold_since,
                offbus_power_cycle.episode_age_sec(hold_since) or 0.0,
                log,
                standback,
                bound=offbus_power_cycle.hold_handoff_sec() or 0,
                off_bus=off_bus,
            )
            return ""
        if cycle:
            why = (
                f"a partial off-bus drop outlived its {offbus_power_cycle.grace_sec()}s grace: "
                f"the reset ladder runs once before the power cycle"
            )
        else:
            why = f"the {offbus_power_cycle.hold_handoff_sec()}s operator hand-off of this off-bus hold ran out"
        log(
            f"{off_bus}/{expected} chip(s) off the bus and {why} — lifting TT_DEVICE_MCP_HOST_RESET_GATE=hold "
            f"for this pass: the ladder's own rungs may fire, then the full health check"
        )
        health_event(
            "offbus_gated_hold", phase="gate_lifted", held_since=hold_since, off_bus=off_bus, expected=expected
        )
        return why

    async def _climb_offbus_stuck_hold(self, indices: list, expected: int, log, *, force: bool = False) -> bool:
        """Escalate an idle OFF-BUS hold the read-only relift cannot clear through the gate's OWN
        gentlest-first recovery once it has stood past the ceiling. Returns True iff a recovery action
        ran (recovered or not); False leaves the hold standing for the next window.

        The off-bus twin of :meth:`_escalate_stuck_hold`. A chip that LEFT THE BUS below the reset floor
        self-heals, so the gate holds it and the relift waits — but an idle box has no
        next-job gate to run the cascade if it never self-returns, so read-only the hold outlives every
        ceiling. This moves that cascade to the idle timeout, climbing exactly as the gate does: a chip
        already isolated (reading all-ones) gets the surgical per-chip bridge reset first — it spares the
        other 31 and the fabric survives it (measured) — and a drop the per-chip rung could not clear
        falls to the mesh-wide galaxy reset, BELOW the floor included: the gate suppresses a below-floor
        mesh reset (it can invert the drop all-off-bus), but on this ladder every rung gentler than the
        host pair is tried before either fires — the ladder ends in the cold rung regardless, so the
        inversion risk buys a chance at recovery, not a worse terminal state. The host rungs themselves
        are bounded below by the hold ceiling (see ``_climb_to_host_recovery_after_failed_reset``): no
        reboot or power cycle before the episode has stood the full ladder. A chip GONE from
        sysfs (not merely reading all-ones) never enters isolated_chips, so when the gone-chip bridge
        reset is opted in (``gone_chip_bridge_reset_enabled``) a single/few-chip gone drop below the floor
        is routed to that same surgical rung.

        Every guard fails closed and mirrors the present-mesh sibling: armed by opt-in
        (``_offbus_hold_escalate_enabled``); an OFF-BUS drop past the ceiling
        (``_offbus_stuck_hold_due``, re-reading off-bus now under the lock); one recovery per hold
        episode (``device_hold_episode_escalated``); no reset already cycling; no foreign tenant (an
        unreadable scan counts as one); and outside the failed-reset cooldown. A recovered mesh retires
        the fault and clears the hold exactly as the gate does."""
        # force = the hold-deadline backstop (see _escalate_stuck_hold): on a HELD box past the ceiling it
        # bypasses the one-reset latch, an unreadable-scan tenant, and the failed-reset cooldown — none
        # protect a running job that no longer exists — while keeping a live reset scope and a real
        # readable tenant. This is what turns the gentlest-first ladder below (surgical bridge reset ->
        # mesh reset -> reboot -> power cycle) from "runs once, then holds" into "runs each window
        # until the box recovers or reaches the loud terminal rung." There is no per-tray BMC re-power
        # rung (spec 04 I23).
        #
        # Only the first and last rungs are universal. The surgical rung cannot reach a chip that has
        # LEFT the bus, because the parent bridge it resets through went with it (exit 3, "no bridge to
        # reset"). A dropped ASIC on a non-Galaxy host therefore falls past it to the mesh reset and
        # then, at the ladder's end, to the cold rung — which must stay armed for the ladder to
        # terminate at all.
        if not _offbus_hold_escalate_enabled() and not force:
            return False
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = offbus_count(beats, expected)
        hold_since = self.deps.device_hold_episode_since()
        if not force and not _offbus_stuck_hold_due(hold_since, off_bus=off_bus):
            return False
        # Before the once-per-episode latch: a gated hold must reach its bounded end (I27) on the
        # first pass past its grace, not wait out the latch's re-arm.
        gated = await self._offbus_gated_power_cycle(off_bus, expected, hold_since, log)
        if gated is not None:
            return gated
        if not force and self.deps.hold_escalation_latched():
            log(
                f"off-bus hold past the ceiling but this episode already climbed once — holding for up to "
                f"{HOLD_ESCALATION_REARM_SEC}s, then the guarded ladder may climb again; after a climb "
                f"that ran and failed (DOWN) the forced hold-deadline watchdog is what keeps climbing"
            )
            return False
        if await asyncio.to_thread(self.mechanism.scope_active):
            return False
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        blocker = self._tenant_blocker(scan, force=force)
        if blocker is not None:
            self.tenant_deferred = True
            log(f"off-bus hold past the ceiling but {self._describe_blocker(blocker)} — holding; escalates once idle")
            health_event("stuck_hold_deferred", mesh="offbus", forced=force, **blocker)
            return False
        if not force and self.mechanism.cooling():
            log(
                "off-bus hold past the ceiling but a reset failed within the cooldown — holding; a reset "
                "that did not recover the mesh will not recover it on a back-to-back retry"
            )
            metrics.stage_fired("smi_reset", "blocked")
            return False
        # Forced runs come from the watchdog's own clocks (the off-bus one-shot, the risky floor, a
        # ceiling window) — which one, its log line says; claiming the general ceiling here was wrong
        # two times out of three.
        log(
            (
                f"off-bus hold escalation forced by the hold watchdog ({off_bus}/{expected} off-bus)"
                if force
                else f"off-bus hold stood past the {_stuck_hold_ceiling_sec()}s ceiling ({off_bus}/{expected} off-bus)"
            )
            + " with no tenant — escalating through the gate's gentlest-first recovery"
        )
        health_event(
            "stuck_offbus_hold_escalation",
            held_since=hold_since,
            reason=self.deps.device_hold_episode_reason(),
            off_bus=off_bus,
        )
        # Latch before any action, not after: a recovery that throws or fails must not re-fire next
        # window. This episode's one attempt is spent; the latch clears only when the device comes back
        # fit (the release transition clears the episode clock and this latch together).
        self.deps.set_device_hold_episode_escalated(True)
        offbus_power_cycle.note_ladder_ran(hold_since)
        if off_bus > 0 and self.mechanism.reset_regressed_in(hold_since):
            # This episode's galaxy reset already made the drop WORSE. Re-running the ladder would fire
            # that same mesh reset again (one Galaxy host went 4 -> 31 -> 32 off-bus across three, one
            # per forced window). Go straight to
            # the host climb: it routes the drop to the cold rung, whose last-chance sweep still runs the
            # full reset ladder once before any power cycle (I22), or pages and holds when no power
            # cycle is opted in (spec 04 I31).
            log(
                f"{off_bus}/{expected} off-bus and this hold's galaxy reset already regressed the mesh — "
                f"not repeating it; climbing to the host rung"
            )
            health_event("galaxy_reset_skipped_regressed", held_since=hold_since, off_bus=off_bus, phase="idle")
            await self._climb_to_host_recovery_after_failed_reset(expected, off_bus, log)
            return True
        ids_now = await self._offbus_ids_now(expected) if off_bus > 0 else None
        spent = self.mechanism.mesh_reset_spent(hold_since)
        if spent or self.mechanism.rung_failed_unchanged(hold_since, "mesh_reset", ids_now):
            # This episode already ran the ladder to its mesh reset and that reset failed on exactly the
            # chips off the bus now. Re-running it repeats a known failure (one Galaxy host fired six
            # identical 263 s resets in one hold, one per window): take the next stronger rung, whose
            # last-chance sweep still runs every reset once before a power cycle (I22), or hand the hold
            # to an operator when no host rung is available (spec 04 I36). A reset that failed on another
            # set in this hold is spent all the same: one mesh reset per episode (spec 04 I87).
            # Spec 04 I89: at the 10-min bound a below-floor drop still gets its rescan and SBR first; the
            # spent mesh reset is not repeated (I87), and the climb below takes over if they did not heal.
            floor_now = _galaxy_reset_min_dead_chips(expected)
            if (
                self._bound
                and ids_now
                and floor_now is not None
                and 0 < off_bus < floor_now
                and await self._below_floor_bound_rungs(set(ids_now), expected, log)
            ):
                return True
            await self._climb_past_failed_rung(expected, ids_now, hold_since, log, spent=spent)
            return True
        isolated_chips = self.deps.isolated_chips()
        device_pci_map = self.deps.device_pci_map()
        # A chip GONE from the bus entirely — absent from sysfs, not reading all-ones — never entered
        # isolated_chips, so the surgical rung below skips it and it falls to the galaxy reset, which
        # here is unbounded: with the power cycle opted out a galaxy reset that inverts a below-floor
        # drop has no host rung to climb to. When opted in, route a single/few-chip gone drop to the surgical
        # rung: reset_chip_via_bridge finds its bridge by the secondary bus it still points at even with
        # the endpoint's own node gone. Detection is duplicated from the gate, not shared, so the gate
        # stays byte-identical and its inertness holds unconditionally.
        if gone_chip_bridge_reset_enabled():
            beats_a = await asyncio.to_thread(self.deps.read_heartbeats)
            candidates = (set(device_pci_map) - set(beats_a)) - isolated_chips
            if candidates:
                # A chip absent from one read may be a transient miss on live silicon whose bridge must
                # not be reset; confirm against a second read past a settle. A foreign reset scope that
                # opened during the settle means one is already cycling — skip the SBR (it ends in a PCI
                # rescan) and let the galaxy-reset fallback below adopt that scope.
                await asyncio.sleep(self.deps.gone_chip_confirm_settle_sec())
                if not await asyncio.to_thread(self.mechanism.scope_active):
                    beats_b = await asyncio.to_thread(self.deps.read_heartbeats)
                    # Absent from both reads still is not proof a chip LEFT THE BUS: read_heartbeats omits
                    # a chip whose ARC read merely stalled the same way it omits a gone node, and that
                    # chip's parent bridge still hosts live silicon an SBR must not touch. Route only chips
                    # whose own PCIe node is actually absent.
                    gone = [
                        c
                        for c in sorted(candidates - set(beats_b), key=int)
                        if not await asyncio.to_thread(chip_node_present, device_pci_map[c])
                    ]
                    off_bus_gone = offbus_count(beats_b, expected)
                    floor_gone = _galaxy_reset_min_dead_chips(expected)
                    if gone and floor_gone is not None and off_bus_gone < floor_gone:
                        log(
                            f"chip(s) {gone} left the PCIe bus entirely and are below the galaxy-reset "
                            f"floor ({off_bus_gone}/{expected} off-bus); routing to a per-chip bridge "
                            f"reset before the mesh-wide reset"
                        )
                        health_event(
                            "gone_chip_bridge_reset_queued",
                            chips=gone,
                            off_bus=off_bus_gone,
                            expected=expected,
                            floor=floor_gone,
                        )
                        isolated_chips.update(gone)
        # A chip the sampler already isolated (reading all-ones) is cut from the kernel and its bridge
        # answers: the surgical per-chip reset brings it back without touching the other 31 or the
        # fabric. Try it first; only fall to the mesh-wide reset if it does not recover. But the bridge
        # reset ends in a system-wide PCI rescan, so it must not run while a foreign reset is cycling in
        # its own scope — that is two resets at once, the concurrent-reset hazard this module exists to
        # prevent, and the gone-chip settle above can let a scope open after the top guard. Skip it while
        # a scope is live; the galaxy reset below waits that scope out (self.mechanism.await_foreign_scope)
        # and verifies instead.
        # Spec 04 I89: the chips this rung resets, so the bound's kept rungs below do not SBR them twice.
        sbr_tried: frozenset = frozenset()
        if isolated_chips and not await asyncio.to_thread(self.mechanism.scope_active):
            sbr_tried = frozenset(isolated_chips) | frozenset(removed_chip_ids())
        if sbr_tried and await self._recover_isolated_chips(log):
            healthy, evidence = await self._verify_device(expected, log, run_fabric=True)
            FABRIC_DEBT.note_evidence(evidence)
            if healthy:
                self.deps.clear_device_reported_fault("idle off-bus escalation: bridge reset recovered the mesh")
                self.deps.clear_device_dirty(
                    verified=True, why="idle off-bus escalation: bridge reset recovered the mesh"
                )
                return True
        # Below the galaxy-reset floor, hold rather than fire the mesh-wide reset — the same suppression
        # the gate applies (galaxy_reset_suppressed_below_mass_threshold). A galaxy reset is the measured
        # all-chip drop: run against a single/few-chip off-bus wedge it inverts the mesh (the drop goes
        # all-0xFFFFFFFF), so resetting a below-floor drop can only make it worse. The drop self-heals and the surgical per-chip rung
        # above is the only reset that applies below the floor, so once that cannot recover it, hold: the
        # deadline watchdog keeps the hold visible and self-heal or a cold power cycle clears it, never a
        # reset that deepens the drop. Skip the floor only while a reset already cycles (its all-ones reads
        # like a mass drop — let _reset_and_verify_device adopt and verify that scope) or when an operator
        # disabled the floor entirely (reset_floor None -> the old reset-any-unhealthy behaviour).
        reset_floor = _galaxy_reset_min_dead_chips(expected)
        off_bus_before = None
        if reset_floor is not None and not await asyncio.to_thread(self.mechanism.scope_active):
            beats_pre = await asyncio.to_thread(self.deps.read_heartbeats)
            off_bus_pre = offbus_count(beats_pre, expected)
            if off_bus_pre < reset_floor:
                # A below-floor drop holding a whole UBB tray goes to the bounded power cycle with no
                # grace (I37): no reset brings such a tray back, and no tray re-power exists (I23).
                if self._note_whole_tray_drop(beats_pre, off_bus_pre, expected, log):
                    hold_since = self.deps.device_hold_episode_since()
                    log(
                        f"{off_bus_pre} of {expected} chip(s) off the bus with a whole tray down — below "
                        f"the galaxy-reset floor ({reset_floor}); no mesh-wide reset, handing the hold to "
                        f"the bounded power cycle"
                    )
                    health_event(
                        "galaxy_reset_skipped_whole_tray",
                        held_since=hold_since,
                        off_bus=off_bus_pre,
                        expected=expected,
                        floor=reset_floor,
                        fault_class="whole_tray",
                        hold_age_sec=offbus_power_cycle.episode_age_sec(hold_since),
                    )
                    metrics.stage_fired("smi_reset", "blocked")
                    gated = await self._offbus_gated_power_cycle(off_bus_pre, expected, hold_since, log)
                    if gated is None:
                        await self._climb_to_host_recovery_after_failed_reset(expected, off_bus_pre, log, swept=True)
                    return True
                # Not a whole-tray drop. The gate keeps the mesh
                # reset suppressed below the floor (it can invert a below-floor drop all-off-bus),
                # but on the stuck-hold ladder every rung gentler than the host pair gets its try
                # before either fires: the ladder ends in the cold rung regardless, so the reset's
                # inversion risk buys a chance at recovery, not a worse terminal state. An inverted
                # mesh reads as a mass drop on the next forced window and climbs this same ladder.
                # The old risky-reset floor deferred this attempt for the first 10 min of the hold;
                # it is gone (ladder-v2). Nothing is gained by waiting — a reset brings a recoverable
                # drop back at once — so the mesh reset is tried now, before any host rung.
                log(
                    f"{off_bus_pre} of {expected} chip(s) off the bus — below the galaxy-reset floor "
                    f"({reset_floor}) and the per-chip rung could not recover it; trying the "
                    f"mesh-wide reset before any host rung"
                    + (" (I76 bound: PCI rescan and SBR of the bridged chips first)" if self._bound else "")
                )
                health_event(
                    "stuck_hold_galaxy_reset_below_floor_attempt",
                    off_bus=off_bus_pre,
                    expected=expected,
                    floor=reset_floor,
                    bound=self._bound,
                )
                if self._bound and await self._below_floor_bound_rungs(
                    _offbus_chip_ids(beats_pre or {}, expected), expected, log, sbr_tried=sbr_tried
                ):
                    return True
            off_bus_before = off_bus_pre
        if off_bus_before is None:
            # The floor read above did not run (floor opted out, or a reset was cycling), so take a
            # pre-reset baseline here — the climb needs it to catch a reset that regresses the mesh.
            beats_before = await asyncio.to_thread(self.deps.read_heartbeats)
            off_bus_before = offbus_count(beats_before, expected)
        ids_before = await self._offbus_ids_now(expected)
        recovered = await self._reset_and_verify_device(indices, log)
        await self._note_mesh_reset_failed(recovered, hold_since, ids_before, expected)
        if recovered is None:
            # The fabric is UNKNOWN after the reset (a persistent 77): held fabric-unverified, which the
            # idle relift re-checks within its bound (spec 03 I40). Nothing measured a failure to climb on.
            return True
        if recovered:
            self.deps.clear_device_reported_fault("idle off-bus escalation: galaxy reset recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why="idle off-bus escalation: galaxy reset recovered the mesh")
            return True
        # A reset that did not recover must climb, never sit: a galaxy reset that dropped this drop to an
        # all-off-bus mesh is the mass drop a host reboot/power cycle recovers and self-heal never will.
        # This episode's one reset is spent and an idle box has no next-job gate to run another, so hand
        # the mesh straight to the host rung, or to an operator when there is none (I36).
        await self._climb_or_hand_off(expected, off_bus_before, hold_since, log)
        return True

    async def _below_floor_bound_rungs(
        self, offbus_chips: set, expected: int, log, sbr_tried: frozenset = frozenset()
    ) -> bool:
        """The I76 bound's kept rungs ahead of its one mesh reset (spec 04 I89): PCI rescan, then SBR of
        the bridged off-bus chips (a gone chip only with the opt-in: see _below_floor_kept_rungs). True only when every chip is back and the full
        check with fabric passed: the caller then skips the mesh reset. Otherwise the ladder goes on: the
        mesh reset when this hold has not spent it, else the climb's last-chance sweep (I87), then the
        host rung."""
        still_off = await self._below_floor_kept_rungs(
            offbus_chips, expected, log, context="I76 bound ladder", at_bound=True, sbr_tried=sbr_tried
        )
        if still_off is None or still_off:
            return False
        healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase="idle-escalation")
        FABRIC_DEBT.note_evidence(evidence)
        if not healthy:
            log("every chip is back after the bound's rescan/SBR but the full check failed — the ladder goes on")
            health_event("below_floor_kept_rungs_unverified", phase="idle-escalation", evidence=evidence)
            return False
        why = "I76 bound: below-floor rescan/SBR recovered the mesh"
        self.deps.clear_device_reported_fault(why)
        self.deps.clear_device_dirty(verified=True, why=why)
        return True

    async def _note_mesh_reset_failed(
        self, recovered: Optional[bool], hold_since: str, ids_before: Optional[set], expected: int
    ) -> None:
        """Record a mesh reset that RAN and failed in this boot's record, on disk before any next rung
        (spec 04 I36, I58). Only a launched reset counts (``last_reset_rc`` set): one the host gate or an
        outage hold refused, or one still cycling at its timeout, did not fail here. The reset clock
        cannot tell, since it is stamped before launch."""
        rc = self.mechanism.last_reset_rc
        if recovered is False and rc is not None:
            ids_after = await self._offbus_ids_now(expected)
            self.mechanism.note_rung_failed(hold_since, "mesh_reset", ids_before, ids_after, rc=rc)

    async def _climb_past_failed_rung(
        self, expected: int, ids_now: Optional[set], hold_since: str, log, *, spent: bool = False
    ) -> None:
        """Skip a reset that already failed in this hold episode — on the same off-bus set (spec 04 I36)
        or at all (``spent``, I87) — and take the next stronger rung."""
        off_bus = len(ids_now or ())
        if spent:
            log(
                f"{off_bus}/{expected} off-bus and this hold's mesh reset already ran and failed — "
                f"not repeating it; climbing to the next rung"
            )
            health_event("reset_rung_skipped_spent", rung="mesh_reset", held_since=hold_since, off_bus=off_bus)
        else:
            log(
                f"{off_bus}/{expected} off-bus and this hold's mesh reset already failed on exactly these chips — "
                f"not repeating it; climbing to the next rung"
            )
            health_event(
                "reset_rung_skipped_failed_unchanged", rung="mesh_reset", held_since=hold_since, off_bus=off_bus
            )
        await self._climb_or_hand_off(expected, off_bus, hold_since, log, present_reset_failed=off_bus == 0)

    async def _climb_or_hand_off(
        self, expected: int, off_bus: Optional[int], hold_since: str, log, *, present_reset_failed: bool = False
    ) -> None:
        """Climb a failed (or skipped) reset to the host rung. With no host rung available the hold goes
        to an operator, once per episode, and no further reset fires on it while its off-bus set stays
        the same (spec 04 I36, I33)."""
        if (
            await self._climb_to_host_recovery_after_failed_reset(
                expected, off_bus, log, present_reset_failed=present_reset_failed
            )
            != NO_HOST_RUNG
        ):
            return
        offbus_power_cycle.note_hold_handoff(
            hold_since,
            off_bus or 0,
            expected,
            "the mesh reset failed and no host rung is available",
            log,
            latched="no host power cycle enabled",
            cause="the broker has no power cycle to climb to",
        )

    async def _sweep_after_unverified_reset(self, expected: int, off_bus_before: int, hold_since: str, log) -> None:
        """The next rung for a present-mesh episode whose reset came back UNKNOWN and whose I40 fallback
        refused: the last-chance sweep once per episode, then the host climb (spec 04 I29).

        The sweep runs here, not behind the host-rung permission: the climb asks the host-action ledger
        first, and on one Galaxy host its boot-loop guard kept the sweep waiting an hour. A sweep whose verify
        is still a 77 is not a recovery (the fallback that refused it measured more than a 77 can). With
        a known no-reset cause (a cable fault, a topology mismatch, hugepages not yet allocated, the
        validator missing) it holds and pages once; the idle relift keeps re-running the fabric pass on
        its backoff (spec 03 I48). Otherwise the one rung left that rebuilds what a 77 lacks is the host
        rung (a power cycle refills the 1G hugepages, reloads the driver and retrains every link): it climbs
        there once per episode, through every guard of the climb. A 77 that rung did not end is handed
        to an operator with a deadline, still held."""
        if self.mechanism.unverified_sweep_since != hold_since:
            self.mechanism.unverified_sweep_since = hold_since
            self._sweep_read_77 = False
            health_event("unverified_reset_sweep", held_since=hold_since)
            log(
                "this hold's reset already left the fabric unverified and the startup checks refused it — "
                "running the last-chance sweep now instead of another identical reset"
            )
            swept = await self._settle_and_verify_before_host_rung(
                expected,
                log,
                "idle-escalation",
                context="idle escalation after an unverified reset",
                fabric_77_unverified=True,
            )
            if swept is not False:
                # True: recovered and released. None: the sweep could not run in full — the next
                # escalation of this episode climbs; the climb's own sweep covers what this one missed.
                if swept is None:
                    self.mechanism.unverified_sweep_since = None
                return
            if self._sweep_read_77:
                self.mechanism.unverified_sweep_77_since = hold_since
        if self.mechanism.unverified_sweep_77_since == hold_since:
            no_reset = self.deps.fabric_77_no_reset_cause
            if no_reset is not None and no_reset():
                self._hold_77_without_host_rung(hold_since, expected, log)
                return
            spent = self._fabric_77_host_rung_spent(hold_since)
            if not spent:

                def on_fire() -> None:
                    # Logged as the rung fires, before the power cycle can take the broker down; a stale
                    # hand-off of an earlier "could not run" ends here.
                    self._fabric_77_handoff = None
                    health_event("fabric_unverified_host_rung", held_since=hold_since, expected=expected)
                    log(
                        "the reset and the last-chance sweep both left the fabric unverified (77) with no "
                        "known no-reset cause — climbing once to the host rung, which refills the hugepages, "
                        "reloads the driver and retrains the links"
                    )

                outcome = await self._climb_to_host_recovery_after_failed_reset(
                    expected,
                    off_bus_before,
                    log,
                    swept=True,
                    cause=FABRIC_77_HOST_RUNG_CAUSE,
                    on_fire=on_fire,
                    ladder_ran=True,
                )
                if outcome == HOST_RUNG_FIRED:
                    self.mechanism.unverified_77_host_rung_since = hold_since
                    return
                if outcome not in HOST_RUNG_CANNOT_RUN:
                    # Only waiting (the ladder ceiling, a reset in its scope): no page, the next
                    # escalation of the episode retries the climb.
                    return
                if outcome == HOST_RUNG_DENIED and self._wait_77_cold_rung_cooldown(hold_since, log):
                    return
                if outcome == HOST_RUNG_DENIED:
                    # Held off for now (the rate limit, a tenant): the rung is not spent, so every
                    # later re-check of the episode climbs again and fires it once allowed (spec 04
                    # I79). The page says so; an operator acts only if it never can.
                    spent = (
                        f"the host rung could not run yet ({self._host_rung_denied_why}); the broker retries "
                        "it on every re-check and fires it once allowed"
                    )
                    self._hand_77_hold_to_operator(hold_since, expected, spent, log, broker_retries=True)
                    return
                spent = "the host rung could not run (not opted in)"
            self._hand_77_hold_to_operator(hold_since, expected, spent, log)
            return
        await self._climb_to_host_recovery_after_failed_reset(
            expected, off_bus_before, log, swept=True, ladder_ran=True
        )

    def _wait_77_cold_rung_cooldown(self, hold_since: str, log) -> bool:
        """True when the 77 climb's power cycle was denied only by the bounded I65 cooldown (or its cap
        or backstop, each with an end): no page and no hand-off, since the broker fires the cycle
        itself at the first re-check after ``until`` (spec 04 I79). /health shows the deadline as
        ``power_cycle_cooldown.next_deadline``. Journaled once per episode and deadline."""
        cd = self.mechanism.power_cycle_cooldown_state
        if not cd or cd.get("why") != self._host_rung_denied_why or not cd.get("until"):
            return False
        until = float(cd["until"])
        if until <= time.time():
            return False
        until_iso = datetime.fromtimestamp(until).isoformat(timespec="seconds")
        if self._fabric_77_cooldown_logged != (hold_since, until):
            self._fabric_77_cooldown_logged = (hold_since, until)
            health_event(
                "fabric_unverified_host_rung_cooldown",
                held_since=hold_since,
                until=until_iso,
                until_epoch=until,
                cause=cd.get("cause"),
            )
        log(
            f"fabric still unverified (77); its power cycle waits for the cooldown ({cd.get('cause')}) "
            f"until {until_iso} — held, no page: the broker fires it on the first re-check after that"
        )
        return True

    def _fabric_77_host_rung_spent(self, hold_since: str) -> str:
        """Why this 77 episode's one host rung is spent, or "" (spec 04 I29): it already fired on this
        episode, or this boot is that rung's result and no fabric pass has passed since the broker
        started (the power cycle did not end the 77). A pass after the boot frees the rung for a later
        episode; the host-action ledger's rate limit still bounds it."""
        if self.mechanism.unverified_77_host_rung_since == hold_since:
            return "the host rung already ran on this hold"
        if FABRIC_DEBT.passed_since_start:
            return ""
        rec = self.mechanism.boot_from_broker_escalation(self.deps.current_boot_id())
        if rec and str(rec.get("reason", "")) == FABRIC_77_HOST_RUNG_CAUSE:
            return f"this boot is the {rec.get('action')} that rung fired, and the fabric still reads 77"
        return ""

    def fabric_77_climb_owed(self, hold_since: str) -> bool:
        """Whether a 77-only hold owes the climb at once rather than the I40/I48 waits (spec 04 I79):
        its one reset already came back UNKNOWN on this episode. False once this episode's host rung has
        fired. Counts nothing; the caller decides."""
        if not hold_since or self.mechanism.unverified_77_host_rung_since == hold_since:
            return False
        return self.mechanism.unverified_reset_since == hold_since

    def _hand_77_hold_to_operator(
        self, hold_since: str, expected: int, why: str, log, *, broker_retries: bool = False
    ) -> None:
        """A 77 the host rung did not end: keep the device held and page an operator once per episode
        with a deadline (spec 04 I29). The idle relift keeps re-running the full check, fabric
        included, on its backoff (spec 03 I48); only a passing one releases it. ``broker_retries``:
        the rung was only held off (a cooldown, the rate limit, a tenant) and the broker fires it
        itself once allowed (I79), so the page and /health name the broker as the owner meanwhile."""
        h = self._fabric_77_handoff
        if h and h["episode"] == hold_since:
            h["why"], h["broker_retries"] = why, broker_retries
            log(f"fabric still unverified (77) and {why} — held, handed to an operator")
            return
        deadline = time.time() + FABRIC_77_HANDOFF_SEC
        act_by = datetime.fromtimestamp(deadline).isoformat(timespec="seconds")
        self._fabric_77_handoff = {
            "episode": hold_since,
            "deadline": deadline,
            "why": why,
            "broker_retries": broker_retries,
        }
        event = {
            "kind": "fabric_unverified_operator_handoff",
            "held_since": hold_since,
            "reason": self.deps.device_hold_episode_reason(),
            "why": why,
            "expected": expected,
            "act_by": act_by,
            "deadline_epoch": deadline,
            "broker_retries": broker_retries,
        }
        health_event(event["kind"], **{k: v for k, v in event.items() if k != "kind"})
        log(
            f"the reset, the last-chance sweep and the host rung left the fabric unverified (77) — {why}. "
            + ("If it never can, an operator" if broker_retries else "An operator")
            + f" must check the host (cables, hugepages, the fabric validator) by {act_by}; the "
            f"device stays held and the broker re-runs the full health check, fabric included, on its "
            f"backoff, releasing only on a pass"
        )
        alert.send_alert(event, _logger)

    def fabric_77_handoff_status(self, episode: str) -> Optional[dict]:
        """The /health ``operator_handoff`` field while this episode's 77 is handed off, or None."""
        h = self._fabric_77_handoff
        if not h or not episode or h["episode"] != episode:
            return None
        retries = bool(h.get("broker_retries"))
        return {
            "reason": f"fabric unverified (77): {h['why']}",
            "owner": "broker" if retries else "operator",
            "act_by": datetime.fromtimestamp(h["deadline"]).isoformat(timespec="seconds"),
            "act_by_epoch": h["deadline"],
            "then": (
                "held; the broker retries the host rung on every re-check and fires it once allowed, "
                if retries
                else "held; "
            )
            + "the broker re-runs the full health check, fabric included, and releases only on a pass",
        }

    def _hold_77_without_host_rung(self, hold_since: str, expected: int, log) -> None:
        """Every lower rung ran on a 77-only episode (one reset, the sweep) and the fabric pass still could
        not run for a cause no reboot fixes (a cable fault, a topology mismatch, hugepages not yet
        allocated, the validator missing): hold without a reboot or power cycle and page once per
        episode (spec 04 I29)."""
        if self._host_rung_77_paged_since == hold_since:
            log("fabric still unverified (77) after the reset and the sweep, for a known no-reset cause — holding")
            return
        self._host_rung_77_paged_since = hold_since
        event = {
            "kind": "fabric_unverified_no_host_rung",
            "held_since": hold_since,
            "reason": self.deps.device_hold_episode_reason(),
            "expected": expected,
        }
        health_event(event["kind"], **{k: v for k, v in event.items() if k != "kind"})
        log(
            "the reset and the last-chance sweep both left the fabric unverified (77) for a known no-reset "
            "cause — no power cycle; holding, paging once, the idle relift keeps re-checking"
        )
        alert.send_alert(event, _logger)

    async def _climb_to_host_recovery_after_failed_reset(
        self,
        expected: int,
        off_bus_before: Optional[int],
        log,
        *,
        swept: bool = False,
        cause: str = "",
        on_fire: Optional[Callable[[], None]] = None,
        ladder_ran: bool = False,
        present_reset_failed: bool = False,
    ) -> str:
        """Climb an idle escalation whose galaxy reset failed to revive the mesh to the host rung — the
        power cycle, the only host rung (I71) — so a reset that did not recover does not sit.
        Bounded below by the hold ceiling: the power cycle never fires before the episode has stood the
        full ladder — an early forced pass fires the cheap rungs only, and the deadline watchdog runs
        the ladder at the ceiling where the climb is then admitted. The exceptions are holds with no
        gentler rung left to try (spec 04 I38, I79), which includes a hold whose one mesh reset already
        ran and failed (I87).

        Fires ONLY for a mass drop. The galaxy reset can drop a present-but-eth-unverifiable mesh to
        all-0xFFFFFFFF, which a reboot/power cycle recovers and self-heal never does; a still-present or
        few-chip mesh stays below the reboot floor and keeps holding, exactly as the gate refuses to
        reboot one for a self-healing wedge. Shares the gate's own guards — the reboot floor, the
        reset-scope check (a reset left cycling reads all-ones like a mass drop, so ask the scope, not
        the count), the tenant scan (an unreadable /proc counts as a tenant), and
        self.mechanism.auto_recovery_allowed's fail-closed rate-limit ledger.

        It deliberately does NOT carry the gate's two-strikes was_failing precondition (a PRIOR failed
        cycle on top of the current one). The idle escalation fires exactly one reset per episode (the
        once-per-episode latch), and an idle stuck box has no next-job gate to run a second, so requiring
        a second failure would hold forever — the indefinite hold this path exists to end. One failed
        reset that mass-dropped an idle, tenant-free mesh is enough to power-cycle here; the rate limiter bars
        a loop. A no-op where the host opted into no host rung — except an all-off-bus drop, which always
        emits the loud actionable event before holding, since absence is never health.

        ``off_bus_before`` is the caller's pre-reset baseline. A reset that REGRESSED the mesh (more chips
        off the bus than before) or hard-exited leaving chips off it is latched for the episode and goes
        to the power cycle — the same verdict the gate reaches. If the cycle cannot launch the hold
        stays, the broker pages, and the next pass asks for the cycle again.

        ``cause`` replaces the recorded reason (the I29 77 climb names itself, so the next boot can tell
        it apart); ``on_fire`` runs just before the rung fires. Returns HOST_RUNG_FIRED once it requested the
        power cycle, HOST_RUNG_DEFERRED when it only waited (a later escalation retries),
        NO_HOST_RUNG when no rung is available, or HOST_RUNG_DENIED when auto_recovery_allowed refused it."""
        if await asyncio.to_thread(self.mechanism.scope_active):
            log(
                "reset did not recover but one is still cycling in its own scope — not escalating over "
                "its in-flight all-ones; the next gate adopts and verifies it"
            )
            return HOST_RUNG_DEFERRED
        # The host rungs are bounded BELOW by the hold ceiling: however futile the gentler rungs
        # look, a reboot or power cycle never fires before the episode has stood the full ladder.
        # The early off-bus pass (_offbus_hold_ceiling_sec) exists to fire the CHEAP rungs early,
        # and the deadline watchdog re-forces the ladder each ceiling window, so a climb deferred
        # here is re-attempted at the ladder's end, never lost. A missing episode clock cannot
        # defer — the watchdog keys on that same clock, so deferring without one would strand the
        # hold with nothing left to re-attempt it.
        hold_since = self.deps.device_hold_episode_since()
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = offbus_count(beats, expected)
        # A reset that made the mesh WORSE off the bus (the incident's 8->32), or that hard-exited
        # leaving chips off it, goes to the power cycle; flag a regression loudly. No reset is cycling here (the top guard
        # already returned on a live scope), so the count is a confirmed post-reset state. This mirrors
        # the gate's own escalation so a regression climbs the same on the idle path as at a job boundary.
        # Detected BEFORE the ceiling deferral below so an early regression is latched for the episode:
        # no later pass of it fires the mesh reset again (spec 04 I31).
        reset_regressed = expected > 1 and off_bus_before is not None and off_bus > off_bus_before
        if reset_regressed:
            self.mechanism.note_reset_regressed(hold_since)
            health_event(
                "reset_regressed_offbus",
                off_bus_before=off_bus_before,
                off_bus_after=off_bus,
                expected=expected,
                host_at_risk=True,
            )
            log(
                f"the idle escalation's reset REGRESSED the mesh — {off_bus_before} -> {off_bus} of "
                f"{expected} chip(s) off the bus. Climbing to the power cycle; no further mesh reset this hold."
            )
        episode_regressed = self.mechanism.reset_regressed_in(hold_since)
        # This episode's mesh reset already ran and failed on exactly the chips off the bus now (I36).
        # Every rung below the host pair has had its one try and the mesh reset may not run again on
        # this set, so neither the ceiling wait nor the last-chance sweep (whose last step is that same
        # mesh reset) has anything left to try: the host rung fires now (spec 04 I38). A second mesh
        # reset after an rc 1 on the same chips killed one Galaxy host.
        failed_unchanged = off_bus > 0 and self.mechanism.rung_failed_unchanged(
            hold_since, "mesh_reset", await self._offbus_ids_now(expected)
        )
        hold_age = _hold_age_sec(hold_since)
        reset_spent = self.mechanism.mesh_reset_spent(hold_since)
        if failed_unchanged and hold_age is not None and hold_age < _stuck_hold_ceiling_sec():
            log(
                f"{off_bus}/{expected} off-bus and this hold's mesh reset already failed on exactly these "
                f"chips — every rung below the host pair has run once; not waiting for the "
                f"{_stuck_hold_ceiling_sec()}s ladder end"
            )
            health_event(
                "host_rung_not_deferred",
                held_since=hold_since,
                held_age_sec=int(hold_age),
                ceiling_sec=_stuck_hold_ceiling_sec(),
                reason="mesh_reset_failed_unchanged",
            )
        elif (
            (ladder_ran or (present_reset_failed and off_bus == 0))
            and hold_age is not None
            and hold_age < _stuck_hold_ceiling_sec()
        ):
            # This hold already ran its reset and failed the full check after it on a present mesh (an
            # eth/fabric wedge), or ran the reset and the full last-chance sweep and still failed: the
            # ceiling wait has no rung left to try. A present mesh still runs the sweep below before the
            # host rung; one already swept climbs now (spec 04 I79).
            log(
                f"this {int(hold_age)}s-old hold's reset "
                + ("and the full last-chance sweep both failed" if ladder_ran else "failed its check on a present mesh")
                + f" — not waiting for the {_stuck_hold_ceiling_sec()}s ladder end"
            )
            health_event(
                "host_rung_not_deferred",
                held_since=hold_since,
                held_age_sec=int(hold_age),
                ceiling_sec=_stuck_hold_ceiling_sec(),
                reason="reset_and_sweep_failed" if ladder_ran else "present_mesh_reset_failed",
            )
        elif (
            off_bus > 0
            and (reset_regressed or episode_regressed)
            and hold_age is not None
            and hold_age < _stuck_hold_ceiling_sec()
        ):
            # This hold's reset made the drop worse. I31 bars every reset rung but the sweep for the rest
            # of the episode, so the ceiling wait has nothing to try: one 8-chip host's hold sat 512 s in
            # it, then swept and power-cycled anyway. Sweep now, then the cold rung (spec 04 I79).
            log(
                f"this {int(hold_age)}s-old hold's reset regressed the mesh ({off_bus}/{expected} off the bus) — "
                f"no reset rung may repeat it; not waiting for the {_stuck_hold_ceiling_sec()}s ladder end"
            )
            health_event(
                "host_rung_not_deferred",
                held_since=hold_since,
                held_age_sec=int(hold_age),
                ceiling_sec=_stuck_hold_ceiling_sec(),
                reason="reset_regressed_offbus",
            )
        elif reset_spent and hold_age is not None and hold_age < _stuck_hold_ceiling_sec():
            # This hold's one mesh reset already ran and failed (spec 04 I87). Waiting for the ladder end
            # used to mean more mesh resets on the ceiling windows; none may run, so the ceiling wait has
            # nothing left to try. The last-chance sweep below still runs once before the power cycle.
            log(
                f"this {int(hold_age)}s-old hold's mesh reset already ran and failed — not waiting for the "
                f"{_stuck_hold_ceiling_sec()}s ladder end"
            )
            health_event(
                "host_rung_not_deferred",
                held_since=hold_since,
                held_age_sec=int(hold_age),
                ceiling_sec=_stuck_hold_ceiling_sec(),
                reason="mesh_reset_spent",
            )
        elif hold_age is not None and hold_age < _stuck_hold_ceiling_sec():
            log(
                f"mesh unrecovered after the reset ladder but the hold is only {int(hold_age)}s "
                f"old — the power cycle waits for the "
                f"{_stuck_hold_ceiling_sec()}s ladder end; holding for the next forced window"
            )
            health_event(
                "host_rung_deferred_until_ladder_end",
                held_since=hold_since,
                held_age_sec=int(hold_age),
                ceiling_sec=_stuck_hold_ceiling_sec(),
            )
            return HOST_RUNG_DEFERRED
        # An off-bus drop reaching this climb is NOT self-healing: it survived the grace and a reset that
        # could not recover it, and a present mesh survived the deepest reset. The power cycle is the one
        # host rung for both (I71); with it not armed the climb holds loudly.
        escalation, reboot_blocked = _host_escalation_for_drop(off_bus, expected, **self.deps.host_escalation_kwargs())
        if reboot_blocked:
            # No chassis power cycle is opted in and no rung can recover it: fail closed loudly and hold.
            # A whole-bus drop and a reset
            # that regressed/hard-failed a partial drop off the bus each need the cold rung, but name
            # them apart so the count is not misreported as all-off-bus.
            if off_bus >= expected:
                self.deps.emit_all_off_bus_power_cycle_required(log, off_bus, expected, "idle escalation")
            else:
                self._emit_reset_unrecoverable_power_cycle_required(
                    log, off_bus, expected, regressed=reset_regressed or episode_regressed, context="idle escalation"
                )
            metrics.stage_fired("power_cycle", "blocked")
            return NO_HOST_RUNG
        if off_bus == 0 and not cause:
            # A PRESENT mesh (every chip on the bus, ARC-healthy) whose galaxy reset STILL did not verify
            # is an eth/fabric wedge the reset could not clear — and self-heal will not either, since it
            # already survived the deepest reset. The old code held it here (below the mass floor), which
            # is an INDEFINITE hold: measured on box D, hours of ceiling -> galaxy-reset -> hold with the
            # eth core wedged the whole time, the ledger never firing a rung above the reset. "Survived a
            # galaxy reset" is the proof this is a reboot/power-cycle-class wedge, not a self-healing one —
            # so climb, do not sit. (A transient post-reset training window reports 77/skip, which reads as
            # recovered upstream and never reaches here; reaching here means the fabric measured BAD.)
            log(
                "galaxy reset ran and the present mesh still did not verify — an eth/fabric wedge the "
                "reset could not clear and self-heal will not (it survived the deepest reset); climbing "
                "to the host rung rather than holding indefinitely"
            )
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        tenant_active = self._tenant_active(scan)
        allowed, why = self.mechanism.auto_recovery_allowed(escalation, tenant_active=tenant_active)
        if not allowed:
            log(
                f"reset dropped the mesh ({off_bus}/{expected} off-bus) and a host {escalation} is "
                f"opted in but held off: {why}"
            )
            self.mechanism._journal_auto_recovery_denied(escalation, why)
            metrics.stage_fired(_HOST_ESCALATION_STAGE.get(escalation, escalation), "blocked")
            self._host_rung_denied_why = why
            return HOST_RUNG_DENIED
        # The recorded cause names what is actually true at this point — the old fixed string
        # blamed a galaxy reset even on paths where the reset was suppressed and never ran.
        cause = cause or (
            "idle escalation: present mesh failed the post-reset verify"
            if off_bus == 0
            else f"idle escalation: {off_bus}/{expected} chip(s) off the bus after the reset ladder"
        )
        # Last chance: every reset type once more, back-to-back, then one settle and one verify. This
        # is the road MOST real power cycles took (the idle/stuck-hold escalation), and before ladder-v2
        # it climbed to the host rung without ever re-trying the cheaper rungs. Skipped when the caller
        # already swept this episode (_sweep_after_unverified_reset).
        if failed_unchanged and not swept:
            log("last-chance sweep skipped: its mesh reset already failed on these chips in this hold")
            health_event(
                "reset_rung_skipped_failed_unchanged", rung="last_chance_sweep", held_since=hold_since, off_bus=off_bus
            )
        elif not swept and (
            await self._settle_and_verify_before_host_rung(
                expected, log, "idle-escalation", context=f"idle escalation, {off_bus}/{expected} off the bus"
            )
            is not False
        ):
            return HOST_RUNG_DEFERRED
        if on_fire is not None:
            on_fire()
        health_event("stuck_hold_host_escalation", escalation=escalation, off_bus=off_bus)
        if await self.deps.auto_power_cycle_host(log, cause) is False:
            self._hold_after_cycle_launch_failure(log, cause)
        return HOST_RUNG_FIRED

    def _emit_reset_unrecoverable_power_cycle_required(
        self, log, off_bus: int, expected: int, *, regressed: bool, context: str
    ) -> None:
        """Fail closed, loudly, on a PARTIAL drop no reset recovers: a reset that regressed the mesh off
        the bus, or hard-exited leaving chips off it, with no auto power cycle opted in. Distinct from
        the whole-bus event so the count is not misreported as all-off-bus. The only rung that can
        recover the dropped ASICs is the power cycle. Name it and leave the device HELD."""
        cause = "regressed the mesh off the bus" if regressed else "could not run and left chips off the bus"
        health_event(
            "reset_unrecoverable_power_cycle_required",
            off_bus=off_bus,
            expected=expected,
            present=max(0, expected - off_bus),
            regressed=regressed,
            auto_power_cycle_enabled=self.deps.auto_power_cycle_enabled(),
            host_at_risk=True,
        )
        log(
            f"a reset {cause} — {off_bus}/{expected} chip(s) off the PCIe bus [{context}]. Only a power "
            f"cycle brings them back: set TT_DEVICE_MCP_AUTO_POWER_CYCLE=1 to let the broker cycle the "
            f"chassis, or power-cycle it manually (BMC/ipmitool). Device stays HELD."
        )

    def _note_whole_tray_drop(self, beats: dict, off_bus: int, expected: int, log) -> bool:
        """Whether a below-floor drop holds a WHOLE UBB tray; if so, start this hold episode's whole-tray
        clock (:func:`offbus_power_cycle.note_whole_tray_down`), which hands the hold to the bounded power
        cycle with no grace (spec 04 I37). Fires nothing: no reset brings a whole-down tray back, and
        there is no BMC tray re-power (I23). Without an I16 map, tt-smi's UBB bus groups are read
        instead. A fully-off-bus mesh is not this class: its own all-off-bus handling owns it."""
        if expected > 0 and off_bus >= expected:
            return False
        offbus_ids = _offbus_chip_ids(beats, expected)
        tray_map = self._tray_map_now()
        reset_floor = _galaxy_reset_min_dead_chips(expected)
        if not (reset_floor is None or off_bus < reset_floor):
            return False
        whole: list = _whole_trays_off(offbus_ids, expected, tray_map)
        if tray_map is None:
            present_buses, groups = self._presence_read(offbus_ids, expected)
            absent_groups = _absent_tray_groups(present_buses, expected, offbus_ids, groups)
            unreadable = set() if absent_groups else unreadable_on_bus_chips(offbus_ids) & set(offbus_ids)
            if unreadable:
                # Enumerated, bound chips whose heartbeat read fails are on the bus (see
                # _power_cycle_direct_class_now): read the drop's shape without them.
                present_buses, groups = self._presence_read(set(offbus_ids) - unreadable, expected)
                absent_groups = _absent_tray_groups(present_buses, expected, set(offbus_ids) - unreadable, groups)
            whole = [f"bus group 0x{g:02x}" for g in absent_groups]
        if not whole:
            return False
        log(
            f"{off_bus}/{expected} chip(s) off the bus with whole tray(s) {whole} down — handing the hold to "
            f"the power cycle"
        )
        health_event(
            "whole_tray_down_power_cycle",
            trays=whole,
            off_bus=sorted(offbus_ids, key=int),
            expected=expected,
            fault_class="whole_tray",
            host_at_risk=True,
        )
        offbus_power_cycle.note_whole_tray_down(
            self.deps.device_hold_episode_since(), f"trays {whole}, {off_bus}/{expected} off the bus"
        )
        return True
