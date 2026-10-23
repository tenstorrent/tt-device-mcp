# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I71: the power cycle is the only host rung. With chips off the bus or a present mesh the
reset ladder cannot recover, the climb takes the power cycle; a cycle the governor denies, or one
that does not launch, keeps the hold, pages and is asked for again. Nothing stands in for it: there
is no warm reboot, tray re-power or repeat mesh reset, and no setting restores one."""

import json
import subprocess
import time
import types

import pytest

from tests.conftest import patch_health_event
from tests.test_device_safety import (
    _arm_offbus_stuck_hold,
    _ledger_dir,
    _reach_reboot_rung_with_regressing_reset,
    _rung,
)
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import BLOCKED, DEFER, STAGE_POWER_CYCLE, WAIT
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy as galaxy_mod
from tt_device_mcp.health.recovery.stages import power_cycle as pc

_real_auto_power_cycle_host = srv._auto_power_cycle_host  # before any test stubs it
_real_fire_power_cycle = pc._fire_power_cycle


def _arm_partial_drop(monkeypatch, off_bus):
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=off_bus, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    return counters


def _deny(monkeypatch, *denied):
    asked = []

    def allowed(action, **_):
        asked.append(action)
        return (False, "boot-loop cooldown") if action in denied else (True, "ok")

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    return asked


def test_the_power_cycle_is_the_only_host_rung():
    """Armed, every drop gets the power cycle; not armed, no host rung at all (hold and page)."""
    for off_bus in (0, 8, 16, 32):
        assert galaxy_mod._host_escalation_for_drop(off_bus, 32, auto_power_cycle_enabled=lambda: True) == (
            "power-cycle",
            False,
        )
        assert galaxy_mod._host_escalation_for_drop(off_bus, 32, auto_power_cycle_enabled=lambda: False) == (
            None,
            True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("off_bus", [8, 16])
async def test_partial_drop_past_the_ladder_cycles(monkeypatch, clear_job_state, off_bus):
    """8 off (below the floor) and 16 off (at the floor): the ladder runs first (one mesh reset),
    then the power cycle."""
    counters = _arm_partial_drop(monkeypatch, off_bus)
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert ran is True
    assert counters["resets"] == 1, "the ladder runs before the host rung (only whole-tray skips it, I37)"
    assert counters["power_cycles"] == 1
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("off_bus", [8, 16])
async def test_a_denied_cycle_holds_and_asks_only_for_the_cycle(monkeypatch, clear_job_state, off_bus):
    """A cooldown-denied power cycle holds and is asked for again later; the governor is never asked
    for any other host action, and the fault is never cleared."""
    counters = _arm_partial_drop(monkeypatch, off_bus)
    asked = _deny(monkeypatch, "power-cycle")
    patch_health_event(monkeypatch, lambda *a, **k: None)

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert asked and set(asked) == {"power-cycle"}, "only the power cycle is asked for"
    assert counters["power_cycles"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


def _cycle_never_launches(monkeypatch):
    calls = {"n": 0}

    async def failed(log, reason):
        calls["n"] += 1
        return False

    monkeypatch.setattr(srv, "_auto_power_cycle_host", failed)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("off_bus", [8, 16])
async def test_a_cycle_that_does_not_launch_holds_pages_and_retries(monkeypatch, clear_job_state, off_bus):
    """An unreachable BMC: the hold stays, the operator is paged, and the next pass asks for the
    cycle again. No other host action runs and the fault is never cleared."""
    counters = _arm_partial_drop(monkeypatch, off_bus)
    asked = _deny(monkeypatch)
    cycles = _cycle_never_launches(monkeypatch)
    events, alerts = [], []
    patch_health_event(monkeypatch, lambda kind, **k: events.append(kind))
    monkeypatch.setattr(galaxy_mod.alert, "send_alert", lambda ev, *a, **k: alerts.append(ev["kind"]))

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert cycles["n"] == 1
    assert "power_cycle_launch_failed_holding" in events
    assert "power_cycle_launch_failed_holding" in alerts, "a cycle that cannot launch pages"
    assert srv.recovery_mechanism.host_action_requested is None, "nothing launched, nothing pending"

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert cycles["n"] == 2, "the next pass asks for the cycle again"
    assert set(asked) == {"power-cycle"}
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_a_cycle_that_cannot_launch_os_first_never_reboots_and_holds(monkeypatch, clear_job_state, tmp_path):
    """Spec 04 I88/I71: the real escalation, with no shutdown hook installed, raises
    PowerCycleNotLaunched. No reboot is asked for and no step runs (no warm-reboot fallback), the ledger
    keeps no row, the hold stays (fault and dirty flags untouched), and the next pass asks for the
    power cycle again, never another host action. The episode gets one page (power_cycle_not_launched,
    naming why), not a second launch-failure page; the hourly re-page still follows while it fails."""
    counters = _arm_partial_drop(monkeypatch, 8)
    _ledger_dir(monkeypatch, tmp_path)
    asked = _deny(monkeypatch)
    monkeypatch.delenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", raising=False)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", _real_auto_power_cycle_host)
    monkeypatch.setattr(srv, "_fire_power_cycle", _real_fire_power_cycle)
    steps = []
    monkeypatch.setattr(pc, "_run_step", lambda argv, timeout=None: steps.append(list(argv)) or (1, "stub"))
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    monkeypatch.setattr(srv.recovery_mechanism, "_power_cycle_not_launched_paged", None)
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-01-01T00:00:00")
    monkeypatch.setattr(srv.galaxy_recovery, "_cycle_launch_paged", None)
    events, alerts = [], []
    patch_health_event(monkeypatch, lambda kind, **k: events.append(kind))
    monkeypatch.setattr(recovery_base, "health_event", lambda kind, **k: events.append(kind))
    monkeypatch.setattr(galaxy_mod.alert, "send_alert", lambda ev, *a, **k: alerts.append(ev["kind"]))

    for force in (False, True):
        await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=force)
    assert steps == [], "nothing ran: no reboot, no fallback unit"
    assert srv.recovery_mechanism.read_auto_recovery_ledger() == []
    assert srv.recovery_mechanism.host_action_requested is None
    assert not hasattr(srv.recovery_mechanism, "power_cycle_unlaunched_at"), "no reboot comes back"
    assert set(asked) == {"power-cycle"}, "the next pass asks for the cycle again, nothing else"
    assert events.count("power_cycle_not_launched") == 2 and "auto_reboot" not in " ".join(events)
    assert events.count("power_cycle_launch_failed_holding") == 2, "every failure is journaled"
    assert alerts == ["power_cycle_not_launched"], "one page per hold episode"
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0, "the hold stays"

    episode, at = srv.galaxy_recovery._cycle_launch_paged
    assert episode == "2026-01-01T00:00:00"
    srv.galaxy_recovery._cycle_launch_paged = (episode, at - recovery_base.CYCLE_LAUNCH_REPAGE_SEC)
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert alerts == ["power_cycle_not_launched", "power_cycle_launch_failed_holding"], "the hourly re-page"
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert len(alerts) == 2 and steps == [] and set(asked) == {"power-cycle"}


@pytest.mark.asyncio
async def test_two_failed_launches_in_one_episode_page_once_and_retry_each_pass(monkeypatch, clear_job_state):
    """Spec 04 I71: an unreachable BMC fails every launch. Each pass journals the failure and asks for the
    cycle again, but the operator is paged once per hold episode, then at most once per
    CYCLE_LAUNCH_REPAGE_SEC while it keeps failing; a new episode pages again. The hold stays and the
    fault is never cleared."""
    counters = _arm_partial_drop(monkeypatch, 8)
    _deny(monkeypatch)
    cycles = _cycle_never_launches(monkeypatch)
    events, alerts = [], []
    patch_health_event(monkeypatch, lambda kind, **k: events.append((kind, k)))
    monkeypatch.setattr(galaxy_mod.alert, "send_alert", lambda ev, *a, **k: alerts.append(ev))
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-01-01T00:00:00")

    async def one_pass(force):
        await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=force)

    await one_pass(False)
    await one_pass(True)
    failed = [k for kind, k in events if kind == "power_cycle_launch_failed_holding"]
    assert cycles["n"] == 2, "every pass asks for the cycle again (no pacing)"
    assert [k["paged"] for k in failed] == [True, False], "each failure is journaled; only the first pages"
    assert len(alerts) == 1 and alerts[0]["kind"] == "power_cycle_launch_failed_holding"
    assert alerts[0]["held_since"] == "2026-01-01T00:00:00"
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0, "the hold stays"
    assert srv.recovery_mechanism.host_action_requested is None

    episode, at = srv.galaxy_recovery._cycle_launch_paged
    srv.galaxy_recovery._cycle_launch_paged = (episode, at - recovery_base.CYCLE_LAUNCH_REPAGE_SEC)
    await one_pass(True)
    assert len(alerts) == 2, "still failing an interval later: one more page"
    await one_pass(True)
    assert len(alerts) == 2

    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-01-01T01:00:00")
    await one_pass(True)
    assert len(alerts) == 3 and alerts[2]["held_since"] == "2026-01-01T01:00:00", "a new episode pages again"
    assert cycles["n"] == 5
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


def _gate_partial_drop(monkeypatch, tmp_path):
    _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before=16, off_after=16)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")


@pytest.mark.asyncio
async def test_the_gate_cycles_a_partial_drop_when_allowed(monkeypatch, tmp_path, clear_job_state):
    """Allowed and launched, the gate's climb fires the cycle once."""
    _gate_partial_drop(monkeypatch, tmp_path)
    cycles = {"n": 0}

    async def fake_power_cycle(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_power_cycle)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert cycles["n"] == 1


