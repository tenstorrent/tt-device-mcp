# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The power cycle's cooldown (spec 04 I65).

A power cycle is spaced from the latest power cycle or boot of any cause: the broker's own (its
ledger), the host's current boot (a manual, datacenter or external cycle included) and an external
cycler's history file. While the cooldown runs the hold stays and says until when; at its end the
cycle fires at once if the fault persists. Repeated cycles that heal nothing back off (30 min,
30 min, 1 h, 2 h, then 4 h at most); a verified release ends the streak. No test reaches a real
reset or power cycle: the ``rig`` records them."""

import json
import time
import types
from datetime import datetime
from functools import partial

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off, rig  # noqa: F401
from tt_device_mcp import server as srv
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.base import AUTO_RECOVERY_LEDGER, POWER_CYCLE_MIN_INTERVAL_SEC

T0 = 1_800_000_000.0
LADDER = {"glx_reset", "ubb_tray", "sweep"}


@pytest.fixture
def cool(rig, monkeypatch, tmp_path):  # noqa: F811
    """The ``rig`` with the REAL governor on a settable epoch clock: a power cycle writes its ledger
    record as the real one does, and the host's boot time and boot id are settable."""
    g = rig["g"]
    m = g.mechanism
    clk = [T0]
    boot = {"id": "boot-A", "btime": T0 - 86400}
    fake_time = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith("_")})
    fake_time.time = lambda: clk[0]
    monkeypatch.setattr(recovery_base, "time", fake_time)
    monkeypatch.setattr(m, "auto_recovery_allowed", partial(type(m).auto_recovery_allowed, m))
    monkeypatch.setattr(srv, "_current_boot_id", lambda: boot["id"])
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(boot["btime"])))
    monkeypatch.setattr(m, "power_cycle_retry", None)
    monkeypatch.setattr(m, "power_cycle_cooldown_state", None)
    monkeypatch.delenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, raising=False)
    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.fromtimestamp(T0 - 60).isoformat())

    async def power_cycle(log, reason):
        rig["order"].append("power_cycle")
        m.record_auto_recovery("power-cycle", reason, now_epoch=clk[0])

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    rig["clk"], rig["boot"] = clk, boot
    rig["g"]._td_reset_seen = True  # the rescan prelude is I19's; only the cooldown is under test
    return rig


def _tray_off(r):
    r["beats"] = _beats_without(_off(r["map"], {2: 8}))


def _ledger(*recs):
    (recovery_base.health_dir() / AUTO_RECOVERY_LEDGER).write_text("".join(json.dumps(r) + "\n" for r in recs))


def _cycles(r):
    return r["order"].count("power_cycle")


def _watchdog(r, monkeypatch, at):
    """One watchdog tick at ``at`` with every other forced-escalation window out of reach."""
    spawned = []
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 10**9)
    monkeypatch.setattr(srv, "_offbus_hold_ceiling_sec", lambda: 10**9)
    monkeypatch.setattr(srv, "_hold_deadline_sec", lambda: 10**9)
    monkeypatch.setattr(srv.offbus_power_cycle, "due_for_watchdog", lambda *a, **k: False)
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: spawned.append(at) or True)
    srv._check_hold_deadline(now=datetime.fromtimestamp(at))
    return spawned


# ---- (a) a manual cycle or boot just before startup ----------------------------------------------


