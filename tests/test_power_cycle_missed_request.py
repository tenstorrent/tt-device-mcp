# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A power-cycle request that never took the host down (spec 04 I72), and the backoff streak's end
(spec 04 I65).

The power-cycle command can return 0 while the cycle never happens (its BMC call failed). The host
stays up on the same boot past the pending window. That request is a missed attempt, not a cycle: it
neither anchors the cooldown nor counts toward its doubling, and it is asked again 5, 10, then every
15 min after its window, with an operator paged from the second miss on. A cycle a verified release
followed on a later boot healed the box, whether or not a heal file was written. No test reaches a
real reset or power cycle: the ``rig`` records them."""

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
    POWER_CYCLE_HEALED_FILE,
    POWER_CYCLE_MIN_INTERVAL_SEC,
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


# ---- (1) a missed request is asked again within 15 min --------------------------------------------


def test_a_request_the_host_never_acted_on_is_asked_again_5_min_after_its_window(
    cool, monkeypatch, paged  # noqa: F811
):
    """The request returned, the host never went down: at the end of its window it is marked missed
    on disk, the watchdog asks again 5 min later, and the governor allows it then, not 30 min or 4 h
    after the request."""
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    assert _rows()[-1]["took_host_down"] is False
    retry_at = T0 + WINDOW + 300
    assert m.power_cycle_retry[0] == retry_at <= T0 + WINDOW + 900
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=retry_at - 1)[0] is False
    assert _watchdog(cool, monkeypatch, retry_at - 1) == []
    assert _watchdog(cool, monkeypatch, retry_at) == [retry_at]
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=retry_at)
    assert allowed, why
    assert paged == [], "one miss pages no one"


def test_the_retries_back_off_5_10_then_15_min_and_never_stop(cool, paged):  # noqa: F811
    m = cool["g"].mechanism
    at, waits = T0, []
    for _ in range(5):
        _request(cool, at)
        _window_ends(cool, at + WINDOW)
        nxt = m.power_cycle_retry[0]
        waits.append(nxt - at - WINDOW)
        at = nxt
    assert waits == [300, 600, 900, 900, 900]
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=at)[0] is True


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
    new boot (the BMC answered later, or an operator cycled it)."""
    m = cool["g"].mechanism
    _request(cool, T0)
    _window_ends(cool, T0 + WINDOW)
    _request(cool, m.power_cycle_retry[0])
    _window_ends(cool, cool["clk"][0] + WINDOW)
    cool["boot"].update(id="boot-B", btime=cool["clk"][0] + 60)
    assert m.power_cycle_interval(now_epoch=cool["clk"][0] + 120) == POWER_CYCLE_MIN_INTERVAL_SEC
    assert m.boot_from_broker_escalation("boot-B") is None


def test_a_restarted_broker_still_reads_an_old_same_boot_request_as_missed(cool):  # noqa: F811
    """No pending window in memory after a broker restart: a same-boot row older than the window is
    missed all the same, and its retry is due 5 min after its window, not after the 30-min cooldown."""
    m = cool["g"].mechanism
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0, "at": "x"})
    now = T0 + WINDOW + 10
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=now)
    assert not allowed and "never took the host down" in why
    assert m.power_cycle_retry[0] == T0 + WINDOW + 300


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
    cd = _health(monkeypatch, T0 + WINDOW + 1)
    assert cd["missed_requests"] == 1 and cd["cause"] == "missed power-cycle request"
    assert cd["next_deadline"] == datetime.fromtimestamp(T0 + WINDOW + 300).isoformat(timespec="seconds")
    assert cd["interval_sec"] == 300 and m.power_cycle_retry[0] == T0 + WINDOW + 300


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
