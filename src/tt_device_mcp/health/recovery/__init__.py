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
from tt_device_mcp.device_holders import HolderScan, is_tenant
from tt_device_mcp.health.aiclk_ceiling import CEILING
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors import fabric, pci
from tt_device_mcp.health.monitors.heartbeat import (
    arc_live_chips,
    arc_not_ready_chips,
    heartbeat_attr_present,
    offbus_count,
)
from tt_device_mcp.health.pci_writer import PCI_WRITER, PciWedged
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base as _base
from tt_device_mcp.health.recovery import pcie_guard
from tt_device_mcp.health.recovery.base import (
    RESET_COOLDOWN_SEC,
    RecoveryMechanism,
    drop_returned_isolated,
    pci_rescan,
    pci_wedged_refusal,
    reenumerate_withheld,
    refuse_mesh_reset,
)
from tt_device_mcp.health.recovery.stages.bridge_reset import (
    SBR_RETRAIN_SETTLE_SEC,
    bridge_reset_enabled,
    bridge_reset_unavailable_reason,
    reset_chip_via_bridge,
    reset_removed_chip,
)

# A `next_stage` decision that is not a stage name: WAIT (the wedge may self-heal; do nothing),
# DEFER (a reset is already cycling; adopt and verify it, start nothing new), RELEASE (the door
# opens — nothing is wrong, or the last action just proved it), HOLD_FABRIC_UNVERIFIED (a fabric
# pass that ran but reached no verdict; hold without resetting), BLOCKED (a drop no opted-in
# rung can recover — distinct from WAIT, which means "it may self-heal"), or BELOW_FLOOR (fewer
# chips are gone than the mesh-wide reset needs to be worth its blast radius: classify the drop —
# a whole tray goes straight to the power cycle, anything else waits for the hold bound's ladder). The stage names below
# are constants.STAGE_NAMES' own values, re-bound here (not re-exported by import: pyflakes reads
# a bare re-export as an unused import) so a decision routes straight to the name
# metrics.stage_fired() and _HOST_ESCALATION_STAGE use.
WAIT = "wait"
DEFER = "defer"
RELEASE = "release"
HOLD_FABRIC_UNVERIFIED = "hold_fabric_unverified"
BLOCKED = "blocked"
BELOW_FLOOR = "below_floor"
STAGE_BRIDGE_RESET = constants.STAGE_BRIDGE_RESET
STAGE_SMI_RESET = constants.STAGE_SMI_RESET
STAGE_POWER_CYCLE = constants.STAGE_POWER_CYCLE

# Recovery.escalate's outcome.
OUTCOME_RECOVERED = "recovered"
OUTCOME_WAITING = "waiting"
OUTCOME_TERMINAL = "terminal"


def bridge_reset_call_timeout() -> float:
    """Bound on one reset_chip_via_bridge call: its three setpci calls (15 s each), the link-retrain
    settle, its rescan (PCI_SYSFS_WRITE_TIMEOUT_SEC) and its 10 s re-enumeration poll, plus slack."""
    return 3 * 15 + SBR_RETRAIN_SETTLE_SEC + _base.PCI_SYSFS_WRITE_TIMEOUT_SEC + 10 + 5


def _sysfs_rescan() -> None:
    """The bare sysfs rescan write, scoped to the Tenstorrent root ports; pci_rescan bounds it."""
    pcie_guard.rescan_tt_root_ports()


# A surgical bridge reset fired seconds after a chip leaves the bus can lose the race with the
# endpoint's link retrain and report failure, yet the same chip re-binds in ~2s once the link
# settles. Retry a few times before giving up on it (see Recovery._recover_isolated_chips) — this
# is what turns a ~13 min recovery into a ~seconds one.
BRIDGE_RESET_MAX_TRIES = 3
BRIDGE_RESET_SETTLE_SEC = 4
# The bridge rung's reason for a removed chip whose every recorded-bridge SBR fired and left it off
# the bus (spec 04 I69): no SBR brings it back, the same no-window fact as no_bridge.
NO_WINDOW_REMOVED_REASON = "removed_not_reenumerated"
# Spec 04 I94: a chip the bridge rung brought back counts recovered only once its ARC heartbeat reads
# as a counter. A live chip reads on the first try; this bounds the wait for one whose ARC is still
# coming up. One that stays dead stays isolated, so the off-bus one-shot climbs past it.
BRIDGE_RESET_ARC_POLLS = 10
BRIDGE_RESET_ARC_POLL_SEC = 0.5
BRIDGE_RESET_ARC_WAIT_SEC = BRIDGE_RESET_ARC_POLLS * BRIDGE_RESET_ARC_POLL_SEC
# A KMD without the heartbeat attr cannot answer the I94 check: it is skipped there, said once.
_arc_check_skip_logged = False

# A reset returns before the eth links re-train, so the immediate post-reset fabric pass can find
# no trained link (77). Retry (see Recovery._verify_device_after_reset), giving the links time to
# train, so the reset resolves to a REAL verdict rather than a transient 77 that reads as
# "recovered" onto an unverified fabric. A 77 that persists past the retries is UNKNOWN: the reset is
# neither a recovery nor a failure, and the mesh is held fabric-unverified (spec 03 I34/I40 bound it).
POST_RESET_FABRIC_RETRIES = int(os.environ.get("TT_DEVICE_MCP_POST_RESET_FABRIC_RETRIES", "1"))
POST_RESET_FABRIC_RETRY_SLEEP_SEC = float(os.environ.get("TT_DEVICE_MCP_POST_RESET_FABRIC_SLEEP_SEC", "60"))
# A mesh reset that exits non-zero with every chip back on the bus but some ARC not ready gets ONE more
# reset, after this settle, on an idle box (:meth:`Recovery._arc_not_ready_retry_due`). Measured on a
# 32-chip Galaxy: the first -glx_reset brought an off-bus chip back but left 31 chips ARC-not-ready and
# exited 1; the hold re-observed that for 10 min, and the next -glx_reset recovered it at once.
ARC_NOT_READY_RETRY_SETTLE_SEC = float(os.environ.get("TT_DEVICE_MCP_ARC_NOT_READY_RETRY_SETTLE_SEC", "5"))


