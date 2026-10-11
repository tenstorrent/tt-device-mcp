# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Recovery mechanisms that bring devices back online after a fault.

Two platforms need two different reset commands and two different escalation ladders: a 6U
Galaxy resets mesh-wide and can climb to a host reboot/power-cycle when a reset alone does not
revive it; every other board resets per-target and stops there. :class:`Recovery` is the shared
contract — ``next_stage`` names the next rung, ``reset_argv`` names the reset command, and the
platform-invariant verify/reset loop lives here too. :func:`select_recovery` re-derives WHICH
platform fits every time it is called — the board type is not known at startup — but the instance
it hands back is one of two the process ever constructs (see ``_persistent_recovery``), never a
fresh one, so per-pass re-derivation costs a cache lookup, not a rebuild.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from tt_device_mcp import constants, metrics
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.recovery import pcie_guard
from tt_device_mcp.health.recovery.base import RESET_COOLDOWN_SEC, RecoveryMechanism
from tt_device_mcp.health.recovery.stages.bridge_reset import (
    bridge_reset_enabled,
    bridge_reset_unavailable_reason,
    reset_chip_via_bridge,
)

# A `next_stage` decision that is not a stage name: WAIT (the wedge may self-heal; do nothing),
# DEFER (a reset is already cycling; adopt and verify it, start nothing new), RELEASE (the door
# opens — nothing is wrong, or the last action just proved it), HOLD_FABRIC_UNVERIFIED (a fabric
# pass that ran but reached no verdict; hold without resetting), or BLOCKED (a drop no opted-in
# rung can recover — distinct from WAIT, which means "it may self-heal"). The stage names below
# are constants.STAGE_NAMES' own values, re-bound here (not re-exported by import: pyflakes reads
# a bare re-export as an unused import) so a decision routes straight to the name
# metrics.stage_fired() and _HOST_ESCALATION_STAGE use.
WAIT = "wait"
DEFER = "defer"
RELEASE = "release"
HOLD_FABRIC_UNVERIFIED = "hold_fabric_unverified"
BLOCKED = "blocked"
STAGE_BRIDGE_RESET = constants.STAGE_BRIDGE_RESET
STAGE_SMI_RESET = constants.STAGE_SMI_RESET
STAGE_UBB_TRAY = constants.STAGE_UBB_TRAY
STAGE_HOST_REBOOT = constants.STAGE_HOST_REBOOT
STAGE_POWER_CYCLE = constants.STAGE_POWER_CYCLE

# Recovery.escalate's outcome.
OUTCOME_RECOVERED = "recovered"
OUTCOME_WAITING = "waiting"
OUTCOME_TERMINAL = "terminal"

# A surgical bridge reset fired seconds after a chip leaves the bus can lose the race with the
# endpoint's link retrain and report failure, yet the same chip re-binds in ~2s once the link
# settles. Retry a few times before giving up on it (see Recovery._recover_isolated_chips) — this
# is what turns a ~13 min recovery into a ~seconds one.
BRIDGE_RESET_MAX_TRIES = 3
BRIDGE_RESET_SETTLE_SEC = 4

# A reset returns before the eth links re-train, so the immediate post-reset fabric pass can find
# no trained link (77). Retry (see Recovery._verify_device_after_reset), giving the links time to
# train, so the reset resolves to a REAL verdict rather than a transient 77 that reads as
# "recovered" onto an unverified fabric. A 77 that persists past the retries is a genuine wedge.
POST_RESET_FABRIC_RETRIES = int(os.environ.get("TT_DEVICE_MCP_POST_RESET_FABRIC_RETRIES", "1"))
POST_RESET_FABRIC_RETRY_SLEEP_SEC = float(os.environ.get("TT_DEVICE_MCP_POST_RESET_FABRIC_SLEEP_SEC", "60"))


@dataclass(frozen=True)
class Evidence:
    """The gate's live read of the mesh, reshaped for :meth:`Recovery.next_stage`.

    ``galaxy_floor``/``reboot_floor`` are deliberately not fields: the platform derives its own
    floors from ``expected`` (see ``GalaxyRecovery.next_stage``), so a caller cannot hand a Galaxy
    floor to a per-target host by forgetting to recompute it for the platform actually selected.

    ``host_escalation``/``reboot_blocked`` are deliberately NOT fields here: ``next_stage`` derives
    both itself from ``off_bus_before``/``reset_exit_nonzero`` plus the host's own opt-in env reads
    (see ``GalaxyRecovery.next_stage``), so a caller cannot hand it a pre-folded escalation that
    silently disagrees with what the platform would have chosen from the same raw inputs.

    ``last_action``/``last_action_recovered`` replace the old single-action ``reset_recovered``:
    three different rungs (a per-chip bridge reset, the mesh-wide galaxy reset, a per-tray BMC
    reset) can be "the action this pass already ran", each with its own outcome semantics, so the
    router needs to know WHICH action to react to, not just whether one recovered the mesh.
    """

    off_bus: int
    frozen_chips: int
    expected: int
    sbr_candidates: int
    eth_frozen: bool
    cooling: bool
    scope_active: bool
    was_failing: bool
    # The healthy-mesh release path (RELEASE) and the fabric-unverified hold nested inside it
    # (HOLD_FABRIC_UNVERIFIED) — both ahead of the eth-frozen/cooling/reset ladder below, exactly
    # as the gate checks them first.
    healthy: bool
    fault_reported: bool
    fabric_ok: Optional[bool]
    fabric_ran: bool
    dirty: bool
    fabric_forced: bool
    # The action THIS pass already took, if any, and whether it recovered the mesh — None/None
    # before any action has run yet (the pre-action ladder above still applies).
    last_action: Optional[str]
    last_action_recovered: Optional[bool]
    # Raw warm-reboot-futility inputs: the pre-reset off-bus baseline and whether the action's own
    # exit was non-zero. Only meaningful (and only read) when `last_action` is the galaxy reset.
    off_bus_before: Optional[int]
    reset_exit_nonzero: bool
    holding_fabric_unverified: bool = False
    # ladder-v2: the per-chip SBR outcome the bridge rung produced THIS pass — chip id ->
    # ``{"reason": "no_bridge"}`` for every chip whose secondary bus reset could not fire because
    # the endpoint left the bus (no bridge to write to). Threaded by the server's ``replace(ev, …)``
    # AFTER the bridge rung fires (from ``Recovery.last_bridge_reset_reasons``), read only by the
    # STAGE_UBB_TRAY gate rung to classify the hold (:func:`galaxy._classify_hold`). ``next_stage``
    # never reads it — the router stays pure; only the tray rung's fire does. None when no bridge
    # rung ran this pass, which reads as "window unknown" -> the generic branch.
    bridge_reset_failed: Optional[dict] = None


