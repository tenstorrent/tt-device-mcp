# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I96: the chips that arm the off-bus one-shot are the ones still off the bus. A chip that came
back (on the below-floor rescan, or later on its own) no longer arms it, so no reset runs for it. The hold
stays until the full check passes, fabric included, and the 10-min bound's full ladder still runs."""

from datetime import datetime, timedelta

import pytest

from tests import test_post_reset_gate as prg
from tests.test_arc_not_ready import _fake_sysfs
from tests.test_device_safety import _setup_down_offbus
from tests.test_offbus_one_shot_idle import IDLE, TENANT, _below_floor_hold
from tests.test_offbus_one_shot_unisolated import DEAD, _dead_arc_on_bus, _gate_below_floor_pass, _real_mesh_reset
from tests.test_post_reset_gate import _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import heartbeat
from tt_device_mcp.health.recovery import galaxy

mesh = prg.mesh  # the post-reset gate's fixtures, shared
verdict = prg.verdict

BOUND = 601


def _chip_comes_back(monkeypatch):
    """Chip 8's heartbeat reads again: it is on the bus."""
    beats = {str(i): 100 + i for i in range(32)}
    monkeypatch.setattr(srv, "read_heartbeats", lambda *a, **k: dict(beats))
    monkeypatch.setattr(heartbeat, "_read_beat", lambda node: 108)


async def tick(sec):
    srv._check_hold_deadline(now=datetime.fromisoformat(srv.device_hold_episode_since) + timedelta(seconds=sec))
    if srv._forced_escalation_task is not None:
        await srv._forced_escalation_task
        srv._forced_escalation_task = None


async def _assert_bound_ladder_runs(monkeypatch, count, verdict, resets):
    """The hold stood with no reset; the 10-min bound's ladder runs and frees the device only through the
    full check, fabric included."""
    assert srv.fsm.state is not ServerState.HEALTHY, "the dropped one-shot lifted the hold"
    evidence, released = verdict
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    await tick(BOUND)
    assert count("hold_deadline_forced_escalation") == 1, "the 10-min bound did not run the ladder"
    assert resets == ["mesh reset"], "the bound's ladder did not run the mesh reset"
    assert calls and all(calls), "the bound's post-reset verify skipped the fabric pass"
    if released:
        _assert_settled(released)
    else:
        assert srv.fsm.state is not ServerState.HEALTHY, "released over an unproven fabric"


@pytest.mark.asyncio
async def test_a_dead_arc_chip_back_on_the_rescan_does_not_arm_the_one_shot(mesh, monkeypatch, verdict):
    """Chip 8 enumerates with a dead ARC; the gate's below-floor rescan brings it back but the full check
    still fails. The one-shot is not armed by the read from before the rescan: no reset on the next tick.
    The hold stands and the bound's ladder runs at 10 min."""
    _, bridges, count = _below_floor_hold(monkeypatch, mesh, {"now": IDLE})
    monkeypatch.setattr(srv, "_offbus_hold_chips", set(), raising=False)
    _dead_arc_on_bus(monkeypatch)
    monkeypatch.setattr(galaxy, "TRAY_DOWN_RESCAN_SETTLE_SEC", 0)
    monkeypatch.setattr(galaxy, "_pci_rescan", lambda: _chip_comes_back(monkeypatch))
    await _gate_below_floor_pass(monkeypatch)
    assert count("below_floor_kept_rungs") + count("below_floor_kept_rungs_unverified") >= 1
    assert srv._offbus_hold_chips == set(), "a chip back on the bus still arms the one-shot"
    resets = _real_mesh_reset(monkeypatch)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert count("hold_deadline_forced_escalation") == 0 and resets == [], "a reset ran for a chip back on the bus"
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold lifted without a passing full check"
    await _assert_bound_ladder_runs(monkeypatch, count, verdict, resets)
    assert bridges == [], "the enumerated chip was bridge-reset (I89)"


@pytest.mark.asyncio
async def test_a_chip_back_while_a_tenant_waited_drops_the_one_shot(mesh, monkeypatch, verdict):
    """A tenant at the first tick keeps the tenant wait; chip 8 comes back meanwhile. On the first idle
    tick after the wait the one-shot finds no chip off the bus: it is dropped with no reset and no forced
    escalation, the hold stays, and the 10-min bound's ladder still runs."""
    scan = {"now": IDLE}
    _, bridges, count = _below_floor_hold(monkeypatch, mesh, scan)
    monkeypatch.setattr(srv, "_offbus_hold_chips", set(), raising=False)
    _dead_arc_on_bus(monkeypatch)
    await _gate_below_floor_pass(monkeypatch)
    assert srv._offbus_hold_chips == {DEAD}
    resets = _real_mesh_reset(monkeypatch)
    scan["now"] = TENANT
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert srv.device_hold_offbus_tenant_wait is True and srv.device_hold_offbus_escalated is False
    wait = srv._offbus_one_shot_sec()
    _chip_comes_back(monkeypatch)
    scan["now"] = IDLE
    await tick(wait + 1)
    assert count("hold_deadline_forced_escalation") == 0 and resets == [], "a reset ran for a chip back on the bus"
    assert count("offbus_one_shot_dropped") == 1
    assert srv._offbus_hold_chips == set() and srv.device_hold_offbus_escalated is False
    assert srv.fsm.state is not ServerState.HEALTHY, "the dropped one-shot lifted the hold"
    await _assert_bound_ladder_runs(monkeypatch, count, verdict, resets)
    assert bridges == []


@pytest.mark.asyncio
async def test_the_down_offbus_recheck_drops_a_returned_chip_and_keeps_the_hold(monkeypatch, tmp_path, clear_job_state):
    """Every chip is back on a DOWN off-bus hold: the idle re-check drops chip 6 from the one-shot's chips
    by the same read, and a full gate that does not pass keeps the hold."""
    _setup_down_offbus(monkeypatch, tmp_path)
    _fake_sysfs(monkeypatch, tmp_path / "fake")
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)
    monkeypatch.setattr(srv, "_offbus_hold_chips", {"6"}, raising=False)

    async def failing_gate(expected, log, *a, **k):
        return False, {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": "links short"}}

    monkeypatch.setattr(srv, "_full_check_gate", failing_gate)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda *a, **k: pytest.fail("released"))
    await srv._attempt_down_offbus_recheck()
    assert srv._offbus_hold_chips == set(), "a chip back on the bus still arms the one-shot"
    assert srv.fsm.state is ServerState.DOWN and srv.fsm.record.why == "off_bus"