@pytest.mark.asyncio
async def test_a_boot_3_min_before_startup_holds_to_a_deadline_then_cycles_once(cool, monkeypatch):
    """The box came up 3 min ago from a cycle the broker did not fire (manual, BMC, datacenter): a
    whole tray is off at startup. The hold stays with its deadline on /health; no ladder rung runs
    meanwhile; at the deadline the watchdog asks once and exactly one cycle fires."""
    cool["boot"]["btime"] = T0 - 180
    _tray_off(cool)
    assert await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup") == galaxy.OUTCOME_WAITING
    until = T0 - 180 + POWER_CYCLE_MIN_INTERVAL_SEC
    assert _cycles(cool) == 0 and not LADDER & set(cool["order"])
    assert cool["g"].mechanism.power_cycle_retry[0] == until
    state = cool["g"].mechanism.power_cycle_cooldown_state
    assert state["cause"] == "host boot" and "power cycle in cooldown until" in state["why"]
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    cd = srv._health_payload(now=datetime.fromtimestamp(T0))["power_cycle_cooldown"]
    assert cd["escalation_stage"] == "power-cycle" and cd["cause"] == "host boot"
    assert cd["next_deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")
    # Later passes inside the cooldown hold the same way: no reset, tray re-power or sweep.
    cool["clk"][0] = T0 + 600
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 0 and not LADDER & set(cool["order"])
    assert _watchdog(cool, monkeypatch, until - 1) == []
    assert _watchdog(cool, monkeypatch, until) == [until], "asked the moment the cooldown ends"
    assert _watchdog(cool, monkeypatch, until + 30) == [], "once per denial"
    cool["clk"][0] = until
    assert await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert _cycles(cool) == 1 and not LADDER & set(cool["order"])


def test_health_carries_the_cooldown_deadline_while_held(cool, monkeypatch):
    m = cool["g"].mechanism
    until = T0 + 900
    m.power_cycle_cooldown_state = {
        "until": until,
        "last": T0 - 900,
        "cause": "host boot",
        "interval_sec": 1800,
        "why": "power cycle in cooldown until ...",
    }
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    payload = srv._health_payload(now=datetime.fromtimestamp(T0))
    assert payload["power_cycle_cooldown"]["next_deadline"] == datetime.fromtimestamp(until).isoformat(
        timespec="seconds"
    )
    assert "power_cycle_cooldown" not in srv._health_payload(now=datetime.fromtimestamp(until))


# ---- (b) the broker's own cycle, then a boot with the tray still off -----------------------------


@pytest.mark.asyncio
async def test_after_the_broker_cycle_and_boot_no_second_cycle_before_the_cooldown_ends(cool):
    cool["boot"].update(id="boot-B", btime=T0 + 120)
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0, "at": "x"})
    _tray_off(cool)
    cool["clk"][0] = T0 + 300
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    state = cool["g"].mechanism.power_cycle_cooldown_state
    assert state["cause"] == "host boot after the broker power-cycle"
    until = T0 + 120 + POWER_CYCLE_MIN_INTERVAL_SEC  # the boot is the latest anchor
    assert state["until"] == until and _cycles(cool) == 0
    cool["clk"][0] = until - 1
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 0
    cool["clk"][0] = until
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 1 and not LADDER & set(cool["order"])


def test_an_external_cycler_history_is_an_anchor(cool, monkeypatch, tmp_path):
    hist = tmp_path / "cycles.jsonl"
    hist.write_text(json.dumps({"at_epoch": T0 - 100, "cause": "BMC chassis power cycle"}) + "\nnot json\n")
    monkeypatch.setenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, str(hist))
    m = cool["g"].mechanism
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)
    assert not allowed and "BMC chassis power cycle" in why
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + 1700)[0] is True
    monkeypatch.setenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, str(tmp_path / "missing"))
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)[0] is True


def test_a_boot_stamp_in_the_future_anchors_nothing(cool):
    cool["boot"]["btime"] = T0 + 3600
    assert cool["g"].mechanism.power_cycle_cooldown(now_epoch=T0) is None


def _hist(monkeypatch, tmp_path, *lines):
    hist = tmp_path / "cycles.jsonl"
    hist.write_text("".join(ln + "\n" for ln in lines))
    monkeypatch.setenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, str(hist))


