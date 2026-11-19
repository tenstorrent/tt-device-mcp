# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I76/I87/I89 and I65: the full ladder at the 10-min hold bound and right before a cooldown-deferred power
cycle, on a fake clock.

At 600 s the bound runs every rung, the mesh reset this hold already spent included, and asks for the power cycle only
after the full check with fabric fails. When the I65 cooldown denies that cycle, the cooldown's end runs the last-chance
sweep first (the mesh reset, then the full check with fabric, under the I97/I100 deadline) and requests the cycle only
when that check fails or is cut at its deadline. Only a just-passed full check with fabric lifts the hold. No test
reaches a real reset or power cycle, and none changes how the I97 deadline
(from the hold's start) is computed or enforced."""

import asyncio
from datetime import datetime

import pytest

from tests.conftest import patch_recovery
from tests.test_below_floor_offbus_ladder import GONE, _bound_ladder, _clock, _rig
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import galaxy

COOLDOWN_SEC = 900


def _cooldown_deferred(monkeypatch, tmp_path, *, verdicts=(), high_water=True):
    """A below-floor hold 300 s old whose mesh reset already failed on chip 31, and an I65 cooldown that denies every
    power cycle until ``state['cooling']`` is cleared."""
    order, events, counters = _rig(monkeypatch, tmp_path, held_min=5, verdicts=verdicts)
    # The host's high-water mark of 32 chips, as on a real box: without it ``expected`` is the 31 still present and
    # chip 31 drops out of the off-bus set the sweep reads.
    if high_water:
        monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    clock = _clock(monkeypatch)
    rm = srv.recovery_mechanism
    srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    until = datetime.now().timestamp() + COOLDOWN_SEC
    monkeypatch.setattr(rm, "power_cycle_retry", None)
    monkeypatch.setattr(rm, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(rm, "_journal_auto_recovery_denied", lambda *a: None)
    state = {"cooling": True}

    def allowed(action, tenant_active=False, **k):
        if state["cooling"] and action == "power-cycle":
            return rm._deny_power_cycle(
                {"until": until, "last": until - 1800, "cause": "boot", "interval_sec": 1800, "why": "cooldown"}
            )
        return True, ""

    monkeypatch.setattr(rm, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_bound_tenant_blocker", lambda *a: None)
    return order, events, counters, clock, state


def _age() -> float:
    return galaxy._hold_age_sec(srv.device_hold_episode_since)


async def _at_the_bound(order, events, clock):
    clock.shift = galaxy._stuck_hold_ceiling_sec() - _age()
    await _bound_ladder()
    assert order == ["rescan", "mesh reset", "full check"], "the full ladder at 600 s, its spent mesh reset included"
    assert "bound_reruns_spent_rung" in events
    assert "power cycle" not in order, "the cooldown holds the cycle"
    assert srv.recovery_mechanism.power_cycle_retry is not None, "the denial armed the cooldown-end retry"


async def _cooldown_end(order, clock, state):
    order.clear()
    state["cooling"] = False
    clock.shift += COOLDOWN_SEC
    await srv._hold_bound_host_rung(_age(), lambda m: None, sweep=True)


@pytest.mark.asyncio
async def test_a_cooldown_deferred_cycle_gets_the_full_ladder_at_600s_and_the_sweep_at_the_cooldown_end(
    monkeypatch, tmp_path, clear_job_state
):
    order, events, counters, clock, state = _cooldown_deferred(monkeypatch, tmp_path)
    await _at_the_bound(order, events, clock)
    await _cooldown_end(order, clock, state)
    assert order == [
        "mesh reset",
        "full check",
        "power cycle",
    ], "the sweep, then its full check with fabric, then the cycle"
    assert "check without fabric" not in order, "the sweep's check runs with fabric"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed check lifts nothing"


@pytest.mark.asyncio
async def test_a_sweep_that_passes_at_the_cooldown_end_lifts_the_hold_and_cycles_nothing(
    monkeypatch, tmp_path, clear_job_state
):
    # The bound's check fails; the sweep's full check with fabric passes.
    order, events, counters, clock, state = _cooldown_deferred(monkeypatch, tmp_path, verdicts=[False, True])
    await _at_the_bound(order, events, clock)
    assert counters["dirty_cleared"] == 0, "no lift on the bound's failed check"
    await _cooldown_end(order, clock, state)
    assert order == ["mesh reset", "full check"], "no power cycle on a check that passed"
    assert counters["dirty_cleared"] == 1, "lifted once, on the just-passed full check with fabric"


async def _hung_check_at_the_cooldown_end(monkeypatch, order, events, counters, clock, state):
    """The cooldown-end sweep with a full check that never ends: it is cut at the deadline and the cycle fires."""
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_SEC", 0.5)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_GAVE_UP_SEC", 0.5)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_CHECK_SEC", 1)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC", 0.2)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC", 0.2)
    hung = asyncio.Event()

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check" if run_fabric else "check without fabric")
        await hung.wait()  # never set: a check that does not end

    patch_recovery(monkeypatch, "_verify_device", verify)
    started = datetime.now()
    await asyncio.wait_for(_cooldown_end(order, clock, state), 30)
    took = (datetime.now() - started).total_seconds()
    assert order == ["mesh reset", "full check", "power cycle"], "the cut check counts as failed: the cycle fires"
    assert took < 10, f"bounded by the deadline, not by the check: {took:.1f}s"
    cut = [e for e in events if e == "whole_mesh_reset_before_host_rung_unverified"]
    assert cut, "the cut check is journaled"
    assert counters["dirty_cleared"] == 0, "a cut check lifts nothing"


@pytest.mark.asyncio
async def test_a_present_mesh_sweep_check_cut_at_its_deadline_at_the_cooldown_end_still_power_cycles(
    monkeypatch, tmp_path, clear_job_state
):
    """Spec 04 I65/I100: a hold with every expected chip on the bus (no off-bus set, so no failed reset on it to
    repeat) whose bound cycle the cooldown denied: the cooldown-end sweep's mesh reset and full check still run under
    the deadline, and a check that never ends is cut there and the cycle fires, within the bound."""
    order, events, counters, clock, state = _cooldown_deferred(monkeypatch, tmp_path, high_water=False)
    await _at_the_bound(order, events, clock)
    assert not galaxy._offbus_chip_ids(srv.read_heartbeats(), srv.health_monitor.expected(31)), "a present mesh"
    await _hung_check_at_the_cooldown_end(monkeypatch, order, events, counters, clock, state)


@pytest.mark.asyncio
async def test_a_sweep_check_cut_at_its_deadline_at_the_cooldown_end_still_power_cycles(
    monkeypatch, tmp_path, clear_job_state
):
    """The sweep's repeated mesh reset runs under the I97/I100 deadline (the hold-start one, or its own start plus the
    reset and check allowance when that is too close): a check that never ends is failed there and the cycle fires."""
    order, events, counters, clock, state = _cooldown_deferred(monkeypatch, tmp_path)
    await _at_the_bound(order, events, clock)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_SEC", 0.5)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_GAVE_UP_SEC", 0.5)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_CHECK_SEC", 1)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC", 0.2)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC", 0.2)
    hung = asyncio.Event()

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check" if run_fabric else "check without fabric")
        await hung.wait()  # never set: a check that does not end

    patch_recovery(monkeypatch, "_verify_device", verify)
    started = datetime.now()
    await asyncio.wait_for(_cooldown_end(order, clock, state), 30)
    took = (datetime.now() - started).total_seconds()
    assert order == ["mesh reset", "full check", "power cycle"], "the cut check counts as failed: the cycle fires"
    assert took < 10, f"bounded by the deadline, not by the check: {took:.1f}s"
    cut = [e for e in events if e == "whole_mesh_reset_before_host_rung_unverified"]
    assert cut, "the cut check is journaled"
    assert counters["dirty_cleared"] == 0, "a cut check lifts nothing"