@dataclass
class RecoveryDeps:
    """Server-state accessors and env-derived predicates late-bound by the caller exactly like
    ``RecoveryMechanism.__init__`` itself (server.py wraps its own module globals/functions in
    lambdas) so a test that monkeypatches the underlying server function still takes effect here.
    Shared by :class:`Recovery` and :class:`~tt_device_mcp.health.monitor.HealthMonitor` — one
    accessor bag for the whole health subsystem rather than two that could drift apart.

    ``board_types_provider``/``glx_board_types_provider`` back ``_is_galaxy``'s mode derivation;
    the board-types cache itself lives on the ``HealthMonitor`` singleton (server.py's
    ``health_monitor``), so these are injected rather than read from a module global.

    Most of the remaining fields exist for the same reason: the value they wrap is either genuine
    server state, or a probe primitive that ALREADY lives in the health package but that server.py
    re-exports and tests monkeypatch by its ``server.<name>`` path — a bare import into this
    package would leave that mock aimed at a binding nothing here reads. Wrapping it in a lambda
    exactly like the server-owned fields keeps the mock live: the lambda re-resolves the name from
    server.py's namespace on every call, so a monkeypatched fake still takes effect.
    """

    # Forwarded verbatim to RecoveryMechanism.__init__:
    current_boot_id: Callable[[], str]
    boot_btime_id: Callable[[], str]
    scoped_reset_backend: Callable[[], bool]
    set_device_pollers: Callable[[bool, Callable], Awaitable[list[str]]]
    set_device_op_detail: Callable[[str], None]
    begin_action_row: Callable[[str, str], None]
    write_action_log: Callable[[str, str, float, str, Optional[int]], None]
    device_hold_episode_since: Callable[[], str]
    # GalaxyRecovery's host-escalation ladder (reboot/power-cycle opt-ins + the loop-guard read):
    auto_reboot_enabled: Callable[[], bool]
    auto_power_cycle_enabled: Callable[[], bool]
    # HealthMonitor's own probes (verify_fabric_health/verify_eth_heartbeat): the job-killing
    # ladder and the module logger both live in server.py, not in the health package, so they are
    # injected exactly like everything else here rather than imported back across the boundary.
    terminate_process_group: Callable[[int], Awaitable[None]]
    logger: Callable[[], Optional[logging.Logger]]
    # HealthMonitor.update()'s present-chip count: the SAME /dev/tenstorrent node enumeration the
    # gate, the idle escalators, and the reset routes already use (server._present_chip_indices),
    # injected rather than re-derived a second way — a heartbeat-based count would silently read
    # 0 present chips on a host whose driver exposes no ARC heartbeat at all, which neuters
    # expected()'s enumeration-count check instead of catching a real chip drop.
    present_chip_indices: Callable[[], list[str]]
    # A holder scan the escalation ladder must treat an incomplete read as "a tenant is here" —
    # never let the highest-risk rungs act on a scan that merely could not see everyone:
    enumerate_device_holders: Callable[[], HolderScan]
    # True while the broker has a job in RUNNING state. The holder scan alone is NOT enough evidence
    # that the device is idle: it reads open /dev/tenstorrent fds, and a running job spends minutes
    # off the device (compiling, loading weights, between opens), during which the scan is empty and
    # the ladder read the box as idle. Measured on g15blx02 2026-09-17: the stuck-hold escalation
    # fired a mesh reset 106 s into a running job, and the job died of SIGPIPE the second the reset
    # released the device. The broker knows its own queue — ask it, never infer idleness from fds.
    job_running: Callable[[], bool]
    # isolated_chips/device_pci_map are module globals server.py REASSIGNS at startup, so these
    # are getters re-resolving the current object each call, not a reference captured once (which
    # would go stale the moment startup replaces it). Both sets/dicts are otherwise mutated
    # in place (add/discard/update), so a getter is all a mutator needs.
    isolated_chips: Callable[[], set]
    device_pci_map: Callable[[], dict]
    # The reason the device is currently held — read only, for logging/journaling alongside an
    # escalation attempt.
    device_hold_episode_reason: Callable[[], str]
    # These two latches are both READ and SET. The off-bus ladder checks the current latch to
    # enforce its once-per-episode limit, setting it before acting so a rung that throws or fails
    # never re-fires next window; the present-mesh ladder sets it for visibility only (its retries
    # are paced by the grace cadence, not capped).
    device_hold_episode_escalated: Callable[[], bool]
    # The escalated flag as the ladder must read it: latched, but expiring. Distinct from the raw
    # flag above, which stays True for the whole episode and is what the journal/reporting read.
    hold_escalation_latched: Callable[[], bool]
    set_device_hold_episode_escalated: Callable[[bool], None]
    device_hold_episode_ubb_reset_fired: Callable[[], bool]
    set_device_hold_episode_ubb_reset_fired: Callable[[bool], None]
    clear_device_reported_fault: Callable[[str], None]
    clear_device_dirty: Callable[..., None]
    # Whether the mesh is CURRENTLY dirty, unverified, or carrying a runtime-reported fault —
    # read after an escalation runs to tell a rung that recovered the mesh from one that merely
    # ran (climbed to a further rung, or found none left): every recovering rung clears all
    # three, and nothing else does, so this is the one signal common to every path that can
    # recover, not just the one (_reset_and_verify_device) that happens to also flip a
    # RecoveryMechanism-wide flag another, unrelated reset could have set.
    device_degraded: Callable[[], bool]
    # The two host-level rungs' OTHER half — GalaxyRecovery gets _auto_reboot_host (it is
    # Galaxy-only in practice); the chassis power cycle and the loud whole-bus event stay
    # server-owned (the gate journals that event itself, and the power cycle's ledger row and job
    # entry are server state), so they are injected rather than moved.
    auto_power_cycle_host: Callable[[Callable, str], Awaitable[None]]
    emit_all_off_bus_power_cycle_required: Callable[[Callable, int, int, str], None]
    host_escalation_kwargs: Callable[[], dict]
    # Probe primitives that already live in the health package (health.monitors.heartbeat) but
    # that server.py re-exports by import and tests monkeypatch on ``server.<name>`` to drive the
    # moved bodies' behavior deterministically — see the class docstring. Real server-side callers
    # of these three remain (the gate, the startup probe, the exec/reset tools), which is what
    # distinguishes them from e.g. ``reset_chip_via_bridge`` or ``_fire_ubb_reset``, which have
    # none and so are imported directly where the moved bodies need them instead.
    read_heartbeats: Callable[[], dict]
    heartbeat_supported: Callable[[], bool]
    heartbeat_verdict: Callable[..., tuple]
    # A timing knob a unit test collapses to 0 so it never really sleeps; late-bound for the same
    # reason as everything else here. Distinct from the other two probe-timing constants
    # (BRIDGE_RESET_SETTLE_SEC, POST_RESET_FABRIC_RETRIES/_SLEEP_SEC), which moved into this
    # package and are no longer injected: this one gates the gate's OWN gone-chip detection too
    # (server.py), so it still needs a real remaining reader there.
    gone_chip_confirm_settle_sec: Callable[[], float]
    # select_recovery's mode derivation. Default None, not required of the caller: all three read
    # state that lives on the HealthMonitor built AFTER this bag exists, so ServerFsm.boot wires
    # them against the monitor it constructs (a caller that already has a monitor — the conftest
    # deps fixture — may supply its own bindings, which boot leaves untouched).
    board_types_provider: Optional[Callable[[], Optional[list]]] = None
    glx_board_types_provider: Optional[Callable[[], tuple]] = None
    # {chip index: PCI address}, the map the tray rung derives its trays from (spec 04 I16). Same
    # injection reason as the two above: the cache lives on the HealthMonitor singleton.
    chip_buses_provider: Optional[Callable[[], Optional[dict]]] = None
    journal_skip_once: Optional[Callable[[str, str], None]] = None
    # The telemetry sampler's server-side callbacks (see tt_device_mcp.telemetry). Same lambda
    # discipline as every field above — each re-resolves a server.py name per call so a
    # monkeypatched fake still takes effect. Default None only because the conftest deps fixture
    # predates the sampler and never constructs one; ServerFsm.boot always receives real values.
    chip_sample: Optional[Callable[[], dict]] = None
    append_trace: Optional[Callable[[dict], None]] = None
    capture_incident: Optional[Callable[..., object]] = None
    isolate_chip: Optional[Callable[[str], bool]] = None
    chip_pci_bdf: Optional[Callable[[str], Optional[str]]] = None
    health_event: Optional[Callable[..., None]] = None
    kill_device_holders: Optional[Callable[[str], Awaitable[list]]] = None
    mark_device_dirty: Optional[Callable[..., None]] = None
    refresh_idle_hold_ledger: Optional[Callable[[], None]] = None
    spawn_idle_relift: Optional[Callable[[], None]] = None
    # The sampler's two FSM reads. Late-bound like everything else here — NOT the fsm reference
    # itself: srv.fsm is the one singleton the test fixtures rebind wholesale (a fresh durable
    # record per test), and a captured reference would keep judging a previous test's episode.
    # episode_dirty is the record's dirty axis, NOT state-is-healthy: a held-not-dirty box that
    # then loses every sysfs node must still journal the loss and escalate the episode to dirty —
    # only an already-dirty one has nothing left to re-flag.
    episode_dirty: Optional[Callable[[], bool]] = None
    # Whether any episode is open (state not HEALTHY). A short chip count is flagged only on a
    # HEALTHY box: under a hold the gate already placed (an off-bus drop held dirty=False) the
    # short count IS that fault, and re-dirtying it would disable the hold's idle relift.
    episode_open: Optional[Callable[[], bool]] = None
    episode_job: Optional[Callable[[], dict]] = None


