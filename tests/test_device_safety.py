# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The properties that make a device reset safe to run.

A reset is a routine repair, not a hazard — but only if it is exclusive, lands on
a quiesced bus, and cannot be interrupted partway through 32 ASICs. Each of those
is a real failure this broker caused in production, so each gets a test.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, fsm_healthy, patch_health_event, patch_recovery
from tt_device_mcp import device_holders, privileges
from tt_device_mcp import server as srv
from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerFsm, ServerState
from tt_device_mcp.health import evidence as health
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.core import HealthState, Verdict
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors import eth, heartbeat, pci
from tt_device_mcp.health.monitors.heartbeat import heartbeat_verdict, read_heartbeats
from tt_device_mcp.health.recovery import (
    BLOCKED,
    BRIDGE_RESET_MAX_TRIES,
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
    galaxy,
)
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery.galaxy import GalaxyRecovery
from tt_device_mcp.health.recovery.per_target import PerTargetRecovery
from tt_device_mcp.health.recovery.stages import bridge_reset as bridge


def _no_holders(monkeypatch):
    """No foreign tenant on the device, so the gate is free to act."""
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))


def _chip(sysfs, idx, beat):
    d = sysfs / f"tenstorrent!{idx}"
    d.mkdir(exist_ok=True)
    (d / "tt_heartbeat").write_text(f"{beat}\n")


def _chip_node_no_heartbeat(sysfs, idx):
    """A tenstorrent class node with no tt_heartbeat attribute — an old KMD."""
    (sysfs / f"tenstorrent!{idx}").mkdir(exist_ok=True)


# --- the Recovery._verify_device adapter --------------------------------------


@pytest.mark.asyncio
async def test_verify_device_adapter_forwards_the_callers_real_phase(monkeypatch):
    """The adapter used to hand monitor.update() a fixed "verify_device" literal because it had
    no gate phase to forward. The gate is now that caller and knows its own real phase
    ('pre-job'/'post-job'/'startup'), so it must reach HealthState.phase instead of being
    permanently mislabeled."""
    seen = {}

    async def fake_update(phase, **kw):
        seen["phase"] = phase
        return HealthState(phase=phase, at=datetime.now(), expected=kw.get("expected", 0))

    monkeypatch.setattr(srv.health_monitor, "update", fake_update)
    await srv.galaxy_recovery._verify_device(1, lambda m: None, phase="post-job")
    assert seen["phase"] == "post-job"


# --- the free liveness probe -------------------------------------------------


def test_heartbeat_healthy_when_all_chips_advance(monkeypatch):
    sysfs = pci.SYSFS_CLASS_DIR
    for i in range(4):
        _chip(sysfs, i, 100)

    # The probe samples, sleeps, samples again; advance every counter in between.
    def advance(_):
        for i in range(4):
            _chip(sysfs, i, 200)

    monkeypatch.setattr(health.time, "sleep", advance)
    verdict, detail, _ = heartbeat_verdict(expected_count=4)
    assert verdict is Verdict.HEALTHY, detail


def test_heartbeat_unhealthy_when_a_chip_arc_is_frozen(monkeypatch):
    sysfs = pci.SYSFS_CLASS_DIR
    for i in range(4):
        _chip(sysfs, i, 100)

    def advance(_):
        for i in range(4):
            if i != 2:  # chip 2's ARC is wedged: its counter never moves
                _chip(sysfs, i, 200)

    monkeypatch.setattr(health.time, "sleep", advance)
    verdict, detail, evidence = heartbeat_verdict(expected_count=4)
    assert verdict is Verdict.UNHEALTHY
    assert evidence["stalled"] == ["2"], detail


def test_heartbeat_unhealthy_when_chips_drop_off_the_bus(monkeypatch):
    for i in range(3):  # 3 present, 4 expected — a tray went down
        _chip(pci.SYSFS_CLASS_DIR, i, 100)
    verdict, detail, _ = heartbeat_verdict(expected_count=4)
    assert verdict is Verdict.UNHEALTHY
    assert "expected 4" in detail


def test_heartbeat_healthy_when_more_chips_than_a_stale_expected(monkeypatch):
    """A reset that recovered the full mesh leaves MORE chips on the bus than a stale,
    degraded high-water mark (expected baked at the survivor count). An over-count is a
    recovery, not a drop — reading it unhealthy would escalate a healthy box."""
    for i in range(5):  # 5 present, only 4 expected — the 5th just came back
        _chip(pci.SYSFS_CLASS_DIR, i, 100)

    def advance(_):
        for i in range(5):
            _chip(pci.SYSFS_CLASS_DIR, i, 200)

    monkeypatch.setattr(health.time, "sleep", advance)
    verdict, detail, _ = heartbeat_verdict(expected_count=4)
    assert verdict is Verdict.HEALTHY, detail


def test_heartbeat_probe_touches_no_device(monkeypatch):
    """The entire reason this probe exists: tt-smi does a full device init on every
    call, and a device init aimed at a wedged chip is what escalates a PCIe error to
    fatal and reboots the host. Reading sysfs must not shell out to anything."""

    def boom(*a, **k):
        raise AssertionError("the heartbeat probe must not execute any subprocess")

    monkeypatch.setattr(srv.subprocess, "run", boom)
    _chip(pci.SYSFS_CLASS_DIR, 0, 1)
    assert read_heartbeats() == {"0": 1}


def test_heartbeat_boot_false_journals_sysfs_absent(monkeypatch):
    """An empty class dir at boot — no chips enumerated — must not silently disable the
    rung. The determination is journaled loudly and tagged as the alarm case
    (sysfs_absent), so it reads as "nothing enumerated" not the tolerable old-KMD one."""
    assert heartbeat.heartbeat_supported(refresh=True) is False
    events = health.read_health_events(kinds={"heartbeat_unsupported"})
    assert [e["reason"] for e in events] == ["sysfs_absent"], events


def test_heartbeat_boot_false_journals_attr_absent(monkeypatch):
    """Nodes present but no tt_heartbeat attribute is the tolerable old-KMD degrade — still
    journaled, but tagged distinctly from the no-chips alarm so an operator can tell them
    apart from the one durable record."""
    _chip_node_no_heartbeat(pci.SYSFS_CLASS_DIR, 0)
    _chip_node_no_heartbeat(pci.SYSFS_CLASS_DIR, 1)
    assert heartbeat.heartbeat_supported(refresh=True) is False
    events = health.read_health_events(kinds={"heartbeat_unsupported"})
    assert [e["reason"] for e in events] == ["attr_absent"], events


def test_heartbeat_supported_present_stays_silent(monkeypatch):
    """When the probe works, nothing is journaled — the loud event is reserved for a real
    degrade so it never becomes noise the operator learns to scroll past."""
    _chip(pci.SYSFS_CLASS_DIR, 0, 100)
    assert heartbeat.heartbeat_supported(refresh=True) is True
    assert health.read_health_events(kinds={"heartbeat_unsupported"}) == []


def test_heartbeat_boot_false_does_not_latch_off_after_chips_return(monkeypatch):
    """A box that boots with its chips off the bus must not disable the live-drop detector
    for the life of the process. sysfs_absent is unknowable, not "unsupported": the first
    probe reports False (nothing to sample) but does NOT cache it, so a cold power cycle
    that re-enumerates the chips WITHOUT a broker restart re-arms the rung on the next
    probe. Latching False here is what left blx02's fastest drop detector off after boot."""
    assert heartbeat.heartbeat_supported() is False  # boots dead: nothing enumerated
    _chip(pci.SYSFS_CLASS_DIR, 0, 100)  # cold power cycle brings a chip back
    assert heartbeat.heartbeat_supported() is True  # re-armed with no refresh, no restart


def test_heartbeat_absent_probes_journal_once_not_every_poll(monkeypatch):
    """Re-probing an empty class dir must not re-journal on every idle poll — the durable
    log would drown in duplicates. One event per absence; re-armed only once chips return."""
    for _ in range(4):
        assert heartbeat.heartbeat_supported() is False
    events = health.read_health_events(kinds={"heartbeat_unsupported"})
    assert [e["reason"] for e in events] == ["sysfs_absent"], events


# --- the authoritative "is the device degraded" check ------------------------


def test_degraded_reason_flags_a_chip_off_the_bus_with_no_flag_set(monkeypatch):
    """A chip can fall off the bus with nothing having flagged it — a spontaneous drop, or
    a broker restart that lost the in-memory dirty flag. The authoritative check reads it
    live from sysfs, so a status query cannot report a dead mesh as free."""
    monkeypatch.setattr(srv, "device_op_active", "")  # in-memory record: device is free
    _chip(pci.SYSFS_CLASS_DIR, 0, 100)
    _chip(pci.SYSFS_CLASS_DIR, 1, heartbeat.ALL_ONES)  # ...but chip 1 is off the bus, now
    reason = srv._device_degraded_for_tenant()
    assert "1" in reason and "bus" in reason.lower(), reason


def test_degraded_reason_empty_on_a_healthy_idle_mesh(monkeypatch):
    monkeypatch.setattr(srv, "device_op_active", "")
    for i in range(4):
        _chip(pci.SYSFS_CLASS_DIR, i, 100)
    assert srv._device_degraded_for_tenant() == ""


def test_degraded_reason_still_reports_the_in_memory_signals(monkeypatch):
    """The live probe is additive: a broker op or a dirty flag degrades the device even
    when every chip's heartbeat is fine."""
    for i in range(4):
        _chip(pci.SYSFS_CLASS_DIR, i, 100)
    monkeypatch.setattr(srv, "device_op_active", "reset")
    monkeypatch.setattr(srv, "device_op_detail", "galaxy reset")
    assert "reset" in srv._device_degraded_for_tenant()


# --- the visible HOLD (G2c) --------------------------------------------------


def test_hold_state_latches_a_held_device_and_clears_when_fit(monkeypatch):
    """A device degraded and idle (no broker op) is HELD from tenants. The hold carries a
    stable 'since' so a watcher sees how long it has sat refused — the latch does not
    re-stamp on every poll — and it clears the instant the device is fit again."""
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_held_since", "")
    fsm_dirty(srv, "job 070 ended killed")

    hold = srv._device_hold_state()
    assert hold is not None
    reason, since = hold
    assert "killed" in reason and since
    assert srv._device_hold_state()[1] == since  # idempotent latch: same since, not re-stamped

    fsm_healthy(srv)
    assert srv._device_hold_state() is None  # fit again -> no hold row
    assert srv.device_held_since == ""  # latch cleared


def test_hold_state_suppressed_while_a_broker_op_runs(monkeypatch):
    """A reset/fabric pass already occupies the RUNNING row and IS the recovery; a second
    HOLD row for the same device would read as two things happening at once. The latch is
    kept running underneath, so the hold resumes with its original clock once the op ends."""
    monkeypatch.setattr(srv, "device_held_since", "2026-07-16T00:00:00")
    monkeypatch.setattr(srv, "device_op_active", "reset")
    monkeypatch.setattr(srv, "device_op_detail", "galaxy reset")
    fsm_dirty(srv, "chip 26 fell off the bus")

    assert srv._device_hold_state() is None
    assert srv.device_held_since == "2026-07-16T00:00:00"  # latch preserved across the op


# --- /health exposes the device hold state (Part C) --------------------------


def test_health_payload_reports_a_held_device_as_degraded(monkeypatch):
    """A box sitting held-and-refused must not read 'ok' to a /health poller — the gap that
    let a stuck hold go unseen for hours. ``status`` flips to 'degraded' and the body carries
    the hold's age and reason so a monitor can alarm on how long the device has been unusable."""
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_held_since", "")
    fsm_dirty(srv, "chip 22 fell off the bus")

    srv._device_hold_state(datetime.fromisoformat("2026-07-22T00:00:00"))  # latch the hold
    payload = srv._health_payload(now=datetime.fromisoformat("2026-07-22T00:20:00"))

    assert payload["status"] == "degraded"
    assert payload["held"] is True
    assert payload["held_age_sec"] == 1200
    assert payload["held_since"] == "2026-07-22T00:00:00"
    assert "chip 22" in payload["held_reason"]


def test_health_payload_reads_ok_on_a_fit_device(monkeypatch):
    """A fit, idle device reads 'ok' with no hold. The detector is absent in tests, so no live
    chip probe contradicts the clean in-memory state."""
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_held_since", "")

    payload = srv._health_payload()
    assert payload["status"] == "ok"
    assert payload["held"] is False
    assert "held_age_sec" not in payload


def test_tenant_gate_writes_one_held_and_one_released_per_episode(monkeypatch):
    """The refusals are per-job and in-memory; the durable timeline needs exactly one mark
    when a device opens a tenant-refused hold and one when it lifts — not one per refused
    job (that floods the journal during an outage), and not zero (a spontaneous drop the
    live probe caught set no dirty flag, so nothing else recorded it)."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_logged", False)

    srv._note_tenant_gate_verdict("chip 1 fell off the PCIe bus")
    srv._note_tenant_gate_verdict("chip 1 fell off the PCIe bus")  # same outage, next job
    assert [k for k, _ in events] == ["device_held"], "an outage must log one held event, not one per refusal"
    assert "bus" in events[0][1]["reason"]

    srv._note_tenant_gate_verdict("")  # a tenant job passed -> the device recovered
    srv._note_tenant_gate_verdict("")  # still fit; nothing new to say
    assert [k for k, _ in events] == ["device_held", "device_released"]

    srv._note_tenant_gate_verdict("chip 2 fell off the PCIe bus")  # a fresh outage reopens it
    assert [k for k, _ in events] == ["device_held", "device_released", "device_held"]


def test_idle_blackout_writes_a_durable_held_event_with_no_job_flowing(monkeypatch):
    """A device that goes degraded with an empty queue must still leave a durable trace. The
    tenant gate only writes the held/released marks at a job boundary, so before this an idle
    blackout set device_dirty and a status-query hold but nothing on the timeline — the window
    it was unusable in was invisible. The always-on sampler now drives the same ledger."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_held_since", "")
    monkeypatch.setattr(srv, "device_op_active", "")  # no op, no gate — only the sampler is here

    fsm_dirty(srv, "all 32 chips stopped answering on the PCIe bus", why="heartbeat")
    srv._refresh_idle_hold_ledger()
    srv._refresh_idle_hold_ledger()  # still degraded, still idle -> one held, not one per sample
    assert [k for k, _ in events] == ["device_held"]
    assert "answering" in events[0][1]["reason"]

    fsm_healthy(srv)
    srv._refresh_idle_hold_ledger()  # mesh back while still idle -> the sampler closes it too
    assert [k for k, _ in events] == ["device_held", "device_released"]


def test_a_hold_that_outlives_the_deadline_is_flagged_to_the_durable_timeline(monkeypatch):
    """A hold must terminate — in a verified recovery or the next rung. When the idle escalation
    defers on all its fail-closed guards (opted out, a persistent tenant or an unreadable holder
    scan, its one reset already spent, a killed relift), the hold sat silent for hours after the
    opening device_held — the 17h idle blackout the incident found. The sampler now flags any hold
    past the deadline to the durable timeline, so a monitor keying on the timeline sees the stuck
    box instead of nothing."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_op_active", "")  # idle: only the sampler is here
    monkeypatch.setattr(srv, "device_hold_logged", True)  # the episode is already open
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    srv.fsm.set_latch("escalated", False)
    monkeypatch.setattr(
        srv, "device_hold_episode_reason", "eth/fabric fault on a present mesh (all 32 chips on the bus)"
    )
    # the episode opened well past the deadline ago and the mesh is still degraded and idle
    old = (datetime.now() - timedelta(seconds=srv._hold_deadline_sec() + 60)).isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", old)
    fsm_dirty(srv, "eth/fabric fault on a present mesh", why="eth_frozen")

    srv._refresh_idle_hold_ledger()

    stuck = [f for k, f in events if k == "hold_stuck_past_deadline"]
    assert stuck, "a hold past the deadline must be flagged to the durable timeline"
    assert stuck[0]["host_at_risk"] is True
    assert stuck[0]["held_age_sec"] >= srv._hold_deadline_sec()
    assert "eth/fabric" in stuck[0]["reason"]

    srv._refresh_idle_hold_ledger()  # still held, same deadline window -> not one alert per sample
    assert len([f for k, f in events if k == "hold_stuck_past_deadline"]) == 1


def test_a_hold_within_the_deadline_is_not_flagged_stuck(monkeypatch):
    """The watchdog must fire only once a hold has genuinely outlived the deadline, never on every
    held device — a self-heal hold the idle relift clears in an early window is not stuck."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_hold_logged", True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_episode_reason", "held for self-heal")
    recent = (datetime.now() - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", recent)
    fsm_dirty(srv, "held for self-heal", why="off_bus")

    srv._refresh_idle_hold_ledger()
    assert not [k for k, _ in events if k == "hold_stuck_past_deadline"]


def test_the_stuck_hold_watchdog_re_alerts_each_window_and_re_arms_per_episode(monkeypatch):
    """One alert per deadline window, not one per sample; a fresh alert each further window so a
    recency monitor keeps firing; and the latch re-arms when the episode closes, so the next
    stuck hold is flagged on its own clock rather than swallowed by the last one's spent bucket."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    srv.fsm.set_latch("escalated", False)
    monkeypatch.setattr(srv, "device_hold_episode_reason", "eth/fabric fault on a present mesh")
    since = datetime(2026, 7, 26, 9, 56, 22)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    d = srv._hold_deadline_sec()

    def n_stuck():
        return len([k for k, _ in events if k == "hold_stuck_past_deadline"])

    srv._check_hold_deadline(now=since + timedelta(seconds=d - 1))  # under the deadline
    assert n_stuck() == 0
    srv._check_hold_deadline(now=since + timedelta(seconds=d + 1))  # first window crossed
    assert n_stuck() == 1
    srv._check_hold_deadline(now=since + timedelta(seconds=d + 30))  # same window, next sample
    assert n_stuck() == 1
    srv._check_hold_deadline(now=since + timedelta(seconds=2 * d + 1))  # next window re-alarms
    assert n_stuck() == 2

    monkeypatch.setattr(srv, "device_hold_logged", True)
    srv._note_tenant_gate_verdict("")  # the device comes back fit -> episode closes
    assert srv.device_hold_deadline_bucket == 0, "a closed episode must re-arm the watchdog"


def test_a_closed_episode_re_arms_the_forced_escalation_windows(monkeypatch):
    """The forced-escalation latch (device_hold_escalate_bucket) must re-arm on release, exactly like
    the deadline-alert latch beside it: it is what gates the one-per-ceiling-window forced recovery, so
    a stale value from a prior episode suppresses the next hold's escalation until its age crosses the
    stale window instead of its own 20-min ceiling — a hold past the ceiling the forced escalation never
    acts on. The first crossing of a broker uptime fires (latch 0), so the bug hides until a second hold
    strands on the same uptime."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    srv.fsm.set_latch("escalated", False)
    monkeypatch.setattr(srv, "device_hold_episode_reason", "eth/fabric fault on a present mesh")
    # Stub a successful spawn: a sync test has no event loop, so the real one declines and the
    # caller would never advance the latch. Under test is the latch's re-arm on episode close.
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: True)
    since = datetime(2026, 7, 26, 9, 56, 22)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    ceiling = srv._stuck_hold_ceiling_sec()

    # a hold standing past two ceiling windows advances the forced-escalation latch off zero
    srv._check_hold_deadline(now=since + timedelta(seconds=2 * ceiling + 1))
    assert srv.device_hold_escalate_bucket >= 1, "the latch must advance once the hold outlives the ceiling"

    monkeypatch.setattr(srv, "device_hold_logged", True)
    srv._note_tenant_gate_verdict("")  # the device comes back fit -> episode closes
    assert srv.device_hold_deadline_bucket == 0, "a closed episode re-arms the deadline watchdog"
    assert srv.device_hold_escalate_bucket == 0, "a closed episode must re-arm the forced-escalation windows too"


def _hold_deadline_probe(monkeypatch, *, isolated):
    """Arm a fresh hold episode and record every forced escalation _check_hold_deadline spawns."""
    fired = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: None)
    # returns True: the spawn succeeded. The caller now consumes its latch only on a real spawn.
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)
    srv.fsm.set_latch("escalated", False)
    monkeypatch.setattr(srv, "isolated_chips", set(isolated))
    since = datetime(2026, 8, 15, 1, 17, 33)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    return fired, since


def test_an_offbus_hold_escalates_long_before_the_general_ceiling(monkeypatch):
    """A chip off the PCIe bus never returns by waiting, so it must not serve the 20-min ceiling that
    exists for a self-healing ARC wedge. Its answer is the cheap bridge-reset + rescan rung."""
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chip(s) 6,7 fell off the PCIe bus")
    fired, since = _hold_deadline_probe(monkeypatch, isolated={"6", "7"})
    offbus = srv._offbus_hold_ceiling_sec()
    assert offbus < srv._stuck_hold_ceiling_sec(), "the off-bus ceiling is the whole point — it must be shorter"

    srv._check_hold_deadline(now=since + timedelta(seconds=offbus - 1))
    assert fired == [], "must not escalate before its own ceiling"

    srv._check_hold_deadline(now=since + timedelta(seconds=offbus + 1))
    assert len(fired) == 1, "an off-bus hold past the short ceiling must get the ladder immediately"


def test_a_present_mesh_hold_still_waits_the_full_ceiling(monkeypatch):
    """The short clock is scoped to off-bus. A present mesh whose ARC merely wedged does self-heal, and
    resetting it early throws away a recovery that costs nothing to wait for."""
    monkeypatch.setattr(srv, "device_hold_episode_reason", "eth/fabric fault on a present mesh")
    fired, since = _hold_deadline_probe(monkeypatch, isolated=set())

    srv._check_hold_deadline(now=since + timedelta(seconds=srv._offbus_hold_ceiling_sec() + 1))
    assert fired == [], "no chip is off the bus, so the short ceiling must not apply"

    srv._check_hold_deadline(now=since + timedelta(seconds=srv._stuck_hold_ceiling_sec() + 1))
    assert len(fired) == 1, "the general ceiling must still fire"


def test_an_offbus_hold_gets_one_fast_attempt_not_a_fast_cadence(monkeypatch):
    """Each forced escalation climbs a rung past the one-reset-per-episode latch, so repeating on the
    120s clock would walk a shared box to a warm reboot and a BMC power cycle within minutes. The
    early off-bus window is a one-shot; later windows fall back to the general ceiling. (ladder-v2
    removed the separate risky-floor window, so it is two forced runs per episode, each on its own
    clock — never a cadence.)"""
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chip(s) 6,7 fell off the PCIe bus")
    fired, since = _hold_deadline_probe(monkeypatch, isolated={"6", "7"})
    offbus = srv._offbus_hold_ceiling_sec()

    srv._check_hold_deadline(now=since + timedelta(seconds=offbus + 1))
    assert len(fired) == 1

    for mult in (2, 3):
        srv._check_hold_deadline(now=since + timedelta(seconds=offbus * mult + 1))
    assert len(fired) == 1, "the short clock must fire once, never as a cadence"

    srv._check_hold_deadline(now=since + timedelta(seconds=srv._stuck_hold_ceiling_sec() + 1))
    assert len(fired) == 2, "the early off-bus attempt is additive: the ceiling window still fires on its own clock"


@pytest.mark.asyncio
async def test_forced_escalation_fires_on_a_hold_held_only_by_a_reported_fault(monkeypatch, tmp_path):
    """A hold whose sole cause is a runtime-reported eth/fabric fault must still get the forced ladder
    past the ceiling. device_fault_reported is a first-class tenant-refused reason
    (_device_unavailable_for_tenant), and it outlives the reset that clears device_dirty — the checks
    the reset verifies with are blind to this fault, so dirty and unverified go clear while the fault
    stands and holds the door. If the escalation's "did the hold clear?" check counts only dirty and
    unverified, it reads the box as fit and bails, stranding the exact eth-wedge class the ceiling
    teeth exist to terminate."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    # dirty and unverified stay clear (conftest zeroes them); only the reported fault holds the device.
    monkeypatch.setattr(
        srv, "device_fault_reported", "job 571 eth/fabric fault: 'waiting for active ethernet core'", raising=False
    )
    # select_recovery must resolve to the Galaxy ladder for this 32-chip mesh, not the per-target
    # default it falls back to with no board type cached.
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    # a present 32-chip mesh -> off_bus 0 -> the present-mesh rung, not the off-bus one.
    dev = tmp_path / "tenstorrent"
    dev.mkdir()
    for i in range(32):
        (dev / str(i)).mkdir()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: present)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32)})
    monkeypatch.setattr(srv, "dead_chips", lambda beats: [])
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)
    monkeypatch.setattr(srv, "device_op_lock", None)  # bind a fresh lock to this test's loop

    climbed = []

    async def _fake_present(indices, expected, log, *, force=False):
        climbed.append(("present", force))

    async def _fake_offbus(indices, expected, log, *, force=False):
        climbed.append(("offbus", force))

    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_stuck_hold", _fake_present)
    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_offbus_stuck_hold", _fake_offbus)

    await srv._force_escalate_stuck_hold()

    assert (
        "hold_deadline_forced_escalation" in events
    ), "a hold held only by a reported fault must force the ladder, not read as already cleared"
    assert climbed == [
        ("present", True)
    ], "the present-mesh reported-fault hold must climb the present-mesh ladder, forced past the defers"


@pytest.mark.asyncio
async def test_forced_escalation_power_cycles_when_all_device_nodes_are_gone(monkeypatch, tmp_path):
    """A full sysfs blackout — every /dev/tenstorrent node gone — is the catastrophic all-off-bus drop
    that most needs the cold rung. Base bailed on ``if not indices: return``, so the deadline watchdog
    kept advancing the escalate bucket while nothing ran — the forced-escalation guarantee devolving into
    an indefinite hold precisely when the nodes disappear. expected falls back to the host baseline, so
    off_bus reads full-mesh and the off-bus ladder (which climbs an all-off-bus mesh to the power cycle)
    runs. Fails on base, which returns before escalating."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    fsm_dirty(srv, "all chips off the bus", why="heartbeat")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    dev = tmp_path / "tenstorrent"
    dev.mkdir()  # exists but holds NO digit nodes -> indices == []
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)  # a host with a 32-chip baseline
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {})
    monkeypatch.setattr(srv, "dead_chips", lambda beats: [])
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)
    monkeypatch.setattr(srv, "device_op_lock", None)  # bind a fresh lock to this test's loop

    climbed = []

    async def _fake_offbus(indices, expected, log, *, force=False):
        climbed.append(("offbus", expected, force))

    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_offbus_stuck_hold", _fake_offbus)

    await srv._force_escalate_stuck_hold()

    assert (
        "hold_deadline_forced_escalation" in events
    ), "an all-nodes-gone blackout past the ceiling must force the ladder, not bail on empty sysfs"
    assert climbed == [
        ("offbus", 32, True)
    ], "a full blackout is all-off-bus (off_bus == expected) -> the off-bus ladder climbs to the power cycle"


@pytest.mark.asyncio
async def test_an_all_chips_blackout_needs_two_samples_before_it_dirties_the_device(monkeypatch):
    """Every chip reading all-ones at once is the driver, the bus, or a reset — which is why it
    never isolates. It must not dirty the device on one sample either: anything that re-inits
    the mesh (a job's teardown, tt-smi, the validator) makes the heartbeat read all-ones for a
    moment, and with a BLOCKING tenant gate one untrusted sample takes a healthy box offline.
    Measured in prod: a queue held ~7min on "all 32 chips stopped answering" while all 32 ARC
    heartbeats were advancing."""
    srv.sampler.dead_chip_strikes = {}
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    marks = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **kw: marks.append(reason))
    patch_health_event(monkeypatch, lambda kind, **f: None)
    blackout = {str(i): (heartbeat.ALL_ONES,) for i in range(32)}

    await srv.sampler.check_for_dead_chips(blackout)
    assert marks == [], "a one-sample all-chips blackout is a re-init blip, not a degraded device"

    await srv.sampler.check_for_dead_chips(blackout)
    assert len(marks) == 1 and "stopped answering" in marks[0], "a confirmed blackout must dirty"


@pytest.mark.asyncio
async def test_an_idle_all_gone_drop_is_confirmed_by_the_sampler_and_put_on_the_timeline(monkeypatch):
    """Every chip off the bus reads as an EMPTY sample, not 32 all-ones — chip_sample() omits a
    chip whose node is gone — so the all-ones blackout branch never saw it. On a fully idle box no
    job gate or status query runs the fuller liveness probe, so the single worst state (absence is
    never health) set no dirty flag, wrote no held event, and never armed the deadline watchdog
    until something finally poked it. The always-on sampler now confirms it on two samples and
    drives the same durable ledger — with no reset, no reboot, no power cycle."""
    srv.sampler.all_chips_gone_strikes = 0
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")  # this host is a 32-chip mesh
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_held_since", "")
    monkeypatch.setattr(srv, "device_op_active", "")  # idle: only the sampler is here

    # One empty sample is a rescan/re-init blip until a second confirms it: nothing flagged yet.
    await srv.sampler.check_for_dead_chips({})
    assert srv.fsm.state is ServerState.HEALTHY, "a one-sample sysfs blackout is a blip, not a confirmed drop"
    assert not [k for k, _ in events], "a one-sample blackout must not journal a loss"

    await srv.sampler.check_for_dead_chips({})
    assert srv.fsm.state is not ServerState.HEALTHY, "an all-gone mesh on two samples must dirty, not read as fit"
    off_bus = [f for k, f in events if k == "all_chips_off_bus"]
    assert off_bus, "the catastrophic idle loss must be on the durable timeline"
    assert off_bus[0]["present"] == 0 and off_bus[0]["expected"] == 32
    assert off_bus[0]["host_at_risk"] is True

    # The idle sampler's ledger now writes the durable held mark and arms the deadline watchdog,
    # with no job flowing — the visibility gap that left an idle all-gone drop silent.
    srv._refresh_idle_hold_ledger()
    assert [k for k, _ in events if k == "device_held"], "the idle hold must reach the timeline"
    assert srv.device_hold_episode_since, "the deadline watchdog's clock must be armed"

    # Held and still idle -> one held mark, not one per sample.
    await srv.sampler.check_for_dead_chips({})
    srv._refresh_idle_hold_ledger()
    assert len([k for k, _ in events if k == "device_held"]) == 1
    assert len([k for k, _ in events if k == "all_chips_off_bus"]) == 1, "no re-journal while held"


@pytest.mark.asyncio
async def test_an_all_gone_drop_on_a_held_not_dirty_box_still_journals_and_goes_dirty(monkeypatch):
    """The sampler's re-journal suppression keys on the episode's DIRTY axis, not on the FSM merely
    being non-HEALTHY. A box under an affirmative hold (a frozen eth core, held dirty=False for
    self-heal) that then loses EVERY sysfs node has escalated to the single worst state — merge-base
    suppressed only on device_dirty, so this fired the host-at-risk event and re-raised the dirty
    flag. Suppressing on any open episode would leave the blackout off the durable timeline and the
    next gate owing no reset, with the box still classified as a self-healing eth hold."""
    srv.sampler.all_chips_gone_strikes = 0
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    # An affirmative self-heal hold: open episode, dirty deliberately dropped.
    srv.fsm.on_fault("eth_frozen", detail="active-eth heartbeat frozen — held for self-heal", dirty=False)
    assert srv.fsm.state is not ServerState.HEALTHY and srv.fsm.record.dirty is False

    await srv.sampler.check_for_dead_chips({})  # first empty sample: blip until confirmed
    assert not [k for k, _ in events if k == "all_chips_off_bus"]

    await srv.sampler.check_for_dead_chips({})  # confirmed: must journal despite the open hold
    off_bus = [f for k, f in events if k == "all_chips_off_bus"]
    assert off_bus, "a held-not-dirty box that loses every node must still write the host-at-risk event"
    assert off_bus[0]["host_at_risk"] is True
    assert srv.fsm.record.dirty is True, "the episode must go dirty so the next gate owes a real reset"
    assert (
        srv.fsm.record.why == "eth_frozen"
    ), "the dirty mark must not overwrite the hold's classification (on_fault's existing-hold rule)"

    await srv.sampler.check_for_dead_chips({})  # now dirty: back to one journal entry, not one per sample
    assert len([k for k, _ in events if k == "all_chips_off_bus"]) == 1, "no re-journal once dirty"


@pytest.mark.asyncio
async def test_a_device_less_host_never_reads_an_empty_sysfs_as_a_drop(monkeypatch):
    """A box with no TT silicon has an empty class dir as its normal state. It must never be read
    as an all-gone drop — expected_chip_count is 0 there (no baseline was ever written), which is
    exactly what keeps this off a device-less host while catching it on a mesh that dropped."""
    srv.sampler.all_chips_gone_strikes = 0
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.delenv("TT_DEVICE_MCP_EXPECTED_CHIPS", raising=False)  # no override; fresh HEALTH_DIR -> baseline 0
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    await srv.sampler.check_for_dead_chips({})
    await srv.sampler.check_for_dead_chips({})
    assert srv.fsm.state is ServerState.HEALTHY, "a device-less host must not be flagged as an all-gone drop"
    assert not [k for k, _ in events if k == "all_chips_off_bus"]


@pytest.mark.asyncio
async def test_a_queued_job_survives_the_broker_restarting_under_it(monkeypatch, tmp_path, clear_job_state):
    """The queue was an in-memory asyncio.Queue: a restart re-adopted what was RUNNING and
    silently ate everything WAITING. Restarts are routine here — an autoupdate deploys on any
    idle window — so a tenant's queued work must outlive one, in the order it was queued."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()

    for jid, cmd in (("001", "pytest first"), ("002", "pytest second")):
        srv._persist_queued_job(
            srv.Job(
                id=jid,
                owner="jsmith",
                workspace="/w",
                command=cmd,
                queued_at=f"2026-07-17T00:00:0{jid[-1]}",
                timeout_sec=600,
            )
        )
    # the broker dies: in-memory state is gone, only the specs on disk remain
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()

    await srv._restore_queued_jobs()

    assert sorted(srv.jobs) == ["001", "002"], "queued jobs did not survive the restart"
    assert srv.jobs["001"].command == "pytest first"
    assert srv.jobs["001"].owner == "jsmith" and srv.jobs["001"].timeout_sec == 600
    assert srv.get_job_queue().get_nowait() == "001", "restored out of queue order"
    assert srv.get_job_queue().get_nowait() == "002"


@pytest.mark.asyncio
async def test_a_started_job_is_not_revived_by_a_restart(monkeypatch, tmp_path, clear_job_state):
    """Once a job's process is live its scope is what a restart re-adopts, so the queued spec
    must be gone: reviving it would run a tenant's command a second time."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()
    srv._persist_queued_job(
        srv.Job(id="003", owner="jsmith", workspace="/w", command="pytest", queued_at="2026-07-17T00:00:00")
    )
    srv._forget_queued_job("003")  # the runner spawned it
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()

    await srv._restore_queued_jobs()
    assert srv.jobs == {}, "a job that already started was re-queued by the restart"


@pytest.mark.parametrize("door", ["degraded", "busy", "privsep"])
@pytest.mark.parametrize("refusal_raises", [False, True], ids=["refused", "refusal-raised"])
@pytest.mark.asyncio
async def test_a_job_refused_at_the_door_is_not_revived_by_a_restart(
    monkeypatch, tmp_path, clear_job_state, door, refusal_raises
):
    """A job refused at the door (degraded device, a process outside the broker holding the
    device, or a privsep identity we cannot honour) is FAILED and the submitter is told so.
    Its queued spec must go with it: if it stays on disk, the next broker re-queues it and
    runs a command its owner was told never ran. That holds for the runner's fallback too,
    when the refusal helper itself raises."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    monkeypatch.setattr(srv, "_tenant_hold_enabled", lambda: False)
    monkeypatch.setattr(srv, "_note_tenant_gate_verdict", lambda reason: None)

    async def gate(job_log_file):
        return "chip 1 off the bus" if door == "degraded" else ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", gate)
    monkeypatch.setattr(srv, "privsep_refusal", lambda uid: "no passwd entry" if door == "privsep" else "")
    monkeypatch.setattr(srv, "_tenant_holder_reason", lambda: "device held outside the broker" if door == "busy" else "")
    if refusal_raises:

        async def boom(*a, **k):
            raise RuntimeError("refusal helper bug")

        monkeypatch.setattr(srv, "_refuse_job_on_degraded_device", boom)
        monkeypatch.setattr(srv, "_refuse_job_privsep_identity", boom)

    marker = tmp_path / "the_command_ran"
    job = srv.Job(id="904", owner="tenant", workspace="/tmp", command=f"touch {marker}", queued_at="t")
    srv.jobs["904"] = job
    srv._persist_queued_job(job)
    await srv.get_job_queue().put("904")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(100):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.FAILED, f"job 904 reached {job.status.value}, expected a door refusal"
    assert not marker.exists(), "the refused job's command ran"

    # the broker restarts: in-memory state is gone, only what is on disk remains
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()
    await srv._restore_queued_jobs()

    assert srv.jobs == {}, "a job refused at the door was re-queued by the restart"
    assert srv.get_job_queue().empty()


@pytest.mark.parametrize("spawn", ["shell", "privsep-exec"])
@pytest.mark.asyncio
async def test_a_job_whose_spawn_raised_is_not_revived_by_a_restart(monkeypatch, tmp_path, clear_job_state, spawn):
    """A job whose process could not be spawned (fork failed, systemd-run missing) is FAILED and
    the submitter is told so. Its queued spec must go with it: if it stays on disk, the next
    broker re-queues it and runs a command its owner was told had failed."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)

    async def _free_gate(job_log_file):
        return ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _free_gate)

    async def boom(*a, **k):
        raise OSError("fork failed")

    if spawn == "shell":
        monkeypatch.setattr(srv.asyncio, "create_subprocess_shell", boom)
    else:
        monkeypatch.setattr(srv, "privsep_refusal", lambda uid: "")
        monkeypatch.setattr(srv, "privsep_prefix_for", lambda uid, unit=None: ["systemd-run", "--scope"])
        monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", boom)

    job = srv.Job(id="905", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    srv.jobs["905"] = job
    srv._persist_queued_job(job)
    await srv.get_job_queue().put("905")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(100):
            if job.finished_at:
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.FAILED, f"job 905 reached {job.status.value}, expected a failed spawn"
    assert "fork failed" in job.error

    # the broker restarts: in-memory state is gone, only what is on disk remains
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()
    await srv._restore_queued_jobs()

    assert srv.jobs == {}, "a job whose spawn raised was re-queued by the restart"
    assert srv.get_job_queue().empty()


@pytest.mark.asyncio
async def test_an_unreadable_queued_spec_is_set_aside_not_guessed_at(monkeypatch, tmp_path, clear_job_state):
    """A spec we cannot parse is the one case a job may be dropped — but it is moved aside and
    recorded, never silently discarded, and it must not take the readable jobs down with it."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()
    (tmp_path / srv.QUEUED_SPEC_DIR).mkdir(parents=True, exist_ok=True)
    (tmp_path / srv.QUEUED_SPEC_DIR / "004.json").write_text("{not json")
    srv._persist_queued_job(
        srv.Job(id="005", owner="jsmith", workspace="/w", command="pytest ok", queued_at="2026-07-17T00:00:01")
    )
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()

    await srv._restore_queued_jobs()

    assert list(srv.jobs) == ["005"], "a bad spec must not stop the good ones being restored"
    assert (tmp_path / srv.QUEUED_SPEC_DIR / "004.invalid").exists(), "bad spec vanished silently"


@pytest.mark.parametrize("step", ["started-log", "activation", "privsep-prefix"])
@pytest.mark.asyncio
async def test_a_setup_error_after_running_fails_the_job_not_the_runner(monkeypatch, tmp_path, clear_job_state, step):
    """Between flipping a job to RUNNING and spawning it, the runner writes the job's
    '[Started at]' line, builds its activation script and its privsep prefix. Any of those can
    raise (a full disk is enough). Done outside the try, that killed the runner task: the job sat
    RUNNING forever, nothing behind it ever dispatched, and its queued spec stayed on disk for a
    restart to run again. The job must end FAILED with its spec gone, and the queue keep moving."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)

    async def _free_gate(job_log_file):
        return ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _free_gate)
    monkeypatch.setattr(srv, "privsep_refusal", lambda uid: "")

    disk_full = OSError(28, "No space left on device")

    def activation(workspace, *a, **k):
        if step == "activation" and workspace == "/fails":
            raise disk_full
        return "", {}  # no workspace python env to source here

    def prefix(uid, unit=None):
        if step == "privsep-prefix" and unit == srv.job_scope_unit("906"):
            raise disk_full
        return None  # no privsep: the job runs as a plain shell

    monkeypatch.setattr(srv, "get_activation_script", activation)
    monkeypatch.setattr(srv, "privsep_prefix_for", prefix)

    bad = srv.Job(id="906", owner="tenant", workspace="/fails", command="echo ran", queued_at="t")
    if step == "started-log":
        bad.log_file = str(tmp_path / "no-such-dir" / "906.log")  # every write to it raises
    good = srv.Job(id="907", owner="tenant", workspace="/tmp", command="true", queued_at="t")
    for job in (bad, good):
        srv.jobs[job.id] = job
        srv._persist_queued_job(job)
        await srv.get_job_queue().put(job.id)

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(250):
            if good.finished_at or runner.done():
                break
            await asyncio.sleep(0.02)
        assert not runner.done(), f"the runner died: {runner.exception() if runner.done() else ''}"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert bad.status is srv.JobStatus.FAILED, f"job 906 is {bad.status.value}, expected FAILED"
    assert bad.finished_at, "the failed job was never finished"
    assert "[EXCEPTION:" in bad.error, f"the failed job does not say why: {bad.error!r}"
    assert good.status is srv.JobStatus.COMPLETED, f"the job queued behind it is {good.status.value}"
    assert srv.current_job_id is None

    # the broker restarts: only what is on disk remains, and the failed job is not on it
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()
    await srv._restore_queued_jobs()
    assert srv.jobs == {}, "a job whose setup raised was re-queued by the restart"


@pytest.mark.asyncio
async def test_startup_records_what_came_back_after_a_reboot(monkeypatch, tmp_path):
    """A reboot row says the box went away and returned; it never said WHAT returned. A host
    back with a tray missing looked exactly like a clean one, which is how a 24/32 box ran for
    hours calling itself healthy. The start-time probe is sysfs-only, so it costs nothing and
    is safe beside a re-adopted job."""
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    events, rows = [], []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "write_action_log", lambda owner, cmd, rt, status, rc: rows.append((owner, cmd, status)))

    # came back short a tray
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(24)})
    monkeypatch.setattr(
        srv,
        "heartbeat_verdict",
        lambda expected, **k: (Verdict.UNHEALTHY, f"24 chip(s) in sysfs, expected {expected}", {}),
    )
    await srv._record_startup_health()
    startup = next((f for kind, f in events if kind == "startup_health"), None)
    assert startup is not None and startup["healthy"] is False
    assert startup["expected"] == 32 and startup["present"] == 24
    assert rows[0][2] == "failed", "a box back short a tray must not read as a clean start"
    assert "expected 32" in rows[0][1]

    # came back whole
    events.clear()
    rows.clear()
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32)})
    monkeypatch.setattr(srv, "heartbeat_verdict", lambda expected, **k: (Verdict.HEALTHY, "all 32 advancing", {}))
    await srv._record_startup_health()
    startup = next((f for kind, f in events if kind == "startup_health"), None)
    assert startup is not None and startup["healthy"] is True and rows[0][2] == "completed"


@pytest.mark.asyncio
async def test_a_failing_startup_probe_never_blocks_the_broker_coming_up(monkeypatch, tmp_path):
    """The probe is a report, not a gate: if it raises, the broker still serves the queue."""
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)

    def boom():
        raise OSError("sysfs gone")

    monkeypatch.setattr(srv, "read_heartbeats", boom)
    await srv._record_startup_health()  # must not raise
    assert srv.device_op_active == "", "a probe that raised must still release the device op"


@pytest.mark.asyncio
async def test_startup_probe_is_a_device_op_and_waits_out_a_cycling_reset(monkeypatch, tmp_path):
    """The observed race, pinned shut: a hold episode that outlives a restart can have the deadline
    watchdog force the recovery ladder within the first sample, and a startup probe reading sysfs
    while that reset cycles the bus sees live chips as fallen mid-probe — a false failed-startup
    row, and an off-bus count that can route a healthy boot toward the cold rung. The probe must
    (1) run under the device op, so the forced spawn declines while it reads and the probe waits
    while an escalation runs, and (2) wait out a reset scope the PREVIOUS broker process left
    cycling before it reads."""
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    order = []

    # The scope question itself is the pin: it must be asked (before the read) so a reset the
    # previous broker left cycling is waited out by await_foreign_scope's own loop.
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: order.append("scope-poll"))

    def read_beats():
        order.append(("read", srv.device_op_active))
        return {str(i): 100 for i in range(32)}

    monkeypatch.setattr(srv, "read_heartbeats", read_beats)
    monkeypatch.setattr(srv, "heartbeat_verdict", lambda expected, **k: (Verdict.HEALTHY, "all 32 advancing", {}))

    await srv._record_startup_health()
    read = next(o for o in order if isinstance(o, tuple) and o[0] == "read")
    assert read[1] == "startup-health", "the probe must hold the device op while it reads"
    assert order.index("scope-poll") < order.index(
        read
    ), "the probe must check for a still-cycling foreign reset before reading the bus"
    assert srv.device_op_active == "", "the op must be released when the probe finishes"


@pytest.mark.asyncio
async def test_an_unverified_fabric_is_never_recorded_as_verified_healthy(monkeypatch, tmp_path):
    """The gate clears the dirty flag when the fabric pass could not run, so the queue keeps
    moving — but it must not claim the fabric was VERIFIED. Only enum + ARC were proven. A mesh
    with a wedged eth core reading "verified healthy" on the timeline is how the next tenant
    finds out instead of the broker."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)

    async def healthy_but_no_fabric(expected, log, run_fabric=True, **_):
        return True, {"fabric": {"ok": None, "detail": "validator did not complete a measurement"}}

    patch_recovery(monkeypatch, "_verify_device", healthy_but_no_fabric)

    srv._mark_device_dirty("job ended killed")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert (
        srv.fsm.state is not ServerState.HEALTHY
    ), "the gate must still hold — the fabric was never proven, not merely left dirty"
    assert srv.fsm.record.why == "fabric_unverified", "recorded an unrun fabric pass as 'verified healthy'"
    assert "unverified" in srv.fsm.record.detail


@pytest.mark.asyncio
async def test_an_empty_dev_dir_on_a_host_that_expects_chips_holds_not_releases(monkeypatch, tmp_path):
    """Every chip off the bus is the worst state, not an absent device. The gate found an empty
    /dev/tenstorrent one second after the startup probe held the mesh, read it as "nothing to be
    dirty about", and released — so the escalation ladder never ran and the box sat dead while the
    broker called itself fine. A host whose baseline expects chips must stay dirty and HELD on an
    empty dir, and put the loss on the durable timeline loudly."""
    # An empty fixture dir: the /dev nodes are gone, exactly as when a Galaxy mesh drops off the bus.
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")  # this host is a 32-chip mesh
    _no_holders(monkeypatch)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    await srv._device_health_gate(None, phase="startup", run_fabric=True)

    assert srv._device_degraded_for_tenant(), "released the hold on a mesh with every chip gone"
    assert srv.fsm.state is not ServerState.HEALTHY, "an all-chips-missing mesh must stay dirty, not read as fit"
    missing = [f for kind, f in events if kind == "all_chips_missing"]
    assert missing, "the catastrophic loss must be on the durable timeline"
    assert missing[0]["present"] == 0 and missing[0]["expected"] == 32
    assert missing[0]["host_at_risk"] is True


def test_a_mesh_that_lost_chips_does_not_lower_its_own_bar(monkeypatch, tmp_path):
    """`expected` came from the /dev nodes present, so a box that lost a tray verified itself
    against the survivors: 24-of-24 read HEALTHY while a full-mesh job could not build one.
    Measured: a 24/32 box whose health gate called it fine. The bar must ratchet up, never down."""
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_EXPECTED_CHIPS", raising=False)

    assert srv.health_monitor.expected(32) == 32  # first sight of a full mesh sets the bar
    assert srv.health_monitor.expected(24) == 32, "a mesh short a tray must still expect 32"
    assert srv.health_monitor.expected(32) == 32  # back to full, bar unchanged

    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "8")
    assert srv.health_monitor.expected(24) == 8, "an explicit per-host override wins"


def test_expected_chip_count_survives_an_unreadable_baseline(monkeypatch, tmp_path):
    """A baseline we cannot read must not wedge the gate: fall back to what is present rather
    than raise inside the health check."""
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_EXPECTED_CHIPS", raising=False)
    (tmp_path / srv.health_monitor.CHIP_BASELINE_FILE).write_text("{not json")
    assert srv.health_monitor.expected(24) == 24


def test_an_unreadable_baseline_with_no_chips_present_is_not_read_as_device_less(monkeypatch, tmp_path):
    """The F1 inversion through the baseline: with every chip off the bus (present==0) and a
    corrupt baseline, `baseline or present` collapsed to 0, and the gate's empty-/dev branch
    reads expected==0 as a device-less box and RELEASES the hold on a dead mesh — the exact
    fail-open F1 closed, re-opened by an unreadable count. A baseline file that exists is proof
    the host has shown chips, so expected must stay nonzero and the hold must stand."""
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_EXPECTED_CHIPS", raising=False)
    (tmp_path / srv.health_monitor.CHIP_BASELINE_FILE).write_text("{not json")
    assert srv.health_monitor.expected(0) >= 1


def test_a_host_with_no_baseline_and_no_chips_stays_device_less(monkeypatch, tmp_path):
    """The other side of the split: a host that never wrote a baseline (the file is ABSENT, not
    corrupt) and shows no chips is genuinely device-less. It must still read expected==0 so the
    gate releases rather than holding a chip-less box's door shut forever."""
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_EXPECTED_CHIPS", raising=False)
    assert srv.health_monitor.expected(0) == 0


@pytest.mark.asyncio
async def test_a_reset_that_outlives_our_wait_still_shields_its_chips(monkeypatch):
    """reset_in_flight only spans the window we AWAIT the reset. Our wait times out while the
    reset keeps running — its scope is deliberately never killed mid-reset — so the flag drops
    while every chip is still legitimately reading all-ones. Isolating then amputates endpoints
    from a LIVE reset and they do not come back until the host reboots. The scope outlives the
    timer, so the scope is the authority."""
    srv.sampler.dead_chip_strikes = {}
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)  # our wait already timed out
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "ttdev-reset-123-1")  # but it runs on
    isolated, marks = [], []
    monkeypatch.setattr(srv.sampler, "isolate_dead_chips", lambda d: isolated.append(d))
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **kw: marks.append(reason))
    patch_health_event(monkeypatch, lambda kind, **f: None)

    half = {str(i): (heartbeat.ALL_ONES,) for i in range(8)}  # a tray, mid-reset
    half.update({str(i): (100,) for i in range(8, 32)})
    await srv.sampler.check_for_dead_chips(half)
    await srv.sampler.check_for_dead_chips(half)  # two samples: would be "confirmed" without the guard
    assert isolated == [], "amputated a chip out of a live reset scope"
    assert marks == [], "dirtied the device on a reset's own all-ones"

    blackout = {str(i): (heartbeat.ALL_ONES,) for i in range(32)}
    await srv.sampler.check_for_dead_chips(blackout)
    await srv.sampler.check_for_dead_chips(blackout)
    assert marks == [], "a live reset's all-ones is not a degraded device"


@pytest.mark.asyncio
async def test_an_all_chips_blip_that_clears_leaves_no_strike_behind(monkeypatch):
    """The blip must not accumulate across unrelated re-inits: a mesh that answers again resets
    the count, so two blips minutes apart never add up to a false 'confirmed' blackout."""
    srv.sampler.dead_chip_strikes = {}
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    marks = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **kw: marks.append(reason))
    patch_health_event(monkeypatch, lambda kind, **f: None)
    blackout = {str(i): (heartbeat.ALL_ONES,) for i in range(32)}
    alive = {str(i): (100 + i,) for i in range(32)}

    await srv.sampler.check_for_dead_chips(blackout)  # blip 1
    await srv.sampler.check_for_dead_chips(alive)  # mesh answers -> strikes cleared
    await srv.sampler.check_for_dead_chips(blackout)  # blip 2 is strike 1 again, not strike 2
    assert marks == [], "two separate blips must not add up to a confirmed blackout"


def test_idle_ledger_ignores_an_unconfirmed_single_sample_blip(monkeypatch):
    """The durable ledger must match the sampler's two-strike debounce, not the on-demand
    gate's single-sample liveness probe. A chip reading all-ones for ONE sample — the exact
    transient the strike counter is built to swallow — sets no dirty flag; a held/released
    pair recorded for it would pollute the very timeline this exists to keep honest."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_op_active", "")  # strike 1 only — not yet confirmed/isolated
    _chip(pci.SYSFS_CLASS_DIR, 0, 100)
    _chip(pci.SYSFS_CLASS_DIR, 1, heartbeat.ALL_ONES)  # one-sample blip, live in sysfs

    srv._refresh_idle_hold_ledger()
    assert events == []  # an unconfirmed transient leaves nothing on the durable timeline


def test_idle_hold_ledger_holds_the_latch_while_a_broker_op_recovers(monkeypatch):
    """Once a hold is open, a reset firing must not close and reopen it every sample. The op
    already owns the RUNNING row and IS the recovery; the ledger keeps the latch rather than
    writing a released it would immediately take back."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_logged", True)  # a held episode is already open
    monkeypatch.setattr(srv, "device_held_since", "2026-07-16T00:00:00")
    fsm_dirty(srv, "chip 26 fell off the bus")
    monkeypatch.setattr(srv, "device_op_active", "reset")  # the recovery is running
    monkeypatch.setattr(srv, "device_op_detail", "galaxy reset")

    srv._refresh_idle_hold_ledger()
    assert events == []  # neither a released nor a second held


def test_sampler_drives_the_idle_hold_ledger(monkeypatch):
    """The ledger only helps if the sampler — the one loop that runs with an empty queue —
    actually calls it. Prove the wiring, not just the helper in isolation."""
    calls = []
    monkeypatch.setattr(srv, "_refresh_idle_hold_ledger", lambda: calls.append(1))
    monkeypatch.setattr(srv, "chip_sample", lambda: {})
    monkeypatch.setattr(srv, "append_trace", lambda s: None)

    async def _noop_dead_check(sample):
        pass

    monkeypatch.setattr(srv.sampler, "check_for_dead_chips", _noop_dead_check)

    class _Stop(Exception):
        pass

    async def _sleep_once(_):
        raise _Stop()  # break the forever-loop after exactly one pass

    monkeypatch.setattr(srv.asyncio, "sleep", _sleep_once)

    with pytest.raises(_Stop):
        asyncio.run(srv.sampler.run())
    assert calls == [1]


# --- the durable journal -----------------------------------------------------


def test_chip_snapshot_captures_what_a_postmortem_needs(monkeypatch, tmp_path):
    """After a chip wedges, nobody can go back and ask whether it was hot, throttling,
    or taking bus errors. It has to have been written down before."""
    sysfs = pci.SYSFS_CLASS_DIR
    pci_dir = tmp_path / "pci"
    (pci_dir / "0000:01:00.0").mkdir(parents=True)
    dev = pci_dir / "0000:01:00.0"
    (dev / "current_link_width").write_text("1\n")
    (dev / "max_link_width").write_text("8\n")
    (dev / "current_link_speed").write_text("16.0 GT/s PCIe\n")
    (dev / "aer_dev_correctable").write_text("RxErr 3\nBadTLP 2\n")
    (dev / "aer_dev_fatal").write_text("Undefined 0\nDLP 1\n")

    c = sysfs / "tenstorrent!0"
    c.mkdir()
    (c / "tt_heartbeat").write_text("12345\n")
    (c / "tt_therm_trip_count").write_text("2\n")
    (c / "tt_arcclk").write_text("800\n")
    (c / "tt_fw_bundle_ver").write_text("19.8.1.0\n")
    (c / "device").symlink_to(dev)

    snap = pci.chip_snapshot()
    assert snap["0"]["tt_heartbeat"] == 12345  # the raw counter, so a stall is provable
    assert snap["0"]["tt_therm_trip_count"] == 2  # it ran hot enough to trip, twice
    assert snap["0"]["tt_fw_bundle_ver"] == "19.8.1.0"
    assert snap["0"]["aer_correctable"] == 5  # the escalation-to-fatal signal
    assert snap["0"]["aer_fatal"] == 1
    assert snap["0"]["link_width"] == "1" and snap["0"]["link_width_max"] == "8"

    assert pci.aer_totals(snap) == {"correctable": 5, "nonfatal": 0, "fatal": 1}


def test_chip_snapshot_is_written_to_its_own_durable_journal():
    c = pci.SYSFS_CLASS_DIR / "tenstorrent!0"
    c.mkdir()
    (c / "tt_heartbeat").write_text("7\n")

    pci.chip_snapshot_event("device_dirty", reason="job 063 ended timeout")
    rec = json.loads((health.HEALTH_DIR / pci.CHIPS_FILE).read_text().strip())
    assert rec["event"] == "device_dirty"
    assert rec["reason"] == "job 063 ended timeout"
    assert rec["chips"]["0"]["tt_heartbeat"] == 7


def test_chip_sample_carries_only_what_moves():
    """The trace runs every few seconds for a job's whole life. Carrying the static
    fields (asic id, firmware) on every sample would bloat the ring for no information —
    the static picture is already in chips.jsonl."""
    c = pci.SYSFS_CLASS_DIR / "tenstorrent!0"
    c.mkdir()
    (c / "tt_heartbeat").write_text("100\n")
    (c / "tt_aiclk").write_text("1350\n")
    (c / "tt_arcclk").write_text("800\n")
    (c / "tt_therm_trip_count").write_text("0\n")

    row = pci.chip_sample()["0"]
    assert row[:4] == [100, 1350, 800, 0]
    assert pci.SAMPLE_FIELDS[:4] == ["tt_heartbeat", "tt_aiclk", "tt_arcclk", "tt_therm_trip_count"]


def test_incident_bundle_freezes_the_kernel_log(monkeypatch, tmp_path):
    """The kernel's account of the bus is the evidence nobody has ever had: the documented
    path from a wedged chip to an ungraceful reboot runs through the root complex
    escalating PCIe errors to fatal, and the reboot then destroys the log that proves it.
    Copying it out at the moment of failure is the only way anyone reads it afterwards."""
    fake_dmesg = (
        "[Mon Jul 13 18:19:04 2026] pcieport 0000:c0:01.2: AER: Uncorrected (Fatal) error\n"
        "[Mon Jul 13 18:19:04 2026] tenstorrent 0000:c1:00.0: reset failed\n"
        "[Mon Jul 13 18:19:05 2026] this line is unrelated chatter\n"
        "[Mon Jul 13 18:19:06 2026] GHES: APEI firmware first mode is enabled\n"
    )

    class FakeRun:
        returncode = 0
        stdout = fake_dmesg
        stderr = ""

    monkeypatch.setattr(health.subprocess, "run", lambda *a, **k: FakeRun())

    joblog = tmp_path / "job.log"
    joblog.write_text("pytest output\nRuntimeError: waiting for active ethernet core\n")

    bundle = health.capture_incident(
        "dirty",
        job={"id": "093", "owner": "[agent]ksmith", "command": "pytest ...", "exit_code": 1},
        job_log=joblog,
        trace=[{"ts": 1.0, "chips": {"0": [100, 1350, 800, 0, 0, 0]}}],
        reset_output="full reset transcript",
        fabric_output="fabric validator said link 7 is down",
    )
    assert bundle is not None

    kern = (bundle / "kernel.log").read_text()
    assert "AER: Uncorrected (Fatal)" in kern
    assert "GHES: APEI firmware first mode" in kern
    assert "unrelated chatter" not in kern, "the filter let noise through"

    # The rest of the evidence that used to be truncated or thrown away entirely.
    assert "waiting for active ethernet core" in (bundle / "job.log").read_text()
    assert (bundle / "reset.log").read_text() == "full reset transcript"
    assert "link 7 is down" in (bundle / "fabric.log").read_text()

    trace = json.loads((bundle / "telemetry_trace.json").read_text())
    assert trace["samples"][0]["chips"]["0"][1] == 1350  # aiclk, in the run-up

    meta = json.loads((bundle / "incident.json").read_text())
    assert meta["job"]["owner"] == "[agent]ksmith"  # WHOSE job did this
    assert meta["job"]["exit_code"] == 1


def test_incidents_are_pruned_so_evidence_cannot_fill_the_disk(monkeypatch):
    monkeypatch.setattr(health, "MAX_INCIDENTS", 3)
    monkeypatch.setattr(
        health.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
    )

    for i in range(6):
        # Distinct names: the stamp has 1s resolution, so lean on the job id.
        health.capture_incident("dirty", job={"id": f"{i:03d}"})

    kept = sorted(d.name for d in (health.HEALTH_DIR / health.INCIDENTS_DIR).iterdir())
    assert len(kept) == 3, f"pruning failed: {kept}"
    assert kept[-1].endswith("_005")  # newest survive


# Verbatim from g03blx02's dmesg after the 2026-07-13 21:29 reboot. A parser that only
# works on invented input is worthless; this is the exact text the firmware emits.
_REAL_BERT = """\
[Mon Jul 13 21:29:29 2026] ERST: Error Record Serialization Table (ERST) support is initialized.
[Mon Jul 13 21:29:30 2026] BERT: Error records from previous boot:
[Mon Jul 13 21:29:30 2026] [Hardware Error]: event severity: fatal
[Mon Jul 13 21:29:30 2026] [Hardware Error]:  Error 0, type: fatal
[Mon Jul 13 21:29:30 2026] [Hardware Error]:  fru_text: ProcessorError
[Mon Jul 13 21:29:30 2026] [Hardware Error]:   section_type: IA32/X64 processor error
[Mon Jul 13 21:29:30 2026] [Hardware Error]:   Local APIC_ID: 0x34
[Mon Jul 13 21:29:30 2026] [Hardware Error]:   Error Information Structure 0:
[Mon Jul 13 21:29:30 2026] [Hardware Error]:    Error Structure Type: cache error
[Mon Jul 13 21:29:30 2026] [Hardware Error]:     Processor Context Corrupt: true
[Mon Jul 13 21:29:30 2026] [Hardware Error]:     Uncorrected: true
[Mon Jul 13 21:29:30 2026] [Hardware Error]:    Register Context Type: MSR Registers (Machine Check and other MSRs)
[Mon Jul 13 21:29:30 2026] [Hardware Error]:    MSR Address: 0xc0002051
[Mon Jul 13 21:29:30 2026] mce: [Hardware Error]: CPU 18: Machine Check: 0 Bank 5: aea0000000000108
[Mon Jul 13 21:29:31 2026] pci 0000:00:00.0: something else entirely
"""


def test_previous_boot_error_parses_the_real_firmware_record(monkeypatch):
    """BERT is the firmware's account of what killed the machine, and it is readable only
    from the current boot's dmesg — the next reboot overwrites it. The one thing it has
    to answer is whether the fatal was a PCIe fault (the wedged-chip theory) or a
    processor fault (something else), because those lead to opposite investigations."""
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": _REAL_BERT, "stderr": ""})(),
    )
    rec = health.previous_boot_error()

    assert rec["present"] is True
    assert rec["event_severity"] == "fatal"
    assert rec["error_structure_type"] == "cache error"
    assert rec["processor_context_corrupt"] == "true"
    assert rec["uncorrected"] == "true"

    # The verdict that decides which investigation to run.
    assert rec["is_processor"] is True
    assert rec["is_pcie"] is False

    # The RAW status is the only field that says which hardware block actually failed.
    # Firmware's "cache error" label is wrong — bank 5 on this silicon is the execution
    # unit, and 0xaea0000000000108 decodes to a watchdog timeout. A record that keeps the
    # label and drops the raw word preserves the lie and discards the truth.
    assert rec["mce_cpu"] == 18
    assert rec["mce_bank"] == 5
    assert rec["mce_status"] == "aea0000000000108"

    # The block must end at the first non-[Hardware Error] line, not run into the rest.
    assert not any("something else entirely" in ln for ln in rec["raw"])


def test_previous_boot_error_absent_on_a_clean_boot(monkeypatch):
    """No BERT record means the firmware logged no fatal error. That is a fact worth
    keeping — a clean reboot and an unexplained one must not look the same."""
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "[ 0.0] Linux version 6.8\n", "stderr": ""})(),
    )
    assert health.previous_boot_error() == {"present": False}


# --- a reboot is a device event the jobs list has to show -----------------------
#
# Every reset the broker runs leaves a visible row; a reboot took the whole box off the bus
# and back and left nothing but a health_event. That makes a box that reboots most days a
# blank in the one record an operator reads — the jobs list. Startup back-fills the row.


def _boot_recording(monkeypatch, tmp_path, rec):
    """Wire _record_previous_boot_error to write into tmp_path and nowhere real, with the
    firmware's account of the last boot forced to `rec`."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path / "health")
    # RecoveryMechanism.boot_from_broker_escalation (called by _record_previous_boot_error) reads
    # the auto-recovery ledger through health.recovery.base's OWN import of health_dir, a separate
    # binding from server.py's — both must be redirected or the moved ledger read still hits the
    # real state dir.
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path / "health")
    (tmp_path / "health").mkdir()
    monkeypatch.setattr(srv, "previous_boot_error", lambda: rec)
    monkeypatch.setattr(srv, "previous_boot_bus_locks", lambda: 0)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "mark_boot", lambda *a, **k: None)


def _reboot_rows(tmp_path):
    return [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]reboot"]


def test_a_crash_reboot_back_fills_a_visible_jobs_row(monkeypatch, tmp_path):
    """A reboot that ended in a firmware crash record must surface in recent history with
    its class — a processor watchdog and a wedged-chip PCIe fault lead opposite ways, and a
    reboot nobody can see after the fact is how these boxes stayed unexplained."""
    crash = {
        "present": True,
        "is_processor": True,
        "is_pcie": False,
        "event_severity": "fatal",
        "raw": ["Hardware Error"],
    }
    _boot_recording(monkeypatch, tmp_path, crash)

    srv._record_previous_boot_error()

    rows = _reboot_rows(tmp_path)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "crash"
    assert "processor" in rows[0]["command"]


def test_a_clean_reboot_and_a_crash_do_not_read_the_same(monkeypatch, tmp_path):
    """No BERT record means a clean shutdown or an intentional reboot. It still gets a row —
    an intentional reboot is device history too — but it must not wear the "crash" status a
    real fault does, or the audit trail turns every reboot into an incident."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})

    srv._record_previous_boot_error()

    rows = _reboot_rows(tmp_path)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "reboot"
    assert "clean shutdown or intentional" in rows[0]["command"]


def test_reboot_row_is_written_once_per_boot(monkeypatch, tmp_path):
    """The broker restarts many times within one boot; the same reboot re-logged on each
    restart would make one reboot look like a storm of them — precisely the confusion this
    record exists to remove. The boot-id marker gates it to one row per boot."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})

    srv._record_previous_boot_error()
    srv._record_previous_boot_error()

    assert len(_reboot_rows(tmp_path)) == 1


def test_reboot_row_dedups_even_when_the_kernel_boot_id_is_unreadable(monkeypatch, tmp_path):
    """The once-per-boot guard keyed on the kernel random boot_id alone, so a host that could
    not serve one back-filled a fresh reboot row on every broker restart within a single boot —
    the same one-reboot-looks-like-many confusion the guard exists to remove. /proc/stat btime
    changes only on a real reboot, so it dedups the boot when boot_id is unreadable."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})
    real_read_text = srv.Path.read_text

    def read_text_no_boot_id(self, *a, **k):
        if str(self) == "/proc/sys/kernel/random/boot_id":
            raise OSError("boot_id unreadable")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(srv.Path, "read_text", read_text_no_boot_id)

    srv._record_previous_boot_error()
    srv._record_previous_boot_error()

    assert len(_reboot_rows(tmp_path)) == 1


def test_reboot_row_maps_fault_class_and_status():
    """The why + status the row carries, isolated from I/O: PCIe vs processor vs clean."""
    assert srv._reboot_why_status({"present": False}) == (
        "reboot — no firmware crash record (clean shutdown or intentional)",
        "reboot",
    )
    pcie_why, pcie_status = srv._reboot_why_status({"present": True, "is_pcie": True, "event_severity": "fatal"})
    assert "PCIe" in pcie_why and pcie_status == "crash"
    proc_why, _ = srv._reboot_why_status({"present": True, "is_processor": True, "event_severity": "fatal"})
    assert "processor" in proc_why


def _write_recovery_ledger(tmp_path, *records):
    """Seed the durable auto-recovery ledger _boot_recording's health_dir reads. Any record without an
    at_epoch is anchored just before THIS boot's start so the boot-attribution window in
    _boot_from_broker_escalation accepts it as this boot's cause (these tests exercise the back-fill;
    the staleness guard itself is covered by test_boot_attribution_*)."""
    try:
        btime = float(srv._boot_btime_id())
    except (ValueError, TypeError):
        btime = srv.time.time()
    rows = []
    for r in records:
        r = dict(r)
        r.setdefault("at_epoch", btime - 60.0)
        rows.append(r)
    path = tmp_path / "health" / recovery_base.AUTO_RECOVERY_LEDGER
    path.write_text("".join(json.dumps(x) + "\n" for x in rows))


def _boot_rows(tmp_path, owner):
    return [r for r in srv._recent_jobs(20) if r["owner"] == owner]


def test_a_broker_power_cycle_boot_reads_as_a_power_cycle_not_a_reboot(monkeypatch, tmp_path):
    """When the broker itself BMC-power-cycled the host as its last recovery rung, the back-filled
    row must name that action. A power cycle read back as a plain "reboot" is the audit trail
    lying about what took the box down — and power cycle vs reboot is exactly the distinction an
    operator chasing a whole-bus wedge needs."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})
    _write_recovery_ledger(
        tmp_path, {"action": "power-cycle", "boot_id": "prev-boot-0000", "reason": "reset could not recover the device"}
    )

    srv._record_previous_boot_error()

    rows = _boot_rows(tmp_path, "[broker]power-cycle")
    assert len(rows) == 1, srv._recent_jobs(20)
    assert rows[0]["status"] == "power-cycle"
    assert "power cycle" in rows[0]["command"]
    assert "broker auto-recovery" in rows[0]["command"]
    assert not _boot_rows(tmp_path, "[broker]reboot")


def test_a_broker_reboot_boot_reads_as_broker_auto_recovery(monkeypatch, tmp_path):
    """A broker-fired warm reboot keeps the reboot owner, but its why must say the broker did it —
    not read the same as an operator's intentional reboot, which is a different event to chase."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})
    _write_recovery_ledger(
        tmp_path, {"action": "reboot", "boot_id": "prev-boot-0000", "reason": "reset could not recover the device"}
    )

    srv._record_previous_boot_error()

    rows = _boot_rows(tmp_path, "[broker]reboot")
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "reboot"
    assert "broker auto-recovery" in rows[0]["command"]
    assert "clean shutdown or intentional" not in rows[0]["command"]


def test_a_broker_reboot_boot_consumes_its_pre_down_request_row(monkeypatch, tmp_path):
    """_fire_recovery_escalation leaves a '[broker]reboot-request' row before it takes the box down
    so the escalation is visible even if the box never returns. When it DOES return, the post-boot
    back-fill writes the authoritative '[broker]reboot' row for the same event — keeping both shows
    one reboot as two. The back-fill must consume the request row so the escalation is a single row."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})
    # The pre-down request row that survived the warm reboot's clean shutdown.
    srv.write_action_log(
        "[broker]reboot-request", "auto host reboot — reset could not recover the device", 0.0, "reboot", None
    )
    assert _boot_rows(tmp_path, "[broker]reboot-request"), "request row should exist before back-fill"
    _write_recovery_ledger(
        tmp_path, {"action": "reboot", "boot_id": "prev-boot-0000", "reason": "reset could not recover the device"}
    )

    srv._record_previous_boot_error()

    assert len(_boot_rows(tmp_path, "[broker]reboot")) == 1, srv._recent_jobs(20)
    assert _boot_rows(tmp_path, "[broker]reboot-request") == [], "duplicate request row not consumed"


def test_a_power_cycle_request_row_that_never_synced_leaves_one_row(monkeypatch, tmp_path):
    """An abrupt BMC power cycle may never sync its '[broker]power-cycle-request' row. Consuming a
    row that is not there must be a no-op, not an error, and the back-fill still stands as the one
    row for the escalation — one reboot, one row, whether or not the request row survived."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})
    _write_recovery_ledger(
        tmp_path, {"action": "power-cycle", "boot_id": "prev-boot-0000", "reason": "reset could not recover the device"}
    )

    srv._record_previous_boot_error()

    assert len(_boot_rows(tmp_path, "[broker]power-cycle")) == 1, srv._recent_jobs(20)


def test_an_escalation_fired_on_this_same_boot_is_not_attributed_to_it(monkeypatch, tmp_path):
    """A ledger record stamped with the boot now running means the escalation fired but the box
    has NOT rebooted since — so this boot is not its result. Attributing it would invent a reboot
    that never happened; the row must fall back to the ordinary clean-reboot labelling."""
    _boot_recording(monkeypatch, tmp_path, {"present": False})
    _write_recovery_ledger(
        tmp_path,
        {"action": "power-cycle", "boot_id": srv._current_boot_id(), "reason": "reset could not recover the device"},
    )

    srv._record_previous_boot_error()

    assert not _boot_rows(tmp_path, "[broker]power-cycle")
    rows = _boot_rows(tmp_path, "[broker]reboot")
    assert len(rows) == 1, rows
    assert "clean shutdown or intentional" in rows[0]["command"]


# --- a chip that leaves the PCIe bus ------------------------------------------
#
# A host was lost this way: a chip started returning all-ones, and for two minutes a hung
# fabric check, tt-telemetry (segfaulting and being restarted straight back into it) and our
# own sampler all kept issuing MMIO at it. A core stalled on a read that never completed and
# the watchdog took the machine.


def test_all_ones_is_recognised_as_a_chip_off_the_bus_not_a_stall(monkeypatch):
    """0xFFFFFFFF is not a value — no counter, clock or trip count is ever all-ones. It is
    the bus saying the chip is gone, and it demands a different response from a stall:
    cut it out of the kernel, do not reset the mesh around it."""
    sysfs = pci.SYSFS_CLASS_DIR
    for i in range(4):
        _chip(sysfs, i, 100)
    _chip(sysfs, 2, heartbeat.ALL_ONES)  # chip 2 fell off the bus

    monkeypatch.setattr(health.time, "sleep", lambda _: None)
    verdict, detail, evidence = heartbeat.heartbeat_verdict(expected_count=4)

    assert verdict is Verdict.UNHEALTHY
    assert evidence["dead"] == ["2"]
    assert "FELL OFF THE PCIe BUS" in detail
    assert "isolate" in detail


def test_a_chip_dropping_off_the_bus_mid_probe_is_not_read_as_healthy(monkeypatch):
    """A chip fine in the first sample can fall off the bus during the settle window: it is still
    a present key reading 0xFFFFFFFF, so it is neither "missing" (still in sysfs) nor "stalled"
    (all-ones != its prior counter) and would slip through to HEALTHY. It must be caught as
    off-the-bus, exactly as a first-sample drop is."""
    sysfs = pci.SYSFS_CLASS_DIR
    for i in range(4):
        _chip(sysfs, i, 100)

    def advance(_):
        for i in range(4):
            _chip(sysfs, i, 200)
        _chip(sysfs, 2, heartbeat.ALL_ONES)  # chip 2 leaves the bus during the settle window

    monkeypatch.setattr(health.time, "sleep", advance)
    verdict, detail, evidence = heartbeat.heartbeat_verdict(expected_count=4)

    assert verdict is Verdict.UNHEALTHY, detail
    assert evidence["dead"] == ["2"]
    assert "FELL OFF THE PCIe BUS" in detail


def test_dead_chips_are_picked_out_of_a_live_mesh():
    beats = {"0": 500, "13": heartbeat.ALL_ONES, "31": 502}
    assert heartbeat.dead_chips(beats) == ["13"]
    assert heartbeat.dead_chips({"0": 500}) == []


@pytest.mark.asyncio
async def test_a_dead_chip_is_isolated_without_waiting_for_the_device_lock(monkeypatch):
    """This must NOT take the device lock. Every other device op waits its turn because two
    at once is dangerous; this one cannot wait, because the waiting IS the danger — and the
    thing holding the lock may be the very fabric check that is hung on the dead chip."""
    srv.device_op_lock = None
    srv.isolated_chips = set()
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")

    removed = []
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: removed.append(idx) or True)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: f"0000:46:00.{idx}")

    quiesced = []

    async def pollers(active, log):
        quiesced.append(active)
        return ["tt-telemetry.service"]

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)

    # Hold the device lock, as a hung fabric check would. Isolation must proceed anyway.
    async with srv._device_op("health-gate/post-job"):
        await asyncio.wait_for(srv.sampler.isolate_dead_chips(["13"]), timeout=2.0)

    assert removed == ["13"], "the dead chip was not removed from the kernel"
    assert quiesced == [False], "tt-telemetry was not stopped before/while isolating"
    assert srv.isolated_chips == {"13"}
    assert srv.fsm.state is not ServerState.HEALTHY


def _no_isolation(monkeypatch):
    isolated = []

    async def fake(dead):
        isolated.extend(dead)

    monkeypatch.setattr(srv.sampler, "isolate_dead_chips", fake)
    srv.sampler.dead_chip_strikes = {}
    srv.recovery_mechanism.reset_in_flight = False
    return isolated


@pytest.mark.asyncio
async def test_a_reset_in_flight_must_not_look_like_dead_chips(monkeypatch):
    """Taking the chips off the bus is what a reset IS. During one they read all-ones exactly
    like a dead chip — and isolating them then rips the endpoints out of the kernel halfway
    through and destroys the reset. This removed all 32 chips from a healthy host."""
    isolated = _no_isolation(monkeypatch)
    srv.recovery_mechanism.reset_in_flight = True

    sample = {str(i): [heartbeat.ALL_ONES, 0, 0, 0, 0, 0] for i in range(32)}
    await srv.sampler.check_for_dead_chips(sample)
    await srv.sampler.check_for_dead_chips(sample)

    assert isolated == [], "isolated chips during a reset — that destroys the reset"


@pytest.mark.asyncio
async def test_every_chip_at_once_is_a_global_event_not_32_dead_chips(monkeypatch):
    """32 chips do not fail independently in the same 10ms. That is the driver, the bus, or a
    reset — and amputating every device leaves nothing to run on and no way back."""
    isolated = _no_isolation(monkeypatch)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)

    sample = {str(i): [heartbeat.ALL_ONES, 0, 0, 0, 0, 0] for i in range(32)}
    await srv.sampler.check_for_dead_chips(sample)
    await srv.sampler.check_for_dead_chips(sample)

    assert isolated == [], "removed every device from the box"
    assert srv.fsm.state is not ServerState.HEALTHY, "a total blackout must still flag the device"


@pytest.mark.asyncio
async def test_a_transient_all_ones_does_not_amputate_a_chip(monkeypatch):
    """One sample is not enough. A dead chip stays dead; a blip does not. 20s of patience is a
    rounding error against the ~130s a host survives after a chip drops."""
    isolated = _no_isolation(monkeypatch)

    live = [100, 800, 800, 0, 0, 0]
    blip = {"0": live, "13": [heartbeat.ALL_ONES, 0, 0, 0, 0, 0], "31": live}
    await srv.sampler.check_for_dead_chips(blip)
    assert isolated == [], "isolated on a single sample"

    healthy = {"0": live, "13": live, "31": live}
    await srv.sampler.check_for_dead_chips(healthy)  # it recovered — strike cleared
    await srv.sampler.check_for_dead_chips(blip)  # one strike again
    assert isolated == [], "a cleared strike must not carry over"


@pytest.mark.asyncio
async def test_a_chip_dead_on_two_consecutive_samples_is_isolated(monkeypatch):
    """The case that matters: a real chip drop still gets cut out, fast."""
    isolated = _no_isolation(monkeypatch)

    live = [100, 800, 800, 0, 0, 0]
    dead = {"0": live, "13": [heartbeat.ALL_ONES, 0, 0, 0, 0, 0], "31": live}
    await srv.sampler.check_for_dead_chips(dead)
    await srv.sampler.check_for_dead_chips(dead)

    assert isolated == ["13"], "a genuinely dead chip was not isolated"


@pytest.mark.asyncio
async def test_a_chip_is_only_isolated_once(monkeypatch):
    """The sampler runs every few seconds. Removing an already-removed chip on every pass
    would be pointless churn — and the second remove would fail anyway."""
    srv.device_op_lock = None
    srv.isolated_chips = set()
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")

    calls = []
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: calls.append(idx) or True)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: "0000:46:00.0")

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)

    await srv.sampler.isolate_dead_chips(["13"])
    await srv.sampler.isolate_dead_chips(["13"])
    await srv.sampler.isolate_dead_chips(["13"])

    assert calls == ["13"], f"isolated repeatedly: {calls}"


def test_isolate_chip_journals_a_failure_to_resolve_the_bdf(monkeypatch):
    """The safety valve failing is the hazard, not a routine no-op. When the BDF cannot be
    resolved the endpoint is never unbound, so the dead chip stays on the bus reachable by
    MMIO — the exact condition that takes the host down. A silent False reads as "isolated"
    to the caller; the failure must land in the durable journal, tagged host-at-risk."""
    monkeypatch.setattr(pci, "chip_pci_bdf", lambda idx: None)

    assert pci.isolate_chip("13") is False
    events = health.read_health_events(kinds={"chip_isolation_failed"})
    assert [(e["chip"], e["reason"], e["host_at_risk"]) for e in events] == [("13", "no_pci_bdf", True)], events


def test_isolate_chip_journals_a_failed_remove_write(monkeypatch):
    """The other valve-failure path: the BDF resolves but writing the kernel remove node
    raises. The chip is still on the bus, so this is host-at-risk too — journaled, with the
    bdf and the errno text, not swallowed."""
    monkeypatch.setattr(pci, "chip_pci_bdf", lambda idx: "0000:ff:00.0")  # never a real device

    assert pci.isolate_chip("13") is False
    events = health.read_health_events(kinds={"chip_isolation_failed"})
    assert len(events) == 1, events
    e = events[0]
    assert (e["chip"], e["bdf"], e["reason"], e["host_at_risk"]) == (
        "13",
        "0000:ff:00.0",
        "remove_write_failed",
        True,
    ), e
    assert e["error"], "the OSError text must be recorded"


def test_isolate_chip_writes_under_the_configured_pci_dir(monkeypatch, tmp_path):
    """The valve must fire inside TT_DEVICE_MCP_PCI_DIR, never at an absolute /sys/bus/pci/devices.

    Its sibling chip_node_present() already honours the override, so a hardcoded path makes the two
    disagree about which topology they mean. It is also the one write in this module that unbinds a
    live endpoint: unsealed, a suite run on a box whose PCI addresses happen to match takes a real
    device off the bus. The neighbouring failure test is safe only because 0000:ff:00.0 is absent
    on the hosts seen so far — that is luck, not sandboxing."""
    bdf = "0000:31:00.0"  # a REAL address on a 4x n300 box
    node = tmp_path / bdf
    node.mkdir()
    (node / "remove").write_text("")
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(pci, "chip_pci_bdf", lambda idx: bdf)

    assert pci.isolate_chip("13") is True
    assert (node / "remove").read_text() == "1", "the remove write did not land in the sealed dir"
    assert health.read_health_events(kinds={"chip_isolation_failed"}) == []


@pytest.mark.asyncio
async def test_recovery_resets_through_the_bridge_and_clears_the_chip(monkeypatch):
    """A dead endpoint cannot be reset through its own config space. The bridge above it can,
    and always answers. The PCI address must come from the map cached while the chip was
    still alive — removing it from the kernel takes its sysfs link, and with it the address."""
    srv.isolated_chips = {"13"}
    srv.device_pci_map = {"13": "0000:46:00.0"}

    seen = {}

    def bridge_reset(idx, bdf):
        seen["idx"], seen["bdf"] = idx, bdf
        return True

    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", bridge_reset)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)

    assert ok is True
    assert seen == {"idx": "13", "bdf": "0000:46:00.0"}
    assert srv.isolated_chips == set(), "recovered chip should no longer be isolated"


@pytest.mark.asyncio
async def test_bridge_reset_retries_before_escalating(monkeypatch):
    """The first bridge reset often loses the race with the endpoint's link retrain and
    reports failure, yet the same chip re-binds once the link settles. Retry before
    declaring the chip lost — otherwise a self-healing drop escalates to a galaxy reset and
    a ~13 min recovery instead of a few seconds."""
    srv.isolated_chips = {"13"}
    srv.device_pci_map = {"13": "0000:46:00.0"}
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)  # do not really sleep in a unit test
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)

    calls = {"n": 0}

    def bridge_reset(idx, bdf):
        calls["n"] += 1
        return calls["n"] >= 2  # fails the first shot, succeeds once the link settles

    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", bridge_reset)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)

    assert ok is True, "a chip that comes back on a retry must be recovered, not lost"
    assert calls["n"] == 2, "must retry after the first failed shot"
    assert srv.isolated_chips == set(), "recovered chip should no longer be isolated"


@pytest.mark.asyncio
async def test_a_chip_that_will_not_come_back_stays_out_of_the_kernel(monkeypatch):
    """If the bridge reset fails EVERY retry, the chip STAYS removed. Putting an unrecoverable
    endpoint back on the bus just re-arms the thing that reboots the host."""
    srv.isolated_chips = {"13"}
    srv.device_pci_map = {"13": "0000:46:00.0"}
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)
    calls = {"n": 0}

    def never(idx, bdf):
        calls["n"] += 1
        return False

    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", never)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)

    assert ok is False
    assert srv.isolated_chips == {"13"}, "an unrecoverable chip must stay isolated"
    assert calls["n"] == BRIDGE_RESET_MAX_TRIES, "must exhaust retries before giving up"


@pytest.mark.asyncio
async def test_an_inapplicable_bridge_reset_is_not_retried(monkeypatch):
    """A None from reset_chip_via_bridge means the rung could not fire at all — the endpoint
    left the bus so there is no bridge to write to (or setpci is gone). A settle-and-retry
    re-resolves the same absent bridge and no-ops identically, so the chip must fall to the next
    rung after ONE attempt, not spin the full retry budget. The incident burnt ~64s repeating a
    no_bridge reset 3x per gone chip, then again on the stuck-hold escalation, on a rung that
    structurally cannot work for an off-bus chip."""
    srv.isolated_chips = {"13"}
    srv.device_pci_map = {"13": "0000:46:00.0"}
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)

    calls = {"n": 0}

    def no_bridge(idx, bdf):
        calls["n"] += 1
        return None  # the rung cannot be issued for this chip

    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", no_bridge)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)

    assert ok is False, "an unreachable chip must escalate, not read as recovered"
    assert calls["n"] == 1, "a rung that cannot fire must not be retried"
    assert srv.isolated_chips == {"13"}, "the chip stays isolated so it cannot take the host down"


@pytest.mark.asyncio
async def test_an_all_inapplicable_bridge_reset_never_reads_as_recovered(monkeypatch):
    """Every chip returning None (the whole set left the bus, no bridges to reset) must count as
    still dead, never as a healed mesh. An all-inapplicable recovery reporting True would clear
    the hold with zero chips back — absence read as health, the fail-open this loop exists to kill."""
    srv.isolated_chips = {"13", "14"}
    srv.device_pci_map = {"13": "0000:46:00.0", "14": "0000:47:00.0"}
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: None)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)

    assert ok is False, "an all-gone mesh must not read as recovered"
    assert srv.isolated_chips == {"13", "14"}, "no chip came back, so all stay isolated"


def test_find_bridge_by_secondary_bus_reaches_a_vanished_chips_parent(monkeypatch, tmp_path):
    """A chip that has fully left the bus takes its own sysfs node with it, so the parent
    link the bridge reset walks is gone. Its bridge stays enumerated with secondary_bus_number
    still pointing at the empty bus — reading that finds the bridge without touching the
    vanished endpoint. secondary_bus_number is decimal; a BDF bus field is hex."""
    pci_dir = tmp_path / "devices"
    pci_dir.mkdir()

    def add_bridge(bdf, secondary_decimal):
        d = pci_dir / bdf
        d.mkdir()
        (d / "secondary_bus_number").write_text(f"{secondary_decimal}\n")

    # A bridge above bus 0x84 (132 decimal) — the blx04 drop — plus a decoy on another bus,
    # a same-secondary-bus bridge in a DIFFERENT domain, and an endpoint with no such attr.
    add_bridge("0000:80:03.1", 132)
    add_bridge("0000:80:01.1", 64)
    add_bridge("0001:10:00.0", 132)
    (pci_dir / "0000:00:00.0").mkdir()  # an endpoint: no secondary_bus_number, must be skipped
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", pci_dir)

    # The endpoint 0000:84:00.0 is GONE — no dir for it — yet its bridge is still found.
    assert bridge.find_bridge_by_secondary_bus("0000:84:00.0") == "0000:80:03.1"
    # Domain scoping: the domain-0001 chip resolves to ITS bridge, not the domain-0000 one
    # that sorts first and shares the secondary bus. Bus numbers repeat across domains.
    assert bridge.find_bridge_by_secondary_bus("0001:84:00.0") == "0001:10:00.0"
    # No bridge for a bus nothing bridges → None, never a wrong guess to reset.
    assert bridge.find_bridge_by_secondary_bus("0000:99:00.0") is None
    # A malformed address is rejected, not matched against.
    assert bridge.find_bridge_by_secondary_bus("garbage") is None


def test_a_reused_bus_refuses_the_bridge_so_a_stale_address_never_sbrs_live_silicon(monkeypatch, tmp_path):
    """The address is cached at broker start and never refreshed, so a galaxy reset or rescan that
    renumbered buses can leave a chip's cached address pointing at a bus a DIFFERENT, live chip now
    holds. Resolving the bridge by that stale secondary bus would name a bridge whose downstream is
    that live chip — a Secondary Bus Reset there knocks out silicon that never dropped. A chip that
    truly left the bus leaves it EMPTY, so anything enumerated on the target bus proves the address
    is stale: refuse the bridge and let the chip escalate to a heavier reset instead."""
    pci_dir = tmp_path / "devices"
    pci_dir.mkdir()

    def add_bridge(bdf, secondary_decimal):
        d = pci_dir / bdf
        d.mkdir()
        (d / "secondary_bus_number").write_text(f"{secondary_decimal}\n")

    # Two bridges still enumerated with their secondary buses intact: above bus 0x84 (132) and
    # above bus 0x40 (64). Bus 0x40 is empty; bus 0x84 has since been reused by a live endpoint.
    add_bridge("0000:80:03.1", 132)
    add_bridge("0000:80:01.1", 64)
    (pci_dir / "0000:84:00.0").mkdir()  # a live endpoint now occupies bus 0x84
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", pci_dir)

    # Bus 0x40 is empty — a chip that truly left it: its bridge resolves as before.
    assert bridge.find_bridge_by_secondary_bus("0000:40:00.0") == "0000:80:01.1"
    # Bus 0x84 is occupied by live silicon — the cached address is stale, so the bridge is refused
    # rather than resolved to 0000:80:03.1, whose SBR would reset the live chip now on that bus.
    assert bridge.find_bridge_by_secondary_bus("0000:84:00.0") is None


def test_bridge_reset_falls_back_to_secondary_bus_for_a_gone_endpoint(monkeypatch, tmp_path):
    """A chip that has fully left the bus takes its own sysfs node — and the parent link the
    bridge reset walks — with it. Opted in, the reset must then find the bridge by the secondary
    bus it still points at, not give up, or a link-dropped chip strands where an all-ones one
    recovers. No real setpci is ever issued here: the fallback resolves to no bridge and the reset
    gives up before it, and setpci is trapped so a stray real call fails loud, not touches a box."""
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")  # opt in to the fallback
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)  # empty: the endpoint's node is absent

    def _no_setpci(*a, **k):
        raise AssertionError("bridge reset issued a real setpci in a unit test")

    monkeypatch.setattr(health.subprocess, "run", _no_setpci)

    consulted = {"bdf": None}

    def fake_find(bdf):
        consulted["bdf"] = bdf
        return None  # no bridge -> the reset gives up cleanly, before any setpci

    monkeypatch.setattr(bridge, "find_bridge_by_secondary_bus", fake_find)

    # 0000:fe:00.0 has no /sys node, so the parent walk yields no bridge and the fallback runs.
    # None (not False): no reset fired, so the caller must fall through, not retry the no-op.
    assert bridge.reset_chip_via_bridge("13", "0000:fe:00.0") is None
    assert consulted["bdf"] == "0000:fe:00.0", "a gone endpoint must resolve its bridge by secondary bus"


def test_bridge_reset_does_not_reach_a_gone_endpoint_by_default(monkeypatch, tmp_path):
    """Default-safe: with the opt-in off, a node-less endpoint yields no bridge and the reset
    returns without ever consulting the secondary-bus fallback — byte-for-byte the prior behavior,
    so the existing all-ones recovery path fires no Secondary Bus Reset it did not fire before."""
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)  # default off
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)

    def _no_setpci(*a, **k):
        raise AssertionError("bridge reset issued a real setpci in a unit test")

    monkeypatch.setattr(health.subprocess, "run", _no_setpci)

    consulted = {"n": 0}
    monkeypatch.setattr(
        bridge, "find_bridge_by_secondary_bus", lambda bdf: consulted.__setitem__("n", consulted["n"] + 1)
    )

    assert bridge.reset_chip_via_bridge("13", "0000:fe:00.0") is None
    assert consulted["n"] == 0, "the fallback must not run with the opt-in off"


def test_bridge_reset_journals_no_bridge_distinctly(monkeypatch, tmp_path):
    """No bridge to write to is a topology fact, not a broken setpci — the reset never shells
    out. It still owes the journal a line so the gentlest rung's no-op is never silent, tagged
    no_bridge so it reads apart from a setpci that was present and failed."""
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)  # opt-in off: no fallback
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)  # node absent -> no parent bridge

    def _no_setpci(*a, **k):
        raise AssertionError("no bridge resolved, yet setpci was shelled out")

    monkeypatch.setattr(health.subprocess, "run", _no_setpci)

    assert bridge.reset_chip_via_bridge("13", "0000:fe:00.0") is None
    events = health.read_health_events(kinds={"bridge_reset_failed"})
    assert [e["reason"] for e in events] == ["no_bridge"], events


def test_bridge_reset_journals_a_missing_setpci_as_host_at_risk(monkeypatch, tmp_path):
    """setpci gone means the gentlest recovery rung is dead for EVERY chip — every bridge reset
    then falls through to a heavier reset. Preflight refuses to serve without it, so a missing
    setpci at runtime is a real post-start regression: it must land in the journal loud and
    host_at_risk, distinct from a chip that simply had no bridge to reset."""
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)  # node absent -> fallback resolves the bridge
    monkeypatch.setattr(bridge, "find_bridge_by_secondary_bus", lambda bdf: "0000:80:01.1")

    def _enoent(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "setpci")

    monkeypatch.setattr(health.subprocess, "run", _enoent)

    assert bridge.reset_chip_via_bridge("13", "0000:fe:00.0") is None
    events = health.read_health_events(kinds={"bridge_reset_failed"})
    assert [e["reason"] for e in events] == ["setpci_missing"], events
    assert events[0].get("host_at_risk") is True, events


def test_bridge_reset_journals_a_setpci_read_failure_distinctly(monkeypatch, tmp_path):
    """setpci present but the control-word read returns non-zero is a per-attempt failure — a
    permission or bad-bridge error — not the binary being absent. It must journal with the rc and
    bridge so an operator sees a real setpci failure, not the same silent give-up a missing binary
    or an absent bridge leaves."""
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(bridge, "find_bridge_by_secondary_bus", lambda bdf: "0000:80:01.1")

    def _rc1(*a, **k):
        return health.subprocess.CompletedProcess(a[0], 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(health.subprocess, "run", _rc1)

    assert bridge.reset_chip_via_bridge("13", "0000:fe:00.0") is False
    events = health.read_health_events(kinds={"bridge_reset_failed"})
    assert [e["reason"] for e in events] == ["setpci_read_rc"], events
    assert events[0]["rc"] == 1 and events[0]["bridge"] == "0000:80:01.1", events


def _setpci_fake(assert_rc=0, release_rc=0):
    """A setpci whose control-word read succeeds and whose two writes carry chosen rcs.

    With val=0x0000 the assert write is BRIDGE_CONTROL=0040 and the release BRIDGE_CONTROL=0000,
    so the two halves of the reset are distinguishable by argv alone."""

    def run(argv, *a, **k):
        arg = argv[-1]
        if arg == "BRIDGE_CONTROL":
            return health.subprocess.CompletedProcess(argv, 0, stdout="0000", stderr="")
        rc = assert_rc if arg.endswith("=0040") else release_rc
        return health.subprocess.CompletedProcess(argv, rc, stdout="", stderr="denied")

    return run


def test_bridge_reset_journals_a_failed_sbr_assert(monkeypatch, tmp_path):
    """A write that never landed means nothing was reset. Unchecked, the rung's failure surfaces
    only as endpoint_not_reenumerated after the re-enumerate wait — which names the endpoint as the
    problem and sends the reader past the setpci that actually failed."""
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(bridge, "find_bridge_by_secondary_bus", lambda bdf: "0000:80:01.1")
    monkeypatch.setattr(health.subprocess, "run", _setpci_fake(assert_rc=1))

    assert bridge.reset_chip_via_bridge("13", "0000:fe:00.0") is False
    events = health.read_health_events(kinds={"bridge_reset_failed"})
    assert [e["reason"] for e in events] == ["setpci_assert_rc"], events
    assert events[0]["rc"] == 1 and events[0]["bridge"] == "0000:80:01.1", events


def test_bridge_reset_flags_a_failed_sbr_release_as_host_at_risk(monkeypatch, tmp_path):
    """Assert landed, release did not: Secondary Bus Reset stays asserted, so every device behind
    that bridge is held in reset — a wider outage than the one dead chip the rung came to fix, and
    setpci is the only lever. It must not read as a routine rung miss."""
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(bridge, "find_bridge_by_secondary_bus", lambda bdf: "0000:80:01.1")
    monkeypatch.setattr(health.subprocess, "run", _setpci_fake(release_rc=1))

    assert bridge.reset_chip_via_bridge("13", "0000:fe:00.0") is False
    events = health.read_health_events(kinds={"bridge_reset_failed"})
    assert [e["reason"] for e in events] == ["setpci_release_rc"], events
    assert events[0]["host_at_risk"] is True, events


@pytest.mark.asyncio
async def test_a_bridge_reset_leaves_a_visible_job_row(monkeypatch, tmp_path):
    """Every reset the broker performs must be auditable after the fact. A bridge reset is a
    real reset — it cycles chips off the bus and back — so, like the tt-smi resets, it owes
    the jobs list one row naming which chips it recovered."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    srv.isolated_chips = {"13"}
    srv.device_pci_map = {"13": "0000:46:00.0"}
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: True)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)
    assert ok is True

    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]bridge-reset"]
    assert len(rows) == 1, "a bridge reset must leave exactly one visible job row"
    assert "13" in rows[0]["command"], "the row must name the chip that was reset"
    assert rows[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_a_failed_bridge_reset_is_still_recorded_as_failed(monkeypatch, tmp_path):
    """A reset that did NOT bring the chip back is exactly the one an operator needs to see
    later — the row must exist and read 'failed', not silently vanish because it lost."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    srv.isolated_chips = {"13"}
    srv.device_pci_map = {"13": "0000:46:00.0"}
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: False)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)
    assert ok is False

    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]bridge-reset"]
    assert len(rows) == 1 and rows[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_a_timed_out_reset_does_not_arm_the_cooldown(monkeypatch):
    """A reset that TIMES OUT is still running in its own PID-1 scope — we never kill it
    mid-reset. The next gate adopts it via _await_foreign_reset_scope and verifies. Arming
    the 10-min cooldown on it suppresses exactly that re-verify, stretching a self-healing
    drop into a dead window. Only a real non-zero EXIT is a failure worth the cooldown."""

    async def no_foreign(log):
        return False

    async def timed_out(argv, log):
        return None, "reset timed out"

    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", no_foreign)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", timed_out)
    srv.recovery_mechanism.last_reset_failed = False

    ok = await srv.galaxy_recovery._reset_and_verify_device(["0", "1"], lambda m: None)

    assert ok is False, "a timed-out reset has not verified recovery"
    assert srv.recovery_mechanism.last_reset_failed is False, "a still-in-flight reset must not arm the cooldown"


@pytest.mark.asyncio
async def test_a_reset_that_exits_nonzero_still_arms_the_cooldown(monkeypatch):
    """The cooldown must still fire for a reset that actually EXITED non-zero: that is a real
    failure at a possibly-dead endpoint, and re-resetting it is what trips the host."""

    async def no_foreign(log):
        return False

    async def exited_1(argv, log):
        return 1, "reset failed"

    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", no_foreign)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", exited_1)
    srv.recovery_mechanism.last_reset_failed = False

    ok = await srv.galaxy_recovery._reset_and_verify_device(["0", "1"], lambda m: None)

    assert ok is False
    assert srv.recovery_mechanism.last_reset_failed is True, "a real non-zero exit must arm the cooldown"


@pytest.mark.asyncio
async def test_an_adopted_foreign_reset_that_failed_arms_the_cooldown(monkeypatch):
    """A reset scope adopted from a prior gate (or one still cycling after a broker restart) is a
    COMPLETED reset once we have waited it out. If it did not revive the mesh the cooldown must be
    armed exactly as for our own failed reset — otherwise the next gate fires another back-to-back
    galaxy reset, the repeat-MMIO-at-a-dead-endpoint the cooldown exists to prevent."""

    async def adopted(log):
        return True

    async def verify_bad(expected, log, **kw):
        return False, {}

    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", adopted)
    patch_recovery(monkeypatch, "_verify_device", verify_bad)
    srv.recovery_mechanism.last_reset_failed = False
    srv.recovery_mechanism.last_reset_monotonic = 0.0

    ok = await srv.galaxy_recovery._reset_and_verify_device(["0", "1"], lambda m: None)

    assert ok is False
    assert srv.recovery_mechanism.last_reset_failed is True, "an adopted-and-failed reset must arm the cooldown"
    assert (
        srv.recovery_mechanism.last_reset_monotonic > 0.0
    ), "the reset clock must advance so the gate sees a recent reset"


@pytest.mark.asyncio
async def test_bridge_reset_is_skipped_while_a_reset_scope_is_in_flight(monkeypatch, tmp_path):
    """A galaxy reset that timed out is still running in its own PID-1 scope. The surgical
    bridge reset ends in a system-wide PCI rescan, so starting it on top of that live scope
    runs two resets at once — the concurrent-reset hazard. The gate must skip the bridge
    reset while a scope is live and let _reset_and_verify_device wait it out instead."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    fsm_healthy(srv)
    srv.recovery_mechanism.last_reset_failed = False  # no cooldown in the way — isolate the scope guard
    srv.isolated_chips = {"13"}

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 13 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)

    recover_calls = {"n": 0}

    async def recover(log):
        recover_calls["n"] += 1
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    async def reset(indices, log):
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)

    # A reset scope is in flight -> the bridge reset MUST be skipped.
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "ttdev-reset-123-1")
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert recover_calls["n"] == 0, "bridge reset ran concurrently with an in-flight reset scope"

    # No scope in flight -> the surgical bridge reset runs as usual.
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert recover_calls["n"] == 1, "bridge reset must run when no reset scope is live"


def test_health_event_is_durable_jsonl():
    health_event("reset_begin", rc=0, chips=32)
    line = (health.HEALTH_DIR / health.EVENTS_FILE).read_text().strip()
    rec = json.loads(line)
    assert rec["kind"] == "reset_begin" and rec["chips"] == 32
    assert rec["ts"] and rec["iso"]


def test_health_event_never_raises_when_the_journal_is_unwritable(monkeypatch):
    """A journal that cannot be written must not take the broker down with it."""
    monkeypatch.setattr(health, "HEALTH_DIR", health.Path("/proc/nonexistent/nope"))
    health_event("reset_begin")  # must not raise


def test_journal_is_capped_so_it_cannot_fill_the_disk(monkeypatch):
    """A journal that fills the disk stops being written to — a worse failure than the
    one it exists to survive. It rolls to a single .1 and never grows past 2x the cap."""
    monkeypatch.setattr(health, "MAX_EVENTS_BYTES", 400)
    path = health.HEALTH_DIR / health.EVENTS_FILE

    for i in range(80):
        health_event("gate", i=i, filler="x" * 60)

    assert path.stat().st_size < 400 * 2, "live journal blew past its cap"
    rolled = path.with_suffix(path.suffix + ".1")
    assert rolled.exists(), "nothing was rolled — old records were silently dropped"
    # The newest records must be the ones that survived in the live file.
    last = json.loads(path.read_text().strip().splitlines()[-1])
    assert last["i"] == 79


# --- the reset is uninterruptible -------------------------------------------


@pytest.mark.asyncio
async def test_reset_runs_in_its_own_systemd_scope(monkeypatch):
    """A reset launched as a plain child of the broker sits in the broker's cgroup, so
    KillMode=control-group SIGTERMs it whenever the broker restarts — that is the
    `reset exited -15` that left 32 ASICs half-reset. Its own scope is owned by PID 1."""
    seen = {}

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv

        class P:
            returncode = 0

            async def communicate(self):
                return b"ok", b""

        return P()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    rc, out = await srv.recovery_mechanism.run_scoped(["tt-smi", "-glx_reset"], lambda m: None)

    assert rc == 0 and out == "ok"
    argv = seen["argv"]
    assert argv[0] == "systemd-run" and "--scope" in argv
    assert any(a.startswith("--unit=" + recovery_base.RESET_SCOPE_PREFIX) for a in argv)
    # The reset command must survive as the scope's payload, after the `--`.
    assert list(argv[argv.index("--") + 1 :]) == ["tt-smi", "-glx_reset"]


@pytest.mark.asyncio
async def test_reset_timeout_leaves_the_scope_running(monkeypatch):
    """Killing a reset that overran is worse than letting it finish: a half-reset mesh
    needs a power-cycle. We must never kill it."""
    killed = {"n": 0}

    async def fake_exec(*argv, **kwargs):
        class P:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            def kill(self):
                killed["n"] += 1

        return P()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    # Overrunning only earns a warning and more waiting; never ending is what fails.
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_OVERRUN_SEC", 0.02)
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_TIMEOUT_SEC", 0.05)
    rc, out = await srv.recovery_mechanism.run_scoped(["tt-smi", "-glx" + "_reset"], lambda m: None)

    assert rc is None and "never finished" in out
    assert killed["n"] == 0, "the reset was killed mid-flight — exactly the bug"


@pytest.mark.asyncio
async def test_a_reset_that_cannot_launch_leaves_a_visible_job_row(monkeypatch, tmp_path):
    """A reset that never launches is a reset attempt too — and the one an operator most needs
    to find later, because the recovery ladder tried and could not even start. Like the timeout
    path, it owes the jobs list one row; otherwise the failed attempt vanishes silently."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)

    async def fake_exec(*argv, **kwargs):
        raise OSError("systemd-run: command not found")

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    rc, out = await srv.recovery_mechanism.run_scoped(["tt-smi", "-glx_reset"], lambda m: None)

    assert rc is None and "could not be launched" in out
    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]health-gate"]
    assert len(rows) == 1, "a reset that could not launch must leave exactly one visible row"
    assert rows[0]["status"] == "error", "the row must read 'error' — it never ran, so not 'failed'"
    assert "-glx_reset" in rows[0]["command"], "the row must name the reset it tried to run"


# --- the reset lands on a quiesced bus ---------------------------------------


@pytest.mark.asyncio
async def test_pollers_are_stopped_before_the_reset_and_restarted_after(monkeypatch):
    """tt-telemetry core-dumped 125x with SIGBUS polling a dead endpoint and kept
    coming back. Non-completing MMIO at a dead chip is what drives the root complex
    past its error threshold, at which point firmware-first RAS reboots the host."""
    order = []

    async def pollers(active, log):
        order.append("start" if active else "stop")
        return ["tt-telemetry.service"]

    async def reset(argv, log, owner="[broker]health-gate"):
        order.append("reset")
        return 0, ""

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", reset)
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)

    assert order == ["stop", "reset", "start"]


@pytest.mark.asyncio
async def test_pollers_are_restarted_even_if_the_reset_raises(monkeypatch):
    order = []

    async def pollers(active, log):
        order.append("start" if active else "stop")
        return ["tt-telemetry.service"]

    async def reset(argv, log, owner="[broker]health-gate"):
        raise OSError("boom")

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", reset)
    with pytest.raises(OSError):
        await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)

    assert order == ["stop", "start"], "a failed reset must not leave telemetry down"


@pytest.mark.asyncio
async def test_pollers_quiesce_journals_none_active_when_configured_units_resolve_to_nothing(monkeypatch):
    """Configured poller units that resolve to no active unit make the quiesce a no-op: the reset
    then lands on a bus the broker only ASSUMES is quiet, while the real pollers may still hammer
    MMIO under a name it was never told — the exact condition that reboots the host. A no-op quiesce
    must land in the journal loud, not read as a clean one."""
    monkeypatch.setattr(srv, "DEVICE_POLLER_SERVICES", ("tt-telemetry.service", "tt-metrics-exporter.service"))

    class _NotLoaded:
        returncode = 5  # `systemctl stop` against a unit not loaded on this host

        async def wait(self):
            return self.returncode

    async def _exec(*argv, **kw):
        return _NotLoaded()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _exec)

    touched = await srv._set_device_pollers(False, lambda m: None)

    assert touched == []
    events = health.read_health_events(kinds={"pollers_none_active"})
    assert len(events) == 1, events
    assert events[0]["verb"] == "stop"
    assert events[0]["host_at_risk"] is True
    assert events[0]["configured"] == ["tt-telemetry.service", "tt-metrics-exporter.service"]


@pytest.mark.asyncio
async def test_pollers_quiesce_stays_quiet_when_no_pollers_are_configured(monkeypatch):
    """An operator who leaves TT_DEVICE_MCP_POLLER_SERVICES empty has declared this host has no
    pollers to stop — opting out, not a misconfiguration. The none-active alarm must fire only for
    configured units that resolve to nothing, never for a deliberate empty list."""
    monkeypatch.setattr(srv, "DEVICE_POLLER_SERVICES", ())

    async def _exec(*argv, **kw):
        raise AssertionError("no poller is configured; systemctl must not be invoked")

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _exec)

    touched = await srv._set_device_pollers(False, lambda m: None)

    assert touched == []
    assert health.read_health_events(kinds={"pollers_none_active"}) == []


# --- the reset is exclusive --------------------------------------------------


@pytest.mark.asyncio
async def test_device_ops_never_overlap(monkeypatch):
    """Two concurrent -glx_reset calls at the same 32 ASICs is how one wedged chip
    became a mesh that only a power-cycle recovered. The gate and the reset tool must
    serialize on the same lock."""
    srv.device_op_lock = None  # fresh lock bound to this test's event loop
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    concurrent = {"now": 0, "max": 0}

    async def op(name):
        async with srv._device_op(name):
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
            await asyncio.sleep(0.02)
            concurrent["now"] -= 1

    await asyncio.gather(*(op(f"op-{i}") for i in range(5)))
    assert concurrent["max"] == 1, "device operations ran concurrently"


# --- the reset is not hammered ----------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_reset_is_not_immediately_retried(monkeypatch, tmp_path):
    """A reset that did not revive the mesh will not revive it on an immediate retry,
    and each extra reset is another pass of MMIO at a dead endpoint. Back off instead."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)

    resets = {"n": 0}

    async def never_recovers(indices, log):
        resets["n"] += 1
        return False

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "wedged"}}

    patch_recovery(monkeypatch, "_reset_and_verify_device", never_recovers)
    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0")  # isolate the reset/cooldown path

    srv.recovery_mechanism.last_reset_monotonic = 0.0
    srv.recovery_mechanism.last_reset_failed = False

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert resets["n"] == 1

    # The gate marks the attempt failed; a second gate inside the cooldown must not
    # reset again.
    srv.recovery_mechanism.last_reset_failed = True
    srv.recovery_mechanism.last_reset_monotonic = srv.time.monotonic()
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)
    assert resets["n"] == 1, "a second reset fired inside the cooldown window"


@pytest.mark.asyncio
async def test_a_healthy_device_is_not_reset_just_because_a_job_timed_out(monkeypatch, tmp_path):
    """A job timing out says something about the job, not about the silicon.

    This gate used to reset on any abnormal job exit without consulting the health
    check — so a run of timeouts reset a verifiably healthy 32-chip galaxy over and
    over, ~60s a time, for nothing.
    """
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")

    resets = {"n": 0}

    async def reset(indices, log):
        resets["n"] += 1
        return True

    # Healthy. The traffic pass is NOT expected here any more: pre-job never runs it, because its
    # ~45-100s lands on a submitter who is only waiting to start (one submit paid 102s and was held
    # anyway). A dirty device is held on the flag; the pass that clears it runs post-job, at
    # startup, or in the ladder — all with the device idle. The subject of this test is unchanged:
    # an abnormal job exit must not by itself reset a healthy device.
    async def healthy(expected, log, run_fabric=True, **_):
        assert not run_fabric, "pre-job must not run the slow fabric pass on a submitter's clock"
        return True, {"fabric": {"ok": True, "detail": "links healthy"}}

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    patch_recovery(monkeypatch, "_verify_device", healthy)

    srv._mark_device_dirty("job 065 ended timeout")
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)

    assert resets["n"] == 0, "reset a healthy device because a job timed out"
    assert srv.fsm.state is ServerState.HEALTHY, "verified-healthy device should be marked clean"


@pytest.mark.asyncio
async def test_dirty_device_is_not_reset_when_the_fabric_cannot_be_checked(monkeypatch, tmp_path):
    """Enumeration + ARC heartbeat pass but the fabric traffic pass could not run, so a
    wedged eth core cannot be ruled out. We used to reset here to be safe — but on marginal
    silicon that blanket galaxy reset is itself what knocks a chip off the bus: in prod a
    user-killed job forced this reset, and 3s after it reported "health verified" chip 26
    dropped and all 32 read 0xFFFFFFFF, dead until a UBB power cycle. The reset cannot be
    verified either (fabric still will not check afterward), and a wedged eth core is a fast,
    recoverable per-job failure — so do NOT reset, and keep the queue moving."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    srv.recovery_mechanism.last_reset_monotonic = 0.0
    srv.recovery_mechanism.last_reset_failed = False

    resets = {"n": 0}

    async def reset(indices, log):
        resets["n"] += 1
        return True

    async def healthy_but_no_fabric(expected, log, run_fabric=True, **_):
        return True, {"fabric": {"ok": None, "detail": "validator not installed"}}

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    patch_recovery(monkeypatch, "_verify_device", healthy_but_no_fabric)

    srv._mark_device_dirty("job ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 0, "a galaxy reset on unverifiable fabric wedges marginal silicon"
    assert (
        srv.fsm.record.why == "fabric_unverified"
    ), "the dirty flag drops (no reset owed) but the door holds until a real fabric verdict"


@pytest.mark.asyncio
async def test_a_dirty_flag_dropped_without_a_check_leaves_a_durable_trace(monkeypatch, tmp_path):
    """The gate meets a device a foreign tenant is using: it will not touch it, and it
    drops the dirty flag. That is a real "the last job left this unverified" state going
    away with nobody having looked at the silicon — so it must leave a mark on the health
    timeline. A recovery clears the flag too, but the reset/verify it ran is the record;
    this path has no such record, so the clear itself is the only thing worth journalling.
    Without it, the device reads clean at 14:05 after reading dirty at 14:00 with no reset
    between — a hole no postmortem can close."""
    from tt_device_mcp.device_holders import DeviceHolder

    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=4242, uid=1000)], complete=True)
    )
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    srv._mark_device_dirty("job 511 ended killed (exit -9)")
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert (
        srv.fsm.record.why == "foreign_holder"
    ), "the gate did not reword the hold once a foreign tenant blocked the verify"
    cleared = [f for k, f in events if k == "device_dirty_cleared"]
    assert cleared, "the dirty flag was dropped with no durable trace on the health timeline"
    assert cleared[0]["prior_reason"] == "job 511 ended killed (exit -9)"
    assert "foreign holder" in cleared[0]["why"]
    assert cleared[0]["verified"] is False, "no silicon was checked; the clear must say so"


@pytest.mark.asyncio
async def test_a_verified_clear_names_itself_and_records_it_was_verified(monkeypatch, tmp_path):
    """The gate's HEALTHY branch used to drop the dirty flag with a bare clear that logged
    nothing — the very path that let a blackout-marked flag go False between an idle window
    and the next job with no gate, no reset, and no record. A verified clear now emits the
    same durable ``device_dirty_cleared`` trace as an unverified one, marked ``verified`` so
    a postmortem can tell a checked mesh from a flag someone merely dropped."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv.health_monitor, "verify_device_health", lambda n, **k: (True, "ok"))
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "exit 0")

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    srv._mark_device_dirty("job 611 ended killed (exit -9)")
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert srv.fsm.state is ServerState.HEALTHY, "a verified-healthy gate must release the flag"
    cleared = [f for k, f in events if k == "device_dirty_cleared"]
    assert cleared, "the healthy gate dropped the dirty flag with no durable trace"
    assert cleared[0]["verified"] is True, "a full snapshot+fabric pass stood behind this clear"
    assert cleared[0]["prior_reason"] == "job 611 ended killed (exit -9)"


@pytest.mark.asyncio
async def test_a_clean_job_does_not_pay_for_a_fabric_pass(monkeypatch, tmp_path):
    """The traffic pass costs ~45s. A clean exit on a mesh whose chips are all ticking
    has nothing to explain, and charging every job 45s to re-prove that is how the
    device ends up being checked instead of used."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    fsm_healthy(srv)
    srv.last_fabric_check_monotonic = srv.time.monotonic()  # a pass ran recently

    seen = {}

    async def verify(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert seen["run_fabric"] is False, "a clean job was charged for a fabric pass"


@pytest.mark.asyncio
async def test_gate_runs_the_fabric_pass_when_asked_and_no_pass_is_fresh(monkeypatch, tmp_path):
    """When a caller asks (run_fabric) and no pass has run this process — the startup
    check after a boot — the gate runs the ~45s fabric pass and advances the clock."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    fsm_healthy(srv)
    srv.last_fabric_check_monotonic = 0.0  # nothing has run in this process's lifetime

    seen = {}

    async def verify(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        return True, {"fabric": {"ok": True, "detail": "links healthy"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._device_health_gate(None, phase="startup", run_fabric=True)

    assert seen["run_fabric"] is True
    assert srv.last_fabric_check_monotonic > 0, "the fabric-pass clock never advanced"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age_sec, expect_fabric",
    [
        (9 * 60, False),  # inside the window: the last pass still speaks for the fabric
        (11 * 60, True),  # past it: a wedged eth core must not hide longer than this
    ],
)
async def test_gate_fabric_pass_respects_the_stale_interval(monkeypatch, tmp_path, age_sec, expect_fabric):
    """A run_fabric caller pays for the ~45s pass at most once per interval: inside the window the
    last pass still speaks for the fabric, past it the gate runs a fresh one."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "FABRIC_CHECK_MIN_INTERVAL_SEC", 600)
    fsm_healthy(srv)
    # Fake clock so `now - age` cannot land on a negative tick on a low-uptime runner.
    clock = {"t": 10_000.0}
    monkeypatch.setattr(srv.time, "monotonic", lambda: clock["t"])
    srv.last_fabric_check_monotonic = clock["t"] - age_sec

    seen = {}

    async def verify(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        evidence = {"fabric": {"ok": True, "detail": "links healthy"}} if run_fabric else {}
        return True, evidence

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert seen["run_fabric"] is expect_fabric


def test_fabric_interval_defaults_to_twenty_minutes():
    assert srv.FABRIC_CHECK_MIN_INTERVAL_SEC == 1200


@pytest.mark.asyncio
async def test_a_failed_job_forces_a_fabric_pass_even_inside_the_quiet_window(monkeypatch, tmp_path):
    """One tenant's job fails; the next tenant must not inherit an unproven mesh just
    because a pass happened to run three minutes ago.

    A failure means we do not know what the job did to the fabric, and "a pass ran
    recently" is not an answer to that. The interval exists to avoid taxing SUCCESSFUL
    jobs — it does not get a vote on whether a failure is investigated.
    """
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    srv.last_fabric_check_monotonic = srv.time.monotonic()  # a pass ran seconds ago

    seen = {}
    resets = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        evidence = {"fabric": {"ok": True, "detail": "links healthy"}} if run_fabric else {}
        return True, evidence

    async def reset(indices, log):
        resets["n"] += 1
        return True

    patch_recovery(monkeypatch, "_verify_device", verify)
    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)

    await srv._verify_device_after_job(None, job_failed=True)

    assert seen["run_fabric"] is True, "a failed job handed the mesh on unproven"
    # ...but a pytest assertion failure is not evidence of broken silicon. Verify, then
    # believe the answer — resetting on exit 1 is the superstition that caused the storm.
    assert resets["n"] == 0


@pytest.mark.asyncio
async def test_a_successful_job_still_respects_the_quiet_window(monkeypatch, tmp_path):
    """The flip side: success inside the window stays cheap, or we are back to charging
    every job 45s."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    fsm_healthy(srv)
    srv.last_fabric_check_monotonic = srv.time.monotonic()

    seen = {}

    async def verify(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._verify_device_after_job(None, job_failed=False)

    assert seen["run_fabric"] is False


@pytest.mark.asyncio
async def test_broker_device_work_shows_up_as_running(monkeypatch):
    """A reset or fabric pass owns the device for 45-60s during which nothing can start.
    Reporting an empty queue through that window makes the broker look hung to whoever
    is waiting — these ops used to appear only in RECENT, after they had finished."""
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    app = srv.build_asgi_app(srv.create_mcp_server())

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        idle = client.post("/api/tt_device_queue_status", json={}).json()
        assert idle["running"] == [] and idle["device_busy"] is False

        async with srv._device_op("health-gate/post-job"):
            srv._set_device_op_detail("health check: fabric traffic pass across all links (~45s)")
            busy = client.post("/api/tt_device_queue_status", json={}).json()

    assert busy["device_busy"] is True
    assert len(busy["running"]) == 1
    row = busy["running"][0]
    assert row["owner"] == "[broker]"
    assert "fabric traffic pass" in row["command"]
    assert row["started_at"], "no start time, so the watcher cannot show a runtime"


@pytest.mark.asyncio
async def test_the_owner_comes_from_the_peer_uid_and_the_surface(monkeypatch, clear_job_state):
    """The submitter is named by the socket, not by the request. A job off the MCP surface is
    tagged `[agent]<user>`; the same call over /api/* is the bare user. Nothing the caller sends
    can change either, so the tag cannot be claimed by someone it does not describe."""
    from tt_device_mcp.socket_transport import current_peer_uid, current_via_mcp

    monkeypatch.setattr(srv, "username_for_uid", lambda uid: "jdoe" if uid == 4242 else f"uid:{uid}")
    monkeypatch.setattr(srv, "job_log_dir", None)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    mcp = srv.create_mcp_server()

    tok = current_peer_uid.set(4242)
    mcp_tok = current_via_mcp.set(False)  # the CLI's /api/* surface
    try:
        out = await mcp.call_tool("tt_device_job_run_bg", {"params": {"workspace": "/tmp", "command": "echo hi"}})
        job = srv.jobs[json.loads(out.content[0].text)["job_id"]]
        assert job.owner == "jdoe", f"a CLI submit stored {job.owner!r}, not the peercred user"

        current_via_mcp.set(True)  # an agent, through the stdio shim
        out2 = await mcp.call_tool("tt_device_job_run_bg", {"params": {"workspace": "/tmp", "command": "echo hi"}})
        job2 = srv.jobs[json.loads(out2.content[0].text)["job_id"]]
        assert job2.owner == "[agent]jdoe", f"an agent submit stored {job2.owner!r}"
    finally:
        current_via_mcp.reset(mcp_tok)
        current_peer_uid.reset(tok)


@pytest.mark.asyncio
async def test_rest_submit_clamps_timeout_to_the_hard_ceiling(monkeypatch, clear_job_state):
    """The 25-min ceiling has to hold on the REST path, not only the MCP tool's schema. The CLI
    and tt-run submit through this route and pass timeout_sec through raw; a job that asked for an
    hour ran for most of it before this was enforced at the shared queue helper."""
    srv.device_op_lock = None
    monkeypatch.setattr(srv, "job_log_dir", None)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    app = srv.build_asgi_app(srv.create_mcp_server())

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        res = client.post(
            "/api/tt_device_job_run_bg",
            json={
                "owner": "mjones",
                "workspace": "/tmp",
                "command": "echo hi",
                "timeout_sec": 3600,
            },
        ).json()

    assert "job_id" in res, res
    job = srv.jobs[res["job_id"]]
    assert (
        job.timeout_sec == srv.MAX_TIMEOUT_SEC
    ), f"REST submit honored {job.timeout_sec}s — the {srv.MAX_TIMEOUT_SEC}s ceiling was bypassed"


@pytest.mark.asyncio
async def test_rest_submit_bad_env_file_is_a_refusal_not_a_500(monkeypatch, clear_job_state, tmp_path):
    """A bad env file is client input, so the submit must come back {"error": ...} like every
    other refusal. load_env_file documents three failure modes; only FileNotFoundError was
    caught, so non-dict content (and unparseable YAML) escaped the endpoint as a raw 500
    traceback — seen live: `ValueError: Env file must contain key-value pairs, got str`."""
    srv.device_op_lock = None
    monkeypatch.setattr(srv, "job_log_dir", None)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    app = srv.build_asgi_app(srv.create_mcp_server())

    from starlette.testclient import TestClient

    not_a_dict = tmp_path / "scalar.yaml"
    not_a_dict.write_text("just a string\n")
    not_yaml = tmp_path / "broken.yaml"
    not_yaml.write_text("key: [unclosed\n")

    with TestClient(app) as client:
        for env_file in (not_a_dict, not_yaml):
            res = client.post(
                "/api/tt_device_job_run_bg",
                json={
                    "owner": "tester",
                    "workspace": str(tmp_path),
                    "command": "echo hi",
                    "env": str(env_file),
                },
            )
            assert res.status_code == 200, f"{env_file.name}: bad env file must not 500"
            body = res.json()
            assert (
                "error" in body and "job_id" not in body
            ), f"{env_file.name}: expected a structured refusal, got {body}"


@pytest.mark.asyncio
async def test_broker_row_times_the_stage_it_names(monkeypatch):
    """The row's clock and the row's label have to measure the same thing.

    A post-job gate that times out its fabric pass, resets, and then re-proves the fabric
    is twelve minutes into the OP and forty seconds into the ~45s CHECK it is showing. Timed
    from the op, that check reads as a 45s check hung for twelve minutes — a dead device.
    It was neither: it was seconds from finishing.
    """
    from datetime import datetime, timedelta

    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    app = srv.build_asgi_app(srv.create_mcp_server())

    from starlette.testclient import TestClient

    held_for = timedelta(minutes=12)
    with TestClient(app) as client:
        async with srv._device_op("health-gate/post-job"):
            # The op has owned the device for twelve minutes: a 600s fabric timeout and a
            # galaxy reset. Only NOW does it start the pass it is about to report.
            srv.device_op_started_at = (datetime.now() - held_for).isoformat()
            srv._set_device_op_detail("health check: fabric traffic pass across all links (~45s)")
            row = client.post("/api/tt_device_queue_status", json={}).json()["running"][0]

    elapsed = (datetime.now() - datetime.fromisoformat(row["started_at"])).total_seconds()
    assert "fabric traffic pass" in row["command"]
    assert elapsed < 60, (
        f"the row labels a ~45s fabric pass but clocks it at {elapsed:.0f}s — that is the "
        f"whole op's elapsed time wearing the current stage's name"
    )

    # The twelve-minute hold is a real thing to know; it just is not how long the fabric
    # pass has been running. It stays reportable, under the name of the op it belongs to.
    assert row["op"] == "health-gate/post-job"
    op_elapsed = (datetime.now() - datetime.fromisoformat(row["op_started_at"])).total_seconds()
    assert op_elapsed >= held_for.total_seconds()


# --- one device, one gate ----------------------------------------------------
#
# The invariant: no tenant work starts while a broker device op holds the device, or while
# the device is dirty and unverified. Every path onto the device answers to it.


def _quiet_post_job_gate(monkeypatch):
    """The post-job gate is not what these tests are about; keep the runner off the device."""

    async def _noop(job_log_file, job_failed=False):
        return None

    monkeypatch.setattr(srv, "_verify_device_after_job", _noop)


def _free_device_lock(monkeypatch):
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")


@pytest.mark.asyncio
async def test_no_job_starts_while_a_broker_op_holds_the_device(monkeypatch, clear_job_state):
    """ANY broker device op, on a device nobody has flagged. Not just a dirty one.

    The fabric traffic pass deliberately pushes packets across every inter-chip ethernet
    link for ~45s. A tenant job overlapping it corrupts in BOTH directions: a CCL benchmark
    measures the broker's traffic as its own, and the broker's health verdict is taken over
    a mesh someone else is loading. Nothing about that needs the device to be dirty — the
    clean, post-job, exit-0 path holds the device just as hard, and is the common case.

    The runner asked "is a tenant job running?" and called a device with a fabric pass in
    flight idle, because the queue's notion of idle and the device lock were not the same
    gate. Nothing in the runner said otherwise — it merely happened to be single-threaded,
    which is a property of the call graph, not a promise to anyone.
    """
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)

    job = srv.Job(id="900", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    srv.jobs["900"] = job
    await srv.get_job_queue().put("900")

    runner = asyncio.create_task(srv.job_runner())
    try:
        async with srv._device_op("health-gate/post-job"):
            srv._set_device_op_detail("health check: fabric traffic pass across all links (~45s)")
            assert srv.fsm.state is ServerState.HEALTHY, "the clean path is the one under test"
            await asyncio.sleep(0.4)
            assert job.status is srv.JobStatus.QUEUED, (
                f"job 900 reached {job.status.value} while the broker was driving traffic "
                f"across every ethernet link — its measurements and ours are now both junk"
            )

        # The broker hands the device back: now, and only now, the job may have it.
        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert job.status is not srv.JobStatus.QUEUED, "the gate never let the job through"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_no_job_starts_on_a_device_flagged_dirty(monkeypatch, clear_job_state):
    """The other half of the same invariant. A killed job can leave an ethernet core
    unretrained, and the next tenant is the one who finds out — so the flag has to be
    answered (verified, reset if need be) before anyone else's job touches the mesh."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)

    gate_ran = asyncio.Event()

    async def gate(job_log_file, *, phase, run_fabric, force_fabric=False):
        # Stand in for the real reset+verify: the device is only clean once this has run.
        assert srv.fsm.state is not ServerState.HEALTHY, "the gate was run on a device nobody had flagged"
        gate_ran.set()
        await asyncio.sleep(0.2)
        srv._clear_device_dirty(verified=True)

    monkeypatch.setattr(srv, "_device_health_gate", gate)
    srv._mark_device_dirty("job 482 ended killed (exit -15)")

    job = srv.Job(id="901", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    srv.jobs["901"] = job
    await srv.get_job_queue().put("901")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert gate_ran.is_set(), "the job ran without the dirty device ever being verified"
        assert srv.fsm.state is ServerState.HEALTHY, "the job started while the device was still dirty"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


# --- nothing of a finished job outlives it; nothing outside the broker shares it ------
#
# A finished privsep job's scope can outlive the job: killpg on the wrapper pid misses a child
# that left the job's process group. The post-job gate then sees a uid >= 1000 holder and skips
# every probe, so the next job was dispatched beside the leftover onto an unchecked device.


def _patch_device_holders(monkeypatch, scan, ppids=None):
    """``ppids`` maps pid -> parent pid for the parent-chain walk; a pid not in it reads as gone,
    so no test depends on the real /proc."""
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: ["0"])
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: scan)
    monkeypatch.setattr(device_holders, "_read_proc_ppid", (ppids or {}).get)


async def _run_one_job(monkeypatch, job_id, *, privsep, scope_active, holders=lambda: ""):
    """Run one exit-0 job through the real runner; return (job, systemctl calls, is-active calls, killpgs).

    ``scope_active`` answers every is-active query, or is a list answered in order (the last
    answer then repeats). ``holders`` stands in for the holder scan before dispatch."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)

    async def _free_gate(job_log_file):
        return ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _free_gate)
    monkeypatch.setattr(srv, "_tenant_holder_reason", holders)
    monkeypatch.setattr(srv, "_HOLDER_WAIT_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **kw: ("", None))  # no venv to source here
    monkeypatch.setattr(srv, "privsep_prefix_for", lambda uid, unit=None: ["env"] if privsep else None)
    monkeypatch.setattr(srv, "GRACEFUL_KILL_GRACE_SEC", 0.05)
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_SEC", 0.05)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_POLL_SEC", 0.01)

    active_checks = []
    answers = list(scope_active) if isinstance(scope_active, list) else [scope_active]

    def _active(scope):
        active_checks.append(scope)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(srv, "_scope_active", _active)

    systemctl = []
    real_run = srv.subprocess.run

    def _run(argv, *a, **kw):
        if argv and argv[0] == "systemctl":
            systemctl.append(list(argv))
            return srv.subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(srv.subprocess, "run", _run)

    killpgs = []
    real_killpg = srv.os.killpg

    def _killpg(pgid, sig):
        killpgs.append((pgid, len(systemctl)))
        real_killpg(pgid, sig)

    monkeypatch.setattr(srv.os, "killpg", _killpg)

    job = srv.Job(id=job_id, owner="tenant", workspace="/tmp", command="true", queued_at="t")
    srv.jobs[job_id] = job
    await srv.get_job_queue().put(job_id)
    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.finished_at:
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.finished_at, "the job never finished"
    return job, systemctl, active_checks, killpgs


@pytest.mark.asyncio
async def test_a_completed_privsep_job_stops_its_scope(monkeypatch, clear_job_state):
    """A leftover keeps the scope alive after an exit-0 job: it gets SIGINT, then the stop,
    before the job is finalized and before any killpg SIGKILLs it."""
    job, systemctl, _, killpgs = await _run_one_job(monkeypatch, "920", privsep=True, scope_active=True)
    scope = srv.job_scope_unit("920")
    assert job.status is srv.JobStatus.COMPLETED
    assert systemctl == [
        ["systemctl", "kill", "--signal=SIGINT", scope],
        ["systemctl", "stop", scope],
    ], "a finished privsep job left its scope, and whatever is still in it, holding the device"
    assert killpgs and killpgs[0][1] == 2, "the leftover was SIGKILLed before it was offered SIGINT"


@pytest.mark.asyncio
async def test_a_scope_that_ended_with_its_job_is_not_signalled(monkeypatch, clear_job_state):
    """The common case costs one is-active query and nothing else."""
    job, systemctl, active_checks, _ = await _run_one_job(monkeypatch, "921", privsep=True, scope_active=False)
    assert job.status is srv.JobStatus.COMPLETED
    assert active_checks == [srv.job_scope_unit("921")]
    assert not systemctl


@pytest.mark.asyncio
async def test_a_scope_that_settles_after_its_job_is_not_signalled(monkeypatch, clear_job_state):
    """systemd sees an emptied scope asynchronously: a scope still active for a moment after a clean
    exit is not a leftover, and is not logged or signalled as one."""
    job, systemctl, active_checks, _ = await _run_one_job(
        monkeypatch, "924", privsep=True, scope_active=[True, True, False]
    )
    assert job.status is srv.JobStatus.COMPLETED
    assert len(active_checks) == 3
    assert not systemctl


@pytest.mark.asyncio
async def test_an_interrupted_scope_is_reaped_without_a_second_sigint(monkeypatch):
    """A kill or the hung reaper already sent SIGINT; a second one can abort the teardown it
    started. The leftover is still reaped once the grace runs out."""
    monkeypatch.setattr(srv, "GRACEFUL_KILL_GRACE_SEC", 0.05)
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_SEC", 0.02)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: True)
    systemctl = []

    def _run(argv, *a, **kw):
        systemctl.append(list(argv))
        return srv.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(srv.subprocess, "run", _run)
    marks = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **kw: marks.append(reason))
    await srv._stop_job_scope("925", None, interrupted=True)
    assert systemctl == [["systemctl", "stop", srv.job_scope_unit("925")]]
    assert len(marks) == 1, "the reaped leftover was not flagged"


@pytest.mark.asyncio
async def test_a_scope_reaped_after_a_clean_exit_marks_the_device_dirty(monkeypatch, clear_job_state):
    """An exit-0 job raises no wedge-risk flag of its own. When its leftover outlives SIGINT and the
    stop kills it without unwinding, the device is flagged so the next gate resets and verifies it."""
    marks = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **kw: marks.append((reason, job)))
    job, systemctl, _, _ = await _run_one_job(monkeypatch, "926", privsep=True, scope_active=True)
    scope = srv.job_scope_unit("926")
    assert job.status is srv.JobStatus.COMPLETED and job.exit_code == 0
    assert systemctl[-1] == ["systemctl", "stop", scope]
    assert len(marks) == 1, "a leftover was stopped without unwinding and the device was left clean"
    reason, marked_job = marks[0]
    assert scope in reason and marked_job is job


@pytest.mark.asyncio
async def test_a_scope_that_ends_on_sigint_leaves_the_device_clean(monkeypatch):
    """A leftover that unwinds on SIGINT released the device itself: no reap, no dirty flag."""
    monkeypatch.setattr(srv, "GRACEFUL_KILL_GRACE_SEC", 0.05)
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_SEC", 0.02)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_POLL_SEC", 0.01)
    systemctl = []
    monkeypatch.setattr(srv, "_scope_active", lambda scope: not systemctl)

    def _run(argv, *a, **kw):
        systemctl.append(list(argv))
        return srv.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(srv.subprocess, "run", _run)
    marks = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **kw: marks.append(reason))
    await srv._stop_job_scope("927", None)
    assert systemctl == [["systemctl", "kill", "--signal=SIGINT", srv.job_scope_unit("927")]]
    assert not marks


@pytest.mark.asyncio
async def test_a_reaped_scope_of_a_recovery_killed_job_is_not_flagged(monkeypatch):
    """Our own recovery killed the job to reset the device; its reap is not evidence for another
    reset (I14)."""
    monkeypatch.setattr(srv, "GRACEFUL_KILL_GRACE_SEC", 0.05)
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_SEC", 0.02)
    monkeypatch.setattr(srv, "_SCOPE_SETTLE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: True)
    monkeypatch.setattr(srv.subprocess, "run", lambda argv, *a, **kw: srv.subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(srv, "reset_killed_job_ids", {"928"})
    marks = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **kw: marks.append(reason))
    await srv._stop_job_scope("928", None, interrupted=True)
    assert not marks


@pytest.mark.asyncio
async def test_a_completed_non_privsep_job_only_killpgs_its_group(monkeypatch, clear_job_state):
    """No scope, so nothing to query or stop: the process-group kill is all there is."""
    job, systemctl, active_checks, killpgs = await _run_one_job(monkeypatch, "922", privsep=False, scope_active=True)
    assert job.status is srv.JobStatus.COMPLETED
    assert not active_checks and not systemctl
    assert [pgid for pgid, _ in killpgs] == [job.pid]


@pytest.mark.asyncio
async def test_a_tenant_holder_blocks_dispatch(monkeypatch, clear_job_state):
    """A process outside the broker holds the device and the hold is off: the job is refused
    without running, before the gate, and the refusal names who holds it."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD", "0")
    _patch_device_holders(monkeypatch, HolderScan(holders=[DeviceHolder(pid=4242, uid=1234)]))
    gate_calls = []

    async def _gate(job_log_file):
        gate_calls.append(job_log_file)
        return ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _gate)

    job = srv.Job(id="923", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    srv.jobs["923"] = job
    await srv.get_job_queue().put("923")
    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.FAILED and job.started_at is None, "the job ran beside a foreign holder"
    assert "device busy" in job.error and "pid 4242" in job.error
    assert not gate_calls


@pytest.mark.asyncio
async def test_a_tenant_holder_holds_the_job_until_it_exits(monkeypatch, clear_job_state):
    """With the hold on (the default) the job waits while the holder stays and runs once it is
    gone. A busy device is not a degraded one: no device_held episode opens for it."""
    monkeypatch.delenv("TT_DEVICE_MCP_TENANT_HOLD", raising=False)
    held = []
    monkeypatch.setattr(srv, "_note_tenant_gate_verdict", lambda reason: reason and held.append(reason))
    scans = ["device held outside the broker by u(pid 4242)"] * 3 + [""]
    job, _, _, _ = await _run_one_job(
        monkeypatch, "926", privsep=False, scope_active=False, holders=lambda: scans.pop(0) if scans else ""
    )
    assert job.status is srv.JobStatus.COMPLETED
    assert not scans, "the job was dispatched while the holder was still there"
    assert not held, "a busy device was recorded as a degraded hold"


@pytest.mark.parametrize(
    "holder",
    [
        DeviceHolder(pid=4242, uid=0),  # root: the broker's own probes, a system daemon
        DeviceHolder(pid=4242, uid=999),  # a service account below MIN_TENANT_UID
        DeviceHolder(pid=os.getpid(), uid=1234),  # a per-user broker holding the device itself
    ],
    ids=["root", "service-account", "broker-pid"],
)
def test_broker_owned_holders_do_not_block_dispatch(monkeypatch, holder):
    _patch_device_holders(monkeypatch, HolderScan(holders=[holder]))
    assert srv._tenant_holder_reason() == ""


def test_the_brokers_own_child_probe_does_not_block_dispatch(monkeypatch):
    """A per-user broker runs its probes (startup fabric verify, relift, reset, post-step gate)
    as subprocesses under the tenant's uid; one holding the device is not an outside holder."""
    probe, shell = 5001, 5000  # broker -> shell -> probe
    _patch_device_holders(
        monkeypatch,
        HolderScan(holders=[DeviceHolder(pid=probe, uid=1234)]),
        ppids={probe: shell, shell: os.getpid()},
    )
    assert srv._tenant_holder_reason() == ""


def test_a_leftover_reparented_away_from_the_broker_still_blocks_dispatch(monkeypatch):
    """A daemonized leftover reparented to init is no longer the broker's child: it still counts."""
    _patch_device_holders(monkeypatch, HolderScan(holders=[DeviceHolder(pid=5001, uid=1234)]), ppids={5001: 1})
    assert "pid 5001" in srv._tenant_holder_reason()


def test_a_holder_that_exits_mid_walk_does_not_break_the_scan(monkeypatch):
    """The holder's parent is gone before its stat is read: no crash, and it is not exempted."""
    _patch_device_holders(monkeypatch, HolderScan(holders=[DeviceHolder(pid=5001, uid=1234)]), ppids={5001: 5000})
    assert "pid 5001" in srv._tenant_holder_reason()


def test_a_parent_cycle_ends_the_walk(monkeypatch):
    _patch_device_holders(
        monkeypatch, HolderScan(holders=[DeviceHolder(pid=5001, uid=1234)]), ppids={5001: 5000, 5000: 5001}
    )
    assert "pid 5001" in srv._tenant_holder_reason()


def test_an_incomplete_holder_scan_does_not_block_dispatch(monkeypatch):
    """A per-user broker cannot read other users' fds; that blind spot must not stop its queue."""
    _patch_device_holders(monkeypatch, HolderScan(holders=[], complete=False))
    assert srv._tenant_holder_reason() == ""


# --- inter-job cooldown ------------------------------------------------------
#
# Back-to-back saturating jobs with zero gap is a degradation cause, not just a wedge symptom:
# hours of continuous 32-chip CCL/fabric traffic with no idle between runs is what pushed a
# marginal tray off the bus. The cooldown is the guaranteed mesh rest between one job's device
# work ending and the next's beginning. Off by default; an armed host trades throughput for it.


@pytest.mark.parametrize(
    "cooldown, last_end, now, expected",
    [
        (0.0, 100.0, 100.0, 0.0),  # disabled -> never waits, even right after a job
        (30.0, 0.0, 500.0, 0.0),  # no prior job this process (0.0 sentinel) -> first job never waits
        (30.0, 100.0, 140.0, 0.0),  # 40s already elapsed while queued -> rest is done, no wait
        (30.0, 100.0, 110.0, 20.0),  # only 10s elapsed -> wait the 20s remainder, not a fresh 30s
    ],
)
def test_job_cooldown_remaining_is_only_the_unspent_rest(monkeypatch, cooldown, last_end, now, expected):
    monkeypatch.setattr(srv, "JOB_COOLDOWN_SEC", cooldown)
    assert srv._job_cooldown_remaining_sec(last_end, now) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_an_armed_cooldown_rests_the_mesh_before_the_next_job(monkeypatch, clear_job_state):
    """A job dequeued right after the previous one finished waits out the cooldown before its
    device work, and says so durably. On base there is no cooldown, so no rest and no event."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)

    async def _free_gate(job_log_file):
        return ""  # device is fit; isolate this test from the health gate

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _free_gate)

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    monkeypatch.setattr(srv, "JOB_COOLDOWN_SEC", 0.5)
    monkeypatch.setattr(srv, "last_job_end_monotonic", srv.time.monotonic())  # a job just ended

    job = srv.Job(id="910", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    srv.jobs["910"] = job
    await srv.get_job_queue().put("910")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert job.status is not srv.JobStatus.QUEUED, "the job never dispatched through the cooldown"
        cooldowns = [f for kind, f in events if kind == "job_cooldown"]
        assert cooldowns, "an armed cooldown rested nothing and left no durable trace"
        assert cooldowns[0]["wait_sec"] > 0 and cooldowns[0]["job"] == "910"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_default_off_cooldown_never_delays_a_job(monkeypatch, clear_job_state):
    """Default OFF is exact: a job dequeued the instant after the previous one finished
    dispatches with no wait and no cooldown event, so no host loses throughput unasked."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)

    async def _free_gate(job_log_file):
        return ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _free_gate)

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    monkeypatch.setattr(srv, "JOB_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(srv, "last_job_end_monotonic", srv.time.monotonic())

    job = srv.Job(id="911", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    srv.jobs["911"] = job
    await srv.get_job_queue().put("911")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert job.status is not srv.JobStatus.QUEUED, "the job never dispatched"
        assert not [f for kind, f in events if kind == "job_cooldown"], "the disabled cooldown delayed a job"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


# --- per-owner submission burst cap ------------------------------------------
#
# The incident's LTX vbench sweep queued ten 32-chip jobs in ~90s — one every ~11s — then drained
# them back-to-back with no idle, which is what walked a tray off the bus. The cap refuses a burst
# at SUBMISSION, before it fills the queue, keyed per owner so one tenant's sweep cannot flood the
# shared device. Off by default; an armed host trades a tenant's submission rate for mesh headroom.


@pytest.mark.parametrize(
    "cap, window, recent, now, expect_allowed, expect_retry, expect_kept",
    [
        (
            0,
            60.0,
            [100.0, 101.0, 102.0],
            103.0,
            True,
            0.0,
            [100.0, 101.0, 102.0],
        ),  # disabled -> always allowed, nothing pruned
        (3, 60.0, [100.0, 105.0], 110.0, True, 0.0, [100.0, 105.0]),  # under the cap -> allowed
        (
            2,
            60.0,
            [100.0, 105.0],
            110.0,
            False,
            50.0,
            [100.0, 105.0],
        ),  # at the cap in-window -> refused, oldest frees in 50s
        (1, 60.0, [10.0], 100.0, True, 0.0, []),  # the only prior submit aged out of the window -> pruned, allowed
    ],
)
def test_job_burst_decision_prunes_the_window_and_gates_on_the_cap(
    monkeypatch, cap, window, recent, now, expect_allowed, expect_retry, expect_kept
):
    monkeypatch.setattr(srv, "JOB_BURST_MAX", cap)
    monkeypatch.setattr(srv, "JOB_BURST_WINDOW_SEC", window)
    allowed, retry_after, kept = srv._job_burst_decision(recent, now)
    assert allowed is expect_allowed
    assert retry_after == pytest.approx(expect_retry)
    assert kept == expect_kept


@pytest.mark.asyncio
async def test_an_armed_burst_cap_refuses_one_owners_flood_but_not_another(monkeypatch, clear_job_state):
    """With the cap armed, an owner's submits past the cap inside the window are refused at the
    submit tool with a clear owner-facing reason and a durable event; a second owner is untouched
    because the cap is per-owner. On base the cap is unwired, so the flood is admitted."""
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setattr(srv, "job_log_dir", None)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    monkeypatch.setattr(srv, "JOB_BURST_MAX", 2, raising=False)
    monkeypatch.setattr(srv, "JOB_BURST_WINDOW_SEC", 60.0, raising=False)

    mcp = srv.create_mcp_server()

    async def _submit(owner):
        # The owner is derived from the peer uid now, not sent — vary it at that seam.
        monkeypatch.setattr(srv, "submitting_owner", lambda: owner)
        out = await mcp.call_tool("tt_device_job_run_bg", {"params": {"workspace": "/tmp", "command": "echo hi"}})
        return json.loads(out.content[0].text)

    first = await _submit("sweeper")
    second = await _submit("sweeper")
    assert "job_id" in first and "job_id" in second, "the first two in-cap submits were not admitted"

    third = await _submit("sweeper")
    assert "error" in third, f"the 3rd submit past a cap of 2 was admitted: {third}"
    assert "burst cap" in third["error"], third
    assert third["retry_after_sec"] > 0, "a refused submit must say when a slot frees"

    refused = [f for kind, f in events if kind == "job_burst_refused"]
    assert (
        refused and refused[0]["owner"] == "sweeper" and refused[0]["cap"] == 2
    ), "the refusal left no durable trace naming the owner and cap"

    other = await _submit("bystander")
    assert "job_id" in other, f"the cap is global, not per-owner — a second owner was refused: {other}"


@pytest.mark.asyncio
async def test_the_default_off_burst_cap_admits_every_submit(monkeypatch, clear_job_state):
    """Default OFF is exact: a rapid burst from one owner is admitted with no refusal event and no
    per-owner state accumulated, so no host loses admission headroom unasked."""
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setattr(srv, "job_log_dir", None)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    monkeypatch.setattr(srv, "JOB_BURST_MAX", 0, raising=False)
    monkeypatch.setattr(srv, "owner_submit_times", {}, raising=False)

    mcp = srv.create_mcp_server()
    for _ in range(5):
        out = await mcp.call_tool(
            "tt_device_job_run_bg", {"params": {"owner": "sweeper", "workspace": "/tmp", "command": "echo hi"}}
        )
        assert "job_id" in json.loads(out.content[0].text), "the disabled cap refused a submit"

    assert not [f for kind, f in events if kind == "job_burst_refused"], "the disabled cap refused a burst"
    assert srv.owner_submit_times == {}, "the disabled cap accumulated per-owner state it should not touch"


@pytest.mark.asyncio
async def test_exec_refuses_the_device_the_broker_is_working_on(monkeypatch):
    """tt_device_exec runs its command on the device DIRECTLY — no queue, no runner. It
    asked the same tenant-jobs-only question, so it was the one path that could put a
    tenant's command on a mesh mid-galaxy-reset, or on one the broker had flagged dirty."""
    _free_device_lock(monkeypatch)
    mcp = srv.create_mcp_server()

    async def call():
        out = await mcp.call_tool(
            "tt_device_exec",
            {"params": {"owner": "tenant", "command": "echo touched-the-device"}},
        )
        return json.loads(out.content[0].text)

    async with srv._device_op("health-gate/post-job"):
        srv._set_device_op_detail("device reset: tt-smi -glx_reset (~60s)")
        held = await call()

    srv._mark_device_dirty("job 482 ended killed (exit -15)")
    try:
        dirty = await call()
    finally:
        srv._clear_device_dirty(verified=True)

    assert "error" in held, f"exec ran a command during a galaxy reset: {held}"
    assert "glx_reset" in held["error"]
    assert "touched-the-device" not in json.dumps(held)

    assert "error" in dirty, f"exec ran a command on a device flagged dirty: {dirty}"
    assert "dirty" in dirty["error"]


@pytest.mark.asyncio
async def test_exec_refuses_a_chip_off_the_bus(monkeypatch):
    """A chip off the bus sets no in-memory flag — device_dirty stays False — so the op/dirty
    predicate waves exec through and its command runs directly on a mesh whose 0xFFFFFFFF reads
    hang the host CPU that issues them. exec is a tenant device-touching path exactly like the
    job runner, which already refuses this; exec must take the same live-probe verdict and
    refuse at the door, not only on a flag it happens to have set in time."""
    _free_device_lock(monkeypatch)
    srv.jobs.clear()
    _chip(pci.SYSFS_CLASS_DIR, 0, 100)
    _chip(pci.SYSFS_CLASS_DIR, 1, heartbeat.ALL_ONES)  # chip 1 off the bus, no flag set
    mcp = srv.create_mcp_server()

    out = await mcp.call_tool(
        "tt_device_exec",
        {"params": {"owner": "tenant", "command": "echo touched-the-device"}},
    )
    res = json.loads(out.content[0].text)

    assert "error" in res, f"exec ran a command onto a chip off the bus: {res}"
    assert "bus" in res["error"].lower(), res["error"]
    assert "touched-the-device" not in json.dumps(res), "exec executed the command on the wedged mesh"


@pytest.mark.asyncio
async def test_exec_force_runs_a_diagnostic_alongside_a_foreign_job(monkeypatch):
    """Triaging a hung job means triaging the job that WEDGED the shared device — usually
    another tenant's. Exec refuses a foreign-owned run by default, but force lets a read-only
    diagnostic run alongside it, and the foreign owner is on the record."""
    _free_device_lock(monkeypatch)
    srv.jobs.clear()
    srv.jobs["001"] = srv.Job(
        id="001", owner="other", workspace="/tmp", command="hung", queued_at="t", status=srv.JobStatus.RUNNING
    )
    mcp = srv.create_mcp_server()

    async def call(force):
        out = await mcp.call_tool(
            "tt_device_exec",
            {"params": {"owner": "tenant", "command": "echo triaged", "force": force}},
        )
        return json.loads(out.content[0].text)

    try:
        refused = await call(False)
        forced = await call(True)
    finally:
        srv.jobs.clear()

    assert "error" in refused and "Device busy" in refused["error"]
    assert "triaged" not in json.dumps(refused)
    assert "error" not in forced, f"force did not run the diagnostic: {forced}"
    assert "triaged" in forced["stdout"]


@pytest.mark.asyncio
async def test_exec_timeout_kills_the_command_it_gave_up_on(monkeypatch, tmp_path):
    """A triage that hangs must not become a second stuck process on the very mesh it was
    meant to inspect — the timeout kills the whole tree, not just the shell."""
    _free_device_lock(monkeypatch)
    srv.jobs.clear()
    marker = tmp_path / "exec_orphan"
    logged = []
    monkeypatch.setattr(srv, "write_action_log", lambda owner, cmd, rt, status, ec: logged.append(status))
    mcp = srv.create_mcp_server()

    out = await mcp.call_tool(
        "tt_device_exec",
        {"params": {"owner": "t", "command": f"(sleep 30; touch {marker}) & wait", "timeout_sec": 1}},
    )
    res = json.loads(out.content[0].text)

    assert "timed out after 1 seconds" in res.get("error", ""), res
    # Recorded under its own name, not "failed" or nothing.
    assert logged == ["timeout"], logged
    await asyncio.sleep(0.5)
    assert not marker.exists(), "the timed-out exec left an orphan running on the device"


@pytest.mark.asyncio
async def test_exec_timeout_scope_routes_a_privsep_diagnostic(monkeypatch):
    """A privsep exec runs its tt-triage in a systemd scope; the pid the broker holds is the
    systemd-run wrapper, not the reparented payload. killpg on that pid reaps only the wrapper
    and orphans the hung triage onto the very mesh it was inspecting — the same wedge F6 fixed
    for jobs. The timeout must signal the SCOPE the exec spawned into, never killpg."""
    _free_device_lock(monkeypatch)
    srv.jobs.clear()

    scope_kills = []
    killpg_calls = []

    async def fake_scope(scope, grace_sec=srv.GRACEFUL_KILL_GRACE_SEC):
        scope_kills.append(scope)

    def fake_prefix(uid, *, env=None, unit=None):
        # Stand in for an active-privsep prefix so _exec_impl takes the scoped spawn path.
        return ["systemd-run", "--scope", f"--unit={unit}"]

    class _HangingProc:
        pid = 4242

        async def communicate(self):
            await asyncio.sleep(10)  # outlast timeout_sec so wait_for gives up
            return (b"", b"")

    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _HangingProc()

    monkeypatch.setattr(srv, "privsep_prefix_for", fake_prefix)
    monkeypatch.setattr(srv, "_terminate_scope", fake_scope)
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(srv.os, "killpg", lambda *a, **k: killpg_calls.append(a))
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)

    mcp = srv.create_mcp_server()
    out = await mcp.call_tool(
        "tt_device_exec",
        {"params": {"owner": "t", "command": "tt-triage", "timeout_sec": 1}},
    )
    res = json.loads(out.content[0].text)

    assert "timed out" in res.get("error", ""), res
    # The wedge is killpg on the wrapper pid; the reap must go to the scope instead.
    assert not killpg_calls, "a privsep exec must be reaped by its scope, not killpg on the wrapper"
    assert len(scope_kills) == 1, scope_kills
    # The reaped scope is exactly the unique ttdev-exec unit the exec spawned into.
    spawned_unit = captured["argv"][2][len("--unit=") :]
    assert scope_kills == [spawned_unit], (scope_kills, spawned_unit)
    assert spawned_unit.startswith(srv.EXEC_SCOPE_PREFIX), spawned_unit


@pytest.mark.asyncio
async def test_a_blocked_device_is_not_reported_idle_to_the_submitter(monkeypatch, clear_job_state):
    """What the incident actually looked like from outside: the broker logged
    "STARTING (DEVICE IDLE)" for a job that then waited eleven minutes behind a reset on a
    device it had itself flagged dirty. The job was never dispatched — but the line says it
    was, and a submitter told "starting" has no reason to read it any other way."""
    _free_device_lock(monkeypatch)
    monkeypatch.setattr(srv, "job_log_dir", None)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    mcp = srv.create_mcp_server()

    async def submit():
        out = await mcp.call_tool(
            "tt_device_job_run_bg",
            {"params": {"owner": "dsmith", "workspace": "/tmp", "command": "echo hi"}},
        )
        return json.loads(out.content[0].text)

    async with srv._device_op("health-gate/post-job"):
        srv._set_device_op_detail("health check: fabric traffic pass across all links (~45s)")
        srv._mark_device_dirty("job 482 ended killed (exit -15)")
        try:
            res = await submit()
        finally:
            srv._clear_device_dirty()

    assert res["status"] == "queued", (
        f"the broker told the submitter {res['status']!r} — {res['message']!r} — while it "
        f"held the device for a reset on a mesh it had flagged dirty"
    )
    assert "device idle" not in res["message"]


@pytest.mark.asyncio
async def test_runner_holds_a_job_off_a_chip_off_the_bus(monkeypatch, tmp_path, clear_job_state):
    """The gate is BLOCKING, not advisory. A chip off the bus leaves no in-memory flag —
    device_dirty stays False — so the runner's op/dirty checks wave the job through, and it
    would run its command onto a mesh whose 0xFFFFFFFF reads can hang the host CPU that
    issues them. Under the queue-never-refuse default the runner must HOLD such a job at the
    door — queued, its command never dispatched onto the wedge — until the device is fit (the
    recovery ladder resets it), not bounce it back to the submitter. The safety property is
    identical to the old fail-fast path: the command never runs on the wedge."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 0.02, raising=False)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_held_since", "")

    _chip(pci.SYSFS_CLASS_DIR, 0, 100)
    _chip(pci.SYSFS_CLASS_DIR, 1, heartbeat.ALL_ONES)  # chip 1 fell off the bus, no flag set

    # A held device keeps sweeping finished jobs through the hold — an already-expired one
    # proves the hold loop still runs cleanup_finished_jobs() each pass.
    stale = srv.Job(
        id="801", owner="tenant", workspace="/tmp", command="old", queued_at="t", status=srv.JobStatus.FAILED
    )
    stale.finished_at = "2000-01-01T00:00:00"
    srv.jobs["801"] = stale

    marker = tmp_path / "the_command_ran"
    job = srv.Job(id="902", owner="tenant", workspace="/tmp", command=f"touch {marker}", queued_at="t")
    srv.jobs["902"] = job
    await srv.get_job_queue().put("902")

    runner = asyncio.create_task(srv.job_runner())
    try:
        # It holds: the job stays queued and its command never runs on the wedged mesh.
        for _ in range(100):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert job.status is srv.JobStatus.QUEUED, (
            f"job 902 reached {job.status.value} — a chip off the bus must HOLD a tenant job "
            f"in the queue, not refuse it or dispatch it onto the wedge"
        )
        assert not marker.exists(), "the held job's command was executed on the wedged mesh"
        assert "801" not in srv.jobs, (
            "the hold path stopped sweeping finished jobs — memory grows unbounded while "
            "a degraded device holds every job"
        )
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_runner_records_a_held_job_in_the_health_timeline(monkeypatch, tmp_path, clear_job_state):
    """A chip off the bus with no flag set is HELD at the gate — but the hold lives only in
    the jobs list, in memory, and the drop set no dirty flag so no device_dirty snapshot
    fired for it either. Nothing durable said the device went bad. The gate must leave one
    `device_held` mark in the health journal so an incident can be reconstructed after a
    broker restart."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 0.02, raising=False)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_held_since", "")

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    _chip(pci.SYSFS_CLASS_DIR, 0, 100)
    _chip(pci.SYSFS_CLASS_DIR, 1, heartbeat.ALL_ONES)  # chip 1 off the bus, no flag set

    job = srv.Job(id="903", owner="tenant", workspace="/tmp", command="true", queued_at="t")
    srv.jobs["903"] = job
    await srv.get_job_queue().put("903")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(100):
            if [f for k, f in events if k == "device_held"]:
                break
            await asyncio.sleep(0.02)
        assert job.status is srv.JobStatus.QUEUED, job.status
        held = [f for k, f in events if k == "device_held"]
        assert held, "the gate held a job but left no device_held mark in the health timeline"
        assert "bus" in held[0]["reason"].lower(), held[0]
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_hold_mode_holds_a_degraded_device_then_dispatches_when_fit(monkeypatch, tmp_path, clear_job_state):
    """With TT_DEVICE_MCP_TENANT_HOLD=1 a degraded device HOLDS the tenant job instead of
    failing it: the job stays queued and its command never runs while the mesh is bad, a
    device_held mark lands, and the job dispatches — closed by a device_released mark — the
    instant the device is fit. This is G2's absolute hold, not the fail-fast bound."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD", "1")
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 0.02, raising=False)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_held_since", "")

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    _chip(pci.SYSFS_CLASS_DIR, 0, 100)
    _chip(pci.SYSFS_CLASS_DIR, 1, heartbeat.ALL_ONES)  # chip 1 off the bus -> degraded

    marker = tmp_path / "held_command_ran"
    job = srv.Job(id="960", owner="tenant", workspace="/tmp", command=f"touch {marker}", queued_at="t")
    srv.jobs["960"] = job
    await srv.get_job_queue().put("960")

    runner = asyncio.create_task(srv.job_runner())
    try:
        # It holds: while the device is degraded the job neither fails nor runs.
        await asyncio.sleep(0.2)
        assert (
            job.status is srv.JobStatus.QUEUED
        ), f"held job reached {job.status.value} — the gate failed it instead of holding it"
        assert not marker.exists(), "a held job ran its command onto the degraded device"
        assert any(k == "device_held" for k, _ in events), "the hold left no device_held mark in the health timeline"

        # The device recovers -> the hold lifts and the job dispatches.
        _chip(pci.SYSFS_CLASS_DIR, 1, 100)
        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert job.status in (
            srv.JobStatus.RUNNING,
            srv.JobStatus.COMPLETED,
        ), f"the device recovered but the held job stayed {job.status.value} — hold never lifted"
        assert any(
            k == "device_released" for k, _ in events
        ), "the device recovered but no device_released mark closed the hold"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


def test_hold_poll_sec_floors_values_that_would_defeat_the_governor():
    """TENANT_HOLD_POLL_SEC doubles as the reset governor, so a malformed env value must floor
    to a safe cadence, never disable it. inf/nan sleep forever and would hold a recovered
    device indefinitely — the exact freeze the hold loop exists to remove; a non-positive poll
    spins the re-check. All floor to a safe cadence; a good value passes through."""
    f = srv._hold_poll_sec_from_env
    assert f(None) == 60.0
    assert f("abc") == 60.0
    assert f("inf") == 60.0
    assert f("-inf") == 60.0
    assert f("nan") == 60.0
    assert f("0") >= 1.0
    assert f("-5") >= 1.0
    assert f("30") == 30.0


@pytest.mark.asyncio
async def test_hold_mode_self_heals_a_dirty_device_by_re_running_the_gate(monkeypatch, tmp_path, clear_job_state):
    """A dirty flag is cleared only by the gate's reset+verify, and while the runner is parked
    holding a job it never reaches its own gate. So the hold must re-run the gate — a passive
    re-check would leave a recovered-but-still-flagged device held forever and freeze the queue
    on hardware that is fine. The job holds while a gate pass cannot clean the device and
    dispatches the moment one does."""
    _free_device_lock(monkeypatch)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD", "1")
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 0.02, raising=False)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_held_since", "")
    # Isolate the dirty path: liveness is fine, so the only degradation is the dirty flag.
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    fsm_dirty(srv, "device dirty")

    # The gate cleans the device only once the hardware has settled; until then a reset cannot
    # recover it (the reset-did-not-recover case), so the flag persists and the job holds.
    recovered = {"on": False}

    async def fake_gate(_log):
        if recovered["on"]:
            fsm_healthy(srv)

    monkeypatch.setattr(srv, "_ensure_device_clean_for_next_job", fake_gate)

    marker = tmp_path / "dirty_command_ran"
    job = srv.Job(id="961", owner="tenant", workspace="/tmp", command=f"touch {marker}", queued_at="t")
    srv.jobs["961"] = job
    await srv.get_job_queue().put("961")

    runner = asyncio.create_task(srv.job_runner())
    try:
        # Held while a gate pass cannot clean the device.
        await asyncio.sleep(0.2)
        assert (
            job.status is srv.JobStatus.QUEUED
        ), f"held job reached {job.status.value} — it did not wait for the gate to recover"
        assert not marker.exists()

        # Hardware settles -> a gate pass clears the flag -> the hold lifts and dispatches.
        recovered["on"] = True
        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert job.status in (srv.JobStatus.RUNNING, srv.JobStatus.COMPLETED), (
            f"the gate recovered the device but the held job stayed {job.status.value} — the "
            f"hold re-checked a passive predicate the parked runner can never clear"
        )
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_dirty_flag_survives_a_reset_that_did_not_recover(monkeypatch, tmp_path):
    """Clearing the flag on a device that never came back is what made the next job's
    gate reset it all over again, and the one after that — the reset storm."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)

    async def never_recovers(indices, log):
        return False

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {}

    patch_recovery(monkeypatch, "_reset_and_verify_device", never_recovers)
    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0")  # isolate the reset/dirty path
    srv.recovery_mechanism.last_reset_monotonic = 0.0
    srv.recovery_mechanism.last_reset_failed = False

    srv._mark_device_dirty("job crashed")
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert srv.fsm.state is not ServerState.HEALTHY, "an unrecovered device was marked clean"


# --- the erratum-1431 discriminator ------------------------------------------


def test_previous_boot_bus_locks_counts_only_the_boot_that_just_ended():
    """Both theories for these reboots produce an identical EX-unit watchdog record, so the
    firmware's account cannot separate them. Only AMD erratum 1431 needs a bus lock — a
    crash whose boot recorded ZERO bus locks cannot be 1431. That makes this count the
    discriminator, and it must not bleed across boots."""
    log = health.HEALTH_DIR / health.BUSLOCK_FILE
    log.write_text(
        "     1.000000000            999      ls_locks.bus_lock\n"  # an older boot
        "#BOOT old-boot-id 2026-07-13T00:00:00Z\n"
        "     1.000000000              4      ls_locks.bus_lock\n"  # the boot that just ended
        "     2.000000000             38      ls_locks.bus_lock\n"
    )
    assert health.previous_boot_bus_locks() == 42, "counted the wrong boot"


def test_previous_boot_bus_locks_zero_is_reported_as_zero_not_missing():
    """Zero is the whole point: it REFUTES erratum 1431. It must never be confused with
    'the counter was not running', which proves nothing."""
    log = health.HEALTH_DIR / health.BUSLOCK_FILE
    log.write_text(
        "#BOOT b 2026-07-13T00:00:00Z\n"
        "     1.000000000              0      ls_locks.bus_lock\n"
        "     2.000000000              0      ls_locks.bus_lock\n"
    )
    assert health.previous_boot_bus_locks() == 0

    log.write_text("#BOOT b 2026-07-13T00:00:00Z\n")  # counter never ran
    assert health.previous_boot_bus_locks() is None


def test_mark_boot_separates_the_counts():
    health.mark_boot("boot-abc")
    assert "#BOOT boot-abc" in (health.HEALTH_DIR / health.BUSLOCK_FILE).read_text()


# --- a dead chip must kill whoever still has it mapped ------------------------
#
# A host was lost WITH the isolation in place: chip 28 died 8s into a fabric traffic pass, the
# endpoint was cut out of the kernel within 13s, and the host still took a fatal MCE 135s later.
# `pci remove` does not tear down an existing userspace mmap — the validator kept reading the
# dead BAR through its own page tables until a CPU core stalled.


@pytest.mark.asyncio
async def test_a_dead_chip_kills_the_processes_that_still_map_it(monkeypatch):
    from tt_device_mcp.device_holders import DeviceHolder

    srv.device_op_lock = None
    srv.isolated_chips = set()
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: True)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: "0000:46:00.0")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=4242, uid=1000)], complete=True)
    )

    killed = []
    monkeypatch.setattr(srv.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: killed.append(pid))

    class FakeProc:
        pid = 9999

    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", FakeProc())
    monkeypatch.setattr(srv, "current_process", None)

    await srv.sampler.isolate_dead_chips(["28"])

    assert 4242 in killed, "the tenant holding the device was not killed — its mapping outlives the endpoint"
    assert 9999 in killed, "the fabric check was not killed — it maps every chip and it killed a host twice"


# --- the hard timeout ceiling -------------------------------------------------


def test_max_timeout_is_25_minutes_and_is_a_hard_ceiling():
    assert srv.MAX_TIMEOUT_SEC == 1500


def test_hitting_the_ceiling_does_not_offer_a_bigger_number():
    """Below the cap, point at the knob. AT the cap there is no knob, and pretending otherwise
    just teaches the next agent to ask for one."""
    msg = srv.timeout_hint(srv.MAX_TIMEOUT_SEC)
    assert "HARD MAXIMUM" in msg
    assert "CANNOT BE RAISED" in msg
    assert "MOVE WORK OFF THE DEVICE, OR BREAK THE WORK INTO PIECES" in msg
    assert "IT IS ALWAYS POSSIBLE" in msg
    # It must NOT tell them to raise it.
    assert "re-run with a higher" not in msg

    below = srv.timeout_hint(600)
    assert "re-run with a higher" in below  # below the cap, the knob is real
    assert "1500" in below  # ...and the ceiling is stated


# --- the last recovery rung: an auto host reboot, off by default -----------------
#
# Bridge reset -> galaxy reset is the ladder today; when both fail the device sits flagged.
# The last rung is a warm host reboot, and it is the highest-risk action in the repo: it takes
# every tenant's work down with the box. So it fires only where opted in, never over a tenant,
# and rate-limited durably — a reboot the ledger did not record is one the next boot repeats.


def _ledger_dir(monkeypatch, tmp_path):
    """Point the auto-recovery ledger at tmp_path and nowhere real."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    # The ledger read/write itself lives in RecoveryMechanism now, which imported health_dir
    # into health.recovery.base's own namespace — a separate binding from server.py's.
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    return tmp_path


def test_auto_reboot_is_off_by_default(monkeypatch):
    """The one action that takes every tenant down with the box must never fire on a host that
    did not ask for it. Only TT_DEVICE_MCP_AUTO_REBOOT=1 enables it."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    assert srv._auto_reboot_enabled() is False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    assert srv._auto_reboot_enabled() is False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    assert srv._auto_reboot_enabled() is True


def test_governor_never_reboots_over_a_tenant(monkeypatch, tmp_path):
    """Absolute rule: a tenant on the device outranks every other signal. A reboot would abort
    their run — the exact interference this loop must never cause."""
    _ledger_dir(monkeypatch, tmp_path)
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=True)
    assert allowed is False
    assert "tenant" in why


def test_governor_blocks_a_reboot_inside_the_min_interval(monkeypatch, tmp_path):
    """The boot-loop guard. An escalation recorded 60s ago, with a 1h min interval, means the
    box just rebooted and came back still wedged — rebooting again is a loop, not a recovery.
    The interval is wall-clock, so it spans the reboot the record survived."""
    _ledger_dir(monkeypatch, tmp_path)
    srv.recovery_mechanism.record_auto_recovery("reboot", "first wedge", now_epoch=1000.0)
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=1060.0)
    assert allowed is False
    assert "boot-loop guard" in why


def test_governor_blocks_after_the_per_boot_cap(monkeypatch, tmp_path):
    """Even past the min interval, a single boot may escalate only so many times. The cap is
    counted against the running boot's id, and the record already sits at that id."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MAX_PER_BOOT", 1)
    srv.recovery_mechanism.record_auto_recovery("reboot", "wedge", now_epoch=1000.0)
    # now_epoch far enough past the interval that only the per-boot cap can block it
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=1000.0 + 4000)
    assert allowed is False
    assert "this boot" in why


def test_governor_allows_when_opted_in_idle_and_within_limits(monkeypatch, tmp_path):
    """The one path that says yes: no tenant, no prior escalation, clean ledger."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=False)
    assert allowed is True
    assert why == ""


def test_governor_fails_closed_when_the_ledger_is_unreadable(monkeypatch, tmp_path):
    """An unreadable ledger must NOT read as 'never rebooted' — that is precisely the guess
    that starts a loop. Any doubt about the durable record denies the reboot."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv.recovery_mechanism, "read_auto_recovery_ledger", lambda: None)
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=False)
    assert allowed is False
    assert "unreadable" in why


@pytest.mark.asyncio
async def test_auto_reboot_records_durably_before_it_fires(monkeypatch, tmp_path):
    """The ledger write is the rate limiter, so it must land on disk BEFORE the reboot request
    takes the process down — else the next boot cannot see this reboot already happened. The
    action also leaves a visible jobs-list row so the reboot is not a blank in the audit trail."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    patch_health_event(monkeypatch, lambda *a, **k: None)

    order = []
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: order.append("record") or True)
    monkeypatch.setattr(galaxy, "_fire_host_reboot", lambda: order.append("fire"))

    await srv.galaxy_recovery._auto_reboot_host(lambda _m: None, "reset failed twice")

    assert order == ["record", "fire"], "recorded the escalation before firing the reboot"
    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]reboot-request"]
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "reboot"


@pytest.mark.asyncio
async def test_auto_reboot_aborts_when_the_ledger_cannot_be_written(monkeypatch, tmp_path):
    """The durable record IS the rate limiter. If it cannot be persisted (full disk, read-only
    rootfs), the reboot must be ABORTED — an unrecorded reboot is one the next boot repeats, a
    loop. Firing anyway is the fail-open bug this guards against."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: False)

    fired = []
    monkeypatch.setattr(galaxy, "_fire_host_reboot", lambda: fired.append(True))

    await srv.galaxy_recovery._auto_reboot_host(lambda _m: None, "reset failed twice")

    assert fired == [], "must NOT reboot when the escalation could not be recorded"
    assert [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]reboot-request"] == []


def test_governor_fails_closed_on_a_record_with_no_timestamp(monkeypatch, tmp_path):
    """A newest ledger record whose timestamp is missing or unparseable must block, not wave the
    reboot through — the interval is the boot-loop guard, so a garbled stamp fails closed."""
    d = _ledger_dir(monkeypatch, tmp_path)
    (d / recovery_base.AUTO_RECOVERY_LEDGER).write_text('{"action": "reboot", "boot_id": "old"}\n')
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "new")  # even a fresh boot must not slip through
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=1e12)
    assert allowed is False
    assert "timestamp" in why


def test_record_auto_recovery_round_trips_through_the_durable_ledger(monkeypatch, tmp_path):
    """What the governor reads back is what the action wrote: action, boot id, and the
    wall-clock stamp the interval math depends on."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    srv.recovery_mechanism.record_auto_recovery("reboot", "wedge", now_epoch=1234.0)

    ledger = srv.recovery_mechanism.read_auto_recovery_ledger()
    assert len(ledger) == 1
    assert ledger[0]["action"] == "reboot"
    assert ledger[0]["boot_id"] == "boot-A"
    assert ledger[0]["at_epoch"] == 1234.0


def test_ledger_skips_a_corrupt_line_without_losing_the_rest(monkeypatch, tmp_path):
    """One garbled line must not blind the limiter to the real records around it — that would
    silently drop the rate limit."""
    d = _ledger_dir(monkeypatch, tmp_path)
    (d / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        '{"action": "reboot", "at_epoch": 1.0, "boot_id": "b"}\n'
        "this is not json\n"
        '{"action": "reboot", "at_epoch": 2.0, "boot_id": "b"}\n'
    )
    ledger = srv.recovery_mechanism.read_auto_recovery_ledger()
    assert [r["at_epoch"] for r in ledger] == [1.0, 2.0]


def test_ledger_absent_is_no_escalations_not_an_error(monkeypatch, tmp_path):
    """A host that has never auto-rebooted has no ledger file; that is an empty history, not the
    unreadable case that fails closed."""
    _ledger_dir(monkeypatch, tmp_path)
    assert srv.recovery_mechanism.read_auto_recovery_ledger() == []


# --- G5: the BMC power-cycle rung, above the warm reboot ----------------------


def test_auto_power_cycle_is_off_by_default(monkeypatch):
    """The final, most drastic rung must never fire on a host that did not ask for it, and its
    opt-in is separate from the reboot's: only TT_DEVICE_MCP_AUTO_POWER_CYCLE=1 enables it."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    assert srv._auto_power_cycle_enabled() is False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    assert srv._auto_power_cycle_enabled() is False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    assert srv._auto_power_cycle_enabled() is True


def test_a_host_without_ipmitool_serves_with_the_cold_rung_off(monkeypatch, tmp_path):
    """Armed means armed AND fireable. A box with no ipmitool cannot cold-cycle, so the rung reads
    OFF — and the broker still SERVES. Refusing to boot there leaves the host with no queue and no
    gating at all, every tenant back on bare metal, which is a worse outage than a ladder that
    stops one rung short. The shortfall is stated in the rung inventory, never assumed."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(
        privileges,
        "_LATCHED",
        {**privileges._PROBES, **dict.fromkeys(privileges._PROBES, True), "ipmitool": False, "ipmi_node": False},
    )

    assert (
        srv._auto_power_cycle_enabled() is False
    ), "a rung whose binary is absent must not report as armed — the router would keep choosing it"

    # Preflight skips every device capability when /dev/tenstorrent is empty (nothing to
    # arbitrate), so stage a device node or this asserts against the early return instead of the
    # rung — which is exactly how it passed on a device box and failed on CI.
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))

    fails, warns, _degrade = srv._preflight_required_capabilities(None)
    assert not any(
        "ipmitool" in f for f in fails
    ), "a missing ipmitool must never be a fatal preflight — that is the no-broker-at-all outage"
    assert any("ipmitool" in w for w in warns), "and it must still be said loudly"


def test_ipmitool_present_leaves_the_cold_rung_armed(monkeypatch):
    """The other half: where the binary exists the rung is armed by default, so a dropped ASIC on
    a host that cannot reach it any other way still has a terminating rung."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(privileges, "_LATCHED", dict.fromkeys(privileges._PROBES, True))
    assert srv._auto_power_cycle_enabled() is True


def test_power_cycle_escalates_past_a_recent_reboot(monkeypatch, tmp_path):
    """The escalation the shared interval must NOT block: a warm reboot fired minutes ago and did
    not clear a whole-bus wedge, so the broker must be free to escalate to the power cycle that
    will — without waiting out the full loop-guard interval. The guard is per severity: a weaker
    prior rung does not gate a stronger one."""
    d = _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    # A reboot fired 60s ago under the previous boot; the box came back still wedged.
    (d / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        '{"action": "reboot", "boot_id": "boot-A", "at_epoch": 1000.0}\n'
    )
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=1060.0)
    assert allowed is True, why


def test_power_cycle_does_not_escalate_past_a_recent_power_cycle(monkeypatch, tmp_path):
    """Each rung's own loop guard holds: a power cycle that just fired and did not clear the mesh
    will not clear it now, so a second one inside the interval is a loop, not a recovery."""
    d = _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    (d / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        '{"action": "power-cycle", "boot_id": "boot-A", "at_epoch": 1000.0}\n'
    )
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=1060.0)
    assert allowed is False
    assert "boot-loop guard" in why


def test_reboot_does_not_de_escalate_past_a_recent_power_cycle(monkeypatch, tmp_path):
    """A stronger rung that already fired blocks dropping back to a weaker one inside the interval:
    if a power cycle could not recover it, a warm reboot underneath it certainly will not."""
    d = _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    (d / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        '{"action": "power-cycle", "boot_id": "boot-A", "at_epoch": 1000.0}\n'
    )
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=1060.0)
    assert allowed is False
    assert "boot-loop guard" in why


def test_choose_escalation_prefers_reboot_then_power_cycle(monkeypatch, tmp_path):
    """With both rungs opted in and no reboot yet tried, the least-drastic one is chosen. Once a
    reboot has been tried under a previous boot and the box is back still wedged, the chooser
    escalates to the power cycle."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    # No reboot on record yet: reboot is the rung.
    assert galaxy._choose_recovery_escalation(**srv._host_escalation_kwargs()) == "reboot"
    # A reboot from the previous boot within the interval: escalate to the power cycle.
    monkeypatch.setattr(srv.recovery_mechanism, "_reboot_already_attempted", lambda **k: True)
    assert galaxy._choose_recovery_escalation(**srv._host_escalation_kwargs()) == "power-cycle"


def test_choose_escalation_power_cycle_alone_goes_straight_to_it(monkeypatch, tmp_path):
    """A host may opt into the power cycle without the reboot — e.g. it only ever sees whole-bus
    wedges a reboot cannot clear. With reboot off, the power cycle is the rung directly, with no
    reboot-first requirement to satisfy."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    assert galaxy._choose_recovery_escalation(**srv._host_escalation_kwargs()) == "power-cycle"


def test_choose_escalation_none_when_neither_opted_in(monkeypatch, tmp_path):
    """Both rungs off (the default): no host-level escalation is offered, and the ladder leaves the
    device flagged exactly as it did before either rung existed."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    assert galaxy._choose_recovery_escalation(**srv._host_escalation_kwargs()) is None


def test_host_escalation_for_drop_sends_all_off_bus_to_the_cold_rung(monkeypatch, tmp_path):
    """A whole-bus drop (present==0) is the one wedge a warm reboot cannot clear: it does not
    power-cycle the Galaxy UBBs, so dropped ASICs stay off across it. So an all-off-bus mesh never
    takes the reboot rung — power-cycle when opted in, else no rung + reboot_blocked so the caller
    holds loudly. A partial drop (a chip still on the bus) keeps the ordinary reboot-first ladder."""
    _ledger_dir(monkeypatch, tmp_path)
    # Reboot opted in, power cycle NOT: an all-off-bus drop BLOCKS the reboot (it cannot re-enumerate).
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    assert galaxy._host_escalation_for_drop(off_bus=32, expected=32, **srv._host_escalation_kwargs()) == (None, True)
    # A partial drop keeps the reboot rung — some chips are on the bus, a reboot may re-enumerate them.
    assert galaxy._host_escalation_for_drop(off_bus=31, expected=32, **srv._host_escalation_kwargs()) == (
        "reboot",
        False,
    )
    # Power cycle opted in: an all-off-bus drop skips straight to the cold rung, no useless reboot first.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    assert galaxy._host_escalation_for_drop(off_bus=32, expected=32, **srv._host_escalation_kwargs()) == (
        "power-cycle",
        False,
    )
    # A single-chip host has no UBB re-enumeration failure mode: the ordinary ladder applies.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    assert galaxy._host_escalation_for_drop(off_bus=1, expected=1, **srv._host_escalation_kwargs()) == ("reboot", False)


def test_host_escalation_for_drop_routes_a_futile_reboot_to_the_cold_rung(monkeypatch, tmp_path):
    """A PARTIAL drop the caller has proven a warm reboot cannot recover — a reset that regressed
    the mesh off the bus, or hard-exited leaving chips off it — is routed like a whole-bus drop:
    the reboot is skipped for the cold rung when opted in, else no rung + reboot_blocked to hold
    loudly. Without the flag the same partial drop keeps the ordinary reboot-first ladder."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    # 20/32 off-bus is a partial drop: the ordinary ladder warm-reboots it.
    assert galaxy._host_escalation_for_drop(off_bus=20, expected=32, **srv._host_escalation_kwargs()) == (
        "reboot",
        False,
    )
    # warm_reboot_futile blocks that useless reboot; with no cold rung opted in the caller must hold.
    assert galaxy._host_escalation_for_drop(
        off_bus=20, expected=32, warm_reboot_futile=True, **srv._host_escalation_kwargs()
    ) == (None, True)
    # Cold rung opted in: skip straight to it, never the reboot that cannot re-enumerate the drop.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    assert galaxy._host_escalation_for_drop(
        off_bus=20, expected=32, warm_reboot_futile=True, **srv._host_escalation_kwargs()
    ) == ("power-cycle", False)
    # A single-chip host has no UBB re-enumeration failure mode: the flag does not apply.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    assert galaxy._host_escalation_for_drop(
        off_bus=1, expected=1, warm_reboot_futile=True, **srv._host_escalation_kwargs()
    ) == ("reboot", False)


def test_an_unconfigured_host_still_has_a_terminating_ladder(monkeypatch, tmp_path):
    """The rungs are ON unless switched off, so a host nobody configured still climbs to something
    that can actually recover a dropped ASIC. This is the regression guard for the failure the
    defaults exist to prevent: with both rungs off, a partial drop a warm reboot cannot clear finds
    no rung at all and the device is HELD forever — every gentler rung is already spent (the
    per-chip bridge rung cannot reach a chip whose bridge left with it, and the tray rung exists
    only on a Galaxy), so 'hold' means 'hold until a human notices'.

    Deletes the vars rather than setting them: the suite-wide fixture pins both OFF so no test
    fires a real reboot, and this is the one test whose subject IS the unset default. It only
    calls the pure chooser, so nothing can fire."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_REBOOT", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", raising=False)

    assert srv._auto_reboot_enabled(), "the warm-reboot rung must be armed on an unconfigured host"
    assert srv._auto_power_cycle_enabled(), "the cold rung must be armed on an unconfigured host"
    # Gentlest-first still holds: an ordinary partial drop takes the reboot, not the power cycle.
    assert galaxy._host_escalation_for_drop(off_bus=1, expected=8, **srv._host_escalation_kwargs()) == ("reboot", False)
    # The drop that used to strand the queue: reboot proven futile, so climb — never (None, True).
    assert galaxy._host_escalation_for_drop(
        off_bus=3, expected=8, warm_reboot_futile=True, **srv._host_escalation_kwargs()
    ) == ("power-cycle", False)
    assert galaxy._host_escalation_for_drop(off_bus=8, expected=8, **srv._host_escalation_kwargs()) == (
        "power-cycle",
        False,
    )


@pytest.mark.asyncio
async def test_auto_power_cycle_records_durably_before_it_fires(monkeypatch, tmp_path):
    """The ledger write is the rate limiter, so it must land on disk BEFORE the power cycle pulls
    the box down — else the next boot cannot see this escalation already happened. The action also
    leaves its own visible jobs-list row so the power cycle is not a blank in the audit trail."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    patch_health_event(monkeypatch, lambda *a, **k: None)

    order = []
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: order.append("record") or True)
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: order.append("fire"))

    await srv._auto_power_cycle_host(lambda _m: None, "reset and reboot could not recover it")

    assert order == ["record", "fire"], "recorded the escalation before pulling power"
    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]power-cycle-request"]
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "power-cycle"


@pytest.mark.asyncio
async def test_auto_power_cycle_aborts_when_the_ledger_cannot_be_written(monkeypatch, tmp_path):
    """The durable record IS the rate limiter. If it cannot be persisted, the power cycle must be
    ABORTED — an unrecorded escalation is one the next boot repeats, a loop. Firing anyway is the
    fail-open bug this guards against."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: False)

    fired = []
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: fired.append(True))

    await srv._auto_power_cycle_host(lambda _m: None, "reset and reboot could not recover it")

    assert fired == [], "must NOT power-cycle when the escalation could not be recorded"
    assert [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]power-cycle-request"] == []


def test_reboot_already_attempted_ignores_this_boot_and_stale_records(monkeypatch, tmp_path):
    """The escalation trigger is a reboot from a PREVIOUS boot within the interval. A reboot record
    stamped with the current boot has not taken the box down yet; a reboot older than the interval
    is a different, already-recovered wedge. Neither is the 'reboot did not clear it' signal."""
    d = _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    d_ledger = d / recovery_base.AUTO_RECOVERY_LEDGER
    # This-boot reboot: box has not gone down for it yet.
    d_ledger.write_text('{"action": "reboot", "boot_id": "boot-B", "at_epoch": 1000.0}\n')
    assert srv.recovery_mechanism._reboot_already_attempted(now_epoch=1060.0) is False
    # Previous-boot reboot but older than the interval: a separate, recovered wedge.
    d_ledger.write_text('{"action": "reboot", "boot_id": "boot-A", "at_epoch": 1000.0}\n')
    assert srv.recovery_mechanism._reboot_already_attempted(now_epoch=1000.0 + 4000) is False
    # Previous-boot reboot within the interval: the escalation signal.
    assert srv.recovery_mechanism._reboot_already_attempted(now_epoch=1060.0) is True


def test_unreadable_boot_id_never_escalates_to_power_cycle(monkeypatch, tmp_path):
    """Fail-closed guard for a host whose /proc boot id cannot be read. Without a boot id we cannot
    prove a reboot crossed a boot, and the interval (weaker-rank record) and the per-boot cap (no
    boot id) both no-op — so if the chooser offered the power cycle here it would fire in the SAME
    boot as the reboot with NO loop guard at all. It must stay on the reboot instead."""
    d = _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "")  # unreadable
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    # A reboot recorded 60s ago; with no boot id this could be THIS boot's, not a crossed one.
    (d / recovery_base.AUTO_RECOVERY_LEDGER).write_text('{"action": "reboot", "boot_id": "", "at_epoch": 1000.0}\n')
    assert srv.recovery_mechanism._reboot_already_attempted(now_epoch=1060.0) is False
    assert galaxy._choose_recovery_escalation(**srv._host_escalation_kwargs()) == "reboot"
    # And the reboot's own interval guard still blocks a repeat, so nothing loops.
    allowed, why = srv.recovery_mechanism.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=1060.0)
    assert allowed is False
    assert "boot-loop guard" in why


def _no_workspace_activation(monkeypatch):
    """Run the bare command: these tests are about the reaper, not about venv activation."""
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", None))


# --- the hung reaper ---------------------------------------------------------
#
# A job wedged on the device holds it until its own timeout, which is sized for the slowest
# legitimate run. Every tenant queued behind it waits out that whole window for a job that
# will never produce another line. Silence is the only signal available from outside the
# process, and it is a proxy — hence the guard test below, which is the one that matters.


@pytest.mark.asyncio
async def test_a_silent_job_is_reaped_as_hung(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    # Speaks once, then goes quiet for far longer than the silence limit.
    job = srv.Job(id="901", owner="tenant", workspace="/tmp", command="echo working; sleep 60", queued_at="t")
    srv.jobs["901"] = job
    await srv.get_job_queue().put("901")

    runner = asyncio.create_task(srv.job_runner())
    try:
        # The reaper stamps the verdict before the process dies; wait for the runner to
        # finish tearing it down, or the buffers it drains are still in flight.
        for _ in range(300):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)
        assert (
            job.status is srv.JobStatus.HUNG
        ), f"a job silent for 60s ended as {job.status.value}; it holds the device the whole time"
        assert "working" in (job.output or ""), "output produced before the hang was lost"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_talking_job_is_never_reaped(monkeypatch, clear_job_state):
    """The guard. A job that keeps reporting is working, however slowly — killing it is the
    failure mode this feature can cause, and it is worse than the one it fixes."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    # Each line lands inside the limit, but the run as a whole outlasts it several times over.
    job = srv.Job(
        id="902",
        owner="tenant",
        workspace="/tmp",
        command="for i in 1 2 3 4 5 6 7 8; do echo tick $i; sleep 0.5; done",
        queued_at="t",
    )
    srv.jobs["902"] = job
    await srv.get_job_queue().put("902")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(400):
            if job.status not in (srv.JobStatus.QUEUED, srv.JobStatus.RUNNING):
                break
            await asyncio.sleep(0.05)
        assert job.status is srv.JobStatus.COMPLETED, f"a job reporting every 0.5s was reaped as {job.status.value}"
        assert "tick 8" in (job.output or ""), "job did not run to completion"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_hung_silence_of_zero_disables_the_reaper(monkeypatch, clear_job_state):
    """Every host must be able to turn this off: silence is a proxy, not proof."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    job = srv.Job(id="903", owner="tenant", workspace="/tmp", command="sleep 1.5; echo done", queued_at="t")
    srv.jobs["903"] = job
    await srv.get_job_queue().put("903")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.status not in (srv.JobStatus.QUEUED, srv.JobStatus.RUNNING):
                break
            await asyncio.sleep(0.05)
        assert job.status is srv.JobStatus.COMPLETED, f"reaper fired while disabled ({job.status.value})"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


# --- the doomed reaper ------------------------------------------------------
#
# The silence reaper above cannot see the worst case. When metal hits a dispatch timeout it
# declares the device unrecoverable and then unwinds, waiting the full per-operation timeout on
# each device in turn and printing at every one — eight devices at 180s is 24 minutes of a job
# that is talking steadily, is never silent, and is already dead. Job 970 held the device for
# exactly that, with the queue frozen behind it.


@pytest.mark.asyncio
async def test_a_job_that_reports_an_unrecoverable_device_is_reaped_while_still_talking(monkeypatch, clear_job_state):
    """The case silence misses: it keeps printing all the way down, so only the verdict in its
    own output identifies it as dead."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 3600)  # silence must not be what fires
    monkeypatch.setattr(srv, "DOOMED_GRACE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    # Metal's verdict, then the noisy unwind that keeps the silence clock reset.
    job = srv.Job(
        id="904",
        owner="tenant",
        workspace="/tmp",
        command=(
            "echo 'TT_THROW: TIMEOUT: device timeout, potential hang detected, "
            "the device is unrecoverable'; "
            "for i in $(seq 1 60); do echo unwinding device $i; sleep 0.5; done"
        ),
        queued_at="t",
    )
    srv.jobs["904"] = job
    await srv.get_job_queue().put("904")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)
        assert job.status is srv.JobStatus.HUNG, (
            f"a job that declared its device unrecoverable ended as {job.status.value}; "
            "it holds the device for its whole unwind"
        )
        assert "unrecoverable" in (job.error or "") + (
            job.output or ""
        ), "the runtime's verdict was lost, so nobody can tell why it was reaped"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_doomed_grace_lets_the_backtrace_land(monkeypatch, clear_job_state):
    """The grace is the whole reason this is not an instant kill: the lines after the verdict
    name the wedged cores, and that is usually the only evidence of what hung."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 3600)
    monkeypatch.setattr(srv, "DOOMED_GRACE_SEC", 2)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    job = srv.Job(
        id="905",
        owner="tenant",
        workspace="/tmp",
        command=(
            "echo 'Timeout detected (metal_context.cpp:778)'; "
            "sleep 0.3; echo 'waiting for physical cores to finish: 14-3, 14-2'; "
            "sleep 60"
        ),
        queued_at="t",
    )
    srv.jobs["905"] = job
    await srv.get_job_queue().put("905")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(400):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)
        assert job.status is srv.JobStatus.HUNG
        assert "14-3" in (job.output or "") + (job.error or ""), "reaped before the cores it wedged on were printed"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_doomed_grace_of_zero_disables_the_doomed_reaper(monkeypatch, clear_job_state):
    """Same escape hatch as the silence reaper: a host must be able to turn it off."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)
    monkeypatch.setattr(srv, "DOOMED_GRACE_SEC", 0)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    job = srv.Job(
        id="906",
        owner="tenant",
        workspace="/tmp",
        command=(
            "echo 'device timeout, potential hang detected, the device is " "unrecoverable'; sleep 1.5; echo done"
        ),
        queued_at="t",
    )
    srv.jobs["906"] = job
    await srv.get_job_queue().put("906")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.status not in (srv.JobStatus.QUEUED, srv.JobStatus.RUNNING):
                break
            await asyncio.sleep(0.05)
        assert job.status is srv.JobStatus.COMPLETED, f"doomed reaper fired while disabled ({job.status.value})"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


def test_a_hung_job_is_treated_as_a_wedge_risk():
    """We signalled it mid-flight, exactly like KILLED/TIMEOUT: the mesh must be verified
    before the next tenant gets the device."""
    assert srv._is_wedge_risk_exit(srv.JobStatus.HUNG, -15) is True
    assert srv._is_wedge_risk_exit(srv.JobStatus.COMPLETED, 0) is False


def test_a_signal_death_is_a_wedge_risk_under_either_convention():
    """Job 012 crashed with 134 (128+SIGABRT) on a mesh the previous job's kill had left
    wedged, and the gate called it a clean application failure and dispatched the next
    tenant onto it. waitpid says -N; a shell — and the job's own exit file, the only
    source for a re-adopted job — says 128+N. Both are the same dead process."""
    F = srv.JobStatus.FAILED
    assert srv._is_wedge_risk_exit(F, -6) is True, "waitpid SIGABRT"
    assert srv._is_wedge_risk_exit(F, 134) is True, "shell SIGABRT (128+6) — job 012"
    assert srv._is_wedge_risk_exit(F, 143) is True, "shell SIGTERM (128+15) — a reaped job"
    assert srv._is_wedge_risk_exit(F, 137) is True, "shell SIGKILL (128+9)"
    # An application's own failure is not a signal death: resetting on every pytest
    # assertion would reset the box all day and converge on nothing.
    assert srv._is_wedge_risk_exit(F, 1) is False, "pytest assertion failure"
    assert srv._is_wedge_risk_exit(F, 2) is False
    assert srv._is_wedge_risk_exit(srv.JobStatus.COMPLETED, 0) is False


@pytest.mark.asyncio
async def test_exhausting_the_verify_budget_does_not_dispatch(monkeypatch, clear_job_state):
    """The bound on how many times the gate resets is not a bound on the gate. A device that
    will not come clean keeps its flag, and the job does not get it — the comment here used
    to promise the opposite, which is the kind of thing someone reads before deleting a check."""
    monkeypatch.setattr(srv, "TENANT_GATE_MAX_VERIFY", 2)
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "job 088 ended timeout")

    attempts = []

    async def never_recovers(job_log_file):
        attempts.append(1)  # a real reset+verify that fails to fix it

    monkeypatch.setattr(srv, "_ensure_device_clean_for_next_job", never_recovers)

    reason = await srv._await_device_free_for_tenant(None)

    assert len(attempts) == 2, "the reset budget was not honoured"
    assert reason, "the gate cleared a device that never verified — the job would run on the wedge"
    assert "unverified" in reason or "degraded" in reason


# --- a hang must be legible for workloads nobody instrumented -----------------
#
# The runtime has no always-on heartbeat, and a framework's progress printing covers that
# framework only — models/tt_dit prints per-step and per-load lines; a CCL test or an LLM
# prints nothing at all. Job 010 wedged after "Done creating persistent buffers" and its log
# was blank for the next 30 minutes. The broker holds the clock on every job's output no
# matter what the job is written in.


@pytest.mark.asyncio
async def test_a_silent_job_is_announced_in_its_own_log(monkeypatch, clear_job_state, tmp_path):
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 2)
    monkeypatch.setattr(srv, "HUNG_NOTICE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    log = tmp_path / "job.log"
    job = srv.Job(id="905", owner="tenant", workspace="/tmp", command="echo working; sleep 60", queued_at="t")
    job.log_file = str(log)
    srv.jobs["905"] = job
    await srv.get_job_queue().put("905")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)
        text = log.read_text()
        assert "no output for" in text, "the log just stops: nothing distinguishes a wedged job from a slow one"
        assert "terminating this job as hung" in text, "reaped without saying so in the log"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_silence_is_announced_even_when_the_reaper_is_disabled(monkeypatch, clear_job_state, tmp_path):
    """Visibility and reaping are separate decisions. A host that will not let the broker kill
    its jobs still needs to see which of them stopped talking."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)  # reaper off
    monkeypatch.setattr(srv, "HUNG_NOTICE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    log = tmp_path / "job2.log"
    job = srv.Job(id="906", owner="tenant", workspace="/tmp", command="echo working; sleep 2.5", queued_at="t")
    job.log_file = str(log)
    srv.jobs["906"] = job
    await srv.get_job_queue().put("906")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)
        assert job.status is srv.JobStatus.COMPLETED, f"reaper fired while disabled ({job.status.value})"
        text = log.read_text()
        assert "no output for" in text, "silence went unannounced because the reaper was off"
        assert "reaping as hung" not in text, "threatened a kill the broker will never make"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_talking_job_is_never_announced_silent(monkeypatch, clear_job_state, tmp_path):
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)
    monkeypatch.setattr(srv, "HUNG_NOTICE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    log = tmp_path / "job3.log"
    job = srv.Job(
        id="907",
        owner="tenant",
        workspace="/tmp",
        command="for i in 1 2 3 4 5 6; do echo tick $i; sleep 0.3; done",
        queued_at="t",
    )
    job.log_file = str(log)
    srv.jobs["907"] = job
    await srv.get_job_queue().put("907")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)
        assert "no output for" not in log.read_text(), "called a job that never stopped talking silent"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


# --- an unverified device is not a fit device ---------------------------------
#
# `verified` was write-only telemetry: it reached health_event and nothing else, so the
# dispatch predicate could not tell a clear a full snapshot+fabric pass stood behind from
# one that never touched silicon. Dropping the flag ends the resetting, not the doubt.


def test_an_unverified_clear_holds_the_device(monkeypatch):
    """The audit's leak: dirty device, fabric check SKIPPED (rc 77 / unavailable), enum+ARC
    healthy. A wedged eth core leaves sysfs perfectly healthy, so nothing else catches it."""
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "job 088 ended timeout")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    srv._clear_device_dirty(verified=False, why="gate/pre-job: enum+ARC healthy, fabric unverified")

    reason = srv._device_unavailable_for_tenant()
    assert reason, "a device nothing verified was handed to a tenant"
    assert "unverified" in reason


def test_a_verified_clear_releases_the_device(monkeypatch):
    """The hold lifts on proof, not on elapsed time."""
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "an earlier pass could not tell")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")

    assert srv._device_unavailable_for_tenant() == "", "a fully verified device stayed held"


def test_switched_off_verification_does_not_brick_the_host(monkeypatch):
    """A host with no device nodes, or an operator who set health checks to 0, will never
    verify anything. Holding there shuts the door once and never reopens it — that protects
    no silicon, it just ends the box."""
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "job 088 ended timeout")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    srv._clear_device_dirty_unverified("health checks disabled", holds=False)

    assert srv.fsm.state is ServerState.HEALTHY
    assert srv._device_unavailable_for_tenant() == ""


def test_a_gate_that_errored_holds(monkeypatch):
    """ "We tried and could not tell" is exactly the case the hold exists for."""
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "job 088 ended timeout")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    srv._clear_device_dirty_unverified("pre-job gate error: boom")

    assert "gate error" in srv._device_unavailable_for_tenant()


def test_a_clean_device_is_not_held_by_an_unverified_clear(monkeypatch):
    """Nothing was ever wrong: an unverified clear on an undirty device invents no doubt."""
    monkeypatch.setattr(srv, "device_op_active", "")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    srv._clear_device_dirty(verified=False, why="foreign holder present: someone")

    assert srv._device_unavailable_for_tenant() == ""


# --- an admission check that errors fails closed ------------------------------
#
# A raise out of _await_device_free_for_tenant used to be swallowed as "fit", so the job was
# dispatched onto exactly the device nobody could verify. Now the job waits at the door while the
# check is retried; a check that keeps raising holds the device instead.


async def _run_one_job_through_the_gate(job, *, until):
    """Queue ``job``, run the real job_runner, and stop once ``until()`` holds (or ~6 s pass)."""
    srv.jobs[job.id] = job
    await srv.get_job_queue().put(job.id)
    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(300):
            if until():
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_an_admission_check_error_retries_instead_of_dispatching(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "ADMISSION_GATE_RETRY_SEC", (0.01, 0.01))

    job = srv.Job(id="912", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    status_at_call = []

    async def _flaky_gate(job_log_file):
        status_at_call.append(job.status)
        if len(status_at_call) == 1:
            raise RuntimeError("boom")
        return ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _flaky_gate)

    await _run_one_job_through_the_gate(job, until=lambda: job.status is not srv.JobStatus.QUEUED)

    assert len(status_at_call) == 2, "the errored admission check was not retried"
    assert status_at_call[1] is srv.JobStatus.QUEUED, "the job was dispatched before the check answered"
    assert job.status is not srv.JobStatus.QUEUED, "a check that recovered on retry never dispatched the job"
    assert srv.fsm.state is ServerState.HEALTHY, "a single transient error held the device"


@pytest.mark.asyncio
async def test_three_admission_check_errors_in_a_row_hold_the_device(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "ADMISSION_GATE_RETRY_SEC", (0.01, 0.01))
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD", "0")  # refuse at the door: the job ends, fast

    calls = []

    async def _broken_gate(job_log_file):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _broken_gate)
    job = srv.Job(id="913", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")

    await _run_one_job_through_the_gate(job, until=lambda: job.finished_at is not None)

    assert len(calls) == srv.ADMISSION_GATE_MAX_ERRORS == 3
    assert job.started_at is None, "a job was dispatched past an admission check that never answered"
    assert job.status is srv.JobStatus.FAILED
    assert "boom" in (job.error or "")
    assert srv.fsm.state is ServerState.RECOVERING
    assert srv.fsm.record.why == "gate_error"
    assert srv.fsm.record.why in srv.GENERIC_ESCALATE_WHYS
    assert not srv.fsm.record.dirty, "a hold, not a dirty mark: the failing gate is not asked to reset"
    assert srv._device_degraded_for_tenant(), "the next job would be admitted onto the unverified device"


@pytest.mark.asyncio
async def test_a_held_job_stays_held_while_the_admission_check_keeps_erroring(monkeypatch, clear_job_state):
    """Default hold mode: the hold's own re-check errors too, and the job stays queued rather than
    being refused or dispatched."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "ADMISSION_GATE_RETRY_SEC", (0.01, 0.01))
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 0.01)

    calls = []

    async def _broken_gate(job_log_file):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _broken_gate)
    job = srv.Job(id="914", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")

    await _run_one_job_through_the_gate(job, until=lambda: len(calls) >= 3 * srv.ADMISSION_GATE_MAX_ERRORS)

    assert len(calls) >= 3 * srv.ADMISSION_GATE_MAX_ERRORS, "the hold stopped re-checking the device"
    assert job.status is srv.JobStatus.QUEUED, "a held job was refused or dispatched on a gate error"
    assert job.started_at is None
    assert srv.fsm.record.why == "gate_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["off_bus", "job_killed"])
async def test_admission_check_errors_leave_an_open_episode_alone(monkeypatch, clear_job_state, why):
    """An open episode already shuts the door. Overwriting it with gate_error would drop the reset
    a dirty device is owed, or the self-heal lift an off-bus hold arms."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "ADMISSION_GATE_RETRY_SEC", (0.01, 0.01))
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD", "0")
    fsm_dirty(srv, "an earlier finding", why=why)
    dirty_before = srv.fsm.record.dirty

    async def _broken_gate(job_log_file):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _broken_gate)
    job = srv.Job(id="915", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")

    await _run_one_job_through_the_gate(job, until=lambda: job.finished_at is not None)

    assert job.started_at is None and job.status is srv.JobStatus.FAILED
    assert srv.fsm.record.why == why
    assert srv.fsm.record.dirty == dirty_before


@pytest.mark.asyncio
async def test_a_job_cancelled_during_admission_retries_never_dispatches(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "ADMISSION_GATE_RETRY_SEC", (0.01, 0.01))

    job = srv.Job(id="916", owner="tenant", workspace="/tmp", command="echo ran", queued_at="t")
    calls = []

    async def _gate_errors_then_the_job_is_killed(job_log_file):
        calls.append(1)
        if len(calls) == 1:
            job.status = srv.JobStatus.KILLED  # a cancel landing during the retry pause
            raise RuntimeError("boom")
        return ""

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _gate_errors_then_the_job_is_killed)

    await _run_one_job_through_the_gate(job, until=lambda: srv.get_job_queue()._unfinished_tasks == 0)

    assert job.status is srv.JobStatus.KILLED
    assert job.started_at is None, "a job cancelled at the door was dispatched anyway"


# --- a malformed fsm.json must degrade, never poison the admission path --------
#
# ServerFsm._load coerces job/since/detail to their expected shapes: a hand-edited or truncated
# file that leaves the wrong type in one of them must never reach dict()/strptime()/f-string
# formatting downstream still holding that type, or the tenant predicate and the health payload
# raise instead of answering — and every job would then wait out job_runner's admission retries
# and end on a gate_error hold.


def test_a_non_dict_job_on_disk_never_poisons_the_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "device_op_active", "")
    path = tmp_path / "fsm.json"
    path.write_text(
        json.dumps(
            {
                "state": "recovering",
                "why": "job_killed",
                "since": "2026-08-17T00:00:00Z",
                "job": "not-a-dict",
            }
        )
    )
    monkeypatch.setattr(srv, "fsm", ServerFsm(path))

    assert srv.fsm.record.job == {}
    assert srv._device_unavailable_for_tenant() != ""
    assert srv._health_payload()["fsm_state"] == "recovering"


def test_a_non_str_since_on_disk_never_poisons_the_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "device_op_active", "")
    path = tmp_path / "fsm.json"
    path.write_text(json.dumps({"state": "recovering", "why": "job_killed", "since": 12345}))
    monkeypatch.setattr(srv, "fsm", ServerFsm(path))

    assert isinstance(srv.fsm.record.since, str)
    assert srv._device_unavailable_for_tenant() != ""
    assert srv._health_payload()["fsm_state"] == "recovering"


def test_a_non_str_detail_on_disk_never_poisons_the_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "device_op_active", "")
    path = tmp_path / "fsm.json"
    path.write_text(
        json.dumps(
            {
                "state": "recovering",
                "why": "job_killed",
                "since": "2026-08-17T00:00:00Z",
                "detail": {"nested": "object"},
            }
        )
    )
    monkeypatch.setattr(srv, "fsm", ServerFsm(path))

    assert isinstance(srv.fsm.record.detail, str)
    reason = srv._device_unavailable_for_tenant()
    assert reason != ""
    assert srv._health_payload()["fsm_state"] == "recovering"


# --- the runtime outranks our blind checks ------------------------------------
#
# Live on blx02: every job died on "Timed out while waiting for active ethernet core 27-25
# to become active again. Try resetting the board", and the gate cleared the device
# "verified healthy (incl. fabric)" after each one. Both checks the broker owns are
# structurally blind to that fault — tt-smi reads a wedged eth core as fine, and the fabric
# validator runs with a second erisc disabled — so their agreement is silence, not evidence.


def test_a_runtime_reported_fault_holds_the_device(monkeypatch):
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_fault_reported", "")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)

    srv._mark_device_reported_fault("job 031 waiting for active ethernet core")

    reason = srv._device_unavailable_for_tenant()
    assert reason, "the runtime said the mesh is wedged and the next tenant got it anyway"
    assert "ethernet core" in reason, f"the hold does not say what is wrong: {reason}"
    assert srv.device_fault_reported, "the fault was not recorded as outranking our checks"


def test_a_verified_clear_does_not_retire_a_reported_fault(monkeypatch):
    """The checks that would 'verify' it are the ones that cannot see it. Only a reset."""
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "job 031 eth core")
    monkeypatch.setattr(srv, "device_fault_reported", "job 031 waiting for active ethernet core")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")

    assert srv.device_fault_reported, "a snapshot+fabric pass overruled the runtime itself"
    assert "runtime reported" in srv._device_unavailable_for_tenant()


def test_a_reset_retires_a_reported_fault(monkeypatch):
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_fault_reported", "job 031 waiting for active ethernet core")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    srv._clear_device_reported_fault("gate/post-job: reset recovered the mesh")

    assert srv.device_fault_reported == ""
    assert srv._device_unavailable_for_tenant() == ""


# --- an operator's reset must not become two ----------------------------------
#
# A reset SIGKILLs the running job to take the device. That death landed as an ordinary
# wedge-risk exit, flagged the device, and the post-job gate — finding chips still coming
# back from the reset — fired a second one. Live at 02:08: "device marked dirty: job 039
# ended failed (exit -9)" then "device needs reset — failed health check", ten seconds after
# the operator's own reset. Every extra reset of 32 ASICs is another chance to drop a chip.


@pytest.mark.asyncio
async def test_a_job_killed_by_a_reset_does_not_flag_the_device(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "reset_killed_job_ids", set())
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)

    job = srv.Job(id="039", owner="tenant", workspace="/tmp", command="sleep 30", queued_at="t")
    srv.jobs["039"] = job
    await srv.get_job_queue().put("039")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(200):  # let it reach RUNNING
            if job.status is srv.JobStatus.RUNNING and job.pid:
                break
            await asyncio.sleep(0.05)
        assert job.status is srv.JobStatus.RUNNING

        srv._note_reset_killed_job()  # a reset takes the device ...
        assert "039" in srv.reset_killed_job_ids
        import os as _os
        import signal as _signal

        _os.killpg(job.pid, _signal.SIGKILL)  # ... by SIGKILLing the job

        for _ in range(200):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)

        assert (
            srv.fsm.state is ServerState.HEALTHY
        ), "the reset's own kill flagged the device, so the operator's one reset becomes two"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_job_that_dies_on_its_own_still_flags_the_device(monkeypatch, clear_job_state):
    """The guard: only deaths WE caused are exempt. A job that crashes on its own still
    leaves a mesh nobody has checked."""
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "reset_killed_job_ids", set())
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)

    job = srv.Job(id="040", owner="tenant", workspace="/tmp", command="sleep 30", queued_at="t")
    srv.jobs["040"] = job
    await srv.get_job_queue().put("040")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(200):
            if job.status is srv.JobStatus.RUNNING and job.pid:
                break
            await asyncio.sleep(0.05)
        import os as _os
        import signal as _signal

        _os.killpg(job.pid, _signal.SIGKILL)  # nobody noted a reset

        for _ in range(200):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)

        assert srv.fsm.state is not ServerState.HEALTHY, "a job that died by a signal left the mesh unchecked"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


# --- a single-chip wedge must NOT reboot the box (it self-heals) --------------
#
# A single/few-chip drop is an active-eth-core freeze that clears itself in 15-89 min (verified
# chips 17/30/20/13/4). Rebooting it kills every in-flight tenant/agent for a fault that heals on
# its own; the holder-kill already covers the dead-BAR MCE path. Only a mass drop earns the reboot.


def _reach_reboot_rung(monkeypatch, tmp_path, n_present):
    """Drive the gate to the escalation point: N chips present, reset fails, prior pass failed,
    auto-reboot armed, no tenant."""
    for i in range(n_present):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)  # baseline mesh = 32

    async def never_recovers(indices, log):
        return False

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "wedged"}}

    patch_recovery(monkeypatch, "_reset_and_verify_device", never_recovers)
    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    # No reset is cycling at the escalation point by default; a test that models an in-flight reset
    # overrides this. Stubbed so the gate never shells out to the real systemctl on the CI host.
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda action, *, tenant_active: (True, "ok"))
    srv.recovery_mechanism.last_reset_failed = True  # a prior pass already failed => was_failing
    # Its cooldown has elapsed, so this pass resets again. Must be phrased relative to
    # time.monotonic(): a bare 0.0 only reads as "long ago" on a box whose monotonic clock
    # is already large. On a freshly-booted runner (monotonic ~90s) 0.0 lands *inside* the
    # 600s cooldown, so the gate holds off instead of resetting and every reboot/reset
    # assertion below silently fails. Anchor to now so it is past the cooldown on any uptime.
    srv.recovery_mechanism.last_reset_monotonic = srv.time.monotonic() - 100_000.0
    reboots = {"n": 0}

    async def fake_reboot(log, reason):
        reboots["n"] += 1

    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", fake_reboot)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    return reboots


@pytest.mark.asyncio
async def test_a_mass_drop_still_reboots(monkeypatch, tmp_path, clear_job_state):
    """The other half: a mass drop self-heal does NOT clear, so the reboot must still fire."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)
    # Only 1 of 32 chips answers — the mesh is gone.
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert reboots["n"] == 1, "a mass drop must still escalate to a reboot"


@pytest.mark.asyncio
async def test_gate_all_off_bus_holds_loudly_never_reboots(monkeypatch, tmp_path, clear_job_state):
    """The gate's whole-bus fix: every chip off the bus (present==0) with the /dev nodes still
    present is a drop a warm reboot cannot re-enumerate (a 6U-Galaxy reboot does not power-cycle the
    UBBs). With only the reboot opted in the gate fires NO rung — it emits the loud power-cycle-
    required event and keeps holding. Fails on base, which warm-reboots the box uselessly."""
    events = []
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)  # 32 /dev nodes: past F1's empty check
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {})  # no chip answers: all 32 off the bus
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert reboots["n"] == 0, "an all-off-bus mesh must never warm-reboot — the reboot cannot re-enumerate it"
    assert (
        "all_chips_off_bus_power_cycle_required" in events
    ), "the whole-bus wedge must emit the loud actionable event and hold"


@pytest.mark.asyncio
async def test_gate_all_off_bus_power_cycles_when_opted_in(monkeypatch, tmp_path, clear_job_state):
    """The other branch: with the chassis power cycle opted in, an all-off-bus mesh skips the reboot
    (which cannot re-enumerate it) and fires the cold rung directly."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    power_cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        power_cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert (
        power_cycles["n"] == 1 and reboots["n"] == 0
    ), "an all-off-bus mesh with the cold rung opted in must power-cycle, not reboot"


@pytest.mark.asyncio
async def test_a_present_mesh_wedge_climbs_to_reboot_at_the_gate(monkeypatch, tmp_path, clear_job_state):
    """The GATE path must progress like the idle-relift path already does: a PRESENT mesh (every chip
    on the bus, off_bus==0) that stays unhealthy after a failed reset is an eth/fabric wedge the reset
    could not clear and self-heal will not either — it must climb to the host reboot, not sit below
    the mass floor forever. Fails on base, whose gate suppresses any off_bus < floor
    (reboot_suppressed_below_mass_threshold), present mesh included — the indefinite hold seen on blx02."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)
    # Model blx02: a reset ran and the chips came back ON the bus (off_bus==0) but STILL unhealthy —
    # a fabric wedge. Disable the galaxy-reset floor so the reset runs and the flow reaches the reboot
    # block (the floor's present-mesh hold is a separate rung, exercised elsewhere).
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(32)})

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert reboots["n"] == 1, "a present-mesh wedge that survived the reset must climb to reboot, not hold"


@pytest.mark.asyncio
async def test_a_reset_still_cycling_is_not_read_as_a_mass_drop(monkeypatch, tmp_path, clear_job_state):
    """A reset that timed out is left cycling in its own scope for the next gate to adopt, and a
    reset takes every chip off the bus — so the heartbeat read shows all 32 off-bus, an apparent
    mass drop. The destructive rung must ask the scope, not that read: rebooting here yanks power
    from under a live reset for what may be a single-chip wedge. The sampler and the galaxy-reset
    floor already guard this same signal; the reboot floor was the one consumer that did not."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)
    srv.isolated_chips = set()
    # A reset in flight takes every chip off the bus: all 32 read all-ones, an apparent mass drop.
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 0xFFFFFFFF for i in range(32)})
    # ...but a reset scope is still cycling, so that "drop" is the reset's own doing, not a wedge.
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "tt-reset-1234.scope")

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert reboots["n"] == 0, "rebooted the box over a reset's own in-flight all-ones"


# --- a reset that made the mesh WORSE, or hard-failed, must climb to the cold rung, not the reboot -
#
# The incident: a galaxy reset the gate fired on an 8/32 off-bus drop exited rc=1 and inverted the
# mesh to 32/32 off-bus, then the ladder warm-rebooted it — a rung a 6U-Galaxy reboot cannot work
# (it does not power-cycle the UBBs, so the dropped ASICs stay off). F14 catches BOTH the regression
# (off_bus grew) and the hard exit and routes them to the cold rung, never the warm reboot.


def _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before, off_after):
    """The reboot-rung harness, but with a stateful heartbeat read and a reset that changes it: the
    mesh reads ``off_before`` off-bus until the reset fires, then ``off_after`` — a regression when
    off_after > off_before. Both are partial drops (< 32) so the whole-bus path does not fire."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)  # 32 /dev nodes: past F1's empty check
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    state = {"off": off_before}

    def heartbeats():
        present = 32 - state["off"]
        return {str(i): 100 + i for i in range(present)}

    async def regressing_reset(indices, log):
        state["off"] = off_after
        return False

    monkeypatch.setattr(srv, "read_heartbeats", heartbeats)
    patch_recovery(monkeypatch, "_reset_and_verify_device", regressing_reset)
    return reboots


@pytest.mark.asyncio
async def test_gate_reset_regression_holds_loudly_never_reboots(monkeypatch, tmp_path, clear_job_state):
    """A gate reset that grew the drop off the bus (20 -> 28 of 32) must not warm-reboot: the reboot
    cannot re-enumerate the dropped ASICs. With only the reboot opted in the gate fires NO rung — it
    flags the regression and names the cold rung, holding. Fails on base, which warm-reboots it."""
    events = []
    reboots = _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before=20, off_after=28)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert reboots["n"] == 0, "a reset that regressed the mesh off the bus must never warm-reboot"
    assert "reset_regressed_offbus" in events, "the regression must be flagged loudly"
    assert "reset_unrecoverable_power_cycle_required" in events, "the cold rung must be named for the held drop"


@pytest.mark.asyncio
async def test_gate_reset_regression_power_cycles_when_opted_in(monkeypatch, tmp_path, clear_job_state):
    """With the chassis power cycle opted in, a regressed drop skips straight to the cold rung — never
    a warm reboot first (the rung that cannot re-enumerate it). Base warm-reboots instead."""
    reboots = _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before=20, off_after=28)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    power_cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        power_cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert (
        power_cycles["n"] == 1 and reboots["n"] == 0
    ), "a regressed drop with the cold rung opted in must power-cycle, not reboot"


@pytest.mark.asyncio
async def test_gate_hard_failed_reset_holds_loudly_never_reboots(monkeypatch, tmp_path, clear_job_state):
    """A reset that EXITED non-zero (a real reset_done rc=1), leaving chips off the bus without
    growing the count, is also a warm-reboot-futile drop: the reset could not even run, and the
    off-bus ASICs stay off across a reboot. Route it to the cold rung, held. Base warm-reboots."""
    events = []
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    # 20/32 off-bus, unchanged across the reset — no regression, but the reset command hard-exited.
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(12)})

    async def hard_failing_reset(indices, log):
        srv.recovery_mechanism.last_reset_exit_nonzero = True
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", hard_failing_reset)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert reboots["n"] == 0, "a hard-failed reset leaving chips off the bus must never warm-reboot"
    assert "reset_unrecoverable_power_cycle_required" in events, "the cold rung must be named for the held drop"
    assert "reset_regressed_offbus" not in events, "an unchanged off-bus count is not a regression"


@pytest.mark.asyncio
async def test_a_tenant_arriving_before_the_reboot_decision_blocks_it(monkeypatch, tmp_path, clear_job_state):
    """The gate scans for foreign holders TWICE: once before it touches anything, and again right
    before the host rung — and the window between them is a whole galaxy reset wide. A tenant that
    arrived inside it must reach the governor as active and block the reboot: the highest-risk
    action in the repo does not get to take the box down over a run it merely started too late to
    see. Only the idle path's equivalent re-scan was covered."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})  # 31 of 32 off the bus
    _ledger_dir(monkeypatch, tmp_path)

    scans = {"n": 0}

    def holders():
        scans["n"] += 1
        if scans["n"] == 1:
            return HolderScan(holders=[], complete=True)  # nobody, at the top of the gate
        # A tenant opened the device while the reset ran.
        return HolderScan(holders=[DeviceHolder(pid=4242, uid=1000)], complete=True)

    monkeypatch.setattr(srv, "enumerate_device_holders", holders)
    # The REAL governor, so the re-scan's tenant verdict is what decides — _reach_reboot_rung's
    # blanket allow-everything stub would hide exactly the check under test.
    real_governor = type(srv.recovery_mechanism).auto_recovery_allowed
    seen = []

    def governor(action, *, tenant_active):
        seen.append(tenant_active)
        return real_governor(srv.recovery_mechanism, action, tenant_active=tenant_active)

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", governor)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert scans["n"] >= 2, "the gate must re-scan for holders before the host rung"
    assert seen == [True], "the tenant that arrived mid-pass must reach the governor as active"
    assert reboots["n"] == 0, "rebooted the box out from under a tenant that arrived mid-pass"


@pytest.mark.asyncio
async def test_the_gate_aborts_the_host_reboot_when_the_ledger_cannot_be_written(
    monkeypatch, tmp_path, clear_job_state
):
    """The durable ledger write IS the rate limiter, and it must land BEFORE the rung fires: a
    reboot no record survives is one the next boot repeats, which is a loop across boots. Driven
    through the GATE, because the gate's host rung fires inside escalate() now and nothing pinned
    that this ordering survived the move."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)  # reboot opted in, mass drop
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    # Drop the harness's counting stub so the real _auto_reboot_host — the ledger-then-fire
    # ordering under test — is what the gate reaches.
    monkeypatch.delattr(srv.galaxy_recovery, "_auto_reboot_host")

    recorded, fired = [], []
    monkeypatch.setattr(
        srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: recorded.append(a) or False
    )  # unwritable ledger
    monkeypatch.setattr(galaxy, "_fire_host_reboot", lambda: fired.append(True))

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert recorded, "the gate never reached the host rung, so this proves nothing"
    assert fired == [], "fired a reboot the ledger could not record — the next boot repeats it"


@pytest.mark.asyncio
async def test_a_per_target_gate_pass_never_reaches_down(monkeypatch, tmp_path, clear_job_state):
    """DOWN means the ladder RAN and could not help — the Galaxy idle ladder's terminal verdict,
    reached once per hold episode past the ceiling. A job-boundary gate pass is never that: the next
    gate, the idle relift and the deadline watchdog all still own the mesh. So a per-target host
    (n150/n300/loudbox — no tray rung, nothing above the reset) whose reset fails must still get
    that reset, and must come out RECOVERING rather than wedged in DOWN the first time one fails."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)
    resets = _count_galaxy_resets(monkeypatch)  # also opens a dirty episode
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})  # 31 of 32 off the bus
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "per-target")
    assert isinstance(srv.select_recovery(), PerTargetRecovery), "not the platform under test"

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 1, "a per-target host must still get its reset at the gate"
    assert (
        srv.fsm.state is ServerState.RECOVERING
    ), "a failed gate reset wedged a per-target host in DOWN — it has no ladder to have exhausted"


def test_reboot_floor_holds_single_chips_and_trays(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_REBOOT_MIN_DEAD_FRAC", raising=False)
    floor = srv._reboot_min_dead_chips(32)
    assert floor == 16  # half the mesh at the default 0.5
    assert 1 < floor and 8 < floor  # one dead chip, and a full 8-chip tray, both hold
    # A malformed fraction falls back rather than crashing the broker at start.
    monkeypatch.setenv("TT_DEVICE_MCP_REBOOT_MIN_DEAD_FRAC", "not-a-number")
    assert srv._reboot_min_dead_chips(32) == 16
    monkeypatch.setenv("TT_DEVICE_MCP_REBOOT_MIN_DEAD_FRAC", "0.25")
    assert srv._reboot_min_dead_chips(32) == 8


def test_watchdog_ping_gates_on_sampler_liveness(monkeypatch):
    """blx04's 37h hold: the telemetry sampler (which drives EVERY relift/escalation) stalled on a
    hung device read while the event loop kept serving and pinging the systemd watchdog — so a held
    device never escalated and nothing restarted the broker. The watchdog ping must track the
    SAMPLER's liveness, not just the event loop's. Fails on base, which has no sampler-stall gate."""
    # A fake monotonic clock: the real one can read below the stall window on a low-uptime runner,
    # which would put `now - (now - STALL)` on a NEGATIVE tick that the `> 0` guard reads as alive.
    clock = {"t": 10_000.0}
    monkeypatch.setattr(srv.time, "monotonic", lambda: clock["t"])
    # a fresh tick -> alive -> the ping is sent
    srv.sampler.tick()
    assert srv.sampler.is_stalled() is False
    # ticked, then stopped past the stall window -> stalled -> ping withheld -> systemd restarts
    clock["t"] += srv.sampler.STALL_SEC + 5
    assert srv.sampler.is_stalled() is True
    # never ticked (startup, before the first sample) must NOT read as stalled — else a false restart
    srv.sampler.last_tick = 0.0
    assert srv.sampler.is_stalled() is False


def test_auto_recovery_denial_is_journaled_loudly_once_per_episode(monkeypatch):
    """A blocked/exhausted escalation must be a durable, visible event, not a scrolling log line —
    else the ladder stopping (rate-limited / cascade exhausted) is silent. Deduped per hold episode
    so a held box does not re-journal it every gate pass; a tenant deferral (expected) is not flagged
    host_at_risk. Fails on base, which only _log's the denial."""
    events = []
    monkeypatch.setattr(recovery_base, "health_event", lambda k, **kw: events.append((k, kw)))
    monkeypatch.setattr(srv.recovery_mechanism, "_auto_recovery_denied_episode", "", raising=False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-07-28T22:00:00", raising=False)
    # a rate-limit / exhausted denial -> loud + host_at_risk (the ladder climbed as far as it can)
    srv.recovery_mechanism._journal_auto_recovery_denied(
        "reboot", "a reboot auto-recovery was 100s ago (< 3600s interval)"
    )
    assert events and events[-1][0] == "auto_recovery_denied" and events[-1][1]["host_at_risk"] is True
    # same episode again -> deduped, no new event
    n = len(events)
    srv.recovery_mechanism._journal_auto_recovery_denied("power-cycle", "already auto-escalated 2x this boot")
    assert len(events) == n, "must not re-journal a denial within the same hold episode"
    # a NEW episode, and a tenant deferral -> journals again, but NOT host_at_risk (expected wait)
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-07-28T23:00:00", raising=False)
    srv.recovery_mechanism._journal_auto_recovery_denied(
        "reboot", "a tenant holds the device; a reboot would destroy a running job"
    )
    assert events[-1][0] == "auto_recovery_denied" and events[-1][1]["host_at_risk"] is False


# --- the galaxy-reset floor must hold a single-chip wedge off the mesh-wide reset -------------
#
# The reboot floor above gates the host-reboot rung, which runs only AFTER a galaxy reset already
# failed. But the galaxy reset itself is the drop that inverts the mesh: one chip off the bus,
# one -glx_reset, and the other 31 go to 0xFFFFFFFF (measured on blx04). This floor gates the
# reset itself so a single/few-chip wedge holds and self-heals instead. OFF by default.


def _count_galaxy_resets(monkeypatch):
    """Reach the galaxy-reset rung with the isolated/fault globals cleared, counting reset calls."""
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    resets = {"n": 0}

    async def counting_reset(indices, log):
        resets["n"] += 1
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", counting_reset)
    return resets


@pytest.mark.asyncio
async def test_galaxy_reset_floor_holds_a_single_chip_wedge(monkeypatch, tmp_path, clear_job_state):
    """With the floor armed, a single-chip off-bus wedge HOLDS instead of firing the mesh-wide
    galaxy reset — the reset that turns one wedged chip into 31 at 0xFFFFFFFF."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    resets = _count_galaxy_resets(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "fired the galaxy reset on a single-chip wedge — the mesh-inverting drop"
    assert srv.fsm.state is not ServerState.HEALTHY, "a held wedge must keep the tenant door shut for the next job"


@pytest.mark.asyncio
async def test_galaxy_reset_floor_still_resets_a_mass_drop(monkeypatch, tmp_path, clear_job_state):
    """The other half: a mass drop self-heal does NOT clear, so the reset must still fire even
    with the floor armed."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})  # 1 of 32 => 31 off the bus
    resets = _count_galaxy_resets(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 1, "a mass drop must still reset — self-heal does not recover it"


# --- a chip GONE from the bus (not merely all-ones) must reach the gentle bridge reset too -----
#
# _isolate_dead_chips only tracks chips still enumerated at 0xFFFFFFFF, so the surgical rung
# recovers those but a chip that LEFT THE BUS ENTIRELY skips it and strands at the below-floor
# hold until a human reset (blx04, bus 0x84 gone). Opted in, a single/few-chip gone drop is
# routed to that same rung; reset_chip_via_bridge finds its bridge by secondary bus. OFF by default.


@pytest.mark.asyncio
async def test_a_gone_chip_is_routed_to_the_gentle_bridge_reset_when_opted_in(monkeypatch, tmp_path, clear_job_state):
    """A chip gone from sysfs, below the galaxy-reset floor, is routed to the per-chip bridge
    reset instead of stranding at the hold — the blx04 gap that needed a human reset."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)  # expected = 32
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    # 31 chips tick; chip "31" has left the bus entirely — absent from sysfs, not reading all-ones.
    # Point PCI_DEVICES_DIR at a dir without its node so it reads as gone regardless of the host's
    # real chips (whose BDFs could otherwise collide with the fake ones above).
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    monkeypatch.setattr(srv, "GONE_CHIP_CONFIRM_SETTLE_SEC", 0)  # no real settle in a unit test
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")  # opt in

    seen = {"n": 0, "isolated": None}

    async def recover(log):
        seen["n"] += 1
        seen["isolated"] = set(srv.isolated_chips)
        return False  # didn't come back -> the gate falls to the below-floor hold, no reset

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert seen["n"] == 1, "a gone chip below the floor must be routed to the per-chip bridge reset"
    assert seen["isolated"] == {"31"}, "the gone chip must be the one queued for recovery"


@pytest.mark.asyncio
async def test_a_stalled_but_enumerated_chip_is_not_treated_as_gone(monkeypatch, tmp_path, clear_job_state):
    """read_heartbeats omits a chip whose ARC read merely stalled the same way it omits one gone
    from the bus, and the two-sample confirm cannot tell them apart. A chip STILL ENUMERATED — its
    PCIe node present — must NOT be routed to the gone-chip bridge reset: its parent bridge still
    hosts live silicon a Secondary Bus Reset would knock off. Only a node that is actually absent
    is off the bus. It is held, not reset."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)  # expected = 32
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    # chip "31" is absent from both heartbeat reads (a mailbox stall) but STILL ENUMERATED: its
    # PCIe node is present in sysfs, so it has not left the bus.
    pci_dir = tmp_path / "pci"
    pci_dir.mkdir()
    (pci_dir / "0000:5f:00.0").mkdir()  # chip 31 (bus 0x40+31=0x5f) present
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", pci_dir)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    monkeypatch.setattr(srv, "GONE_CHIP_CONFIRM_SETTLE_SEC", 0)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")  # opt in

    seen = {"n": 0}

    async def recover(log):
        seen["n"] += 1
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert seen["n"] == 0, "an enumerated chip whose node is still present must not be SBR'd as gone"
    assert srv.fsm.state is not ServerState.HEALTHY, "the stalled chip must still hold the tenant door shut"


@pytest.mark.asyncio
async def test_a_chip_with_an_unresolved_pci_address_is_not_treated_as_gone(monkeypatch, tmp_path, clear_job_state):
    """chip_pci_bdf yields None when a chip's PCI address cannot be resolved, so device_pci_map may
    hold None against a chip index. Feeding that None to the node-present confirm must not raise: a
    crash there aborts the whole health gate, and the pre-job caller then clears the dirty flag and
    admits a tenant onto an unverified mesh. An unresolved address is fail-safe 'present' — the chip
    is held, never routed to a Secondary Bus Reset on a bridge we cannot even name."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)  # expected = 32
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    srv.device_pci_map["31"] = None  # its PCI address never resolved
    # chip "31" is absent from both heartbeat reads, so it reaches the node-present confirm.
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    monkeypatch.setattr(srv, "GONE_CHIP_CONFIRM_SETTLE_SEC", 0)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")  # opt in

    seen = {"n": 0}

    async def recover(log):
        seen["n"] += 1
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert seen["n"] == 0, "a chip with an unresolved PCI address must not be SBR'd as gone"
    assert srv.fsm.state is not ServerState.HEALTHY, "the unverifiable chip must still hold the tenant door shut"


@pytest.mark.asyncio
async def test_a_gone_chip_is_left_to_the_hold_by_default(monkeypatch, tmp_path, clear_job_state):
    """Default-safe: with the opt-in unset, a gone chip is NOT routed to a bridge reset — the
    broker fires no Secondary Bus Reset on a class of drop it has not validated live."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)  # default off

    seen = {"n": 0}

    async def recover(log):
        seen["n"] += 1
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert seen["n"] == 0, "routed a gone chip to a bridge reset with the opt-in off"
    assert srv.fsm.state is not ServerState.HEALTHY, "the gone chip must still hold the tenant door shut"


@pytest.mark.asyncio
async def test_a_mass_gone_drop_is_left_to_the_galaxy_reset(monkeypatch, tmp_path, clear_job_state):
    """A floor's worth of gone chips is a mass drop, not the single-chip case the gentle rung is
    for — a floor of Secondary Bus Resets is not the gentle path. Opted in, it is still left to
    the galaxy reset the floor lets fire at/above the mass threshold."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)  # 31 of 32 gone => at/above floor
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})
    monkeypatch.setattr(srv, "GONE_CHIP_CONFIRM_SETTLE_SEC", 0)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")

    seen = {"n": 0}

    async def recover(log):
        seen["n"] += 1
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)
    resets = {"n": 0}

    async def counting_reset(indices, log):
        resets["n"] += 1
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", counting_reset)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert seen["n"] == 0, "queued per-chip bridge resets for a mass drop"
    assert resets["n"] == 1, "a mass gone drop must still reach the galaxy reset"


@pytest.mark.asyncio
async def test_a_queued_gone_chip_is_unqueued_when_the_surgical_rung_is_skipped(monkeypatch, tmp_path, clear_job_state):
    """The gone-chip rung queues a chip into isolated_chips, then a foreign reset scope opens during
    the two-sample settle, so the surgical rung is skipped and the galaxy reset adopts that scope. The
    queued gone chip must be un-queued: left there, once the in-flight reset revives it, the next gate
    would fire a needless bridge reset on an already-recovered chip. A still-gone chip is re-queued."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)  # expected = 32
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    # chip "31" has left the bus entirely — absent from sysfs, not reading all-ones.
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    monkeypatch.setattr(srv, "GONE_CHIP_CONFIRM_SETTLE_SEC", 0)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")  # opt in

    # No scope when the gone-chip rung checks (it queues), but a foreign reset scope is live by the
    # time the surgical rung and the floor check run — modelling a reset opened during the settle.
    scope_calls = {"n": 0}

    def scope():
        scope_calls["n"] += 1
        return None if scope_calls["n"] == 1 else "ttdev-reset-999.scope"

    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", scope)

    seen = {"n": 0}

    async def recover(log):
        seen["n"] += 1
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    async def recovered(indices, log):
        return True  # the in-flight galaxy reset revives it

    patch_recovery(monkeypatch, "_reset_and_verify_device", recovered)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert seen["n"] == 0, "the surgical rung must be skipped while a reset scope is in flight"
    assert "31" not in srv.isolated_chips, (
        "a gone chip queued for the surgical rung must be un-queued when that rung is skipped, "
        "or the next gate bridge-resets an already-recovered chip"
    )


@pytest.mark.asyncio
async def test_galaxy_reset_floor_defaults_to_holding_a_single_chip(monkeypatch, tmp_path, clear_job_state):
    """Default-safe: with the floor UNSET, a single-chip wedge now HOLDS instead of galaxy-resetting.
    A single galaxy reset on one wedged chip is the measured way the whole mesh is lost, so the floor
    defaults to the mass-drop threshold rather than the old reset-any-unhealthy-device behavior."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    resets = _count_galaxy_resets(monkeypatch)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", raising=False)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "galaxy-reset a single-chip wedge by default — the mesh-inverting drop"
    assert reboots["n"] == 0, "rebooted the whole box for a single-chip wedge that self-heals"
    assert srv.fsm.state is not ServerState.HEALTHY, "the held wedge must keep the tenant door shut for the next job"


def test_galaxy_reset_floor_defaults_to_the_mass_floor_and_parses(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", raising=False)
    assert srv._galaxy_reset_min_dead_chips(32) == 16  # default-safe: the mass-drop floor
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "not-a-number")
    assert srv._galaxy_reset_min_dead_chips(32) == 16  # malformed falls back to the default
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "1.5")
    assert srv._galaxy_reset_min_dead_chips(32) == 16  # >1 is no valid fraction -> fail safe, not disable
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0")
    assert srv._galaxy_reset_min_dead_chips(32) is None  # 0 disables -> old reset-any behavior
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    assert srv._galaxy_reset_min_dead_chips(32) == 16
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.25")
    assert srv._galaxy_reset_min_dead_chips(32) == 8


def test_a_reset_frac_above_one_fails_safe_to_the_default_floor_not_disabled(monkeypatch):
    """A fraction can't exceed 1.0, so a value like 16 is a chip COUNT typed where the fraction
    belongs. Disabling the floor on it would silently restore the reset-any-unhealthy hazard the
    floor guards against, so it must fail safe to the default floor. Only 0/negative deliberately
    opts out."""
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "16")  # meant 16 chips, not 16x the mesh
    assert srv._galaxy_reset_min_dead_chips(32) == 16  # fail safe to the default floor
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "2")
    assert srv._galaxy_reset_min_dead_chips(32) == 16  # any >1 value, never disabled
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "inf")
    assert srv._galaxy_reset_min_dead_chips(32) == 16  # +inf is >1 -> fail safe
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "nan")
    assert srv._galaxy_reset_min_dead_chips(32) == 16  # NaN is malformed -> fail safe, never crash
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "-1")
    assert srv._galaxy_reset_min_dead_chips(32) is None  # negative stays the deliberate opt-out
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "-inf")
    assert srv._galaxy_reset_min_dead_chips(32) is None  # -inf stays the opt-out too


def _rung(monkeypatch, deps, **kw):
    """GalaxyRecovery.next_stage with the past-the-release-check defaults filled in; a test
    overrides only the axis it exercises. expected=32 derives the same mass-drop floor (16) for
    both the galaxy reset and the reboot rung the old raw galaxy_floor=16/reboot_floor=16
    defaults gave.

    ``host_escalation``/``reset_recovered`` are legacy test-only shorthands, not ``Evidence``
    fields (``Evidence`` deliberately has neither — see its own docstring): ``host_escalation``
    sets the auto-reboot/auto-power-cycle env opt-ins ``next_stage``'s own fold reads (mirroring
    ``_choose_recovery_escalation``, so "reboot" needs only TT_DEVICE_MCP_AUTO_REBOOT and
    "power-cycle" only TT_DEVICE_MCP_AUTO_POWER_CYCLE — the ledger read the fold also touches,
    ``reboot_already_attempted``, reads an empty per-test ledger by default); ``reset_recovered``
    maps onto ``last_action=STAGE_SMI_RESET`` with ``last_action_recovered`` set to the same
    bool, ``None`` staying the pre-action ladder."""
    host_escalation = kw.pop("host_escalation", "unset")
    if host_escalation != "unset":
        # Both rungs are armed by DEFAULT, so selecting one means explicitly disarming the other:
        # clearing the var would leave it on and the router would name the gentler rung instead.
        monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1" if host_escalation == "reboot" else "0")
        monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1" if host_escalation == "power-cycle" else "0")
    reset_recovered = kw.pop("reset_recovered", "unset")
    if reset_recovered is not None and reset_recovered != "unset":
        kw.setdefault("last_action", STAGE_SMI_RESET)
        kw.setdefault("last_action_recovered", reset_recovered)
    base = dict(
        off_bus=0,
        frozen_chips=0,
        expected=32,
        sbr_candidates=0,
        eth_frozen=False,
        cooling=False,
        scope_active=False,
        was_failing=False,
        healthy=False,
        fault_reported=False,
        fabric_ok=True,
        fabric_ran=True,
        dirty=False,
        fabric_forced=False,
        last_action=None,
        last_action_recovered=None,
        off_bus_before=None,
        reset_exit_nonzero=False,
        holding_fabric_unverified=False,
    )
    base.update(kw)
    return GalaxyRecovery(None, srv.recovery_mechanism, deps).next_stage(Evidence(**base))


def test_cascade_router_climbs_gentlest_first(monkeypatch, health_deps):
    """Before any action this pass (last_action is None), the router names the gate's pre-action
    ladder as one decision, gentlest-first. Each rung is reached only when the gentler one is
    inapplicable, so the ordering can be verified in isolation and cannot drift silently as the
    gate's inline branches change."""
    # A frozen active-eth-core heartbeat holds ahead of everything, even a mass drop — the gate's
    # first check, because a galaxy reset can neither see nor safely clear a stuck eth core.
    assert _rung(monkeypatch, health_deps, eth_frozen=True, off_bus=16) == WAIT
    # A reset already failed and its cooldown has not elapsed: sit it out even at a mass drop —
    # hammering a dead endpoint is what escalates a PCIe error to fatal.
    assert _rung(monkeypatch, health_deps, cooling=True, off_bus=16) == WAIT
    # A reset already cycling in its own scope: adopt and verify it, start nothing new — this
    # guards the same signal the SBR, galaxy-reset floor, and reboot rungs each guard inline.
    assert (
        _rung(
            monkeypatch,
            health_deps,
            scope_active=True,
            sbr_candidates=4,
            off_bus=20,
            was_failing=True,
            host_escalation="reboot",
        )
        == DEFER
    )
    # A single/few-chip drop with chips eligible for a per-chip Secondary Bus Reset gets that
    # surgical rung first — it spares the other chips and the fabric survives it. THIS is the blx04
    # gap: a below-floor gone chip must climb to the gentle reset, not strand at the hold.
    assert _rung(monkeypatch, health_deps, sbr_candidates=1, off_bus=1) == STAGE_BRIDGE_RESET
    # Below the galaxy floor with nothing to surgically reset: the per-tray BMC reset is the
    # next-gentlest rung the gate always attempts before holding (B5) — never a jump straight to
    # a mesh-wide reset over a single-chip wedge, whether or not a prior reset has failed.
    assert _rung(monkeypatch, health_deps, off_bus=1) == STAGE_UBB_TRAY
    assert _rung(monkeypatch, health_deps, off_bus=1, was_failing=True, host_escalation="reboot") == STAGE_UBB_TRAY
    # At or above the floor, before this pass's reset: the mesh-wide galaxy reset — whether or not a
    # prior cycle failed. The gate runs the reset again on a fresh pass; a failed prior reset plus an
    # elapsed cooldown earns another attempt, NOT a jump to the host rung.
    assert _rung(monkeypatch, health_deps, off_bus=16) == STAGE_SMI_RESET
    assert _rung(monkeypatch, health_deps, off_bus=16, was_failing=True, host_escalation="reboot") == STAGE_SMI_RESET
    # Floor disabled (operator opt-out to reset-any-unhealthy): any off-bus chip resets.
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0")
    assert _rung(monkeypatch, health_deps, off_bus=1) == STAGE_SMI_RESET


def test_cascade_router_keys_the_galaxy_floor_on_frozen_chips_too(monkeypatch, health_deps):
    """D1: the gate's own floor (server.py) keys the galaxy reset on every WEDGED chip —
    off_bus PLUS frozen — because an all-present, all-ARC-frozen mesh has nothing off the bus to
    invert and a reset is the only remedy. A router that compares off_bus alone reads that mesh as
    0/32 and names the below-floor rung while the gate resets it."""
    assert _rung(monkeypatch, health_deps, off_bus=0, frozen_chips=32) == STAGE_SMI_RESET
    # Below the floor even combined: a few frozen chips still self-heal, so the per-tray rung (the
    # gate always attempts it below the floor) still stands, never the mesh-wide reset.
    assert _rung(monkeypatch, health_deps, off_bus=0, frozen_chips=3) == STAGE_UBB_TRAY


def test_cascade_router_escalates_to_the_host_rung_only_once_reset_is_exhausted(monkeypatch, health_deps):
    """The host rungs (warm reboot, then BMC power cycle) are the highest-risk — each takes the whole
    box down. The gate reaches one only AFTER the galaxy reset it just ran also failed to recover
    (last_action is the galaxy reset, last_action_recovered is False), so the router names a host
    rung only in that post-reset phase, and only when a prior cycle failed too (was_failing), the
    drop is at/above the reboot floor, no reset is still cycling, AND that rung is opted in.
    Anything short holds the mesh degraded."""
    # Reset just ran and did not recover, a prior cycle failed too, mass drop, opted in: climb to the
    # chosen host rung, gentlest of the two.
    assert (
        _rung(monkeypatch, health_deps, reset_recovered=False, off_bus=16, was_failing=True, host_escalation="reboot")
        == STAGE_HOST_REBOOT
    )
    assert (
        _rung(
            monkeypatch, health_deps, reset_recovered=False, off_bus=16, was_failing=True, host_escalation="power-cycle"
        )
        == STAGE_POWER_CYCLE
    )
    # Post-reset, mass drop, but neither host rung opted in (the default): hold, never reboot.
    assert (
        _rung(monkeypatch, health_deps, reset_recovered=False, off_bus=16, was_failing=True, host_escalation=None)
        == WAIT
    )
    # Post-reset but only THIS pass's reset failed (was_failing False — no prior cycle): one failed
    # reset is not "clearly won't recover", so hold, do not reboot.
    assert (
        _rung(monkeypatch, health_deps, reset_recovered=False, off_bus=16, was_failing=False, host_escalation="reboot")
        == WAIT
    )
    # Post-reset below the reboot floor: a single/few-chip wedge self-heals and the holder-kill covers
    # the MCE path, so hold rather than reboot in-flight work away.
    assert (
        _rung(monkeypatch, health_deps, reset_recovered=False, off_bus=1, was_failing=True, host_escalation="reboot")
        == WAIT
    )
    # Post-reset but a reset re-opened its scope (a chip dropped mid-reset reads all-ones like a mass
    # drop): do not pull power from under a live reset — the next gate adopts and verifies it.
    assert (
        _rung(
            monkeypatch,
            health_deps,
            reset_recovered=False,
            off_bus=16,
            was_failing=True,
            host_escalation="reboot",
            scope_active=True,
        )
        == WAIT
    )
    # Reset NOT yet run this pass (pre-reset) at a mass drop: run the galaxy reset first, not the host
    # rung — even with a prior cycle failed.
    assert (
        _rung(monkeypatch, health_deps, reset_recovered=None, off_bus=16, was_failing=True, host_escalation="reboot")
        == STAGE_SMI_RESET
    )
    # A whole-bus drop after the failed reset, with only the reboot opted in and no power cycle to
    # fall back to: BLOCKED (B7), never WAIT — no opted-in rung can recover it, so the loud
    # fail-closed alert owns this, not the ordinary self-heal hold.
    assert (
        _rung(
            monkeypatch,
            health_deps,
            reset_recovered=False,
            off_bus=32,
            expected=32,
            was_failing=True,
            host_escalation="reboot",
        )
        == BLOCKED
    )


def test_cascade_router_expresses_release_and_the_fabric_unverified_hold(monkeypatch, health_deps):
    """B1+B2: before any action this pass, a wholly healthy fault-free mesh RELEASEs, and a fabric
    pass that ran but reached no verdict (a 77) on a dirty or multi-chip host holds
    fabric-unverified instead — never both, and never a reset for either."""
    # Every check agrees, no runtime fault: open the door.
    assert (
        _rung(monkeypatch, health_deps, healthy=True, fault_reported=False, fabric_ok=True, fabric_ran=True) == RELEASE
    )
    # Fabric ran but returned no verdict (77) on a DIRTY device: hold fabric-unverified, not
    # release and not reset — a 77 is not something a reset fixes.
    assert (
        _rung(monkeypatch, health_deps, healthy=True, fault_reported=False, fabric_ok=None, fabric_ran=True, dirty=True)
        == HOLD_FABRIC_UNVERIFIED
    )
    # Same 77, not dirty, but the pass was FORCED on a multi-chip host: still fabric-unverified.
    assert (
        _rung(
            monkeypatch,
            health_deps,
            healthy=True,
            fault_reported=False,
            fabric_ok=None,
            fabric_ran=True,
            dirty=False,
            fabric_forced=True,
            expected=32,
        )
        == HOLD_FABRIC_UNVERIFIED
    )
    # A mesh already held fabric-unverified with no fresh fabric pass stays held fabric-unverified
    assert (
        _rung(
            monkeypatch,
            health_deps,
            healthy=True,
            fault_reported=False,
            fabric_ok=None,
            fabric_ran=False,
            dirty=False,
            fabric_forced=False,
            holding_fabric_unverified=True,
            expected=32,
        )
        == HOLD_FABRIC_UNVERIFIED
    )
    # The untested arm R8 flags: a single-chip host (expected == 1) with an unresolved fabric pass
    # and no dirty flag is NOT held — enum+ARC are the whole ladder on a host with no UBB topology
    # to protect, so a forced-but-inconclusive pass there still releases.
    assert (
        _rung(
            monkeypatch,
            health_deps,
            healthy=True,
            fault_reported=False,
            fabric_ok=None,
            fabric_ran=True,
            dirty=False,
            fabric_forced=True,
            expected=1,
        )
        == RELEASE
    )


def test_cascade_router_climbs_the_present_mesh_post_reset_wedge_too(monkeypatch, health_deps):
    """D2: the gate also climbs to the host rung when a PRESENT mesh (off_bus==0) is still
    unhealthy after the reset it just ran — an eth/fabric wedge the reset could not clear, which
    self-heal will not either since it already survived the deepest reset (server.py:3148). A
    router that requires off_bus >= reboot_floor can never be satisfied by off_bus==0 (the reboot
    floor is floored at 2) and names WAIT while the gate reboots."""
    assert (
        _rung(monkeypatch, health_deps, reset_recovered=False, off_bus=0, was_failing=True, host_escalation="reboot")
        == STAGE_HOST_REBOOT
    )


@pytest.mark.asyncio
async def test_galaxy_reset_floor_retires_a_reported_fault_into_the_hold(monkeypatch, tmp_path, clear_job_state):
    """A runtime-reported fault must be retired into the hold, not left to force a reset on a
    later gate: left set, `if healthy and not device_fault_reported` would skip the clear once
    the wedge self-heals and reset an already-recovered mesh."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})
    resets = _count_galaxy_resets(monkeypatch)
    srv.device_fault_reported = "runtime reported: a frozen active-eth core"
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "held a single-chip wedge, so no galaxy reset"
    assert srv.device_fault_reported == "", "the reported fault must be retired into the hold"
    assert srv.fsm.state is not ServerState.HEALTHY, "the door still holds until a later gate verifies recovery"


# --- each rung's own release semantics, driven through the live gate -------------------------
#
# The router's unit tests above pin WHICH rung each degradation names; these drive the real
# _device_health_gate to prove what happens once that rung has actually run and recovered the mesh
# (or not) — the outcome half, which differs per rung and is what reopens the tenant door.


@pytest.mark.asyncio
async def test_a_recovered_bridge_reset_releases_without_the_mesh_wide_reset(monkeypatch, tmp_path, clear_job_state):
    """A chip isolated off the bus that comes back via the surgical per-chip bridge reset reopens
    the door without the mesh-wide reset ever running — the whole point of trying the gentle rung
    first."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)  # expected = 32
    resets = _count_galaxy_resets(monkeypatch)  # clears isolated_chips/device_fault_reported
    srv.isolated_chips = {"31"}
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}

    # The gate's own decision must see the isolated chip as unhealthy to reach the SBR rung; the
    # bridge reset's own re-verify (run_fabric=True) must then see a recovered mesh.
    verify_calls = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_calls["n"] += 1
        ok = verify_calls["n"] > 1
        return ok, {"snapshot": {"ok": ok, "detail": "" if ok else "chip 31 isolated"}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    async def recover(log):
        return True  # the bridge reset recovers it

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "the bridge reset pre-empts the mesh-wide galaxy reset"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_recovered_ubb_tray_reset_releases_without_the_mesh_wide_reset(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """A clean below-floor whole-tray drop, opted in, fires the per-tray BMC reset instead of
    holding, and a walk that verifies healthy reopens the door — the mesh-wide reset (which inverts
    a below-floor drop) never runs."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=24)
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    # chips 24-31 (tray 3) off the bus; 0-23 tick -> off_bus 8, below the floor of 16.
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(24)})
    resets = _count_galaxy_resets(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)

    verify_calls = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_calls["n"] += 1
        ok = verify_calls["n"] > 1
        return ok, {"snapshot": {"ok": ok, "detail": "" if ok else "chips 24-31 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: None, raising=False)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "the tray reset pre-empts the mesh-wide galaxy reset"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_recovered_mass_drop_reset_releases_without_reaching_the_host_rungs(
    monkeypatch, tmp_path, clear_job_state
):
    """A mass drop the mesh-wide galaxy reset DOES recover reopens the door there and then — the
    host rungs above it are for a reset that failed, and must not be reached by one that worked."""
    unhealthy = (False, {"snapshot": {"ok": False, "detail": "wedged"}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, unhealthy)  # its own reset() stub recovers
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})  # 1 of 32 => mass drop
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 1, "the mass drop must still reset"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_drop_that_comes_back_below_the_reboot_floor_holds(monkeypatch, tmp_path, clear_job_state):
    """A mass drop the galaxy reset fails to recover, but which comes back to only a single chip off
    the bus, is below the reboot floor: it holds rather than rebooting in-flight work away for a
    wedge that self-heals."""
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=1)  # pre-reset: mass drop
    calls = {"n": 0}

    def read_heartbeats_stub():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"0": 100}  # floor check: 31/32 off the bus
        return {str(i): 100 + i for i in range(31)}  # post-reset: only 1 off the bus

    monkeypatch.setattr(srv, "read_heartbeats", read_heartbeats_stub)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert reboots["n"] == 0, "a single-chip wedge below the reboot floor must never reboot"


@pytest.mark.asyncio
async def test_galaxy_reset_floor_holds_just_below_the_floor(monkeypatch, tmp_path, clear_job_state):
    """The `<` boundary: 15 of 32 off the bus is below the floor of 16, so it holds."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(17)})  # 15 off
    resets = _count_galaxy_resets(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "15 off-bus is below the floor of 16 and must hold"


@pytest.mark.asyncio
async def test_galaxy_reset_floor_resets_exactly_at_the_floor(monkeypatch, tmp_path, clear_job_state):
    """The other side of the boundary: 16 of 32 off the bus is AT the floor — a mass drop, resets."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(16)})  # 16 off
    resets = _count_galaxy_resets(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 1, "16 off-bus is at the floor and must reset, not hold"


@pytest.mark.asyncio
async def test_galaxy_reset_floor_holds_a_zero_off_bus_wedge_but_needs_eth_to_lift(
    monkeypatch, tmp_path, clear_job_state
):
    """off_bus==0 with the gate unhealthy is a fabric/eth wedge — every chip present and ARC-ticking,
    only the traffic pass failing — NOT a chip that left the bus. It must still HOLD (a galaxy reset of
    a present-but-wedged chip is the measured all-chip drop, the blx04 incident), but because the idle
    relift re-verifies read-only on enum+ARC and is blind to the fabric, this hold must require an
    advancing eth heartbeat to lift — never enum+ARC alone, or the relift dispatches the next tenant
    onto the still-wedged mesh."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(32)})  # 0 off-bus
    resets = _count_galaxy_resets(monkeypatch)  # clears device_fault_reported
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "a zero-off-bus wedge holds, not galaxy-resets — the blx04 incident"
    assert srv.fsm.state is not ServerState.HEALTHY, "the door holds for the next tenant"
    assert (
        srv.fsm.record.why == "eth_frozen"
    ), "a zero-off-bus (fabric/eth) hold must wait for an advancing eth heartbeat, not enum+ARC alone"


@pytest.mark.asyncio
async def test_galaxy_reset_floor_hold_with_a_reported_fault_requires_eth_to_lift(
    monkeypatch, tmp_path, clear_job_state
):
    """A genuine below-floor off-bus drop that ALSO carries a runtime-reported fault is a wedge the
    relift's enum+ARC read cannot confirm cleared: the chip returning to the bus is not proof the
    eth/fabric wedge that faulted is gone. So the hold must require a confirmed-advancing eth heartbeat
    to lift, never enum+ARC alone — otherwise the relift admits the next tenant onto a chip whose wedge
    only the eth reader can rule out."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})  # 1 off-bus
    resets = _count_galaxy_resets(monkeypatch)
    srv.device_fault_reported = "runtime reported: a frozen active-eth core"
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "1 off-bus is below the floor — held, not reset"
    assert srv.fsm.state is not ServerState.HEALTHY, "the door holds for the next tenant"
    assert (
        srv.fsm.record.why == "eth_frozen"
    ), "a fault-carrying hold must wait for an advancing eth heartbeat, not enum+ARC alone"


@pytest.mark.asyncio
async def test_galaxy_reset_floor_hold_without_a_fault_lifts_on_enum_arc(monkeypatch, tmp_path, clear_job_state):
    """The asymmetry's other side: a plain off-bus drop with no runtime fault is exactly what
    enum+ARC+sysfs can see return, so its hold must NOT demand the (possibly-unconfigured) eth reader —
    that is the whole point of the off-bus relift path. Only a fault-carrying hold escalates to
    needing eth."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=31)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(31)})  # 1 off-bus
    resets = _count_galaxy_resets(monkeypatch)  # clears device_fault_reported
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "1 off-bus is below the floor — held, not reset"
    assert srv.fsm.state is not ServerState.HEALTHY, "the door holds for the next tenant"
    assert (
        srv.fsm.record.why != "eth_frozen"
    ), "a no-fault off-bus hold lifts on enum+ARC — it must not strand on the eth reader"


# --- the eth-heartbeat pre-gate must spare a frozen chip the traffic probe -----
#
# The wedge is a frozen active-eth-core; the fabric traffic pass is what converts that
# frozen-but-present chip into an off-the-bus 0xFFFFFFFF drop. Reading the heartbeat (passive)
# first and routing a frozen core to HOLD means the harmful traffic pass never runs on it.


def _eth_gate_stubs(monkeypatch):
    monkeypatch.setattr(srv, "heartbeat_supported", lambda: False)  # skip the sysfs rung
    monkeypatch.setattr(srv.health_monitor, "verify_device_health", lambda expected, **k: (True, "32 chips"))


@pytest.mark.asyncio
async def test_a_frozen_eth_core_skips_the_traffic_probe(monkeypatch):
    _eth_gate_stubs(monkeypatch)
    traffic_ran = {"n": 0}

    async def frozen_eth(timeout_sec=60.0):
        return False, "a frozen active-eth core: core 27-25"

    async def traffic(timeout_sec=None):
        traffic_ran["n"] += 1
        return True, "links healthy"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", frozen_eth)
    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", traffic)

    ok, evidence = await srv.galaxy_recovery._verify_device(32, lambda m: None, run_fabric=True)

    assert ok is False, "a frozen eth core must report the mesh degraded"
    assert traffic_ran["n"] == 0, "ran --send-traffic across a frozen core — the iatrogenic drop"
    assert evidence["eth_heartbeat"]["ok"] is False


@pytest.mark.asyncio
async def test_live_eth_cores_still_run_the_traffic_probe(monkeypatch):
    """No frozen core => the traffic pass is safe (nothing to push off the bus) and still runs
    as the definitive fabric check."""
    _eth_gate_stubs(monkeypatch)
    traffic_ran = {"n": 0}

    async def live_eth(timeout_sec=60.0):
        return True, "all active-eth cores advancing"

    async def traffic(timeout_sec=None):
        traffic_ran["n"] += 1
        return True, "links healthy"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", live_eth)
    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", traffic)

    ok, _ = await srv.galaxy_recovery._verify_device(32, lambda m: None, run_fabric=True)

    assert ok is True
    assert traffic_ran["n"] == 1, "a healthy mesh should still get the definitive traffic pass"


@pytest.mark.asyncio
async def test_eth_heartbeat_unconfigured_is_a_noop(monkeypatch):
    """Default-off: with no command set the read skips and behavior is unchanged."""
    _eth_gate_stubs(monkeypatch)
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    traffic_ran = {"n": 0}

    async def traffic(timeout_sec=None):
        traffic_ran["n"] += 1
        return True, "links healthy"

    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", traffic)

    ok, evidence = await srv.galaxy_recovery._verify_device(32, lambda m: None, run_fabric=True)

    assert ok is True and traffic_ran["n"] == 1
    assert evidence["eth_heartbeat"]["ok"] is None  # skipped, not configured


@pytest.mark.asyncio
async def test_eth_heartbeat_hang_reads_as_frozen_not_skipped(monkeypatch):
    """A configured read that HANGS past its timeout is wedge evidence, not a skip. A frozen eth
    core can itself hang the read, so a timeout must return False (gate holds) — never None, which
    falls through to the traffic pass that shoves the frozen chip off the bus. Mirrors the sibling
    verify_fabric_health, whose timeout is already False."""
    # The rung must be armed to reach the verdict mapping under test; arming itself is pinned
    # in tests/test_rung_arming.py.
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "sleep 5")
    ok, detail = await srv.health_monitor.verify_eth_heartbeat(timeout_sec=0.3)
    assert ok is False, f"a hung eth read must read as frozen/False, got {ok!r}: {detail}"


@pytest.mark.asyncio
async def test_eth_heartbeat_hang_kills_the_process_group(tmp_path, monkeypatch):
    """The test above only asserts the VERDICT, and it passed while leaking: asyncio.wait_for's
    own cancellation of eth.check() raises CancelledError, which does not match run_probe's
    `except asyncio.TimeoutError` (subproc.py) — run_probe's `finally` only clears tracking
    (track(None)) and never kills the group in that path. Left unfixed, a frozen core hangs the
    read, the outer bound fires, the gate HOLDs (which by design never reaches
    _kill_device_holders), and the reader stays attached to the wedged mesh forever — the exact
    "nested subprocess holds the device" class CLAUDE.md's process-group doctrine exists to
    prevent. Proven by actually checking the process group is dead, not just the return value."""
    pgidfile = tmp_path / "pgid"
    # Armed so the override actually runs; arming itself is pinned in tests/test_rung_arming.py.
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "1")
    # start_new_session=True (subproc.run_probe) makes the spawned bash's own pid its pgid, so
    # `echo $$` from inside it names the group to check afterward.
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", f"echo $$ > {pgidfile}; sleep 5")

    ok, _ = await srv.health_monitor.verify_eth_heartbeat(timeout_sec=0.3)
    assert ok is False

    for _ in range(40):
        if pgidfile.exists() and pgidfile.read_text().strip():
            break
        time.sleep(0.05)
    pgid = int(pgidfile.read_text().strip())

    for _ in range(40):
        try:
            os.killpg(pgid, 0)  # signal 0: probe existence, kill nothing
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"process group {pgid} ('sleep 5') is still alive after the outer timeout — leaked")


@pytest.mark.asyncio
async def test_misconfigured_probe_timeout_cannot_swallow_the_inner_skip(tmp_path, monkeypatch):
    """TTDEV_ETH_CHECK_TIMEOUT is an operator-set value (/etc/default), not something this code
    can trust to stay below the caller's own bound. If it is set AT OR ABOVE timeout_sec, the
    built-in path's inner bound must still fire first (capped, see verify_eth_heartbeat) so a
    merely-slow probe still launders to SKIP via classify_exit(None) — not the caller's own
    outer-hang branch, which would read the same slow-but-not-wedged probe as a false
    UNHEALTHY/HOLD."""
    vroot = tmp_path / "validator"
    py_dir = vroot / "current" / "python_env" / "bin"
    py_dir.mkdir(parents=True)
    py = py_dir / "python"
    py.write_text('#!/usr/bin/env bash\nif [ "$1" = "-c" ]; then exit 0; fi\nsleep 5\n')
    py.chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    install_root = tmp_path / "install-root"
    install_root.mkdir()
    (install_root / "eth-heartbeat-probe.py").write_text("# dummy\n")
    monkeypatch.setenv("TTDEV_ROOT", str(install_root))
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    monkeypatch.setenv("TTDEV_ETH_CHECK_TIMEOUT", "999")  # misconfigured: far above timeout_sec

    ok, detail = await srv.health_monitor.verify_eth_heartbeat(timeout_sec=0.5)
    assert ok is None, f"the misconfigured inner timeout let the outer hang branch fire instead: {detail}"


@pytest.mark.asyncio
async def test_build_call_does_not_block_the_event_loop(monkeypatch):
    """eth.build() can shell out to up to three candidate pythons (resolve_python) once armed.
    Called synchronously from verify_eth_heartbeat, that stalls the event loop for the whole
    health gate — the job queue, MCP requests, and the sd_notify watchdog ping all freeze for as
    long as those subprocess.run calls take. Proven by running a fast concurrent tick alongside
    a slow, blocking eth.build() and checking ticks actually land WHILE it runs, not just that
    they eventually run once it returns."""
    ticks: list[float] = []

    async def _ticker():
        for _ in range(6):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    def _slow_build():
        time.sleep(0.3)
        return None

    monkeypatch.setattr(eth, "build", _slow_build)
    monkeypatch.setattr(eth, "build_reason", lambda: "n/a")

    ticker_task = asyncio.ensure_future(_ticker())
    t0 = time.monotonic()
    await srv.health_monitor.verify_eth_heartbeat(timeout_sec=5)
    elapsed = time.monotonic() - t0
    await ticker_task

    during = [t for t in ticks if t - t0 < elapsed - 0.05]
    assert len(during) >= 2, "the event loop never advanced while eth.build() ran — it is blocking"


# --- a check that produces NO verdict must be journaled, not vanish (R1/R2) ------
#
# A skip returns None, which the gate reads as "learned nothing, do not act". That is correct,
# but it left NO trace: an operator could not tell a mesh proved healthy from one never checked at
# all. The skip paths now journal the lost verdict, once per process, exactly as the fabric check's
# exit-77 path already did — loud, so unvalidated coverage is a record and not a silence.


@pytest.mark.asyncio
async def test_fabric_check_unset_journals_the_lost_verdict(monkeypatch):
    """No fabric command means the one check that proves the fabric moves data never runs. It is
    hard-required at preflight, so reaching here is a real anomaly — journal it, do not skip in
    silence."""
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)

    ok, _ = await srv.health_monitor.verify_fabric_health(timeout_sec=5)

    assert ok is None  # still a skip: never a reset trigger
    events = health.read_health_events(kinds={"fabric_check_unavailable"})
    assert [e["reason"] for e in events] == ["not_configured"], events


@pytest.mark.asyncio
async def test_fabric_check_not_runnable_journals_the_lost_verdict(monkeypatch):
    """Configured but the check could not even spawn — the same lost verdict as an exit-77, and it
    must be just as loud, not a silent None."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")

    async def _cannot_spawn(*a, **k):
        raise OSError("cannot spawn the fabric check")

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _cannot_spawn)

    ok, _ = await srv.health_monitor.verify_fabric_health(timeout_sec=5)

    assert ok is None
    events = health.read_health_events(kinds={"fabric_check_unavailable"})
    assert [e["reason"] for e in events] == ["not_runnable"], events


@pytest.mark.asyncio
async def test_a_repeated_skip_journals_only_once(monkeypatch):
    """These checks run on every gate and every idle relift. A static skip journaled on each pass
    would bury its own signal, so the durable record is emitted once per process — the first is the
    whole message."""
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)

    for _ in range(3):
        await srv.health_monitor.verify_fabric_health(timeout_sec=5)

    events = health.read_health_events(kinds={"fabric_check_unavailable"})
    assert len(events) == 1, events


@pytest.mark.asyncio
async def test_a_real_fabric_verdict_journals_no_skip(monkeypatch):
    """A check that actually ran and passed produced a verdict — nothing was skipped, so the loud
    record must stay reserved for a genuine no-verdict skip and not fire on the healthy path."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "exit 0")

    ok, _ = await srv.health_monitor.verify_fabric_health(timeout_sec=10)

    assert ok is True
    assert health.read_health_events(kinds={"fabric_check_unavailable"}) == []


@pytest.mark.asyncio
async def test_eth_heartbeat_unset_journals_the_optional_rung_is_off(monkeypatch):
    """The passive rung is optional, but loud-if-absent: its being off must leave a durable trace,
    tagged distinctly from a wired rung that could not check, so an operator can tell "never
    configured" from "configured but broken"."""
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)

    ok, _ = await srv.health_monitor.verify_eth_heartbeat(timeout_sec=5)

    assert ok is None  # unchanged: still falls through to the traffic pass
    events = health.read_health_events(kinds={"eth_heartbeat_unavailable"})
    assert [e["reason"] for e in events] == ["not_configured"], events


@pytest.mark.asyncio
async def test_eth_heartbeat_could_not_check_journals_the_lost_verdict(monkeypatch):
    """A wired rung that exits 77 ran but reached no verdict — the silent degrade of a capability an
    operator asked for. It must be as loud as the fabric check's own exit-77."""
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", f"exit {FABRIC_CHECK_CANNOT_CHECK_RC}")
    # Armed so the exit-77 journal (not a disarmed skip) is what's under test; arming itself is
    # pinned in tests/test_rung_arming.py.
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "1")

    ok, _ = await srv.health_monitor.verify_eth_heartbeat(timeout_sec=10)

    assert ok is None
    events = health.read_health_events(kinds={"eth_heartbeat_unavailable"})
    assert [e["reason"] for e in events] == ["could_not_check"], events


# --- a frozen-eth verdict must route the GATE to HOLD, never to a reset ---------
#
# The passive read reports the wedge; the gate has to act on it. A frozen verdict returns
# healthy=False, which skips the healthy-path hold and used to fall straight through to the galaxy
# reset — the exact iatrogenic all-chip drop the read exists to prevent. The gate must hold instead.


def _gate_with_verdict(monkeypatch, tmp_path, verdict):
    """Drive _device_health_gate to its reset decision with _verify_device returning `verdict`
    and count the galaxy resets it triggers. Returns the resets counter dict."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    srv.recovery_mechanism.last_reset_monotonic = 0.0
    srv.recovery_mechanism.last_reset_failed = False
    resets = {"n": 0}

    async def reset(indices, log):
        resets["n"] += 1
        return True

    async def verify(expected, log, run_fabric=True, **_):
        return verdict

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    patch_recovery(monkeypatch, "_verify_device", verify)
    return resets


@pytest.mark.asyncio
async def test_a_frozen_eth_verdict_is_held_not_reset(monkeypatch, tmp_path):
    """The load-bearing half of Item 2: a frozen active-eth core (enum + ARC + snapshot all pass,
    only the eth firmware heartbeat is stuck) is a single-chip wedge that self-heals. The gate must
    HOLD it — a galaxy reset here is the measured drop that takes all 32 chips to 0xFFFFFFFF. Fails
    on base, where a bare healthy=False routes to _reset_and_verify_device."""
    frozen = (False, {"eth_heartbeat": {"ok": False, "detail": "a frozen active-eth core: 27-25"}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, frozen)

    srv._mark_device_dirty("job ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 0, "reset a frozen single-chip wedge — the measured all-chip drop"
    assert srv.fsm.state is not ServerState.HEALTHY, "door not held — the next tenant runs on the frozen mesh"
    assert srv.fsm.record.why == "eth_frozen"


@pytest.mark.asyncio
async def test_a_frozen_eth_verdict_holds_the_door_even_when_not_pre_dirty(monkeypatch, tmp_path):
    """The gate reaches its verdict on a CLEAN flag too — the startup fabric probe, and a job's
    plain non-wedge exit that forces the read without marking dirty. On those paths the hold used to
    call an unverified CLEAR, which records no doubt on an undirty device, so the door stayed OPEN
    and the next tenant was dispatched onto the frozen mesh — the exact all-chip drop. The hold must
    key on the frozen verdict, not on a prior dirty flag. Fails on the pre-fix commit, where the
    branch clears an already-clean device and leaves device_unverified_why empty."""
    frozen = (False, {"eth_heartbeat": {"ok": False, "detail": "a frozen active-eth core: 27-25"}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, frozen)

    # No _mark_device_dirty: the device is clean when the forced probe finds the frozen core.
    await srv._device_health_gate(None, phase="startup", run_fabric=True)

    assert resets["n"] == 0, "reset a frozen single-chip wedge — the measured all-chip drop"
    assert srv.fsm.state is not ServerState.HEALTHY, "clean-flag path left the door open onto the frozen mesh"
    assert srv._device_unavailable_for_tenant(), "the next tenant would be dispatched onto it"


@pytest.mark.asyncio
async def test_frozen_eth_reset_needs_both_kill_switches(monkeypatch, tmp_path):
    """Both guards default-on now, so restoring the old reset for a frozen verdict takes disabling
    BOTH: the eth-freeze hold's kill switch AND the mass-drop reset floor. For an operator on a host
    where the reader is proving too false-positive before D2 hardening lands."""
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_FREEZE_HOLD", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0")  # disable the floor too
    frozen = (False, {"eth_heartbeat": {"ok": False, "detail": "a frozen active-eth core: 27-25"}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, frozen)

    srv._mark_device_dirty("job ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 1, "both kill switches set but the frozen verdict was still held, not reset"


@pytest.mark.asyncio
async def test_an_eth_heartbeat_that_could_not_be_read_does_not_hold_the_gate(monkeypatch, tmp_path):
    """SKIPPED is not FROZEN. The passive eth read returns None when the probe is unconfigured or
    could not complete — the state every host is in until the reader is validated on a reserved
    box — and False only on a confirmed freeze. A gate that read None as frozen would take the
    eth-frozen hold on every pass, and with the probe self-blocked that IS the default reading. The
    probe's own tri-state is pinned elsewhere; this pins the gate's reading of it, on the pass where
    it decides anything: enum+ARC pass, the eth read cannot run, and the traffic pass then measures
    the fabric BAD (an unreadable eth read falls THROUGH to the traffic pass by design). The eth
    verdict is what picks between holding for a frozen core and walking the ordinary ladder there."""
    events = []
    unread_eth = (
        False,
        {
            "snapshot": {"ok": True, "detail": "1 chip"},
            "eth_heartbeat": {"ok": None, "detail": "could not check"},
            "fabric": {"ok": False, "detail": "links down"},
        },
    )
    resets = _gate_with_verdict(monkeypatch, tmp_path, unread_eth)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))

    srv._mark_device_dirty("job ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 0, "one chip off the bus is below the floor — the mesh reset is the drop to avoid"
    assert (
        "galaxy_reset_suppressed_below_mass_threshold" in events
    ), "an eth read that could not run was taken for a frozen core — the gate held short of its ladder"


@pytest.mark.asyncio
async def test_a_single_chip_unverifiable_fabric_still_holds_when_dirty(monkeypatch, tmp_path):
    """The untested arm of the fabric-unverified hold. On a SINGLE-chip host an unresolved fabric
    pass with a clean flag releases — there is no mesh for a wedged inter-chip link to be in — but a
    DIRTY one still holds: the job that dirtied it is exactly what could have left this host's own
    links unretrained, and enum+ARC cannot see that. Do not reset either: a 77 is not a fault a
    reset fixes."""
    no_verdict = (
        True,
        {"snapshot": {"ok": True, "detail": "1 chip"}, "fabric": {"ok": None, "detail": "no trained link to test"}},
    )
    resets = _gate_with_verdict(monkeypatch, tmp_path, no_verdict)  # one /dev node -> expected 1

    srv._mark_device_dirty("job ended killed")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 0, "a 77 is not a fault a reset fixes"
    assert srv.fsm.state is not ServerState.HEALTHY, "the fabric was never proven — must still hold"
    assert srv.fsm.record.why == "fabric_unverified"


@pytest.mark.asyncio
async def test_a_mass_drop_unhealthy_verdict_still_resets(monkeypatch, tmp_path):
    """The boundary under the default-safe floor: a sub-mass-drop unhealthy verdict now HOLDS, but a
    genuine mass drop (most of the mesh off the bus) still reaches the galaxy reset — self-heal does
    not recover it."""
    unhealthy = (False, {"snapshot": {"ok": False, "detail": "chips did not enumerate"}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, unhealthy)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: 32)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})  # 1 of 32 present => 31 off-bus

    srv._mark_device_dirty("job ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 1, "a mass drop must still reach the galaxy reset"


@pytest.mark.asyncio
async def test_a_single_arc_frozen_chip_holds_by_default(monkeypatch, tmp_path):
    """The blx04 incident as a regression: one chip's ARC heartbeat stalls — the gate reads
    healthy=False, yet nothing is off the bus (0 dead, all present). By DEFAULT (no floor env) this
    must HOLD, not fire the galaxy reset that drove the whole mesh to 0xFFFFFFFF for hours."""
    arc_frozen = (False, {"heartbeat": {"verdict": "unhealthy", "detail": "chip 8 ARC frozen"}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, arc_frozen)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: 32)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(32)})  # 0 off-bus
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", raising=False)  # DEFAULT env

    srv._mark_device_dirty("job 031 ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 0, "galaxy-reset a single ARC-frozen chip by default — the blx04 incident"
    assert srv._device_unavailable_for_tenant(), "the frozen mesh must be held from the next tenant"


@pytest.mark.asyncio
async def test_a_mass_arc_frozen_present_mesh_is_not_suppressed_and_resets(monkeypatch, tmp_path):
    """F17, the blx04 present-mesh deadlock: EVERY chip present on the bus but its ARC heartbeat
    frozen (a mass wedge). Nothing is off the bus, so the off-bus floor read it as 0/32 and held it
    below the mass-drop floor forever — the one effective remedy blocked. A galaxy reset here is both
    correct and light (chips are present, so it re-inits rather than inverts), so the floor must key
    on the wedged chips and NOT suppress. Fails on base, where wedged==off_bus==0 < 16 and it holds."""
    stalled = [str(i) for i in range(32)]
    arc_frozen = (False, {"heartbeat": {"verdict": "unhealthy", "detail": "all 32 ARC frozen", "stalled": stalled}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, arc_frozen)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: 32)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(32)})  # 0 off-bus
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", raising=False)  # DEFAULT floor

    srv._mark_device_dirty("job 031 ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 1, "an all-ARC-frozen present mesh is a mass wedge — reset must NOT be suppressed"


@pytest.mark.asyncio
async def test_a_below_floor_arc_frozen_count_still_holds(monkeypatch, tmp_path):
    """The boundary the wedged floor must keep: a FEW frozen chips (below the mass floor), nothing off
    the bus, self-heals — a galaxy reset for them is the measured all-chip drop the floor guards
    against, so it must still HOLD. Guards the single/few-frozen contract while the mass case resets."""
    stalled = ["3", "8", "17"]  # 3 of 32 frozen — below the 16-chip mass floor
    arc_frozen = (False, {"heartbeat": {"verdict": "unhealthy", "detail": "3 ARC frozen", "stalled": stalled}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, arc_frozen)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: 32)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(32)})  # 0 off-bus
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", raising=False)

    srv._mark_device_dirty("job 031 ended timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets["n"] == 0, "a below-floor frozen count self-heals — a mesh reset is the drop to avoid"
    assert srv._device_unavailable_for_tenant(), "the frozen mesh must be held from the next tenant"


@pytest.mark.asyncio
async def test_a_self_healed_frozen_core_is_not_reset_over_a_lingering_reported_fault(monkeypatch, tmp_path):
    """The blx02 signature: one CCL job BOTH reports a device fault ('waiting for active ethernet
    core') AND freezes that core. Gate 1 reads the core frozen and holds it — good. But a reported
    fault stands until a reset, so once the core self-heals, gate 2 sees enum+ARC healthy, skips the
    healthy-clear on the still-set fault, and falls through to the galaxy reset the hold exists to
    avoid — now on an already-recovered mesh. The heartbeat reader positively identified the frozen
    core the runtime named, so the hold must retire that fault: the door still holds on the unverified
    flag, and the next healthy gate clears it without a reset. Fails on base at gate 2's reset."""
    frozen = (False, {"eth_heartbeat": {"ok": False, "detail": "a frozen active-eth core: 27-25"}})
    resets = _gate_with_verdict(monkeypatch, tmp_path, frozen)

    # Gate 1: the wedging job left both a reported fault and the frozen core.
    monkeypatch.setattr(srv, "device_fault_reported", "job 031 waiting for active ethernet core")
    srv._mark_device_dirty("job 031 waiting for active ethernet core")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert resets["n"] == 0, "gate 1 reset a frozen single-chip wedge — the measured all-chip drop"
    assert srv.fsm.state is not ServerState.HEALTHY, "gate 1 left the door open onto the frozen mesh"
    assert srv.device_fault_reported == "", "the hold did not retire the fault it subsumes"

    # Gate 2: the core self-healed — enum + ARC + eth all read healthy now.
    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"eth_heartbeat": {"ok": True, "detail": "advancing"}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    await srv._device_health_gate(None, phase="pre-job", run_fabric=True)

    assert (
        resets["n"] == 0
    ), "a reported fault outlived the hold and reset a self-healed mesh — the all-chip drop D1 avoids"
    assert not srv._device_unavailable_for_tenant(), "the door never reopened after the core self-healed"


# --- the idle relift must reopen a self-healed hold with no job flowing to do it ---------------
#
# A self-heal HOLD (a frozen eth core, or an off-bus drop below the reset floor) drops the dirty
# flag and holds the tenant door on device_unverified_why. Nothing else re-checks it: the pre-job
# gate and the tenant hold-poll only re-verify a DIRTY device. So a mesh that self-heals in
# ~15-89 min stays refused until a re-dirty or a broker restart. The idle relift closes that gap —
# READ-ONLY (enum+ARC + the passive eth read, never the traffic pass, never a reset). OFF by default.


def _setup_selfheal_hold(monkeypatch, tmp_path, *, n_present=31):
    """Put the device into a self-heal HOLD with the relift armed, ready for _attempt_idle_relift."""
    for i in range(n_present):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)  # no detached reset cycling
    srv.device_op_lock = None
    srv.device_op_active = ""
    fsm_dirty(srv, "gate/post-job: eth-core heartbeat frozen — held, not reset", why="eth_frozen")
    srv.last_relift_monotonic = 0.0
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "1")


@pytest.mark.asyncio
async def test_idle_relift_lifts_a_self_healed_hold(monkeypatch, tmp_path, clear_job_state):
    """The mesh came back: enum+ARC read healthy AND the eth heartbeat is advancing, so it lifts."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    seen = {"run_fabric": None}

    async def healthy(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        return True, {"snapshot": {"ok": True}}

    async def eth_advancing(timeout_sec=60.0):
        return True, "every active-eth-core heartbeat advancing"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_advancing)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is ServerState.HEALTHY, "a self-healed mesh must reopen to tenants on its own"
    assert srv.fsm.record.why not in srv.SELFHEAL_WHYS, "the self-heal marker must clear on lift"
    assert seen["run_fabric"] is False, "the relift must never run the fabric traffic pass"


@pytest.mark.asyncio
async def test_idle_relift_lifts_an_off_bus_floor_hold_without_the_eth_reader(monkeypatch, tmp_path, clear_job_state):
    """An off-bus-below-floor hold is placed for a chip that LEFT THE BUS, which enum+ARC+sysfs see
    directly — so once the chip is back and answering, that alone lifts it, even with the eth reader
    unconfigured (None). Only a frozen-eth hold needs a confirmed-advancing heartbeat. Without this
    a floor-hold on a box with no eth reader never reopens — the stuck-hold the reset floor depends
    on the relift to avoid. Fails on base, where the relift demands eok is True and None never lifts."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    fsm_dirty(srv, "gate/post-job: 3/32 off-bus below the reset floor — held, not reset", why="off_bus")

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped (TT_DEVICE_MCP_ETH_HEARTBEAT_CMD not set)"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is ServerState.HEALTHY, "an off-bus chip back on the bus must reopen without the eth reader"
    assert srv.fsm.record.why not in srv.SELFHEAL_WHYS, "the hold must clear once the chip is verified back"


@pytest.mark.asyncio
async def test_idle_relift_still_holds_an_off_bus_hold_whose_eth_is_frozen(monkeypatch, tmp_path, clear_job_state):
    """The fail-safe on the off-bus path: a chip back on the bus whose eth reader now reads FROZEN is
    an active wedge whatever first dropped it — so a configured reader reading False still holds, even
    though this hold does not REQUIRE an advancing read to lift."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    fsm_dirty(srv, "gate/post-job: 3/32 off-bus below the reset floor — held, not reset", why="off_bus")

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def frozen(timeout_sec=60.0):
        return False, "core 27-25 frozen"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", frozen)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is not ServerState.HEALTHY, "a frozen eth read is an active wedge — hold even an off-bus hold"


@pytest.mark.asyncio
async def test_idle_relift_holds_an_off_bus_hold_while_the_fabric_is_unhealthy(monkeypatch, tmp_path, clear_job_state):
    """The flap fix: an off-bus chip back on the bus and ARC-healthy, with the eth reader unconfigured
    (None), still HOLDS when the last fabric verdict was UNHEALTHY. enum+ARC recover before the eth
    cores do, so lifting on them alone readmits the next tenant onto a fabric that still cannot move
    data — which re-wedges the chip and flaps the hold. Only a healthy fabric verdict reopens the door.
    Fails on base, where an off-bus hold lifts on enum+ARC with the reader unconfigured, fabric or not."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    fsm_dirty(srv, "gate/post-job: 3/32 off-bus below the reset floor — held, not reset", why="off_bus")
    srv.health_monitor.last_fabric_ok = False  # the last traffic pass said the fabric cannot move data

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped (TT_DEVICE_MCP_ETH_HEARTBEAT_CMD not set)"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)

    await srv._attempt_idle_relift()

    assert (
        srv.fsm.state is not ServerState.HEALTHY
    ), "an off-bus hold must not lift onto a fabric whose last verdict was unhealthy"
    assert srv.fsm.record.why in srv.SELFHEAL_WHYS, "the self-heal hold stands until a healthy fabric pass"


@pytest.mark.asyncio
async def test_idle_relift_never_lifts_an_eth_hold_onto_a_failed_fabric_verdict(monkeypatch, tmp_path, clear_job_state):
    """The eth-hold twin of the off-bus flap fix: a gate can place an eth_frozen hold on a present
    mesh whose fabric traffic pass measured UNHEALTHY while every eth-core heartbeat was ADVANCING
    (a routing/link wedge the passive read is blind to). An advancing heartbeat proves the firmware
    is alive, not that the links move data — lifting on it readmits tenants onto a mesh whose last
    real traffic verdict was FAIL. Fails on base, where the eth_frozen branch lifts on the
    heartbeat alone, seconds after the gate held it."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "0")  # isolate the hold from the reset rung
    srv.health_monitor.last_fabric_ok = False

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_advancing(timeout_sec=60.0):
        return True, "every active-eth-core heartbeat advancing"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_advancing)

    await srv._attempt_idle_relift()

    assert (
        srv.fsm.state is not ServerState.HEALTHY
    ), "an eth hold must not lift onto a fabric whose last verdict was unhealthy"
    assert srv.fsm.record.why == "eth_frozen", "the hold stands until a healthy fabric pass or the reset"


@pytest.mark.asyncio
async def test_idle_relift_escalates_an_eth_hold_with_a_failed_fabric_to_the_present_reset(
    monkeypatch, tmp_path, clear_job_state
):
    """The escalation half: the fabric-failed eth hold above is a FULLY PRESENT mesh — exactly the
    state the present-mesh galaxy reset cures by re-initing every eth core — so past the grace on
    an idle, tenant-free mesh the relift runs that reset (riding its guards) instead of standing
    until a broker restart's startup gate."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    counters = _arm_stuck_hold(monkeypatch)
    srv.health_monitor.last_fabric_ok = False

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_advancing(timeout_sec=60.0):
        return True, "every active-eth-core heartbeat advancing"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_advancing)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 1, "past the grace, the fabric-failed present mesh must escalate to the galaxy reset"


@pytest.mark.asyncio
async def test_idle_relift_holds_when_eth_read_is_inconclusive(monkeypatch, tmp_path, clear_job_state):
    """`None` from the eth read is "could not tell" — reader unconfigured, a 60s timeout, or an
    exit-77 uncheckable, any of which a frozen core can itself provoke — NOT proof of health.
    enum+ARC are blind to a frozen core, so lifting here would hand a still-wedged mesh to a
    tenant whose traffic pass then inverts it. It must stay held."""
    _setup_selfheal_hold(monkeypatch, tmp_path)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_inconclusive(timeout_sec=60.0):
        return None, "read timed out after 60s (ARC unresponsive)"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_inconclusive)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is not ServerState.HEALTHY, "an inconclusive eth read is not proof — the hold must stand"
    assert srv.fsm.record.why in srv.SELFHEAL_WHYS, "still unconfirmed: still the relift's self-heal hold"


@pytest.mark.asyncio
async def test_idle_relift_holds_a_still_frozen_eth_core(monkeypatch, tmp_path, clear_job_state):
    """enum+ARC are blind to a frozen eth core — the wedge itself. The passive heartbeat read is
    not, so a chip back on the bus with its core still frozen must stay held, not be handed out."""
    _setup_selfheal_hold(monkeypatch, tmp_path)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def frozen(timeout_sec=60.0):
        return False, "core 27-25 frozen"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", frozen)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is not ServerState.HEALTHY, "enum+ARC pass but the eth core is frozen — the hold must stand"
    assert srv.fsm.record.why in srv.SELFHEAL_WHYS, "still frozen: still the relift's self-heal hold"


@pytest.mark.asyncio
async def test_idle_relift_holds_a_still_off_bus_chip_without_resetting(monkeypatch, tmp_path, clear_job_state):
    """Still off the bus: leave it held and wait for self-heal. The relift NEVER resets — a reset
    at a still-wedged endpoint is the mesh-inverting drop the hold exists to avoid."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    resets = {"n": 0}
    eth_called = {"n": 0}

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 8 off the bus"}}

    async def counting_reset(indices, log):
        resets["n"] += 1
        return True

    async def eth(timeout_sec=60.0):
        eth_called["n"] += 1
        return None, "skipped"

    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    patch_recovery(monkeypatch, "_reset_and_verify_device", counting_reset)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is not ServerState.HEALTHY, "a still-off-bus chip must stay held"
    assert resets["n"] == 0, "the relift must never reset — it only waits for self-heal"
    assert eth_called["n"] == 0, "an unhealthy enum+ARC short-circuits before the eth read"


@pytest.mark.asyncio
async def test_idle_relift_bails_if_a_chip_drops_during_the_verify(monkeypatch, tmp_path, clear_job_state):
    """The sampler's dead-chip path is LOCK-FREE — it isolates a dropped chip and marks the device
    dirty without the _device_op lock. So a DIFFERENT chip can drop during the relift's long awaits
    (enum+ARC, then the up-to-60s eth read), re-raising device_dirty after the relift's under-lock
    guard already passed. The relift must re-check dirty AFTER those awaits, before the clear: a
    _clear_device_dirty(verified=True) here would wipe that real dead-chip flag and reopen the door
    on a mesh now silently short a chip — the exact unverified-mesh handoff the flag exists to stop."""
    _setup_selfheal_hold(monkeypatch, tmp_path)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_advancing_but_a_chip_dropped(timeout_sec=60.0):
        # models the lock-free sampler isolating a chip that dropped mid-read: dirty flips True
        # during this await, after the under-lock guard already saw it clear.
        fsm_dirty(srv, "test")
        return True, "measured cores advancing"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_advancing_but_a_chip_dropped)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is not ServerState.HEALTHY, "the relift must not reopen the door over a fresh dead-chip drop"
    assert srv.fsm.record.why in srv.SELFHEAL_WHYS, "nothing was verified-cleared, so the self-heal hold stands"


@pytest.mark.asyncio
async def test_idle_relift_is_inert_when_kill_switched(monkeypatch, tmp_path, clear_job_state):
    """The kill switch (SELFHEAL_RELIFT=0) restores the old wait: the relift never touches the
    device, and the hold stands until a job or a broker restart reopens it."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "0")
    verify_called = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_called["n"] += 1
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert verify_called["n"] == 0, "kill-switched: the relift must not probe the device"
    assert srv.fsm.state is not ServerState.HEALTHY, "and the hold must stand under the kill switch"


@pytest.mark.asyncio
async def test_idle_relift_ignores_a_non_self_heal_hold(monkeypatch, tmp_path, clear_job_state):
    """A fabric-uncheckable host holds on device_unverified_why with device_selfheal_hold False.
    An enum+ARC re-verify proves nothing that hold was placed for, so the relift must leave it —
    else it would falsely reopen the door on a mesh whose fabric was never checked."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    fsm_dirty(srv, "gate/post-job: enum+ARC healthy, fabric unverified", why="fabric_unverified")
    verify_called = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_called["n"] += 1
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert verify_called["n"] == 0, "a non-self-heal hold is out of the relift's scope"
    assert srv.fsm.state is not ServerState.HEALTHY, "the fabric-unverified hold must stand untouched"


# --- the fabric-unverified hold: the f07 recurring strand ------------------------------------
#
# A job that crashes flags the device dirty; the gate then runs the full check and, when enum+ARC
# pass but the fabric traffic check exits 77 (could-not-run — NOT fabric-wedged), holds the door on
# device_unverified_why WITHOUT device_selfheal_hold. The self-heal relift is scoped past that hold,
# and nothing else re-checks a held-but-undirty mesh, so it strands until a broker restart or a human
# reset. The opt-in fabric relift (TT_DEVICE_MCP_FABRIC_RELIFT, OFF by default because it re-runs the
# traffic pass) closes the gap: it re-runs the health check and lifts ONLY on a real fabric verdict.


def _setup_fabric_unverified_hold(monkeypatch, tmp_path, *, n_present=31):
    """Put the device into a FABRIC-UNVERIFIED hold with the opt-in fabric relift armed."""
    for i in range(n_present):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    srv.device_op_lock = None
    srv.device_op_active = ""
    fsm_dirty(srv, "gate/post-job: enum+ARC healthy, fabric unverified", why="fabric_unverified")
    srv.last_relift_monotonic = 0.0
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "1")


@pytest.mark.asyncio
async def test_idle_relift_lifts_a_fabric_unverified_hold_when_fabric_reverifies(
    monkeypatch, tmp_path, clear_job_state
):
    """The fix for the f07 strand: an intermittent fabric-check exit-77 after a job crash leaves the
    door held on an UNVERIFIED (not self-heal) hold the relift was scoped past. With the fabric relift
    armed, the relift re-runs the full check INCLUDING the traffic pass and lifts once the fabric
    returns a real healthy verdict. Fails on base: device_hold_needs_fabric arms nothing there."""
    _setup_fabric_unverified_hold(monkeypatch, tmp_path)
    seen = {"run_fabric": None}

    async def healthy(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "links healthy"}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is ServerState.HEALTHY, "a fabric that re-verifies on retry must reopen the door"
    assert srv.fsm.record.why != "fabric_unverified", "the fabric-hold marker must clear on lift"
    assert seen["run_fabric"] is True, "the fabric-unverified relift must re-run the traffic pass for a verdict"


@pytest.mark.asyncio
async def test_idle_relift_holds_a_fabric_unverified_hold_when_the_check_still_cannot_run(
    monkeypatch, tmp_path, clear_job_state
):
    """Fail-safe: enum+ARC pass and the traffic pass RUNS but exits 77 again (fok None), so nothing
    proved the fabric. _verify_device returns healthy=True on a SKIPPED fabric, so the lift must gate
    on fok IS True — never on healthy alone — or it reopens the door onto fabric no pass has cleared."""
    _setup_fabric_unverified_hold(monkeypatch, tmp_path)

    async def healthy_but_fabric_skipped(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}

    patch_recovery(monkeypatch, "_verify_device", healthy_but_fabric_skipped)

    await srv._attempt_idle_relift()

    assert (
        srv.fsm.state is not ServerState.HEALTHY
    ), "an unverifiable fabric on retry is not proof — the hold must stand"
    assert srv.fsm.record.why == "fabric_unverified", "still unverified: the fabric-hold marker stands"


@pytest.mark.asyncio
async def test_idle_relift_holds_a_fabric_unverified_hold_when_fabric_now_fails(monkeypatch, tmp_path, clear_job_state):
    """Fail-safe: the retry gets a real verdict and it is UNHEALTHY (fok False -> healthy False). The
    relift never resets — it leaves the hold for the next gate to reset — and must not reopen the door."""
    _setup_fabric_unverified_hold(monkeypatch, tmp_path)
    resets = {"n": 0}

    async def fabric_unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": "link 3 down"}}

    async def counting_reset(indices, log):
        resets["n"] += 1
        return True

    patch_recovery(monkeypatch, "_verify_device", fabric_unhealthy)
    patch_recovery(monkeypatch, "_reset_and_verify_device", counting_reset)

    await srv._attempt_idle_relift()

    assert srv.fsm.state is not ServerState.HEALTHY, "a fabric that now reads unhealthy must stay held"
    assert resets["n"] == 0, "the relift must never reset — that is the next gate's job"


@pytest.mark.asyncio
async def test_fabric_relift_is_off_by_default(monkeypatch, tmp_path, clear_job_state):
    """Default-safe: the fabric relift re-runs the TRAFFIC PASS, which can push a marginal chip off
    the bus, so it is opt-in. Unset, the relift must not touch a fabric-unverified hold — the
    behavior deployed today is unchanged (this is the guard the existing non-self-heal test relies on)."""
    _setup_fabric_unverified_hold(monkeypatch, tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_RELIFT", raising=False)
    verify_called = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_called["n"] += 1
        return True, {"fabric": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert verify_called["n"] == 0, "off by default: the fabric relift must not probe the device"
    assert srv.fsm.state is not ServerState.HEALTHY, "and the fabric-unverified hold must stand"


def test_hold_device_fabric_unverified_shuts_the_door_even_when_undirty(monkeypatch):
    """The fabric hold arms the PERTURBING traffic-pass relift, so it must shut the tenant door
    whatever the dirty state was when placed — never leave the door open on a device the relift can
    push traffic across. (The live call site is always dirty, but the door-shut must not depend on it.)"""
    patch_health_event(monkeypatch, lambda *a, **k: None)
    fsm_healthy(srv)

    srv._hold_device_fabric_unverified("gate/startup: enum+ARC healthy, fabric unverified")

    assert (
        srv.fsm.state is not ServerState.HEALTHY
    ), "a fabric-unverified hold must shut the door even from an undirty device"
    assert srv.fsm.record.why == "fabric_unverified", "and arm the fabric relift"
    assert srv.fsm.record.why not in srv.SELFHEAL_WHYS, "a fabric hold is not a self-heal hold"


@pytest.mark.asyncio
async def test_idle_relift_is_rate_limited(monkeypatch, tmp_path, clear_job_state):
    """It rides the sampler, which ticks far faster than a wedge self-heals; re-probing the device
    every tick would spin tt-smi at a held mesh, so a min interval governs it."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def still_held(expected, log, run_fabric=True, **_):
        calls["n"] += 1
        return False, {}

    patch_recovery(monkeypatch, "_verify_device", still_held)

    await srv._attempt_idle_relift()  # first pass runs (last_relift_monotonic was falsy)
    await srv._attempt_idle_relift()  # within the interval -> skipped

    assert calls["n"] == 1, "the relift must be rate-limited, not re-probe every sample"


def test_self_heal_hold_marks_and_a_verified_clear_lifts(monkeypatch):
    """The marker lifecycle: only a self-heal hold sets device_selfheal_hold, and a verified clear
    (what the relift calls on lift) retires it."""
    patch_health_event(monkeypatch, lambda *a, **k: None)
    fsm_healthy(srv)

    srv._hold_device_unverified("gate/post-job: eth-core heartbeat frozen — held, not reset")
    assert srv.fsm.record.why in srv.SELFHEAL_WHYS, "a self-heal hold must mark itself for the relift"
    assert srv.fsm.state is not ServerState.HEALTHY, "and hold the tenant door"

    srv._clear_device_dirty(verified=True, why="idle relift: verified healthy")
    assert srv.fsm.record.why not in srv.SELFHEAL_WHYS, "a verified clear must retire the self-heal marker"
    assert srv.fsm.state is ServerState.HEALTHY, "and reopen the door"


def test_a_non_self_heal_doubt_does_not_mark_for_relift(monkeypatch):
    """An unverified CLEAR of a dirty device records doubt but is NOT a self-heal hold: the relift
    must not later reopen it on an enum+ARC pass alone."""
    patch_health_event(monkeypatch, lambda *a, **k: None)
    fsm_dirty(srv, "test")

    srv._clear_device_dirty(verified=False, why="enum+ARC healthy, fabric unverified")

    assert srv.fsm.state is not ServerState.HEALTHY, "the doubt is recorded"
    assert srv.fsm.record.why not in srv.SELFHEAL_WHYS, "but it is not a self-heal hold the relift owns"


def test_relift_interval_parses_defensively(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_SELFHEAL_RELIFT_SEC", raising=False)
    assert srv._relift_interval_from_env() == 120  # default
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT_SEC", "not-a-number")
    assert srv._relift_interval_from_env() == 120  # malformed -> default, never crashes
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT_SEC", "0")
    assert srv._relift_interval_from_env() == 1  # floored so it cannot busy-spin
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT_SEC", "300")
    assert srv._relift_interval_from_env() == 300


@pytest.mark.asyncio
async def test_maybe_spawn_idle_relift_gates_and_single_flights(monkeypatch, tmp_path, clear_job_state):
    """The sampler spawns the relift as its own task (so a slow eth read never blocks the dead-chip
    tripwire): disabled -> no task; enabled+held -> exactly one, never a second while one runs."""
    _setup_selfheal_hold(monkeypatch, tmp_path)
    srv._relift_task = None
    ran = {"n": 0}
    gate = asyncio.Event()

    async def fake_relift():
        ran["n"] += 1
        await gate.wait()

    monkeypatch.setattr(srv, "_attempt_idle_relift", fake_relift)

    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "0")
    srv._maybe_spawn_idle_relift()
    assert srv._relift_task is None, "kill-switched: no relift task is spawned"

    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "1")
    srv._maybe_spawn_idle_relift()
    first = srv._relift_task
    assert first is not None, "enabled + held: the relift is spawned"
    await asyncio.sleep(0)  # let the task start and block on the gate (so it is not yet done)
    assert ran["n"] == 1
    srv._maybe_spawn_idle_relift()
    assert srv._relift_task is first, "single-flight: no second relift while one is in flight"
    assert ran["n"] == 1, "the in-flight task must not be re-spawned"

    gate.set()
    await first


# --- Part B: the idle escalation resets a present-mesh hold no read-only relift can clear --------
#
# A present-mesh eth/fault hold (device_hold_needs_eth, off_bus == 0 — the blx04 8h strand) is one
# NO read the broker owns can clear: enum+ARC see every chip present, the fabric pass is eth-blind,
# and there may be no eth reader. The read-only relift can only hold it, and an IDLE box has no next
# job whose gate would reset it, so it strands. The escalation moves that same gate reset to the idle
# timeout: past a short grace (shorter than the hold ceiling — a present mesh needs no 20-min wait to
# earn a reset), a present mesh, no tenant -> the galaxy reset that re-inits every eth core. ON by
# default so no hold outlives the ceiling; =0 is the operator kill-switch. An off-bus drop is never
# escalated — a galaxy reset there inverts the mesh.


def test_stuck_hold_escalation_is_on_by_default(monkeypatch):
    """No hold may stand indefinitely: with the env unset the escalation is armed on every box, so
    deploying the code makes it live without a per-host env. =0 is the operator kill-switch."""
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", raising=False)
    assert galaxy._stuck_hold_reset_enabled() is True
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "0")
    assert galaxy._stuck_hold_reset_enabled() is False
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "1")
    assert galaxy._stuck_hold_reset_enabled() is True


def test_stuck_hold_reset_due_past_the_grace_on_a_present_mesh(monkeypatch):
    """A present mesh earns its galaxy reset at the short grace (default 2 min), not the 20-min hold
    ceiling — an eth wedge does not self-heal by waiting, so the present-mesh reset is NOT a risky
    reset and the 10-min floor does not apply to it."""
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRESENT_MESH_RESET_GRACE_SEC", raising=False)
    now = datetime(2026, 7, 22, 12, 0, 0)
    held = (now - timedelta(minutes=3)).isoformat()
    assert galaxy._stuck_hold_reset_due(held, off_bus=0, now=now) is True


def test_stuck_hold_reset_not_due_before_the_grace(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRESENT_MESH_RESET_GRACE_SEC", raising=False)
    now = datetime(2026, 7, 22, 12, 0, 0)
    held = (now - timedelta(minutes=1)).isoformat()
    assert galaxy._stuck_hold_reset_due(held, off_bus=0, now=now) is False


def test_stuck_hold_reset_never_due_for_an_off_bus_drop(monkeypatch):
    """The mesh-inversion guard: a chip that LEFT the bus self-heals; a galaxy reset there takes all
    32 off and is the drop the below-floor hold exists to avoid. Never escalate it, however long."""
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", raising=False)
    now = datetime(2026, 7, 22, 12, 0, 0)
    held = (now - timedelta(hours=8)).isoformat()
    assert galaxy._stuck_hold_reset_due(held, off_bus=1, now=now) is False


def test_stuck_hold_reset_not_due_without_or_with_a_bad_stamp(monkeypatch):
    """A missing or unparseable episode clock fails closed — never fire on a hold whose age we
    cannot establish."""
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", raising=False)
    now = datetime(2026, 7, 22, 12, 0, 0)
    assert galaxy._stuck_hold_reset_due("", off_bus=0, now=now) is False
    assert galaxy._stuck_hold_reset_due("not-a-timestamp", off_bus=0, now=now) is False


def _arm_stuck_hold(
    monkeypatch, *, held_minutes_ago=21, beats=None, scan=None, reset_result=True, beats_after_reset=None
):
    """Arm the idle escalation: present-mesh hold past the ceiling, no tenant, no cooldown, reset
    mocked. ``beats_after_reset`` (if given) is what ``read_heartbeats`` returns AFTER the mocked
    reset runs — how a galaxy reset that dropped the mesh off the bus is expressed to the failed-
    reset climb. The host rung (reboot/power-cycle) is mocked to count and its rate-limit ledger +
    boot id are stubbed open. Returns a counters dict recording reset / fault-clear / dirty-clear /
    reboot / power-cycle calls."""
    beats = beats if beats is not None else {str(i): 100 for i in range(32)}
    scan = scan if scan is not None else HolderScan(holders=[], complete=True)
    counters = {"resets": 0, "fault_cleared": 0, "dirty_cleared": 0, "reboots": 0, "power_cycles": 0}
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "1")
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", raising=False)
    # The idle escalation ladder only exists on the Galaxy platform (PerTargetRecovery.escalate
    # always reports WAITING); select_recovery must resolve there for this 32-chip mesh.
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: beats)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: scan)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "read_auto_recovery_ledger", lambda: [])
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "test-boot")
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=held_minutes_ago)).isoformat()
    srv.device_hold_episode_reason = "gate/post-job: eth-core heartbeat frozen — held, not reset"
    srv.recovery_mechanism.last_reset_failed = False
    srv.recovery_mechanism.last_reset_monotonic = 0.0

    async def reset(indices, log):
        counters["resets"] += 1
        if beats_after_reset is not None:
            monkeypatch.setattr(srv, "read_heartbeats", lambda: beats_after_reset)
        return reset_result

    async def reboot(log, reason):
        counters["reboots"] += 1

    async def power_cycle(log, reason):
        counters["power_cycles"] += 1

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", reboot)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(
        srv,
        "_clear_device_reported_fault",
        lambda why: counters.__setitem__("fault_cleared", counters["fault_cleared"] + 1),
    )
    monkeypatch.setattr(
        srv, "_clear_device_dirty", lambda **kw: counters.__setitem__("dirty_cleared", counters["dirty_cleared"] + 1)
    )
    return counters


@pytest.mark.asyncio
async def test_stuck_present_mesh_hold_escalates_to_the_gate_reset(monkeypatch, clear_job_state):
    """Armed, past the ceiling, every chip present, no tenant: the idle escalation runs the gate's
    galaxy reset and — on recovery — retires the fault and clears the hold, exactly as the gate."""
    counters = _arm_stuck_hold(monkeypatch)
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "a stuck present-mesh hold past the ceiling must escalate to a reset"
    assert (
        counters["fault_cleared"] == 1 and counters["dirty_cleared"] == 1
    ), "a recovered mesh retires the fault and clears the hold"


@pytest.mark.asyncio
async def test_stuck_hold_escalation_holds_under_a_foreign_tenant(monkeypatch, clear_job_state):
    """The absolute guard: a foreign tenant on the device blocks the reset — never take a box's mesh
    out from under a running job. (blx04's 8h hold was idle, so this correctly did not apply there.)"""
    counters = _arm_stuck_hold(
        monkeypatch, scan=HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True)
    )
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert counters["resets"] == 0, "a foreign tenant must block the idle escalation reset"


@pytest.mark.asyncio
async def test_stuck_hold_escalation_holds_when_the_holder_scan_is_incomplete(monkeypatch, clear_job_state):
    """A scan that could not read every /proc counts as a tenant present: the reset does not land
    under a job it merely could not see."""
    counters = _arm_stuck_hold(monkeypatch, scan=HolderScan(holders=[], complete=False))
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert counters["resets"] == 0


@pytest.mark.asyncio
async def test_stuck_hold_escalation_holds_on_an_off_bus_drop(monkeypatch, clear_job_state):
    """A chip off the bus self-heals; a galaxy reset there inverts the mesh. Even past the ceiling
    and idle, an off-bus hold is never escalated."""
    counters = _arm_stuck_hold(monkeypatch, beats={str(i): 100 for i in range(31)})  # off_bus == 1
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert counters["resets"] == 0, "an off-bus drop must keep holding, never reset and invert the mesh"


@pytest.mark.asyncio
async def test_stuck_hold_escalation_holds_within_the_reset_cooldown(monkeypatch, clear_job_state):
    """A reset that already failed within the cooldown will not fix the mesh on a back-to-back retry
    — the repeat MMIO at a dead endpoint the cooldown exists to prevent. Hold, do not re-reset."""
    counters = _arm_stuck_hold(monkeypatch)
    srv.recovery_mechanism.last_reset_failed = True
    srv.recovery_mechanism.last_reset_monotonic = srv.time.monotonic()  # a failed reset just now
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert counters["resets"] == 0


@pytest.mark.asyncio
async def test_stuck_hold_escalation_is_inert_when_kill_switched(monkeypatch, clear_job_state):
    """The kill-switch disarms it fully: with TT_DEVICE_MCP_STUCK_HOLD_RESET=0 a stuck present-mesh
    hold past the ceiling, idle, is left standing — the reset never runs."""
    counters = _arm_stuck_hold(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "0")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert counters["resets"] == 0, "kill-switched: the escalation must not perturb the device"


@pytest.mark.asyncio
async def test_stuck_hold_escalation_retries_on_the_grace_cadence(monkeypatch, clear_job_state):
    """A present-mesh reset that does not clear the wedge RETRIES — an eth wedge does not self-heal
    by waiting and the box is idle, so the only cure keeps getting its shot until recovery or the
    ceiling. Retries are paced, not capped: a pass inside the grace window since the last reset
    defers; one past it fires again, episode latch notwithstanding."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False)  # the reset ran but did not recover
    first = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert first is True and counters["resets"] == 1

    srv.galaxy_recovery.mechanism.last_reset_monotonic = time.monotonic()
    second = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert (
        second is False and counters["resets"] == 1
    ), "inside the grace window since the last reset the retry must defer, not stack"

    srv.galaxy_recovery.mechanism.last_reset_monotonic = time.monotonic() - galaxy._present_mesh_reset_grace_sec() - 1
    third = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert (
        third is True and counters["resets"] == 2
    ), "past the grace window the escalation retries — the episode latch is not a cap here"


@pytest.mark.asyncio
async def test_stuck_hold_escalation_re_arms_for_a_new_episode(monkeypatch, clear_job_state):
    """The escalation latch clears with the episode clock: once the device comes back fit and a
    fresh hold opens later, that new episode escalates on its own clock — and the release keeps the
    watchdog's escalated marker honest."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert counters["resets"] == 1 and srv.fsm.latch("escalated") is True

    # Device comes back fit for a tenant: the release transition clears the episode clock AND the latch.
    srv.device_hold_logged = True
    srv._note_tenant_gate_verdict("")
    assert srv.fsm.latch("escalated") is False, "a recovered episode must clear the escalation latch"

    # A fresh hold episode opens and stands past the grace again -> the escalation fires for it.
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=21)).isoformat()
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 2


@pytest.mark.asyncio
async def test_stuck_hold_failed_reset_all_off_bus_holds_loudly_never_reboots(monkeypatch, clear_job_state):
    """The idle galaxy reset drops the present mesh to all-off-bus (present==0), which self-heal never
    recovers — but a warm reboot cannot re-enumerate dropped Galaxy ASICs either (32/32 came back off
    the bus in the incident; only a cold power cycle recovered them). With only the reboot opted in,
    the climb fires NO rung: it emits the loud power-cycle-required event and keeps holding. Fails on
    base, which warm-reboots the whole-bus wedge and lands right back in the dead state."""
    events = []
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={})  # reset bricked it
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["reboots"] == 0 and counters["power_cycles"] == 0
    ), "an all-off-bus mesh must never warm-reboot — the reboot cannot re-enumerate dropped ASICs"
    assert (
        "all_chips_off_bus_power_cycle_required" in events
    ), "a whole-bus wedge with no power cycle opted in must emit the loud actionable event"


@pytest.mark.asyncio
async def test_stuck_hold_failed_reset_on_a_present_mesh_climbs_to_the_host_rung(monkeypatch, clear_job_state):
    """A galaxy reset that did NOT recover but left the mesh PRESENT (every chip on the bus, ARC-
    healthy) is an eth/fabric wedge the reset could not clear — reaching here means the galaxy reset,
    which normally re-inits every eth core, failed, so it is NOT self-healing and the cascade must
    climb to the host rung rather than hold indefinitely (measured on blx04: hours of ceiling ->
    galaxy-reset -> hold with the eth core wedged the whole time). Only a FEW chips off the bus below
    the floor still holds (they re-enumerate) — see the below-floor test. Opt-in + rate-limited."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={str(i): 100 for i in range(32)})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert counters["reboots"] == 1, "a present-mesh wedge that survived the galaxy reset must climb to the host rung"


@pytest.mark.asyncio
async def test_stuck_hold_failed_reset_all_off_bus_holds_loudly_with_no_rung_opted_in(monkeypatch, clear_job_state):
    """A full mesh drop fires no destructive rung where the host opted into none — but absence is
    never health, so the climb still emits the loud power-cycle-required event before holding. The
    only automatic action it could take (a warm reboot) cannot re-enumerate the drop anyway. Fails
    on base, where a whole-bus drop with no opt-in returns silently with no event."""
    events = []
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={})
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert counters["reboots"] == 0 and counters["power_cycles"] == 0
    assert (
        "all_chips_off_bus_power_cycle_required" in events
    ), "absence is never health — a whole-bus wedge must be loud even with no rung opted in"


@pytest.mark.asyncio
async def test_stuck_hold_climb_is_blocked_by_a_fail_closed_ledger(monkeypatch, clear_job_state):
    """The climb rides the gate's own rate limiter: an unreadable auto-recovery ledger denies the
    escalation rather than risk a boot loop, even when the mesh dropped and the host opted in. An
    all-off-bus mesh takes the power-cycle rung (the reboot cannot re-enumerate it); the ledger must
    gate it just the same."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "read_auto_recovery_ledger", lambda: None)  # unreadable -> fail closed
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["power_cycles"] == 0 and counters["reboots"] == 0
    ), "an unreadable ledger must deny the climb (boot-loop guard)"


@pytest.mark.asyncio
async def test_stuck_hold_climb_does_not_reboot_when_a_tenant_arrives(monkeypatch, clear_job_state):
    """Defense in depth on the destructive rung: the box is idle at the outer gate but a foreign
    tenant lands on the device by the time the climb re-scans. The climb must re-check holders and
    refuse the escalation — the property the outer gate alone cannot pin, since it passed before the
    tenant arrived. An all-off-bus mesh takes the power-cycle rung, and a tenant must block it."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    scans = [
        HolderScan(holders=[], complete=True),  # idle at the outer gate
        HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True),
    ]
    seen = {"n": 0}

    def scan():
        i = min(seen["n"], len(scans) - 1)
        seen["n"] += 1
        return scans[i]

    monkeypatch.setattr(srv, "enumerate_device_holders", scan)
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["power_cycles"] == 0 and counters["reboots"] == 0
    ), "a foreign tenant on the device must block the climb's power cycle"


@pytest.mark.asyncio
async def test_stuck_hold_climb_does_not_reboot_on_an_incomplete_holder_scan(monkeypatch, clear_job_state):
    """An unreadable /proc counts as a tenant on the destructive rung too: if the climb's holder scan
    cannot see every process, it must not take the box down over a tenant it merely could not see. An
    all-off-bus mesh takes the power-cycle rung, and an incomplete scan must fail it closed."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    scans = [
        HolderScan(holders=[], complete=True),  # complete at the outer gate
        HolderScan(holders=[], complete=False),
    ]  # incomplete by the climb's scan
    seen = {"n": 0}

    def scan():
        i = min(seen["n"], len(scans) - 1)
        seen["n"] += 1
        return scans[i]

    monkeypatch.setattr(srv, "enumerate_device_holders", scan)
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["power_cycles"] == 0 and counters["reboots"] == 0
    ), "an incomplete scan must block the climb's power cycle (fail closed)"


@pytest.mark.asyncio
async def test_stuck_hold_climb_defers_when_a_reset_is_still_cycling(monkeypatch, clear_job_state):
    """A reset left cycling in its own scope reads all-ones exactly like a mass drop. The climb asks
    the scope, not the count: it defers to that in-flight reset rather than pulling the box out from
    under it, and does not reboot."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    scopes = [None, "cycling"]  # idle at the outer gate; a reset is cycling by the climb's check
    seen = {"n": 0}

    def scope():
        i = min(seen["n"], len(scopes) - 1)
        seen["n"] += 1
        return scopes[i]

    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", scope)
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert counters["reboots"] == 0, "an in-flight reset must defer the climb, not trigger a reboot"


@pytest.mark.asyncio
async def test_stuck_hold_climb_power_cycles_when_that_is_the_chosen_rung(monkeypatch, clear_job_state):
    """When the host opted into the power cycle alone (a whole-bus wedge a reboot cannot clear), the
    climb takes _choose_recovery_escalation's power-cycle rung rather than the reboot."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["power_cycles"] == 1 and counters["reboots"] == 0
    ), "power-cycle-only opt-in must climb straight to the power cycle"


@pytest.mark.asyncio
async def test_idle_relift_escalates_the_blx04_strand_to_a_reset(monkeypatch, tmp_path, clear_job_state):
    """The blx04 8h strand end-to-end: a present-mesh fault hold (device_hold_needs_eth) with NO eth
    reader (eok is None) reaches the relift's frozen/unreadable branch, which base can only hold.
    Armed and past the ceiling on an idle present mesh, the relift now runs the gate's galaxy reset
    instead of stranding. Fails on base, where the relift never resets a held mesh."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    counters = _arm_stuck_hold(monkeypatch)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped (TT_DEVICE_MCP_ETH_HEARTBEAT_CMD not set)"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 1, "the idle relift must escalate the stranded present-mesh hold to a reset"


@pytest.mark.asyncio
async def test_idle_relift_still_strands_the_blx04_case_when_kill_switched(monkeypatch, tmp_path, clear_job_state):
    """The kill-switch counterpart: with TT_DEVICE_MCP_STUCK_HOLD_RESET=0 the relift keeps the
    read-only base behavior — a present-mesh fault hold with no eth reader stays held, no reset."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    counters = _arm_stuck_hold(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "0")

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped"

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 0, "unarmed: the relift must hold read-only, never reset"
    assert srv.fsm.state is not ServerState.HEALTHY, "and the hold must stand"


@pytest.mark.asyncio
async def test_idle_relift_escalates_an_all_arc_frozen_present_mesh(monkeypatch, tmp_path, clear_job_state):
    """F17, the idle half: the blx04 mass ARC wedge — every chip present, every heartbeat frozen —
    makes _verify_device UNHEALTHY, so the relift's not-healthy branch runs. The off-bus escalator
    cannot see it (off_bus==0) and the frozen-eth branch needs enum+ARC HEALTHY, so base held it
    'never a reset here' forever (37h live). Armed and past the ceiling it must now escalate a
    confirmed mass wedge to the present-mesh galaxy reset. Fails on base, where it never resets."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    counters = _arm_stuck_hold(monkeypatch)
    stalled = [str(i) for i in range(32)]

    async def all_arc_frozen(expected, log, run_fabric=True, **_):
        return False, {"heartbeat": {"verdict": "unhealthy", "detail": "all 32 ARC frozen", "stalled": stalled}}

    patch_recovery(monkeypatch, "_verify_device", all_arc_frozen)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 1, "the relift must escalate an all-ARC-frozen present mesh to a reset"


@pytest.mark.asyncio
async def test_idle_relift_holds_a_below_floor_arc_frozen_present_mesh(monkeypatch, tmp_path, clear_job_state):
    """The relift boundary mirroring the gate's: a FEW frozen chips (below the mass floor) self-heal,
    so the relift must still HOLD read-only — a galaxy reset for them is the drop the floor guards
    against. Locks the mass-vs-sub-mass split on the idle path."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    counters = _arm_stuck_hold(monkeypatch)
    stalled = ["3", "8", "17"]

    async def few_arc_frozen(expected, log, run_fabric=True, **_):
        return False, {"heartbeat": {"verdict": "unhealthy", "detail": "3 ARC frozen", "stalled": stalled}}

    patch_recovery(monkeypatch, "_verify_device", few_arc_frozen)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 0, "a below-floor frozen count self-heals — the relift must hold, not reset"
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold must stand"


# --- A2 (off-bus): the escalation must reach an OFF-BUS hold that never self-returns ------------
#
# The present-mesh sibling above closes the eth/fault strand; a chip that LEFT THE BUS below the
# reset floor leaves the twin. The read-only relift cannot lift it — it is still off the bus — and
# an idle box has no next-job gate to run the gone-chip cascade, so read-only it stands past every
# ceiling (blx04's live 1/32-off-bus hold). TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE (default off this
# increment) closes it: past the ceiling, idle, tenant-free, it climbs the gate's gentlest-first
# recovery — the surgical bridge reset for an already-isolated chip, then the galaxy reset, then the
# host reboot rung. Never a read-only lift, never under a tenant, one recovery per episode.


def test_offbus_hold_escalate_is_on_by_default(monkeypatch):
    """On by default so no off-bus hold outlives the ceiling; the =0 kill-switch is the operator's
    only opt-out. Fails on base, where the default is off and an off-bus hold strands indefinitely."""
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", raising=False)
    assert galaxy._offbus_hold_escalate_enabled() is True
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "0")
    assert galaxy._offbus_hold_escalate_enabled() is False
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "1")
    assert galaxy._offbus_hold_escalate_enabled() is True


def test_generic_hold_escalate_is_on_by_default(monkeypatch):
    """On by default so an arms-neither hold no relift can lift does not stand until a broker restart;
    the =0 kill-switch is the operator's only opt-out. Fails on base, where the default is off."""
    monkeypatch.delenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", raising=False)
    assert srv._generic_hold_escalate_enabled() is True
    monkeypatch.setenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "0")
    assert srv._generic_hold_escalate_enabled() is False
    monkeypatch.setenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "1")
    assert srv._generic_hold_escalate_enabled() is True


def test_offbus_stuck_hold_due_is_the_off_bus_mirror_of_the_present_mesh_check(monkeypatch):
    """The mirror image of _stuck_hold_reset_due: due only for a DROP (off_bus > 0) past the ceiling.
    A present mesh (off_bus == 0) is the sibling's class and is excluded here so it is not double-
    armed; before the ceiling, and a missing/bad stamp, all fail closed."""
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", raising=False)
    now = datetime(2026, 7, 22, 12, 0, 0)
    past = (now - timedelta(minutes=21)).isoformat()
    assert galaxy._offbus_stuck_hold_due(past, off_bus=1, now=now) is True
    assert (
        galaxy._offbus_stuck_hold_due(past, off_bus=0, now=now) is False
    ), "a present mesh is the present-mesh sibling's class, not the off-bus path's"
    before = (now - timedelta(minutes=5)).isoformat()
    assert galaxy._offbus_stuck_hold_due(before, off_bus=1, now=now) is False
    assert galaxy._offbus_stuck_hold_due("", off_bus=1, now=now) is False
    assert galaxy._offbus_stuck_hold_due("not-a-timestamp", off_bus=1, now=now) is False


def _arm_offbus_stuck_hold(
    monkeypatch,
    *,
    off_bus=1,
    isolated=None,
    sbr_recovers=True,
    sbr_verify_healthy=True,
    reset_result=True,
    beats_after_reset=None,
):
    """Arm the idle OFF-BUS escalation: an off-bus drop past the ceiling, no tenant, no cooldown, the
    bridge reset / galaxy reset / host rung all mocked to count. Reuses _arm_stuck_hold for the shared
    guards + galaxy-reset + host-rung mocks, then overrides the heartbeats to a drop, arms the off-bus
    env, and mocks the surgical bridge-reset path. Returns the counters dict, extended with 'sbr'."""
    present = max(0, 32 - off_bus)
    counters = _arm_stuck_hold(
        monkeypatch,
        beats={str(i): 100 for i in range(present)},
        reset_result=reset_result,
        beats_after_reset=beats_after_reset,
    )
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "1")
    srv.device_hold_episode_reason = "gate/post-job: 1/32 off-bus below the reset floor — held, not reset"
    counters["sbr"] = 0
    srv.isolated_chips = set(isolated or set())

    async def recover(log):
        counters["sbr"] += 1
        return sbr_recovers

    async def verify(expected, log, run_fabric=True, **_):
        return sbr_verify_healthy, {"snapshot": {"ok": sbr_verify_healthy}}

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)
    patch_recovery(monkeypatch, "_verify_device", verify)
    return counters


@pytest.mark.asyncio
async def test_stuck_offbus_hold_escalates_to_a_reset_when_nothing_is_isolated(monkeypatch, clear_job_state):
    """A mass drop with no isolated (all-ones) chip: no surgical candidate, and at/above the floor the
    galaxy reset is applicable, so the escalation runs it and — on recovery — retires the fault and
    clears the hold, exactly as the gate's cascade does."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=16, isolated=set())
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert (
        counters["sbr"] == 0 and counters["resets"] == 1
    ), "nothing isolated, at/above the floor -> no bridge reset, straight to the galaxy reset"
    assert counters["fault_cleared"] == 1 and counters["dirty_cleared"] == 1


@pytest.mark.asyncio
async def test_stuck_offbus_hold_prefers_the_surgical_bridge_reset(monkeypatch, clear_job_state):
    """An isolated (all-ones) chip is reset through its own bridge first — it spares the other 31 and
    the mesh-wide reset is never reached when the surgical one recovers."""
    counters = _arm_offbus_stuck_hold(monkeypatch, isolated={"8"}, sbr_recovers=True, sbr_verify_healthy=True)
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert (
        counters["sbr"] == 1 and counters["resets"] == 0
    ), "an isolated chip takes the surgical bridge reset, not the galaxy reset"
    assert counters["fault_cleared"] == 1 and counters["dirty_cleared"] == 1


# --- GalaxyRecovery.escalate() — outcome mapping ------------------------------------------------
#
# escalate() is the thin dispatcher _attempt_idle_relift and _force_escalate_stuck_hold call
# instead of _escalate_stuck_hold/_escalate_offbus_stuck_hold directly. Its own guard/dispatch
# logic is exercised end-to-end by every test above (through those two methods); these pin only
# its added value: the phase parse and the WAITING/RECOVERED/TERMINAL mapping.


def _make_clear_fns_real(monkeypatch, counters):
    """Replace _arm_stuck_hold's/_arm_offbus_stuck_hold's counter-only _clear_device_dirty/
    _clear_device_reported_fault with versions that ALSO flip the real device_dirty/
    device_unverified_why/device_fault_reported globals escalate()'s device_degraded() reads —
    the counters alone never touch them, so a test asserting on the OUTCOME (not just the call
    count) needs the flags to actually move."""

    def _clear_dirty(**kw):
        counters["dirty_cleared"] += 1
        fsm_healthy(srv)

    def _clear_fault(why):
        counters["fault_cleared"] += 1
        srv.device_fault_reported = ""

    monkeypatch.setattr(srv, "_clear_device_dirty", _clear_dirty)
    monkeypatch.setattr(srv, "_clear_device_reported_fault", _clear_fault)


@pytest.mark.asyncio
async def test_escalate_waits_when_nothing_ran(monkeypatch, clear_job_state):
    """A guard blocks the escalation (a tenant on the device) — nothing ran, so escalate() reports
    WAITING, not a verdict on the mesh."""
    counters = _arm_stuck_hold(
        monkeypatch, scan=HolderScan(holders=[DeviceHolder(pid=1, uid=srv.MIN_TENANT_UID)], complete=True)
    )
    outcome = await srv.galaxy_recovery.escalate("present", ["0", "1"], 32, lambda m: None)
    assert outcome == OUTCOME_WAITING
    assert counters["resets"] == 0


@pytest.mark.asyncio
async def test_escalate_recovers_on_a_present_mesh_reset(monkeypatch, clear_job_state):
    """The present-mesh ladder's own reset recovers the mesh — device_degraded() reads the flags
    that reset just cleared, so escalate() reports RECOVERED."""
    counters = _arm_stuck_hold(monkeypatch)
    _make_clear_fns_real(monkeypatch, counters)
    fsm_dirty(srv, "test")
    outcome = await srv.galaxy_recovery.escalate("present", ["0", "1"], 32, lambda m: None)
    assert outcome == OUTCOME_RECOVERED
    assert counters["resets"] == 1 and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_escalate_reports_terminal_when_a_present_mesh_reset_fails_and_climbs(monkeypatch, clear_job_state):
    """The reset ran but did not recover the mesh (it climbs to the host rung instead) — the mesh is
    still degraded, so escalate() reports TERMINAL even though something ran."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False)
    fsm_dirty(srv, "test")
    outcome = await srv.galaxy_recovery.escalate("present", ["0", "1"], 32, lambda m: None)
    assert outcome == OUTCOME_TERMINAL
    assert counters["resets"] == 1 and srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_escalate_recovers_via_bridge_reset_despite_a_stale_failed_reset_flag(monkeypatch, clear_job_state):
    """Regression for the finding that escalate() used to read ``mechanism.last_reset_failed`` for
    its verdict: that flag is written ONLY by _reset_and_verify_device, but the off-bus ladder can
    recover the mesh via the surgical bridge reset WITHOUT ever calling it. Arm an UNRELATED earlier
    reset as already failed (the stale flag), then let this escalation recover through the bridge
    reset alone — escalate() must still report RECOVERED, not TERMINAL, because the mesh itself is
    no longer degraded."""
    counters = _arm_offbus_stuck_hold(monkeypatch, isolated={"8"}, sbr_recovers=True, sbr_verify_healthy=True)
    _make_clear_fns_real(monkeypatch, counters)
    fsm_dirty(srv, "test")
    # Stale: some OTHER reset failed earlier. Stamp it outside the cooldown too — the flag alone
    # leaves last_reset_monotonic at 0.0, and cooling() measures against time.monotonic(), so
    # "how long ago" would be answered by the host's uptime: an armed cooldown under 600s of it.
    srv.recovery_mechanism.last_reset_failed = True
    srv.recovery_mechanism.last_reset_monotonic = time.monotonic() - recovery_base.RESET_COOLDOWN_SEC - 1
    outcome = await srv.galaxy_recovery.escalate("offbus", ["0", "1"], 32, lambda m: None)
    assert (
        outcome == OUTCOME_RECOVERED
    ), "the bridge reset recovered the mesh; a stale last_reset_failed from an unrelated reset must not sink it"
    assert counters["sbr"] == 1 and counters["resets"] == 0 and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_escalate_forced_suffix_bypasses_the_retry_pacing(monkeypatch, clear_job_state):
    """The "-forced" suffix is what _force_escalate_stuck_hold relies on to bypass the guarded
    defers — on the present ladder, the grace-cadence retry pacing. Unforced, a pass inside the
    grace window since the last reset reports WAITING; the same phase with "-forced" reaches the
    reset and reports RECOVERED."""
    counters = _arm_stuck_hold(monkeypatch)
    _make_clear_fns_real(monkeypatch, counters)
    fsm_dirty(srv, "test")
    srv.galaxy_recovery.mechanism.last_reset_monotonic = time.monotonic()  # a reset just ran
    waited = await srv.galaxy_recovery.escalate("present", ["0", "1"], 32, lambda m: None)
    assert waited == OUTCOME_WAITING and counters["resets"] == 0
    forced = await srv.galaxy_recovery.escalate("present-forced", ["0", "1"], 32, lambda m: None)
    assert forced == OUTCOME_RECOVERED and counters["resets"] == 1


@pytest.mark.asyncio
async def test_escalate_rejects_an_unknown_phase(monkeypatch, clear_job_state):
    """An unrecognized phase must fail closed, never silently fall into the destructive present-
    mesh ladder (a typo like "off-bus" for "offbus" must not fire a galaxy reset)."""
    _arm_stuck_hold(monkeypatch)
    with pytest.raises(ValueError):
        await srv.galaxy_recovery.escalate("off-bus", ["0", "1"], 32, lambda m: None)


@pytest.mark.asyncio
async def test_escalate_does_not_report_recovered_when_the_mesh_was_never_degraded(monkeypatch, clear_job_state):
    """The RECOVERED/TERMINAL split is a TRANSITION (degraded-before, not-degraded-after), not a
    bare post-call read: a rung that runs against an ALREADY-CLEAN mesh must not report RECOVERED
    just because the mesh also reads clean afterward — it recovered nothing, because there was
    nothing to recover."""
    counters = _arm_stuck_hold(monkeypatch)
    assert srv.fsm.state is ServerState.HEALTHY and not srv.device_fault_reported
    outcome = await srv.galaxy_recovery.escalate("present", ["0", "1"], 32, lambda m: None)
    assert outcome == OUTCOME_TERMINAL
    assert counters["resets"] == 1


@pytest.mark.asyncio
async def test_escalate_recovers_via_a_ubb_tray_reset(monkeypatch, clear_job_state, galaxy_trays):
    """Closes the UBB-tray-recovered branch's escalate()-level coverage gap: the per-tray BMC
    reset (neither the surgical bridge reset nor the mesh-wide reset) is what clears the hold, and
    escalate() must report RECOVERED for it exactly as for the other two recovering rungs."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=8, isolated=set())  # chips 24-31 = tray 3
    _make_clear_fns_real(monkeypatch, counters)
    fsm_dirty(srv, "test")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16, 8 is below it
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, chip_ids: None)

    async def verified_after_reset(expected, log):
        return True, {"snapshot": {"ok": True}}, 0

    patch_recovery(monkeypatch, "_verify_device_after_reset", verified_after_reset)
    outcome = await srv.galaxy_recovery.escalate("offbus", ["0", "1"], 32, lambda m: None)
    assert outcome == OUTCOME_RECOVERED
    assert counters["sbr"] == 0 and counters["resets"] == 0 and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_escalate_reports_terminal_when_a_below_floor_tray_down_fails_the_mesh_reset(
    monkeypatch, clear_job_state
):
    """Closes the below-floor climb's escalate()-level coverage gap: the per-tray fire is opted out,
    the mesh-wide reset gets its below-floor attempt and does not recover, and the climb (past the
    ceiling, nothing opted in) only names the host rung — nothing recovered, so escalate() must
    report TERMINAL even though something ran."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=8, isolated=set(), reset_result=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    fsm_dirty(srv, "test")
    outcome = await srv.galaxy_recovery.escalate("offbus", ["0", "1"], 32, lambda m: None)
    assert outcome == OUTCOME_TERMINAL
    assert counters["sbr"] == 0 and counters["resets"] == 1


@pytest.mark.asyncio
async def test_stuck_offbus_hold_falls_to_the_galaxy_reset_when_the_bridge_reset_does_not_recover(
    monkeypatch, clear_job_state
):
    """The surgical rung is tried first but is not the last word: on a mass drop at/above the floor, a
    bridge reset the mesh does not come back from falls through to the mesh-wide galaxy reset, same as
    the gate."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=16, isolated={"8"}, sbr_recovers=True, sbr_verify_healthy=False
    )
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert (
        counters["sbr"] == 1 and counters["resets"] == 1
    ), "a bridge reset that did not verify healthy must fall to the galaxy reset at/above the floor"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_routes_a_gone_chip_to_the_gentle_bridge_reset(monkeypatch, tmp_path, clear_job_state):
    """The blx04 strand: a chip GONE from sysfs (never isolated/all-ones), below the galaxy-reset
    floor, on an idle box must take the gentle per-chip bridge reset first — not fall straight to the
    mesh-inverting galaxy reset, which here is unbounded (the host-reboot rung is opted out). Mirrors
    the gate's gone-chip routing, behind the same opt-in."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=1, isolated=set(), sbr_recovers=True, sbr_verify_healthy=True
    )
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path)  # chip "31" node absent -> gone
    monkeypatch.setattr(srv, "GONE_CHIP_CONFIRM_SETTLE_SEC", 0)  # no real settle in a unit test
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")  # opt in to the gone-chip rung

    seen = {"isolated": None}

    async def recover(log):
        counters["sbr"] += 1
        seen["isolated"] = set(srv.isolated_chips)
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert (
        counters["sbr"] == 1 and counters["resets"] == 0
    ), "a gone chip must take the surgical bridge reset, not the mesh-wide galaxy reset"
    assert seen["isolated"] == {"31"}, "the gone chip must be the one queued for the bridge reset"
    assert counters["fault_cleared"] == 1 and counters["dirty_cleared"] == 1


@pytest.mark.asyncio
async def test_stuck_offbus_hold_below_floor_climbs_loud_when_no_host_rung_opted_in(monkeypatch, clear_job_state):
    """A gone chip below the galaxy-reset floor with the per-chip bridge reset NOT opted in has no
    applicable surgical rung: the ladder TRIES the mesh-wide reset anyway (every rung gentler than the
    host pair gets its attempt — the ladder ends in the cold rung regardless, so the inversion risk buys
    a chance at recovery). A reset that did not recover climbs; with no host rung opted in the climb
    fails closed LOUDLY (reset_unrecoverable_power_cycle_required), never a silent hold."""
    events = []
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=8, isolated=set(), reset_result=False)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True, "a below-floor drop past the grace must climb, never hold indefinitely"
    assert (
        counters["sbr"] == 0 and counters["resets"] == 1
    ), "no surgical candidate -> the mesh reset gets its attempt, below the floor included"
    assert "stuck_hold_galaxy_reset_below_floor_attempt" in events
    assert (
        "reset_unrecoverable_power_cycle_required" in events
    ), "with no host rung opted in the climb must be loud, never a silent hold"
    assert (
        counters["power_cycles"] == 0 and counters["reboots"] == 0
    ), "nothing opted in -> no host rung fires, but the box is loud about needing one"
    assert (
        counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0
    ), "an unrecovered climb must not clear the fault or release the hold"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_below_floor_fires_the_power_cycle_when_opted_in(monkeypatch, clear_job_state):
    """The directive, positively: a below-floor off-bus drop (one UBB tray) that stood the FULL ladder
    and could not be recovered by any gentler rung MUST reach the cold power cycle when opted in —
    never hold a degraded box. The mesh reset gets its attempt first (below the floor included), and
    the warm reboot is skipped (it cannot re-enumerate a dropped ASIC); the terminal rung is the
    chassis power cycle, fired only past the hold ceiling."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")  # cold rung opted in
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")  # tray reset named, not fired
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "the mesh reset gets its attempt before the cold rung"
    assert (
        counters["power_cycles"] == 1
    ), "a below-floor drop past the full ladder must reach the cold power cycle, never hold"
    assert (
        counters["reboots"] == 0
    ), "a dropped ASIC cannot be re-enumerated by a warm reboot — straight to the cold rung"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_below_floor_tries_the_mesh_reset_and_recovers(monkeypatch, clear_job_state):
    """The rung the old ladder skipped: an off-bus drop below the galaxy-reset floor with no surgical
    candidate gets the mesh-wide reset ANYWAY — a reset that recovers retires the fault and releases
    the hold without any host rung ever firing. Fails on a ladder that suppresses the below-floor
    reset and jumps to the host rung (the observed 2-minute power-cycle)."""
    events = []
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=1, isolated=set(), reset_result=True)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "the below-floor drop gets its mesh-reset attempt"
    assert "stuck_hold_galaxy_reset_below_floor_attempt" in events
    assert (
        counters["power_cycles"] == 0 and counters["reboots"] == 0
    ), "a reset that recovered means no host rung fires at all"
    assert (
        counters["fault_cleared"] == 1 and counters["dirty_cleared"] == 1
    ), "a recovered mesh retires the fault and releases the hold"


@pytest.mark.asyncio
async def test_below_floor_mesh_reset_fires_immediately_but_host_rungs_wait_for_the_ceiling(
    monkeypatch, clear_job_state
):
    """ladder-v2: the risky-reset floor is gone. A below-floor drop's mesh-wide reset fires on the
    FIRST forced off-bus pass — a reset brings a recoverable drop back at once, so there is nothing
    to gain by waiting — while a failed attempt still defers the host rungs to the hold ceiling. So a
    young hold gets its glx reset now, and the power cycle only once the hold has stood the full
    ladder. Fails on a ladder that routes a 3-minute-old hold straight to the cold rung."""
    events = []
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=1, isolated=set(), reset_result=False)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")  # cold rung armed — must still wait
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=3)).isoformat()  # < 600s ceiling

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert ran is True, "the below-floor mesh reset fires now, no risky-floor wait"
    assert counters["resets"] == 1, "the below-floor mesh reset fires on the first forced pass"
    assert "stuck_hold_galaxy_reset_below_floor_attempt" in events
    assert (
        counters["power_cycles"] == 0 and counters["reboots"] == 0
    ), "no host rung before the hold has stood the full ladder"
    assert "host_rung_deferred_until_ladder_end" in events


@pytest.mark.asyncio
async def test_below_floor_hold_past_the_ceiling_climbs_to_the_power_cycle(monkeypatch, clear_job_state):
    """The end of the ladder-v2 below-floor path: past the (now 10-min) ceiling the mesh reset fires
    AND, having failed, the hold climbs straight to the cold power cycle — a below-floor drop that
    survived a reset is warm-reboot-futile (a 6U-Galaxy reboot does not re-power the UBBs)."""
    events = []
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=1, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=12)).isoformat()  # past the 600s ceiling

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert ran is True
    assert counters["resets"] == 1, "the below-floor mesh reset still fires before the power cycle"
    assert "stuck_hold_galaxy_reset_below_floor_attempt" in events
    assert counters["power_cycles"] == 1, "past the ceiling the failed reset climbs to the cold rung"
    assert counters["reboots"] == 0, "a below-floor drop is warm-reboot-futile — never the warm reboot"


@pytest.mark.asyncio
async def test_present_mesh_failed_reset_defers_host_rung_before_ladder_end(monkeypatch, clear_job_state):
    """The present-mesh twin of the ladder-end bound: an eth-wedged mesh whose galaxy reset ran at the
    grace and did not verify must NOT reboot there — the host rungs wait for the hold ceiling, the
    deferral is journaled, and the grace cadence keeps retrying the reset meanwhile."""
    events = []
    counters = _arm_stuck_hold(monkeypatch, held_minutes_ago=6, reset_result=False)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")

    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "the grace-time galaxy reset still fires"
    assert counters["reboots"] == 0 and counters["power_cycles"] == 0, "a 6-minute-old hold must not reach a host rung"
    assert "host_rung_deferred_until_ladder_end" in events


# (ladder-v2 removed the watchdog's separate risky-floor window; the general ceiling window is now
# the one forced run past the early off-bus one-shot — see test_hold_past_ceiling_triggers_forced_escalation.)


@pytest.mark.asyncio
async def test_stuck_offbus_hold_still_galaxy_resets_at_the_floor(monkeypatch, clear_job_state):
    """The boundary the suppression must not overreach: exactly AT the floor is a mass drop, so the
    galaxy reset is applicable and still fires. Guards F12 against over-suppressing an at/above-floor
    drop that the reset can legitimately recover."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=16, isolated=set())
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "a drop at the floor is a mass drop — the galaxy reset still fires"


def test_hold_past_ceiling_triggers_forced_escalation(monkeypatch, clear_job_state):
    """The teeth: _check_hold_deadline must SPAWN the forced escalation once a hold outlives the
    ceiling — a held box is tenant-free, so no gate may keep it holding. Fails on base, where the
    watchdog only journals and never escalates (the 21h blx04 hold)."""
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=25)).isoformat()  # past 20-min ceiling
    srv.device_hold_episode_reason = "1/32 off the bus, below the reset floor — held"
    srv.device_hold_escalate_bucket = 0
    srv.device_hold_deadline_bucket = 0
    srv.device_op_active = ""
    patch_health_event(monkeypatch, lambda *a, **k: None)
    spawned = {"n": 0}
    # returns True: a spawn that actually started. The caller consumes its window only on one.
    monkeypatch.setattr(
        srv, "_maybe_spawn_forced_escalation", lambda: spawned.__setitem__("n", spawned["n"] + 1) or True
    )
    srv._check_hold_deadline()
    assert spawned["n"] == 1, "a hold past the ceiling must trigger the forced escalation"
    assert srv.device_hold_escalate_bucket >= 1, "the escalate bucket must advance so it fires once per window"


@pytest.mark.asyncio
async def test_forced_offbus_escalation_bypasses_the_once_per_episode_latch(monkeypatch, clear_job_state):
    """The exact gate that held blx04 21h: the once-per-episode latch (device_hold_episode_escalated).
    Without force it blocks a second escalation — the hold sits forever. WITH force (the deadline
    backstop on a tenant-free held box) the gate's OWN ladder runs anyway: the tray reset is named,
    and when it cannot recover the below-floor drop it CLIMBS to the cold rung. Not a jump to power
    cycle — it climbs only because the gentler rung failed. Fails on base (no force param)."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")  # name the tray rung, do not fire it
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    # Latch through the writer, not fsm.set_latch directly: the predicate the escalator reads
    # (_hold_escalation_latched) also checks the expiry clock, and a raw latch with a zero stamp
    # reads as already re-armed.
    srv._set_hold_escalated(True)  # this episode's one recovery already ran

    gated = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert gated is False, "the once-per-episode latch must block a NON-forced re-escalation"
    assert counters["power_cycles"] == 0, "a latched, non-forced escalation must do nothing"

    forced = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert forced is True, "force must bypass the once-per-episode latch and run the ladder"
    assert counters["resets"] == 1, "the mesh reset gets its below-floor attempt before any host rung"
    assert (
        counters["power_cycles"] == 1
    ), "the forced below-floor ladder climbs to the power cycle once every gentler rung failed"


@pytest.mark.asyncio
async def test_force_escalate_present_mesh_galaxy_resets_then_climbs(monkeypatch, clear_job_state, tmp_path):
    """A present-mesh / mass-drop hold past the ceiling: the forced escalation runs the galaxy reset
    (authoritative for a present-mesh eth wedge); if it does not recover, it climbs. Here the reset
    fails, so it must climb to the host rung."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=0, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    # _force_escalate_stuck_hold reads the real /dev/tenstorrent for its indices; mock it (and the
    # count) so the present-mesh path is exercised on any runner, not just one that happens to expose
    # nodes — the sibling forced-escalation tests do the same. Without it a device-less runner reads no
    # indices, expected falls to 0, and the escalation returns before any reset.
    dev = tmp_path / "tenstorrent"
    dev.mkdir()
    for i in range(32):
        (dev / str(i)).mkdir()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: present)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    fsm_dirty(srv, "gate/post-job: eth/fabric fault on a present mesh — held", why="eth_frozen")
    await srv._force_escalate_stuck_hold()
    assert counters["resets"] == 1, "a present-mesh hold past the ceiling gets the authoritative galaxy reset"
    assert counters["reboots"] + counters["power_cycles"] >= 1, "a reset that did not recover must climb"


@pytest.mark.asyncio
async def test_force_escalate_has_teeth_on_a_per_target_host(monkeypatch, clear_job_state, tmp_path):
    """The forced escalation must fire on a NON-Galaxy host too. The idle ladders live on
    GalaxyRecovery but apply on every platform (the reset argv is re-derived per platform inside
    _reset_and_verify_device); a per-target host's own escalate() has no ladder and reports WAITING
    forever, so routing the teeth through select_recovery makes them a silent no-op there: the hold
    outlives every ceiling with no rung ever attempted, no event, no log — an eth-wedged 8-chip box
    stands HELD until a human resets it. select_recovery resolves to per-target here, exactly as it
    does on a loudbox, and the reset must still fire."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=0, isolated=set(), reset_result=True)
    dev = tmp_path / "tenstorrent"
    dev.mkdir()
    for i in range(8):
        (dev / str(i)).mkdir()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: present)
    monkeypatch.setattr(srv, "select_recovery", lambda: srv.per_target_recovery)
    fsm_dirty(
        srv,
        "gate/post-job: eth/fabric fault on a present mesh (all 8 chips on the bus) — " "held for self-heal, not reset",
        why="eth_frozen",
    )
    await srv._force_escalate_stuck_hold()
    assert (
        counters["resets"] == 1
    ), "a per-target host past the ceiling must still fire the reset — never a silent WAITING"
    assert counters["dirty_cleared"] == 1, "the recovered mesh must clear the hold"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_skips_the_surgical_reset_when_a_scope_opened(monkeypatch, clear_job_state):
    """The surgical bridge reset ends in a system-wide PCI rescan, so it must not run concurrently
    with a foreign reset already cycling in its own scope — two resets at once. The scope can open
    between the top guard and this rung (the gone-chip settle sleeps in that window), so the rung
    re-checks: a scope live here skips the SBR and falls to the galaxy reset, which waits the scope
    out. Isolated chip present + scope inactive at the top guard but live at the surgical rung."""
    counters = _arm_offbus_stuck_hold(monkeypatch, isolated={"8"}, sbr_recovers=True, sbr_verify_healthy=True)
    calls = {"n": 0}

    def scope():
        calls["n"] += 1
        return None if calls["n"] == 1 else "ttdev-reset-999.scope"  # clear at top, live at the rung

    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", scope)

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert (
        counters["sbr"] == 0 and counters["resets"] == 1
    ), "a foreign reset scope live at the surgical rung must skip the SBR and fall to the galaxy reset"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_holds_under_a_foreign_tenant(monkeypatch, clear_job_state):
    """The absolute guard, on the off-bus path too: a foreign tenant blocks every recovery action —
    never take a box's mesh out from under a running job."""
    counters = _arm_offbus_stuck_hold(monkeypatch, isolated={"8"})
    monkeypatch.setattr(
        srv,
        "enumerate_device_holders",
        lambda: HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True),
    )
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert (
        counters["sbr"] == 0 and counters["resets"] == 0
    ), "a foreign tenant must block the off-bus escalation entirely"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_never_escalates_a_present_mesh(monkeypatch, clear_job_state):
    """The off-bus path must not double-arm the present-mesh class: with every chip present
    (off_bus == 0) it is not due here, so no recovery runs even past the ceiling and idle."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=0, isolated=set())
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert counters["sbr"] == 0 and counters["resets"] == 0


@pytest.mark.asyncio
async def test_stuck_offbus_hold_is_inert_when_kill_switched(monkeypatch, clear_job_state):
    """The kill-switch: with TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE=0 a stuck off-bus hold past the
    ceiling, idle, is left standing — no recovery runs. The operator's only opt-out of the default."""
    counters = _arm_offbus_stuck_hold(monkeypatch, isolated={"8"})
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "0")
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is False
    assert (
        counters["sbr"] == 0 and counters["resets"] == 0
    ), "kill-switched: the off-bus escalation must not perturb the device"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_resets_at_most_once_per_episode(monkeypatch, clear_job_state):
    """One recovery per hold episode: on a mass drop at/above the floor, a galaxy reset that did not
    recover must not re-fire next window — a reset can itself drop every chip off the bus, so looping
    it courts that drop."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=16, isolated=set(), reset_result=False)
    first = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    second = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert first is True and second is False
    assert counters["resets"] == 1, "the off-bus escalation must recover at most once per episode"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_failed_reset_all_off_bus_holds_loudly_never_reboots(monkeypatch, clear_job_state):
    """The off-bus twin: a mass drop's galaxy reset that inverted it to an all-off-bus mesh (16 off-bus
    goes to 32) must not warm-reboot either — the reboot cannot re-enumerate dropped ASICs. With only
    the reboot opted in the climb holds loudly. Same failed-reset climb the present-mesh path uses.
    Fails on base, which warm-reboots the whole-bus wedge instead of holding for the cold rung."""
    events = []
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=16, isolated=set(), reset_result=False, beats_after_reset={})
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["reboots"] == 0 and counters["power_cycles"] == 0
    ), "an all-off-bus mesh must never warm-reboot — the reboot cannot re-enumerate dropped ASICs"
    assert (
        "all_chips_off_bus_power_cycle_required" in events
    ), "a whole-bus wedge with no power cycle opted in must emit the loud actionable event"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_reset_regression_holds_loudly_never_reboots(monkeypatch, clear_job_state):
    """The idle twin of the gate's regression guard. An at/above-floor drop (16 off-bus) whose galaxy
    reset GREW the drop but stopped short of all-off-bus (16 -> 28 of 32) must not warm-reboot: a
    6U-Galaxy reboot cannot re-enumerate the ASICs the reset just dropped. With only the reboot opted
    in the climb fires NO rung — it flags the regression and names the cold rung, holding. Fails on
    base, which warm-reboots the partial regression because the idle climb never saw the baseline."""
    events = []
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=16, isolated=set(), reset_result=False, beats_after_reset={str(i): 100 for i in range(4)}
    )  # 28 off-bus
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # galaxy + reboot floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["reboots"] == 0
    ), "a reset that regressed the mesh off the bus must never warm-reboot on the idle path"
    assert "reset_regressed_offbus" in events, "the regression must be flagged loudly"
    assert (
        "reset_unrecoverable_power_cycle_required" in events
    ), "the cold rung must be named for the held partial regression"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_reset_regression_power_cycles_when_opted_in(monkeypatch, clear_job_state):
    """With the chassis power cycle opted in, an idle reset regression (16 -> 28) skips straight to the
    cold rung — never the warm reboot first (the rung that cannot re-enumerate it). Base picks the
    reboot (no prior reboot in the ledger), so it warm-reboots instead."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch,
        off_bus=16,
        isolated=set(),
        reset_result=False,
        beats_after_reset={str(i): 100 for i in range(4)},
        sbr_verify_healthy=False,
    )  # 28 off-bus
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # galaxy + reboot floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["power_cycles"] == 1 and counters["reboots"] == 0
    ), "an idle reset regression with the cold rung opted in must power-cycle, not reboot"


@pytest.mark.asyncio
async def test_stuck_offbus_hold_hard_failed_reset_holds_loudly_never_reboots(monkeypatch, clear_job_state):
    """A reset that EXITED non-zero (a real reset_done rc=1), leaving chips off the bus without growing
    the count, is also warm-reboot-futile on the idle path: the reset could not even run, and the
    off-bus ASICs stay off across a reboot. Route it to the cold rung, held — not a warm reboot. Base
    warm-reboots. No regression, so only the unrecoverable event fires, not reset_regressed_offbus."""
    events = []
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=16, isolated=set())
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # galaxy + reboot floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    async def hard_failing_reset(indices, log):
        counters["resets"] += 1
        srv.recovery_mechanism.last_reset_exit_nonzero = True  # the reset command hard-exited; off_bus stays 16
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", hard_failing_reset)
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert (
        counters["reboots"] == 0
    ), "a hard-failed reset leaving chips off the bus must never warm-reboot on the idle path"
    assert "reset_unrecoverable_power_cycle_required" in events, "the cold rung must be named for the held drop"
    assert "reset_regressed_offbus" not in events, "an unchanged off-bus count is not a regression"


# --- F13: the per-UBB tray rung (a tray-down named as the lightest sufficient recovery) ---


def test_ubb_reset_plan_maps_a_clean_whole_tray_drop_to_its_bitmap(galaxy_trays):
    """A drop confined to whole trays maps to those trays and a bitmap. Tray numbers and bits come
    from the chips' bus groups (I16): chips 0-7 are tray 1, 24-31 are tray 3, and 16-23 are tray 4
    — so a 0-7 + 16-23 drop is trays 1 and 4, bits 0 and 3."""
    assert galaxy._ubb_reset_plan({str(i) for i in range(8)}, 32, galaxy_trays) == ([1], 0x01)
    assert galaxy._ubb_reset_plan({str(i) for i in range(24, 32)}, 32, galaxy_trays) == ([3], 0x04)
    drop = {str(i) for i in list(range(8)) + list(range(16, 24))}
    assert galaxy._ubb_reset_plan(drop, 32, galaxy_trays) == ([1, 4], 0x09)


def test_ubb_reset_plan_rejects_a_partial_or_scattered_drop(galaxy_trays):
    """The tray rung fits only a clean tray-down. A partial tray (half of tray 3 still up) or a chip
    off-bus outside an otherwise-down tray would have the reset either miss it or take out live
    silicon, so the plan is None and the drop falls through to the per-chip rung / hold."""
    assert galaxy._ubb_reset_plan({"28", "29", "30", "31"}, 32, galaxy_trays) is None  # half of tray 3 up
    assert galaxy._ubb_reset_plan({str(i) for i in range(8)} | {"20"}, 32, galaxy_trays) is None  # tray 1 + a stray
    assert galaxy._ubb_reset_plan(set(), 32, galaxy_trays) is None  # nothing off-bus


def test_ubb_reset_plan_needs_multi_tray_topology(galaxy_trays):
    """Only a host with at least two full trays has the tray-reset failure mode; a single-tray or
    non-tray-aligned chip count (an n300, a 20-chip host) gets None so the rung never fires there."""
    assert galaxy._ubb_reset_plan({"0", "1"}, 2, galaxy_trays) is None  # not multi-tray
    assert galaxy._ubb_reset_plan({str(i) for i in range(8)}, 8, galaxy_trays) is None  # one tray only
    assert galaxy._ubb_reset_plan({str(i) for i in range(8)}, 20, galaxy_trays) is None  # 20 not a multiple of 8


# --- F18: the tray rung broadened — walk ANY affected tray one at a time, not clean-tray-down only ---


def test_affected_trays_maps_any_off_bus_chip_to_its_tray(galaxy_trays):
    """F18: every off-bus chip belongs to a tray, so the tray rung is no longer clean-tray-down-only. A
    lone chip, a partial-tray drop, and a whole tray all resolve to the tray(s) that hold the fault."""
    assert galaxy._affected_trays({str(i) for i in range(8)}, 32, galaxy_trays) == [1]  # a whole tray
    assert galaxy._affected_trays({"28", "29", "30", "31"}, 32, galaxy_trays) == [3]  # half of tray 3
    assert galaxy._affected_trays({"20"}, 32, galaxy_trays) == [4]  # one lone chip -> its tray
    assert galaxy._affected_trays({"0", "31"}, 32, galaxy_trays) == [1, 3]  # two affected trays, sorted


def test_affected_trays_rejects_a_non_tray_topology_or_bad_drop(galaxy_trays):
    """No tray to re-power: an empty drop, a single-tray or non-8-aligned host, and a malformed or
    out-of-range id all resolve to None so the rung never fires there."""
    assert galaxy._affected_trays(set(), 32, galaxy_trays) is None  # nothing off-bus
    assert galaxy._affected_trays({str(i) for i in range(8)}, 8, galaxy_trays) is None  # one tray only
    assert galaxy._affected_trays({"0"}, 20, galaxy_trays) is None  # 20 not a multiple of 8
    assert galaxy._affected_trays({"99"}, 32, galaxy_trays) is None  # id out of range
    assert galaxy._affected_trays({"x"}, 32, galaxy_trays) is None  # malformed id


def test_ubb_tray_walk_plan_orders_affected_trays_first_then_the_rest(galaxy_trays):
    """F18: the walk re-powers the trays holding a fault first, then sweeps the rest — one tray at a
    time, so a full pass never takes the whole mesh off the bus at once (the mesh reset's failure mode).
    The order is affected then the remaining trays, each ascending by real tray number."""
    assert galaxy._ubb_tray_walk_plan({str(i) for i in range(8)}, 32, galaxy_trays) == [1, 2, 3, 4]
    assert galaxy._ubb_tray_walk_plan({str(i) for i in range(24, 32)}, 32, galaxy_trays) == [3, 1, 2, 4]
    assert galaxy._ubb_tray_walk_plan({"20"}, 32, galaxy_trays) == [4, 1, 2, 3]  # a lone chip -> its tray leads
    assert galaxy._ubb_tray_walk_plan({"0", "31"}, 32, galaxy_trays) == [1, 3, 2, 4]  # two affected trays lead
    assert galaxy._ubb_tray_walk_plan(set(), 32, galaxy_trays) is None  # no tray to re-power
    assert galaxy._ubb_tray_walk_plan({str(i) for i in range(8)}, 8, galaxy_trays) is None  # single-tray host


def test_maybe_emit_ubb_reset_required_names_the_exact_bmc_command_on_a_tray_down(monkeypatch, galaxy_trays):
    """A tray-down emits the loud actionable event carrying the exact ipmitool command an operator
    must run — the one the opted-in fire (F13b) would issue — and leaves the hold to the caller.
    Chips 24-31 are tray 3, which is bit 2, so the named command must carry 0x04."""
    calls = []
    patch_health_event(monkeypatch, lambda name, *a, **k: calls.append((name, k)))
    beats = {str(i): 100 for i in range(24)}  # chips 24-31 (tray 3) off the bus
    trays = galaxy._maybe_emit_ubb_reset_required(beats, 8, 32, lambda m: None, galaxy_trays)
    assert trays == [3]
    names = [c[0] for c in calls]
    assert "ubb_reset_required" in names
    kw = dict(calls[names.index("ubb_reset_required")][1])
    assert kw["command"] == "ipmitool raw 0x30 0x8b 0x04 0xff 0x00 0x0f"
    assert kw["host_at_risk"] is True and kw["trays"] == [3]


def test_maybe_emit_ubb_reset_required_names_the_affected_tray_even_for_a_lone_chip(monkeypatch, galaxy_trays):
    """F18 broadening: a single off-bus chip is enough to name its tray — every off-bus chip belongs to
    a tray, and re-powering that tray is the lightest recovery. The misfire guard fires only where
    there is no tray to re-power at all: a host with no multi-tray topology."""
    calls = []
    patch_health_event(monkeypatch, lambda name, *a, **k: calls.append(name))
    beats = {str(i): 100 for i in range(31)}  # only chip 31 off — a lone chip in tray 3
    assert galaxy._maybe_emit_ubb_reset_required(beats, 1, 32, lambda m: None, galaxy_trays) == [3]
    assert "ubb_reset_required" in calls
    # A host with no multi-tray topology (a single n300) still gets nothing — no tray to re-power.
    calls.clear()
    assert galaxy._maybe_emit_ubb_reset_required({"0": 100}, 1, 2, lambda m: None, galaxy_trays) is None
    assert "ubb_reset_required" not in calls


@pytest.mark.asyncio
async def test_stuck_offbus_hold_names_the_ubb_tray_rung_then_climbs_on_a_clean_tray_drop(
    monkeypatch, clear_job_state, galaxy_trays
):
    """A whole tray (8 chips) off the bus is below the galaxy-reset floor: the mesh reset must not fire
    (it inverts the drop), and with the per-tray BMC reset forced off (=0; the default is now ON) it is
    only NAMED — the lightest sufficient recovery an operator can run. But naming a rung must never
    become a silent indefinite hold: past the grace the escalation climbs to the host rung, loudly when
    none is opted in. Fails on base, which holds silently."""
    events = []
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated=set(), reset_result=False
    )  # chips 24-31 = tray 3
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")  # force off -> name it, then climb
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True, "a below-floor tray-down past the grace must climb, never hold indefinitely"
    assert (
        counters["sbr"] == 0 and counters["resets"] == 1
    ), "the named-but-unfired tray rung falls to the mesh reset's below-floor attempt"
    assert "ubb_reset_required" in events, "a clean tray-down must name the lightest sufficient recovery"
    assert (
        "stuck_hold_galaxy_reset_below_floor_attempt" in events
    ), "the below-floor mesh reset is attempted, not suppressed, on the stuck-hold ladder"
    assert (
        "reset_unrecoverable_power_cycle_required" in events
    ), "with no host rung opted in the climb must be loud, never a silent hold"
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_gate_below_floor_hold_names_the_ubb_tray_rung_on_a_clean_tray_drop(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """The gate side of the tray-down, made actionable, with the fire forced off. A whole tray (8 chips
    = tray 3) off the bus at a job boundary is below the galaxy-reset floor, so the gate holds it (the mesh
    reset would invert it, the reboot cannot re-enumerate it) — but base holds SILENTLY until the box
    goes idle. It must name the per-tray BMC reset while still holding (no reset, no release). With the
    rung forced off (=0; the default is now ON) this is the name-and-hold path. Fails on base, which
    emits no ubb_reset_required at the gate's below-floor hold."""
    events = []
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=24)  # 24 /dev nodes: past the empty-dir check
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    # chips 24-31 (tray 3) off the bus; 0-23 tick -> off_bus 8, below the floor of 16.
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(24)})
    resets = _count_galaxy_resets(monkeypatch)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")  # force off -> name it and hold
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0, "a below-floor tray-down must hold at the gate, never fire the mesh-inverting galaxy reset"
    assert (
        "ubb_reset_required" in events
    ), "the gate must name the lightest sufficient recovery for a clean tray-down, not hold silently"
    assert (
        "galaxy_reset_suppressed_below_mass_threshold" in events
    ), "the below-floor suppression still holds — the tray event only adds actionability"
    assert srv.fsm.state is not ServerState.HEALTHY, "the tray-down must keep the tenant door shut"


@pytest.mark.asyncio
async def test_ubb_tray_reset_fires_and_recovers_a_clean_tray_down_when_opted_in(
    monkeypatch, clear_job_state, galaxy_trays
):
    """Opted in, a clean below-floor tray-down FIRES the per-tray BMC reset — the lightest sufficient
    recovery — instead of holding. A reset that verifies healthy retires the fault and clears the hold,
    and the mesh-wide galaxy reset (which would invert the below-floor drop) never runs. Fails on base,
    which has no tray-reset rung: the tray-down holds and no fire is issued."""
    fired = {"n": 0}
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated=set(), sbr_verify_healthy=True  # chips 24-31 = tray 3
    )
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    monkeypatch.setattr(
        galaxy, "_fire_ubb_reset", lambda *a, **k: fired.__setitem__("n", fired["n"] + 1), raising=False
    )
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert fired["n"] == 1, "a clean tray-down opted in must fire the per-tray BMC reset"
    assert counters["resets"] == 0, "the tray reset must pre-empt the mesh-wide galaxy reset"
    assert (
        counters["fault_cleared"] == 1 and counters["dirty_cleared"] == 1
    ), "a tray reset that verified healthy retires the fault and clears the hold"


@pytest.mark.asyncio
async def test_ubb_tray_reset_fires_on_a_partial_tray_below_floor_drop(monkeypatch, clear_job_state, galaxy_trays):
    """F18 broadening: opted in, a below-floor drop that is NOT a clean whole-tray-down — half of a
    tray, the rest of that tray still up — now FIRES the per-tray reset on the AFFECTED tray instead of
    holding. Re-powering the tray bounces its live chips too, but that is strictly lighter than the
    32-chip mesh reset (which inverts a partial drop off the bus). chips 28-31 off is half of tray 3.
    Fails on base, whose clean-tray-down-only rung returns None here and holds silently."""
    fired = {"n": 0}
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=4, isolated=set(), sbr_verify_healthy=True  # 28-31 off = half tray 3
    )
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    monkeypatch.setattr(
        galaxy, "_fire_ubb_reset", lambda *a, **k: fired.__setitem__("n", fired["n"] + 1), raising=False
    )
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert fired["n"] == 1, "the affected tray (tray 3) is re-powered even though only half of it dropped"
    assert counters["resets"] == 0, "the tray reset pre-empts the mesh-wide galaxy reset"
    assert counters["fault_cleared"] == 1 and counters["dirty_cleared"] == 1


@pytest.mark.asyncio
async def test_ubb_tray_reset_walk_falls_through_to_the_hold_when_the_whole_sweep_fails(
    monkeypatch, clear_job_state, galaxy_trays
):
    """A per-tray reset walk that re-powered every tray (affected first, then the rest, one at a time)
    and still did not recover must not sit: past the grace it climbs straight to the host rung (a warm
    reboot cannot re-enumerate a still-off-bus tray, so the cold power cycle is the only rung left),
    loudly when none is opted in. The mesh-wide galaxy reset still never runs, and the episode's one
    walk is spent so it does not loop. Fails on base, which falls to a silent below-floor hold."""
    fired = {"n": 0}
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated=set(), sbr_verify_healthy=False, reset_result=False
    )  # no tray reset recovers the mesh
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setattr(
        galaxy, "_fire_ubb_reset", lambda *a, **k: fired.__setitem__("n", fired["n"] + 1), raising=False
    )
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True, "a walk that did not recover must climb, never sit"
    assert fired["n"] == 4, "the walk swept all four trays (affected first, then the rest) one at a time"
    assert counters["resets"] == 1, "a failed walk falls to the mesh reset's below-floor attempt"
    assert counters["dirty_cleared"] == 0, "a walk that did not verify healthy must not clear the hold"
    assert "ubb_reset_did_not_recover" in events
    assert "stuck_hold_galaxy_reset_below_floor_attempt" in events
    assert (
        "reset_unrecoverable_power_cycle_required" in events
    ), "the failed walk climbs loudly to the host rung, never a silent hold"
    assert srv.fsm.latch("ubb_reset_fired") is True, "the episode's one walk is spent"


@pytest.mark.asyncio
async def test_ubb_tray_reset_fires_at_most_once_per_hold_episode(monkeypatch, clear_job_state, galaxy_trays):
    """The per-tray reset carries its own once-per-episode latch — the gate, unlike the idle
    escalation, never sets the general one — so a second attempt in the same episode names the command
    and holds rather than re-powering the tray again."""
    fired = {"n": 0}
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(
        galaxy, "_fire_ubb_reset", lambda *a, **k: fired.__setitem__("n", fired["n"] + 1), raising=False
    )
    beats = {str(i): 100 for i in range(24)}  # chips 24-31 = tray 3 off the bus
    first = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)
    second = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)
    assert (
        first is True and second is None
    ), "the first attempt fires and recovers; the second is latched off and only names the command"
    assert fired["n"] == 1, "at most one per-tray reset per hold episode"


@pytest.mark.asyncio
async def test_ubb_tray_reset_walk_stops_as_soon_as_the_mesh_is_healthy(monkeypatch, clear_job_state, galaxy_trays):
    """F18: the affected tray leads and the walk STOPS the moment the mesh verifies healthy — it does
    not go on to bounce the healthy trays. One affected tray (tray 3), verify healthy after its reset ->
    exactly one fire, of that tray's bitmap."""
    fired = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append(bitmap), raising=False)
    beats = {str(i): 100 for i in range(24)}  # chips 24-31 = tray 3 off the bus
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)
    assert out is True
    assert fired == [0x04], "only the affected tray (tray 3 = bit 2) was re-powered; the walk stopped healthy"


@pytest.mark.asyncio
async def test_ubb_tray_reset_declines_a_fully_off_bus_mesh_it_is_the_cold_rung(monkeypatch, clear_job_state):
    """A fully-off-bus mesh (every chip gone from the bus) is the cold-power-cycle case, never the tray
    rung: a BMC tray reset cannot verify a tray with no chip on the bus, and re-powering it does not
    re-enumerate a dropped ASIC. _attempt_ubb_tray_reset must DECLINE — return None, fire nothing, and
    not spend the episode's one walk — so the caller's all-off-bus cold rung owns it. Fails on base,
    whose walk maps all 32 off-bus chips onto all four trays and re-powers every one."""
    fired = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)

    async def never_healthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {}}

    patch_recovery(monkeypatch, "_verify_device", never_healthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append(bitmap), raising=False)
    beats = {}  # no chip on the bus — the whole 32-chip mesh is off
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 32, 32, lambda m: None)
    assert out is None, "a fully-off-bus mesh is declined to the cold rung, not walked"
    assert fired == [], "no tray is re-powered on an all-off-bus mesh"
    assert srv.fsm.latch("ubb_reset_fired") is False, "declining must not spend the episode's one per-tray walk"


@pytest.mark.asyncio
async def test_ubb_tray_reset_walk_sweeps_the_rest_when_the_affected_tray_does_not_recover(
    monkeypatch, clear_job_state, galaxy_trays
):
    """F18: when re-powering the affected tray does not clear the drop (a fabric wedge spanning trays),
    the walk continues through the remaining trays ONE AT A TIME — a full sweep that never takes the
    whole mesh off the bus at once, unlike the mesh reset. Verify never healthy -> every tray re-powered,
    affected first, then the rest, then False. Fails on base, whose single-fire rung bounces only the
    one clean tray."""
    fired = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append(bitmap), raising=False)
    beats = {str(i): 100 for i in range(24)}  # tray 3 off; 0-23 up
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)
    assert out is False
    assert fired == [
        0x04,
        0x01,
        0x02,
        0x08,
    ], "affected tray 3 first, then the rest one at a time — never all four trays off the bus at once"


@pytest.mark.asyncio
async def test_ubb_tray_reset_walk_does_not_clear_a_hold_on_an_unverified_fabric(
    monkeypatch, clear_job_state, galaxy_trays
):
    """A per-tray reset whose post-reset fabric never verifies (a persistent 77 — the pass found no
    trained link, a training-window verdict, not proof the mesh moves data) is NOT a recovery. The walk
    must not STOP and clear the hold onto an unproven fabric — the blx04 shape, where enum+ARC read OK
    while every full-mesh job died on a down eth link. It gives the 77 training time, then, still
    unverified, treats that tray as not-recovered and sweeps on; a full sweep that never verifies returns
    False and holds. Fails on base, where a 77 reads healthy=True so the first tray's reset stops the
    walk and clears the hold."""
    fired = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRIES", 1)  # bound the training retry
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)  # no real sleep in a unit test
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)

    async def verify_77(expected, log, run_fabric=True, **_):
        return True, {"fabric": {"ok": None, "detail": "no link tested (77)"}}  # persistent 77

    patch_recovery(monkeypatch, "_verify_device", verify_77)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append(bitmap), raising=False)
    beats = {str(i): 100 for i in range(24)}  # chips 24-31 = tray 3 off the bus
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)
    assert out is False, "a persistent post-reset 77 is NOT a verified recovery — the walk must not clear the hold"
    assert fired == [
        0x04,
        0x01,
        0x02,
        0x08,
    ], "an unverified fabric keeps the walk sweeping every tray, never stops-and-clears on the 77"


@pytest.mark.asyncio
async def test_ubb_tray_reset_walk_defers_the_dead_chip_sampler_during_the_transient_drop(
    monkeypatch, clear_job_state, galaxy_trays
):
    """F18 constraint: a tray reset takes its 8 chips transiently off the bus; without reset_in_flight
    held across the walk the dead-chip sampler would isolate a tray mid-reset and tear it out of the
    kernel (mistaking the expected transient drop for a new fault). The flag must be set while
    _fire_ubb_reset runs, and cleared once the walk returns. Fails on base, whose rung never sets it."""
    srv.fsm.set_latch("ubb_reset_fired", False)
    srv.recovery_mechanism.reset_in_flight = False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)
    seen = {"in_flight_during_fire": None}

    def fire(bitmap, ids):
        seen["in_flight_during_fire"] = srv.recovery_mechanism.reset_in_flight

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", fire, raising=False)
    beats = {str(i): 100 for i in range(24)}  # tray 3 off
    await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)
    assert seen["in_flight_during_fire"] is True, "reset_in_flight must be set while the tray reset fires"
    assert srv.recovery_mechanism.reset_in_flight is False, "and cleared once the walk returns"


@pytest.mark.asyncio
async def test_gate_fires_the_ubb_tray_reset_on_a_clean_tray_down_when_opted_in(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """The gate side of the fire: a clean below-floor tray-down at a job boundary, opted in, fires the
    per-tray BMC reset instead of holding, and a reset that verifies healthy clears the tenant door.
    The mesh-wide galaxy reset never runs. Fails on base, which holds and issues no fire."""
    fired = {"n": 0}
    cleared = {"n": 0}
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=24)
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    # chips 24-31 (tray 3) off the bus; 0-23 tick -> off_bus 8, below the floor of 16.
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(24)})
    resets = _count_galaxy_resets(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)

    # The gate's own health decision must see the tray-down (unhealthy) to reach the reset path; the
    # per-tray reset's post-fire verify must then see a recovered mesh. First call unhealthy, rest OK.
    verify_calls = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_calls["n"] += 1
        ok = verify_calls["n"] > 1
        return ok, {"snapshot": {"ok": ok, "detail": "" if ok else "chips 24-31 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **kw: cleared.__setitem__("n", cleared["n"] + 1))
    monkeypatch.setattr(
        galaxy, "_fire_ubb_reset", lambda *a, **k: fired.__setitem__("n", fired["n"] + 1), raising=False
    )

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert fired["n"] == 1, "the gate must fire the per-tray reset on a clean tray-down when opted in"
    assert resets["n"] == 0, "the tray reset pre-empts the mesh-wide galaxy reset"
    assert cleared["n"] >= 1, "a tray reset that verified healthy clears the hold at the gate"


def test_the_tray_reset_rung_is_armed_by_default():
    """A below-floor tray-down that holds forever IS the reported bug, and the per-tray BMC reset is the
    lightest hardware-verified way out of it — only the target tray's ARC restarts, the other 24 chips
    keep running, ~30s, and it does not risk the 8->32 inversion the mesh-wide reset does — so the rung
    is ON by default. Fails on base, which shipped it default-OFF."""
    assert galaxy._ubb_reset_enabled() is True


def test_an_operator_can_still_force_the_tray_reset_rung_off(monkeypatch):
    """The knob survives the default flip: a host whose BMC/ipmitool bit-order an operator has not
    confirmed forces the rung off with =0, back to naming the command and holding."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")
    assert galaxy._ubb_reset_enabled() is False


@pytest.mark.asyncio
async def test_ubb_tray_reset_fires_by_default_on_a_clean_tray_down(monkeypatch, clear_job_state, galaxy_trays):
    """The default flip, end to end: with nothing set (the shipped default), a clean below-floor
    tray-down FIRES the per-tray reset instead of naming it and holding — no opt-in required. Mirrors
    the opted-in fire test but leaves the env at its default. Fails on base, whose default-OFF rung only
    named the command and held here."""
    fired = {"n": 0}
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated=set(), sbr_verify_healthy=True  # chips 24-31 = tray 3
    )
    # Deliberately leave TT_DEVICE_MCP_AUTO_UBB_RESET unset — this IS the shipped default now.
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    monkeypatch.setattr(
        galaxy, "_fire_ubb_reset", lambda *a, **k: fired.__setitem__("n", fired["n"] + 1), raising=False
    )
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert fired["n"] == 1, "the shipped default now fires the per-tray BMC reset without an opt-in"
    assert counters["resets"] == 0, "the tray reset pre-empts the mesh-wide galaxy reset"


@pytest.mark.asyncio
async def test_idle_relift_escalates_a_stuck_off_bus_hold(monkeypatch, tmp_path, clear_job_state):
    """blx04's live gap end-to-end: an off-bus self-heal hold whose chips never self-return reaches the
    relift's still-degraded branch, which base can only hold. Armed and past the ceiling on an idle,
    tenant-free box, the relift now escalates a mass drop (at/above the floor) to the recovery cascade
    instead of stranding. Fails on base, where the still-off-bus branch holds forever and never reaches
    a reset."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=16)  # 16/32 off the bus, a mass drop
    fsm_dirty(srv, "gate/post-job: 16/32 off-bus — a mass drop, held pending recovery", why="off_bus")
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=16, isolated=set())

    async def still_off_bus(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 8 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", still_off_bus)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 1, "an idle off-bus hold past the ceiling must escalate, not sit"


@pytest.mark.asyncio
async def test_idle_relift_still_strands_a_stuck_off_bus_hold_when_kill_switched(
    monkeypatch, tmp_path, clear_job_state
):
    """The kill-switch counterpart end-to-end: with the off-bus arm set to 0 the relift keeps the
    read-only base behavior — a still-off-bus chip stays held, no reset, wait for self-heal."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=31)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor — held, not reset", why="off_bus")
    counters = _arm_offbus_stuck_hold(monkeypatch, isolated=set())
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "0")

    async def still_off_bus(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 8 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", still_off_bus)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 0, "kill-switched: a still-off-bus hold must stay held, never reset"
    assert srv.fsm.state is not ServerState.HEALTHY, "and the hold must stand"


# --- A2: the escalation must reach a hold no read-only relift can lift --------------------------
#
# A foreign holder that blocked verification, or a gate that errored out, sets device_unverified_why
# but arms neither the self-heal nor the fabric relift — enum+ARC proves nothing those were placed
# for. Read-only nothing re-checks it, so it strands until a broker restart: the one indefinite hold
# Part B did not reach. TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE (default off) closes it by escalating a
# present, tenant-free mesh past the ceiling to the gate's galaxy reset — never a read-only lift.


def _setup_generic_hold(monkeypatch, tmp_path, *, n_present=32):
    """Put the device into an arms-neither HOLD (a foreign holder blocked verification), with the
    generic-hold escalation armed, ready for _attempt_idle_relift."""
    for i in range(n_present):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    srv.device_op_lock = None
    srv.device_op_active = ""
    fsm_dirty(srv, "foreign holder present: [agent]someone", why="foreign_holder")
    srv.last_relift_monotonic = 0.0
    monkeypatch.setenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "1")


@pytest.mark.asyncio
async def test_idle_relift_escalates_a_generic_arms_neither_hold(monkeypatch, tmp_path, clear_job_state):
    """The remaining indefinite hold, end-to-end: a foreign-holder hold arms neither relift, so base
    leaves it standing forever. Armed and past the ceiling on an idle present mesh with no tenant, the
    relift now escalates it to the gate's galaxy reset — WITHOUT a read-only verify, which cannot prove
    this class fit. Fails on base, where a generic hold arms nothing and never reaches a reset."""
    _setup_generic_hold(monkeypatch, tmp_path)
    counters = _arm_stuck_hold(monkeypatch)
    verify_called = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_called["n"] += 1
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 1, "a stranded arms-neither hold past the ceiling must escalate to a reset"
    assert (
        verify_called["n"] == 0
    ), "the generic branch must never run a read-only verify — a read cannot prove this class fit"


@pytest.mark.asyncio
async def test_idle_relift_bails_on_a_re_dirtied_hold(monkeypatch, tmp_path, clear_job_state):
    """on_fault preserves a hold's why when a dirty mark lands on it, so the armed categories still
    read armed on a re-dirtied hold — but a dirty device is the pre-job gate's to reset+verify, not
    this read-only path's to probe or escalate around. The dirty axis must be re-read under the
    lock and bail, exactly as the pre-fsm fold did."""
    _setup_generic_hold(monkeypatch, tmp_path)
    fsm_dirty(srv, "job ended timeout while held", why="foreign_holder", dirty=True)
    counters = _arm_stuck_hold(monkeypatch)
    verify_called = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_called["n"] += 1
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 0, "a re-dirtied hold is the gate's to reset, never the idle path's"
    assert verify_called["n"] == 0, "and it must not probe a device that owes the gate a reset"
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stands"


@pytest.mark.asyncio
async def test_idle_relift_escalates_a_fabric_unverified_hold_when_relift_is_off(
    monkeypatch, tmp_path, clear_job_state
):
    """A present-mesh fabric-unverified hold with the fabric relift OFF (the default) is a hold no read
    can clear and nothing re-verifies — the exact 20-min strand this grace exists to end. It now falls
    through to the generic galaxy reset instead of waiting for the ceiling. Fails on base, where the
    generic branch excludes every fabric-unverified hold outright."""
    _setup_generic_hold(monkeypatch, tmp_path)
    srv.device_hold_needs_fabric = True
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_RELIFT", raising=False)  # OFF: nothing re-verifies the fabric
    counters = _arm_stuck_hold(monkeypatch)

    async def verify(expected, log, run_fabric=True):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 1, "a fabric-unverified hold no relift re-verifies must escalate at the grace"


@pytest.mark.asyncio
async def test_idle_relift_leaves_a_generic_hold_untouched_when_kill_switched(monkeypatch, tmp_path, clear_job_state):
    """The kill-switch: with TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE=0 a foreign-holder hold arms neither
    relift and is left standing — no reset, no read-only verify — the read-only base behavior."""
    _setup_generic_hold(monkeypatch, tmp_path)
    counters = _arm_stuck_hold(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "0")
    verify_called = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        verify_called["n"] += 1
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 0, "kill-switched: a generic hold must not escalate"
    assert verify_called["n"] == 0, "and it must not even re-verify"
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stands"


@pytest.mark.asyncio
async def test_generic_hold_escalation_holds_under_a_foreign_tenant(monkeypatch, tmp_path, clear_job_state):
    """The tenant guard reaches the new class too: an arms-neither hold on a device a foreign tenant
    holds must not escalate — never take a box's mesh out from under a running job."""
    _setup_generic_hold(monkeypatch, tmp_path)
    counters = _arm_stuck_hold(
        monkeypatch, scan=HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True)
    )

    async def verify(expected, log, run_fabric=True, **_):
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert counters["resets"] == 0, "a foreign tenant must block the generic-hold escalation"
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stands until the box is idle"


@pytest.mark.asyncio
async def test_maybe_spawn_idle_relift_arms_for_a_generic_hold_unless_kill_switched(
    monkeypatch, tmp_path, clear_job_state
):
    """The sampler spawns the relift for a generic arms-neither hold unless the escalation is
    kill-switched; with =0 it stays dormant so the sampler spawns nothing for that class."""
    _setup_generic_hold(monkeypatch, tmp_path)
    srv._relift_task = None
    ran = {"n": 0}
    gate = asyncio.Event()

    async def fake_relift():
        ran["n"] += 1
        await gate.wait()

    monkeypatch.setattr(srv, "_attempt_idle_relift", fake_relift)

    monkeypatch.setenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "0")
    srv._maybe_spawn_idle_relift()
    assert srv._relift_task is None, "kill-switched: a generic hold arms no relift task"

    monkeypatch.setenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "1")
    srv._maybe_spawn_idle_relift()
    first = srv._relift_task
    assert first is not None, "enabled + held: the generic hold spawns the relift"
    await asyncio.sleep(0)
    assert ran["n"] == 1

    gate.set()
    await first


def test_preflight_requires_fabric_check_but_only_warns_on_eth_heartbeat(monkeypatch, tmp_path):
    """The startup preflight hard-FAILS on a missing REQUIRED capability (the fabric check on a
    multi-chip host) but only WARNS on the absent eth-heartbeat probe — the fabric traffic pass is
    the authoritative eth/fabric validator; the eth-heartbeat is a non-perturbing optimization the
    relift falls back from. Fails on base, which has no startup preflight at all."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()  # a 2-chip (fabric) host
    (dev / "1").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")  # explicit reset mode -> ok
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "")  # REQUIRED, missing -> fail
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)  # OPTIONAL, missing -> warn
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: True)  # a host with systemd
    monkeypatch.setattr(srv.shutil, "which", lambda n: f"/usr/bin/{n}")  # tt-smi/setpci/systemctl present
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)

    fails, warns, _degrade = srv._preflight_required_capabilities(socket_path="/run/x.sock")
    assert any("FABRIC_CHECK_CMD" in f for f in fails), fails
    assert not any("ETH_HEARTBEAT" in f for f in fails), "eth-heartbeat must NOT be a hard fail"
    assert any("ETH_HEARTBEAT" in w for w in warns), warns


def test_preflight_fabric_check_satisfied_by_the_built_in_validator(monkeypatch, tmp_path):
    """TT_DEVICE_MCP_FABRIC_CHECK_CMD unset must NOT hard-fail preflight when the built-in
    validator path (health.monitors.fabric.build_command) is itself available — a host that
    never wires an operator override but has the pinned validator installed can still boot."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    (dev / "1").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "/usr/bin/true")  # keep the warn out of the way
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: True)
    monkeypatch.setattr(srv.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(
        srv.fabric,
        "build_command",
        lambda: (["/opt/tt-device-broker/validator/" "current/build/tools/scaleout/" "run_cluster_validation"], {}),
    )

    fails, _warns, _degrade = srv._preflight_required_capabilities(socket_path="/run/x.sock")
    assert not any("FABRIC_CHECK_CMD" in f for f in fails), fails


def test_the_scoped_backend_needs_systemd_running_not_a_binary_on_path(monkeypatch):
    """Keys on /run/systemd/system (what sd_booted(3) reads), not on a systemctl binary, which a
    restricted-PATH host could lack while systemd runs."""
    monkeypatch.setattr(privileges, "is_root", lambda: True)
    monkeypatch.setattr(privileges, "has_systemd", lambda: True)
    assert srv._scoped_reset_backend() is True
    monkeypatch.setattr(privileges, "has_systemd", lambda: False)
    assert srv._scoped_reset_backend() is False


def test_a_non_root_daemon_on_a_systemd_host_does_not_pick_the_scope_backend(monkeypatch):
    """`systemd-run --scope` on the system bus refuses an unprivileged caller ("Interactive
    authentication required", rc 1). The caller reads that rc as a failed reset and arms the 600s
    cooldown, so a non-root daemon that picked this backend would suppress its own next attempt
    after a reset that never ran. It takes the local-lock backend instead."""
    monkeypatch.setattr(privileges, "has_systemd", lambda: True)
    monkeypatch.setattr(privileges, "is_root", lambda: False)

    assert srv._scoped_reset_backend() is False


def test_preflight_in_a_container_serves_without_the_deploy_staged_probes(monkeypatch, tmp_path):
    """Generic per-user container: no systemd and no explicit device ownership. Recovery remains a
    host-broker responsibility, so its tools degrade to a warning instead of blocking serialization."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()  # a single-chip host
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)  # no systemd
    # tt-smi is installed (health snapshot).
    monkeypatch.setattr(srv.shutil, "which", lambda n: "/usr/bin/tt-smi" if n == "tt-smi" else None)

    fails, warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert fails == [], f"a container broker must still serve; got fails: {fails}"
    assert any("recovery" in w.lower() or "systemd" in w.lower() for w in warns), warns


def test_preflight_accepts_the_local_reset_backend_without_systemd(monkeypatch, tmp_path):
    """A daemon with tt-smi must not be classified as serialization-only just because
    PID 1 is not systemd."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.shutil, "which", lambda name: "/usr/bin/tt-smi" if name == "tt-smi" else None)

    fails, warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert fails == []
    assert not any("not driven here" in warning for warning in warns), warns
    assert any("setpci" in warning for warning in warns), warns


def test_preflight_requires_a_reset_command_even_when_health_is_disabled(monkeypatch, tmp_path):
    """Disabling probes must not let a recovery-enabled profile start with no executable recovery
    rung."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.shutil, "which", lambda _name: None)

    fails, _warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert any("reset command" in failure for failure in fails), fails


def test_preflight_multichip_per_user_daemon_warns_rather_than_refusing_to_serve(monkeypatch, tmp_path):
    """The fabric validator is staged by the SYSTEM installer; install-user.sh has no way to put
    it down. Requiring it of a per-user daemon would refuse to serve over a file that shape can
    never have, costing the queue to gain nothing.

    Safety does not rest on this check. A reset leaves the device dirty, and on a dirty
    multi-chip host `fabric_ok is None` — which is what "no validator configured" produces —
    routes to HOLD_FABRIC_UNVERIFIED, not RELEASE (galaxy._route). Tenants are held, not admitted
    onto an unproven mesh; see test_cascade_router_expresses_release_and_the_fabric_unverified_hold."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    (dev / "1").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.fabric, "build_command", lambda: None)
    monkeypatch.setattr(srv.shutil, "which", lambda name: "/usr/bin/tt-smi" if name == "tt-smi" else None)

    fails, warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert fails == [], "a per-user daemon must still serve; the queue is the point"
    assert any("fabric validator" in warning for warning in warns), warns


def test_preflight_multichip_container_serves_without_fabric_or_reset_mode(monkeypatch, tmp_path):
    """A MULTI-CHIP container (fabric_host, no systemd) must still serve: without a recovery ladder
    to act on a bad-fabric verdict, the FABRIC_CHECK_CMD and RESET_MODE requirements relax from hard
    fails to the container warning. This pins the branch the change newly creates — on a host these
    two are REQUIRED (see the fabric-check test)."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    (dev / "1").touch()  # 2 chips -> fabric_host
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)  # unset — a host would FAIL
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)  # unset — a host would FAIL
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.shutil, "which", lambda n: "/usr/bin/tt-smi" if n == "tt-smi" else None)

    fails, warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert fails == [], f"a multi-chip container must still serve; got fails: {fails}"
    assert not any("FABRIC_CHECK_CMD" in f for f in fails)
    assert not any("RESET_MODE" in f for f in fails)


def test_preflight_on_a_host_requires_systemctl_and_systemd_run(monkeypatch, tmp_path):
    """The ladder-available (host) branch drives every recovery rung through systemd: each reset runs
    in a systemd-run scope, and poller-quiesce / scope-adoption / the reboot rung go through systemctl.
    Preflight must require both binaries — otherwise the broker boots "healthy" yet cannot perform the
    recovery it gates access on. Single-chip host so the fabric/reset-mode requirements stay out."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: True)

    present = {"tt-smi", "setpci", "systemctl", "systemd-run"}
    for missing in ("systemctl", "systemd-run"):
        avail = present - {missing}
        monkeypatch.setattr(srv.shutil, "which", lambda n, _a=avail: f"/usr/bin/{n}" if n in _a else None)
        fails, _, _degrade = srv._preflight_required_capabilities(socket_path="/run/x.sock")
        assert any(missing in f for f in fails), (missing, fails)

    # All four present -> the host preflight is clean.
    monkeypatch.setattr(srv.shutil, "which", lambda n: f"/usr/bin/{n}" if n in present else None)
    fails, _, _degrade = srv._preflight_required_capabilities(socket_path="/run/x.sock")
    assert fails == [], fails


@pytest.mark.asyncio
async def test_the_reset_without_systemd_runs_bare_never_through_systemd_run(monkeypatch, tmp_path):
    """Without systemd the reset still runs — it is an ioctl on a device node the submitter holds
    (spec 04 I17) — but it must not reach for a systemd-run that cannot exist in a container. It
    goes out as the bare reset argv in a new session, holding the cross-process lock instead."""
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "_local_reset_dir", lambda: tmp_path)
    spawned = []

    async def _capture(*argv, **kwargs):
        spawned.append((list(argv), kwargs))
        raise OSError("stop here; the argv is what this pins")

    monkeypatch.setattr(recovery_base.asyncio, "create_subprocess_exec", _capture)

    await srv.recovery_mechanism.run_scoped(["tt-smi", "-r", "0"], lambda m: None)

    assert len(spawned) == 1, "one launch attempt, not a retry loop around a missing systemd-run"
    argv, kwargs = spawned[0]
    assert argv == ["tt-smi", "-r", "0"], "the bare reset, unwrapped"
    assert kwargs["start_new_session"] is True, "it has to outlive this broker"


@pytest.mark.asyncio
async def test_a_power_cycle_that_fails_to_launch_retracts_its_ledger_entry(monkeypatch, tmp_path):
    """R3: a power cycle recorded to the rate-limit ledger whose fire FAILS to launch (ipmitool
    missing / non-zero / timeout) must RETRACT the record — otherwise the per-boot cap counts a
    recovery that never happened and blocks the retry a still-wedged box needs. Fails on base, which
    records before firing and lets the fire's exception escape (swallowed upstream), leaving the
    entry. A SUCCESSFUL fire takes the box down, so this retract path is reached only on a real
    launch failure."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    logs: list = []

    def failing_fire():
        raise FileNotFoundError("ipmitool")  # the binary is not installed on this host

    await srv.recovery_mechanism._fire_recovery_escalation(
        "power-cycle",
        row_owner="[broker]power-cycle-request",
        label="auto BMC power cycle",
        detail="reset+reboot could not recover",
        fire=failing_fire,
        log=logs.append,
        reason="wedge",
    )

    ledger = srv.health_dir() / recovery_base.AUTO_RECOVERY_LEDGER
    entries = [ln for ln in ledger.read_text().splitlines() if ln.strip()] if ledger.exists() else []
    assert not any('"power-cycle"' in e for e in entries), f"a failed fire left a poisoning entry: {entries}"
    assert any("FAILED TO LAUNCH" in m for m in logs), logs


@pytest.mark.asyncio
async def test_a_clean_multichip_gate_holds_on_an_unverifiable_fabric(monkeypatch, tmp_path):
    """Fail CLOSED on an unverifiable fabric. On a MULTI-chip host, a fabric pass that RAN but could
    not return a verdict (77 — "no link tested" when the eth links never trained) must HOLD the device
    fabric-unverified, not read healthy: enum+ARC are blind to a wedged eth core, so admitting on them
    alone puts tenants onto a possibly-dead fabric (measured on blx04, where chip 4/14 eth links were
    down and every full-mesh job died while the box read ok). Fails on base, which clears a clean
    device healthy on a None fabric and admits."""
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")  # a 2-chip (fabric) host
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "2")
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    fsm_healthy(srv)
    srv.last_fabric_check_monotonic = 0.0  # stale -> the fabric pass runs (full=True)

    async def verify(expected, log, run_fabric=True, **_):
        # enum+ARC healthy, but the fabric pass returned no verdict (77 -> None)
        return True, {"fabric": {"ok": None, "detail": "no link tested (77)"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert srv.fsm.state is not ServerState.HEALTHY, "an unverifiable fabric on a multi-chip host must hold the door"
    assert srv.fsm.record.why == "fabric_unverified", "the hold must arm the fabric relift to re-verify"


@pytest.mark.asyncio
async def test_a_runtime_eth_fault_is_retired_when_the_fabric_pass_passes(monkeypatch, tmp_path):
    """Fix A: a runtime eth fault ('waiting for active ethernet core') that the fabric traffic pass
    then CLEARS (a real OK verdict) is retired at the gate and the door reopens immediately — not 20
    minutes later after a galaxy reset of 32 healthy ASICs. The fabric check is the authoritative
    eth/fabric validator. Fails on base, which holds the fault until a reset regardless of the pass."""
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "2")
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    fsm_healthy(srv)
    srv.device_fault_reported = "job 533 eth/fabric fault: 'waiting for active ethernet core'"
    srv.last_fabric_check_monotonic = 0.0  # stale -> the fabric pass runs

    async def verify(expected, log, run_fabric=True, **_):
        return True, {"fabric": {"ok": True, "detail": "links healthy"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert not srv.device_fault_reported, "a real fabric-OK must retire the runtime eth fault"
    assert srv.fsm.state is ServerState.HEALTHY, "the door must reopen once the fabric verifies healthy"


@pytest.mark.asyncio
async def test_a_recurring_eth_fault_is_not_retired_on_the_fabric_ok(monkeypatch, tmp_path):
    """Fix A's second-erisc backstop: if the same runtime eth fault keeps recurring fast despite the
    fabric passing each time, the pass is not reaching the wedge (a second-erisc core it cannot drive)
    — stop retiring on the fabric-OK, so it falls through to the hold/reset path (device stays held)
    instead of reopening onto a mesh that re-wedges. Fails on base, which has no retire path at all."""
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "2")
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    fsm_healthy(srv)
    srv.device_fault_reported = "eth fault: 'waiting for active ethernet core'"
    srv.last_fabric_check_monotonic = 0.0
    # already at the streak ceiling, retired moments ago -> this recurrence must NOT retire
    srv._fabric_ok_retire_streak = srv.FABRIC_RETIRE_MAX_STREAK
    srv._fabric_ok_retire_monotonic = srv.time.monotonic()

    events: list = []
    patch_health_event(monkeypatch, lambda ev, **k: events.append(ev))

    async def verify(expected, log, run_fabric=True, **_):
        return True, {"fabric": {"ok": True, "detail": "links healthy"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    # The refused retirement drops through to the hold/reset path. This test is about the
    # retirement decision, so the reset is stubbed as ineffective — issuing it would spawn tt-smi.
    resets: list = []

    async def no_reset(indices, log):
        resets.append(indices)
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", no_reset)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert "fault_retire_refused_recurring" in events, events
    assert "fault_retired_on_fabric_ok" not in events, "a recurring fault must NOT be retired on the fabric-OK"


@pytest.mark.asyncio
async def test_a_present_mesh_wedge_that_survived_the_galaxy_reset_climbs_to_the_host_rung(
    monkeypatch, tmp_path, clear_job_state
):
    """The cascade must PROGRESS. A present mesh (every chip on the bus) whose galaxy reset still did
    not verify is an eth/fabric wedge the reset could not clear — it survived the deepest reset, so it
    is not self-healing and must climb to the host reboot/power-cycle, not hold indefinitely (measured
    on blx04: hours of ceiling->galaxy-reset->hold with the eth core wedged). Fails on base, which
    holds any off_bus < reboot_floor, present mesh included."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32)})  # all 32 present
    _no_holders(monkeypatch)
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda esc, *, tenant_active: (True, "ok"))
    patch_health_event(monkeypatch, lambda *a, **k: None)
    reboots = {"n": 0}

    async def fake_reboot(log, reason):
        reboots["n"] += 1

    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", fake_reboot)
    await srv.galaxy_recovery._climb_to_host_recovery_after_failed_reset(
        32, 0, lambda _m: None
    )  # present before the reset
    assert reboots["n"] == 1, "a present-mesh wedge that survived the galaxy reset must climb to the host rung"


@pytest.mark.asyncio
async def test_a_few_chip_off_bus_drop_still_holds_below_the_reboot_floor(monkeypatch, tmp_path):
    """The other side of the fix: a FEW chips off the bus below the mass floor genuinely re-enumerate
    on their own, so they must still HOLD (not reboot). Only a PRESENT mesh climbs, not a small drop."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(30)})  # 2 off the bus
    patch_health_event(monkeypatch, lambda *a, **k: None)
    reboots = {"n": 0}

    async def fake_reboot(log, reason):
        reboots["n"] += 1

    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", fake_reboot)
    await srv.galaxy_recovery._climb_to_host_recovery_after_failed_reset(
        32, 2, lambda _m: None
    )  # 2 off-bus, no regression
    assert reboots["n"] == 0, "a few-chip drop below the mass floor self-heals and must not reboot"


def _seed_reboot_ledger(tmp_path, boot_id="prev-boot", *, age_sec=120.0):
    """A durable auto-recovery ledger whose last escalation is a warm reboot from a PREVIOUS boot —
    the evidence that this boot is that reboot's result. Written where _ledger_dir points health_dir.
    ``at_epoch`` is anchored ``age_sec`` before THIS boot's start so the attribution window in
    _boot_from_broker_escalation accepts it; pass a large ``age_sec`` to simulate a STALE entry."""
    try:
        btime = float(srv._boot_btime_id())
    except (ValueError, TypeError):
        btime = srv.time.time()
    (tmp_path / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        json.dumps({"action": "reboot", "boot_id": boot_id, "at_epoch": btime - age_sec, "at": "prev"}) + "\n"
    )


def test_boot_attribution_rejects_a_stale_escalation(monkeypatch, tmp_path):
    """A ledger whose last escalation predates THIS boot by more than the reboot window is NOT this
    boot's cause — the box rebooted for another reason since (an external reboot, KMD remediation). The
    back-fill must not stamp such a boot as a broker auto-recovery it never fired (the phantom
    'power cycle — broker auto-recovery (post-reboot verify…)' rows an ansible reboot got 16 h after the
    last real escalation). Fails on base, which attributes any older-boot-id entry regardless of age."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path, age_sec=100000.0)  # ~28 h before this boot: stale
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    assert (
        srv.recovery_mechanism.boot_from_broker_escalation("boot-now") is None
    ), "a stale escalation must not be attributed to this boot"


def test_boot_attribution_accepts_a_recent_escalation(monkeypatch, tmp_path):
    """The positive: an escalation recorded within the reboot window before this boot started IS this
    boot's cause and is attributed, so a genuine broker reboot still back-fills its row."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path, age_sec=120.0)  # 2 min before this boot: adjacent
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    rec = srv.recovery_mechanism.boot_from_broker_escalation("boot-now")
    assert (
        rec is not None and rec.get("action") == "reboot"
    ), "a recent, previous-boot escalation must be attributed to this boot"


@pytest.mark.asyncio
async def test_post_reboot_all_off_bus_climbs_to_the_power_cycle(monkeypatch, tmp_path):
    """The blx02 incident: the broker's warm reboot came back 0/32 (a 6U-Galaxy reboot cannot re-
    enumerate dropped UBBs), and base re-held the box dead ~18h. With the cold rung opted in, the boot
    that verifies the reboot climbs straight to the power cycle instead. Fails on base (no verify)."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    _no_holders(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    await srv.galaxy_recovery._verify_post_reboot_recovery(off_bus=32, expected=32, log=lambda _m: None)
    assert cycles["n"] == 1, "a warm reboot that came back all-off-bus must climb to the cold rung"


@pytest.mark.asyncio
async def test_post_reboot_all_off_bus_holds_loudly_with_no_cold_rung_opted_in(monkeypatch, tmp_path):
    """No power cycle opted in: the reboot is the rung that just failed and must NOT repeat, so the
    verify emits the loud actionable event and keeps holding — never a second warm reboot, never a
    release. Fails on base, which re-holds silently with no event and no next rung."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    reboots, cycles = {"n": 0}, {"n": 0}

    async def fake_reboot(log, reason):
        reboots["n"] += 1

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", fake_reboot)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    await srv.galaxy_recovery._verify_post_reboot_recovery(off_bus=32, expected=32, log=lambda _m: None)
    assert reboots["n"] == 0 and cycles["n"] == 0, "no rung may fire with none opted in — hold loudly"
    assert "all_chips_off_bus_power_cycle_required" in events, "the whole-bus wedge must be loud, not swallowed"


@pytest.mark.asyncio
async def test_post_reboot_verify_never_repeats_the_warm_reboot(monkeypatch, tmp_path):
    """A mass drop the reboot did not clear, still short of all-off-bus, with only the warm reboot
    opted in: _host_escalation_for_drop re-offers the reboot — the rung that just failed. The verify
    must NOT repeat it; it holds loudly. Fails on base, which has no post-reboot verify at all."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    reboots = {"n": 0}

    async def fake_reboot(log, reason):
        reboots["n"] += 1

    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", fake_reboot)
    # 20 of 32 off the bus: a mass drop (>= floor) but not all-off-bus.
    await srv.galaxy_recovery._verify_post_reboot_recovery(off_bus=20, expected=32, log=lambda _m: None)
    assert reboots["n"] == 0, "the warm reboot already failed on this drop — it must never be repeated"
    assert "post_reboot_recovery_stuck_no_cold_rung" in events, "the stuck box must be loud about needing the cold rung"


@pytest.mark.asyncio
async def test_post_reboot_verify_never_cold_cycles_a_single_chip_host_without_power_cycle_consent(
    monkeypatch, tmp_path
):
    """_host_escalation_for_drop's reboot_futile short-circuits on expected > 1 (a single-chip host
    has no UBB-re-enumeration failure mode), so on a single-chip host it hands back escalation=
    "reboot" instead of the None/"power-cycle" pair a multi-chip drop always resolves to. With
    AUTO_REBOOT=1 and AUTO_POWER_CYCLE=0, that must still hold — never fire the chassis power cycle
    the operator explicitly did not opt into, and never repeat the warm reboot that just failed."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    _no_holders(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    # Withheld consent is =0, not an absent var: the cold rung defaults ARMED, so deleting this
    # grants the very consent the assertions below require to be absent.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    reboots, cycles = {"n": 0}, {"n": 0}

    async def fake_reboot(log, reason):
        reboots["n"] += 1

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", fake_reboot)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    await srv.galaxy_recovery._verify_post_reboot_recovery(off_bus=1, expected=1, log=lambda _m: None)
    assert cycles["n"] == 0, "AUTO_POWER_CYCLE=0 must never cold-cycle the chassis regardless of chip count"
    assert reboots["n"] == 0, "the warm reboot already ran and failed — this method must never repeat it"
    assert "all_chips_off_bus_power_cycle_required" in events


@pytest.mark.asyncio
async def test_post_reboot_few_chip_drop_below_floor_climbs_to_the_power_cycle(monkeypatch, tmp_path):
    """A warm reboot that brought the mesh back with chips STILL off the bus did not fully recover it,
    and a dropped ASIC does not re-enumerate on another warm reboot (a 6U-Galaxy reboot does not
    power-cycle the UBBs). Below the mass floor or not, a still-off-bus mesh here is not self-healing —
    the cold power cycle is the only rung above the reboot that just ran, so with it opted in the verify
    climbs to it rather than holding a degraded box. Fails on base, which holds below the floor."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    _no_holders(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    await srv.galaxy_recovery._verify_post_reboot_recovery(off_bus=2, expected=32, log=lambda _m: None)
    assert cycles["n"] == 1, "a still-off-bus mesh after a reboot climbs to the cold rung, never holds"
    assert "post_reboot_host_escalation" in events


@pytest.mark.asyncio
async def test_post_reboot_verify_is_inert_when_this_boot_is_not_a_broker_reboot(monkeypatch, tmp_path):
    """An external/manual reboot or a firmware crash leaves no broker reboot record attributing this
    boot, so there is nothing to verify — the verify must not climb over a boot it did not cause."""
    _ledger_dir(monkeypatch, tmp_path)  # empty ledger dir: no auto-recovery record at all
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    await srv.galaxy_recovery._verify_post_reboot_recovery(off_bus=32, expected=32, log=lambda _m: None)
    assert cycles["n"] == 0, "no broker reboot attributes this boot — nothing to climb past"


def test_post_reboot_verify_is_on_by_default(monkeypatch):
    """The verify closes the incident's dead-box hold, so it is on unless an operator opts out."""
    monkeypatch.delenv("TT_DEVICE_MCP_POST_REBOOT_VERIFY", raising=False)
    assert galaxy._post_reboot_verify_enabled() is True
    monkeypatch.setenv("TT_DEVICE_MCP_POST_REBOOT_VERIFY", "0")
    assert galaxy._post_reboot_verify_enabled() is False


@pytest.mark.asyncio
async def test_startup_health_climbs_after_a_reboot_that_did_not_bring_the_mesh_back(
    monkeypatch, tmp_path, clear_job_state
):
    """End-to-end through the startup probe: a broker warm reboot came back 0/32, the sysfs probe
    marks the box dirty (held) — and now, with the cold rung opted in, the same startup climbs to the
    power cycle instead of re-holding it dead. The device stays dirty throughout: it never releases.
    Fails on base, where startup only re-holds and no rung ever fires."""
    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-now")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    _no_holders(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {})  # 0/32: nothing came back
    monkeypatch.setattr(srv, "heartbeat_verdict", lambda expected, **k: (Verdict.UNHEALTHY, "0 chip(s) in sysfs", {}))
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    await srv._record_startup_health()
    assert cycles["n"] == 1, "a reboot that came back 0/32 must climb to the power cycle from startup"
    assert srv.fsm.state is not ServerState.HEALTHY, "the box must stay HELD across the verify — it never releases"


# --- a carried episode is still an ordinary episode to the gate that clears it ----------------
#
# boot_merge (and the cross-boot load underneath it) never itself reopens the door — that is by
# design, since nothing available at boot time proves the mesh fit. But "never reopens on its own"
# must not calcify into "harder to clear than usual": the startup gate's own forced probe pass,
# given every check healthy, has to clear a carried episode exactly as it clears one opened this
# boot. These pin that guarantee against the FSM level (test_fsm.py's
# test_cross_boot_load_voids_job_but_keeps_device_facts) covering only the load half of it.


def _cross_boot_carry(tmp_path, state, why):
    """Build a two-boot ServerFsm sequence: an episode opened under one boot_id, then swap to a
    second boot_id and run it through boot_merge exactly as run_startup_tasks does. Returns the
    post-merge instance."""
    fsm_path = tmp_path / "fsmdir" / "fsm.json"
    fsm_path.parent.mkdir()
    before = ServerFsm(fsm_path, current_boot_id="boot-before")
    before.on_fault(why, dirty=False)
    if state is ServerState.DOWN:
        before.on_outcome("terminal")

    after = ServerFsm(fsm_path, current_boot_id="boot-after")
    open_episode = after.record if after.record.state in (ServerState.RECOVERING, ServerState.DOWN) else None
    after.boot_merge(open_episode=open_episode, attributed_boot=None)
    assert after.record.state is state, "the episode must have carried across the boot_id change"
    return after


_HEALTHY_VERDICT = (
    True,
    {
        "snapshot": {"ok": True},
        "eth_heartbeat": {"ok": True, "detail": "advancing"},
        "fabric": {"ok": True, "detail": "links healthy"},
    },
)


@pytest.mark.asyncio
async def test_a_cross_boot_recovering_episode_is_cleared_by_a_healthy_startup_gate(monkeypatch, tmp_path):
    """A hold carried across a real reboot (a different boot_id on disk) is not somehow harder to
    clear than one opened this boot: given every probe healthy, the startup gate's forced pass
    clears it exactly as it clears an ordinary in-boot hold."""
    after = _cross_boot_carry(tmp_path, ServerState.RECOVERING, "eth_frozen")
    monkeypatch.setattr(srv, "fsm", after)
    _gate_with_verdict(monkeypatch, tmp_path, _HEALTHY_VERDICT)

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    assert srv.fsm.state is ServerState.HEALTHY
    assert srv.fsm.record.why == ""


@pytest.mark.asyncio
async def test_a_cross_boot_down_episode_is_cleared_by_a_healthy_startup_gate(monkeypatch, tmp_path):
    """The terminal state is not stickier than RECOVERING: a carried DOWN episode clears on the
    same healthy startup gate pass too."""
    after = _cross_boot_carry(tmp_path, ServerState.DOWN, "off_bus")
    monkeypatch.setattr(srv, "fsm", after)
    _gate_with_verdict(monkeypatch, tmp_path, _HEALTHY_VERDICT)

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    assert srv.fsm.state is ServerState.HEALTHY
    assert srv.fsm.record.why == ""


@pytest.mark.asyncio
async def test_post_reboot_climb_keeps_the_escalated_marker_and_the_reset_retries(monkeypatch, tmp_path):
    """The exact loop the durable record exists to prevent: the idle escalation already spent this
    episode's one present-mesh reset before the box's OWN reboot fired (the next rung up), the
    ledger attributes this boot to that reboot, and the mesh comes back still off the bus. Two
    things must both hold: the ledger-driven startup climb still reaches the power cycle with the
    carried episode in place (already proven fsm-agnostic by
    test_startup_health_climbs_after_a_reboot_that_did_not_bring_the_mesh_back; repeated here to
    show the carried episode does not interfere), and the episode's escalated latch survives that
    whole climb untouched — it is the watchdog's durable marker that an escalation ran. The latch
    is NOT a cap on the present-mesh reset though: a mesh back PRESENT but still wedged is again
    in the exact state the galaxy reset cures, so the idle escalation retries it on the grace
    cadence — the reset is paced by the last-reset clock, never barred by episode memory."""
    fsm_path = tmp_path / "fsmdir" / "fsm.json"
    fsm_path.parent.mkdir()
    before = ServerFsm(fsm_path, current_boot_id="boot-before")
    before.on_fault("off_bus", dirty=False)
    before.set_latch("escalated", True)  # this episode's one present-mesh reset already ran

    after = ServerFsm(fsm_path, current_boot_id="boot-after")
    open_episode = after.record if after.record.state in (ServerState.RECOVERING, ServerState.DOWN) else None
    after.boot_merge(open_episode=open_episode, attributed_boot=None)
    assert after.record.state is ServerState.RECOVERING and after.latch("escalated") is True
    monkeypatch.setattr(srv, "fsm", after)

    _ledger_dir(monkeypatch, tmp_path)
    _seed_reboot_ledger(tmp_path)  # this boot IS the result of the broker's own reboot escalation
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-after")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    _no_holders(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {})  # 0/32: the reboot did not bring it back
    monkeypatch.setattr(srv, "heartbeat_verdict", lambda expected, **k: (Verdict.UNHEALTHY, "0 chip(s) in sysfs", {}))
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    await srv._record_startup_health()
    assert cycles["n"] == 1, "a reboot that came back 0/32 must climb straight to the power cycle"
    assert (
        after.latch("escalated") is True
    ), "the climb to the power cycle must not touch the episode's own escalation memory"

    # Later: the mesh is back present (self-healed, or the power cycle above ran) but still wedged —
    # exactly the state the galaxy reset cures, so the idle relift retries it on the grace cadence
    # despite the episode's escalated memory.
    counters = _arm_stuck_hold(monkeypatch)
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True, "a present-but-wedged mesh gets the reset again — the latch is not a cap"
    assert counters["resets"] == 1


@pytest.mark.asyncio
async def test_a_reset_that_leaves_the_fabric_unverified_is_not_recovered(monkeypatch, tmp_path):
    """H2: on a multi-chip host, a galaxy reset whose fabric still cannot verify after eth-training
    retries (persistent 77) is NOT a recovery — enum+ARC came back but the fabric proved nothing, so
    _reset_and_verify_device returns False and the caller climbs (E0) instead of clearing the hold onto
    an unverified fabric (which loops). Fails on base, where a 77 reads as recovered (healthy=True)."""
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRIES", 1)

    async def no_foreign(log):
        return False

    async def ok_reset(argv, log):
        return 0, ""

    calls = {"n": 0}

    async def verify(expected, log, run_fabric=True, **_):
        calls["n"] += 1
        return True, {"fabric": {"ok": None, "detail": "no link tested (77)"}}  # persistent 77

    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", no_foreign)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", ok_reset)
    patch_recovery(monkeypatch, "_verify_device", verify)
    patch_health_event(monkeypatch, lambda *a, **k: None)

    recovered = await srv.galaxy_recovery._reset_and_verify_device(["0", "1"], lambda _m: None)
    assert recovered is False, "a persistent post-reset 77 must count NOT recovered so the caller climbs"
    assert calls["n"] >= 2, "the post-reset fabric must be retried at least once for eth training"


# --- G-tier safe U-fixes: fail-closed / loudness on the silent-degrade paths -------------


@pytest.mark.asyncio
async def test_post_job_gate_error_marks_the_device_dirty(monkeypatch):
    """U2: the post-job gate is the only thing that reliably catches a fabric wedge that
    never shows in an exit code. If it THROWS before it can verify, the device state is
    unknown — leaving it unflagged lets the next tenant run on unverified silicon. It must
    fall dirty so the residual pre-job gate resets + verifies before that job starts."""

    async def boom(*a, **k):
        raise RuntimeError("gate blew up")

    monkeypatch.setattr(srv, "_device_health_gate", boom)
    assert srv.fsm.state is ServerState.HEALTHY
    await srv._verify_device_after_job(None, job_failed=False)  # must never raise
    assert srv.fsm.state is not ServerState.HEALTHY, "a post-job gate that errored left the device unflagged"


@pytest.mark.asyncio
async def test_kill_device_holders_journals_a_survivor_loudly(monkeypatch):
    """U3: this is the rescue that converts 'the host dies in two minutes' into 'one chip is
    offline'. A holder it could NOT kill still maps the dead endpoint and will still stall a
    core — swallowing the failure read as 'all holders cleared'. The survivor must land in the
    journal, tagged host-at-risk."""
    holder = DeviceHolder(pid=424242, uid=1234567)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[holder], complete=True))
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None, raising=False)
    monkeypatch.setattr(srv, "current_process", None, raising=False)

    real_kill = srv.os.kill

    def refuse(pid, sig):
        if pid == holder.pid:
            raise PermissionError("operation not permitted")
        return real_kill(pid, sig)

    monkeypatch.setattr(srv.os, "kill", refuse)

    killed = await srv._kill_device_holders("chip 3 left the bus")
    assert killed == [], "the holder was not actually killed"
    events = health.read_health_events(kinds={"device_holders_killed"})
    assert events, "no device_holders_killed event was journaled"
    ev = events[-1]
    assert ev.get("host_at_risk") is True, "an unkillable holder must flag host_at_risk"
    assert (
        ev.get("survivors") and ev["survivors"][0]["pid"] == holder.pid
    ), "the surviving holder was not named in the journal"


@pytest.mark.asyncio
async def test_kill_device_holders_records_the_running_job_as_recovery_killed(monkeypatch):
    """A job SIGKILLed to rescue the host from an off-bus chip's mapping did not crash on its own — its
    death is the broker's. Record it in reset_killed_job_ids so the post-job gate does not read the kill
    as fresh evidence the mesh needs a reset (the reset justifying itself) and the runner can name the
    real cause to the submitter instead of a bare signal exit. Fails on base, which kills the pid but
    never ties it to the job."""
    import types

    monkeypatch.setattr(srv, "reset_killed_job_ids", set())
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None, raising=False)
    monkeypatch.setattr(srv, "current_process", None, raising=False)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    fake_job = types.SimpleNamespace(id="777", status=srv.JobStatus.RUNNING)
    monkeypatch.setattr(srv, "jobs", {"777": fake_job})

    await srv._kill_device_holders("chip(s) 0,1,2,3,4,5,6,7 left the PCIe bus")
    assert (
        "777" in srv.reset_killed_job_ids
    ), "the job the rescue SIGKILLed must be recorded as recovery-killed, not read as a fresh wedge"


def test_sum_aer_unreadable_reads_none_not_a_clean_zero(tmp_path):
    """U6: a present-but-unreadable AER counter returning 0 reads as a clean bus — the exact
    signal that precedes a wedge-to-reboot — when nothing was measured. None keeps 'unknown'
    apart from 'measured zero'."""
    good = tmp_path / "aer_dev_correctable"
    good.write_text("RxErr 0\nBadTLP 3\nTimeout 4\n")
    assert pci._sum_aer(good) == 7, "a readable counter file must still total"
    # A directory read_text()s to an OSError — a present path that cannot be read as a value.
    assert pci._sum_aer(tmp_path) is None, "an unreadable counter must be None, not 0"


def test_sum_aer_does_not_double_count_the_kernels_own_total(tmp_path):
    """Real sysfs ends every aer_dev_* file with the kernel's own TOTAL_ERR_* line.

    Totalling every line counted it a second time and reported exactly double the errors that
    occurred. Healthy hardware hid it — zero doubled is zero — so it would first have shown the
    moment the counters started moving, which is the one moment the number is load-bearing.
    """
    real = tmp_path / "aer_dev_correctable"
    real.write_text(
        "RxErr 3\nBadTLP 2\nBadDLLP 0\nRollover 0\nTimeout 1\n"
        "NonFatalErr 0\nCorrIntErr 0\nHeaderOF 0\nTOTAL_ERR_COR 6\n"
    )
    assert pci._sum_aer(real) == 6, "the kernel's own total was double-counted"

    fatal = tmp_path / "aer_dev_fatal"
    fatal.write_text("Undefined 0\nDLP 1\nTOTAL_ERR_FATAL 1\n")
    assert pci._sum_aer(fatal) == 1

    # No total line (older kernels, and every fixture written against this function): still sums.
    legacy = tmp_path / "aer_dev_nonfatal"
    legacy.write_text("RxErr 3\nBadTLP 2\n")
    assert pci._sum_aer(legacy) == 5


def test_aer_totals_tallies_unreadable_instead_of_masking_it():
    """U6: totals must not sum an unreadable (None) counter as a clean 0. It is tallied under
    'unreadable' so a bus nobody could read cannot hide as a healthy one; a counter simply
    ABSENT (old kernel, no AER sysfs) stays silent."""
    chips = {
        "0": {"aer_correctable": 5, "aer_nonfatal": 0, "aer_fatal": 0},
        "1": {"aer_correctable": None, "aer_nonfatal": 2, "aer_fatal": 0},  # present, unreadable
        "2": {},  # AER not exposed at all
    }
    out = pci.aer_totals(chips)
    assert out["correctable"] == 5, "a real count must survive"
    assert out["nonfatal"] == 2
    assert out.get("unreadable") == 1, "the unreadable counter must be tallied, not summed as 0"


def test_aer_totals_stays_quiet_when_every_counter_is_readable():
    """U6 negative: no 'unreadable' key when nothing was unreadable — the loud field appears
    only when there is something to be loud about."""
    chips = {"0": {"aer_correctable": 0, "aer_nonfatal": 0, "aer_fatal": 0}}
    out = pci.aer_totals(chips)
    assert "unreadable" not in out


def test_capture_incident_journals_a_distinct_sentinel_when_it_cannot_write(monkeypatch):
    """U7: the forensic bundle for a host-killing incident is the one record a post-mortem
    needs. When it cannot be written, the failure must leave a distinct, greppable sentinel in
    the decision log instead of an unexplained absence — and still never raise."""

    def boom():
        raise OSError("no space left on device")

    monkeypatch.setattr(pci, "chip_snapshot", boom)
    result = health.capture_incident("chip_dead", job={"id": "j1"})  # must never raise
    assert result is None
    events = health.read_health_events(kinds={"incident_capture_failed"})
    assert events, "a failed forensic capture left no sentinel in the journal"
    assert events[-1].get("host_at_risk") is True
    assert events[-1].get("label") == "chip_dead"


@pytest.mark.asyncio
async def test_isolate_dead_chips_is_loud_when_a_chip_will_not_leave_the_kernel(monkeypatch):
    """U8: the isolation valve claiming 'the host is out of danger' when a dead chip could NOT
    be removed is a false safety — that chip is still on the bus, still MMIO-reachable, still
    able to stall a core. An incomplete isolation must be loud and host-at-risk."""
    srv.isolated_chips = set()
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: False)  # the removal fails
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: "0000:46:00.0")
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)

    await srv.sampler.isolate_dead_chips(["13"])

    incomplete = health.read_health_events(kinds={"chip_isolation_incomplete"})
    assert incomplete, "a chip that could not be removed produced no loud event"
    assert incomplete[-1]["stuck"] == ["13"]
    assert incomplete[-1].get("host_at_risk") is True
    isolated = health.read_health_events(kinds={"chip_isolated"})
    assert isolated and isolated[-1]["chips"] == [], "no chip was actually removed"


class _CaptureLogger:
    """Records log calls so a loudness assertion can read them back."""

    def __init__(self):
        self.msgs = []

    def warning(self, m, *a, **k):
        self.msgs.append(m)

    def error(self, m, *a, **k):
        self.msgs.append(m)

    def info(self, m, *a, **k):
        self.msgs.append(m)


@pytest.mark.asyncio
async def test_watchdog_heartbeat_says_so_when_it_is_not_armed(monkeypatch):
    """U10: with no WATCHDOG_USEC a wedged event loop will not be auto-restarted — a real gap
    the pings exist to close. Running silently without it hides the gap; it must say so."""
    log = _CaptureLogger()
    monkeypatch.setattr(srv, "logger", log)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    await srv._watchdog_heartbeat()  # returns immediately when unarmed
    assert any("watchdog not armed" in m for m in log.msgs), "an unarmed systemd watchdog was not surfaced"


def test_sd_notify_error_is_logged_once_not_swallowed(monkeypatch):
    """U10: a dropped WATCHDOG=1 makes systemd count the broker as wedged and kill it. The
    notify failure must be surfaced — once per kind, so a persistently broken socket cannot
    flood the log at the ping interval."""
    log = _CaptureLogger()
    monkeypatch.setattr(srv, "logger", log)
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/dir/tt-notify.sock")
    srv._sd_notify("WATCHDOG=1")
    assert any("systemd notify" in m for m in log.msgs), "a failed notify was swallowed silently"
    log.msgs.clear()
    srv._sd_notify("WATCHDOG=1")
    assert log.msgs == [], "the notify failure was logged more than once for the same kind"


def test_a_non_root_daemon_without_tt_smi_degrades_to_a_serializer(monkeypatch, tmp_path):
    """Health gating is on by default in both shapes, so a container that simply has no tt-smi
    would otherwise fail preflight and refuse to serve — losing the queue, which needs no tt-smi
    at all. It serves, warns, and asks the caller to turn the gate off (spec 03 I25)."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(privileges, "is_root", lambda: False)
    monkeypatch.setattr(srv.shutil, "which", lambda _name: None)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_CHECK", raising=False)

    fails, warns, serialize_only = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert fails == [], f"the serializer must still come up; got {fails}"
    assert serialize_only is True
    assert any("serializes only" in w for w in warns), warns


def test_the_same_host_as_root_still_refuses_to_serve(monkeypatch, tmp_path):
    """A shared host with no way to probe is a trap, not a degraded container: the broker there is
    expected to have the tooling, and tenants cannot tell a silent broker from a healthy one."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(privileges, "is_root", lambda: True)
    monkeypatch.setattr(srv.shutil, "which", lambda _name: None)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_CHECK", raising=False)

    fails, _warns, serialize_only = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert serialize_only is False
    assert any("tt-smi" in f for f in fails), fails


def test_preflight_serves_a_health_off_daemon_without_a_writable_health_dir(monkeypatch, tmp_path):
    """The live symptom: a non-root daemon refused to boot on an unwritable
    /var/lib/tt-device-broker/health. With health checks off nothing writes that dir — no incident
    capture, no chip baseline — so requiring it took the reservation out of service for a journal
    nobody would have written."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: srv.Path("/proc/nonexistent/health"))
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)  # no systemd
    # The subject is the health dir. tt-smi is sealed absent for unmarked tests, and its own
    # absence is a fail this host would otherwise trip over on the way to the assertion.
    monkeypatch.setattr(srv.shutil, "which", lambda name: "/usr/bin/tt-smi" if name == "tt-smi" else None)
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "0")
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    fails, _warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert fails == [], f"a health-off daemon must serve without a writable health dir; got {fails}"


def test_preflight_still_requires_the_health_dir_when_health_is_on(monkeypatch, tmp_path):
    """Where the checks run, the dir has readers — the chip baseline and incident capture — and an
    unwritable one loses them silently."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: srv.Path("/proc/nonexistent/health"))
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_CHECK", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    fails, _warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert any("health dir" in f for f in fails), f"expected a health-dir failure; got {fails}"


def test_preflight_warns_not_fails_when_the_reset_mode_is_undeclared(monkeypatch, tmp_path):
    """A multi-chip host with no declared mode used to be refused outright. It is derived at reset
    time now, so this warns — and the preflight deliberately does not derive it here, because that
    is a tt-smi device init and a broker restarting after a wedge must not aim one at the silicon."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    (dev / "1").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: True)
    monkeypatch.setattr(srv.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "/bin/true")
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    # A tripwire, not an assertion on a stub: the previous check called the very seam conftest
    # pins, so it could not fail even when the preflight was made to derive the machine type.
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: pytest.fail(f"the preflight shelled out: {a}"))

    fails, warns, _degrade = srv._preflight_required_capabilities(socket_path="/tmp/x.sock")

    assert fails == [], f"an undeclared reset mode must not refuse the service; got {fails}"
    assert any("reset mode undeclared" in w for w in warns), warns


def test_a_failed_recovery_delays_the_ladder_but_never_ends_it(monkeypatch):
    """A hold must never become permanent. The latch that stops a rung being hammered used to clear
    only when the device came back fit — so a recovery that FAILED left nothing able to fire again
    and the device sat HELD with a tenant queued behind it until a human noticed (measured: 25 min,
    and unbounded by construction). The latch now expires, so a failed climb costs a delay, never
    the ladder."""
    # The latch itself is fsm's; the expiry clock is what server.py still owns.
    srv._set_hold_escalated(True)

    assert srv._hold_escalation_latched(), "a fresh escalation must hold the latch (no hammering)"

    # Far enough past the re-arm window that the episode is allowed to climb again.
    monkeypatch.setattr(
        srv,
        "device_hold_escalated_monotonic",
        srv.time.monotonic() - srv.HOLD_ESCALATION_REARM_SEC - 1,
    )
    assert not srv._hold_escalation_latched(), (
        "the latch outlived its re-arm window — a failed recovery has ended the ladder, which is the "
        "permanent hold this expiry exists to make impossible"
    )

    # An episode that never escalated is never latched.
    srv._set_hold_escalated(False)
    assert not srv._hold_escalation_latched()


@pytest.mark.asyncio
async def test_a_bridge_less_chip_is_rescanned_before_anything_destructive(monkeypatch, tmp_path):
    """A chip with no parent bridge is not automatically a departed ASIC. Measured twice on a
    loudbox: the endpoint was still in lspci and still bound to the driver, and only its /dev node
    was missing — a state the gate reports as 'off the PCIe bus' because it counts sysfs nodes. Left
    to the old ladder that routes past every cheap remedy to a reboot or a chassis power cycle, when
    a bare PCI rescan restores it in about a second with no downtime. The rescan must be tried, and
    a chip it brings back must end the recovery there."""
    rescans = []
    monkeypatch.setattr(srv, "isolated_chips", {"0"})
    monkeypatch.setattr(srv, "device_pci_map", {"0": "0000:61:00.0"})
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    # No bridge for this chip -> the per-chip rung is inapplicable, which is the case under test.
    # None (not a falsy string) is what the code reads as "no bridge to write to".
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: None)

    class FakePath:
        def __init__(self, p):
            self.p = str(p)

        def write_text(self, v):
            rescans.append(self.p)

        def exists(self):
            # The node reappears only AFTER a rescan has run — exactly the real behaviour.
            return "tenstorrent!0" in self.p and bool(rescans)

    monkeypatch.setattr(recovery_pkg, "Path", FakePath)

    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda *a, **k: None)

    assert "/sys/bus/pci/rescan" in rescans, "the rescan rung never ran — the cheap recovery was skipped"
    assert ok, "the chip came back on the rescan, so recovery must succeed and not escalate further"


@pytest.mark.asyncio
async def test_prejob_dispatch_probe_holds_a_mesh_that_cannot_run_a_kernel(monkeypatch, tmp_path):
    """The gap that admitted job 142: an UNFLAGGED device skips the pre-job gate entirely, and
    every check it would have run passes on a mesh that cannot dispatch — enumeration and the ARC
    heartbeat are reads, and the fabric pass drives ethernet without enqueuing a program. So the
    probe has to run BEFORE the not-dirty early return, and a failing verdict must flag the device
    so the tenant hold keeps the job at the door."""
    monkeypatch.setenv("TT_DEVICE_MCP_PREJOB_DISPATCH", "1")
    fsm_healthy(srv)  # the case that hurts: nothing flagged
    monkeypatch.setattr(srv, "_device_health_gate", _unreachable_gate)
    marked = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None: marked.append(reason))
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    async def _cannot_dispatch():
        return False, "dispatch probe exited 1"

    monkeypatch.setattr(srv, "_dispatch_probe", _cannot_dispatch)

    await srv._ensure_device_clean_for_next_job(None)

    assert marked, "a mesh that cannot run a kernel was admitted — the probe never flagged it"
    assert "dispatch" in marked[0].lower(), marked[0]


@pytest.mark.asyncio
async def test_prejob_dispatch_skip_never_flags_the_device(monkeypatch, tmp_path):
    """'I could not ask' is not 'the answer is no'. A probe that is not built must leave the device
    exactly as it found it — flagging on a skip would hold every queue on a host that simply has no
    probe binary yet."""
    monkeypatch.setenv("TT_DEVICE_MCP_PREJOB_DISPATCH", "1")
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_health_gate", _unreachable_gate)
    marked = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None: marked.append(reason))

    async def _skipped():
        return None, "skipped (no dispatch probe binary)"

    monkeypatch.setattr(srv, "_dispatch_probe", _skipped)

    await srv._ensure_device_clean_for_next_job(None)

    assert not marked, "a SKIPPED probe flagged the device — absence of evidence became evidence"


async def _unreachable_gate(*a, **k):
    raise AssertionError("the health gate must not run for a clean, non-dirty device")


@pytest.mark.asyncio
async def test_prejob_probe_runs_on_the_clean_device_admission_path(monkeypatch):
    """The probe only protects a tenant if it runs on the path a tenant job actually takes:
    _await_device_free_for_tenant. That path admits an unflagged device with a bare early break, so a
    clean-but-dead mesh — the job-142 case — was let through without ever proving a kernel can run.
    Drive the real admission gate on a device nothing has flagged and assert the probe ran."""
    monkeypatch.setenv("TT_DEVICE_MCP_PREJOB_DISPATCH", "1")
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_healthy(srv)  # unflagged: the case that hurts
    monkeypatch.setattr(srv, "_device_health_gate", _unreachable_gate)  # a healthy probe must not reset

    probed = []

    async def _healthy():
        probed.append(True)
        return True, "a kernel ran to completion on the mesh (0.5s)"

    monkeypatch.setattr(srv, "_dispatch_probe", _healthy)

    await srv._await_device_free_for_tenant(None)

    assert probed, "the pre-job dispatch probe never ran on the clean-device admission path — job-142 gap"


@pytest.mark.asyncio
async def test_prejob_dispatch_probe_records_its_verdict_in_the_job_log(monkeypatch, tmp_path):
    """Proof the gate ran must reach the tenant's OWN job log, not only server.log. The per-job log
    is where a run's history reads back and is the tenant-visible record — every other health-gate
    line lands there (see _device_health_gate). Emit the verdict via logger.info alone and the
    per-job log has no evidence dispatch was ever proven, so a real gate run is indistinguishable
    from one that silently no-op'd — exactly the absence-of-evidence gap the ledger keeps catching.
    Drive the real admission entry on a clean device and assert the verdict landed in the job log."""
    monkeypatch.setenv("TT_DEVICE_MCP_PREJOB_DISPATCH", "1")
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_health_gate", _unreachable_gate)  # a healthy probe must not reset
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    async def _healthy():
        return True, "a kernel ran to completion on the mesh (0.5s)"

    monkeypatch.setattr(srv, "_dispatch_probe", _healthy)

    job_log = tmp_path / "2026-08-14_000000_999.log"
    job_log.write_text("")

    await srv._ensure_device_clean_for_next_job(job_log)

    assert "HEALTH-GATE[pre-job] dispatch probe" in job_log.read_text(), (
        "the pre-job dispatch probe verdict never reached the tenant's job log — goal_check greps "
        "the per-job log, and server.log is not where a run's history reads back"
    )


def test_the_hold_clock_survives_a_broker_restart(monkeypatch, tmp_path):
    """The escalation ceiling is measured from the episode start, so keeping that start only in
    memory lets any restart rewind the ladder's clock — a deploy, a crash, or the reconcile timer
    silently buys the wedge another full ceiling. Measured: three laps held at 5/8 while the relift
    declined each time because the episode looked seconds old. It must persist, and it must clear
    when the device genuinely recovers so a stale file cannot resurrect a dead hold."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)

    srv._persist_hold_episode("2026-08-14T18:00:00")
    assert (
        srv._restore_hold_episode() == "2026-08-14T18:00:00"
    ), "the episode start did not survive — a restart rewinds the escalation ceiling"

    srv._persist_hold_episode("")
    assert srv._restore_hold_episode() == "", "a released episode left its clock on disk"


def test_restoring_a_hold_clock_is_safe_when_none_was_recorded(monkeypatch, tmp_path):
    """No file is the normal first-hold case and must read as 'no prior episode', never raise."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path / "does-not-exist")
    assert srv._restore_hold_episode() == ""


@pytest.mark.asyncio
async def test_dispatch_probe_runs_with_the_runtime_env_its_binary_needs(monkeypatch, tmp_path):
    """These binaries JIT their kernels from the runtime root — the fabric check exports
    TT_METAL_HOME/RUNTIME_ROOT and runs from that tree for exactly this reason. Launch the probe
    without them and it fails on a perfectly HEALTHY mesh, and a false UNHEALTHY from a gate that
    runs before every job holds the whole box. Also pins the cache away from any tenant's."""
    probe = tmp_path / "metal_example_add_2_integers_in_compute"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o755)
    root = tmp_path / "pinned"
    root.mkdir()
    monkeypatch.setattr(srv, "DISPATCH_PROBE_BIN", str(probe))
    monkeypatch.setenv("TTDEV_DISPATCH_RUNTIME_ROOT", str(root))

    seen = {}

    async def fake_exec(*argv, **kw):
        seen.update(kw)

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return P()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)

    ok, detail = await srv._dispatch_probe()

    assert ok is True, detail
    env = seen.get("env") or {}
    assert env.get("TT_METAL_HOME") == str(
        root
    ), "probe launched without TT_METAL_HOME — it would fail on a healthy mesh"
    assert env.get("TT_METAL_RUNTIME_ROOT") == str(root), "probe launched without TT_METAL_RUNTIME_ROOT"
    assert "tenant" not in env.get("TT_METAL_CACHE", ""), "probe must not share a tenant's kernel cache"
    assert seen.get("cwd") == str(root), "probe must run from the pinned runtime root"


@pytest.mark.asyncio
async def test_dispatch_probe_creates_its_cache_dir_so_a_healthy_mesh_never_false_fails(monkeypatch, tmp_path):
    """A missing TT_METAL_CACHE makes the binary exit non-zero on a HEALTHY mesh — a false UNHEALTHY
    that holds the whole box. The sibling fabric and eth-heartbeat checks mkdir their cache before
    launching for exactly this reason; the probe must give itself the same guarantee."""
    probe = tmp_path / "metal_example_add_2_integers_in_compute"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o755)
    root = tmp_path / "pinned"
    root.mkdir()
    cache = tmp_path / "cache" / "dispatch-tt-metal-cache"  # does not exist yet
    monkeypatch.setattr(srv, "DISPATCH_PROBE_BIN", str(probe))
    monkeypatch.setenv("TTDEV_DISPATCH_RUNTIME_ROOT", str(root))
    monkeypatch.setenv("TTDEV_DISPATCH_CACHE", str(cache))

    async def fake_exec(*argv, **kw):
        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return P()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)

    assert not cache.exists()
    ok, detail = await srv._dispatch_probe()

    assert ok is True, detail
    assert cache.is_dir(), "probe did not create its cache dir — a missing dir false-fails a healthy mesh"


@pytest.mark.asyncio
async def test_dispatch_probe_ok_is_the_predicate_the_prejob_gate_admits_on(monkeypatch):
    """The pre-job gate decides admission through _dispatch_probe_ok, and its three-state contract
    is the whole safety property: a failing kernel HOLDS (returns False and flags), a skip or an
    opt-out ADMITS without flagging (not-asked and not-enabled are not failures). A gate that
    admitted on a failing probe, or held on a skip, would either pass job 142 through or wedge
    every queue on a box with no probe."""
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    marked = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None: marked.append(reason))

    async def _fail():
        return False, "dispatch probe exited 1"

    async def _skip():
        return None, "skipped (no dispatch probe binary)"

    monkeypatch.setenv("TT_DEVICE_MCP_PREJOB_DISPATCH", "1")
    monkeypatch.setattr(srv, "_dispatch_probe", _fail)
    assert await srv._dispatch_probe_ok() is False, "a failing kernel must NOT admit a tenant"
    assert marked and "dispatch" in marked[0].lower(), "a failing probe must flag the device"

    marked.clear()
    monkeypatch.setattr(srv, "_dispatch_probe", _skip)
    assert await srv._dispatch_probe_ok() is True, "a skip must admit — not-asked is not a failure"
    assert not marked, "a skip flagged the device — absence of evidence became evidence"

    marked.clear()
    monkeypatch.setenv("TT_DEVICE_MCP_PREJOB_DISPATCH", "0")

    async def _must_not_run():
        raise AssertionError("the probe ran while the gate was opted out")

    monkeypatch.setattr(srv, "_dispatch_probe", _must_not_run)
    assert await srv._dispatch_probe_ok() is True, "an opted-out gate must admit without probing"
    assert not marked


@pytest.mark.asyncio
async def test_the_pre_job_gate_never_runs_the_slow_fabric_pass(monkeypatch, tmp_path, clear_job_state):
    """Pre-job is the one phase whose cost lands on a submitter who is only waiting to start, and
    the traffic pass takes ~45-100s. A dirty flag alone used to force it: one ordinary submit paid
    102s and was held anyway. The flag is already the verdict — re-measuring it bills the tenant
    without changing the answer. Post-job/startup/ladder still run it, with the device idle."""
    # The gate returns before _verify_device on a host with no device nodes and no exclusive
    # view of the holder scan, so both are sandboxed — otherwise this passes only where the
    # broker is installed and reads as a no-op everywhere else.
    for i in range(8):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    _no_holders(monkeypatch)
    seen = {}

    async def fake_verify(expected, log, *, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", fake_verify)
    fsm_dirty(srv, "dirty")  # the case that used to force it
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 1 for i in range(8)})

    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)
    assert seen.get("run_fabric") is False, (
        "the pre-job gate ran the slow fabric pass — a submitter is paying ~100s to be told what "
        "the dirty flag already said"
    )

    seen.clear()
    await srv._device_health_gate(None, phase="post-job", run_fabric=False, force_fabric=True)
    assert seen.get("run_fabric") is True, "post-job must still be able to run the traffic pass"


def test_a_declined_spawn_does_not_burn_the_escalation_window(monkeypatch):
    """The spawn declines while a device op is in flight — a transient condition. If the caller marks
    the window spent anyway, the general clock waits another full ceiling and the off-bus one-shot
    never fires again, so a hold outlives its ceiling with no rung ever attempted."""
    fired, since = _hold_deadline_probe(monkeypatch, isolated={"6", "7"})
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chip(s) 6,7 fell off the PCIe bus")
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: False)  # declined
    offbus = srv._offbus_hold_ceiling_sec()

    srv._check_hold_deadline(now=since + timedelta(seconds=offbus + 1))
    assert srv.device_hold_offbus_escalated is False, "a declined spawn must not consume the one-shot"

    # the device op clears; the very next tick must still be able to escalate
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    srv._check_hold_deadline(now=since + timedelta(seconds=offbus + 2))
    assert len(fired) == 1, "the retry after a decline must fire"
    assert srv.device_hold_offbus_escalated is True


def test_a_declined_spawn_does_not_burn_the_general_ceiling_window(monkeypatch):
    """Same invariant on the general (non-off-bus) clock."""
    fired, since = _hold_deadline_probe(monkeypatch, isolated=set())
    monkeypatch.setattr(srv, "device_hold_episode_reason", "eth/fabric fault on a present mesh")
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: False)
    ceiling = srv._stuck_hold_ceiling_sec()

    srv._check_hold_deadline(now=since + timedelta(seconds=ceiling + 1))
    assert srv.device_hold_escalate_bucket == 0, "a declined spawn must not advance the bucket"

    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    srv._check_hold_deadline(now=since + timedelta(seconds=ceiling + 2))
    assert len(fired) == 1, "the retry after a decline must fire within the same window"


@pytest.mark.asyncio
async def test_cancelling_a_queued_job_forgets_its_spec(monkeypatch, tmp_path, clear_job_state):
    """Cancelling a QUEUED job must drop its persisted spec. Left behind, cleanup evicts the job
    from memory while the spec lingers, and the next restart's queue-restore resurrects a job that
    was killed days ago — the phantom that dispatched onto a wedged fabric. Fails on base:
    _kill_job cancels the job but never forgets its queued spec."""
    from starlette.testclient import TestClient

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    job = srv.Job(
        id="778", owner="tester", workspace="/tmp", command="echo hi", queued_at="t", status=srv.JobStatus.QUEUED
    )
    srv.jobs["778"] = job
    srv._persist_queued_job(job)
    assert srv._queued_spec_path("778").exists(), "precondition: the spec was persisted"

    app = srv.build_asgi_app(srv.create_mcp_server())
    with TestClient(app) as client:
        r = client.post("/api/tt_device_job_kill", json={"job_id": "778", "owner": "tester"}).json()

    assert job.status is srv.JobStatus.KILLED, f"kill did not cancel the queued job: {r}"
    assert not srv._queued_spec_path(
        "778"
    ).exists(), "a cancelled queued job kept its spec — a restart's queue-restore will resurrect it"


@pytest.mark.asyncio
async def test_runner_forgets_the_spec_of_a_job_gone_from_memory(monkeypatch, tmp_path, clear_job_state):
    """A job cancelled-then-cleaned is gone from `jobs` but its queued spec can still be on disk when
    the runner reaches its stale queue id. The runner must drop that spec, or the next restart's
    queue-restore resurrects it. Fails on base: the 'skipping removed' path leaves the spec."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})  # evicted from memory; only the on-disk spec remains
    job = srv.Job(id="779", owner="t", workspace="/tmp", command="echo hi", queued_at="t")
    srv._persist_queued_job(job)
    assert srv._queued_spec_path("779").exists(), "precondition: the spec was persisted"
    await srv.get_job_queue().put("779")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(100):
            if not srv._queued_spec_path("779").exists():
                break
            await asyncio.sleep(0.02)
        assert not srv._queued_spec_path(
            "779"
        ).exists(), "runner skipped a removed job but left its spec — a restart will resurrect it"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_restart_split_hold_row_anchors_at_this_segment_not_the_restored_clock(
    monkeypatch, tmp_path, clear_job_state
):
    """A hold that spans a broker restart keeps its ESCALATION clock counting from the original drop
    (episode_since restored), but its ledger ROW must start at THIS process's segment — else the row
    overlaps the orphaned-close row the restart already wrote for the prior segment, and one hold
    reads as two simultaneous holds. Fails on base: the row anchored at the restored clock."""
    from datetime import datetime, timedelta

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "_hold_row", None, raising=False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    old = (datetime.now() - timedelta(hours=1)).isoformat()  # the drop, an hour ago, before the restart
    monkeypatch.setattr(srv, "_restore_hold_episode", lambda: old)
    monkeypatch.setattr(srv, "_persist_hold_episode", lambda *a, **k: None)

    srv._note_tenant_gate_verdict("device is dirty and unverified: chip off the bus")

    assert srv.device_hold_episode_since == old, "escalation clock must keep counting from the original drop"
    assert srv._hold_row is not None
    row_start = srv._hold_row["started_at"]
    assert row_start != old, "the ledger row must not anchor at the restored escalation clock"
    age = (datetime.now() - datetime.fromisoformat(row_start)).total_seconds()
    assert age < 60, f"the hold row should start at this process's segment (~now), not {age:.0f}s ago"


def test_forced_escalation_names_the_watchdog_clock_that_fired(monkeypatch):
    """cs04 2026-09-16 05:02:26: "device HELD past the ceiling" logged 122s into the hold. Two
    watchdog clocks spawn the forced ladder (ladder-v2 removed the separate risky-floor window); the
    message must name the one that did."""
    from datetime import datetime, timedelta

    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: True)
    monkeypatch.setattr(srv, "_offbus_hold_ceiling_sec", lambda: 120)
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 600)
    monkeypatch.setattr(srv, "_hold_deadline_sec", lambda: 10**6)
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)
    monkeypatch.setattr(srv, "isolated_chips", {"1"})
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_escalation_trigger", "")

    def held_for(sec):
        monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=sec)).isoformat())
        srv._check_hold_deadline()
        return srv.device_hold_escalation_trigger

    assert held_for(122) == "the 120s off-bus one-shot"
    assert held_for(603) == "the 600s ceiling (window 1)"
