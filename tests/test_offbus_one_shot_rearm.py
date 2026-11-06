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
from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_bridge_reset_arc_dead import _bridge_rung
from tests.test_device_safety import _arm_offbus_stuck_hold
from tests.test_offbus_one_shot_idle import IDLE, _below_floor_hold
from tests.test_post_reset_gate import _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.recovery import galaxy

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


def _stuck(monkeypatch, mesh, sbr_recovers=False, sbr_verify_healthy=False):
    """Chip 8 off the bus and isolated, every rung mocked to fail unless told otherwise, the power cycle armed.
    Returns (counters, tick)."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch,
        isolated={"8"},
        sbr_recovers=sbr_recovers,
        sbr_verify_healthy=sbr_verify_healthy,
        reset_result=False,
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

    return counters, tick


@pytest.mark.asyncio
async def test_bound_ladder_unchanged(mesh, monkeypatch):
    """A re-armed one-shot that fails keeps the hold; a set lost inside the margin before the bound gets no
    one-shot; the 10-min bound still runs the full ladder and requests the power cycle; nothing releases."""
    counters, tick = _stuck(monkeypatch, mesh)
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


def _record_escalate(monkeypatch):
    """Record each forced run's rearm_deadline and the bridge cutoff it sets, around the real escalate."""
    runs = []
    real = srv.galaxy_recovery.escalate

    async def escalate(*a, rearm_deadline=None, **kw):
        runs.append({"deadline": rearm_deadline, "now": time.monotonic()})
        return await real(*a, rearm_deadline=rearm_deadline, **kw)

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    return runs


@pytest.mark.asyncio
async def test_rearm_inside_margin_runs_no_mesh_reset(mesh, monkeypatch):
    """A set lost just before the margin with a failing bridge rung: its re-armed one-shot runs the bridge rung
    only, never the mesh reset or a host rung, is bounded by the 10-min mark, and the bound's ladder still runs
    the mesh reset and requests the power cycle."""
    counters, tick = _stuck(monkeypatch, mesh)
    runs = _record_escalate(monkeypatch)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert counters["sbr"] == 1 and runs[0]["deadline"] is None, "the first one-shot is not a re-arm"
    resets = counters["resets"]
    late = int(srv._hold_bound_sec() - srv.OFFBUS_ONE_SHOT_REARM_MARGIN_SEC) - 5
    srv.isolated_chips.add("20")
    await tick(late)
    assert _rearmed(mesh) == [["20"]] and counters["sbr"] == 2
    left = runs[-1]["deadline"] - runs[-1]["now"]
    assert abs(left - (srv._hold_bound_sec() - late)) < 2, "the re-armed run is not bounded by the 10-min mark"
    assert counters["resets"] == resets, "the re-armed one-shot ran the mesh reset"
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0, "the re-armed one-shot climbed"
    assert [k for k, _ in mesh if k == "offbus_rearm_mesh_reset_left_to_bound"]
    assert srv.galaxy_recovery.bridge_reset_cutoff is None, "the cutoff outlived the run"
    await tick(srv._stuck_hold_ceiling_sec() + 1)
    assert runs[-1]["deadline"] is None, "the bound's ladder ran as a re-arm"
    assert counters["resets"] > resets, "the bound did not run the mesh reset"
    assert counters["power_cycles"] == 1, "the bound did not request the power cycle"
    assert counters["dirty_cleared"] == 0


def test_rearm_margin_from_bridge_call_bound():
    """The margin is two bridge calls with their settle, from named constants."""
    call = recovery_pkg.bridge_reset_call_timeout() + recovery_pkg.BRIDGE_RESET_SETTLE_SEC
    assert galaxy.offbus_rearm_bridge_call_sec() == call
    assert srv.OFFBUS_ONE_SHOT_REARM_MARGIN_SEC == 2 * call


@pytest.mark.asyncio
async def test_bridge_cutoff_withholds_calls(monkeypatch):
    """Past the cutoff no bridge reset call starts; a cutoff passed during a call stops its retries."""
    calls = []
    _bridge_rung(monkeypatch, "13", ["12345"])

    def bridge_reset(i, bdf):
        calls.append(i)
        return False

    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", bridge_reset)
    gr = srv.galaxy_recovery
    monkeypatch.setattr(gr, "bridge_reset_cutoff", time.monotonic() - 1)
    assert await gr._recover_isolated_chips(lambda m: None) is False
    assert calls == [] and gr.last_bridge_reset_reasons["13"] == {"reason": "past_cutoff"}
    assert srv.isolated_chips == {"13"}

    def bridge_reset_then_cutoff(i, bdf):
        calls.append(i)
        gr.bridge_reset_cutoff = time.monotonic() - 1
        return False

    monkeypatch.setattr(gr, "bridge_reset_cutoff", time.monotonic() + 600)
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", bridge_reset_then_cutoff)
    assert await gr._recover_isolated_chips(lambda m: None) is False
    assert calls == ["13"], "a retry started past the cutoff"
    assert srv.isolated_chips == {"13"}


@pytest.mark.asyncio
async def test_rearm_check_that_does_not_fit_keeps_the_hold(mesh, monkeypatch):
    """The re-armed bridge rung brings the chips back, but its full check with fabric would not end before the
    bound: no check runs, nothing releases, and the bound's ladder takes the hold."""
    counters, tick = _stuck(monkeypatch, mesh)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    verifies = []

    async def recover(log):
        counters["sbr"] += 1
        return True

    async def verify(expected, log, run_fabric=True, **_):
        verifies.append(run_fabric)
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_recover_isolated_chips", recover)
    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(galaxy, "OFFBUS_REARM_VERIFY_SEC", srv._hold_bound_sec())
    srv.isolated_chips.add("20")
    await tick(172)
    assert _rearmed(mesh) == [["20"]] and counters["sbr"] == 2
    assert verifies == [], "a full check ran that could not end before the bound"
    assert [k for k, _ in mesh if k == "offbus_rearm_check_left_to_bound"]
    assert counters["dirty_cleared"] == 0 and srv.fsm.state is not ServerState.HEALTHY, "released unchecked"
    monkeypatch.setattr(galaxy, "OFFBUS_REARM_VERIFY_SEC", 0)
    srv.isolated_chips.add("21")
    await tick(232)
    assert verifies == [True], "a check that fits did not run with its fabric pass"
