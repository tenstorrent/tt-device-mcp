# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I107: a below-floor hold whose every off-bus chip a bridge reset left ARC-dead (I94) skips
the early mesh reset of the off-bus escalation and climbs at once. The last-chance sweep (SBR, mesh
reset, full check with fabric) still runs right before the power cycle, and the 10-min bound's
ladder still runs its own mesh reset. No test reaches a real reset, setpci or power cycle."""

from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_recovery
from tests.test_below_floor_offbus_ladder import GONE, _bound_ladder, _rig
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import Recovery, galaxy

SWEEP = ["sbr ['31']", "mesh reset", "full check", "power cycle"]


def _arc_rig(monkeypatch, tmp_path, *, arc_dead=True, **kw):
    """``_rig`` with chip 31 isolated and an SBR that brings it back with its ARC dead (``arc_dead``)
    or not healed at all (``state['arc_dead']`` switches it). Returns (order, events, counters, state)."""
    order, events, counters = _rig(monkeypatch, tmp_path, **kw)
    state = {"arc_dead": arc_dead}
    srv.isolated_chips = {GONE}
    heals = kw.get("sbr_heals", False)

    async def sbr(log, **_k):
        order.append(f"sbr {sorted(srv.isolated_chips, key=int)}")
        srv.galaxy_recovery.last_bridge_reset_reasons = {}
        srv.galaxy_recovery.last_bridge_reset_fired = set(srv.isolated_chips)
        if heals:
            srv.isolated_chips.clear()
            return True
        if state["arc_dead"]:
            srv.galaxy_recovery.last_bridge_reset_arc_dead = {GONE}
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    return order, events, counters, state


async def _gate_sbr_then_forced_run(order):
    """The gate's SBR at the hold's start, then the off-bus one-shot's forced run."""
    await srv.galaxy_recovery._sbr(lambda m: None)
    order.clear()
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)


@pytest.mark.asyncio
async def test_arc_dead_and_still_dead_skips_the_early_mesh_reset_and_climbs_at_once(
    monkeypatch, tmp_path, clear_job_state
):
    """(a) No early mesh reset; the next step (the last-chance sweep, then the power cycle) runs at
    once, not at the 10-min ladder end."""
    order, events, _counters, _state = _arc_rig(monkeypatch, tmp_path, held_min=1)
    await _gate_sbr_then_forced_run(order)
    assert "galaxy_reset_skipped_arc_dead" in events
    assert "stuck_hold_galaxy_reset_below_floor_attempt" in events
    assert "host_rung_deferred_until_ladder_end" not in events
    assert "host_rung_not_deferred" in events
    assert order == SWEEP, "one mesh reset only, the sweep's, right before the power cycle"


@pytest.mark.asyncio
async def test_without_an_arc_dead_record_the_early_mesh_reset_still_runs(monkeypatch, tmp_path, clear_job_state):
    """The control: an SBR that left the chip off the bus with no ARC verdict keeps the early mesh
    reset and its full check, then waits for the ladder end."""
    order, events, _counters, _state = _arc_rig(monkeypatch, tmp_path, arc_dead=False, held_min=1, mesh_rc=None)
    await _gate_sbr_then_forced_run(order)
    assert "galaxy_reset_skipped_arc_dead" not in events
    assert order == ["mesh reset", "full check"]
    assert "host_rung_deferred_until_ladder_end" in events


@pytest.mark.asyncio
async def test_a_chip_seen_back_since_its_arc_dead_round_is_out_of_the_record(monkeypatch, tmp_path, clear_job_state):
    """A chip back on the bus since (a rescan, the driver wait) is a new drop: its own SBR round, then
    the early mesh reset."""
    order, events, _counters, state = _arc_rig(monkeypatch, tmp_path, held_min=1, mesh_rc=None)
    await srv.galaxy_recovery._sbr(lambda m: None)
    assert srv.galaxy_recovery._arc_dead_now(srv.device_hold_episode_since) == {GONE}
    pci.forget_removed_chip(GONE)  # seen back on the bus
    state["arc_dead"] = False  # its next bridge reset leaves it off the bus with no ARC verdict
    assert srv.galaxy_recovery._arc_dead_now(srv.device_hold_episode_since) == frozenset()
    order.clear()
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert "galaxy_reset_skipped_arc_dead" not in events
    assert order[:3] == ["sbr ['31']", "mesh reset", "full check"]