class Recovery(ABC):
    """Platform-specific recovery on top of the platform-invariant ledger/cooldown/scoped-exec
    machinery (:class:`RecoveryMechanism`, held as ``self.mechanism`` — composition, not
    inheritance, so the mechanism's process-lifetime state is never reset by constructing a
    ``Recovery``; see the module docstring in ``base.py``): which rung is next (``next_stage``),
    which reset command fits this host (``reset_argv``), and the platform-invariant verify/reset
    loop (``_verify_device`` and friends) shared by both platforms."""

    def __init__(self, monitor, mechanism: RecoveryMechanism, deps: RecoveryDeps) -> None:
        # The HealthMonitor aggregate (server.py's ``health_monitor`` singleton) that supplies
        # next_stage/escalate's live evidence.
        self.monitor = monitor
        # THE recovery-mechanism singleton (server.py's ``recovery_mechanism``) — never a private
        # copy. Every Recovery instance selected for any pass shares this one object, which is
        # what lets a cooldown armed while one platform was selected still gate a reset attempted
        # after selection later resolves to the other.
        self.mechanism = mechanism
        self.deps = deps
        # Set fresh by every _recover_isolated_chips call; defined here so the gate can read it
        # even when no bridge rung has run on this instance yet.
        self.last_bridge_reset_reasons: dict = {}

    @abstractmethod
    def next_stage(self, ev: Evidence) -> str:
        """The single gentlest-first rung ``ev`` justifies: a stage name, or WAIT/DEFER."""
        ...

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
        """Run the next justified rung and report the outcome (one of OUTCOME_*). ``indices``/
        ``expected`` are the caller's own single read of the mesh, passed through unchanged —
        see ``GalaxyRecovery.escalate``, which actually dispatches on them. ``stage``/``ev``/
        ``beats`` belong to its ``"gate"`` phase alone (the rung the gate's own ``next_stage``
        already chose, and the evidence it was chosen from); nothing here reads them.

        Base ``Recovery`` has no host-level escalation ladder to run (that ladder is
        Galaxy-specific — see ``GalaxyRecovery.escalate``). A per-target host has nothing to
        escalate TO regardless of ``phase``, so this always reports WAITING: not "an action ran
        and failed" (TERMINAL would say the ladder tried and could not help) but "there is no
        ladder here to try" — a caller must not read WAITING from this platform as a justification
        to climb further, only as license to keep doing whatever it already does for an
        unescalatable hold."""
        return OUTCOME_WAITING

    def reset_argv(self, indices: list) -> list:
        """The ``tt-smi`` reset command for this platform. ``TT_DEVICE_MCP_RESET_ARGS`` (a full
        argv override) always wins, ahead of the platform's own command."""
        override = os.environ.get("TT_DEVICE_MCP_RESET_ARGS", "").strip()
        if override:
            return override.split()
        return self._platform_reset_argv(indices)

    @abstractmethod
    def _platform_reset_argv(self, indices: list) -> list:
        """This platform's reset argv, ignoring the env override (``reset_argv`` handles that)."""
        ...

    # ---- platform-invariant verify/reset loop --------------------------------------------------
    #
    # Shared by both platforms: the actual reset command comes from `select_recovery(self.monitor,
    # self.mechanism, self.deps)` — re-resolved fresh, not `self.reset_argv` — so a call reached
    # through a fixed platform reference (e.g. GalaxyRecovery's own escalation ladder) still fires
    # the command that fits the host ACTUALLY detected this pass, not the platform ``self`` happens
    # to be.

    async def _verify_device(
        self,
        expected: int,
        log,
        *,
        run_fabric: bool = True,
        phase: str = "verify_device",
        run_eth: bool = False,
        fabric_stale: bool = True,
    ) -> tuple[bool, dict]:
        """Is the mesh usable? A thin adapter over :meth:`HealthMonitor.update` — the ONE probe
        pass implementation (see monitor.py); this method exists only as the seam
        ``patch_recovery("_verify_device", ...)`` replaces wholesale in ~56 tests, and as the one
        place ``expected``/``log`` translate into ``update()``'s own argument names. No probe
        logic lives here anymore: ``update()`` does the gentlest-first short-circuiting (a frozen
        heartbeat or a failed snapshot skips every heavier check below it) and the per-probe
        ``set_device_op_detail``/log lines this method used to own directly.

        ``phase`` defaults to a placeholder for the callers with no gate phase of their own (the
        idle relift, the exec/reset tools); the gate is the one caller that has a real one
        ('pre-job'/'post-job'/'startup') and passes it, so the ``HealthState`` this stores on
        ``self.monitor.status()`` labels itself meaningfully instead of always reading the same
        placeholder string.
        """
        state = await self.monitor.update(
            phase, run_fabric=run_fabric, run_eth=run_eth, fabric_stale=fabric_stale, expected=expected, log=log
        )
        return state.healthy, state.as_evidence()

    async def _recover_isolated_chips(self, log) -> bool:
        """Bring back chips that were cut out of the kernel after leaving the PCIe bus.

        Reset through the parent bridge, not the chip: a dead endpoint cannot be reset through
        its own config space, but it does not need to be, and the bridge above it always answers.
        Each Blackhole sits alone behind its own bridge here, so this is surgical — measured on
        live hardware, the other 31 chips keep ticking and a fabric traffic pass still passes
        afterwards, so no galaxy-wide reset is needed to make the mesh usable again.

        Caller MUST hold the device lock: this ends with a PCI rescan, and nothing else may be
        touching the device while the endpoint re-enumerates.
        """
        # ladder-v2: the per-chip SBR window this rung reads, chip id -> {"reason": "no_bridge"} for
        # every chip whose reset could not fire because the endpoint left the bus. Reset FRESH every
        # call (before the empty-set early return), so a later gate never classifies a hold on a
        # stale prior pass; the server threads it onto Evidence.bridge_reset_failed after this fires.
        self.last_bridge_reset_reasons: dict = {}
        isolated_chips = self.deps.isolated_chips()
        if not isolated_chips:
            return True
        targets = sorted(isolated_chips, key=int)
        _t0 = datetime.now()
        self.deps.begin_action_row("[broker]bridge-reset", f"bridge-reset chip(s) {','.join(targets)}")
        self.deps.set_device_op_detail(f"recovering chip(s) {','.join(targets)} " f"via bridge reset (~10s)")
        device_pci_map = self.deps.device_pci_map()
        recovered, lost, inapplicable = [], [], []
        # The SBR write needs root and setpci (spec 04 I17). Decided once, ahead of the loop,
        # because discovering it per chip means reading a permission failure the call site cannot
        # tell from a bad bridge — which is classified retryable, so the loop spends its whole
        # attempt budget on a write the kernel will never allow. The rescan below still runs: it
        # needs no setpci.
        sbr = bridge_reset_enabled()
        if not sbr:
            log(f"per-chip bridge reset unavailable ({bridge_reset_unavailable_reason()}) — trying a PCI rescan")
        # An SBR takes its chip off the bus like any reset, so the per-host gate (spec 04 I24) holds it
        # too: the isolated chips are off the bus by definition.
        gated, why = False, ""
        if sbr:
            allowed, why = pcie_guard.host_reset_gate(len(targets))
            gated = not allowed
        if gated:
            log(f"per-chip bridge reset NOT fired: {why} — trying a PCI rescan")
            health_event("host_reset_gated", context="bridge_reset", chips=targets, reason=why, host_at_risk=True)
        for idx in targets:
            if gated:
                inapplicable.append(idx)
                self.last_bridge_reset_reasons[idx] = {"reason": "gated"}
                metrics.stage_fired("bridge_reset", "blocked")
                continue
            if not sbr:
                inapplicable.append(idx)
                # NOT no_bridge: that means the endpoint left the bus, which is the drop measured
                # never to return and the one _classify_hold escalates straight to a power cycle
                # on. Nothing was written here, so the window is unproven — a distinct reason
                # keeps it out of that class rather than licensing the sweep on an untested chip.
                self.last_bridge_reset_reasons[idx] = {"reason": "no_privilege"}
                # blocked, not not_applicable: metrics.STAGE_OUTCOMES reserves not_applicable for
                # a platform fact that will never change. A missing euid or package is the most
                # operator-fixable thing here, and a fleet needs to see it move.
                metrics.stage_fired("bridge_reset", "blocked")
                continue
            bdf = device_pci_map.get(idx)
            if not bdf:
                lost.append(idx)
                # No PCI address cached for this chip at all — structurally cannot even attempt
                # a bridge write, the same "nothing to fire at" as reset_chip_via_bridge's own
                # no_bridge case below. A topology fact, not an operator-flippable guard — see
                # metrics.STAGE_OUTCOMES for why this is not_applicable, not blocked.
                self.last_bridge_reset_reasons[idx] = {"reason": "no_bridge"}
                metrics.stage_fired("bridge_reset", "not_applicable")
                continue
            log(f"recovering chip {idx} ({bdf}) via a reset of its parent bridge")
            # Retry before giving up: the first shot often races the link retrain, and the
            # settle between tries is the difference between a re-bind here and a galaxy reset.
            # A None result is different in kind: the rung could not be issued at all (no bridge
            # to write to — the endpoint left the bus — or setpci is gone), so every retry re-runs
            # the identical no-op. Firing a rung that cannot work here only delays the next one, so
            # record the chip inapplicable and fall through now (the incident burnt ~64s spinning
            # a no_bridge reset 3x per gone chip, then again on the stuck-hold escalation).
            for attempt in range(BRIDGE_RESET_MAX_TRIES):
                result = await asyncio.to_thread(reset_chip_via_bridge, idx, bdf)
                if result:
                    recovered.append(idx)
                    metrics.stage_fired("bridge_reset", "ok")
                    break
                if result is None:
                    inapplicable.append(idx)
                    # No bridge to write to (the endpoint left the bus) or setpci is gone — either
                    # way structurally cannot fire here, not a policy decline; see the "no bdf"
                    # branch above for why that makes this not_applicable rather than blocked.
                    # Record it as the no-window signal the tray-stage classifier keys on: a chip
                    # with no bridge to reset through is exactly the drop no SBR ever recovers.
                    self.last_bridge_reset_reasons[idx] = {"reason": "no_bridge"}
                    metrics.stage_fired("bridge_reset", "not_applicable")
                    break
                if attempt + 1 < BRIDGE_RESET_MAX_TRIES:
                    await asyncio.sleep(BRIDGE_RESET_SETTLE_SEC)
            else:
                lost.append(idx)
                metrics.stage_fired("bridge_reset", "failed")

        for idx in recovered:
            isolated_chips.discard(idx)
        # An inapplicable chip is NOT recovered — it stays isolated and forces the not-recovered
        # branch, so an all-gone mesh (every chip no_bridge) can never read as healed and release.
        still_dead = sorted(set(lost) | set(inapplicable), key=int)
        health_event(
            "chip_recovery", recovered=recovered, still_dead=still_dead, inapplicable=sorted(inapplicable, key=int)
        )
        # A bridge reset takes chips off the bus and back exactly like a tt-smi reset does, so
        # it owes the jobs list the same visible row — otherwise the cheap recovery that runs
        # most often is the one reset nobody can see after the fact.
        self.deps.write_action_log(
            "[broker]bridge-reset",
            f"bridge-reset chip(s) {','.join(targets)}",
            (datetime.now() - _t0).total_seconds(),
            "failed" if still_dead else "completed",
            len(still_dead),
        )

        if still_dead:
            # Cheapest rung there is, and the one that was missing: a bare PCI rescan. A chip with no
            # parent bridge is not necessarily a departed ASIC — measured twice on this host, the
            # endpoint was still in lspci AND still bound to the driver, and only its /dev node was
            # gone. The gate infers "off the PCIe bus" from the sysfs node count, so that state is
            # indistinguishable from a real drop and routed straight past every cheap remedy to a
            # reboot or a chassis power cycle. A rescan re-enumerates it in about a second with no
            # downtime; both times it restored 8/8. Try it before anything that costs a tenant.
            try:
                await asyncio.to_thread(Path("/sys/bus/pci/rescan").write_text, "1")
                await asyncio.sleep(3)
            except OSError as e:
                log(f"PCI rescan could not run ({e}); falling through to the heavier rungs")
            else:
                back = [i for i in still_dead if Path(f"/sys/class/tenstorrent/tenstorrent!{i}").exists()]
                if back:
                    for idx in back:
                        isolated_chips.discard(idx)
                    still_dead = [i for i in still_dead if i not in back]
                    health_event("chip_recovery_by_rescan", recovered=back, still_dead=still_dead)
                    log(f"chip(s) {back} came back on a PCI rescan — no reset, no reboot, no power cycle")
                    self.deps.write_action_log(
                        "[broker]pci-rescan", f"pci rescan recovered chip(s) {','.join(back)}", 0.0, "completed", 0
                    )
                if not still_dead:
                    return True
            if inapplicable:
                log(
                    f"chip(s) {sorted(inapplicable, key=int)} have no bridge to reset — they left "
                    f"the bus, so the per-chip rung cannot reach them; escalating to a heavier rung."
                )
            if lost:
                log(
                    f"chip(s) {lost} did NOT come back from a bridge reset after "
                    f"{BRIDGE_RESET_MAX_TRIES} tries; they stay out of the kernel so they cannot take "
                    f"the host down. Escalating to a galaxy reset — they re-enumerate on the next PCI "
                    f"rescan, no power-cycle required."
                )
            return False
        log(f"chip(s) {recovered} are back on the bus and bound to the driver")
        return True

    def _fabric_ran_but_unverified(self, evidence: dict) -> bool:
        """A 77: the fabric traffic pass RAN but tested no trained link — the ``fabric`` key is present
        with ``ok=None``. A verify that never ran the fabric has no ``fabric`` key and is NOT a 77.
        Reading the two apart is what keeps a training-window verdict from passing as a cleared fabric."""
        f = evidence.get("fabric")
        return f is not None and f.get("ok") is None

    async def _verify_device_after_reset(self, expected: int, log) -> tuple[bool, dict, int]:
        """Verify a mesh after a reset, giving a post-reset fabric 77 time to settle before it is believed.

        A reset returns before the eth links re-train, so the first fabric pass can find no trained link
        (a 77 — a training-window verdict, not a cleared or a broken fabric). Re-check up to
        ``POST_RESET_FABRIC_RETRIES`` times so it resolves to a REAL verdict; multi-chip only, while
        enum+ARC pass. A fabric that STILL cannot verify after training (a persistent 77) is an eth/fabric
        wedge the reset did not clear — report it NOT healthy so the caller climbs to the next rung instead
        of clearing a hold onto a fabric no pass ever proved (the blx04 shape: enum+ARC read OK while every
        full-mesh job died on a down link). Returns ``(healthy, evidence, fabric_retries)``."""
        healthy, evidence = await self._verify_device(expected, log)
        retries = 0
        while (
            self._fabric_ran_but_unverified(evidence)
            and healthy
            and expected > 1
            and retries < POST_RESET_FABRIC_RETRIES
        ):
            retries += 1
            log(
                f"post-reset fabric found no trained link (77) — waiting {POST_RESET_FABRIC_RETRY_SLEEP_SEC:.0f}s "
                f"for eth training, then re-checking (retry {retries}/{POST_RESET_FABRIC_RETRIES})"
            )
            await asyncio.sleep(POST_RESET_FABRIC_RETRY_SLEEP_SEC)
            healthy, evidence = await self._verify_device(expected, log)
        if healthy and expected > 1 and self._fabric_ran_but_unverified(evidence):
            log(
                "post-reset fabric still could not verify after eth-training retries — treating the reset as "
                "NOT recovered (the eth/fabric wedge survived; the caller climbs, it does not clear)"
            )
            healthy = False
        return healthy, evidence, retries

    async def _reset_and_verify_device(self, indices: list, log) -> bool:
        """Recover the device with ONE reset, performed safely, then prove it worked.

        Safety here is structural, not statistical — the reset is exclusive (the caller
        holds ``device_op_lock``), lands on a bus quiesced of pollers, and runs in its
        restart-safe systemd or local backend so nothing can interrupt it. A reset done
        that way is a cheap, routine repair of an idle device, so we do not hesitate to
        run it.

        What we do NOT do is run it twice in a row. A reset that did not revive the mesh
        will not revive it on an immediate retry, and every extra reset is another pass
        of MMIO at a dead endpoint — the exact traffic that promotes a PCIe error to
        fatal and takes the host down with it. One attempt, then back off and let the
        caller report honestly. Returns True if the device verified healthy.
        """
        try:
            return await self._reset_and_verify_device_once(indices, log)
        finally:
            # The reset and its verify are over and the host is still up: an off-bus intent is spent.
            pcie_guard.end_offbus_reset()

    async def _reset_and_verify_device_once(self, indices: list, log) -> bool:
        # Reset per call: only a reset that actually EXITS non-zero this pass sets it below. An adopted
        # foreign scope or a clean-exit-but-unverified reset must not leave a stale hard-fail flag that
        # steers the next gate's escalation to the cold rung over a reset that never hard-failed.
        self.mechanism.last_reset_exit_nonzero = False
        argv = select_recovery(self.monitor, self.mechanism, self.deps).reset_argv(indices)
        expected = self.monitor.expected(len(indices))

        if await self.mechanism.await_foreign_scope(log):
            # A foreign scope (a prior gate's reset, or one still cycling after a broker restart) ran
            # the reset; we waited it out above, so it is a COMPLETED reset. Arm the cooldown exactly
            # as the own-reset path does below: without this a failed adopt leaves the clock
            # untouched and the next gate fires another back-to-back galaxy reset — the repeat MMIO at
            # a dead endpoint the cooldown exists to prevent.
            self.mechanism.last_reset_monotonic = time.monotonic()
            healthy = (await self._verify_device(expected, log))[0]
            self.mechanism.last_reset_failed = not healthy
            return healthy

        # Unknown only when tt-smi could not be read on a multi-chip host, so `-r` is a guess that
        # no-ops on a Galaxy. Fire it anyway — a no-op still lets the cascade climb to a rung that
        # recovers — but never let a reset that cannot recover the mesh land silently.
        if expected > 1 and not reset_mode_known(self.deps, self.mechanism):
            health_event("reset_mode_unknown", argv=argv, expected_chips=expected, host_at_risk=True)
            log(
                f"WARNING: {expected}-chip host, reset mode could not be derived — issuing per-target "
                "`-r`, which does NOT recover a Galaxy; set TT_DEVICE_MCP_RESET_MODE"
            )

        # The per-host gate (spec 04 I24): on a host whose resets have flooded AER into a crash, an
        # automatic reset never fires over chips already off the bus or during a flood, and the
        # Tenstorrent root ports are masked for the reset window. Off by default. Checked before
        # reset_begin and the cooldown clock: a refused reset did not happen.
        off_bus = pcie_guard.chips_off_bus(expected)
        allowed, why = pcie_guard.host_reset_gate(off_bus)
        if not allowed:
            log(f"automatic reset NOT fired: {why}; holding for an operator")
            health_event("host_reset_gated", argv=argv, reason=why, host_at_risk=True)
            return False

        log(f"resetting device: {' '.join(argv)}  ({expected} device(s); ~30-60s, restart-safe)")
        health_event("reset_begin", argv=argv, expected_chips=expected)
        self.mechanism.last_reset_monotonic = time.monotonic()
        pcie_guard.begin_offbus_reset("mesh reset", off_bus)
        aer_mask = pcie_guard.mask_for_mesh_reset(log)
        try:
            rc, out = await self.mechanism.reset_with_quiesce(argv, log)
        finally:
            if aer_mask is not None:
                await asyncio.to_thread(aer_mask.restore)
        if _journal_cpld_too_old(argv, out, log):
            # Latched on the mechanism both platforms share, so the NEXT rung and every later pass
            # resolve to the galaxy ladder rather than repeating the `-r` tt-smi just disowned.
            self.mechanism.cpld_forces_galaxy = True

        if rc is None or rc != 0:
            log(f"reset {'timed out' if rc is None else f'exited {rc}'}; device not recovered")
            health_event("reset_failed", rc=rc, argv=argv)
            # rc is None means the reset either timed out with its scope still running (we never
            # kill it mid-reset — the next gate adopts it via await_foreign_scope and
            # verifies) or never launched. In NEITHER case did MMIO reach a dead endpoint, so the
            # cooldown — which exists only to stop repeat resets hammering a dead endpoint into
            # the host-rebooting path — is not owed. Arming it on a still-in-flight reset would
            # suppress the very re-verify that confirms recovery, stretching a self-healing drop
            # into a 10 min dead window. Only a real non-zero EXIT arms it.
            if rc is not None:
                self.mechanism.last_reset_failed = True
                self.mechanism.last_reset_exit_nonzero = True
            metrics.stage_fired("smi_reset", "timeout" if rc is None else "failed")
            return False

        # rc == 0: the reset command itself completed, independent of whatever the verify below
        # finds — a stage's own firing outcome is about the ACTION, not the fault it was meant to
        # clear, so this always reports "ok" whether or not the device comes back healthy.
        metrics.stage_fired("smi_reset", "ok")
        healthy, evidence, retries = await self._verify_device_after_reset(expected, log)
        health_event(
            "reset_verified" if healthy else "reset_ineffective",
            rc=rc,
            healthy=healthy,
            evidence=evidence,
            fabric_retries=retries,
        )
        self.mechanism.last_reset_failed = not healthy
        if healthy:
            log("reset complete + health verified")
            return True

        log(
            f"reset exited 0 but the device did NOT come back; not resetting again for "
            f"{RESET_COOLDOWN_SEC // 60} min (repeat resets at a dead endpoint are what take the host down)"
        )
        return False


