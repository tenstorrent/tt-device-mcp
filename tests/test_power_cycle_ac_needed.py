# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A whole tray no in-band power cycle heals ends in the AC-power-needed hold (spec 04 I73).

After POWER_CYCLE_AC_NEEDED_AFTER broker power cycles that took the host down with the same whole
tray still off the bus and no verified release since, the broker stops cycling. It holds the device
dirty, names the state on /health (chips, attempts, last cycle, next presence check), pages once and
then hourly, and re-reads the bus on a fixed interval. A tray that comes back takes the ordinary path
and is released only by a passing full health check, fabric included. External cycles space the
cooldown (I65) but never count as attempts. No test reaches a real reset or power cycle."""

from datetime import datetime

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off, rig  # noqa: F401
from tests.test_power_cycle_cooldown import T0, _cycles, _ledger, _tray_off, _watchdog, cool  # noqa: F401
from tt_device_mcp import server as srv
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.base import (
    AC_POWER_RECHECK_SEC,
    AC_POWER_REPAGE_SEC,
    POWER_CYCLE_AC_NEEDED_AFTER,
    POWER_CYCLE_MIN_INTERVAL_SEC,
)

BOOT = 120  # seconds from a cycle to the boot it brings


@pytest.fixture
def paged(cool, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(recovery_base.alert, "send_alert", lambda event, log=None: sent.append(event) or object())
    return sent


def _boot(cool, at, name):  # noqa: F811
    cool["boot"].update(id=name, btime=at)
    cool["clk"][0] = at + 60  # the startup gate runs once the hugepage pool is ready


def _health(monkeypatch, at):
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    return srv._health_payload(now=datetime.fromtimestamp(at)).get("power_cycle_cooldown")


async def _two_cycles_that_do_not_heal(cool):  # noqa: F811
    """The tray drops; cycle 1 fires at once; it boots with the tray off; cycle 2 at the cooldown's
    end; it boots with the tray off again. Returns the second boot's startup verdict."""
    _tray_off(cool)
    assert await _gate(cool, galaxy.STAGE_SMI_RESET, phase="run") == galaxy.OUTCOME_WAITING
    assert _cycles(cool) == 1, "a whole tray goes to the power cycle at once (I37)"
    _boot(cool, T0 + BOOT, "boot-B")
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 1 and cool["g"].mechanism.power_cycle_cooldown_state["cause"].startswith("host boot")
    cool["clk"][0] = T0 + BOOT + POWER_CYCLE_MIN_INTERVAL_SEC
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 2, "the second cycle fires the moment the cooldown ends"
    second = cool["clk"][0]
    _boot(cool, second + BOOT, "boot-C")
    return await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")


# ---- (1) two cycles that do not heal: the held state, and no third cycle --------------------------


@pytest.mark.asyncio
async def test_two_cycles_that_do_not_heal_end_in_the_ac_hold_and_no_third_cycle_fires(
    cool, monkeypatch, paged  # noqa: F811
):
    assert POWER_CYCLE_AC_NEEDED_AFTER == 2
    assert await _two_cycles_that_do_not_heal(cool) == galaxy.OUTCOME_WAITING
    m = cool["g"].mechanism
    state = m.power_cycle_cooldown_state
    assert state["cause"] == "AC power needed" and "AC power removal needed" in state["why"]
    assert state["ac_power_needed"]["attempts"] == 2
    assert state["ac_power_needed"]["chips"] == [str(c) for c in sorted(_off(cool["map"], {2: 8}), key=int)]
    # Every later pass, the watchdog's forced climb included, holds with no cycle: hours later too.
    for later in (60, 3600, 4 * 3600, 24 * 3600):
        cool["clk"][0] = T0 + later + 2 * POWER_CYCLE_MIN_INTERVAL_SEC
        await _gate(cool, galaxy.STAGE_SMI_RESET, phase="run")
        await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda msg: None)
    assert _cycles(cool) == 2 and m.power_cycle_cooldown_state["cause"] == "AC power needed"
    assert not {"glx_reset", "ubb_tray", "sweep"} & set(cool["order"]), "no reset or tray re-power in between"


@pytest.mark.asyncio
async def test_the_ac_hold_pages_once_then_hourly(cool, monkeypatch, paged):  # noqa: F811
    mono = [1000.0]
    monkeypatch.setattr(recovery_base.time, "monotonic", lambda: mono[0])
    await _two_cycles_that_do_not_heal(cool)
    pages = lambda: [e for e in paged if e["kind"] == "power_cycle_ac_power_needed"]  # noqa: E731
    assert len(pages()) == 1
    for step in (60, AC_POWER_REPAGE_SEC - 120):
        mono[0] += step
        await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda msg: None)
    assert len(pages()) == 1, "no page inside the hour"
    mono[0] += 120
    await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda msg: None)
    assert len(pages()) == 2, "paged again after an hour"


