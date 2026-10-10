# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Device health: vocabulary, evidence journal, probes, and recovery.

This is the package's one public surface. ``server.py`` and ``fsm.py`` must never reach past it
into a submodule directly (``tests/test_health_surface.py`` asserts that with an AST walk, over
both files) — every name either of them needs from here is either:

  * one of the subsystem's construction pieces — :class:`HealthMonitor`,
    :class:`RecoveryMechanism`, :class:`RecoveryDeps`, the two platform classes and
    :func:`_persistent_recovery`/``select_recovery`` — which ``ServerFsm.boot`` (fsm.py, the
    root system) assembles into this process's singletons, once, in the BOOT state; or
  * one of a short, DELIBERATE list of re-exports below, each grouped by why ``server.py``
    needs it for its OWN work — journaling a health event, reading the health dir, capturing an
    incident, sampling telemetry, building the :class:`Evidence` the gate hands
    ``Recovery.next_stage`` and naming the decisions that come back, or reading the escalation
    ladder's own thresholds for the journal around them. This is not a barrel.
"""

from __future__ import annotations

# The operator's AICLK ceiling state: server.py re-applies it at broker start and at the job door,
# marks it owed when a job ends, and must be able to kill its helper on the dead-chip path.
from tt_device_mcp.health.aiclk_ceiling import CEILING

# The shared health vocabulary: HealthMonitor.status() returns a HealthState, and server.py both
# builds one of its own (_healthy_reading()) and compares a probe's Verdict directly.
from tt_device_mcp.health.core import HealthState, Verdict

# The durable event journal and forensic incident capture — genuinely server-facing: this IS
# how the gate/reset/reset tool record their own decisions and freeze evidence for a post-mortem.
from tt_device_mcp.health.evidence import (
    append_trace,
    capture_incident,
    health_dir,
    health_event,
    mark_boot,
    previous_boot_bus_locks,
    previous_boot_error,
    read_health_events,
)
from tt_device_mcp.health.monitor import HealthMonitor

# The fabric probe module itself, not a wrapping function: server.py's own preflight check reads
# fabric.build_command() directly to warn when no fabric check is configured OR installed, and a
# test monkeypatches fabric.build_command on this exact binding (see test_device_safety.py) — a
# wrapper function would leave that patch aimed at a name nothing here reads.
from tt_device_mcp.health.monitors import eth, fabric

# Heartbeat probe primitives the gate/telemetry sampler in server.py calls directly (off-bus
# counts, the dead-chip sampler, the startup probe) — not merely bounced into RecoveryDeps.
from tt_device_mcp.health.monitors.heartbeat import (
    ALL_ONES,
    dead_chips,
    heartbeat_supported,
    heartbeat_verdict,
    read_heartbeats,
)

# The host-software version floors the startup preflight reports. Sysfs reads only, so the
# preflight can assert them without spawning anything or touching a device.
from tt_device_mcp.health.monitors.hostpci import hugepages_shortfall, version_floor_warnings

# The tt-smi/sysfs telemetry-sampler primitives server.py's own polling loop runs directly.
from tt_device_mcp.health.monitors.pci import (
    SAMPLE_INTERVAL_SEC,
    SAMPLE_RING_SIZE,
    aer_totals,
    chip_node_present,
    chip_pci_bdf,
    chip_sample,
    chip_snapshot,
    chip_snapshot_event,
    isolate_chip,
)
from tt_device_mcp.health.recovery import (
    BLOCKED,
    DEFER,
    HOLD_FABRIC_UNVERIFIED,
    OUTCOME_RECOVERED,
    OUTCOME_TERMINAL,
    OUTCOME_WAITING,
    RELEASE,
    RESET_MODE_GALAXY,
    RESET_MODE_TARGET,
    WAIT,
    Evidence,
    Recovery,
    RecoveryDeps,
    _declared_reset_mode,
    _persistent_recovery,
)
from tt_device_mcp.health.recovery import select_recovery as _select_recovery  # noqa: F401
from tt_device_mcp.health.recovery.base import (
    HOLD_ESCALATION_REARM_SEC,
    RESET_COOLDOWN_SEC,
    RecoveryMechanism,
)

# The Galaxy escalation ladder's own thresholds and vocabulary. The gate takes its escalation
# DECISIONS from Recovery.next_stage/escalate now, but it still owns the reads and the journal
# around them: the floors a suppression event reports, the eth-freeze kill switch it folds into the
# evidence, the hold ceiling it names, and the ledger's own word for a host rung the router chose.
from tt_device_mcp.health.recovery.galaxy import (
    _HOST_ESCALATION_ACTION,
    GalaxyRecovery,
    _eth_freeze_holds,
    _galaxy_reset_min_dead_chips,
    _is_galaxy,
    _offbus_hold_ceiling_sec,
    _reboot_min_dead_chips,
    _stuck_hold_ceiling_sec,
    _ubb_reset_enabled,
)
from tt_device_mcp.health.recovery.pcie_guard import pcie_guard_at_start
from tt_device_mcp.health.recovery.per_target import PerTargetRecovery
from tt_device_mcp.health.recovery.stages.bridge_reset import (
    bridge_reset_enabled,
    bridge_reset_unavailable_reason,
    gone_chip_bridge_reset_enabled,
)

# Isolated to its own module so every test can replace the one line that actually shells out to
# the BMC (see the stage's own docstring); server.py's host-escalation ladder passes it straight
# through as the `fire` callback, so it needs the name itself, not a wrapper around it.
from tt_device_mcp.health.recovery.stages.power_cycle import _fire_power_cycle

__all__ = [
    # AICLK ceiling state (start, door, job end, dead-chip kill)
    "CEILING",
    # construction pieces ServerFsm.boot assembles into the process singletons
    "HealthMonitor",
    "RecoveryMechanism",
    "RecoveryDeps",
    "GalaxyRecovery",
    "PerTargetRecovery",
    "_persistent_recovery",
    "Recovery",
    # evidence/journal
    "append_trace",
    "capture_incident",
    "health_dir",
    "health_event",
    "mark_boot",
    "previous_boot_bus_locks",
    "previous_boot_error",
    "read_health_events",
    # boot: latch the off-bus reset gate when the last boot died inside one (spec 04 I25), and
    # record the PCI topology (I23)
    "pcie_guard_at_start",
    # vocabulary
    "HealthState",
    "Verdict",
    # probes
    "eth",
    "fabric",
    "ALL_ONES",
    "dead_chips",
    "heartbeat_supported",
    "heartbeat_verdict",
    "read_heartbeats",
    "hugepages_shortfall",
    "version_floor_warnings",
    "SAMPLE_INTERVAL_SEC",
    "SAMPLE_RING_SIZE",
    "aer_totals",
    "chip_node_present",
    "chip_pci_bdf",
    "chip_sample",
    "chip_snapshot",
    "chip_snapshot_event",
    "isolate_chip",
    # recovery selection/state
    "OUTCOME_RECOVERED",
    "OUTCOME_TERMINAL",
    "OUTCOME_WAITING",
    "RESET_MODE_GALAXY",
    "RESET_MODE_TARGET",
    "HOLD_ESCALATION_REARM_SEC",
    "RESET_COOLDOWN_SEC",
    "_declared_reset_mode",
    # the escalation decision the gate acts on: the evidence it hands next_stage, and every
    # decision value that comes back which is not itself a stage name
    "Evidence",
    "WAIT",
    "DEFER",
    "RELEASE",
    "HOLD_FABRIC_UNVERIFIED",
    "BLOCKED",
    # Galaxy escalation ladder: the thresholds and vocabulary the gate reads around the router's
    # decisions
    "_is_galaxy",
    "_eth_freeze_holds",
    "_galaxy_reset_min_dead_chips",
    "_HOST_ESCALATION_ACTION",
    "_offbus_hold_ceiling_sec",
    "_reboot_min_dead_chips",
    "_stuck_hold_ceiling_sec",
    "_ubb_reset_enabled",
    "bridge_reset_enabled",
    "bridge_reset_unavailable_reason",
    "gone_chip_bridge_reset_enabled",
    "_fire_power_cycle",
]