# tt-smi prints this before attempting `-r` on a Galaxy whose CPLD firmware predates v1.16.
# Matched loosely on the version and the flag rather than the whole sentence, so a reworded
# banner still trips it — a missed match silently restores the old behaviour of discarding the
# only warning the host gives.
_CPLD_TOO_OLD_RE = re.compile(r"CPLD\s+FW\s+v?1\.16.*tt-smi\s+-r", re.IGNORECASE | re.DOTALL)


def _journal_cpld_too_old(argv: list, out: str, log) -> bool:
    """Record that this host's `tt-smi -r` is the wrong reset for it, and why.

    On a Galaxy whose CPLD firmware is older than v1.16 a `-r` does not merely fail: the chips
    re-enumerate and then every register read returns 0xffffffff until a `-glx_reset` runs. tt-smi
    says so on stdout before it tries, and that output already reaches us — it was being
    discarded, so the one warning the host gives about a reset that strands it went nowhere.

    The banner is also the answer to the reset-mode question, not merely a warning about it: tt-smi
    prints it ONLY on a Galaxy, so its presence is positive evidence of the board class — and it
    arrives on exactly the hardware where `_is_galaxy` cannot tell, since a degraded Galaxy is
    where tt-smi reports "N/A" board types. I15 forbids firing the mesh-wide reset on a *guess*;
    this is the host's own statement about itself, and it is journaled loudly either way.

    Returns whether the banner was present. The caller latches it onto the shared mechanism; this
    function only reads and records, so it stays safe to call from anywhere.
    """
    if not out or not _CPLD_TOO_OLD_RE.search(out):
        return False
    log(
        "WARNING: tt-smi reports this host's CPLD FW is older than v1.16, so `tt-smi -r` does not "
        "recover it and leaves register reads returning 0xffffffff until a `-glx_reset` runs — "
        "declare TT_DEVICE_MCP_RESET_MODE=galaxy and have the CPLD updated"
    )
    health_event("reset_cpld_too_old", argv=list(argv), host_at_risk=True)
    return True


