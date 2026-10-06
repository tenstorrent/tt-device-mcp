# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Galaxy recovery: the mesh-wide ``tt-smi -glx_reset``, its gentlest-first routing (a per-chip
bridge reset before the mesh-wide reset; a host reboot/power-cycle only once a reset has already
run and failed), and the explicit cascade router that names the rung each degradation justifies —
the one the between-job health gate now steers on.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp import metrics, privileges
from tt_device_mcp.device_holders import MIN_TENANT_UID
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitor import _UNIDENTIFIED_BOARDS, _normalized_board
from tt_device_mcp.health.monitors.heartbeat import dead_chips
from tt_device_mcp.health.monitors.pci import chip_node_present, pci_bus_number
from tt_device_mcp.health.recovery import (
    BLOCKED,
    DEFER,
    HOLD_FABRIC_UNVERIFIED,
    OUTCOME_RECOVERED,
    OUTCOME_TERMINAL,
    OUTCOME_WAITING,
    RELEASE,
    STAGE_BRIDGE_RESET,
    STAGE_HOST_REBOOT,
    STAGE_POWER_CYCLE,
    STAGE_SMI_RESET,
    STAGE_UBB_TRAY,
    WAIT,
    Evidence,
    Recovery,
)
from tt_device_mcp.health.recovery.base import HOLD_ESCALATION_REARM_SEC
from tt_device_mcp.health.recovery.stages.bridge_reset import gone_chip_bridge_reset_enabled
from tt_device_mcp.health.recovery.stages.host_reboot import _fire_host_reboot
from tt_device_mcp.health.recovery.stages.ubb_tray import _fire_ubb_reset, _ubb_reset_argv

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
    """Fewest dead chips that justifies the destructive reboot/power-cycle rung.

    Below this the wedge is a single/few-chip active-eth-core freeze that clears itself while the holder-kill covers the dead-BAR-read MCE path — so a reboot there kills
    every in-flight tenant/agent for a fault that heals on its own. At or above it the mesh is
    mostly gone, which self-heal does not recover. Floored at 2 so one dead chip never reboots;
    the fraction is per-host tunable and a malformed value falls back rather than crashing start."""
    try:
        frac = float(os.environ.get("TT_DEVICE_MCP_REBOOT_MIN_DEAD_FRAC", "0.5"))
    except ValueError:
        frac = 0.5
    if not (0.0 < frac <= 1.0):
        frac = 0.5
    return max(2, math.ceil(expected * frac))


def _galaxy_reset_min_dead_chips(expected: int) -> Optional[int]:
    """Fewest off-bus chips that justifies the mesh-wide galaxy reset. Defaults to the mass-drop
    floor; None only when an operator explicitly disables it (frac <= 0), restoring the old
    reset-any-unhealthy behavior.

    The galaxy reset is the measured all-chip drop: run against a single/few-chip off-bus wedge it
    takes the mesh with it — one chip off the bus, one -glx_reset, and the other 31 go to
    0xFFFFFFFF until a power cycle. That wedge is an active-eth-core freeze that self-heals on its own; only a mass drop, which self-heal does not clear, is worth the reset. So
    a below-floor drop HOLDS (self-heal works, no tenant runs on the held mesh) and only a mass drop
    resets. Distinct from the REBOOT floor above, which gates the host reboot rung AFTER a reset
    already ran — this one gates the reset itself.

    Default-safe: unset -> the same mass-drop floor as the reboot rung (frac 0.5), because a single
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
    climbs to the reboot rung if the reset does not recover rather than looping or sitting."""
    return os.environ.get("TT_DEVICE_MCP_STUCK_HOLD_RESET", "1").strip() == "1"


