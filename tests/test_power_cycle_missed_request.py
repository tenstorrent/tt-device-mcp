# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A power-cycle request that never took the host down (spec 04 I72), and the backoff streak's end
(spec 04 I65).

The power-cycle command can return 0 while the cycle never happens (its BMC call failed). The host
stays up on the same boot past the pending window. That request is a missed attempt, not a cycle: it
neither anchors the cooldown nor counts toward its doubling, and it is asked again as its window ends,
then 5 min after each later window, with an operator paged from the second miss on. A cycle that lands
after its window (the next boot starts within the window plus the attribution window) still ran: it
counts for the doubling, the backstop and the boot's attribution. A cycle a verified release followed
on a later boot healed the box, whether or not a heal file was written. No test reaches a real reset
or power cycle: the ``rig`` records them."""

import json
import time
from datetime import datetime

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off, rig  # noqa: F401
from tests.test_power_cycle_cooldown import T0, _cycles, _ledger, _tray_off, _watchdog, cool  # noqa: F401
from tt_device_mcp import server as srv
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.base import (
    AUTO_RECOVERY_LEDGER,
    BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC,
    POWER_CYCLE_HEALED_FILE,
    POWER_CYCLE_MIN_INTERVAL_SEC,
    POWER_CYCLE_MISS_RETRY_SEC,
    _power_cycle_backstop_stamps,
    host_action_pending_sec,
)

WINDOW = host_action_pending_sec()


def _request(cool, at):  # noqa: F811
    """The broker requests a power cycle at ``at`` and the command returns: the ledger row is written
    on this boot and the pending window opens, as the governor does it."""
    m = cool["g"].mechanism
    cool["clk"][0] = at
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=at)
    assert allowed, why
    m.record_auto_recovery("power-cycle", "tray off", now_epoch=at)
    m.host_action_requested = ("power-cycle", 0.0)


def _window_ends(cool, at):  # noqa: F811
    """The pending window runs out at ``at`` with the host still up on the same boot."""
    m = cool["g"].mechanism
    cool["clk"][0] = at
    assert m.host_action_pending(now=WINDOW) is None


def _rows():
    path = recovery_base.health_dir() / AUTO_RECOVERY_LEDGER
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _health(monkeypatch, at):
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    return srv._health_payload(now=datetime.fromtimestamp(at)).get("power_cycle_cooldown")


@pytest.fixture
def paged(cool, monkeypatch):  # noqa: F811
    """Pages sent. The mechanism is shared across tests: start with no page sent yet."""
    sent = []
    monkeypatch.setattr(cool["g"].mechanism, "_power_cycle_miss_paged", 0.0)
    monkeypatch.setattr(recovery_base.alert, "send_alert", lambda event, log=None: sent.append(event) or object())
    return sent


# ---- (1) a missed request is asked again as its window ends -----------------------------------------


def test_a_request_the_host_never_acted_on_is_asked_again_as_its_window_ends(cool, monkeypatch, paged):  # noqa: F811
    """The request returned, the host never went down: at the end of its window it is marked missed
    on disk and asked again at once, with no backoff on top of the window that already gave the
    cycler its time; not 30 min or 4 h after the request."""
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    assert _rows()[-1]["took_host_down"] is False
    assert m.power_cycle_retry[0] == T0 + WINDOW
    assert _watchdog(cool, monkeypatch, T0 + WINDOW) == [T0 + WINDOW]
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + WINDOW)
    assert allowed, why
    assert paged == [], "one miss pages no one"


def test_later_retries_wait_5_min_after_their_window_and_never_stop(cool, paged):  # noqa: F811
    m = cool["g"].mechanism
    at, waits = T0, []
    for _ in range(5):
        _request(cool, at)
        _window_ends(cool, at + WINDOW)
        nxt = m.power_cycle_retry[0]
        waits.append(nxt - at - WINDOW)
        assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=nxt - 1)[0] is False
        at = nxt
    assert waits == [0, 300, 300, 300, 300]
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=at)[0] is True


@pytest.mark.parametrize("misses", [1, 2, 6])
def test_the_retry_comes_at_most_the_window_plus_5_min_after_a_request_the_bmc_missed(cool, misses):  # noqa: F811
    """The stated bound (spec 04 I72): the BMC answers again just after a request it missed. The
    next request goes out at most ``host_action_pending_sec()`` + 5 min after that one (the window
    alone for the first miss), whatever the miss count: 15 min, then 20 min, by default."""
    m = cool["g"].mechanism
    at = T0
    for _ in range(misses - 1):
        _request(cool, at)
        _window_ends(cool, at + WINDOW)
        at = m.power_cycle_retry[0]
    _request(cool, at)  # the BMC answers again right after this one went out
    _window_ends(cool, at + WINDOW)
    worst = WINDOW + (0 if misses == 1 else 300)
    assert m.power_cycle_retry[0] - at == worst <= WINDOW + max(POWER_CYCLE_MISS_RETRY_SEC)
    assert max(POWER_CYCLE_MISS_RETRY_SEC) <= 300
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=at + worst)[0] is True


def test_no_retry_while_the_earlier_request_is_still_pending(cool):  # noqa: F811
    """The pending window counts from the hand-off, after the pre-fire hook, so it can end a little
    after the ledger row's window. Until it does the retry is refused with a stated deadline: never
    a second cycle on top of one that may still come."""
    m = cool["g"].mechanism
    _request(cool, T0)
    m.host_action_requested = ("power-cycle", time.monotonic() - WINDOW + 20)  # handed off 20 s after the row
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + WINDOW)
    assert not allowed and "still pending" in why
    assert abs(m.power_cycle_retry[0] - (T0 + WINDOW + 20)) < 5
    m.host_action_requested = None
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + WINDOW + 20)[0] is True


def test_a_late_cycle_before_the_retry_is_never_cycled_again_on_top(cool):  # noqa: F811
    """The retry's boot check: the host went down a minute after the window (a slow cycler). On the
    boot it brought up that request is a cycle that ran, so no second cycle follows before the
    cooldown it anchors ends."""
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    cool["boot"].update(id="boot-B", btime=T0 + WINDOW + 60)
    now = T0 + WINDOW + 180
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=now)
    assert not allowed and "cooldown" in why
    assert m.power_cycle_cooldown_state.get("missed", 0) == 0
    assert m.boot_from_broker_escalation("boot-B") is not None


@pytest.mark.asyncio
async def test_a_whole_tray_drop_still_goes_to_the_power_cycle_after_a_missed_request(cool):  # noqa: F811
    """The missed request's retry runs through the gated whole-tray path (spec 04 I37): the same
    tray off at the retry deadline fires the power cycle, with no ladder rung first."""
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    _tray_off(cool)
    cool["clk"][0] = m.power_cycle_retry[0]
    cool["order"].clear()
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="hold")
    assert cool["order"] == ["power_cycle"]


# ---- (2) missed requests do not double the cooldown -----------------------------------------------


def test_two_missed_requests_leave_the_interval_at_base_and_page(cool, paged):  # noqa: F811
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    _request(cool, m.power_cycle_retry[0])
    second = cool["clk"][0]
    _window_ends(cool, second + WINDOW)
    assert m.power_cycle_interval(now_epoch=second + WINDOW) == POWER_CYCLE_MIN_INTERVAL_SEC
    assert m.power_cycle_cooldown(now_epoch=second + WINDOW) is None, "no cycle ran, nothing anchors I65"
    state = m.power_cycle_cooldown_state
    assert state["missed"] == 2 and "paging an operator" in state["why"]
    assert [e["kind"] for e in paged] == ["power_cycle_missed"]
    # Neither the per-boot cap nor the backstop counts them: the third ask is allowed.
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=state["until"])
    assert allowed, why


def test_a_missed_row_still_counts_for_nothing_on_the_next_boot(cool):  # noqa: F811
    """The on-disk mark keeps a missed request out of the streak after the host does come back on a
    new boot past the attribution window (an operator cycled it later)."""
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    _request(cool, m.power_cycle_retry[0])
    _window_ends(cool, cool["clk"][0] + WINDOW)
    cool["boot"].update(id="boot-B", btime=cool["clk"][0] + BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC + 60)
    assert m.power_cycle_interval(now_epoch=cool["clk"][0] + 120) == POWER_CYCLE_MIN_INTERVAL_SEC
    assert m.boot_from_broker_escalation("boot-B") is None


def test_a_restarted_broker_still_reads_an_old_same_boot_request_as_missed(cool):  # noqa: F811
    """No pending window in memory after a broker restart: a same-boot row older than the window is
    missed all the same, and its retry is due as its window ends, not after the 30-min cooldown."""
    m = cool["g"].mechanism
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0, "at": "x"})
    now = T0 + WINDOW + 10
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=now)
    assert allowed, why
    assert _rows()[-1]["took_host_down"] is False


# ---- (3) a real cycle still anchors and doubles ----------------------------------------------------


def test_a_cycle_that_took_the_host_down_still_anchors_the_cooldown_and_doubles(cool):  # noqa: F811
    m = cool["g"].mechanism
    at = T0
    for n in range(3):
        _request(cool, at)
        cool["boot"].update(id=f"boot-{n}", btime=at + 120)  # the host went down and came back
        m.host_action_requested = None
        cd = m.power_cycle_cooldown(now_epoch=at + 180)
        assert cd is not None and cd["last"] == at + 120
        at = cd["until"]
    assert [r.get("took_host_down") for r in _rows()] == [None, None, None]
    assert m.power_cycle_interval(now_epoch=at) == 7200


def test_cycles_that_land_after_their_window_still_double_the_cooldown_and_count_for_the_backstop(
    cool, paged  # noqa: F811
):
    """The loop guard (spec 04 I65/I72): a slow cycler takes the host down 2 min after each window
    ran out, so each row was marked missed first. The next boot starts within the window plus the
    attribution window of it with no other escalation between: it is a cycle that ran (late), on that
    boot and every later one. The cooldown doubles 30 min, 1 h, 2 h, then 4 h; the backstop and
    the boot's attribution count it; no miss is paged."""
    m = cool["g"].mechanism
    at, intervals = T0, []
    for n in range(5):
        _request(cool, at)
        _window_ends(cool, at + WINDOW)
        btime = at + WINDOW + 120
        cool["boot"].update(id=f"boot-{n}", btime=btime)
        m.host_action_requested = None
        cool["clk"][0] = btime + 60
        assert m.boot_from_broker_escalation(f"boot-{n}") is not None
        cd = m.power_cycle_cooldown(now_epoch=btime + 60)
        assert cd is not None and cd["last"] == btime
        intervals.append(cd["interval_sec"])
        if n == 1:
            ran = m._cycles_that_ran(_rows(), btime + 60)
            assert len(_power_cycle_backstop_stamps(ran, btime + 60)) == 2
        at = cd["until"]
    assert intervals == [1800, 3600, 7200, 14400, 14400]
    assert all(r.get("late") is True and r.get("took_host_down") is True for r in _rows()[:4])
    assert paged == []