RESET_MODE_GALAXY = "galaxy"
RESET_MODE_TARGET = "per-target"


def _declared_reset_mode(journal_skip_once: Callable[[str, str], None]) -> Optional[str]:
    """The operator's declared mode, or None if undeclared. A value naming no known mode is not a
    declaration — silently reading it as per-target is how a typo ships the reset that cannot
    recover a Galaxy, and it would override a correct derivation. ``loudbox`` is accepted as the
    host class it names: operators declare what the box IS, and a loudbox resets per PCIe target."""
    mode = os.environ.get("TT_DEVICE_MCP_RESET_MODE", "").strip().lower()
    if mode == "loudbox":
        return RESET_MODE_TARGET
    if mode in (RESET_MODE_GALAXY, RESET_MODE_TARGET):
        return mode
    if mode:
        journal_skip_once(
            "reset_mode_invalid",
            f"TT_DEVICE_MCP_RESET_MODE={mode!r} names no known mode "
            f"({RESET_MODE_GALAXY}|{RESET_MODE_TARGET}|loudbox) — deriving instead",
        )
    return None


# Imported at the bottom, after Recovery/Evidence/WAIT/DEFER are already bound above: galaxy.py and
# per_target.py each import those names straight from this package (`from
# tt_device_mcp.health.recovery import Recovery, Evidence, ...`), and doing it here — rather than
# at the top of this file — is what lets that work without a circular-import error, since this
# module is still mid-initialization when they run.
from tt_device_mcp.health.recovery.galaxy import GalaxyRecovery, _is_galaxy  # noqa: E402
from tt_device_mcp.health.recovery.per_target import PerTargetRecovery  # noqa: E402


