# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I71: on a multi-chip mesh with chips off the bus the host rung is the power cycle; a
present mesh (off_bus == 0) and a single-chip host keep the warm reboot first. A power cycle that is
denied or does not launch falls back to the warm reboot on a drop the reboot can still recover. With
no power cycle armed the older verdict stands unchanged."""

import json
import subprocess
import time
import types

import pytest

from tests.conftest import patch_health_event
from tests.test_device_safety import (
    _arm_77_reboot_boot_cold_rung,
    _arm_offbus_stuck_hold,
    _arm_stuck_hold,
    _ledger_dir,
    _reach_reboot_rung_with_regressing_reset,
    _rung,
)
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import BLOCKED, DEFER, STAGE_POWER_CYCLE, WAIT
from tt_device_mcp.health.recovery import base as recovery_base
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


def test_the_i79_cold_flag_sends_a_present_mesh_to_the_power_cycle():
    """I79 on top of I71: a present mesh on the boot a 77 climb's warm reboot brought up carries the
    cold flag (``warm_reboot_futile``). That reboot already ran and failed, so the drop takes the
    power cycle even when the reboot is armed and no reboot is on the ledger, holds loudly with no
    cycle armed, and is never eligible for the I71 fallback reboot. A single-chip host is unchanged."""
    assert _choose(0, 32, reboot=True, power_cycle=True, futile=True) == ("power-cycle", False)
    assert _choose(0, 32, reboot=True, power_cycle=False, futile=True) == (None, True)
    assert not galaxy_mod._reboot_fallback_eligible(0, 32, warm_reboot_futile=True)
    assert _choose(0, 1, reboot=True, power_cycle=True, futile=True) == ("reboot", False)


@pytest.mark.asyncio
async def test_the_cold_rung_on_the_77_reboots_boot_never_falls_back_to_a_second_reboot(monkeypatch, clear_job_state):
    """I79 with I71 merged: on the 77 reboot's boot (every chip present) with both rungs armed, the
    reboot's ledger interval passed and the fallback reboot itself allowed, a cooldown-denied power
    cycle holds; it never takes the I71 fallback reboot. Once the cooldown ends, one power cycle
    fires and no reboot. Held throughout: nothing is released unverified."""
    counters, g, alerts, reasons, events, age, asked = _arm_77_reboot_boot_cold_rung(monkeypatch)
    age["v"] = 250
    monkeypatch.setattr(srv.recovery_mechanism, "_reboot_already_attempted", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "fallback_reboot_allowed", lambda **_: (True, "ok"))
    cooldown = {"on": True}

    def allowed(esc, *, tenant_active):
        asked.append(esc)
        if cooldown["on"] and esc == "power-cycle":
            return False, "power-cycle cooldown (I65)"
        return True, "ok"

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    assert await g._escalate_stuck_hold(["0", "1"], 32, lambda m: None) is True
    assert asked == ["power-cycle"], "the cold rung only; no warm reboot asked for"
    assert reasons == [] and counters["power_cycles"] == 0

    cooldown["on"] = False
    assert await g._escalate_stuck_hold(["0", "1"], 32, lambda m: None) is True
    assert counters["power_cycles"] == 1 and reasons == [], "one power cycle, never a second reboot"
    assert counters["resets"] == 0 and counters["dirty_cleared"] == 0, "held, never released unverified"


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


# ---- review #1246 F1: one fallback reboot per wedge ------------------------------------------------


def _reboot_already_ran(monkeypatch, *, verified_before=True):
    """The ledger holds a broker reboot 10 min ago and no verified-healthy release since it: the wedge
    it was fired for is still on the box."""
    now = time.time()
    rec = {"action": "reboot", "boot_id": "boot-before", "at_epoch": now - 600}
    monkeypatch.setattr(srv.recovery_mechanism, "read_auto_recovery_ledger", lambda: [rec])
    verified = {"boot_id": "boot-older", "at_epoch": now - 7200} if verified_before else None
    monkeypatch.setattr(srv.recovery_mechanism, "last_verified_release", lambda: verified)


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_before", [True, False])
async def test_a_denied_cycle_takes_no_second_fallback_reboot_on_the_idle_climb(
    monkeypatch, clear_job_state, verified_before
):
    """F1, idle climb, denied cycle: a reboot already ran for this wedge, so the fallback is spent and
    the hold waits for the cycle at its cooldown end. Only the cycle is asked for the host rung."""
    counters = _arm_partial_drop(monkeypatch, 16)
    _reboot_already_ran(monkeypatch, verified_before=verified_before)
    asked = _deny(monkeypatch, "power-cycle")
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert "reboot" not in asked, "a spent fallback never even asks the governor for a reboot"
    assert counters["reboots"] == 0 and counters["power_cycles"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0
    assert "power_cycle_reboot_fallback" not in events


@pytest.mark.asyncio
async def test_a_cycle_that_does_not_launch_takes_no_second_fallback_reboot(monkeypatch, clear_job_state):
    """F1, idle climb, launch failure: the same one-per-wedge rule."""
    counters = _arm_partial_drop(monkeypatch, 16)
    _reboot_already_ran(monkeypatch)
    cycles = _cycle_never_launches(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)

    asked = _deny(monkeypatch)  # the cycle is allowed; it just does not launch

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert cycles["n"] == 1 and counters["reboots"] == 0
    assert "reboot" not in asked


@pytest.mark.asyncio
async def test_the_gate_takes_no_second_fallback_reboot_when_the_cycle_is_denied(
    monkeypatch, tmp_path, clear_job_state
):
    """F1 at the gate, denied cycle: the fallback is spent, so the gate holds (the cycle is asked
    again at the cooldown end) and never reboots or releases."""
    reboots = _gate_partial_drop(monkeypatch, tmp_path)
    _reboot_already_ran(monkeypatch)
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)
    asked = _deny(monkeypatch, "power-cycle")
    cleared = []
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **kw: cleared.append(kw))

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert cycles["n"] == 0 and reboots["n"] == 0
    assert "reboot" not in asked
    assert cleared == [], "the held drop is never released"


@pytest.mark.asyncio
async def test_the_gate_takes_no_second_fallback_reboot_when_the_cycle_does_not_launch(
    monkeypatch, tmp_path, clear_job_state
):
    reboots = _gate_partial_drop(monkeypatch, tmp_path)
    _reboot_already_ran(monkeypatch)
    cycles = _cycle_never_launches(monkeypatch)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert cycles["n"] == 1 and reboots["n"] == 0


@pytest.mark.parametrize(
    "ledger, verified_ago, spent",
    [
        ([], 7200, False),  # no reboot at all
        ([("reboot", 600)], 7200, True),  # a reboot since the verified release
        ([("reboot", 600)], 300, False),  # a verified release followed it: it healed, a new wedge
        ([("power-cycle", 600)], 7200, False),  # only a cycle: the reboot is still untried
        ([("reboot", 600)], None, True),  # nothing verified on record: a recent reboot counts
        ([("reboot", recovery_base.POWER_CYCLE_BACKOFF_CAP_SEC + 60)], None, False),  # too old to hold the cycle
        ([("reboot", None)], 7200, True),  # an unreadable stamp counts (the cycle side is the safe one)
    ],
)
def test_reboot_fallback_spent_counts_a_reboot_since_the_last_verified_release(
    monkeypatch, ledger, verified_ago, spent
):
    now = 1_900_000_000.0
    recs = [{"action": a, "boot_id": "b0", "at_epoch": None if ago is None else now - ago} for a, ago in ledger]
    rm = srv.recovery_mechanism
    monkeypatch.setattr(rm, "read_auto_recovery_ledger", lambda: recs)
    verified = None if verified_ago is None else {"boot_id": "b1", "at_epoch": now - verified_ago}
    monkeypatch.setattr(rm, "last_verified_release", lambda: verified)
    assert bool(rm.reboot_fallback_spent(now_epoch=now)) is spent
    if spent:
        monkeypatch.setattr(rm, "auto_recovery_allowed", lambda *a, **k: (True, "ok"))
        allowed, why = rm.fallback_reboot_allowed(tenant_active=False, now_epoch=now)
        assert allowed is False and "already ran for this wedge" in why


@pytest.mark.asyncio
async def test_an_unhealed_fallback_reboot_hands_the_next_rung_to_the_cycle_with_the_real_governor(
    monkeypatch, clear_job_state, tmp_path
):
    """F1 with the real governor and ledger: two unhealed cycles double the I65 interval to 1 h, then
    a fallback reboot ran and came back still wedged. At record + 1 h the reboot's own interval has
    lapsed but the cycle's cooldown (anchored on that reboot's boot) has not: the old code rebooted
    again here, and on every hour after, so the cycle never fired. Now the drop holds, and the cycle
    fires the moment the cooldown ends."""
    counters = _arm_partial_drop(monkeypatch, 16)
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(recovery_base, "POWER_CYCLE_MIN_INTERVAL_SEC", 1800)
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    monkeypatch.delenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, raising=False)
    rm = srv.recovery_mechanism
    for name in ("read_auto_recovery_ledger", "auto_recovery_allowed", "last_verified_release"):
        monkeypatch.setattr(rm, name, getattr(type(rm), name).__get__(rm))
    reboot_at = 1_900_000_000.0
    boot_btime = reboot_at + 60  # the reboot's boot, still wedged
    (tmp_path / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in (
                {"action": "power-cycle", "boot_id": "boot-A", "at_epoch": reboot_at - 2 * 3600},
                {"action": "power-cycle", "boot_id": "boot-B", "at_epoch": reboot_at - 3600},
                {"action": "reboot", "boot_id": "boot-C", "at_epoch": reboot_at},
            )
        )
    )
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-D")
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(boot_btime)))
    clk = [reboot_at + 3630]  # the reboot interval has lapsed; the 1 h cooldown ends at boot + 3600
    fake_time = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith("_")})
    fake_time.time = lambda: clk[0]
    monkeypatch.setattr(recovery_base, "time", fake_time)
    assert rm.power_cycle_interval(now_epoch=clk[0]) == 3600
    assert rm.auto_recovery_allowed("reboot", tenant_active=False)[0] is True, "the old loop's opening"
    assert rm.power_cycle_cooldown()["until"] == boot_btime + 3600

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert counters["reboots"] == 0 and counters["power_cycles"] == 0, "held for the cycle, no second reboot"

    clk[0] = boot_btime + 3600  # the I65 retry: the watchdog's forced pass at the cooldown end
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert counters["power_cycles"] == 1 and counters["reboots"] == 0, "the cycle fires at the cooldown end"
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


# ---- review #1246 N-a: the launch verdict the I71 fallback reads -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, returns, kept, pending",
    [
        ("ledger write fails", False, False, False),  # nothing fired: the fallback may run
        ("fire raises", False, False, False),  # did not launch: retracted, no pending request
        ("fire times out", True, False, True),  # may still act: no fallback, resets stay off
        ("subprocess timeout", True, False, True),
        ("fire returns", True, True, True),  # issued: the record stays as the rate limit
    ],
)
async def test_fire_recovery_escalation_reports_whether_the_action_may_have_launched(
    monkeypatch, tmp_path, case, returns, kept, pending
):
    """``_fire_recovery_escalation`` returns False only when the action certainly did not launch (the
    I71 launch-failure fallback fires only then); a timeout or a normal return reads True."""
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    rm = srv.recovery_mechanism
    monkeypatch.setattr(rm, "host_action_requested", None)
    if case == "ledger write fails":
        monkeypatch.setattr(rm, "record_auto_recovery", lambda *a, **k: False)
    fired = []

    def fire():
        fired.append(case)
        if case == "fire raises":
            raise FileNotFoundError("ipmitool")
        if case == "fire times out":
            raise recovery_base.HostActionTimedOut("no answer")
        if case == "subprocess timeout":
            raise subprocess.TimeoutExpired("ipmitool", 30)

    got = await rm._fire_recovery_escalation(
        "power-cycle",
        row_owner="[broker]power-cycle-request",
        label="auto BMC power cycle",
        detail="the ladder could not recover",
        fire=fire,
        log=lambda m: None,
        reason="wedge",
    )
    assert got is returns
    assert fired == ([] if case == "ledger write fails" else [case]), "no record, no fire"
    ledger = tmp_path / recovery_base.AUTO_RECOVERY_LEDGER
    recs = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()] if ledger.exists() else []
    assert any(r.get("action") == "power-cycle" for r in recs) is kept
    assert (rm.host_action_requested is not None) is pending
    if pending:
        assert rm.host_action_requested[0] == "power-cycle"
