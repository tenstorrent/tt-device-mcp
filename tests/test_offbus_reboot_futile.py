"""Spec 04 I71: on a multi-chip mesh the host rung is the power cycle; a warm reboot is not taken
while the power cycle is armed. With no power cycle armed the older verdict stands unchanged."""

import pytest

from tests.conftest import patch_health_event
from tests.test_device_safety import _arm_offbus_stuck_hold, _arm_stuck_hold, _rung
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import BLOCKED, DEFER, STAGE_HOST_REBOOT, STAGE_POWER_CYCLE, WAIT
from tt_device_mcp.health.recovery import galaxy as galaxy_mod


def _choose(off_bus, expected, *, reboot, power_cycle, futile=False):
    return galaxy_mod._host_escalation_for_drop(
        off_bus,
        expected,
        warm_reboot_futile=futile,
        auto_reboot_enabled=lambda: reboot,
        auto_power_cycle_enabled=lambda: power_cycle,
        reboot_already_attempted=lambda: False,
    )


@pytest.mark.parametrize("off_bus", [0, 1, 8, 16, 31, 32])
def test_multi_chip_takes_the_power_cycle_over_the_reboot_when_armed(off_bus):
    """I71: present mesh, partial drop or whole-bus drop alike, both rungs armed -> power cycle."""
    assert _choose(off_bus, 32, reboot=True, power_cycle=True) == ("power-cycle", False)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("off_bus", [8, 16])
async def test_partial_drop_past_the_ladder_cycles_without_a_reboot(monkeypatch, clear_job_state, off_bus):
    """B1 (one tray, below the floor) and B2 (mass partial, at the floor): the full ladder runs first
    (mesh reset attempted), then the power cycle; the warm reboot never fires though it is armed."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=off_bus, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)

    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "the ladder runs before the host rung (only whole-tray skips it, I37)"
    assert counters["power_cycles"] == 1 and counters["reboots"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_present_mesh_wedge_cycles_instead_of_rebooting(monkeypatch, clear_job_state):
    """D: a present-mesh wedge the galaxy reset could not clear climbs to the power cycle, not the
    warm reboot, when both are armed."""
    counters = _arm_stuck_hold(monkeypatch, reset_result=False, beats_after_reset={str(i): 100 for i in range(32)})
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    ran = await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True and counters["resets"] == 1
    assert counters["power_cycles"] == 1 and counters["reboots"] == 0


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
async def test_a_cooldown_denial_holds_and_never_falls_back_to_the_reboot(monkeypatch, clear_job_state):
    """A governor / I65 cooldown denial of the power cycle holds and asks again later; it never
    takes the warm reboot instead, and never clears the fault."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    asked = []

    def deny(action, **_):
        asked.append(action)
        return False, "boot-loop cooldown"

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", deny)
    patch_health_event(monkeypatch, lambda *a, **k: None)

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert asked and set(asked) == {"power-cycle"}, "only the power cycle is asked for"
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


def test_next_stage_names_the_power_cycle_for_a_failed_reset_partial_drop(monkeypatch, health_deps):
    """The router: a was-failing partial drop the reset did not recover, both rungs armed ->
    STAGE_POWER_CYCLE, never STAGE_HOST_REBOOT."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    d = _rung(monkeypatch, health_deps, off_bus=16, was_failing=True, reset_recovered=False, off_bus_before=16)
    assert d == STAGE_POWER_CYCLE


def test_next_stage_present_mesh_with_no_power_cycle_still_reboots(monkeypatch, health_deps):
    d = _rung(monkeypatch, health_deps, off_bus=0, was_failing=True, reset_recovered=False, host_escalation="reboot")
    assert d == STAGE_HOST_REBOOT


def test_a_live_scope_waits_instead_of_blocking(monkeypatch, health_deps):
    """A reboot-blocked whole-bus drop under a live reset scope waits for that scope; BLOCKED only
    when nothing is in flight."""
    d = _rung(monkeypatch, health_deps, off_bus=32, scope_active=True, host_escalation="reboot")
    assert d in (WAIT, DEFER)
    d = _rung(monkeypatch, health_deps, off_bus=32, was_failing=True, reset_recovered=False, host_escalation="reboot")
    assert d == BLOCKED