def reset_mode_known(deps: RecoveryDeps, mechanism: "Optional[RecoveryMechanism]" = None) -> bool:
    """Whether the reset command is known to fit this host — declared, latched from tt-smi's own
    CPLD banner, or derived from a cached snapshot. Unknown only when none of those answered on a
    multi-chip host; the caller keeps its own ``reset_mode_unknown`` journaling (see
    ``Recovery._reset_and_verify_device``) — this only answers the predicate, so that event site
    is not duplicated here.

    ``mechanism`` is optional so a caller that has no reset state to consult (a bare predicate
    check, a test) still works. Passing it is what lets the banner latch count: once tt-smi has
    said this host is a Galaxy whose `-r` cannot recover it, the mode is no longer unknown and
    the loud unknown-mode warning would be repeating a question that has been answered."""
    return bool(
        os.environ.get("TT_DEVICE_MCP_RESET_ARGS", "").strip()
        or _declared_reset_mode(deps.journal_skip_once)
        or (mechanism is not None and mechanism.cpld_forces_galaxy)
        or _is_galaxy(deps.board_types_provider(), deps.glx_board_types_provider()) is not None
    )


# The one instance of each platform class this process ever constructs, keyed on the identity of
# the (monitor, mechanism, deps) triple it was built from — see select_recovery. Production passes
# the SAME three objects on every call (they are module-level singletons in server.py), so this
# holds exactly one GalaxyRecovery and one PerTargetRecovery for the process's whole lifetime; a
# test that builds its own monitor/mechanism/deps for isolation gets its own instance rather than
# a stale one a different test's fixtures left behind.
_recovery_instances: dict[tuple, Recovery] = {}


