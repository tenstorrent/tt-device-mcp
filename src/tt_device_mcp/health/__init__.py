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

# chip_state: the per-chip lost/recovering gate in front of every poller read of a chip (spec 03 I66).
# hold_report: the /health hold report (spec 03 I72, spec 04 I75): its pure helpers, plus the read-only probes
# server.py feeds them (board-type normalising, off-bus chip ids, PCI bus numbers, the UBB tray
# tables and the whole-tray classifier the power-cycle rung itself uses), so the report names a
# hold the way the ladder does.
from tt_device_mcp.health import chip_state, hold_report

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
from tt_device_mcp.health.monitor import (
    FABRIC_KILLED_BY_ISOLATION,
    HealthMonitor,
    _normalized_board,
    eth_read_timed_out,
    journal_interrupted_fabric_check,
)

# The fabric probe module itself, not a wrapping function: server.py's own preflight check reads
# fabric.build_command() directly to warn when no fabric check is configured OR installed, and a
# test monkeypatches fabric.build_command on this exact binding (see test_device_safety.py) — a
# wrapper function would leave that patch aimed at a name nothing here reads.
from tt_device_mcp.health.monitors import eth, fabric

# Heartbeat probe primitives the gate/telemetry sampler in server.py calls directly (off-bus
# counts, the dead-chip sampler, the startup probe) — not merely bounced into RecoveryDeps.
from tt_device_mcp.health.monitors.heartbeat import (
    ALL_ONES,
    arc_not_ready_chips,
    dead_chips,
    enodata_presence,
    heartbeat_supported,
    heartbeat_verdict,
    offbus_chip_ids,
    offbus_count,
    read_heartbeats,
    returned_chips,
)

# The host-software version floors the startup preflight reports. Sysfs reads only, so the
# preflight can assert them without spawning anything or touching a device.
from tt_device_mcp.health.monitors.hostpci import (
    CONFIG_UNREADABLE,
    hugepages_counts,
    hugepages_shortfall,
    link_down,
    version_floor_warnings,
)

# The tt-smi/sysfs telemetry-sampler primitives server.py's own polling loop runs directly.
from tt_device_mcp.health.monitors.pci import (
    SAMPLE_INTERVAL_SEC,
    SAMPLE_RING_SIZE,
    aer_totals,
    chip_node_present,
    chip_pci_bdf,
    chip_pci_bdfs,
    chip_sample,
    chip_snapshot,
    chip_snapshot_event,
    isolate_chip,
    link_signals,
    load_removed_chips,
    pci_bus_number,
    sysfs_chip_ids,
)

# The post-reset fabric debt (spec 04 I32): every reset path and broker start marks it owed, and
# server.py's release funnel and the gate read it to hold fabric-unverified until a pass settles it.
# The one in-flight PCI sysfs writer and its PCI-wedged latch (spec 04 I76): server.py wires the latch
# to the hold and the power-cycle request, and clears it only on a verified lift; telemetry.py's
# dead-chip valve skips chips while it is set and removes chips only through it.
from tt_device_mcp.health.pci_writer import PCI_WRITER, PciWedged
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
    RESET_MODE_GALAXY,
    RESET_MODE_TARGET,
    WAIT,
    Evidence,
    Recovery,
    RecoveryDeps,
    _declared_reset_mode,
    _persistent_recovery,
    offbus_power_cycle,
)
from tt_device_mcp.health.recovery import select_recovery as _select_recovery  # noqa: F401
from tt_device_mcp.health.recovery.base import (
    HOLD_ESCALATION_REARM_SEC,
    RESET_COOLDOWN_SEC,
    RESET_SCOPE_PREFIX,
    RecoveryMechanism,
    host_action_pending_sec,
    mesh_hold_repage_sec,
    mesh_reset_hold_reason,
    refuse_mesh_reset,
)