@pytest.mark.asyncio
async def test_arc_dead_healed_by_the_bridge_reset_lifts_only_on_the_full_check_with_fabric(
    monkeypatch, tmp_path, clear_job_state
):
    """(b) A bridge reset that brings the chip back with a live ARC records nothing: the full check
    with fabric runs and passes before the lift; no mesh reset, no power cycle."""
    order, events, counters, _state = _arc_rig(monkeypatch, tmp_path, held_min=1, sbr_heals=True, verdicts=[True])
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order == ["sbr ['31']", "full check"], "the full check with fabric runs before the lift"
    assert counters["dirty_cleared"] == 1
    assert "galaxy_reset_skipped_arc_dead" not in events
    assert srv.galaxy_recovery._arc_dead_now(srv.device_hold_episode_since) == frozenset()


@pytest.mark.asyncio
async def test_the_pre_cycle_ladder_still_runs_the_mesh_reset_and_the_last_chance_sweep(
    monkeypatch, tmp_path, clear_job_state
):
    """(c) Goal 12: right before the power cycle request the sweep runs its SBR, the mesh reset
    (glx_reset, with its POST_RESET and board re-init) and the full check with fabric."""
    order, events, _counters, _state = _arc_rig(monkeypatch, tmp_path, held_min=1)
    argvs = []
    mesh = srv.recovery_mechanism.reset_with_quiesce

    async def capture(argv, log, *a, **k):
        argvs.append(list(argv))
        return await mesh(argv, log, *a, **k)

    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", capture)
    await _gate_sbr_then_forced_run(order)
    assert "last_chance_reset_sweep" in events
    assert len(argvs) == 1 and "-glx_reset" in argvs[0], "the sweep's mesh reset is a glx_reset"
    cycle = order.index("power cycle")
    assert order[:cycle] == ["sbr ['31']", "mesh reset", "full check"]


@pytest.mark.asyncio
async def test_the_10_min_bound_ladder_is_unchanged_and_still_runs_the_mesh_reset(
    monkeypatch, tmp_path, clear_job_state
):
    """(d) Goal 10: an arc-dead record does not touch the bound's ladder: rescan, its mesh reset with
    the full check, then the sweep's, then the power cycle."""
    order, events, _counters, _state = _arc_rig(monkeypatch, tmp_path)
    await srv.galaxy_recovery._sbr(lambda m: None)
    order.clear()
    await _bound_ladder()
    assert "galaxy_reset_skipped_arc_dead" not in events
    assert "mesh reset" in order and order[-1] == "power cycle"
    assert order.index("mesh reset") < order.index("full check") < order.index("power cycle")


@pytest.mark.asyncio
async def test_a_failed_full_check_after_the_ladder_keeps_the_hold_and_requests_the_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """(e) Goals 8/11: the sweep's full check fails, so the hold stays and the power cycle is
    requested; nothing lifts it."""
    order, _events, counters, _state = _arc_rig(monkeypatch, tmp_path, held_min=1, verdicts=[False])
    await _gate_sbr_then_forced_run(order)
    assert order[-1] == "power cycle"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_the_arc_check_records_the_dead_chips_and_a_lift_clears_the_record(
    monkeypatch, tmp_path, clear_job_state
):
    """The I94 check's verdict reaches the record through GalaxyRecovery, and the lift ends it."""

    async def dead(self, chips, log):
        return ["31"]

    monkeypatch.setattr(Recovery, "_arc_dead_after_bridge_reset", dead)
    _rig(monkeypatch, tmp_path, held_min=1)
    gr = srv.galaxy_recovery
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=1)).isoformat()
    assert await gr._arc_dead_after_bridge_reset(["31"], lambda m: None) == ["31"]
    assert gr.last_bridge_reset_arc_dead == {"31"}
    gr._note_arc_dead(gr.last_bridge_reset_arc_dead)
    assert gr._arc_dead_now(srv.device_hold_episode_since) == {"31"}
    assert gr._arc_dead_now("2000-01-01T00:00:00") == frozenset(), "keyed by the hold episode"
    gr.sbr_record_clear()
    assert gr._arc_dead_now(srv.device_hold_episode_since) == frozenset()
    assert isinstance(gr, galaxy.GalaxyRecovery)