# ---- (2) the cooldown is honoured between the cycles ----------------------------------------------


@pytest.mark.asyncio
async def test_the_cooldown_is_honoured_between_the_two_cycles(cool):  # noqa: F811
    _tray_off(cool)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="run")
    _boot(cool, T0 + BOOT, "boot-B")
    until = T0 + BOOT + POWER_CYCLE_MIN_INTERVAL_SEC
    for at in (T0 + BOOT + 60, until - 600, until - 1):
        cool["clk"][0] = at
        await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
        assert _cycles(cool) == 1, f"no second cycle {until - at:.0f}s before the cooldown ends"
    assert cool["g"].mechanism.power_cycle_retry[0] == until
    cool["clk"][0] = until
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 2


# ---- (3)/(4) a returning tray: released only on a passing full check, held on a failed one --------


@pytest.mark.asyncio
async def test_the_bus_is_rechecked_on_the_interval_and_a_returning_tray_leaves_the_ac_hold(
    cool, monkeypatch  # noqa: F811
):
    await _two_cycles_that_do_not_heal(cool)
    m = cool["g"].mechanism
    until = m.power_cycle_retry[0]
    assert until == cool["clk"][0] + AC_POWER_RECHECK_SEC
    assert _watchdog(cool, monkeypatch, until - 1) == []
    assert _watchdog(cool, monkeypatch, until) == [until], "the presence re-check is forced at its deadline"
    # An operator removed AC power; the same broker (no restart) reads the whole mesh on the bus again.
    cool["beats"] = _beats_without(set())
    cool["clk"][0] = until
    await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda msg: None)
    assert m.whole_tray_off is None, "no whole tray off: the AC count no longer applies"
    assert _cycles(cool) == 2, "a returning tray is never cycled"


def test_a_returning_tray_is_released_only_after_the_full_fabric_check_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    m = srv.recovery_mechanism
    for name, value in (("power_cycle_retry", None), ("power_cycle_cooldown_state", None), ("whole_tray_off", None)):
        monkeypatch.setattr(m, name, value)
    monkeypatch.setattr(m, "_current_boot_id", lambda: "boot-C")
    monkeypatch.setattr(m, "_boot_btime_id", lambda: str(int(T0 - 600)))
    monkeypatch.setattr(m, "_ac_power_paged", 1.0)  # no page from this test
    _ledger(
        {"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0 - 7200, "at": "x"},
        {"action": "power-cycle", "boot_id": "boot-B", "at_epoch": T0 - 1200, "at": "x"},
    )
    m.whole_tray_off = [str(c) for c in range(16, 24)]
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)[0] is False
    assert m.power_cycle_cooldown_state["ac_power_needed"]["attempts"] == 2
    held = []
    monkeypatch.setattr(srv, "_hold_device_fabric_unverified", lambda why: held.append(why))
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: FABRIC_DEBT.owed)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "glx_reset")
    # (4) the check after the tray came back has not passed its fabric pass: still held, still dirty.
    assert srv._clear_device_dirty(verified=True, why="tray back") is False
    assert held and m.power_cycle_cooldown_state["cause"] == "AC power needed"
    # (3) the full check passes: the release clears the AC hold and ends the streak.
    FABRIC_DEBT.owed = ""
    assert srv._clear_device_dirty(verified=True, why="tray back") is True
    assert m.power_cycle_cooldown_state is None and m.whole_tray_off is None and m.power_cycle_retry is None


@pytest.mark.asyncio
async def test_a_tray_that_drops_again_before_any_verified_release_holds_with_no_third_cycle(cool):  # noqa: F811
    await _two_cycles_that_do_not_heal(cool)
    cool["beats"] = _beats_without(set())
    await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda msg: None)  # the tray read back once
    _tray_off(cool)  # its check failed: the same tray is off again
    cool["clk"][0] += AC_POWER_RECHECK_SEC
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="run")
    assert _cycles(cool) == 2 and cool["g"].mechanism.power_cycle_cooldown_state["cause"] == "AC power needed"


# ---- (5) /health shows the state and its next check -----------------------------------------------


@pytest.mark.asyncio
async def test_health_shows_the_ac_needed_state_and_its_next_presence_check(cool, monkeypatch):  # noqa: F811
    await _two_cycles_that_do_not_heal(cool)
    now = cool["clk"][0]
    cd = _health(monkeypatch, now)
    ac = cd["ac_power_needed"]
    assert cd["escalation_stage"] == "ac-power-removal" and cd["cause"] == "AC power needed"
    assert ac["attempts"] == 2 and len(ac["chips"]) == 8
    assert ac["next_presence_check_epoch"] == now + AC_POWER_RECHECK_SEC
    assert cd["next_deadline"] == datetime.fromtimestamp(now + AC_POWER_RECHECK_SEC).isoformat(timespec="seconds")
    assert ac["last_cycle_epoch"] == T0 + BOOT + POWER_CYCLE_MIN_INTERVAL_SEC
    monkeypatch.setattr(srv, "_force_escalate_enabled", lambda: True)
    monkeypatch.setattr(srv.time, "time", lambda: now)
    assert "needs AC power removed" in srv._hold_escalation_note(off_bus=8)