# The Galaxy escalation ladder's own thresholds and vocabulary. The gate takes its escalation
# DECISIONS from Recovery.next_stage/escalate now, but it still owns the reads and the journal
# around them: the floors a suppression event reports, the eth-freeze kill switch it folds into the
# evidence, the hold ceiling it names, and the ledger's own word for a host rung the router chose.
from tt_device_mcp.health.recovery.galaxy import (
    _HOST_ESCALATION_ACTION,
    UBB_CHIP_COUNT,
    GalaxyRecovery,
    _eth_freeze_holds,
    _galaxy_reset_min_dead_chips,
    _is_galaxy,
    _offbus_hold_ceiling_sec,
    _power_cycle_direct_class,
    _present_mesh_reset_grace_sec,
    _reboot_min_dead_chips,
    _stuck_hold_ceiling_sec,
    _ubb_bus_id_tables,
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
from tt_device_mcp.health.recovery.stages.power_cycle import run_pre_power_cycle_hook as _run_pre_power_cycle_hook
from tt_device_mcp.health.shutdown import note_broker_stopping

__all__ = [
    # the one in-flight PCI sysfs writer and its PCI-wedged latch (spec 04 I76)
    "PCI_WRITER",
    "PciWedged",
    # the /health hold report (spec 03 I72, spec 04 I75) and the read-only probes it is fed
    "hold_report",
    "_normalized_board",
    "offbus_chip_ids",
    "chip_pci_bdfs",
    "pci_bus_number",
    "UBB_CHIP_COUNT",
    "_power_cycle_direct_class",
    "_ubb_bus_id_tables",
    # AICLK ceiling state (start, door, job end, dead-chip kill)
    "CEILING",
    # construction pieces ServerFsm.boot assembles into the process singletons
    "HealthMonitor",
    "FABRIC_KILLED_BY_ISOLATION",  # spec 03 I82: the gate holds a pass the isolation killed dirty
    "journal_interrupted_fabric_check",
    "note_broker_stopping",
    "eth_read_timed_out",  # spec 03 I61: the stuck eth read the post-job present-mesh reset answers
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
    "chip_state",
    "dead_chips",
    "arc_not_ready_chips",
    "enodata_presence",
    "CONFIG_UNREADABLE",
    "link_down",
    "offbus_chip_ids",
    "offbus_count",
    "returned_chips",
    "heartbeat_supported",
    "heartbeat_verdict",
    "read_heartbeats",
    "hugepages_counts",
    "hugepages_shortfall",
    "version_floor_warnings",
    "SAMPLE_INTERVAL_SEC",
    "SAMPLE_RING_SIZE",
    "aer_totals",
    "chip_node_present",
    "chip_pci_bdf",
    "sysfs_chip_ids",
    "chip_sample",
    "chip_snapshot",
    "chip_snapshot_event",
    "isolate_chip",
    "link_signals",
    "load_removed_chips",
    # recovery selection/state
    "OUTCOME_RECOVERED",
    "OUTCOME_TERMINAL",
    "OUTCOME_WAITING",
    "RESET_MODE_GALAXY",
    "RESET_MODE_TARGET",
    "HOLD_ESCALATION_REARM_SEC",
    "RESET_COOLDOWN_SEC",
    # the reset's own systemd scope prefix: the in-flight reader cut never kills it (spec 03 I83)
    "RESET_SCOPE_PREFIX",
    "_declared_reset_mode",
    # how often a standing hardware-fault hold is paged again (spec 03 I42)
    "mesh_hold_repage_sec",
    # how long a requested host reboot/power cycle keeps the forced ladder from resetting
    "host_action_pending_sec",
    "mesh_reset_hold_reason",
    "refuse_mesh_reset",
    # the escalation decision the gate acts on: the evidence it hands next_stage, and every
    # decision value that comes back which is not itself a stage name
    "Evidence",
    "WAIT",
    "DEFER",
    "RELEASE",
    "HOLD_FABRIC_UNVERIFIED",
    "BELOW_FLOOR",
    "BLOCKED",
    # Galaxy escalation ladder: the thresholds and vocabulary the gate reads around the router's
    # decisions
    "_is_galaxy",
    "_eth_freeze_holds",
    "_galaxy_reset_min_dead_chips",
    "_HOST_ESCALATION_ACTION",
    "_offbus_hold_ceiling_sec",
    "_present_mesh_reset_grace_sec",
    "_reboot_min_dead_chips",
    "_stuck_hold_ceiling_sec",
    "bridge_reset_enabled",
    "bridge_reset_unavailable_reason",
    "gone_chip_bridge_reset_enabled",
    "_fire_power_cycle",
    "_run_pre_power_cycle_hook",
    # the bounded off-bus hold (spec 04 I27): the gate's hold-deadline check asks it when the
    # grace has run out and records the forced pass it spawned for that episode
    "offbus_power_cycle",
    # the post-reset fabric debt (spec 04 I32): no reset releases until a fabric pass settles it
    "FABRIC_DEBT",
]