def _broker_starts(monkeypatch, tmp_path):
    """The broker's startup step on the current boot (the real one), with the firmware's account of
    the last boot clean and nothing written outside ``tmp_path``."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "previous_boot_error", lambda: {"present": False})
    monkeypatch.setattr(srv, "previous_boot_bus_locks", lambda: 0)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "mark_boot", lambda *a, **k: None)
    srv._record_previous_boot_error()


def test_a_late_cycle_whose_boot_only_reboots_still_doubles_the_cooldown_on_later_boots(
    cool, monkeypatch, tmp_path, paged  # noqa: F811
):
    """The loop guard (spec 04 I65/I72) when the boot a late cycle brought up runs no power-cycle
    gate: it escalates by reboot once the 1 h loop guard allows it. Broker startup writes the late
    mark, so the boot after that reboot still counts the cycle: the cooldown doubles 30 min, 1 h,
    2 h, then 4 h, and the backstop counts the late cycles (two once two fall in its window)."""
    m = cool["g"].mechanism
    assert srv.recovery_mechanism is m
    at, intervals, live_backstop, later_backstop = T0, [], [], []
    for n in range(4):
        _request(cool, at)
        _window_ends(cool, at + WINDOW)
        btime = at + WINDOW + 120
        cool["boot"].update(id=f"boot-{n}a", btime=btime)
        m.host_action_requested = None
        cool["clk"][0] = btime + 30
        _broker_starts(monkeypatch, tmp_path)
        _broker_starts(monkeypatch, tmp_path)  # a restart on the same boot changes nothing
        ran = m._cycles_that_ran(_rows(), btime + 30)
        live_backstop.append(len(_power_cycle_backstop_stamps(ran, btime + 30)))
        t = at + 3600 + 30
        cool["clk"][0] = t
        allowed, why = m.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=t)
        assert allowed, why
        m.record_auto_recovery("reboot", "wedge", now_epoch=t)
        later = t + 60
        cool["boot"].update(id=f"boot-{n}b", btime=later)
        cool["clk"][0] = later + 1
        cd = m.power_cycle_cooldown(now_epoch=later + 1)
        assert cd is not None
        intervals.append(cd["interval_sec"])
        ran = m._cycles_that_ran(_rows(), later + 1)
        later_backstop.append(len(_power_cycle_backstop_stamps(ran, later + 1)))
        at = cd["until"]
    assert intervals == [1800, 3600, 7200, 14400]
    assert live_backstop[:2] == [1, 2] and all(c >= 1 for c in live_backstop)
    assert later_backstop == [1, 1, 1, 1]
    cycles = [r for r in _rows() if r["action"] == "power-cycle"]
    assert len(cycles) == 4 and all(r.get("took_host_down") is True and r.get("late") is True for r in cycles)
    assert paged == []


def test_the_startup_mark_leaves_an_unreadable_ledger_as_it_is(cool, monkeypatch, caplog):  # noqa: F811
    """Fail-safe: a ledger the broker cannot read at startup is left as it is and logged."""
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    cool["boot"].update(id="boot-B", btime=T0 + WINDOW + 120)
    before = (recovery_base.health_dir() / AUTO_RECOVERY_LEDGER).read_text()
    monkeypatch.setattr(m, "read_auto_recovery_ledger", lambda: None)
    with caplog.at_level("WARNING"):
        m.mark_late_power_cycle(now_epoch=T0 + WINDOW + 180)
    assert (recovery_base.health_dir() / AUTO_RECOVERY_LEDGER).read_text() == before
    assert "unreadable" in caplog.text


# ---- (4) goal 8: a healed retry releases only after the full check --------------------------------


def test_a_retried_cycle_that_heals_releases_only_after_the_full_fabric_check(monkeypatch, tmp_path):
    """A missed request, then a retry that took the host down: the release on the new boot still
    waits for the fabric pass (spec 04 I32), and only that release clears the retry and the streak."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    m = srv.recovery_mechanism
    monkeypatch.setattr(m, "power_cycle_retry", None)
    monkeypatch.setattr(m, "power_cycle_cooldown_state", None)
    now = time.time()
    monkeypatch.setattr(m, "_current_boot_id", lambda: "boot-A")
    m.record_auto_recovery("power-cycle", "tray off", now_epoch=now - WINDOW - 600)  # missed
    m.note_missed_power_cycles(now_epoch=now - 595)  # its window just ran out
    assert m.power_cycle_cooldown_state["missed"] == 1
    assert _rows()[0]["took_host_down"] is False
    m.record_auto_recovery("power-cycle", "tray off", now_epoch=now - 280)  # the retry: it cycled
    monkeypatch.setattr(m, "_current_boot_id", lambda: "boot-B")
    assert m.power_cycle_interval() == recovery_base.power_cycle_interval_sec(1), "the miss counts for nothing"
    held = []
    monkeypatch.setattr(srv, "_hold_device_fabric_unverified", lambda why: held.append(why))
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: FABRIC_DEBT.owed)  # a multi-chip mesh with a fabric check
    monkeypatch.setattr(FABRIC_DEBT, "owed", "power-cycle")
    assert srv._clear_device_dirty(verified=True, why="post-cycle check") is False
    assert held and m.power_cycle_cooldown_state is not None, "no release while the fabric pass is owed"
    FABRIC_DEBT.owed = ""
    assert srv._clear_device_dirty(verified=True, why="post-cycle check") is True
    assert m.power_cycle_retry is None and m.power_cycle_cooldown_state is None
    assert m.power_cycle_miss_state() is None
    assert m.power_cycle_interval() == POWER_CYCLE_MIN_INTERVAL_SEC


