# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The fabric link-short hold (spec 04 I83).

Every chip is on the bus, but the full fabric check counts fewer links or ports than the cabling
expects (a topology mismatch, or a known fabric fault). The broker runs its full reset ladder once, on
the first watchdog tick of the hold; when the full check after it (fabric included) still fails, it
asks for the power cycle at once. No later tick in the episode runs a second glx_reset or ladder. Only
the bounded boot / post-power-cycle cooldown may delay the cycle. No test reaches a real reset or cycle."""

import time
from datetime import datetime, timedelta

import pytest

from tests.test_device_safety import _arm_fabric_only_deadline_hold
from tests.test_hold_bound import _KINDS, _REAL_HOST_RUNG, BOUND, _drain, _watchdog
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED, OUTCOME_TERMINAL

LINK_SHORT = ["topology mismatch", "known fabric fault"]


def _arm(monkeypatch, hold, age_sec=10, latched=False):
    """A ``hold`` opened ``age_sec`` ago, with the watchdog at the start of its episode. The host rung
    is stubbed with its real bookkeeping (a requested power cycle marks the host action pending) and
    lands in events as ``host_rung``."""
    why, detail, output, beats, _ladder = _KINDS[hold]
    escalations, alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output, beats=beats, age_min=age_sec / 60
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: hold == "77-only")
    if hold == "known fabric fault":
        monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "known fabric fault: 2 jobs in a row")
        monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "known fabric fault", raising=False)
    if hold == "off-bus":
        monkeypatch.setattr(srv, "dead_chips", lambda b: [])
    if latched:
        monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "hardware fault: needs an operator")

    async def host_rung(age, log):
        events.append("host_rung")
        srv._hold_bound_rung_pending, srv._hold_bound_cycle_age = False, age
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    return escalations, alerts, events


def _ladder(monkeypatch, escalations, outcome, *, passes=False):
    async def ladder(kind, indices, expected, log):
        escalations.append(kind)
        if passes:
            monkeypatch.setattr(srv, "_recovery_degraded", lambda: False)  # the full check passed
        return outcome

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)


def _tick(after_sec):
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=after_sec))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [OUTCOME_RECOVERED, OUTCOME_TERMINAL])
@pytest.mark.parametrize("hold", LINK_SHORT)
async def test_one_ladder_at_hold_start_then_the_power_cycle_at_once(monkeypatch, clear_job_state, hold, outcome):
    """(a) the first tick of the hold runs the full ladder; (b) a full check after it that still counts
    short (or a ladder that did not bring the mesh back) asks for the host rung in the same run, long
    before the 10-min bound; nothing is released."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    _ladder(monkeypatch, escalations, outcome)
    _tick(0)
    await _drain()
    assert srv.device_hold_escalation_kind == "fabric_link_short"
    assert escalations == ["present-forced"], "one full ladder at hold start, not at the 10-min mark"
    assert events.count("host_rung") == 1, "then the power cycle, at once"
    assert events.index("hold_deadline_forced_escalation") < events.index("host_rung")
    assert srv._recovery_degraded() and "device_released" not in events, "no release on a failed check"
    assert srv._hold_age_now() < BOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", LINK_SHORT)
async def test_no_second_glx_reset_or_ladder_in_the_episode(monkeypatch, clear_job_state, hold):
    """(c) after the at-start pass and its power-cycle request, no later tick (the bound, the next
    ceiling window, the hold deadline) runs another glx_reset or ladder, or asks for a second cycle."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    _tick(0)
    await _drain()
    for after in range(10, max(srv._hold_deadline_sec(), 2 * BOUND) + 20, 10):
        _tick(after)
        await _drain()
    assert escalations == ["present-forced"], "the at-start pass is the episode's only ladder"
    assert events.count("host_rung") == 1 and "device_released" not in events


@pytest.mark.asyncio
async def test_a_latched_mismatch_gets_the_power_cycle_and_no_release(monkeypatch, clear_job_state):
    """The I42 hardware-fault latch on a one-port-short mismatch is lifted for the ladder, and the full
    check after it counts the same port short: one power-cycle request, no device_released."""
    escalations, _alerts, events = _arm(monkeypatch, "topology mismatch", latched=True)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    _tick(0)
    await _drain()
    assert "hold_bound_latch_cleared" in events
    assert escalations == ["present-forced"]
    assert events.count("hold_bound_check_failed") == 1
    assert events.count("host_rung") == 1 and "device_released" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", LINK_SHORT)
async def test_a_full_check_that_passes_after_the_ladder_asks_for_no_power_cycle(monkeypatch, clear_job_state, hold):
    """(d) only a full check that passed after the ladder ends the hold's escalation: no host rung, then
    or on a later tick, and no second ladder."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED, passes=True)
    _tick(0)
    await _drain()
    _tick(10)
    await _drain()
    assert escalations == ["present-forced"]
    assert "hold_bound_check_failed" not in events and "host_rung" not in events