@pytest.mark.parametrize("bad", ['{"at_epoch": NaN}', '{"at_epoch": Infinity}', "FUTURE"])
@pytest.mark.parametrize("bad_first", [True, False])
def test_a_nan_or_future_history_line_does_not_hide_the_valid_past_one(cool, monkeypatch, tmp_path, bad, bad_first):
    """one non-finite or future line (a clock that disagrees) took the max and was then
    dropped as future, so the real cycle 100 s ago anchored nothing and the cycle fired at once."""
    bad = bad.replace("FUTURE", json.dumps({"at_epoch": T0 + 6 * 3600, "cause": "clock ahead"}))
    good = json.dumps({"at_epoch": T0 - 100, "cause": "BMC chassis power cycle"})
    _hist(monkeypatch, tmp_path, *((bad, good) if bad_first else (good, bad)))
    cd = cool["g"].mechanism.power_cycle_cooldown(now_epoch=T0)
    assert cd["last"] == T0 - 100 and cd["cause"] == "BMC chassis power cycle"
    assert cd["until"] == T0 - 100 + POWER_CYCLE_MIN_INTERVAL_SEC


@pytest.mark.parametrize("bad", [T0 + 6 * 3600, "NaN", "garbage"])
def test_a_future_or_unreadable_ledger_record_does_not_hide_a_past_broker_cycle(cool, bad):
    _ledger(
        {"action": "power-cycle", "boot_id": "boot-old", "at_epoch": T0 - 100, "at": "x"},
        {"action": "reboot", "boot_id": "boot-old", "at_epoch": bad, "at": "x"},
    )
    cd = cool["g"].mechanism.power_cycle_cooldown(now_epoch=T0)
    assert cd["last"] == T0 - 100 and cd["cause"] == "broker power-cycle"


def test_a_future_this_boot_stamp_holds_only_to_the_stated_gap_and_shows_on_health(cool, monkeypatch):
    """a this-boot ledger stamp 6 h ahead (the clock stepped back) held the cycle until
    stamp + interval, 6.5 h, with nothing on /health. It now anchors nothing: the cooldown runs from
    the host boot, the denial is on /health, and the cycle is allowed at the boot's cooldown end."""
    m = cool["g"].mechanism
    cool["boot"]["btime"] = T0 - 600
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0 + 6 * 3600, "at": "x"})
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)
    until = T0 - 600 + POWER_CYCLE_MIN_INTERVAL_SEC
    assert not allowed and m.power_cycle_retry[0] == until
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    payload = srv._health_payload(now=datetime.fromtimestamp(T0))
    assert payload["power_cycle_cooldown"]["next_deadline"] == datetime.fromtimestamp(until).isoformat(
        timespec="seconds"
    )
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=until)
    assert allowed, why


@pytest.mark.parametrize("action", ["reboot", "power-cycle"])
@pytest.mark.parametrize("stamp", [None, "garbage", "NaN"])
def test_an_unreadable_this_boot_stamp_holds_the_cycle_to_a_fixed_end(cool, action, stamp):
    """an unreadable stamp counted as now, so its end moved forward on every ask and the
    cycle was held for the whole boot. It now ends at the boot's start plus the interval, a fixed
    time; a power-cycle record no longer refuses the rung for good either."""
    m = cool["g"].mechanism
    cool["boot"]["btime"] = T0 - 600
    _ledger({"action": action, "boot_id": "boot-A", "at_epoch": stamp, "at": "x"})
    until = T0 - 600 + POWER_CYCLE_MIN_INTERVAL_SEC
    for now in (T0, until - 1):
        allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=now)
        assert not allowed and m.power_cycle_retry[0] == until, why
        assert m.power_cycle_cooldown_state["until"] == until
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=until)
    assert allowed, why


def test_the_per_boot_cap_denial_shows_on_health(cool, monkeypatch):
    """Any cap denial of the power cycle fills the /health deadline, not only the cooldown's."""
    m = cool["g"].mechanism
    monkeypatch.setattr(m, "power_cycle_cooldown", lambda **k: None)  # isolate the per-boot cap
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0 - 60, "at": "x"})
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)
    until = T0 - 60 + POWER_CYCLE_MIN_INTERVAL_SEC
    assert not allowed and m.power_cycle_cooldown_state["until"] == until == m.power_cycle_retry[0]
    assert m.power_cycle_cooldown_state["last"] == T0 - 60