@pytest.mark.asyncio
async def test_health_shows_no_ac_state_on_an_ordinary_cooldown(cool, monkeypatch):  # noqa: F811
    _tray_off(cool)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="run")
    _boot(cool, T0 + BOOT, "boot-B")
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    cd = _health(monkeypatch, cool["clk"][0])
    assert cd["escalation_stage"] == "power-cycle" and cd["ac_power_needed"] is None


# ---- what counts as an attempt ---------------------------------------------------------------------


def test_external_cycles_space_the_cooldown_but_are_never_attempts(cool, monkeypatch, tmp_path):  # noqa: F811
    """One broker cycle, then an external manual cycle (history file and a new boot): the external
    one anchors the cooldown (I65) but is not a second attempt, so no AC hold yet."""
    m = cool["g"].mechanism
    m.whole_tray_off = [str(c) for c in range(16, 24)]
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0 - 7200, "at": "x"})
    hist = tmp_path / "cycles.jsonl"
    hist.write_text('{"at_epoch": %d, "cause": "external manual cycle"}\n' % (T0 - 600))
    monkeypatch.setenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, str(hist))
    cool["boot"].update(id="boot-X", btime=T0 - 480)
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)
    assert not allowed and m.power_cycle_cooldown_state["cause"] != "AC power needed"
    assert m.power_cycle_cooldown_state["until"] == T0 - 480 + POWER_CYCLE_MIN_INTERVAL_SEC


@pytest.mark.parametrize(
    "rows,counted",
    [
        # a request this boot has not taken the host down yet
        ([("boot-A", -7200), ("boot-B", -1200), ("boot-C", -60)], 2),
        # a request the host never acted on (spec 04 I72)
        ([("boot-A", -7200), ("boot-B", -3600, False)], 1),
        # a cycle for another tray
        ([("boot-A", -7200), ("boot-B", -1200, None, ["0", "1"])], 1),
        # cycles before the last verified release
        ([("boot-A", -90000), ("boot-B", -86000)], 0),
        ([("boot-A", -7200), ("boot-B", -1200)], 2),
        # this boot began hours after the last request: an external cycle, not that attempt
        ([("boot-A", -9000), ("boot-B", -7200)], 1),
    ],
)
def test_only_broker_cycles_that_took_the_host_down_for_this_tray_count(cool, tmp_path, rows, counted):  # noqa: F811
    m = cool["g"].mechanism
    cool["boot"].update(id="boot-C", btime=T0 - 600)
    (recovery_base.health_dir() / recovery_base.AUTO_RECOVERY_VERIFIED).write_text(
        '{"boot_id": "boot-0", "at_epoch": %d}' % (T0 - 80000)
    )
    recs = []
    for boot_id, at, *rest in rows:
        rec = {"action": "power-cycle", "boot_id": boot_id, "at_epoch": T0 + at, "at": "x"}
        if rest and rest[0] is False:
            rec["took_host_down"] = False
        if len(rest) > 1:
            rec["off_bus"] = rest[1]
        recs.append(rec)
    chips = [str(c) for c in range(16, 24)]
    ledger = m._cycles_that_ran(recs, T0)
    assert len(m._unhealed_tray_cycles(ledger, chips, T0, recs)) == counted


def test_a_partial_off_bus_set_never_enters_the_ac_hold(cool):  # noqa: F811
    """Class B1 and C keep their own ladders: the count applies only while a whole tray is off."""
    m = cool["g"].mechanism
    _ledger(
        {"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0 - 7200, "at": "x"},
        {"action": "power-cycle", "boot_id": "boot-B", "at_epoch": T0 - 5400, "at": "x"},
    )
    m.whole_tray_off = None
    m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)
    assert (m.power_cycle_cooldown_state or {}).get("cause") != "AC power needed"


def test_a_power_cycle_row_names_the_tray_it_was_for(cool):  # noqa: F811
    m = cool["g"].mechanism
    m.whole_tray_off = ["16", "17"]
    m.record_auto_recovery("power-cycle", "tray off", now_epoch=T0)
    m.whole_tray_off = None
    m.record_auto_recovery("power-cycle", "all off", now_epoch=T0 + 1)
    rows = m.read_auto_recovery_ledger()
    assert rows[-2]["off_bus"] == ["16", "17"] and "off_bus" not in rows[-1]
