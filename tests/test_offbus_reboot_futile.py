# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I71: on a multi-chip mesh with chips off the bus the host rung is the power cycle; a
present mesh (off_bus == 0) and a single-chip host keep the warm reboot first. A power cycle that is
denied or does not launch falls back to the warm reboot on a drop the reboot can still recover. With
no power cycle armed the older verdict stands unchanged."""

import pytest

from tests.conftest import patch_health_event
from tests.test_device_safety import (
    _arm_offbus_stuck_hold,
    _arm_stuck_hold,
    _reach_reboot_rung_with_regressing_reset,
    _rung,
)
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import BLOCKED, DEFER, STAGE_HOST_REBOOT, STAGE_POWER_CYCLE, WAIT
from tt_device_mcp.health.recovery import galaxy as galaxy_mod


def _choose(off_bus, expected, *, reboot, power_cycle, futile=False, attempted=False):
    return galaxy_mod._host_escalation_for_drop(
        off_bus,
        expected,
        warm_reboot_futile=futile,
        auto_reboot_enabled=lambda: reboot,
        auto_power_cycle_enabled=lambda: power_cycle,
        reboot_already_attempted=lambda: attempted,
    )


@pytest.mark.parametrize("off_bus", [1, 8, 16, 31, 32])
def test_multi_chip_offbus_takes_the_power_cycle_over_the_reboot_when_armed(off_bus):
    """I71: chips off the bus on a multi-chip host, both rungs armed -> power cycle."""
    assert _choose(off_bus, 32, reboot=True, power_cycle=True) == ("power-cycle", False)


def test_a_present_mesh_keeps_the_reboot_first():
    """Review B1: a present mesh (classes D and G2) keeps the warm reboot first when both rungs are
    armed; the power cycle follows a reboot already attempted, or stands alone with no reboot."""
    assert _choose(0, 32, reboot=True, power_cycle=True) == ("reboot", False)
    assert _choose(0, 32, reboot=True, power_cycle=True, attempted=True) == ("power-cycle", False)
    assert _choose(0, 32, reboot=False, power_cycle=True) == ("power-cycle", False)


def test_single_chip_keeps_the_reboot_first():
    """I71 is multi-chip only: a single-chip host has no UBB re-enumeration failure mode."""
    assert _choose(1, 1, reboot=True, power_cycle=True) == ("reboot", False)
    assert _choose(0, 1, reboot=True, power_cycle=True) == ("reboot", False)


def test_without_a_power_cycle_the_old_verdict_stands():
    """No host loses a rung it had: a partial drop the caller has not proven futile still reboots;
    a whole-bus drop or a proven-futile drop is BLOCKED (loud), never a futile reboot."""
    assert _choose(8, 32, reboot=True, power_cycle=False) == ("reboot", False)
    assert _choose(0, 32, reboot=True, power_cycle=False) == ("reboot", False)
    assert _choose(32, 32, reboot=True, power_cycle=False) == (None, True)
    assert _choose(8, 32, reboot=True, power_cycle=False, futile=True) == (None, True)
    assert _choose(8, 32, reboot=False, power_cycle=False) == (None, False)


def test_the_reboot_fallback_is_only_for_drops_a_reboot_can_recover():
    """Review B2: the in-band fallback covers a present mesh, a single chip and a partial drop the
    caller has not proven futile; never a whole-bus drop or a proven-futile one."""
    eligible = galaxy_mod._reboot_fallback_eligible
    assert eligible(0, 32, warm_reboot_futile=False)
    assert eligible(1, 1, warm_reboot_futile=True)
    assert eligible(16, 32, warm_reboot_futile=False)
    assert not eligible(16, 32, warm_reboot_futile=True)
    assert not eligible(32, 32, warm_reboot_futile=False)


def _arm_partial_drop(monkeypatch, off_bus):
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=off_bus, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    return counters


def _deny(monkeypatch, *denied):
    asked = []

    def allowed(action, **_):
        asked.append(action)
        return (False, "boot-loop cooldown") if action in denied else (True, "ok")

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize("off_bus", [8, 16])
async def test_partial_drop_past_the_ladder_cycles_without_a_reboot(monkeypatch, clear_job_state, off_bus):
    """8 off (below the floor, already futile) and 16 off (at the floor, newly cycled first): the
    full ladder runs first (mesh reset attempted), then the power cycle; the warm reboot never fires
    though it is armed."""
    counters = _arm_partial_drop(monkeypatch, off_bus)
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "the ladder runs before the host rung (only whole-tray skips it, I37)"
    assert counters["power_cycles"] == 1 and counters["reboots"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_present_mesh_wedge_reboots_first_with_both_rungs_armed(monkeypatch, clear_job_state):
    """Review B1, class D: a present-mesh wedge the galaxy reset could not clear takes the warm
    reboot first when both rungs are armed (the power cycle follows a failed reboot)."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={str(i): 100 for i in range(32)})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert counters["reboots"] == 1 and counters["power_cycles"] == 0