# ---- (c) backoff ---------------------------------------------------------------------------------


def test_the_backoff_doubles_after_two_failed_cycles_and_caps_at_4h():
    seq = [recovery_base.power_cycle_interval_sec(n) for n in range(8)]
    assert seq == [1800, 1800, 3600, 7200, 14400, 14400, 14400, 14400]
    assert recovery_base.power_cycle_interval_sec(60) == recovery_base.POWER_CYCLE_BACKOFF_CAP_SEC == 4 * 3600


def test_cycles_that_heal_nothing_back_off_and_a_verified_release_resets_it(cool):
    """A fault no cycle heals (a tray with a dead power feed): each cycle fires at its cooldown's
    end, each boot 2 min after it. The gaps grow 30 min, 1 h, 2 h, then stay at 4 h: about six
    cycles a day at most, never a 30-min boot loop. A verified release ends the streak."""
    m = cool["g"].mechanism
    at, fired = T0, []
    for n in range(6):
        boot_at = at + 120
        allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=at)
        assert allowed, why
        m.record_auto_recovery("power-cycle", "tray off", now_epoch=at)
        fired.append(at)
        cool["boot"].update(id=f"boot-{n}", btime=boot_at)
        cd = m.power_cycle_cooldown(now_epoch=boot_at + 1)
        allowed, _ = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=cd["until"] - 1)
        assert not allowed
        at = cd["until"]
    gaps = [b - a - 120 for a, b in zip(fired, fired[1:])]
    assert gaps == [1800, 3600, 7200, 14400, 14400]
    m.note_mesh_verified(now_epoch=at)
    assert m.power_cycle_interval() == POWER_CYCLE_MIN_INTERVAL_SEC
    assert m.power_cycle_retry is None


def test_a_future_record_does_not_keep_the_streak_past_a_verified_release(cool):
    """a power-cycle record stamped later than now stayed 'after the heal', so a verified
    release did not end the backoff streak. A heal file stamped later than now does not hide the
    cycles after it either (it reads as no heal: the longer, capped cooldown)."""
    m = cool["g"].mechanism
    _ledger(
        {"action": "power-cycle", "boot_id": "b1", "at_epoch": T0 - 7200, "at": "x"},
        {"action": "power-cycle", "boot_id": "b2", "at_epoch": T0 - 3600, "at": "x"},
        {"action": "power-cycle", "boot_id": "b3", "at_epoch": T0 + 6 * 3600, "at": "x"},
    )
    assert m.power_cycle_interval(now_epoch=T0) == 3600  # two past cycles; the future one is not counted
    m.note_mesh_verified(now_epoch=T0)
    assert m.power_cycle_interval(now_epoch=T0 + 1) == POWER_CYCLE_MIN_INTERVAL_SEC
    (recovery_base.health_dir() / recovery_base.POWER_CYCLE_HEALED_FILE).write_text(
        json.dumps({"at_epoch": T0 + 9 * 3600})
    )
    assert m.power_cycle_interval(now_epoch=T0) == 3600


# ---- (d) a denial retries at its stated time -----------------------------------------------------


def test_a_cycle_requested_this_boot_that_never_went_down_is_asked_again(cool):
    """Spec 04 I65 step 2: a power cycle recorded this boot (the box never went down) once held the
    rung for the rest of the boot. Now the denial names its end, and the rung is asked again then."""
    m = cool["g"].mechanism
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0, "at": "x"})
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + 60)
    assert not allowed and "until" in why
    assert m.power_cycle_retry[0] == T0 + POWER_CYCLE_MIN_INTERVAL_SEC
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + 1800)
    assert allowed, why
    assert m.power_cycle_retry is None


def test_the_watchdog_retry_is_kept_to_its_own_hold_episode(cool, monkeypatch):
    m = cool["g"].mechanism
    m.power_cycle_retry = (T0, "2020-01-01T00:00:00")
    assert _watchdog(cool, monkeypatch, T0 + 10) == []