@pytest.mark.asyncio
async def test_the_bounds_rerun_of_a_spent_reset_with_a_chip_still_off_is_cut_at_its_deadline(
    monkeypatch, tmp_path, clear_job_state
):
    """Spec 04 I89/I100: at the 10-min bound a hold whose chip is still off after the kept rescan/SBR reruns its spent
    mesh reset as the I100 repeat. A full check after it that never ends is cut at the rerun's deadline and counts as
    failed: the sweep and the power cycle follow within the stated bound, and nothing lifts the hold."""
    order, events, counters, clock, state = _cooldown_deferred(monkeypatch, tmp_path)
    state["cooling"] = False
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_SEC", 0.5)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_GAVE_UP_SEC", 0.5)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_CHECK_SEC", 1)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC", 0.2)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC", 0.2)
    hung = asyncio.Event()

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check" if run_fabric else "check without fabric")
        await hung.wait()  # never set: a check that does not end

    async def plain_mesh_reset(indices, log):
        # The plain reset and its check, with no deadline: the bound's rerun must not take this path.
        order.append("mesh reset")
        srv.recovery_mechanism.last_reset_rc = 1
        return await verify(32, log)

    patch_recovery(monkeypatch, "_verify_device", verify)
    patch_recovery(monkeypatch, "_reset_and_verify_device", plain_mesh_reset)
    clock.shift = galaxy._stuck_hold_ceiling_sec() - _age()
    started = datetime.now()
    await asyncio.wait_for(_bound_ladder(), 30)
    took = (datetime.now() - started).total_seconds()
    assert "bound_reruns_spent_rung" in events
    assert order[:3] == ["rescan", "mesh reset", "full check"], "the kept rungs, then the spent mesh reset again"
    assert order[-1] == "power cycle" and order.count("power cycle") == 1, "the cut check counts as failed"
    assert took < 15, f"bounded by the deadline, not by the check: {took:.1f}s"
    assert "whole_mesh_reset_before_host_rung_unverified" in events, "the hung check is cut at its deadline"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a cut check lifts nothing"