@pytest.mark.asyncio
async def test_present_mesh_wedge_still_reboots_with_no_power_cycle(monkeypatch, clear_job_state):
    """With the power cycle off the present-mesh wedge keeps its warm reboot rung."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={str(i): 100 for i in range(32)})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["reboots"] == 1 and counters["power_cycles"] == 0


@pytest.mark.asyncio
async def test_a_denied_cycle_falls_back_to_the_reboot_on_a_recoverable_drop(monkeypatch, clear_job_state):
    """Review B2: 16 of 32 off (not futile), the power cycle denied by the cooldown -> the warm
    reboot fires now instead of holding to the cooldown's end. The fault is never cleared."""
    counters = _arm_partial_drop(monkeypatch, 16)
    asked = _deny(monkeypatch, "power-cycle")
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append((name, k)))

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert asked[:2] == ["power-cycle", "reboot"]
    assert counters["reboots"] == 1 and counters["power_cycles"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0
    assert any(n == "power_cycle_reboot_fallback" and k.get("trigger") == "denied" for n, k in events)


@pytest.mark.asyncio
async def test_both_rungs_denied_holds(monkeypatch, clear_job_state):
    """Review B2: with the reboot held off too, the climb holds (and asks again next pass); it never
    clears the fault."""
    counters = _arm_partial_drop(monkeypatch, 16)
    _deny(monkeypatch, "power-cycle", "reboot")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert counters["reboots"] == 0 and counters["power_cycles"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_a_cooldown_denial_on_a_futile_drop_holds_and_never_reboots(monkeypatch, clear_job_state):
    """8 of 32 off is below the floor, so a warm reboot is futile: a denied power cycle holds and
    asks again later; it never takes the warm reboot instead, and never clears the fault."""
    counters = _arm_partial_drop(monkeypatch, 8)
    asked = _deny(monkeypatch, "power-cycle")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert asked and set(asked) == {"power-cycle"}, "only the power cycle is asked for"
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


def _cycle_never_launches(monkeypatch):
    calls = {"n": 0}

    async def failed(log, reason):
        calls["n"] += 1
        return False

    monkeypatch.setattr(srv, "_auto_power_cycle_host", failed)
    return calls


@pytest.mark.asyncio
async def test_a_cycle_that_does_not_launch_falls_back_to_the_reboot(monkeypatch, clear_job_state):
    """Review B2: an unreachable BMC (the cycle did not launch) on a recoverable drop takes the warm
    reboot at once instead of retrying the cycle with no bound."""
    counters = _arm_partial_drop(monkeypatch, 16)
    cycles = _cycle_never_launches(monkeypatch)
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append((name, k)))

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert cycles["n"] == 1 and counters["reboots"] == 1
    assert any(n == "power_cycle_reboot_fallback" and k.get("trigger") == "launch_failed" for n, k in events)


@pytest.mark.asyncio
async def test_a_cycle_that_does_not_launch_on_a_futile_drop_never_reboots(monkeypatch, clear_job_state):
    counters = _arm_partial_drop(monkeypatch, 8)
    cycles = _cycle_never_launches(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert cycles["n"] == 1 and counters["reboots"] == 0


def _gate_partial_drop(monkeypatch, tmp_path):
    reboots = _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before=16, off_after=16)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    return reboots


@pytest.mark.asyncio
async def test_the_gate_falls_back_to_the_reboot_when_the_cycle_is_denied(monkeypatch, tmp_path, clear_job_state):
    """Review B2 at the gate: a 16-of-32 drop the reset did not recover names the power cycle; the
    cooldown denies it, so the warm reboot fires as the in-band fallback."""
    reboots = _gate_partial_drop(monkeypatch, tmp_path)
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    _deny(monkeypatch, "power-cycle")

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert cycles["n"] == 0 and reboots["n"] == 1


@pytest.mark.asyncio
async def test_the_gate_falls_back_to_the_reboot_when_the_cycle_does_not_launch(monkeypatch, tmp_path, clear_job_state):
    reboots = _gate_partial_drop(monkeypatch, tmp_path)
    cycles = _cycle_never_launches(monkeypatch)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert cycles["n"] == 1 and reboots["n"] == 1


@pytest.mark.asyncio
async def test_the_gate_cycles_a_partial_drop_when_allowed(monkeypatch, tmp_path, clear_job_state):
    """The control for the two gate fallbacks: allowed and launched, the cycle fires alone."""
    reboots = _gate_partial_drop(monkeypatch, tmp_path)
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert cycles["n"] == 1 and reboots["n"] == 0


def test_next_stage_names_the_power_cycle_for_a_failed_reset_partial_drop(monkeypatch, health_deps):
    """The router: a was-failing partial drop the reset did not recover, both rungs armed ->
    STAGE_POWER_CYCLE, never STAGE_HOST_REBOOT."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    d = _rung(monkeypatch, health_deps, off_bus=16, was_failing=True, reset_recovered=False, off_bus_before=16)
    assert d == STAGE_POWER_CYCLE


def test_next_stage_present_mesh_reboots_first_with_both_rungs_armed(monkeypatch, health_deps):
    """Review B1, class G2: a present mesh the reset did not recover names the warm reboot first."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    d = _rung(monkeypatch, health_deps, off_bus=0, was_failing=True, reset_recovered=False)
    assert d == STAGE_HOST_REBOOT


def test_next_stage_present_mesh_with_no_power_cycle_still_reboots(monkeypatch, health_deps):
    d = _rung(monkeypatch, health_deps, off_bus=0, was_failing=True, reset_recovered=False, host_escalation="reboot")
    assert d == STAGE_HOST_REBOOT


def test_a_live_scope_waits_instead_of_blocking(monkeypatch, health_deps):
    """A whole-bus drop the reboot cannot recover (no power cycle armed): under a live reset scope
    the router waits for that scope; BLOCKED only when nothing is in flight."""
    d = _rung(
        monkeypatch,
        health_deps,
        off_bus=32,
        was_failing=True,
        reset_recovered=False,
        scope_active=True,
        host_escalation="reboot",
    )
    assert d in (WAIT, DEFER)
    d = _rung(monkeypatch, health_deps, off_bus=32, was_failing=True, reset_recovered=False, host_escalation="reboot")
    assert d == BLOCKED