# ---- (e) nothing recent: at once -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_recent_cycle_or_boot_the_cycle_fires_at_once(cool):
    _tray_off(cool)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert cool["order"] == ["power_cycle"]
    assert cool["g"].mechanism.power_cycle_cooldown_state is None


# ---- (f) goal 8: only a passing full check ends the streak ---------------------------------------


def test_only_a_verified_release_with_no_fabric_debt_ends_the_streak(monkeypatch, tmp_path):
    """The heal marker is written in ``_clear_device_dirty(verified=True)`` after its fabric-debt
    check (spec 04 I32): a release refused for a missing fabric pass keeps the hold and the streak.
    The startup pass that precedes it waits for hugepages (test_classac_power_cycle_direct)."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    m = srv.recovery_mechanism
    for i in range(3):
        m.record_auto_recovery("power-cycle", "tray off", now_epoch=time.time() - 3000 + i)
    assert m.power_cycle_interval() == 7200
    held = []
    monkeypatch.setattr(srv, "_hold_device_fabric_unverified", lambda why: held.append(why))
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: FABRIC_DEBT.owed)  # a multi-chip mesh with a fabric check
    FABRIC_DEBT.owed = "power-cycle"
    assert srv._clear_device_dirty(verified=True, why="post-cycle check") is False
    assert held and m.power_cycle_interval() == 7200, "no release, the streak stands"
    FABRIC_DEBT.owed = ""
    assert srv._clear_device_dirty(verified=True, why="post-cycle check") is True
    assert m.power_cycle_interval() == POWER_CYCLE_MIN_INTERVAL_SEC


# ---- (g) class C at startup ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_off_at_startup_with_no_failed_reset_or_broker_cycle_cycles_with_no_reset(cool):
    """Class C needs no failed mesh reset first: with no recent cycle or boot it cycles at once."""
    cool["beats"] = {}
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert cool["order"] == ["power_cycle"], "no mesh reset, sweep or re-power first"


@pytest.mark.asyncio
async def test_all_off_held_by_the_cooldown_shows_its_deadline_and_cycles_at_it(cool, monkeypatch):
    """Class C, the real I65 gate: a boot 3 min ago holds the cycle. /health names the deadline, the
    hold_cannot_escalate event carries the same retry time, no reset runs meanwhile, and the hold
    watchdog asks once at the deadline, where exactly one cycle fires."""
    cool["boot"]["btime"] = T0 - 180
    cool["beats"] = {}
    until = T0 - 180 + POWER_CYCLE_MIN_INTERVAL_SEC
    assert await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup") == galaxy.OUTCOME_WAITING
    assert _cycles(cool) == 0 and not LADDER & set(cool["order"])
    ((_, held),) = [(k, e) for k, e in cool["events"] if k == "hold_cannot_escalate"]
    assert held["fault_class"] == galaxy.FAULT_CLASS_ALL_OFF and held["retry_at_epoch"] == until
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("all off", srv.device_hold_episode_since))
    cd = srv._health_payload(now=datetime.fromtimestamp(T0))["power_cycle_cooldown"]
    assert cd["next_deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds") == held["retry_at"]
    assert _watchdog(cool, monkeypatch, until - 1) == []
    assert _watchdog(cool, monkeypatch, until) == [until], "asked the moment the cooldown ends"
    cool["clk"][0] = until
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 1 and not LADDER & set(cool["order"])


@pytest.mark.asyncio
async def test_all_off_after_the_broker_cycle_brought_this_boot_up_skips_the_reset(cool):
    """The broker's own power cycle did not bring the bus back: no mesh reset first, the next cycle
    waits only for the cooldown and fires at its end."""
    cool["boot"].update(id="boot-B", btime=T0 + 60)
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0, "at": "x"})
    cool["beats"] = {}
    cool["clk"][0] = T0 + 300
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert cool["order"] == ["denied"], "class C, held by the cooldown, no reset"
    cool["clk"][0] = T0 + 60 + POWER_CYCLE_MIN_INTERVAL_SEC
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert cool["order"] == ["denied", "power_cycle"]