def _fabric_of(monitor) -> dict:
    """The post-reset check's fabric verdict for the reset's own event (spec 03 I78): pass/fail, and
    how far the links found fall short of the cabling."""
    verdict = getattr(monitor, "fabric_verdict", None)
    out = verdict() if callable(verdict) else None
    return out if isinstance(out, dict) else {"result": "none"}


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
    # Spec 03 I61: a clean post-job eth read timed out on a present mesh, and this hold episode's one
    # present-mesh reset is unspent and not refused by the outage hold. Routed like dispatch_reset_due.
    eth_timeout_reset_due: bool = False
    # Spec 03 I69: a job the broker stopped mid-run (its timeout, or a requested kill) owes the mesh
    # one reset before release, on the same unspent latch. Routed like dispatch_reset_due.
    kill_reset_due: bool = False
    # Spec 03 I55: the fabric passed traffic but mesh dispatch failed over a present mesh, and this
    # hold episode has not had its one present-mesh reset yet (nor does the outage hold forbid it).
    dispatch_reset_due: bool = False
    # This hold episode's galaxy reset already REGRESSED the off-bus count (spec 04 I31): a failed
    # reset on it is warm-reboot-futile even when this pass's own count did not grow.
    episode_reset_regressed: bool = False


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
    # GalaxyRecovery's host-escalation ladder (the power-cycle opt-in; spec 04 I71):
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
    # the ladder read the box as idle. Measured on box B 2026-09-17: the stuck-hold escalation
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
    clear_device_reported_fault: Callable[[str], None]
    clear_device_dirty: Callable[..., None]
    # Whether the mesh is CURRENTLY dirty, unverified, or carrying a runtime-reported fault —
    # read after an escalation runs to tell a rung that recovered the mesh from one that merely
    # ran (climbed to a further rung, or found none left): every recovering rung clears all
    # three, and nothing else does, so this is the one signal common to every path that can
    # recover, not just the one (_reset_and_verify_device) that happens to also flip a
    # RecoveryMechanism-wide flag another, unrelated reset could have set.
    device_degraded: Callable[[], bool]
    # The host rung's OTHER half — the chassis power cycle and the loud whole-bus event stay
    # server-owned (the gate journals that event itself, and the power cycle's ledger row and job
    # entry are server state), so they are injected rather than moved.
    # Returns False when the cycle certainly did not launch (spec 04 I71: hold, page, retry).
    auto_power_cycle_host: Callable[[Callable, str], Awaitable[Optional[bool]]]
    emit_all_off_bus_power_cycle_required: Callable[[Callable, int, int, str], None]
    host_escalation_kwargs: Callable[[], dict]
    # Probe primitives that already live in the health package (health.monitors.heartbeat) but
    # that server.py re-exports by import and tests monkeypatch on ``server.<name>`` to drive the
    # moved bodies' behavior deterministically — see the class docstring. Real server-side callers
    # of these three remain (the gate, the startup probe, the exec/reset tools), which is what
    # distinguishes them from e.g. ``reset_chip_via_bridge``, which has
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
    # {chip index: PCI address}, the map tray placement is derived from (spec 04 I16). Same
    # injection reason as the two above: the cache lives on the HealthMonitor singleton.
    chip_buses_provider: Optional[Callable[[], Optional[dict]]] = None
    journal_skip_once: Optional[Callable[[str, str], None]] = None
    # The telemetry sampler's server-side callbacks (see tt_device_mcp.telemetry). Same lambda
    # discipline as every field above — each re-resolves a server.py name per call so a
    # monkeypatched fake still takes effect. Default None only because the conftest deps fixture
    # predates the sampler and never constructs one; ServerFsm.boot always receives real values.
    chip_sample: Optional[Callable[[], dict]] = None
    # The sampler's early off-bus watch (spec 03 I79): kernel-held AER counters and the node list.
    link_signals: Optional[Callable[[], dict]] = None
    append_trace: Optional[Callable[[dict], None]] = None
    capture_incident: Optional[Callable[..., object]] = None
    isolate_chip: Optional[Callable[[str], bool]] = None
    chip_pci_bdf: Optional[Callable[[str], Optional[str]]] = None
    health_event: Optional[Callable[..., None]] = None
    kill_device_holders: Optional[Callable[[str], Awaitable[list]]] = None
    # Spec 03 I86: SIGSTOP the broker's own device readers (fabric check, aiclk ceiling) before the
    # valve's PCI remove; returns the pids stopped (server._freeze_device_readers). None: no stop.
    freeze_device_readers: Optional[Callable[[str], list]] = None
    # Spec 03 I92: the broker's own check readers still running (server._broker_device_readers), and
    # the AER watch's stop for them: SIGSTOP then SIGKILL their groups, no verdict, from the watch
    # thread with no I/O before the signals (server._stop_check_readers_on_aer); ``kill=False``
    # leaves them stopped for the dead-chip valve (I94). None: no watch.
    broker_check_readers: Optional[Callable[[], list]] = None
    stop_check_readers_on_aer: Optional[Callable[..., list]] = None
    # A journal append the journal's own writer thread fsyncs, so the caller never waits on it.
    health_event_detached: Optional[Callable[..., None]] = None
    # Spec 03 I95: queue one record for the journal's writer and wait, bounded, for its fsync.
    health_event_flushed: Optional[Callable[..., bool]] = None
    # Bounded wait for the pids it killed to drop out of the holder scan; returns those still
    # listed (spec 03 I19, the shared spec 01 I67 helper).
    wait_device_released: Optional[Callable[[list], Awaitable[set]]] = None
    # Spec 04 I77: names each killed pid that is stuck in the kernel still referencing the dead
    # chip(s), journals host_at_risk, pages, and fences every re-enumerating rung while it lives.
    # Returns the stuck pids. Its third argument: the chips' PCI addresses, read before the removal.
    note_stuck_holders: Optional[Callable[[list, list, tuple], list]] = None
    # Spec 04 I77: the registered dead-chip stuck holders still stuck, by pid, on a fresh /proc read.
    # The I70 rung's 'gone' check counts them as still holding: one with only a removed chip's BAR
    # or deleted node mapped is not in the /dev/tenstorrent holder scan.
    stuck_holder_pids: Optional[Callable[[], set]] = None
    # Spec 04 I77: a RUNNING job the broker has not itself killed for recovery. The I70 host rung's
    # guard asks this instead of job_running: a job the dead-chip path SIGKILLed stays RUNNING while
    # its process is stuck in the kernel, and counting it would hold the rung forever. None: job_running.
    live_job_running: Optional[Callable[[], bool]] = None
    # Marks the pollers the dead-chip valve stopped as owed a restart (server._owe_device_pollers):
    # that stop has no paired restore, and an idle relift can close the episode with no reset.
    owe_device_pollers: Optional[Callable[[str], None]] = None
    # Spec 03 I83: one heartbeat read of only the given chips, after a mesh reset whose POST_RESET
    # failed on them (monitors.heartbeat.read_heartbeats with only). None: the in-flight cut is off.
    read_inflight_heartbeats: Optional[Callable[[frozenset], dict]] = None
    # Spec 03 I83: (pids, scan complete) of the device holders that are not the broker's own or the
    # reset's (server._find_inflight_readers). Read-only.
    find_inflight_readers: Optional[Callable[[], tuple]] = None
    # Spec 03 I83: SIGTERM, then SIGKILL PREEMPT_KILL_GRACE_SEC later, to those holders
    # (server._cut_inflight_readers). The cut calls it only once every confirmed chip is detached.
    cut_inflight_readers: Optional[Callable[[str], Awaitable[list]]] = None
    # Records the holders the dead-chip valve refused to kill (server._note_dead_kill_refused, spec 03
    # I84), so the 10-min bound sends them to the host rung instead of reading them as tenants.
    note_kill_refused: Optional[Callable[[str, list], list]] = None
    mark_device_dirty: Optional[Callable[..., None]] = None
    # Places a not-dirty fabric-unverified hold (server._hold_device_fabric_unverified): a reset whose
    # fabric stayed a 77 settles there, never as a failed reset.
    hold_device_fabric_unverified: Optional[Callable[[str], None]] = None
    # Counts a reset's post-reset 77 passes toward the open episode's I40 bound
    # (server._note_fabric_77_reset_passes), so the next relift falls back at once instead of
    # re-running the passes the reset just spent.
    note_fabric_77_reset_passes: Optional[Callable[[int], None]] = None
    # The open hold's fabric link-short cause (server._fabric_link_short_hold; "" when none): a lost
    # link, a latched topology mismatch or missing descriptor ports. Spec 04 I79/I83 send those holds
    # to the power cycle after one ladder pass, so the sweep never waits to space its mesh reset (I93).
    fabric_link_short_hold: Optional[Callable[[], str]] = None
    # Whether the open hold's fabric 77 has a cause no reset or reboot fixes — a known cable fault (a 78),
    # a topology mismatch, hugepages not yet allocated, the validator missing
    # (server._fabric_77_no_reset_cause; never the I42 latch, which takes the ladder). Such a 77
    # never climbs to a host rung (spec 04 I29).
    fabric_77_no_reset_cause: Optional[Callable[[], bool]] = None
    refresh_idle_hold_ledger: Optional[Callable[[], None]] = None
    spawn_idle_relift: Optional[Callable[[], None]] = None
    watch_idle_holders: Optional[Callable[[], None]] = None
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
    # Re-runs a failed eth self-test at the head of a full health pass (server._rearm_eth_before_check,
    # spec 03 I28). Returns a FROZEN detail when the self-test hung during an open fault episode,
    # else "". None (the conftest deps fixture) skips the retry.
    eth_rearm: Optional[Callable[[], Awaitable[str]]] = None


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
        # The chips the last _recover_isolated_chips call reached with a bridge write, whatever came of
        # it (spec 04 I98): the hold's SBR record keys on this, not on the reasons above.
        self.last_bridge_reset_fired: set = set()

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
        eth_timeout_reset: bool = False,
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
            phase,
            run_fabric=run_fabric,
            run_eth=run_eth,
            fabric_stale=fabric_stale,
            eth_timeout_reset=eth_timeout_reset,
            expected=expected,
            log=log,
        )
        return state.healthy, state.as_evidence()

    async def _recover_isolated_chips(self, log, exclude=frozenset(), not_reset=frozenset()) -> bool:
        """Bring back chips that were cut out of the kernel after leaving the PCIe bus.

        Reset through the parent bridge, not the chip: a dead endpoint cannot be reset through
        its own config space, but it does not need to be, and the bridge above it always answers.
        Each Blackhole sits alone behind its own bridge here, so this is surgical — measured on
        live hardware, the other 31 chips keep ticking and a fabric traffic pass still passes
        afterwards, so no galaxy-wide reset is needed to make the mesh usable again.

        Caller MUST hold the device lock: this ends with a PCI rescan, and nothing else may be
        touching the device while the endpoint re-enumerates.

        ``exclude``: chips that already had their SBR round this hold (spec 04 I98). They get no
        bridge write and stay isolated; a removed one among them was reset this hold, so it does not
        withhold the rescan (I69), unless it is in ``not_reset``: spent with no bridge write (no_bridge),
        never reset, so it still withholds every rescan of the pass.
        """
        # ladder-v2: the per-chip SBR window this rung reads, chip id -> {"reason": "no_bridge"} for
        # every chip whose reset could not fire because the endpoint left the bus. Reset FRESH every
        # call (before the empty-set early return), so a later reader never sees a stale prior pass.
        self.last_bridge_reset_reasons: dict = {}
        self.last_bridge_reset_fired: set = set()
        isolated_chips = self.deps.isolated_chips()
        # Spec 04 I69: every chip the broker removed is off the bus and un-reset, so it is a target
        # whether or not this pass was asked for it (a record reloaded after a restart, say). Left out,
        # the rescan any other chip's reset ends with would re-enumerate it un-reset.
        adopted = [i for i in pci.removed_chip_ids() if i not in isolated_chips]
        if adopted:
            isolated_chips.update(adopted)
            health_event("removed_chips_isolated", chips=adopted, context="bridge_reset")
        if not isolated_chips:
            return True
        spent = {str(c) for c in exclude} & {str(c) for c in isolated_chips}
        never_reset = spent & {str(c) for c in not_reset}
        for idx in spent:
            self.last_bridge_reset_reasons[idx] = {"reason": "sbr_spent_this_hold"}
        targets = sorted((c for c in isolated_chips if str(c) not in spent), key=int)
        if not targets:
            return False
        # Spec 04 I76: a PCI sysfs write is blocked in the kernel (PCI-wedged). Every bridge reset ends in
        # a rescan on that same lock, so none can run: the rung fails at once, its chips stay withheld,
        # and the power cycle is already requested.
        if pci_wedged_refusal(f"bridge reset of chip(s) {','.join(targets)}", log):
            for idx in targets:
                self.last_bridge_reset_reasons[idx] = {"reason": "pci_wedged"}
                metrics.stage_fired("bridge_reset", "blocked")
            health_event("chip_recovery", recovered=[], still_dead=targets, inapplicable=[], pci_wedged=True)
            return False

        # A killed holder stuck in the kernel with a removed chip's BAR mapped (spec 04 I77): the
        # bridge reset and the rescan both re-enumerate under it, so neither runs. The chips stay
        # out and the hold stands; the broker's I70 escalation owns the bound.
        if reenumerate_withheld(self.mechanism, f"bridge reset of chip(s) {','.join(targets)}", log):
            for idx in targets:
                self.last_bridge_reset_reasons[idx] = {"reason": "stuck_holder"}
            metrics.stage_fired("bridge_reset", "blocked")
            return False
        _t0 = datetime.now()
        self.deps.begin_action_row("[broker]bridge-reset", f"bridge-reset chip(s) {','.join(targets)}")
        # Spec 04 I90: a device reset like glx_reset, so /status shows it as one from its first second.
        self.deps.set_device_op_detail(
            f"{constants.DEVICE_RESET_DETAIL_PREFIX} bridge reset of chip(s) {','.join(targets)} (~10s)"
        )
        device_pci_map = self.deps.device_pci_map()
        recovered, lost, inapplicable = [], [], []
        # The SBR write needs root and setpci (spec 04 I17). Decided once, ahead of the loop,
        # because discovering it per chip means reading a permission failure the call site cannot
        # tell from a bad bridge — which is classified retryable, so the loop spends its whole
        # attempt budget on a write the kernel will never allow. The rescan below still runs: it
        # needs no setpci.
        sbr = bridge_reset_enabled()
        if not sbr:
            log(
                f"per-chip bridge reset unavailable ({bridge_reset_unavailable_reason()}) — a PCI rescan follows "
                "unless a chip the broker removed is still un-reset"
            )
        # An SBR takes its chip off the bus like any reset, so the per-host gate (spec 04 I24) holds it
        # too: the isolated chips are off the bus by definition.
        gated, why = False, ""
        if sbr:
            allowed, why = pcie_guard.host_reset_gate(len(targets))
            gated = not allowed
        if gated:
            log(
                f"per-chip bridge reset NOT fired: {why} — a PCI rescan follows unless a chip the broker "
                "removed is still un-reset"
            )
            health_event("host_reset_gated", context="bridge_reset", chips=targets, reason=why, host_at_risk=True)
        # Spec 04 I92: every reset journals its pre-reset holder scan. This one needs none: each target
        # was cut out of the kernel (detached), so no process can map it and closing a holder's fd
        # makes the driver send it nothing. Killing readers here would only stop the device pollers'
        # reads of the chips that stay on the bus.
        if sbr and not gated:
            health_event(
                "pre_reset_holder_scan",
                argv=["bridge-reset", *targets],
                owner="[broker]bridge-reset",
                outcome="targets_detached",
                chips=targets,
                seconds=0.0,
            )
        # Spec 04 I69: a chip the broker removed is still hung, and any rescan this pass would
        # re-enumerate it for the driver to probe. So reset every one of them through its recorded
        # bridge first, then rescan once. If any cannot be reset, no rescan runs this pass at all
        # (each per-chip reset below rescans too): the chips stay isolated and the hold escalates.
        reset_this_pass: set = set()
        withheld: list = []
        # Spec 04 I76: set by the first bridge reset that times out or is refused. The pass stops there:
        # no further bridge call, no rescan; every chip not yet tried is withheld.
        wedged = False
        removed = [t for t in targets if pci.removed_chip(t)]
        # A spent removed chip no bridge write reached this hold is as un-reset as one this pass could
        # not reset: no rescan this pass (spec 04 I69).
        unreset: list = [i for i in pci.removed_chip_ids() if str(i) in never_reset]
        if sbr and not gated and removed:
            for idx in removed:
                for attempt in range(BRIDGE_RESET_MAX_TRIES):
                    FABRIC_DEBT.note_reset(f"bridge reset of chip {idx}")
                    # An SBR clears the chip's firmware clock cap like any reset (spec 03 I35).
                    CEILING.mark_owed(f"bridge reset of chip {idx}")
                    try:
                        fired = await PCI_WRITER.write(
                            f"bridge reset of removed chip {idx}",
                            reset_removed_chip,
                            idx,
                            timeout=bridge_reset_call_timeout(),
                        )
                    except PciWedged as e:
                        health_event("bridge_reset_failed", chip=idx, reason="pci_wedged", error=str(e))
                        log(f"bridge reset of removed chip {idx} failed: {e}")
                        wedged = True
                        break
                    if fired:
                        reset_this_pass.add(idx)
                        self.last_bridge_reset_fired.add(idx)
                        break
                    if fired is None:
                        # Same no-window signal as reset_chip_via_bridge's no_bridge (see below).
                        self.last_bridge_reset_reasons[idx] = {"reason": "no_bridge"}
                        break
                    if attempt + 1 < BRIDGE_RESET_MAX_TRIES:
                        await asyncio.sleep(BRIDGE_RESET_SETTLE_SEC)
                if idx not in reset_this_pass:
                    unreset.append(idx)
                if wedged:
                    unreset.extend(i for i in removed if i not in reset_this_pass and i not in unreset)
                    break
            if not unreset:
                log(f"chip(s) {removed} reset through their recorded bridges — rescanning once")
                # One link-retrain settle for the batch, after the last SBR release, as
                # reset_chip_via_bridge waits before its own rescan.
                await asyncio.sleep(SBR_RETRAIN_SETTLE_SEC)
                try:
                    await pci_rescan("bridge reset batch rescan", _sysfs_rescan)
                except (OSError, asyncio.TimeoutError) as e:
                    log(f"PCI rescan after the bridge reset could not run ({e})")
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    if all((pci.SYSFS_CLASS_DIR / f"tenstorrent!{i}").exists() for i in removed):
                        break
        for idx in targets:
            if wedged and idx not in lost:
                withheld.append(idx)
                self.last_bridge_reset_reasons[idx] = {"reason": "pci_wedged"}
                metrics.stage_fired("bridge_reset", "blocked")
                continue
            if gated:
                inapplicable.append(idx)
                self.last_bridge_reset_reasons[idx] = {"reason": "gated"}
                metrics.stage_fired("bridge_reset", "blocked")
                continue
            if sbr and unreset:
                withheld.append(idx)
                if idx not in unreset:
                    self.last_bridge_reset_reasons[idx] = {"reason": "rescan_withheld"}
                metrics.stage_fired("bridge_reset", "blocked")
                continue
            if idx in reset_this_pass and (pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}").exists():
                # Reset above and back on the bus from the pass's rescan (or a later chip's).
                pci.forget_removed_chip(idx)
                recovered.append(idx)
                continue
            if not sbr:
                inapplicable.append(idx)
                # NOT no_bridge: that means the endpoint left the bus. Nothing was written here,
                # so the window is unproven, and a distinct reason says so.
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
            # A removed chip already spent one try on the reset above.
            tries = BRIDGE_RESET_MAX_TRIES - 1 if idx in reset_this_pass else BRIDGE_RESET_MAX_TRIES
            for attempt in range(tries):
                if idx in reset_this_pass and attempt == 0:
                    await asyncio.sleep(BRIDGE_RESET_SETTLE_SEC)
                FABRIC_DEBT.note_reset(f"bridge reset of chip {idx}")
                # A bridge reset clears the chip's firmware clock cap like any reset (spec 03 I35).
                CEILING.mark_owed(f"bridge reset of chip {idx}")
                try:
                    # The one in-flight PCI write (spec 04 I76): its rescan waits on the kernel's PCI
                    # rescan/remove lock, which a write blocked in the kernel holds for good. A timeout
                    # latches PCI-wedged and ends the whole pass here, not just this chip.
                    result = await PCI_WRITER.write(
                        f"bridge reset of chip {idx}",
                        reset_chip_via_bridge,
                        idx,
                        bdf,
                        timeout=bridge_reset_call_timeout(),
                    )
                except PciWedged as e:
                    health_event(
                        "bridge_reset_failed",
                        chip=idx,
                        bdf=bdf,
                        reason="timeout",
                        timeout_s=bridge_reset_call_timeout(),
                        error=str(e),
                    )
                    log(f"bridge reset of chip {idx} failed: {e}; the pass stops, the power cycle is requested")
                    lost.append(idx)
                    metrics.stage_fired("bridge_reset", "failed")
                    wedged = True
                    break
                if result is not None:
                    self.last_bridge_reset_fired.add(idx)
                if result:
                    recovered.append(idx)
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
                if attempt + 1 < tries:
                    await asyncio.sleep(BRIDGE_RESET_SETTLE_SEC)
            else:
                lost.append(idx)
                if idx in reset_this_pass:
                    # A chip the broker removed, reset through its recorded bridge on every try and
                    # still off the bus: no SBR brings it back, the same no-window fact as no_bridge
                    # (spec 04 I69).
                    self.last_bridge_reset_reasons[idx] = {"reason": NO_WINDOW_REMOVED_REASON}
                metrics.stage_fired("bridge_reset", "failed")

        # Spec 04 I94: back on the bus is not recovered. A chip re-enumerated with its ARC dead stays
        # isolated and still dead, so the rung fails and the off-bus one-shot escalates past it. A
        # PCI-wedged pass already failed and requested the power cycle: it reads nothing more.
        arc_dead = [] if wedged else await self._arc_dead_after_bridge_reset(recovered, log)
        recovered = [i for i in recovered if i not in arc_dead]
        lost.extend(arc_dead)
        for idx in arc_dead:
            metrics.stage_fired("bridge_reset", "failed")
        for idx in recovered:
            metrics.stage_fired("bridge_reset", "ok")
            isolated_chips.discard(idx)
        # An inapplicable chip is NOT recovered — it stays isolated and forces the not-recovered
        # branch, so an all-gone mesh (every chip no_bridge) can never read as healed and release.
        still_dead = sorted(set(lost) | set(inapplicable) | set(withheld), key=int)
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

        # Spec 04 I69: never re-enumerate a removed chip that was not reset this pass — the rescan
        # lets the driver probe a hung chip. Every record counts, not only this pass's targets: the
        # rescan is global. A refused host reset gate withholds it only then (the removed chips were
        # not reset because of it); chips the broker never removed keep the bare rescan, as before.
        unreset_dead = [
            i for i in pci.removed_chip_ids() if i not in reset_this_pass and str(i) not in spent - never_reset
        ]
        withhold = ("host_reset_gated" if gated else "removed_chip_not_reset") if unreset_dead else ""
        if wedged or PCI_WRITER.latched:
            withhold = "pci_wedged"
        if still_dead and withhold:
            health_event("pci_rescan_withheld", chips=still_dead, unreset=unreset_dead, reason=withhold)
            log(
                f"PCI rescan withheld ({withhold}) for chip(s) {still_dead}: re-enumerating a hung chip "
                f"that was not reset can take the host down; they stay isolated, escalating to a heavier rung"
            )
        if still_dead and not withhold and set(still_dead) - set(arc_dead):
            # Cheapest rung there is, and the one that was missing: a bare PCI rescan. A chip with no
            # parent bridge is not necessarily a departed ASIC — measured twice on this host, the
            # endpoint was still in lspci AND still bound to the driver, and only its /dev node was
            # gone. The gate infers "off the PCIe bus" from the sysfs node count, so that state is
            # indistinguishable from a real drop and routed straight past every cheap remedy to a
            # reboot or a chassis power cycle. A rescan re-enumerates it in about a second with no
            # downtime; both times it restored 8/8. Try it before anything that costs a tenant.
            try:
                await pci_rescan("bare rescan of dead chips", _sysfs_rescan)
                await asyncio.sleep(3)
            except (OSError, asyncio.TimeoutError) as e:
                log(f"PCI rescan could not run ({e}); falling through to the heavier rungs")
            else:
                # A rescan cannot revive a chip already on the bus with its ARC dead (I94).
                back = [
                    i
                    for i in still_dead
                    if i not in arc_dead and Path(f"{pci.SYSFS_CLASS_DIR}/tenstorrent!{i}").exists()
                ]
                # The rescan may bring back more than the chips this pass targeted.
                others = isolated_chips - set(arc_dead)
                back = sorted(set(back) | set(drop_returned_isolated(others, "PCI rescan", log)), key=int)
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
                    return not spent & set(map(str, isolated_chips))
        if still_dead:
            if inapplicable:
                log(
                    f"chip(s) {sorted(inapplicable, key=int)} have no bridge to reset — they left "
                    f"the bus, so the per-chip rung cannot reach them; escalating to a heavier rung."
                )
            if arc_dead:
                log(
                    f"chip(s) {arc_dead} came back on the bus with no live ARC heartbeat after "
                    f"{BRIDGE_RESET_ARC_WAIT_SEC:g}s: not recovered, they stay isolated; escalating to a heavier rung."
                )
            if set(lost) - set(arc_dead):
                log(
                    f"chip(s) {[i for i in lost if i not in arc_dead]} did NOT come back from a bridge reset after "
                    f"{BRIDGE_RESET_MAX_TRIES} tries; they stay out of the kernel so they cannot take "
                    f"the host down. Escalating to a galaxy reset: it re-powers every tray and rescans them "
                    f"back in; a chip still off the bus after it goes on to the power cycle."
                )
            return False
        log(f"chip(s) {recovered} are back on the bus and bound to the driver")
        # Spec 04 I98: a chip left out as spent this hold and still off the bus keeps the rung failed.
        return not spent & set(map(str, isolated_chips))

    async def _arc_dead_after_bridge_reset(self, chips: list, log) -> list:
        """Spec 04 I94: of the chips the bridge rung brought back on the bus, those whose ARC heartbeat
        still does not read as a counter after BRIDGE_RESET_ARC_WAIT_SEC. Reads only; a live chip costs
        one heartbeat read and no wait. On a host whose KMD has no heartbeat attr it marks no chip:
        the rung's other checks and the full post-reset check, fabric included, still gate the lift."""
        global _arc_check_skip_logged
        pending = sorted(chips, key=int)
        if (
            pending
            and not await asyncio.to_thread(heartbeat_attr_present, pending)
            and not await asyncio.to_thread(self.deps.heartbeat_supported)
        ):
            if not _arc_check_skip_logged:
                _arc_check_skip_logged = True
                log("post-bridge ARC check skipped: this driver exposes no tt_heartbeat attr")
                health_event("bridge_reset_arc_check_skipped", reason="attr_absent", chips=pending)
            return []
        for attempt in range(BRIDGE_RESET_ARC_POLLS + 1):
            if not pending:
                break
            if attempt:
                await asyncio.sleep(BRIDGE_RESET_ARC_POLL_SEC)
            live = set(await asyncio.to_thread(arc_live_chips, pending))
            pending = [i for i in pending if i not in live]
        if pending:
            health_event("bridge_reset_arc_dead", chips=pending, wait_sec=BRIDGE_RESET_ARC_WAIT_SEC)
        return pending

    def _fabric_ran_but_unverified(self, evidence: dict) -> bool:
        """A 77: the fabric traffic pass RAN but tested no trained link — the ``fabric`` key is present
        with ``ok=None``. A verify that never ran the fabric has no ``fabric`` key and is NOT a 77.
        Reading the two apart is what keeps a training-window verdict from passing as a cleared fabric."""
        f = evidence.get("fabric")
        return f is not None and f.get("ok") is None

    async def _verify_device_after_reset(self, expected: int, log) -> tuple[Optional[bool], dict, int]:
        """Verify a mesh after a reset, giving a post-reset fabric 77 time to settle before it is believed.

        A reset returns before the eth links re-train, so the first fabric pass can find no trained link
        (a 77 — a training-window verdict, not a cleared or a broken fabric). Re-check up to
        ``POST_RESET_FABRIC_RETRIES`` times so it resolves to a REAL verdict; multi-chip only, while
        enum+ARC pass. A fabric that STILL cannot verify after training (a persistent 77) is UNKNOWN —
        ``healthy`` is None: never a recovery (no hold is cleared onto a fabric no pass proved, the box D
        shape: enum+ARC read OK while every full-mesh job died on a down link), and never a failed reset
        either (a 77 measured nothing, so it must not arm the host rungs). Falsy, so a caller that only
        asks "recovered?" still reads no. A 77 from a validator that is not built yet (spec 03 I81) gets no
        retry and no eth-training wait: it is the same UNKNOWN at once. Returns ``(healthy, evidence,
        fabric_retries)``."""
        healthy, evidence = await self._verify_device(expected, log)
        retries = 0
        not_built = None
        while (
            self._fabric_ran_but_unverified(evidence)
            and healthy
            and expected > 1
            and retries < POST_RESET_FABRIC_RETRIES
        ):
            not_built = fabric.validator_not_built()
            if not_built is not None:
                # I81: the 77 is a validator that is not on disk; no wait for eth training makes it run.
                log(
                    f"post-reset fabric could not run: fabric validator not built ({not_built}) — no "
                    "eth-training wait; the fabric stays unverified until the build lands"
                )
                break
            retries += 1
            if fabric.names_dispatch_cap(evidence["fabric"].get("detail", "")):
                # I84: the validator already moved traffic across every link, so they were trained;
                # only the dispatch probe ran out its cap. The eth-training wait buys nothing here.
                log(
                    f"post-reset fabric passed its links but the dispatch probe hit its cap (77) — "
                    f"re-checking now, no eth-training wait (retry {retries}/{POST_RESET_FABRIC_RETRIES})"
                )
            else:
                log(
                    f"post-reset fabric found no trained link (77) — waiting {POST_RESET_FABRIC_RETRY_SLEEP_SEC:.0f}s "
                    f"for eth training, then re-checking (retry {retries}/{POST_RESET_FABRIC_RETRIES})"
                )
                await asyncio.sleep(POST_RESET_FABRIC_RETRY_SLEEP_SEC)
            healthy, evidence = await self._verify_device(expected, log)
        if healthy and expected > 1 and self._fabric_ran_but_unverified(evidence):
            log(
                (
                    "post-reset fabric could not verify (fabric validator not built)"
                    if not_built
                    else "post-reset fabric still could not verify after eth-training retries"
                )
                + " — the fabric is UNKNOWN (enum+ARC healthy, no pass got a verdict): not recovered, and not "
                "a failed reset"
            )
            healthy = None
        FABRIC_DEBT.note_evidence(evidence)
        return healthy, evidence, retries

    async def _reset_and_verify_device(self, indices: list, log) -> Optional[bool]:
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
        caller report honestly. The one exception is a reset that brought every chip back on the bus
        but left ARC not ready: that is not a dead endpoint, and one more reset clears it
        (:meth:`_arc_not_ready_retry_due`). Returns True if the device verified healthy, None when the reset ran,
        enum+ARC verify and only the fabric stayed unverifiable (a persistent 77: held fabric-unverified,
        the caller must not climb on it), False otherwise.
        """
        started = time.monotonic()
        try:
            return await self._reset_and_verify_device_once(indices, log)
        finally:
            # The reset and its verify are over and the host is still up: an off-bus intent is spent.
            pcie_guard.end_offbus_reset()
            if self.mechanism.last_reset_monotonic >= started and "-glx_reset" in self.reset_argv(indices):
                # A launched Galaxy mesh reset: the sweep spaces the episode's second from it (I93).
                self.mechanism.last_mesh_reset_run = {
                    "episode": self.deps.device_hold_episode_since() or "",
                    "started": started,
                    # The reset command's end, not its check's: the sweep's gap counts from here.
                    "ended": max(started, self.mechanism.last_reset_ended_monotonic),
                }

    async def _arc_not_ready_retry_due(self, argv: list, expected: int, log) -> bool:
        """After a mesh reset that EXITED non-zero: is this the one shape a second reset clears, on a box
        it may fire on? All of: a Galaxy ``-glx_reset`` on a multi-chip mesh; every expected chip on the
        bus (none missing, none reading all-ones); at least one chip ARC-not-ready, still so after
        ``ARC_NOT_READY_RETRY_SETTLE_SEC``; no running broker job and a complete holder scan with no
        tenant. A chip still off the bus is the dead-endpoint case the no-repeat rule is for: never."""
        if expected <= 1 or "-glx_reset" not in argv:
            return False

        def bus_back_arc_not_ready() -> list:
            beats = self.deps.read_heartbeats()
            not_ready = arc_not_ready_chips(beats)
            if not not_ready or offbus_count(beats, expected, not_ready) or pcie_guard.chips_off_bus(expected):
                return []
            return sorted(not_ready, key=int)

        if not await asyncio.to_thread(bus_back_arc_not_ready):
            return False
        await asyncio.sleep(ARC_NOT_READY_RETRY_SETTLE_SEC)
        not_ready = await asyncio.to_thread(bus_back_arc_not_ready)
        if not not_ready:
            return False
        if self.deps.job_running():
            return False
        scan = await asyncio.to_thread(self.deps.enumerate_device_holders)
        if not scan.complete or any(is_tenant(h) for h in scan.holders):
            return False
        log(
            f"all {expected} chips back on the bus but chip(s) {not_ready} ARC not ready after the reset — "
            "one more reset on the idle device (not a dead endpoint)"
        )
        health_event("reset_arc_not_ready_retry", argv=argv, arc_not_ready=not_ready, expected_chips=expected)
        return True

    async def _reset_and_verify_device_once(self, indices: list, log, arc_retry: bool = True) -> Optional[bool]:
        # Reset per call: only a reset that actually EXITS non-zero this pass sets it below. An adopted
        # foreign scope or a clean-exit-but-unverified reset must not leave a stale hard-fail flag that
        # steers the next gate's escalation to the cold rung over a reset that never hard-failed.
        self.mechanism.last_reset_exit_nonzero = False
        # Set only once a reset of ours launched and exited: a refused, adopted or still-cycling one
        # leaves None, and only a launched one that failed goes in the boot's record (spec 04 I58).
        self.mechanism.last_reset_rc = None
        argv = select_recovery(self.monitor, self.mechanism, self.deps).reset_argv(indices)
        expected = self.monitor.expected(len(indices))

        if await self.mechanism.await_foreign_scope(log):
            # A foreign scope (a prior gate's reset, or one still cycling after a broker restart) ran
            # the reset; we waited it out above, so it is a COMPLETED reset. Arm the cooldown exactly
            # as the own-reset path does below: without this a failed adopt leaves the clock
            # untouched and the next gate fires another back-to-back galaxy reset — the repeat MMIO at
            # a dead endpoint the cooldown exists to prevent.
            self.mechanism.last_reset_monotonic = time.monotonic()
            self.monitor.last_fabric_ok = None  # the pre-reset verdict describes a mesh that is gone
            healthy, evidence = await self._verify_device(expected, log)
            FABRIC_DEBT.note_evidence(evidence)
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
        # The operator's outage hold (spec 04 I28): held like the gate above, and paged.
        if refuse_mesh_reset("gate/relift", argv, log, off_bus):
            return False

        log(f"resetting device: {' '.join(argv)}  ({expected} device(s); ~30-60s, restart-safe)")
        health_event("reset_begin", argv=argv, expected_chips=expected)
        prior_reset_monotonic = self.mechanism.last_reset_monotonic
        self.mechanism.last_reset_monotonic = time.monotonic()
        pcie_guard.begin_offbus_reset("mesh reset", off_bus)
        aer_mask = pcie_guard.mask_for_mesh_reset(log)
        try:
            rc, out = await self.mechanism.reset_with_quiesce(argv, log)
        finally:
            self.mechanism.last_reset_ended_monotonic = time.monotonic()
            if aer_mask is not None:
                await asyncio.to_thread(aer_mask.restore)
        if rc is None and (out or "").startswith("reset refused:"):
            # Spec 04 I92: the pre-reset scan refused (a tenant, a reader that would not die, an off-bus
            # chip it could not detach, a reset already in flight). No reset ran, so nothing that a
            # reset settles is settled: the cooldown clock goes back, a kill-reset debt or a spent
            # dispatch reset stays owed, and the hold stays for the 10-min ladder (I76).
            self.mechanism.last_reset_monotonic = prior_reset_monotonic
            log(out)
            health_event("reset_refused", argv=argv, detail=out)
            metrics.stage_fired("smi_reset", "blocked")
            return False
        self.mechanism.last_reset_rc = rc
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
                self.mechanism.mark_mesh_reset_failed()
            metrics.stage_fired("smi_reset", "timeout" if rc is None else "failed")
            if rc is not None and arc_retry and await self._arc_not_ready_retry_due(argv, expected, log):
                # The retry is this same gated path (host gate, outage hold, cooldown clock, full verify
                # with the fabric), only once: a second failure returns False and the ladder climbs on.
                return await self._reset_and_verify_device_once(indices, log, arc_retry=False)
            return False

        # rc == 0: the reset command itself completed, independent of whatever the verify below
        # finds — a stage's own firing outcome is about the ACTION, not the fault it was meant to
        # clear, so this always reports "ok" whether or not the device comes back healthy.
        metrics.stage_fired("smi_reset", "ok")
        # The reset re-inited every eth core, so a fabric FAIL measured before it no longer describes
        # this mesh. Left standing, a post-reset 77 read as a measured fault and took the hold to the
        # terminal DOWN no idle path re-checks, instead of the bounded 77 relift (spec 04 I32a). The
        # release is still the debt's: only a passing pass after this reset settles it.
        self.monitor.last_fabric_ok = None
        healthy, evidence, retries = await self._verify_device_after_reset(expected, log)
        if healthy is None:
            # A persistent post-reset 77 is UNKNOWN, not a failed recovery: the chips came back, and the
            # fabric pass measured nothing. Counting it failed armed the cooldown and the gate's two
            # strikes, and drove the idle ladder toward a host rung on no evidence. Hold fabric-unverified
            # (not dirty) instead: the reset was the remedy a reported fault was pending, so retire that,
            # and the hold is 77-only — the idle relift re-runs the pass, bounded by spec 03 I40.
            health_event(
                "reset_unverified", rc=rc, evidence=evidence, fabric_retries=retries, fabric=_fabric_of(self.monitor)
            )
            self.mechanism.last_reset_failed = False
            self.deps.clear_device_reported_fault("reset ran and enum+ARC verify; the fabric is unknown")
            if self.deps.hold_device_fabric_unverified is not None:
                self.deps.hold_device_fabric_unverified(
                    "reset ran: enum+ARC healthy, fabric unverified after the post-reset retries"
                )
            if self.deps.note_fabric_77_reset_passes is not None:
                self.deps.note_fabric_77_reset_passes(1 + retries)
            self.mechanism.unverified_reset_since = self.deps.device_hold_episode_since() or None
            log("reset complete; enum+ARC healthy but the fabric is UNKNOWN — held fabric-unverified, not climbing")
            return None
        health_event(
            "reset_verified" if healthy else "reset_ineffective",
            rc=rc,
            healthy=healthy,
            evidence=evidence,
            fabric_retries=retries,
            fabric=_fabric_of(self.monitor),
        )
        self.mechanism.last_reset_failed = not healthy
        if not healthy:
            self.mechanism.mark_mesh_reset_failed()
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
