# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I23: on a tenant-free box the off-bus one-shot fires on the second sampler tick of the hold,
not after the tenant wait (``_offbus_hold_ceiling_sec``). A live tenant keeps that wait."""

from datetime import datetime, timedelta

import pytest

from tests import test_post_reset_gate as prg
from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold
from tests.test_post_reset_gate import _REAL_CLEAR_DIRTY, _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.post_reset import FABRIC_DEBT

mesh = prg.mesh  # the post-reset gate's fixtures, shared
verdict = prg.verdict

IDLE = HolderScan(holders=[], complete=True)
TENANT = HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True)


def _below_floor_hold(monkeypatch, mesh, scan):
    """One chip of 32 off the bus and isolated, held below the galaxy-reset floor (the bridge rung found
    nothing to reset), the hold opened now. Returns (tick, bridges, count)."""
    _arm_offbus_stuck_hold(monkeypatch, isolated={"8"})
    patch_health_event(monkeypatch, lambda kind, **f: mesh.append((kind, f)))
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    since = datetime.now()
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_offbus_tenant_wait", False)
    monkeypatch.setattr(srv, "device_hold_offbus_idle_early", False)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: scan["now"])
    bridges = []

    async def bridge(log):
        bridges.append("8")
        FABRIC_DEBT.note_reset("bridge reset of chip 8")
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", bridge)

    async def tick(sec):
        srv._check_hold_deadline(now=since + timedelta(seconds=sec))
        if srv._forced_escalation_task is not None:
            await srv._forced_escalation_task

    return tick, bridges, lambda kind: sum(k == kind for k, _ in mesh)


@pytest.mark.asyncio
async def test_an_idle_below_floor_offbus_hold_escalates_within_two_sampler_ticks(mesh, monkeypatch, verdict):
    """Replay: one chip drops, the gate holds it below the reset floor, no tenant. The first escalation
    starts within two sampler ticks (it waited the full tenant wait before), and its reset releases only
    through the full post-reset check, fabric included (goal: no release over an unproven fabric)."""
    evidence, released = verdict
    scan = {"now": IDLE}
    tick, bridges, count = _below_floor_hold(monkeypatch, mesh, scan)
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    for sec in range(int(srv.SAMPLE_INTERVAL_SEC), 21, int(srv.SAMPLE_INTERVAL_SEC)):
        await tick(sec)
        if bridges:
            break
    assert bridges == ["8"], "an idle off-bus hold waited past two sampler ticks for its first escalation"
    assert count("hold_deadline_forced_escalation") == 1 and srv.device_hold_offbus_escalated is True
    assert calls and all(calls), "the early run's post-reset verify skipped the fabric pass"
    if released:
        _assert_settled(released)
    else:
        assert srv.fsm.state is not ServerState.HEALTHY and FABRIC_DEBT.owed == "bridge reset of chip 8"
        assert count("post_reset_release_refused") == 1


@pytest.mark.asyncio
async def test_a_live_tenant_keeps_the_offbus_tenant_wait(mesh, monkeypatch):
    """A tenant on the device at the early tick: the run gives the one-shot back quietly (no device op,
    no forced-escalation event, no rung) and the hold keeps the tenant wait. At that wait the existing
    stand-down (spec 04 I7) applies; the reset runs on the first idle tick after it."""
    scan = {"now": TENANT}
    tick, bridges, count = _below_floor_hold(monkeypatch, mesh, scan)
    _verify_returning(monkeypatch, {"snapshot": {"ok": True}, "fabric": {"ok": True}})
    wait = srv._offbus_hold_ceiling_sec()
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert bridges == [] and count("hold_deadline_forced_escalation") == 0 and count("device_op_begin") == 0
    assert srv.device_hold_offbus_tenant_wait is True and srv.device_hold_offbus_escalated is False
    assert srv._offbus_one_shot_sec() == wait
    for sec in range(20, int(wait), 10):
        await tick(sec)
    assert bridges == [] and count("hold_deadline_forced_escalation") == 0, "fired under a tenant before its wait"
    await tick(wait + 1)
    assert bridges == [] and count("hold_deadline_forced_escalation") == 1, "the tenant wait's run must stand down"
    scan["now"] = IDLE
    await tick(wait + 11)
    assert bridges == ["8"], "the first idle tick after the tenant wait must run the one-shot"


@pytest.mark.asyncio
async def test_a_pending_power_cycle_request_still_suppresses_the_early_one_shot(mesh, monkeypatch):
    """Whole-tray drop: the gate already asked for a power cycle. The early one-shot must not start a
    reset under it (the whole-tray path is unchanged)."""
    scan = {"now": IDLE}
    tick, bridges, count = _below_floor_hold(monkeypatch, mesh, scan)
    monkeypatch.setattr(srv, "_host_action_blocks_escalation", lambda: True)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    await tick(2 * srv.SAMPLE_INTERVAL_SEC)
    assert bridges == [] and count("hold_deadline_forced_escalation") == 0


@pytest.mark.asyncio
async def test_a_failed_early_one_shot_keeps_the_hold_and_reaches_the_power_cycle_at_the_ceiling(mesh, monkeypatch):
    """The early one-shot changes when the first rung runs, not the ladder after it: a reset that fails
    keeps the hold (no release, no host rung on the short clock), and the 10-min ceiling window still
    runs the full ladder and then requests the power cycle."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, isolated={"8"}, sbr_recovers=False, sbr_verify_healthy=False, reset_result=False
    )
    patch_health_event(monkeypatch, lambda kind, **f: mesh.append((kind, f)))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    since = datetime.now()
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_offbus_tenant_wait", False)
    monkeypatch.setattr(srv, "device_hold_offbus_idle_early", False)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)

    async def tick(sec):
        # The real clock reads the hold's age from datetime.now(); move the hold start, not the clock.
        monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=sec)).isoformat())
        srv._check_hold_deadline(now=datetime.now())
        if srv._forced_escalation_task is not None:
            await srv._forced_escalation_task
            srv._forced_escalation_task = None

    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert counters["sbr"] == 1, "the early one-shot did not run the bridge rung"
    assert counters["dirty_cleared"] == 0 and srv.fsm.state is not ServerState.HEALTHY, "a failed reset released"
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0, "a host rung ran on the short clock"
    for sec in range(20, int(srv._stuck_hold_ceiling_sec()), 60):
        await tick(sec)
    assert counters["power_cycles"] == 0, "a host rung ran before the hold had stood the full ladder"
    await tick(srv._stuck_hold_ceiling_sec() + 1)
    assert counters["resets"] >= 1, "the ceiling window did not run the galaxy reset rung"
    assert counters["power_cycles"] == 1, "the ceiling window did not climb to the power-cycle request"
    assert counters["dirty_cleared"] == 0, "the hold released without a passing post-reset check"
