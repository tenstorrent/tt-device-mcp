# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I110: a chip set lost after the hold's off-bus one-shot ran gets its own one-shot, so it heals on
the cheap rung in seconds instead of waiting for the 10-min bound. At most OFFBUS_ONE_SHOT_REARM_MAX re-arms
per hold, none inside OFFBUS_ONE_SHOT_REARM_MARGIN_SEC of the bound; a set already shot does not fire again.
The bound's full ladder, its power-cycle request and the fabric-gated release are unchanged."""

import time
from datetime import datetime, timedelta

import pytest

from tests import test_post_reset_gate as prg
from tests.conftest import fsm_dirty, patch_health_event
from tests.test_device_safety import _arm_offbus_stuck_hold
from tests.test_offbus_one_shot_idle import IDLE, _below_floor_hold
from tests.test_post_reset_gate import _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState

mesh = prg.mesh  # the post-reset gate's fixtures, shared
verdict = prg.verdict

UNPROVEN = prg.UNPROVEN["fabric-77"]  # every check fails to prove the fabric: the hold stays


def _held(monkeypatch, mesh):
    """Chip 8 off the bus and isolated, idle, every post-reset check unproven. Returns (tick, bridges, count)."""
    tick, bridges, count = _below_floor_hold(monkeypatch, mesh, {"now": IDLE})
    _verify_returning(monkeypatch, UNPROVEN)
    return tick, bridges, count


def _rearmed(mesh):
    return [f["chips"] for k, f in mesh if k == "offbus_one_shot_rearmed"]


@pytest.mark.asyncio
async def test_offbus_rearm_second_set(mesh, monkeypatch):
    """Replay: the one-shot ran for chip 8 and failed; chip 20 drops 3 min into the hold. The next tick runs a
    one-shot for it, not the 10-min bound."""
    tick, bridges, count = _held(monkeypatch, mesh)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert count("hold_deadline_forced_escalation") == 1 and srv.device_hold_offbus_escalated is True
    assert srv.fsm.state is not ServerState.HEALTHY, "an unproven check released"
    srv.isolated_chips.add("20")
    await tick(172)
    assert count("hold_deadline_forced_escalation") == 2, "a set lost after the one-shot waited for the bound"
    assert _rearmed(mesh) == [["20"]] and srv.device_hold_offbus_rearms == 1
    assert len(bridges) == 2, "the re-armed one-shot did not run the bridge rung"
    await tick(182)
    assert count("hold_deadline_forced_escalation") == 2, "a set already shot fired again"


@pytest.mark.asyncio
async def test_offbus_rearm_cap(mesh, monkeypatch):
    """A mesh that keeps shedding sets: the first OFFBUS_ONE_SHOT_REARM_MAX new sets each get a one-shot, the
    next waits for the 10-min bound."""
    tick, bridges, count = _held(monkeypatch, mesh)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    new = [str(20 + i) for i in range(srv.OFFBUS_ONE_SHOT_REARM_MAX + 1)]
    for i, chip in enumerate(new):
        srv.isolated_chips.add(chip)
        await tick(60 + 60 * i)
    cap = srv.OFFBUS_ONE_SHOT_REARM_MAX
    assert _rearmed(mesh) == [[c] for c in new[:cap]], "the re-arms did not cover exactly the first sets"
    assert count("hold_deadline_forced_escalation") == 1 + cap and srv.device_hold_offbus_rearms == cap
    for sec in range(300, int(srv._hold_bound_sec()), 10):
        await tick(sec)
    assert count("hold_deadline_forced_escalation") == 1 + cap, "a re-arm ran past the cap"


@pytest.mark.asyncio
async def test_offbus_rearm_same_set_no_repeat(mesh, monkeypatch):
    """The same chip stays off the bus after its one-shot: no second one-shot before the bound."""
    tick, bridges, count = _held(monkeypatch, mesh)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    for sec in range(20, int(srv._hold_bound_sec()), 10):
        await tick(sec)
    assert count("hold_deadline_forced_escalation") == 1 and bridges == ["8"], "the spent set fired again"
    assert _rearmed(mesh) == [] and srv.device_hold_offbus_rearms == 0


@pytest.mark.asyncio
async def test_bound_ladder_unchanged(mesh, monkeypatch):
    """A re-armed one-shot that fails keeps the hold; a set lost inside the margin before the bound gets no
    one-shot; the 10-min bound still runs the full ladder and requests the power cycle; nothing releases."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, isolated={"8"}, sbr_recovers=False, sbr_verify_healthy=False, reset_result=False
    )
    patch_health_event(monkeypatch, lambda kind, **f: mesh.append((kind, f)))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.now().isoformat())
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_offbus_tenant_wait", False)
    monkeypatch.setattr(srv, "device_hold_offbus_idle_early", False)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    rig_cycle = srv._auto_power_cycle_host

    async def cycle_and_mark(log, reason):
        await rig_cycle(log, reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle_and_mark)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)

    async def tick(sec):
        monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=sec)).isoformat())
        srv._check_hold_deadline(now=datetime.now())
        if srv._forced_escalation_task is not None:
            await srv._forced_escalation_task
            srv._forced_escalation_task = None

    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert counters["sbr"] == 1, "the one-shot did not run the bridge rung"
    srv.isolated_chips.add("20")
    await tick(172)
    assert counters["sbr"] == 2 and _rearmed(mesh) == [["20"]], "the new set got no one-shot"
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0, "a host rung ran on the re-armed clock"
    late = srv._hold_bound_sec() - srv.OFFBUS_ONE_SHOT_REARM_MARGIN_SEC
    srv.isolated_chips.add("21")
    for sec in range(int(late), int(srv._stuck_hold_ceiling_sec()), 10):
        await tick(sec)
    assert counters["sbr"] == 2 and len(_rearmed(mesh)) == 1, "a set lost near the bound re-armed the one-shot"
    assert counters["power_cycles"] == 0, "a host rung ran before the hold had stood the full ladder"
    await tick(srv._stuck_hold_ceiling_sec() + 1)
    assert counters["resets"] >= 1, "the bound did not run the galaxy reset rung"
    assert counters["power_cycles"] == 1, "the bound did not climb to the power-cycle request"
    assert counters["dirty_cleared"] == 0, "the hold released without a passing post-reset check"


@pytest.mark.asyncio
async def test_release_needs_fabric_pass(mesh, monkeypatch, verdict):
    """The re-armed one-shot frees the device only through a full check whose fabric pass just passed."""
    evidence, released = verdict
    tick, bridges, count = _held(monkeypatch, mesh)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert srv.fsm.state is not ServerState.HEALTHY
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    srv.isolated_chips.add("20")
    await tick(172)
    assert _rearmed(mesh) == [["20"]]
    assert calls and all(calls), "the re-armed run's post-reset verify skipped the fabric pass"
    if released:
        _assert_settled(released)
    else:
        assert srv.fsm.state is not ServerState.HEALTHY, "released over an unproven fabric"
        assert count("post_reset_release_refused") >= 1