# ---- (5) /health shows the missed count and the next retry -----------------------------------------


def test_health_shows_the_missed_count_and_the_next_retry(cool, monkeypatch):  # noqa: F811
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    second = T0 + WINDOW
    _request(cool, second)
    _window_ends(cool, second + WINDOW)
    cd = _health(monkeypatch, second + WINDOW + 1)
    assert cd["missed_requests"] == 2 and cd["cause"] == "missed power-cycle request"
    assert cd["next_deadline"] == datetime.fromtimestamp(second + WINDOW + 300).isoformat(timespec="seconds")
    assert cd["interval_sec"] == 300 and m.power_cycle_retry[0] == second + WINDOW + 300


def test_health_shows_no_missed_requests_for_a_plain_cooldown(cool, monkeypatch):  # noqa: F811
    cool["boot"]["btime"] = T0 - 180
    cool["g"].mechanism.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0)
    assert _health(monkeypatch, T0)["missed_requests"] == 0


# ---- I65: a cycle a later verified release followed is not a failed cycle --------------------------


@pytest.mark.parametrize("failed, expect", [(2, 3600), (3, 7200)])
def test_a_verified_release_on_a_later_boot_ends_the_streak_without_a_heal_file(cool, failed, expect):  # noqa: F811
    """The heal file is missing (written by a release before it existed). Cycle 1 was followed by a
    verified release on the boot it brought up; the cycles after it healed nothing. Only those count:
    2 failed is 1 h and 3 is 2 h, not the 2 h and 4 h of counting cycle 1 too."""
    m = cool["g"].mechanism
    assert not (recovery_base.health_dir() / POWER_CYCLE_HEALED_FILE).exists()
    h = 3600
    rows = [{"action": "power-cycle", "boot_id": "b0", "at_epoch": T0 - 10 * h, "at": "x"}]
    rows += [
        {"action": "power-cycle", "boot_id": f"b{n}", "at_epoch": T0 - (8 - 2 * n) * h, "at": "x"}
        for n in range(1, failed + 1)
    ]
    _ledger(*rows)
    assert m.power_cycle_interval(now_epoch=T0) == recovery_base.power_cycle_interval_sec(failed + 1)
    (recovery_base.health_dir() / recovery_base.AUTO_RECOVERY_VERIFIED).write_text(
        json.dumps({"boot_id": "b1", "at_epoch": T0 - 9 * h, "via": "gate/startup"})
    )
    assert m.power_cycle_interval(now_epoch=T0) == expect


def test_a_verified_release_before_the_cycles_does_not_end_their_streak(cool):  # noqa: F811
    m = cool["g"].mechanism
    (recovery_base.health_dir() / recovery_base.AUTO_RECOVERY_VERIFIED).write_text(
        json.dumps({"boot_id": "b0", "at_epoch": T0 - 10 * 3600, "via": "gate/startup"})
    )
    _ledger(
        *({"action": "power-cycle", "boot_id": f"b{n}", "at_epoch": T0 - (8 - n) * 3600, "at": "x"} for n in range(4))
    )
    cool["boot"].update(id="b9", btime=T0 - 3600)
    assert m.power_cycle_interval(now_epoch=T0) == 14400