def _offbus_hold_escalate_enabled() -> bool:
    """Whether an idle OFF-BUS hold that has stood past the ceiling may escalate to the recovery
    cascade. ON by default; TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE=0 is the operator kill-switch. The
    present-mesh sibling (_stuck_hold_reset_enabled) closes the eth/fault strand; this closes the
    twin an off-bus drop leaves — a chip that left the bus, held below the reset floor, which the
    read-only relift cannot lift (still off the bus) and which an idle box has no next-job gate to
    run the gone-chip cascade on, so read-only it stands forever. On by default because that hold is
    unbounded otherwise; it fires only on an idle, tenant-free drop past the ceiling, is rate-limited
    to one recovery per episode, and climbs to the reboot rung if the reset does not recover."""
    return os.environ.get("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "1").strip() == "1"


def _post_reboot_verify_enabled() -> bool:
    """Whether the broker, on coming back up from a warm reboot IT fired, climbs to the cold rung
    when the reboot did not bring the mesh back. ON by default; TT_DEVICE_MCP_POST_REBOOT_VERIFY=0
    is the operator kill-switch. It is on by default because a warm reboot cannot re-enumerate a
    whole-bus wedge (a 6U-Galaxy reboot does not power-cycle the UBBs) — so a reboot that came back
    all-off-bus has no other rung to reach, and the incident was exactly that boot re-holding the
    box dead instead of climbing. The actual power cycle stays behind AUTO_POWER_CYCLE regardless
    of this switch: off, or with no power cycle opted in, it emits the loud event and holds."""
    return os.environ.get("TT_DEVICE_MCP_POST_REBOOT_VERIFY", "1").strip() != "0"


def _ubb_reset_enabled() -> bool:
    """Whether the broker may FIRE the per-tray BMC reset itself on a below-floor tray-down, rather than
    only NAMING the command and holding. ON by default, with a knob to force it off.

    A tray reset re-powers one 8-chip tray and leaves the other 24 running, so it is the lightest rung
    that fits the failure that took the incident box down: a whole tray off the bus, below the
    galaxy-reset floor, which the mesh-wide reset inverts to an all-off-bus mesh and a warm reboot cannot
    re-enumerate. Without it that drop HOLDS forever — the mesh reset is suppressed below the floor — and
    that indefinite hold is the failure this rung exists to end. Only the target tray's ARC restarts, so
    it never risks the mesh reset's off-bus inversion, and the bit order and re-enumeration handshake are
    hardware-confirmed. An operator whose BMC the broker has not confirmed forces the rung off with
    TT_DEVICE_MCP_AUTO_UBB_RESET=0, back to naming the command and holding.

    Armed also means fireable (I14, I17): the re-power goes out as `ipmitool raw` to the local
    BMC, so a host with no reachable BMC reports the rung OFF rather than choosing it and raising.
    """
    if os.environ.get("TT_DEVICE_MCP_AUTO_UBB_RESET", "1").strip() == "0":
        return False
    return privileges.can_ipmi()


def _stuck_hold_ceiling_sec() -> int:
    """Seconds a GENERIC hold may stand — and the window its mesh-reset retries run inside — before
    the ladder climbs through it to a power cycle. The hard ceiling on any hold. Default 600 (10
    min): a reset brings a recoverable drop back immediately, so a hold still standing at the
    ceiling has already had every gentler rung and their retries; the box is idle and
    tenant-refused, so climbing then costs nothing a longer wait would save. Parsed defensively — a
    malformed or non-positive value reads as the default, never 0 (which would escalate the instant
    a hold latched)."""
    raw = os.environ.get("TT_DEVICE_MCP_STUCK_HOLD_SEC", "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 600
    return v if v > 0 else 600


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


# The two top-level branches every hold takes, named in code so the ladder reads the way the
# design does. TRAY_DOWN_NO_WINDOW is the one drop the data says no reset ever recovers (a whole
# UBB tray off the bus with no bridge window on any of its chips) — so it fires every reset type
# back-to-back and goes straight to the power cycle. GENERIC is everything else, including anything
# unseen: cheap rungs first, verify between, mesh-reset retries to the ceiling, then the power
# cycle. Selected ONCE per hold (see _classify_hold).
HOLD_CLASS_TRAY_DOWN_NO_WINDOW = "tray-down-no-window"
HOLD_CLASS_GENERIC = "generic"


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


def _classify_hold(
    offbus_chips: set,
    expected: int,
    bridge_reset_failed: Optional[dict] = None,
    tray_map: Optional[dict] = None,
) -> str:
    """The branch a hold takes, chosen once from the gate's inputs.

    ``HOLD_CLASS_TRAY_DOWN_NO_WINDOW`` iff the off-bus set is exactly one or more whole UBB trays
    (``_ubb_reset_plan`` matches — every off-bus chip's tray fully down, nothing off-bus outside
    them) AND every off-bus chip has NO bridge window: a secondary bus reset on it reports
    ``no_bridge`` because its PCIe node left the bus entirely. That is the one drop measured never
    to return by any rung (40/40 episodes), so it earns the back-to-back reset sweep straight to a
    power cycle. Everything else — a partial-tray drop, an all-ones (still-bridged) chip a reset can
    recover, a present-mesh eth/fabric wedge, or anything unseen — is ``HOLD_CLASS_GENERIC``.

    ``bridge_reset_failed`` maps chip id -> its last SBR result dict (``{"reason": "no_bridge", ...}``);
    a chip absent from it has an UNKNOWN window, so the hold is not the no-window class.

    ``tray_map`` is the cached bus-derived tray identity (I16). Without one the caller cannot know
    which chips share a tray, so the classification cannot land in TRAY_DOWN_NO_WINDOW — the same
    "no map, no fire" rule the planners follow. Pure and side-effect-free."""
    if _ubb_reset_plan(offbus_chips, expected, tray_map) is None:
        return HOLD_CLASS_GENERIC
    reasons = bridge_reset_failed if isinstance(bridge_reset_failed, dict) else {}
    for chip in offbus_chips:
        rec = reasons.get(chip)
        if rec is None:
            rec = reasons.get(str(chip))
        if not isinstance(rec, dict) or rec.get("reason") != "no_bridge":
            return HOLD_CLASS_GENERIC
    return HOLD_CLASS_TRAY_DOWN_NO_WINDOW


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
    galaxy-resets it, and the minimum spacing between retries — deliberately far SHORTER than the
    hold ceiling. A galaxy reset re-inits the eth cores in a ~60s recoverable pass and is the ONLY
    cure for a wedge enum+ARC cannot see (it does not self-heal by waiting); eth links re-train in
    seconds, so a wedge still present at the grace is genuine, not a training window. The escalation
    retries on this cadence until the mesh recovers or the 10-min ceiling hands it to the host
    rungs: the box is idle and tenant-refused, so a retry costs nothing, and a retry that drops the
    mesh off the bus flips the hold to the off-bus ladder, which owns that failure mode. Default
    120 (2 min). Parsed defensively — a malformed or non-positive value reads as the default,
    never 0."""
    raw = os.environ.get("TT_DEVICE_MCP_PRESENT_MESH_RESET_GRACE_SEC", "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 120
    return v if v > 0 else 120


def _stuck_hold_reset_due(hold_since: str, *, off_bus: int, now: Optional[datetime] = None) -> bool:
    """Whether an idle PRESENT-mesh hold has stood past the present-mesh reset grace and its class
    admits the gate's galaxy reset.

    True only for a PRESENT mesh (``off_bus == 0``). There a galaxy reset usually re-inits every eth
    core in a ~60s recoverable pass, clearing a frozen-but-present core, and resets a falsely-held
    healthy mesh needlessly but recoverably. This is NOT the gate's default for this class: the gate
    HOLDS a present-mesh eth/fault hold (it is below the reset floor) rather than reset it, because a
    galaxy reset on a present-but-eth-unverifiable mesh can itself drop every chip off the bus. The
    escalation accepts that bounded risk as the cure for an otherwise-unbounded idle strand — at the
    grace, not the hard ceiling, because an eth wedge does not self-heal by waiting longer — and the
    caller retries on the grace cadence until recovery or the ceiling (a drop under the reset flips
    the hold to the off-bus ladder, which owns that failure mode). An off-bus drop (``off_bus > 0``)
    is EXCLUDED: it self-heals and a galaxy reset there is the measured all-chip drop
    that inverts the mesh (1 off-bus -> 31), so it keeps holding to the ceiling. Age is read off the
    idle-latched episode clock; a missing or unparseable stamp fails closed (not due). The caller
    applies the arm switch, retry-spacing, no-tenant and reset-scope guards on top."""
    if off_bus != 0:
        return False
    if not hold_since:
        return False
    try:
        started = datetime.fromisoformat(hold_since)
    except ValueError:
        return False
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


def _choose_recovery_escalation(
    *,
    auto_reboot_enabled: Callable[[], bool],
    auto_power_cycle_enabled: Callable[[], bool],
    reboot_already_attempted: Callable[[], bool],
) -> Optional[str]:
    """The host-level recovery rung to attempt after a reset failed to revive the mesh, or None if
    the host opted into neither. Escalates least-drastic-first: a warm reboot is tried before the
    chassis power cycle, and the power cycle is reached only once a reboot has already been tried
    and the box came back still wedged — UNLESS the host opted into the power cycle alone, in which
    case a whole-bus wedge (which a reboot cannot clear anyway) goes straight to it.

    ``auto_reboot_enabled``/``auto_power_cycle_enabled``/``reboot_already_attempted`` are injected
    (server.py's own env predicates and its ``RecoveryMechanism`` instance) rather than read from a
    module global, so this stays free of a server<->health import cycle."""
    if auto_power_cycle_enabled() and (reboot_already_attempted() or not auto_reboot_enabled()):
        return "power-cycle"
    if auto_reboot_enabled():
        return "reboot"
    return None


def _host_escalation_for_drop(
    off_bus: int,
    expected: int,
    *,
    warm_reboot_futile: bool = False,
    auto_reboot_enabled: Callable[[], bool],
    auto_power_cycle_enabled: Callable[[], bool],
    reboot_already_attempted: Callable[[], bool],
) -> tuple[Optional[str], bool]:
    """The host recovery rung to fire for an ``off_bus``/``expected`` drop, plus whether the warm
    reboot was suppressed as unable to recover it.

    Identical to :func:`_choose_recovery_escalation` for any drop that still leaves a chip on the
    bus. The one exception is a WHOLE-bus drop on a multi-chip host — every chip off the bus
    (``off_bus >= expected``), i.e. present==0. A warm reboot does not power-cycle a 6U-Galaxy's
    UBBs, so ASICs that fell off the bus stay off across it (measured: 32/32 came back off the bus
    after the auto-reboot, and only a cold chassis power cycle recovered them). Firing the reboot
    rung there cannot work — it takes every tenant down for ~3 min and lands back in the dead state
    — so the reboot is never the answer for it:

      * power cycle opted in -> ``("power-cycle", False)``: skip straight to the cold rung.
      * otherwise -> ``(None, True)``: no opted-in rung can recover it. The caller MUST emit the
        loud actionable event and KEEP HOLDING — never fall through to the reboot.

    ``warm_reboot_futile`` extends that same "the reboot cannot recover this" verdict to a PARTIAL
    drop the caller has already proven a reboot cannot clear — a reset that regressed the mesh off
    the bus, or one that hard-exited non-zero leaving chips off it. Those dropped ASICs are off the
    bus too, so the reboot is as useless for them as for a whole-bus drop; route them the same way.

    Pure and side-effect-free; the caller acts on the result. ``expected > 1`` because a
    single-chip host has no UBB-re-enumeration failure mode — its ordinary ladder applies."""
    escalation = _choose_recovery_escalation(
        auto_reboot_enabled=auto_reboot_enabled,
        auto_power_cycle_enabled=auto_power_cycle_enabled,
        reboot_already_attempted=reboot_already_attempted,
    )
    reboot_futile = expected > 1 and (warm_reboot_futile or off_bus >= expected)
    if not reboot_futile or escalation == "power-cycle":
        return escalation, False
    if auto_power_cycle_enabled():
        return "power-cycle", False
    return None, True


# The ledger/auto_recovery_allowed vocabulary — what record_auto_recovery(action=...) and every
# auto_recovery_* event have always written, so it is durable and never translated. These two maps
# are the ONE place a host rung crosses between it and the stage-name vocabulary `_route` returns.
CASCADE_REBOOT = "reboot"  # a warm host reboot — takes the whole box down
CASCADE_POWER_CYCLE = "power-cycle"  # a BMC chassis power cycle, above the reboot
_HOST_ESCALATION_STAGE = {CASCADE_REBOOT: STAGE_HOST_REBOOT, CASCADE_POWER_CYCLE: STAGE_POWER_CYCLE}
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
) -> str:
    """The single gentlest-first recovery rung justified by the current degradation — THE escalation
    policy, reached through :class:`GalaxyRecovery`'s ``next_stage``, which folds the host-rung
    inputs and derives this platform's own floors before calling it.

    The gate is a two-phase machine — it picks an action, runs it, then asks again about the
    outcome — and this router names the rung for each phase, selected by ``last_action``/
    ``last_action_recovered``. A stateless "what is the next rung" cannot represent it faithfully,
    because the host-reboot rung is a decision reachable only AFTER a galaxy reset has already run
    and failed, and a bridge/tray-reset recovery is a release with its own outcome semantics,
    distinct from a fresh pass's release.

    ``last_action_recovered is True`` — whichever rung ran (bridge reset, galaxy reset, or per-tray
    BMC reset) just recovered the mesh -> ``RELEASE``. Which fault bookkeeping accompanies the
    release (retiring a runtime-reported fault or leaving it set) is gate-local, not a router
    concern — every recovering rung reads the same here.

    ``last_action == STAGE_SMI_RESET and last_action_recovered is False`` — the galaxy reset this
    pass ALREADY ran and did not recover; only now is a host rung reachable:

    * ``reboot_blocked`` -> ``BLOCKED``: no opted-in rung can recover this drop (a whole-bus drop,
      or a reset that regressed/hard-failed a partial one, with no power cycle opted in to catch
      it) — distinct from ``WAIT``, which means the wedge may still self-heal.
    * a host rung is opted in (``host_escalation``), a prior cycle failed too (``was_failing`` — one
      bad pass is not "clearly won't recover"), no reset is still cycling, and the drop is a mass
      drop OR a present mesh that survived the reset still unhealthy (``off_bus == 0 or
      off_bus >= reboot_floor``) -> that rung (``STAGE_HOST_REBOOT`` then ``STAGE_POWER_CYCLE``).
      ``off_bus == 0`` climbs on its own because the reboot floor is floored at 2 and could never
      otherwise be satisfied by a present mesh — an eth/fabric wedge the reset could not clear,
      which self-heal will not either since it already survived the deepest reset.
    * anything short holds (``WAIT``). The gate applies its no-tenant and rate-limit guards on top
      before acting; this only names the rung the ladder selects.

    ``last_action == STAGE_UBB_TRAY and last_action_recovered is False`` -> ``WAIT``: the per-tray
    BMC reset ran and did not clear the drop (or found no tray-down to fire on); a warm reboot
    cannot revive a still-off-bus tray either, so the mesh holds for the cold rung, never climbs to
    the galaxy reset the floor already suppressed.

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
    * below the galaxy floor with nothing to surgically reset -> ``STAGE_UBB_TRAY``: the per-tray
      BMC reset is the next-gentlest rung the gate always attempts before holding — a single/
      few-chip wedge self-heals otherwise, and a galaxy reset there is the measured
      all-chip drop. The floor is keyed on every WEDGED chip (``off_bus + frozen_chips``), not the
      off-bus count alone — an all-present, all-ARC-frozen mesh has nothing off the bus to invert,
      so a reset is the only remedy, and counted by off-bus alone it would read as 0 and hold below
      the floor forever. This is the branch that must NOT climb to a mesh-wide reset over a
      single-chip wedge.
    * otherwise -> ``STAGE_SMI_RESET``. The gate runs this reset whether or not a prior cycle
      failed (``was_failing``): one failed reset plus an elapsed cooldown earns another attempt, not
      a jump to the host rung.

    ``galaxy_floor`` is None only when an operator opts out to the old reset-any-unhealthy behavior;
    then any off-bus chip resets. ``host_escalation``/``reboot_blocked`` are the caller's already-
    folded verdict — :func:`_host_escalation_for_drop`'s result (``GalaxyRecovery.next_stage``
    derives it from ``off_bus_before``/``reset_exit_nonzero``, never this function, which stays
    pure and side-effect-free: the gate does the acting, this only names the rung)."""
    if last_action_recovered:
        return RELEASE
    if last_action == STAGE_SMI_RESET and last_action_recovered is False:
        if reboot_blocked:
            return BLOCKED
        if (
            host_escalation is not None
            and was_failing
            and not reset_scope_active
            and (off_bus == 0 or off_bus >= reboot_floor)
        ):
            return _HOST_ESCALATION_STAGE.get(host_escalation, host_escalation)
        return WAIT
    if last_action == STAGE_UBB_TRAY and last_action_recovered is False:
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
    if galaxy_floor is not None and off_bus + frozen_chips < galaxy_floor:
        return STAGE_UBB_TRAY
    return STAGE_SMI_RESET


# --- per-tray UBB BMC reset -----------------------------------------------------------------
#
# A Galaxy's 32 ASICs sit on 4 UBB trays of 8: a tray-level power/link failure drops its whole tray
# off the PCIe bus at once, the measured failure that took chips 0-7 down together. A per-tray BMC
# reset re-powers one tray and leaves the other 24 chips running — lighter than a 32-chip galaxy
# reset and blind to a healthy tray. The BMC bit order is verified: a partial-bitmap reset restarts
# only the targeted tray's ARC and leaves every other tray advancing and on the bus.
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
    heartbeat read (its node left the bus) or present but reading all-ones. Its cardinality matches
    the off_bus count the rungs use (``len(dead_chips) + max(0, expected - len(beats))``); the id
    SET is what a rung needs to map a drop onto physical trays."""
    missing = {str(i) for i in range(expected)} - set(beats)
    return set(dead_chips(beats)) | missing


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
    # A chip the map cannot place has no tray to re-power, and planning around it would report a
    # walk as complete while leaving that chip down (I16).
    if not ids <= {c for chips in tray_map.values() for c in chips}:
        return None
    return ids


def _ubb_reset_plan(offbus_chips: set, expected: int, tray_map: Optional[dict]):
    """Map an off-bus chip set to the whole trays a per-tray BMC reset would re-power, or None when
    the drop is not a clean tray-down the per-tray rung fits.

    Returns ``(sorted_tray_numbers, bitmap)`` ONLY when the off-bus set is exactly the chips of one
    or more FULLY-down trays — every off-bus chip's whole tray is off the bus, and nothing off-bus
    lies outside those trays. A partial-tray drop (some of a tray's chips still up) or a chip off-bus
    in an otherwise-live tray is NOT this signature: re-powering the tray would knock out its healthy
    chips, so that falls through to the per-chip rung / hold instead.

    Tray numbers come from ``tray_map`` and are 1-based (I16); the BMC's bits are 0-based, hence the
    ``tray - 1`` shift. Pure and side-effect-free — the caller decides what to do with the plan."""
    ids = _offbus_ids_on_trays(offbus_chips, expected, tray_map)
    if ids is None:
        return None
    down = set()
    covered: set = set()
    for tray, chips in tray_map.items():
        if set(chips) <= ids:
            down.add(tray)
            covered |= set(chips)
    # Only a drop confined to whole trays fits: an off-bus chip outside a fully-down tray means a
    # tray reset would either miss it or take out live silicon, so the muddy picture holds instead.
    if not down or covered != ids:
        return None
    bitmap = 0
    for tray in down:
        bitmap |= 1 << (tray - 1)
    return sorted(down), bitmap


def _affected_trays(offbus_chips: set, expected: int, tray_map: Optional[dict]) -> Optional[list]:
    """The sorted UBB trays that CONTAIN any off-bus chip — the trays a per-tray BMC reset re-powers to
    clear the drop. Unlike :func:`_ubb_reset_plan` this does NOT require the whole tray to be down: a
    single off-bus chip makes its tray affected, because re-powering a tray re-inits all eight of its
    chips (the incidentally-live ones bounce with it) and that is still far lighter than the 32-chip
    mesh reset — the rung known to invert a partial drop off the bus (8 -> 32) and to hard-exit. None
    on every condition :func:`_offbus_ids_on_trays` refuses. Pure and side-effect-free."""
    ids = _offbus_ids_on_trays(offbus_chips, expected, tray_map)
    if ids is None:
        return None
    return sorted({tray for tray, chips in tray_map.items() if ids & set(chips)})


def _all_trays(expected: int, tray_map: Optional[dict]) -> Optional[list]:
    """EVERY UBB tray on the host, for the last-chance sweep that precedes a host rung: about to take the
    box down, a drop with no tray to blame (a present-mesh eth/fabric wedge) still gets every tray
    re-powered, because a tray re-power is strictly lighter than the reboot or power cycle it is
    delaying. None for a host without multi-tray topology or without a cached bus map (I16): tray
    identity is bus-derived, so no map means no tray to name."""
    if expected < 2 * UBB_CHIP_COUNT or expected % UBB_CHIP_COUNT != 0:
        return None
    if not tray_map:
        return None
    return sorted(tray_map)


# Issue #26 (spec 04 I18): a tray with this many chips off the bus at the FIRST sighting is a tray that
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


def _ubb_tray_walk_plan(offbus_chips: set, expected: int, tray_map: Optional[dict]) -> Optional[list]:
    """Order the trays for a one-at-a-time per-tray reset walk: every AFFECTED tray first (those that
    hold an off-bus chip, sorted), then THE REST (sorted). Reset one tray at a time, re-verifying
    between each, and a full sweep re-powers every tray without ever taking the whole mesh off the bus
    at once — which is the mesh-wide reset's failure mode. The affected trays lead because they hold the
    fault; the rest follow only if re-powering the affected ones did not clear it (a fabric wedge that
    spans trays). None whenever :func:`_affected_trays` is None — no tray to re-power. Pure and
    side-effect-free; the caller walks the list and decides when the mesh has verified healthy."""
    affected = _affected_trays(offbus_chips, expected, tray_map)
    if affected is None:
        return None
    lead = set(affected)
    rest = sorted(t for t in tray_map if t not in lead)
    return affected + rest


def _maybe_emit_ubb_reset_required(beats: dict, off_bus: int, expected: int, log, tray_map: Optional[dict]):
    """When a below-floor drop maps onto UBB trays, name the lightest sufficient recovery — re-powering
    the AFFECTED trays over the BMC — in a loud actionable event, and return the tray ids; else None.

    A below-floor drop otherwise holds indefinitely: the galaxy reset inverts it to an all-off-bus
    mesh, and a warm reboot cannot re-enumerate dropped ASICs, so neither fires. Re-powering just the
    trays that hold the off-bus chip(s) is far lighter. This names the trays and the BMC command and
    leaves the hold standing; the opted-in rung (:func:`GalaxyRecovery._attempt_ubb_tray_reset`,
    TT_DEVICE_MCP_AUTO_UBB_RESET) walks them one at a time instead. Absence is never health: the drop
    is made visible and actionable, never swallowed into a silent hold.

    Silent, though, when ``tray_map`` is None — the trays are unknown, so there is no tray to name
    and the command this would print would target the wrong silicon (I16)."""
    trays = _affected_trays(_offbus_chip_ids(beats, expected), expected, tray_map)
    if not trays:
        return None
    bitmap = 0
    for t in trays:
        bitmap |= 1 << (t - 1)
    command = " ".join(_ubb_reset_argv(bitmap))
    health_event(
        "ubb_reset_required",
        trays=trays,
        ubb_bitmap=bitmap,
        command=command,
        off_bus=off_bus,
        expected=expected,
        present=max(0, expected - off_bus),
        host_at_risk=True,
    )
    tray_list = ", ".join(_tray_label(t, tray_map) for t in trays)
    log(
        f"UBB {tray_list} hold the {off_bus}/{expected} off-bus chip(s) — a below-floor drop "
        f"the mesh-wide reset inverts and a warm reboot cannot re-enumerate. The lightest sufficient "
        f"recovery is a per-tray BMC reset of the affected tray(s): `{command}`. The broker walks them "
        f"one at a time when TT_DEVICE_MCP_AUTO_UBB_RESET is set; here it stays HELD."
    )
    return trays


class GalaxyRecovery(Recovery):
    """Recovery for a 6U Galaxy: the mesh-wide reset, gentlest-first routing ahead of it (a
    per-chip bridge reset when a chip is surgically isolable), and the host reboot/power-cycle
    escalation once a reset has already run and failed to revive the mesh."""

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

    def next_stage(self, ev: Evidence) -> str:
        # host_escalation/reboot_blocked are folded here, not carried on Evidence (see its own
        # docstring): only the post-galaxy-reset phase ever needs them, so the ledger read inside
        # `reboot_already_attempted` — the one piece of this fold that touches disk — is skipped on
        # every other call, exactly as the gate only pays for it once a galaxy reset has failed.
        host_escalation, reboot_blocked = None, False
        if ev.last_action == STAGE_SMI_RESET and ev.last_action_recovered is False:
            reset_regressed = (
                ev.expected > 1
                and ev.off_bus_before is not None
                and ev.off_bus > ev.off_bus_before
                and not ev.scope_active
            )
            warm_reboot_futile = (
                ev.expected > 1
                and ev.off_bus > 0
                and not ev.scope_active
                and (reset_regressed or ev.reset_exit_nonzero)
            )
            host_escalation, reboot_blocked = _host_escalation_for_drop(
                ev.off_bus,
                ev.expected,
                warm_reboot_futile=warm_reboot_futile,
                auto_reboot_enabled=self.deps.auto_reboot_enabled,
                auto_power_cycle_enabled=self.deps.auto_power_cycle_enabled,
                reboot_already_attempted=self.mechanism._reboot_already_attempted,
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
        )

    def _platform_reset_argv(self, indices: list) -> list:
        # All ASICs, no targets: a 6U Galaxy needs -glx_reset — per-PCIe-target -r does not
        # recover it (the other trays never take part in a per-target reset).
        return ["tt-smi", "-glx_reset"]

    # ---- idle/forced hold-escalation ladder --------------------------------------------------
    #
    # This whole ladder is Galaxy-flavored in practice: it fires the mesh-wide galaxy reset, the
    # per-tray UBB reset, and the host reboot/power-cycle escalation, none of which a per-target
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

        Every caller passes here first, so this is where a tray-down onset (spec 04 I18) is latched
        and, before its first rung, given one PCI rescan and the read-only capture. Then the ladder
        runs exactly as it would have, with the caller's own evidence and stage: the prelude never
        picks a rung, skips one or fires a power cycle. Only a rescan that brought every chip back
        onto a mesh that passes the full verify ends the episode before the ladder."""
        rung, _, gate_phase = phase.partition("/")
        if await self._tray_down_latch(expected, beats) == TRAY_DOWN_ONSET and not self._td["prelude_done"]:
            if await self._tray_down_prelude(expected, log, phase=gate_phase or rung):
                return OUTCOME_RECOVERED
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
        if rung == "offbus":
            ran = await self._escalate_offbus_stuck_hold(indices, expected, log, force=force)
        elif rung == "present":
            ran = await self._escalate_stuck_hold(indices, expected, log, force=force)
        else:
            raise ValueError(f"escalate(): unknown phase {phase!r}")
        if not ran:
            return OUTCOME_WAITING
        return OUTCOME_RECOVERED if was_degraded and not self.deps.device_degraded() else OUTCOME_TERMINAL

    # ---- the between-job gate's action rungs --------------------------------------------------
    #
    # The gate (server.py) DECIDES: it builds the Evidence, asks next_stage for the one rung that
    # evidence justifies, and keeps every guard, hold flavour and journal line of its own. This is
    # the other half — firing the named rung, and the release bookkeeping each rung's own outcome
    # semantics call for.
    #
    # Both halves apply on EVERY platform. The gate's ladder has always been one ladder: the
    # galaxy-reset floor, the surgical rung, the tray walk and the host rungs are the policy at a
    # job boundary whatever the board is, and only the reset argv is platform-specific — resolved
    # inside _reset_and_verify_device, which re-derives select_recovery per call precisely so a
    # rung reached through a fixed platform reference still fires the command that fits the host
    # actually detected. So the gate reaches this through the module-global Galaxy instance,
    # exactly as it has always called _attempt_ubb_tray_reset and _auto_reboot_host, not through
    # its per-pass platform choice.

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

        The verdict reported is the ACTION's own (the re-verify, the tray walk, the reset), not a
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
        if stage == STAGE_UBB_TRAY:
            # ladder-v2: the below-floor tray rung splits on the hold's CLASS, decided ONCE here from
            # this pass's evidence (see _classify_hold). A whole UBB tray off the bus with no bridge
            # window on any of its chips — every off-bus chip's SBR reported no_bridge, threaded onto
            # ev.bridge_reset_failed by the server after the bridge rung fired — is the
            # TRAY_DOWN_NO_WINDOW class: the one drop measured never to return by any single reset
            # (0/40), so it fires every reset type back-to-back straight to the power cycle
            # (_fire_tray_down_no_window). Everything else — a partial-tray drop, a chip that still
            # has a bridge window, or a window left unknown because no bridge rung ran this pass —
            # takes the generic verify-between tray walk (_attempt_ubb_tray_reset).
            offbus_set = _offbus_chip_ids(beats or {}, expected)
            tray_map = self._tray_map_now()
            hold_class = _classify_hold(offbus_set, expected, ev.bridge_reset_failed if ev else None, tray_map)
            health_event(
                "hold_classified",
                phase=gate_phase,
                hold_class=hold_class,
                off_bus=sorted(offbus_set, key=int),
                trays=_affected_trays(offbus_set, expected, tray_map),
                expected=expected,
            )
            if hold_class == HOLD_CLASS_TRAY_DOWN_NO_WINDOW:
                # Honour the SAME opt-in the generic walk does: with the per-tray fire off
                # (TT_DEVICE_MCP_AUTO_UBB_RESET=0) name the lightest sufficient recovery and hold
                # rather than re-power silicon unasked — the same name-and-hold the generic path
                # takes. The sweep's own scope/tenant guards and the power-cycle cooldown/boot-loop
                # denials still gate it inside _fire_tray_down_no_window.
                if not _ubb_reset_enabled():
                    _maybe_emit_ubb_reset_required(beats or {}, len(offbus_set), expected, log, tray_map)
                    log("tray-down-no-window but the per-tray reset is not opted in — naming the command and holding")
                    metrics.stage_fired("ubb_tray", "blocked")
                    return OUTCOME_WAITING
                # _fire_tray_down_no_window clears the fault/dirty itself on a recovered verify and
                # holds (WAITING) when it power-cycles or a guard blocks it, so its verdict is final.
                return await self._fire_tray_down_no_window(offbus_set, expected, log, gate_phase=gate_phase, ev=ev)
            # GENERIC: the per-tray verify-between walk. It declines itself (naming the command
            # instead) where there is no tray to re-power, the fire is not opted in, or a guard
            # blocks it — each reads as "did not recover" and the gate's below-floor hold owns it.
            if await self._attempt_ubb_tray_reset(beats or {}, ev.off_bus if ev else 0, expected, log) is not True:
                return OUTCOME_WAITING
            self.deps.clear_device_reported_fault(f"gate/{gate_phase}: per-tray BMC reset recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why=f"gate/{gate_phase}: per-tray BMC reset recovered the mesh")
            return OUTCOME_RECOVERED
        if stage in (STAGE_SMI_RESET, DEFER):
            # DEFER lands here too: a reset already cycling in its own scope is not a second reset
            # to start but one to ADOPT — _reset_and_verify_device waits that scope out
            # (await_foreign_scope) and verifies its result instead of issuing anything.
            if not await self._reset_and_verify_device(indices, log):
                return OUTCOME_WAITING
            self.deps.clear_device_reported_fault(f"gate/{gate_phase}: reset recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why=f"gate/{gate_phase}: reset recovered the mesh")
            return OUTCOME_RECOVERED
        if stage in (STAGE_HOST_REBOOT, STAGE_POWER_CYCLE):
            # The caller has already confirmed the opt-in, the two-strikes precondition, that no
            # tenant holds the device and that the durable rate limits pass. Before taking the box
            # down, give every reset type one last back-to-back run, then ONE settle and ONE verify
            # (ladder-v2: the last-chance sweep gates every host rung): a drop that came back needs
            # no reboot or power cycle.
            if await self._settle_and_verify_before_host_rung(
                expected, log, gate_phase, offbus_chips=_offbus_chip_ids(beats or {}, expected), context="gate ladder"
            ):
                return OUTCOME_RECOVERED
            # A fired host rung takes the box with it, so there is no verdict to report: WAITING
            # leaves the episode as it stands for the boot that comes back to verify it.
            if stage == STAGE_POWER_CYCLE:
                await self.deps.auto_power_cycle_host(log, "reset and a host reboot could not recover the device")
            else:
                await self._auto_reboot_host(log, "reset failed twice; device unrecoverable by reset")
            return OUTCOME_WAITING
        raise ValueError(f"escalate(): {stage!r} is not a gate action rung")

    # ---- the tray-down prelude (spec 04 I18, issue #26) -----------------------------------------
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
        if not td["rescanned"]:
            if tenant_active:
                rescan = "waiting (a tenant holds the device)"
            else:
                td["rescanned"] = True
                try:
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
            if not off:
                healthy, _ = await self._verify_device(expected, log, run_fabric=True, phase=phase)
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

    def _tenant_active(self, scan, *, force: bool = False) -> bool:
        """Is anyone using the device? TWO independent sources, because either one alone lies:

        * the holder scan — open ``/dev/tenstorrent`` fds. Blind while a running job is off the
          device (compiling, loading weights, between opens), which is minutes of a real run.
        * the broker's own queue — a job in RUNNING state. Authoritative about work the broker
          itself dispatched, and it does not care whether that job currently holds an fd.

        Measured failure this exists to stop (g15blx02, 2026-09-17 10:07:09Z): the stuck-hold
        escalation read an empty holder scan 106 s into a running job, fired a mesh reset, and the
        job died of SIGPIPE at 10:09:18Z the moment the reset released the device. The broker knew
        the job was RUNNING the whole time; nothing asked it.

        ``force`` (the hold-deadline backstop) still waives an UNREADABLE scan — that guard protects
        against blindness, not against a known tenant — but never waives a real one, fd-held or
        queue-known: resetting out from under a live job is the thing the guard is for."""
        if self.deps.job_running():
            return True
        if any(h.uid >= MIN_TENANT_UID for h in scan.holders):
            return True
        return not force and not scan.complete

    async def _issue_all_resets_back_to_back(
        self, bitmap: int, tray_chip_ids: list, trays: list, expected: int, log, *, do_sbr: bool = True
    ) -> None:
        """Fire EVERY reset type once, back-to-back, with no waiting or verifying between them: SBR on
        any chip still bridged, then a per-tray BMC re-power of ``bitmap``, then the mesh-wide reset —
        each issued as soon as the previous command returns. The single implementation of "all rungs",
        shared by the TRAY_DOWN_NO_WINDOW branch and by the last-chance sweep that gates every host
        rung, so the two can never drift apart. Never verifies and never raises: a rung that fails to
        launch is logged and the next one is issued anyway — before a reboot or a power cycle there is
        nothing left to lose. The caller owns the guards (tenant, scope), the settle and the verify."""
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
            # 2) per-tray BMC re-power, all at once, no verify. Skipped when the operator turned the
            # tray fire off (TT_DEVICE_MCP_AUTO_UBB_RESET=0) — the command is named instead — and when
            # the host has no tray topology.
            if bitmap:
                if not _ubb_reset_enabled():
                    log(
                        f"per-tray BMC re-power of trays {trays} is not opted in "
                        f"(TT_DEVICE_MCP_AUTO_UBB_RESET=0): {' '.join(_ubb_reset_argv(bitmap))} — "
                        f"skipping that rung and continuing to the mesh reset"
                    )
                    health_event("ubb_reset_required", trays=trays, command=" ".join(_ubb_reset_argv(bitmap)))
                else:
                    try:
                        await asyncio.to_thread(_fire_ubb_reset, bitmap, tray_chip_ids)
                    except Exception as exc:  # noqa: BLE001 - a fire that did not launch falls through to the reset
                        log(
                            f"per-tray BMC re-power of trays {trays} failed to launch: {exc!r}; "
                            f"continuing to the mesh reset"
                        )
                        health_event("ubb_reset_failed", trays=trays, error=repr(exc))
            # 3) mesh-wide reset, no verify (the ONE verify belongs to the caller). Lazy import:
            # select_recovery lives in the package __init__ that imports THIS module, so a top-level
            # import would be circular; by call time the package is fully loaded.
            from tt_device_mcp.health.recovery import select_recovery

            argv = select_recovery(self.monitor, self.mechanism, self.deps).reset_argv(
                [str(i) for i in range(expected)]
            )
            await self.mechanism.reset_with_quiesce(argv, log)
        except Exception as exc:  # noqa: BLE001 - the host rung above must fire even if a reset blew up
            log(f"the back-to-back reset sweep raised {exc!r} — continuing to the host rung")
            health_event("last_chance_reset_sweep_error", error=repr(exc))
        finally:
            self.mechanism.reset_in_flight = False

    async def _settle_and_verify_before_host_rung(
        self, expected: int, log, gate_phase: str, *, offbus_chips: Optional[set] = None, context: str = ""
    ) -> bool:
        """The LAST-CHANCE gate every host rung passes through: fire every reset type once more,
        back-to-back, then ONE settle and ONE verify — and only if the mesh is still bad does the
        caller reboot or power-cycle. True iff the mesh came back, in which case the fault/dirty are
        retired and the door reopens, so the caller skips the host rung entirely.

        Why the resets and not just a settle: a reboot or a power cycle takes the whole box down and
        costs minutes, so before paying that there is nothing to lose by re-issuing SBR, a per-tray
        re-power and the mesh reset (user, 2026-09-17: "we always need to give the resets one last
        chance before power cycle"). Before this, only the TRAY_DOWN_NO_WINDOW branch swept; every
        other road to a host rung — the generic ladder's ceiling, the idle/stuck-hold escalation, the
        post-reboot cold climb — settled and verified but never re-tried the rungs, so a present-mesh
        eth wedge could be power-cycled having only ever seen mesh resets.

        ``offbus_chips`` names the trays to re-power; with none known (a present-mesh wedge) EVERY tray
        is re-powered, which is still lighter than the rung it is delaying. The sweep is SKIPPED — the
        settle and verify still run — when a tenant holds the mesh or a reset is already cycling in its
        own scope: those are the two guards a destructive rung never crosses."""
        where = f" ({context})" if context else ""
        tray_map = self._tray_map_now()
        trays = _affected_trays(offbus_chips or set(), expected, tray_map) or _all_trays(expected, tray_map) or []
        bitmap = 0
        for t in trays:
            bitmap |= 1 << (t - 1)
        # Chip ids come from the tray map (I16), not index arithmetic — bus 0x80 is tray 4 on BH
        # and index arithmetic would target the wrong silicon.
        tray_chip_ids = [c for t in trays for c in (tray_map or {}).get(t, [])]
        swept = False
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
                    ubb_bitmap=bitmap,
                    off_bus=len(offbus_chips or set()),
                    expected=expected,
                    host_at_risk=True,
                )
                log(
                    f"last chance before the host rung{where}: firing every reset type back-to-back "
                    f"(SBR -> per-tray re-power of trays {trays} -> mesh reset) — nothing to lose before a "
                    f"reboot or power cycle"
                )
                await self._issue_all_resets_back_to_back(
                    bitmap, tray_chip_ids, trays, expected, log, do_sbr=bool(self.deps.isolated_chips())
                )
        settle = _settle_before_host_rung_sec()
        log(f"settling {settle}s for re-enumeration, then one verify before the host rung")
        await asyncio.sleep(settle)
        healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase=gate_phase)
        if not healthy:
            return False
        via = "last-chance-reset-sweep" if swept else "settle-before-host-rung"
        log(f"the mesh verified after the {via} — no reboot or power cycle needed")
        health_event("gate", phase=gate_phase, healthy=True, recovered_via=via, evidence=evidence)
        self.deps.clear_device_reported_fault(f"gate/{gate_phase}: mesh recovered before the host rung ({via})")
        self.deps.clear_device_dirty(
            verified=True, why=f"gate/{gate_phase}: mesh recovered before the host rung ({via})"
        )
        return True

    async def _fire_tray_down_no_window(
        self, offbus_chips: set, expected: int, log, *, gate_phase: str, ev: Optional[Evidence]
    ) -> str:
        """The TRAY_DOWN_NO_WINDOW branch: a whole UBB tray off the bus with no bridge window on any
        chip — the one drop measured never to return by any reset (0/40). So fire every reset type
        BACK-TO-BACK with no waiting or verifying between them — SBR on the dead chips, a per-tray
        BMC re-power of the affected trays, then the mesh-wide reset — issue each as soon as the
        previous command returns, then ONE settle and ONE verify, and only if the mesh is still bad,
        the power cycle. No retries, no ceiling wait, no rung skipped: there is nothing to lose
        before a power cycle.

        Returns OUTCOME_RECOVERED if the one verify came back healthy, else OUTCOME_WAITING (the
        power cycle fired, or was blocked/held). Guards: never under a tenant, never over a reset
        already cycling in its own scope — it re-powers silicon."""
        if await asyncio.to_thread(self.mechanism.scope_active):
            log("tray-down-no-window but a reset is already cycling in its own scope — holding; the gate adopts it")
            return OUTCOME_WAITING
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        if self._tenant_active(scan):
            log(
                "tray-down-no-window but a tenant holds the device, the scan is unreadable, or a job is running — holding"
            )
            return OUTCOME_WAITING
        tray_map = self._tray_map_now()
        trays = _affected_trays(offbus_chips, expected, tray_map) or []
        bitmap = 0
        for t in trays:
            bitmap |= 1 << (t - 1)
        # Chip ids come from the tray map (I16); index arithmetic transposes trays 3/4 on BH.
        tray_chip_ids = [c for t in trays for c in (tray_map or {}).get(t, [])]
        health_event(
            "tray_down_no_window_sweep",
            trays=trays,
            ubb_bitmap=bitmap,
            off_bus=len(offbus_chips),
            expected=expected,
            host_at_risk=True,
        )
        log(
            f"TRAY_DOWN_NO_WINDOW ({len(offbus_chips)}/{expected} off the bus, trays {trays}) — firing every "
            f"reset type back-to-back (SBR -> per-tray re-power -> mesh reset), then one {_settle_before_host_rung_sec()}s "
            f"settle and one verify before a power cycle"
        )
        await self._issue_all_resets_back_to_back(bitmap, tray_chip_ids, trays, expected, log)
        # ONE settle, ONE verify.
        settle = _settle_before_host_rung_sec()
        log(f"back-to-back reset sweep issued — settling {settle}s, then one verify")
        await asyncio.sleep(settle)
        healthy, evidence = await self._verify_device(expected, log, run_fabric=True, phase=gate_phase)
        if healthy:
            log("tray-down-no-window sweep recovered the mesh — no power cycle needed")
            health_event("tray_down_no_window_recovered", trays=trays, evidence=evidence)
            self.deps.clear_device_reported_fault(f"gate/{gate_phase}: tray-down-no-window sweep recovered the mesh")
            self.deps.clear_device_dirty(
                verified=True, why=f"gate/{gate_phase}: tray-down-no-window sweep recovered the mesh"
            )
            return OUTCOME_RECOVERED
        # Still bad after every reset — the power cycle, under the same opt-in + tenant + rate-limit
        # guards as any host rung. A whole tray off the bus with no window is warm-reboot-futile (a
        # 6U-Galaxy reboot does not power-cycle the UBBs), so it goes straight to the cold rung.
        off_bus = len(offbus_chips)
        # Log-only: more chips may be off by now than at onset. Every decision below uses off_bus.
        try:
            beats_now = await asyncio.to_thread(self.deps.read_heartbeats)
            off_bus_after = len(_offbus_chip_ids(beats_now or {}, expected))
        except Exception as e:  # noqa: BLE001 - a failed re-read must never block the power cycle
            log(f"tray-down-no-window: could not re-read the heartbeats after the verify: {e!r}")
            off_bus_after = None
        escalation, reboot_blocked = _host_escalation_for_drop(
            off_bus, expected, warm_reboot_futile=True, **self.deps.host_escalation_kwargs()
        )
        if reboot_blocked or escalation != "power-cycle":
            log(
                f"tray-down-no-window sweep did not recover the mesh; power cycle not auto-fired "
                f"(off the bus: {off_bus}/{expected} at onset, {off_bus_after} after the verify)"
            )
            if off_bus >= expected:
                self.deps.emit_all_off_bus_power_cycle_required(log, off_bus, expected, f"gate/{gate_phase}")
            else:
                self._emit_reset_unrecoverable_power_cycle_required(
                    log, off_bus, expected, regressed=False, context=f"gate/{gate_phase} tray-down-no-window"
                )
            metrics.stage_fired("power_cycle", "blocked")
            return OUTCOME_WAITING
        tenant_active = self._tenant_active(scan)
        allowed, why = self.mechanism.auto_recovery_allowed(escalation, tenant_active=tenant_active)
        if not allowed:
            log(
                f"tray-down-no-window sweep did not recover the mesh and a power cycle is opted in but held off: {why} "
                f"(off the bus: {off_bus}/{expected} at onset, {off_bus_after} after the verify)"
            )
            self.mechanism._journal_auto_recovery_denied(escalation, why)
            metrics.stage_fired("power_cycle", "blocked")
            return OUTCOME_WAITING
        health_event("tray_down_no_window_power_cycle", off_bus=off_bus, off_bus_after=off_bus_after, expected=expected)
        await self.deps.auto_power_cycle_host(
            log, "tray-down-no-window: every reset type ran back-to-back and the mesh did not verify"
        )
        return OUTCOME_WAITING

    async def _escalate_stuck_hold(self, indices: list, expected: int, log, *, force: bool = False) -> bool:
        """Escalate a present-mesh hold the read-only relift cannot clear to the gate's galaxy reset
        once it has stood past the ceiling. Returns True iff a reset ran (recovered or not); False
        leaves the hold standing for the next window.

        This is the gate's OWN ``_reset_and_verify_device`` — only the trigger is new (the idle timeout,
        not the next job), so an idle box stops stranding on a hold no read-only check can lift. Called
        only from the relift's under-lock frozen/unreadable-eth branch, where a healthy enum+ARC pass has
        already proven every chip present, so a galaxy reset usually re-inits every eth core. An eth
        wedge does not self-heal by waiting and the box is idle and tenant-refused, so the escalation
        RETRIES on the grace cadence until the mesh recovers or the 10-min ceiling hands it to the host
        rungs — a retry costs nothing, and a retry that drops the mesh off the bus flips the hold to
        the off-bus ladder, which owns that failure mode.

        Every guard fails closed: armed by opt-in only; a PRESENT mesh past the grace
        (``_stuck_hold_reset_due``, re-reading off-bus now under the lock); at least one grace window
        since the last reset attempt (the pacing that keeps retries from stacking on a reset+verify
        still settling); no reset already cycling in its own scope; and no foreign tenant (a scan we
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
        off_bus = len(dead_chips(beats)) + max(0, expected - len(beats))
        hold_since = self.deps.device_hold_episode_since()
        if not force and not _stuck_hold_reset_due(hold_since, off_bus=off_bus):
            return False
        # Retry pacing, not a cap: the grace window between attempts replaces the failed-reset
        # cooldown here — that cooldown guards against repeat MMIO at a DEAD endpoint, but this mesh
        # is fully present (live endpoints), so the only thing to pace is the reset+verify cycle
        # itself.
        since_last_reset = time.monotonic() - self.mechanism.last_reset_monotonic
        if not force and self.mechanism.last_reset_monotonic and since_last_reset < _present_mesh_reset_grace_sec():
            log(
                f"present-mesh escalation ran a reset {int(since_last_reset)}s ago — retrying on the "
                f"{_present_mesh_reset_grace_sec()}s cadence until recovery or the ceiling"
            )
            metrics.stage_fired("smi_reset", "blocked")
            return False
        if await asyncio.to_thread(self.mechanism.scope_active):
            return False
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        tenant_active = self._tenant_active(scan, force=force)
        if tenant_active:
            log("hold past the ceiling but a tenant holds the device — holding; escalates once idle")
            return False
        log(
            f"present-mesh hold stood past the {_present_mesh_reset_grace_sec()}s grace with no tenant — "
            f"escalating to the galaxy reset the next gate would run"
        )
        health_event(
            "stuck_hold_escalation",
            held_since=hold_since,
            reason=self.deps.device_hold_episode_reason(),
            off_bus=off_bus,
        )
        # Still latched for visibility (the deadline watchdog journals it), but no longer a cap on
        # this path: retries are paced by the grace window above.
        self.deps.set_device_hold_episode_escalated(True)
        # Baseline the off-bus count BEFORE the reset so the climb can tell a reset that regressed the
        # mesh (dropped more chips off the bus) from one that merely did not recover — the former needs
        # the cold rung, not a warm reboot that cannot re-enumerate the dropped ASICs.
        beats_before = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus_before = len(dead_chips(beats_before)) + max(0, expected - len(beats_before))
        recovered = await self._reset_and_verify_device(indices, log)
        if recovered:
            self.deps.clear_device_reported_fault("idle escalation: galaxy reset recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why="idle escalation: galaxy reset recovered the mesh")
            return True
        # A reset that did not recover must climb, never sit. The failure mode that must not sit is a
        # galaxy reset that dropped this present mesh to all-0xFFFFFFFF — a mass drop a host
        # reboot/power cycle recovers and self-heal never will. Before the ceiling the climb defers
        # the host rungs (journaled) and the grace cadence retries the reset; at the ceiling the
        # climb hands the mesh to the gate's host rung.
        await self._climb_to_host_recovery_after_failed_reset(expected, off_bus_before, log)
        return True

    async def _escalate_offbus_stuck_hold(self, indices: list, expected: int, log, *, force: bool = False) -> bool:
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
        # per-tray UBB re-power -> reboot -> power cycle) from "runs once, then holds" into "runs each
        # window until the box recovers or reaches the loud terminal rung."
        #
        # Only the first and last rungs are universal. The tray rung is Galaxy-only — on a loudbox both
        # UBB plans return None and it is skipped — and the surgical rung cannot reach a chip that has
        # LEFT the bus, because the parent bridge it resets through went with it (exit 3, "no bridge to
        # reset"). A dropped ASIC on a non-Galaxy host therefore falls past both to the mesh reset and
        # then, at the ladder's end, to the cold rung — which must stay armed for the ladder to
        # terminate at all.
        if not _offbus_hold_escalate_enabled() and not force:
            return False
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = len(dead_chips(beats)) + max(0, expected - len(beats))
        hold_since = self.deps.device_hold_episode_since()
        if not force and not _offbus_stuck_hold_due(hold_since, off_bus=off_bus):
            return False
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
        tenant_active = self._tenant_active(scan, force=force)
        if tenant_active:
            log("off-bus hold past the ceiling but a tenant holds the device — holding; escalates once idle")
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
        isolated_chips = self.deps.isolated_chips()
        device_pci_map = self.deps.device_pci_map()
        # A chip GONE from the bus entirely — absent from sysfs, not reading all-ones — never entered
        # isolated_chips, so the surgical rung below skips it and it falls to the galaxy reset, which
        # here is unbounded: with the host-reboot rung opted out a galaxy reset that inverts a below-floor
        # drop has no reboot to climb to. When opted in, route a single/few-chip gone drop to the surgical
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
                    off_bus_gone = len(dead_chips(beats_b)) + max(0, expected - len(beats_b))
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
        if (
            isolated_chips
            and not await asyncio.to_thread(self.mechanism.scope_active)
            and await self._recover_isolated_chips(log)
        ):
            healthy, evidence = await self._verify_device(expected, log, run_fabric=True)
            if healthy:
                self.deps.clear_device_reported_fault("idle off-bus escalation: bridge reset recovered the mesh")
                self.deps.clear_device_dirty(
                    verified=True, why="idle off-bus escalation: bridge reset recovered the mesh"
                )
                return True
        # Below the galaxy-reset floor, hold rather than fire the mesh-wide reset — the same suppression
        # the gate applies (galaxy_reset_suppressed_below_mass_threshold). A galaxy reset is the measured
        # all-chip drop: run against a single/few-chip off-bus wedge it inverts the mesh (the drop goes
        # all-0xFFFFFFFF), and a warm reboot cannot re-enumerate dropped ASICs, so resetting a below-floor
        # drop can only make it worse. The drop self-heals and the surgical per-chip rung
        # above is the only reset that applies below the floor, so once that cannot recover it, hold: the
        # deadline watchdog keeps the hold visible and self-heal or a cold power cycle clears it, never a
        # reset that deepens the drop. Skip the floor only while a reset already cycles (its all-ones reads
        # like a mass drop — let _reset_and_verify_device adopt and verify that scope) or when an operator
        # disabled the floor entirely (reset_floor None -> the old reset-any-unhealthy behaviour).
        reset_floor = _galaxy_reset_min_dead_chips(expected)
        off_bus_before = None
        if reset_floor is not None and not await asyncio.to_thread(self.mechanism.scope_active):
            beats_pre = await asyncio.to_thread(self.deps.read_heartbeats)
            off_bus_pre = len(dead_chips(beats_pre)) + max(0, expected - len(beats_pre))
            if off_bus_pre < reset_floor:
                # A below-floor drop confined to whole UBB trays is a tray-down: the per-tray BMC reset
                # is the lightest sufficient recovery. Opted in, it fires here — between the per-chip bridge
                # rung above and the suppressed mesh reset; otherwise the command is named and the hold
                # below stands. A reset that recovered clears the hold; one that fired but did not falls to
                # the climb below, since a warm reboot cannot re-enumerate a still-off-bus tray.
                outcome = await self._attempt_ubb_tray_reset(beats_pre, off_bus_pre, expected, log)
                if outcome is True:
                    self.deps.clear_device_reported_fault(
                        "idle off-bus escalation: per-tray BMC reset recovered the mesh"
                    )
                    self.deps.clear_device_dirty(
                        verified=True, why="idle off-bus escalation: per-tray BMC reset recovered the mesh"
                    )
                    return True
                # The tray reset did not recover it (or was not opted in). The gate keeps the mesh
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
                    f"({reset_floor}) and the per-chip/tray rungs could not recover it; trying the "
                    f"mesh-wide reset before any host rung"
                )
                health_event(
                    "stuck_hold_galaxy_reset_below_floor_attempt",
                    off_bus=off_bus_pre,
                    expected=expected,
                    floor=reset_floor,
                )
            off_bus_before = off_bus_pre
        if off_bus_before is None:
            # The floor read above did not run (floor opted out, or a reset was cycling), so take a
            # pre-reset baseline here — the climb needs it to catch a reset that regresses the mesh.
            beats_before = await asyncio.to_thread(self.deps.read_heartbeats)
            off_bus_before = len(dead_chips(beats_before)) + max(0, expected - len(beats_before))
        recovered = await self._reset_and_verify_device(indices, log)
        if recovered:
            self.deps.clear_device_reported_fault("idle off-bus escalation: galaxy reset recovered the mesh")
            self.deps.clear_device_dirty(verified=True, why="idle off-bus escalation: galaxy reset recovered the mesh")
            return True
        # A reset that did not recover must climb, never sit: a galaxy reset that dropped this drop to an
        # all-off-bus mesh is the mass drop a host reboot/power cycle recovers and self-heal never will.
        # This episode's one reset is spent and an idle box has no next-job gate to run another, so hand
        # the mesh straight to the host rung.
        await self._climb_to_host_recovery_after_failed_reset(expected, off_bus_before, log)
        return True

    async def _climb_to_host_recovery_after_failed_reset(
        self, expected: int, off_bus_before: Optional[int], log
    ) -> None:
        """Climb an idle escalation whose galaxy reset failed to revive the mesh to the host rung — a
        warm reboot, then a BMC power cycle above it — so a reset that did not recover does not sit.
        Bounded below by the hold ceiling: the host rungs never fire before the episode has stood the
        full ladder, however futile the gentler rungs proved — an early forced pass fires the cheap
        rungs only, and the deadline watchdog re-runs the ladder at the ceiling where the climb is
        then admitted.

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
        reset that mass-dropped an idle, tenant-free mesh is enough to reboot here; the rate limiter bars
        a loop. A no-op where the host opted into no host rung — except an all-off-bus drop, which always
        emits the loud actionable event before holding, since absence is never health.

        ``off_bus_before`` is the caller's pre-reset baseline. A reset that REGRESSED the mesh (more chips
        off the bus than before) or hard-exited leaving chips off it is a warm-reboot-futile drop: a
        6U-Galaxy reboot does not power-cycle the UBBs, so a dropped ASIC stays off across it. Route such
        a reset straight to the cold rung, never the warm reboot that just proved it cannot help — the
        same verdict the gate reaches, so a regression is handled the same on the idle path as at a job
        boundary."""
        if await asyncio.to_thread(self.mechanism.scope_active):
            log(
                "reset did not recover but one is still cycling in its own scope — not escalating over "
                "its in-flight all-ones; the next gate adopts and verifies it"
            )
            return
        # The host rungs are bounded BELOW by the hold ceiling: however futile the gentler rungs
        # look, a reboot or power cycle never fires before the episode has stood the full ladder.
        # The early off-bus pass (_offbus_hold_ceiling_sec) exists to fire the CHEAP rungs early,
        # and the deadline watchdog re-forces the ladder each ceiling window, so a climb deferred
        # here is re-attempted at the ladder's end, never lost. A missing episode clock cannot
        # defer — the watchdog keys on that same clock, so deferring without one would strand the
        # hold with nothing left to re-attempt it.
        hold_since = self.deps.device_hold_episode_since()
        hold_age = _hold_age_sec(hold_since)
        if hold_age is not None and hold_age < _stuck_hold_ceiling_sec():
            log(
                f"mesh unrecovered after the reset ladder but the hold is only {int(hold_age)}s "
                f"old — the host rungs (reboot/power-cycle) wait for the "
                f"{_stuck_hold_ceiling_sec()}s ladder end; holding for the next forced window"
            )
            health_event(
                "host_rung_deferred_until_ladder_end",
                held_since=hold_since,
                held_age_sec=int(hold_age),
                ceiling_sec=_stuck_hold_ceiling_sec(),
            )
            return
        beats = await asyncio.to_thread(self.deps.read_heartbeats)
        off_bus = len(dead_chips(beats)) + max(0, expected - len(beats))
        reboot_floor = _reboot_min_dead_chips(expected)
        # A below-floor off-bus drop reaching this climb is NOT self-healing: it survived the 20-min grace
        # and a reset that could not recover it. A dropped ASIC does not re-enumerate on a warm reboot (a
        # 6U-Galaxy reboot does not power-cycle the UBBs), so the cold power cycle is the only rung that can
        # recover it — never hold below the floor. Mark it warm-reboot-futile so the chooser routes it
        # straight to the cold rung; the reboot floor only ever gated the mesh reset, not the power cycle.
        below_floor_offbus = 0 < off_bus < reboot_floor
        # A reset that made the mesh WORSE off the bus (the incident's 8->32), or that hard-exited
        # leaving chips off it, cannot be undone by a warm reboot — a 6U-Galaxy reboot does not
        # power-cycle the UBBs, so a dropped ASIC stays off across it. Route such a reset to the cold
        # rung, never the reboot, and flag a regression loudly. No reset is cycling here (the top guard
        # already returned on a live scope), so the count is a confirmed post-reset state. This mirrors
        # the gate's own escalation so a regression climbs the same on the idle path as at a job boundary.
        reset_regressed = expected > 1 and off_bus_before is not None and off_bus > off_bus_before
        warm_reboot_futile = (
            expected > 1
            and off_bus > 0
            and (reset_regressed or self.mechanism.last_reset_exit_nonzero or below_floor_offbus)
        )
        if reset_regressed:
            health_event(
                "reset_regressed_offbus",
                off_bus_before=off_bus_before,
                off_bus_after=off_bus,
                expected=expected,
                host_at_risk=True,
            )
            log(
                f"the idle escalation's reset REGRESSED the mesh — {off_bus_before} -> {off_bus} of "
                f"{expected} chip(s) off the bus. A warm reboot cannot re-enumerate a dropped Galaxy "
                f"ASIC, so climbing to the cold rung, never the reboot."
            )
        escalation, reboot_blocked = _host_escalation_for_drop(
            off_bus, expected, warm_reboot_futile=warm_reboot_futile, **self.deps.host_escalation_kwargs()
        )
        if reboot_blocked:
            # No chassis power cycle is opted in and no rung can recover it: fail closed loudly and hold,
            # never fire a reboot that lands right back in the dead state. A whole-bus drop and a reset
            # that regressed/hard-failed a partial drop off the bus each need the cold rung, but name
            # them apart so the count is not misreported as all-off-bus.
            if off_bus >= expected:
                self.deps.emit_all_off_bus_power_cycle_required(log, off_bus, expected, "idle escalation")
            else:
                self._emit_reset_unrecoverable_power_cycle_required(
                    log, off_bus, expected, regressed=reset_regressed, context="idle escalation"
                )
            # reboot_blocked means specifically: this drop needs the cold rung and power-cycle is
            # not opted in — a power_cycle kill-switch decline, not a "no rung exists at all" case.
            metrics.stage_fired("power_cycle", "blocked")
            return
        if not escalation:
            # Both TT_DEVICE_MCP_AUTO_REBOOT and _AUTO_POWER_CYCLE are off (the default) —
            # gentlest-first, the reboot rung is the one this leaves declined.
            metrics.stage_fired("host_reboot", "blocked")
            return
        if off_bus == 0:
            # A PRESENT mesh (every chip on the bus, ARC-healthy) whose galaxy reset STILL did not verify
            # is an eth/fabric wedge the reset could not clear — and self-heal will not either, since it
            # already survived the deepest reset. The old code held it here (below the mass floor), which
            # is an INDEFINITE hold: measured on blx04, hours of ceiling -> galaxy-reset -> hold with the
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
            return
        # The recorded cause names what is actually true at this point — the old fixed string
        # blamed a galaxy reset even on paths where the reset was suppressed and never ran.
        cause = (
            "idle escalation: present mesh failed the post-reset verify"
            if off_bus == 0
            else f"idle escalation: {off_bus}/{expected} chip(s) off the bus after the reset ladder"
        )
        # Last chance: every reset type once more, back-to-back, then one settle and one verify. This
        # is the road MOST real power cycles took (the idle/stuck-hold escalation), and before ladder-v2
        # it climbed to the host rung without ever re-trying the cheaper rungs.
        if await self._settle_and_verify_before_host_rung(
            expected, log, "idle-escalation", context=f"idle escalation, {off_bus}/{expected} off the bus"
        ):
            return
        health_event("stuck_hold_host_escalation", escalation=escalation, off_bus=off_bus)
        if escalation == "power-cycle":
            await self.deps.auto_power_cycle_host(log, cause)
        else:
            await self._auto_reboot_host(log, cause)

    async def _verify_post_reboot_recovery(self, off_bus: int, expected: int, log) -> None:
        """After a warm reboot the broker itself fired, confirm the mesh came back — and climb to the
        cold rung if it did not, instead of silently re-holding a dead box.

        A warm reboot fires and then the broker dies with the box; the next boot is its ONLY
        verification point. The incident was a reboot that returned 0/32 — a 6U-Galaxy reboot does not
        power-cycle the UBBs, so ASICs that fell off the bus stay off across it — and nothing then
        compared what came back to what left, so the startup hold was re-raised and the box sat dead
        ~18h. This is that comparison: the auto-recovery ledger says this boot IS a broker reboot's
        result, the sysfs probe says how much of the mesh returned.

        A mass drop the reboot did not clear does not self-heal and the reboot already failed on it, so
        the only rung above it is the chassis power cycle — fired under the SAME opt-in + rate-limit +
        tenant guards as every other host rung, never a second warm reboot (the rung that just failed).
        With no power cycle opted in it emits the loud actionable event and keeps holding; absence is
        never health. Below the reboot floor a few-chip drop re-enumerates on its own, so it holds
        exactly as the gate does — the reboot substantially worked and taking the box down again would
        kill tenants for a wedge that clears itself."""
        if not _post_reboot_verify_enabled():
            return
        boot_id = self.deps.current_boot_id()
        escalation_rec = self.mechanism.boot_from_broker_escalation(boot_id)
        if not escalation_rec or escalation_rec.get("action") != "reboot":
            # Not a broker warm reboot's result: an external/manual reboot or firmware crash (nothing to
            # verify), or the top rung — a power cycle — already fired, above which there is no rung to
            # climb. Only a reboot the broker fired to recover the device is verified here.
            return
        if await asyncio.to_thread(self.mechanism.scope_active):
            # A reset re-adopted from the previous broker is still cycling; its all-ones is not a
            # confirmed drop. The startup fabric verify adopts it — do not climb over its in-flight state.
            log(
                "the warm reboot that brought this boot up left a reset still cycling in its own scope — "
                "deferring the post-reboot verdict to it rather than climbing over its in-flight all-ones"
            )
            return
        if off_bus == 0:
            # A fully-present mesh reads healthy on the sysfs probe and never calls this; guard anyway so a
            # zero-drop is never routed to a power cycle.
            return
        # The warm reboot ran and the mesh came back with chips STILL off the bus. A dropped ASIC does not
        # re-enumerate on another warm reboot (a 6U-Galaxy reboot does not power-cycle the UBBs) and the
        # reboot was itself the self-heal escalation, so a still-off-bus mesh here is NOT self-healing at
        # any count — the cold power cycle is the only rung above the reboot that just ran. Route to it,
        # never hold below the floor; the reboot floor only ever gated the mesh reset, not the power cycle.
        escalation, reboot_blocked = _host_escalation_for_drop(
            off_bus, expected, warm_reboot_futile=True, **self.deps.host_escalation_kwargs()
        )
        # _host_escalation_for_drop's reboot_futile only fires for expected > 1 (a single-chip
        # host has no UBB-re-enumeration failure mode of its own), so on a single-chip host it can
        # still hand back "reboot" here even though warm_reboot_futile=True. This method's own
        # contract is never a second warm reboot — the rung that just ran and failed — so anything
        # short of an opted-in power cycle is blocked, same as no rung being available at all.
        if reboot_blocked or escalation != "power-cycle":
            # No chassis power cycle is opted in — the one rung that could recover a mesh a warm reboot
            # cannot re-enumerate. Fail closed LOUDLY (never a silent re-hold); name a whole-bus drop apart
            # from a partial one so the count is not misreported as all-off-bus.
            if off_bus >= expected:
                self.deps.emit_all_off_bus_power_cycle_required(log, off_bus, expected, "post-reboot verify")
            else:
                health_event(
                    "post_reboot_recovery_stuck_no_cold_rung", off_bus=off_bus, expected=expected, host_at_risk=True
                )
                log(
                    f"the warm reboot left {off_bus}/{expected} off the bus and no chassis power cycle is "
                    f"opted in — the warm reboot is the rung that just failed and cannot re-enumerate a "
                    f"dropped ASIC. Set TT_DEVICE_MCP_AUTO_POWER_CYCLE=1 to let the broker cold-cycle the "
                    f"chassis, or power-cycle it manually (BMC/ipmitool). Device stays HELD."
                )
            metrics.stage_fired("power_cycle", "blocked")
            return
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        tenant_active = self._tenant_active(scan)
        allowed, why = self.mechanism.auto_recovery_allowed(escalation, tenant_active=tenant_active)
        if not allowed:
            log(
                f"the warm reboot did not bring the mesh back ({off_bus}/{expected} off the bus) and a "
                f"power cycle is opted in but held off: {why}"
            )
            metrics.stage_fired("power_cycle", "blocked")
            return
        # Last chance before the cold rung: the warm reboot did not re-enumerate the mesh, but it also
        # did not re-power the UBBs — so re-issue every reset type once more, settle and verify before
        # pulling the chassis power.
        if await self._settle_and_verify_before_host_rung(
            expected, log, "post-reboot", context=f"post-reboot, {off_bus}/{expected} off the bus"
        ):
            return
        health_event("post_reboot_host_escalation", escalation=escalation, off_bus=off_bus, expected=expected)
        await self.deps.auto_power_cycle_host(log, "post-reboot verify: the warm reboot did not bring the mesh back")

    def _emit_reset_unrecoverable_power_cycle_required(
        self, log, off_bus: int, expected: int, *, regressed: bool, context: str
    ) -> None:
        """Fail closed, loudly, on a PARTIAL drop a warm reboot cannot recover: a reset that regressed
        the mesh off the bus, or hard-exited leaving chips off it, with no auto power cycle opted in.
        Distinct from the whole-bus event so the count is not misreported as all-off-bus. The dropped
        ASICs are off the bus a reboot cannot re-enumerate, so — like the whole-bus wedge — the only
        rung that can recover them is the cold power cycle. Name it and leave the device HELD."""
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
            f"a reset {cause} — {off_bus}/{expected} chip(s) off the PCIe bus [{context}] — which a warm "
            f"reboot CANNOT re-enumerate (a 6U-Galaxy reboot does not power-cycle the UBBs). The reboot "
            f"is not the answer here: set TT_DEVICE_MCP_AUTO_POWER_CYCLE=1 to let the broker cold-cycle "
            f"the chassis, or power-cycle it manually (BMC/ipmitool). Device stays HELD."
        )

    async def _auto_reboot_host(self, log, reason: str) -> None:
        """Fire the warm-host-reboot recovery rung. The caller has already confirmed both the env
        opt-in AND ``RecoveryMechanism.auto_recovery_allowed``."""
        await self.mechanism._fire_recovery_escalation(
            "reboot",
            row_owner="[broker]reboot-request",
            label="auto host reboot",
            detail="reset could not recover the device",
            fire=_fire_host_reboot,
            log=log,
            reason=reason,
        )

    async def _attempt_ubb_tray_reset(self, beats: dict, off_bus: int, expected: int, log) -> Optional[bool]:
        """The per-tray BMC reset rung, between the per-chip bridge reset and the mesh-wide reset. It walks
        the AFFECTED trays — those holding an off-bus chip — one at a time, re-verifying (chips back, ARC
        advancing, FABRIC re-checked) after each and stopping as soon as the whole mesh verifies healthy; if
        re-powering the affected trays does not clear the drop it walks the REST, still one at a time. A full
        sweep re-powers every tray WITHOUT taking the mesh off the bus all at once — unlike the mesh-wide
        reset, which inverted an 8-chip drop to 32 off the bus and can hard-exit.

        Returns True when a tray reset recovered the mesh (the caller clears the hold), False when the walk
        ran and did not (the caller holds; a warm reboot cannot revive a still-off-bus tray, so the next rung
        is the cold power cycle, and the deadline watchdog keeps the hold visible), and None when nothing
        fired — no tray to re-power, the fire not opted in, or a guard blocked it — in which case the command
        is NAMED and the caller holds exactly as before.

        The whole walk runs at most once per hold episode, never while a reset scope cycles, never with a
        tenant on the mesh: it re-powers silicon, so it carries the same guards as the other destructive
        rungs. Every guard falls back to naming the command and holding, never a half-done or repeated walk.
        A fully-off-bus mesh is declined outright and NOT named as a tray-down — that is the cold-power-cycle
        case, and a BMC tray reset cannot verify a tray with no chip on the bus."""
        if expected > 0 and off_bus >= expected:
            # A fully-off-bus mesh is the cold-power-cycle case, not the tray rung: a BMC tray reset cannot
            # verify a tray with no chip on the bus and cannot re-enumerate a dropped ASIC (a warm re-power
            # does not bring one back), so walking every tray here fires a rung that cannot work. Decline so
            # the caller's all-off-bus handling — the loud power-cycle-required event and the cold rung —
            # owns it, exactly as when there is no tray to re-power.
            log(
                f"all {off_bus}/{expected} chip(s) off the bus — the per-tray BMC reset does not apply "
                f"(a tray with nothing on the bus cannot be verified back); the cold power cycle owns this"
            )
            # A topology fact (nothing left on the bus to verify a tray back onto), not a guard an
            # operator could flip — see STAGE_OUTCOMES for why this is not_applicable, not blocked.
            metrics.stage_fired("ubb_tray", "not_applicable")
            return None
        offbus_ids = _offbus_chip_ids(beats, expected)
        tray_map = self._tray_map_now()
        walk = _ubb_tray_walk_plan(offbus_ids, expected, tray_map)
        if walk is None:
            # Two shapes both classify as not_applicable, not blocked: (1) a per-target (non-Galaxy)
            # host, where there is no tray concept at all — every below-floor off-bus drop lands
            # here; (2) a Galaxy whose gate never banked a bus-id map (spec 04 I16), so the tray a
            # drop belongs to is unknown and the rung declines rather than fire on a guess. Both
            # are missing facts, not operator-flippable kill-switches, and would otherwise climb
            # without bound on every such host.
            metrics.stage_fired("ubb_tray", "not_applicable")
            return None  # no tray to re-power — the caller's existing hold owns it
        if not _ubb_reset_enabled():
            _maybe_emit_ubb_reset_required(beats, off_bus, expected, log, tray_map)  # name it + hold (the default)
            metrics.stage_fired("ubb_tray", "blocked")
            return None
        if self.deps.device_hold_episode_ubb_reset_fired():
            _maybe_emit_ubb_reset_required(beats, off_bus, expected, log, tray_map)
            log("tray-down but this episode's one per-tray reset walk already ran — holding; the next rung owns it")
            metrics.stage_fired("ubb_tray", "blocked")
            return None
        if await asyncio.to_thread(self.mechanism.scope_active):
            _maybe_emit_ubb_reset_required(beats, off_bus, expected, log, tray_map)
            log("tray-down but a reset is already cycling in its own scope — holding; the gate adopts it")
            metrics.stage_fired("ubb_tray", "blocked")
            return None
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        if self._tenant_active(scan):
            _maybe_emit_ubb_reset_required(beats, off_bus, expected, log, tray_map)
            log("tray-down but a tenant holds the device, the holder scan is unreadable, or a job is running — holding")
            metrics.stage_fired("ubb_tray", "blocked")
            return None
        affected = set(_affected_trays(offbus_ids, expected, tray_map))
        # Latch before firing, not after: a walk that throws or does not recover must not re-fire next pass.
        self.deps.set_device_hold_episode_ubb_reset_fired(True)
        log(
            f"UBB tray-down ({off_bus}/{expected} off the bus) — walking the per-tray BMC reset (opted in): "
            f"affected trays {sorted(affected)} first, then the rest, one at a time: {walk}"
        )
        # A tray reset takes its 8 chips transiently off the bus, reading all-ones exactly like a dead
        # chip. Hold self.mechanism.reset_in_flight across the whole walk so the dead-chip sampler defers instead
        # of isolating a tray mid-reset and tearing it out of the kernel. Cleared on every exit.
        self.mechanism.reset_in_flight = True
        self.mechanism.reset_since_release = True
        try:
            for step, tray in enumerate(walk):
                bitmap = 1 << (tray - 1)
                tray_chip_ids = list(tray_map[tray])
                command = " ".join(_ubb_reset_argv(bitmap))
                role = "affected" if tray in affected else "sweep"
                health_event(
                    "ubb_reset_begin",
                    tray=tray,
                    ubb_bitmap=bitmap,
                    command=command,
                    step=step + 1,
                    of=len(walk),
                    role=role,
                    off_bus=off_bus,
                    expected=expected,
                    host_at_risk=True,
                )
                log(
                    f"per-tray BMC reset {step + 1}/{len(walk)} ({role}): re-powering "
                    f"{_tray_label(tray, tray_map)}: `{command}`"
                )
                try:
                    await asyncio.to_thread(_fire_ubb_reset, bitmap, tray_chip_ids)
                except Exception as exc:  # noqa: BLE001 - a fire that did not launch falls through, never raises
                    log(f"per-tray BMC reset of tray {tray} failed to launch: {exc!r}; falling to the next rung")
                    health_event("ubb_reset_failed", tray=tray, error=repr(exc))
                    metrics.stage_fired("ubb_tray", "failed")
                    return False
                # The BMC pulse itself completed — "ok" regardless of whether the verify below finds
                # the mesh recovered yet (a partial walk's earlier trays firing cleanly is real signal,
                # same rc==0-always-fires contract as the mesh-wide reset's own "ok" report).
                metrics.stage_fired("ubb_tray", "ok")
                # Re-verify the FABRIC too, not just enum+ARC: a partial reset can leave the fabric
                # unverifiable, and only a traffic pass settles it. A post-reset 77 (links still training)
                # is given time to resolve, and a fabric that never verifies stays NOT healthy — so the
                # walk never STOPS-and-CLEARS the hold onto a fabric no pass ever proved; a still-degraded
                # verify just moves the walk to the next tray, exactly like an off-bus one that did not
                # come back.
                healthy, _evidence, _retries = await self._verify_device_after_reset(expected, log)
                if healthy:
                    health_event("ubb_reset_recovered", tray=tray, walked=step + 1)
                    return True
            log(
                "the per-tray reset walk re-powered the affected trays and then the rest, one at a time, and "
                "the mesh still did not verify — holding; a warm reboot cannot re-enumerate a still-off-bus "
                "tray, so the next rung is the cold power cycle, not a reboot"
            )
            health_event("ubb_reset_did_not_recover", trays=walk)
            return False
        finally:
            self.mechanism.reset_in_flight = False