def _persistent_recovery(cls: type, monitor, mechanism: RecoveryMechanism, deps: RecoveryDeps) -> Recovery:
    """The one ``cls`` instance for this (monitor, mechanism, deps) triple, constructing it on
    first use and reusing it after. This is what makes ``select_recovery``'s per-pass platform
    choice safe to call every pass without losing the mechanism's cooldown/reset-in-flight state:
    the object identity is stable, only which one gets returned changes."""
    key = (cls, id(monitor), id(mechanism), id(deps))
    inst = _recovery_instances.get(key)
    if inst is None:
        inst = cls(monitor, mechanism, deps)
        _recovery_instances[key] = inst
    return inst


def select_recovery(monitor, mechanism: RecoveryMechanism, deps: RecoveryDeps) -> Recovery:
    """The :class:`Recovery` for this host, chosen fresh every pass: a declared
    ``TT_DEVICE_MCP_RESET_MODE`` wins; otherwise derive from the cached board types (never probed
    here — see ``_is_galaxy``); with neither, default to the per-target ladder, the conservative
    choice — it never fires the mesh-wide reset a misidentified Galaxy would need. Reads only
    cached state, so it is cheap enough to call every pass rather than once at startup, when the
    board types are not cached yet.

    The INSTANCE returned is persistent (see ``_persistent_recovery``) — only the choice of which
    platform's instance to hand back is made fresh every call, never the object itself or the
    ``RecoveryMechanism`` state it shares with every other platform's instance. A cooldown armed
    while Galaxy was selected is therefore still in force on a later pass that resolves to
    per-target: same mechanism, whichever wrapper is asking."""
    declared = _declared_reset_mode(deps.journal_skip_once)
    if declared == RESET_MODE_GALAXY:
        return _persistent_recovery(GalaxyRecovery, monitor, mechanism, deps)
    if declared == RESET_MODE_TARGET:
        return _persistent_recovery(PerTargetRecovery, monitor, mechanism, deps)
    # tt-smi's own banner (see _journal_cpld_too_old) outranks a board-type derivation but not an
    # operator's declaration: they may be deliberately holding a host on the per-target ladder,
    # and this is evidence, not an override.
    if mechanism.cpld_forces_galaxy:
        return _persistent_recovery(GalaxyRecovery, monitor, mechanism, deps)
    if _is_galaxy(deps.board_types_provider(), deps.glx_board_types_provider()):
        return _persistent_recovery(GalaxyRecovery, monitor, mechanism, deps)
    return _persistent_recovery(PerTargetRecovery, monitor, mechanism, deps)
