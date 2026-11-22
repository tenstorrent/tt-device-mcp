# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I111: a hold the gate kept dirty because the dead-chip isolation killed its fabric check (spec 03
I82), with every chip back on the bus, runs the forced ladder at the present-mesh grace. Nothing else resets
that idle dirty hold before the ceiling window, so it used to sit about 600 s; on a 32-chip Galaxy the
forced glx_reset then healed it in 33 s. The run's own full check, fabric included, still decides the
release, and the 10-min bound's ladder and host rung are unchanged."""

from datetime import datetime, timedelta

import pytest

from tests import test_fabric_killed_by_dead_chip as fk
from tests import test_post_reset_gate as prg
from tests.conftest import fsm_dirty
from tests.test_offbus_one_shot_unisolated import _real_mesh_reset
from tests.test_post_reset_gate import _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.post_reset import FABRIC_DEBT

gate = fk.gate  # a 32-chip broker whose verify reads enum+ARC healthy with an isolation-killed fabric run
verdict = prg.verdict
_REAL_ESCALATE = srv.galaxy_recovery.escalate


async def _killed_hold(monkeypatch, *, run_gate=True):
    """The field episode: chip 15 left the bus and came back, the isolation killed the post-job fabric pass,
    and the gate kept the hold dirty. The hold opens now, idle, with no other rung armed."""
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setattr(srv, "read_heartbeats", lambda *a, **k: {str(i): 100 + i for i in range(32)})
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_offbus_hold_chips", set(), raising=False)
    for name, value in (
        ("_isolation_kill_hold", False),
        ("_isolation_kill_escalated", ""),
        ("device_hold_offbus_escalated", False),
        ("device_hold_escalate_bucket", 0),
        ("device_hold_deadline_bucket", 0),
        ("device_hold_tenant_deferred", False),
        ("_forced_escalation_task", None),
    ):
        monkeypatch.setattr(srv, name, value)
    fsm_dirty(srv, fk.REASON, why="heartbeat")
    if run_gate:
        await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.now().isoformat())
    assert srv.fsm.record.dirty and srv.fsm.state is not ServerState.HEALTHY


async def tick(sec):
    """One sampler pass ``sec`` into the open hold episode."""
    srv._check_hold_deadline(now=datetime.fromisoformat(srv.device_hold_episode_since) + timedelta(seconds=sec))
    if srv._forced_escalation_task is not None:
        await srv._forced_escalation_task


def _forced(events):
    return [f for k, f in events if k == "hold_deadline_forced_escalation"]


@pytest.mark.asyncio
async def test_an_isolation_killed_hold_runs_the_forced_ladder_at_the_present_mesh_grace(gate, monkeypatch):
    """Cell isolation-kill-reset-at-grace: the reset fires on the first sampler tick past the grace, not at the
    600 s ceiling, once per episode; the ceiling window still runs if the hold stands."""
    events, seen = gate
    await _killed_hold(monkeypatch)
    assert srv._isolation_kill_hold, "the gate did not mark the isolation-killed hold"
    grace, step, ceiling = srv._present_mesh_reset_grace_sec(), srv.SAMPLE_INTERVAL_SEC, srv._stuck_hold_ceiling_sec()
    assert grace + step < ceiling
    started = datetime.fromisoformat(srv.device_hold_episode_since).timestamp()
    nxt = srv._hold_next_rung(started + 1)
    assert nxt is not None and nxt.at == started + grace, "/health does not report the grace run (spec 04 I75)"

    await tick(grace - 1)
    assert seen["rungs"] == [] and _forced(events) == [], "the forced ladder ran before the grace"
    await tick(grace + step)
    assert seen["rungs"] == ["present-forced"], "the isolation-killed hold waited past the grace"
    (ev,) = _forced(events)
    assert ev["trigger"] == "isolation_kill_grace" and ev["window_sec"] == int(grace)
    assert srv.device_hold_escalate_bucket == 0, "the grace run claimed the ceiling window"

    await tick(grace + 3 * step)
    assert seen["rungs"] == ["present-forced"], "the grace run is once per episode"
    await tick(ceiling)
    assert len(seen["rungs"]) == 2 and srv.device_hold_escalation_kind == "ceiling_window"


@pytest.mark.asyncio
async def test_an_isolated_chip_leaves_the_hold_to_the_off_bus_one_shot(gate, monkeypatch):
    """A chip still isolated is the off-bus one-shot's (its faster clock), not the grace run's."""
    _, seen = gate
    await _killed_hold(monkeypatch)
    monkeypatch.setattr(srv, "isolated_chips", {"15"})
    assert not srv._isolation_kill_grace_due(srv._present_mesh_reset_grace_sec())
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert srv.device_hold_escalation_kind == "offbus_one_shot"


@pytest.mark.asyncio
async def test_a_dirty_hold_the_isolation_did_not_kill_keeps_its_own_clock(gate, monkeypatch):
    """Only the gate's isolation-killed verdict arms the grace run: another dirty hold keeps its own path."""
    events, seen = gate
    await _killed_hold(monkeypatch, run_gate=False)
    await tick(srv._present_mesh_reset_grace_sec() + srv.SAMPLE_INTERVAL_SEC)
    assert seen["rungs"] == [] and _forced(events) == []


@pytest.mark.asyncio
async def test_the_grace_run_releases_only_on_a_just_passed_fabric_check(gate, monkeypatch, verdict):
    """Cell release-needs-fabric-pass: the grace run's mesh reset and its full check run for real. A 77 or a
    check with no fabric verdict keeps the hold, and no device_dirty_cleared is journaled; only a passing fabric
    pass releases."""
    evidence, released = verdict
    events, _ = gate
    await _killed_hold(monkeypatch)
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", _REAL_ESCALATE)
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    resets = _real_mesh_reset(monkeypatch)
    await tick(srv._present_mesh_reset_grace_sec() + srv.SAMPLE_INTERVAL_SEC)
    assert resets == ["mesh reset"], "the grace run did not reset the present mesh"
    assert calls and all(calls), "the post-reset verify skipped the fabric pass"
    kinds = [k for k, _ in events]
    if released:
        assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""
        assert "device_dirty_cleared" in kinds
    else:
        assert srv.fsm.state is not ServerState.HEALTHY, "released with no just-passed fabric check"
        assert "device_dirty_cleared" not in kinds
        assert srv.device_hold_episode_since, "the hold episode closed, and the 10-min bound with it"