@pytest.mark.asyncio
async def test_a_check_still_waiting_gets_the_power_cycle_by_the_bound(monkeypatch, clear_job_state):
    """(d) a full check with no verdict yet (a 77 while the 1G hugepage pool fills) keeps the hold and
    asks for no cycle while it runs; it has until the 10-min bound at the latest, then the host rung."""
    escalations, _alerts, events = _arm(monkeypatch, "topology mismatch")
    monkeypatch.setattr(srv, "_last_77_was_hugepages", lambda: True)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    _tick(0)
    await _drain()
    assert escalations == ["present-forced"] and "host_rung" not in events
    assert srv._recovery_degraded()
    _tick(srv._HOLD_BOUND_CHECK_SEC - 30)
    await _drain()
    assert "host_rung" not in events, "the full check gets its time"
    _tick(BOUND)
    await _drain()
    assert escalations == ["present-forced"] and events.count("host_rung") == 1
    assert srv._hold_age_now() <= BOUND + 30


@pytest.mark.asyncio
async def test_the_cooldown_delays_the_cycle_only_until_its_end(monkeypatch, clear_job_state):
    """(e) a running boot / post-power-cycle cooldown denies the host rung and arms the retry; the cycle
    fires on the first tick past the cooldown's end, with no second ladder."""
    escalations, _alerts, events = _arm(monkeypatch, "topology mismatch")
    monkeypatch.setattr(srv, "_hold_bound_host_rung", _REAL_HOST_RUNG)  # the governor decides below
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "_journal_auto_recovery_denied", lambda *a: None)
    cycles, until, state = [], datetime.now().timestamp() + 240, {"cooling": True}

    def allowed(action, tenant_active=False, **k):
        if state["cooling"]:
            return srv.recovery_mechanism._deny_power_cycle(
                {"until": until, "last": until - 1800, "cause": "boot", "interval_sec": 1800, "why": "cooldown"}
            )
        return True, ""

    async def power_cycle(log, reason):
        cycles.append(reason)

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    _ladder(monkeypatch, escalations, OUTCOME_TERMINAL)
    _tick(0)
    await _drain()
    assert escalations == ["present-forced"] and cycles == []
    assert srv.recovery_mechanism.power_cycle_retry is not None, "the denial arms the retry"

    srv._check_hold_deadline(now=datetime.fromtimestamp(until - 20))
    await _drain()
    assert cycles == [], "no cycle inside the cooldown"
    state["cooling"] = False
    srv._check_hold_deadline(now=datetime.fromtimestamp(until + 1))
    await _drain()
    assert escalations == ["present-forced"], "the cooldown's end goes straight to the power cycle"
    assert len(cycles) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["77-only", "off-bus", "generic"])
async def test_other_classes_get_no_ladder_at_hold_start(monkeypatch, clear_job_state, hold):
    """(f) the at-start pass is this class's only: a 77-only, off-bus or generic hold on its first tick
    runs no ladder from I83 (each keeps its own path: the idle relift, the off-bus one-shot, the bound)."""
    escalations, _alerts, events = _arm(monkeypatch, hold, age_sec=1)
    monkeypatch.setattr(srv, "_offbus_one_shot_sec", lambda: 60.0)
    _ladder(monkeypatch, escalations, OUTCOME_TERMINAL)
    _tick(0)
    await _drain()
    assert srv.device_hold_escalation_kind != "fabric_link_short"
    assert escalations == [] and "host_rung" not in events


def test_an_offbus_chip_is_not_the_link_short_class(monkeypatch, clear_job_state):
    """(f) with a chip isolated off the bus the hold is the partial off-bus class, which keeps its ladder."""
    _arm(monkeypatch, "topology mismatch")
    assert srv._fabric_link_short_hold() == "topology mismatch"
    monkeypatch.setattr(srv, "isolated_chips", {"29"})
    assert srv._fabric_link_short_hold() == ""
    assert not srv._fabric_link_short_ladder_due(10)
