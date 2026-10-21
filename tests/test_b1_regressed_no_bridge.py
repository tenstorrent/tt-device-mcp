# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A partial off-bus hold whose only reset regressed it, on a host with no bridge to reset (class B1).

Replay of one 8-chip host with no PCIe bridge above its chips: a hung job's post-job mesh reset left
chip 1 off the bus, so the bridge reset (SBR) has no route and the mesh reset is latched as regressed
for the episode (spec 04 I31). The first forced pass came 96 s into the hold. Before spec 04 I79 that
pass deferred the power cycle to the 600 s ladder end with no rung left to try; the hold was released
at 903 s. These run the real last-chance sweep (I22) with only the reset, bridge, verify and power
cycle stubbed, and pin: no deferral, the kept rungs run once (SBR attempted and inapplicable, the
sweep's one mesh reset, then the full check with fabric), then the power cycle; a passing full check
is the only thing that releases the hold; a cycle held off on the first pass is asked again at the
10-min mark. No test reaches a real reset, reboot or power cycle."""

from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold
from tt_device_mcp import privileges
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import galaxy as galaxy_mod
from tt_device_mcp.health.recovery import pcie_guard

EXPECTED = 8
OFF_CHIP = "1"


def _episode(monkeypatch, *, held_sec=96, check_passes=False):
    """Arm the replayed episode. Returns (counters, events, order): ``order`` lists the rungs in the
    order they ran; ``counters['fabric_checks']`` counts full checks that included the fabric."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=1, isolated={OFF_CHIP}, reset_result=False)
    beats = {str(i): 100 for i in range(EXPECTED) if str(i) != OFF_CHIP}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: beats)
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=held_sec)).isoformat()
    srv.device_hold_episode_reason = "device unverified: gate/post-job: job ended hung"
    # The post-job gate's reset regressed the mesh 0 -> 1 off the bus and latched the episode (I31).
    srv.recovery_mechanism.note_reset_regressed(srv.device_hold_episode_since)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(privileges, "can_ipmi", lambda: True)
    events, order = [], []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    counters.update(sbr=0, mesh_resets=0, checks=0, fabric_checks=0)

    async def no_bridge(log):
        # The chip has no bridge above it: the SBR cannot fire (bridge_reset_failed reason=no_bridge).
        counters["sbr"] += 1
        order.append("sbr")
        galaxy_mod.health_event("bridge_reset_failed", chip=OFF_CHIP, reason="no_bridge")
        return False

    async def mesh_reset(argv, log):
        counters["mesh_resets"] += 1
        order.append("mesh_reset")
        return True

    async def full_check(expected, log, run_fabric=True, **_):
        counters["checks"] += 1
        counters["fabric_checks"] += bool(run_fabric)
        order.append("check")
        return check_passes, {"snapshot": {"ok": check_passes}}

    async def power_cycle(log, reason):
        counters["power_cycles"] += 1
        order.append("power_cycle")

    patch_recovery(monkeypatch, "_recover_isolated_chips", no_bridge)
    patch_recovery(monkeypatch, "_verify_device", full_check)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", mesh_reset)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(srv.galaxy_recovery, "_tray_map_now", lambda: None)
    monkeypatch.setattr(srv.galaxy_recovery, "_chip_buses_now", lambda: {})
    monkeypatch.setattr(galaxy_mod, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(galaxy_mod, "refuse_mesh_reset", lambda *a, **k: False)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 1)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)
    monkeypatch.setattr(pcie_guard, "begin_offbus_reset", lambda *a, **k: None)
    monkeypatch.setattr(pcie_guard, "end_offbus_reset", lambda *a, **k: None)
    return counters, events, order


async def _forced_pass():
    return await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)


def _kinds(events):
    return [kind for kind, _ in events]


@pytest.mark.asyncio
async def test_no_bridge_regressed_hold_power_cycles_on_the_first_pass_after_the_kept_rungs(
    monkeypatch, clear_job_state
):
    """Spec 04 I79/I31/I22: no deferral to the ladder end. The kept rungs run once each (the SBR, found
    inapplicable; the sweep's one mesh reset; the full check with fabric), then the power cycle, on the
    pass at 96 s. Fails before I79: host_rung_deferred_until_ladder_end, no sweep, no cycle."""
    counters, events, order = _episode(monkeypatch)
    assert await _forced_pass() is True
    kinds = _kinds(events)
    assert "galaxy_reset_skipped_regressed" in kinds
    assert "host_rung_deferred_until_ladder_end" not in kinds
    reasons = [f.get("reason") for kind, f in events if kind == "host_rung_not_deferred"]
    assert reasons == ["reset_regressed_offbus"]
    assert ("bridge_reset_failed", {"chip": OFF_CHIP, "reason": "no_bridge"}) in events
    assert order == ["sbr", "mesh_reset", "check", "power_cycle"], "kept rungs once each, then the cycle"
    assert counters["resets"] == 0, "the regressed episode's own mesh-reset rung is not repeated (I31)"
    assert counters["fabric_checks"] == 1, "the verify before the cycle is the full check, fabric included"
    assert counters["reboots"] == 0, "a dropped chip does not come back on a warm reboot: the cold rung"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed check keeps the hold"


@pytest.mark.asyncio
async def test_no_bridge_regressed_hold_released_only_by_a_passing_full_check(monkeypatch, clear_job_state):
    """Spec 04 I32 / 03 I4: the sweep's full check, fabric included, passes: the hold lifts on that
    check, and no power cycle fires. Fails before I79, which deferred and ran no sweep or check."""
    counters, events, order = _episode(monkeypatch, check_passes=True)
    assert await _forced_pass() is True
    assert order == ["sbr", "mesh_reset", "check"], "released on the check, no cycle"
    assert counters["fabric_checks"] == 1
    assert counters["dirty_cleared"] == 1 and counters["fault_cleared"] == 1
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0


@pytest.mark.asyncio
async def test_no_bridge_regressed_hold_with_the_cycle_held_off_is_asked_again_at_the_ten_minute_mark(
    monkeypatch, clear_job_state
):
    """A power cycle held off on the 96 s pass (a cooldown, the rate limit) returns before the sweep and
    keeps the hold, with no release. The forced pass at the 10-min mark runs the kept rungs and asks for
    the cycle (spec 04 I76/I79)."""
    counters, events, order = _episode(monkeypatch)
    allowed = {"ok": False}
    monkeypatch.setattr(
        srv.recovery_mechanism,
        "auto_recovery_allowed",
        lambda escalation, tenant_active=False: (allowed["ok"], "" if allowed["ok"] else "cooldown"),
    )
    assert await _forced_pass() is True
    assert order == [], "a held-off cycle runs no sweep this pass"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    assert "host_rung_deferred_until_ladder_end" not in _kinds(events)
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=601)).isoformat()
    srv.recovery_mechanism.note_reset_regressed(srv.device_hold_episode_since)
    allowed["ok"] = True
    assert await _forced_pass() is True
    assert order == ["sbr", "mesh_reset", "check", "power_cycle"]
    assert counters["fabric_checks"] == 1 and counters["dirty_cleared"] == 0