def test_next_stage_names_the_power_cycle_for_a_failed_reset_partial_drop(monkeypatch, health_deps):
    """The router: a was-failing partial drop the reset did not recover -> STAGE_POWER_CYCLE."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    d = _rung(monkeypatch, health_deps, off_bus=16, was_failing=True, reset_recovered=False, off_bus_before=16)
    assert d == STAGE_POWER_CYCLE


def test_a_live_scope_waits_instead_of_blocking(monkeypatch, health_deps):
    """No power cycle armed: under a live reset scope the router waits for that scope; BLOCKED only
    when nothing is in flight."""
    kw = dict(off_bus=32, was_failing=True, reset_recovered=False, host_escalation="off")
    assert _rung(monkeypatch, health_deps, scope_active=True, **kw) in (WAIT, DEFER)
    assert _rung(monkeypatch, health_deps, **kw) == BLOCKED


@pytest.mark.asyncio
async def test_a_cooldown_held_cycle_fires_at_the_cooldown_end_with_the_real_governor(
    monkeypatch, clear_job_state, tmp_path
):
    """The real governor and ledger: two unhealed cycles double the I65 interval to 1 h. Inside the
    cooldown the drop holds (no other host action); the cycle fires the moment the cooldown ends."""
    counters = _arm_partial_drop(monkeypatch, 16)
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(recovery_base, "POWER_CYCLE_MIN_INTERVAL_SEC", 1800)
    monkeypatch.setattr(recovery_base, "AUTO_RECOVERY_MIN_INTERVAL_SEC", 3600)
    monkeypatch.delenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, raising=False)
    rm = srv.recovery_mechanism
    for name in ("read_auto_recovery_ledger", "auto_recovery_allowed", "last_verified_release"):
        monkeypatch.setattr(rm, name, getattr(type(rm), name).__get__(rm))
    last_cycle = 1_900_000_000.0
    boot_btime = last_cycle + 60  # the last cycle's boot, still wedged
    (tmp_path / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in (
                {"action": "power-cycle", "boot_id": "boot-B", "at_epoch": last_cycle - 3600},
                {"action": "power-cycle", "boot_id": "boot-C", "at_epoch": last_cycle},
            )
        )
    )
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-D")
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(boot_btime)))
    clk = [boot_btime + 1800]  # inside the 1 h cooldown, which ends at boot + 3600
    fake_time = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith("_")})
    fake_time.time = lambda: clk[0]
    monkeypatch.setattr(recovery_base, "time", fake_time)
    assert rm.power_cycle_interval(now_epoch=clk[0]) == 3600
    assert rm.power_cycle_cooldown()["until"] == boot_btime + 3600

    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert counters["power_cycles"] == 0, "held for the cooldown"

    clk[0] = boot_btime + 3600  # the I65 retry: the watchdog's forced pass at the cooldown end
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    assert counters["power_cycles"] == 1, "the cycle fires at the cooldown end"
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, returns, kept, pending",
    [
        ("ledger write fails", False, False, False),  # nothing fired: hold, page, retry
        ("fire raises", False, False, False),  # did not launch: retracted, no pending request
        ("fire times out", True, False, True),  # may still act: no retry, resets stay off
        ("subprocess timeout", True, False, True),
        ("fire returns", True, True, True),  # issued: the record stays as the rate limit
    ],
)
async def test_fire_recovery_escalation_reports_whether_the_action_may_have_launched(
    monkeypatch, tmp_path, case, returns, kept, pending
):
    """``_fire_recovery_escalation`` returns False only when the action certainly did not launch (the
    I71 hold-page-retry path runs only then); a timeout or a normal return reads True."""
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
