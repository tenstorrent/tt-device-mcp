# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The gated off-bus hold's bounded end with no power cycle configured (spec 04 I33).

``TT_DEVICE_MCP_HOST_RESET_GATE=hold`` refuses every reset while a chip is off the bus. With no
``TT_DEVICE_MCP_POWER_CYCLE_CMD`` there is no I27 cycle to end that hold, so it is handed to an
operator with a deadline; past it the broker lifts the gate for its own ladder, and every reset that
ladder fires still owes the full health check, fabric included, before anything releases the device.
"""

import time
from datetime import datetime, timedelta

import pytest

from tests import test_post_reset_gate as prg
from tests.conftest import fsm_dirty, patch_recovery
from tests.test_offbus_power_cycle import _REAL_GATE, _gated
from tests.test_post_reset_gate import _REAL_CLEAR_DIRTY, _assert_settled, _verify_returning
from tt_device_mcp import metrics
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import post_reset
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import offbus_power_cycle as opc
from tt_device_mcp.health.recovery import pcie_guard

EXPECTED = 32
mesh = prg.mesh  # the post-reset gate's fixtures, shared
verdict = prg.verdict


def _deadline_gauge() -> float:
    return metrics.REGISTRY.get_sample_value("tt_device_broker_offbus_operator_deadline_seconds")


def _hold_gate(monkeypatch, tmp_path, *, held_sec, handoff=None, manual="exit 1", off_bus=1, cmd=None):
    """An off-bus hold refused by a real ``hold`` gate (not a stub), no power cycle configured unless
    ``cmd`` names one."""
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=held_sec, cmd=cmd, manual=manual)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", _REAL_GATE)
    monkeypatch.setattr(pcie_guard, "aer_flooding", lambda ports=None: False)
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: off_bus)
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    if handoff is None:
        monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", raising=False)
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", handoff)
    return hold_since, rec


def _gate_seen_by_reset(monkeypatch, counters):
    """The ladder's mesh reset, stubbed to record what the real gate says inside it."""
    seen = []

    async def reset(indices, log):
        allowed, _ = pcie_guard.host_reset_gate(1)
        seen.append(allowed)
        if allowed:
            counters["resets"] += 1
        return allowed

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    return seen


async def _escalate(force=True):
    return await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=force)


@pytest.mark.asyncio
async def test_the_hold_is_handed_to_an_operator_once_with_a_deadline(monkeypatch, tmp_path, clear_job_state):
    metrics.reset_for_tests()
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=60)
    await _escalate()
    await _escalate()
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_gated_hold_page"], "one page per episode"
    msg = rec["alerts"][0]["message"]
    assert "TT_DEVICE_MCP_POWER_CYCLE_CMD is not set" in msg and "must recover the host by" in msg
    assert "full health check, fabric included" in msg
    deadline = datetime.fromisoformat(hold_since).timestamp() + opc.HOLD_HANDOFF_DEFAULT_SEC
    assert _deadline_gauge() == pytest.approx(deadline)
    status = opc.hold_handoff_status(hold_since)
    assert status["owner"] == "operator" and status["act_by_epoch"] == pytest.approx(deadline)
    assert status["operator_seen"] == "none seen"
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    srv.device_hold_episode_since = hold_since
    payload = srv._health_payload()
    assert payload.get("operator_handoff", {}).get("act_by_epoch") == pytest.approx(deadline)
    opc.end_hold_handoff()
    assert _deadline_gauge() == 0 and opc.hold_handoff_status(hold_since) is None


@pytest.mark.asyncio
async def test_before_the_deadline_the_gate_holds_and_no_reset_fires(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=600)
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    for _ in range(3):
        await _escalate()
    assert counters["resets"] == 0 and not any(seen)
    assert not opc.hold_handoff_due(hold_since)
    assert opc.hold_handoff_status(hold_since)["owner"] == "operator"


@pytest.mark.asyncio
async def test_past_the_deadline_the_ladder_runs_with_the_gate_lifted_for_that_pass_only(
    monkeypatch, tmp_path, clear_job_state
):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=1300)
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    await _escalate()  # hands off; the deadline (hold_since + 1200 s) already passed
    assert opc.hold_handoff_due(hold_since)
    assert await _escalate() is True
    assert counters["resets"] == 1 and seen[-1] is True, "the mesh reset fired with the gate lifted"
    assert opc.hold_handoff_status(hold_since)["owner"] == "broker"
    assert pcie_guard.host_reset_gate(1)[0] is False, "outside the pass the gate holds again"
    assert ("offbus_gated_hold", "gate_lifted") in [(k, f.get("phase")) for k, f in rec["events"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("latch", ["handoff-off", "hang-latch"])
async def test_an_operator_latch_keeps_the_hold_past_the_deadline(monkeypatch, tmp_path, clear_job_state, latch):
    hold_since, rec = _hold_gate(
        monkeypatch, tmp_path, held_sec=1300, handoff="off" if latch == "handoff-off" else None
    )
    if latch == "hang-latch":
        # A fresh latch: well inside its I25 ceiling, so expire_offbus_reset_latch keeps it.
        latch_rec = {"boot_id": "b", "since": hold_since, "latched_at_epoch": time.time()}
        monkeypatch.setattr(pcie_guard, "offbus_reset_latched", lambda: latch_rec)
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    for _ in range(2):
        await _escalate()
    assert counters["resets"] == 0 and not any(seen)
    assert not opc.hold_handoff_due(hold_since)
    status = opc.hold_handoff_status(hold_since)
    assert status["owner"] == "operator" and status["act_by"] is None
    assert "the hold stands until an operator recovers the host" in rec["alerts"][0]["message"]


@pytest.mark.asyncio
async def test_an_operator_at_work_keeps_the_gate_shut(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=1300, manual="exit 0")
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    await _escalate()
    await _escalate()
    assert counters["resets"] == 0 and not any(seen)
    assert "an operator recovery is in progress" in opc.hold_handoff_status(hold_since)["operator_seen"]


def test_the_watchdog_forces_one_pass_at_the_handoff_bound(monkeypatch, clear_job_state):
    fired = []
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 100000)
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", raising=False)
    monkeypatch.setattr(opc, "page_once", lambda *a, **k: None)
    since = datetime(2026, 10, 14, 20, 55, 0)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    opc.note_hold_handoff(since.isoformat(), 1, 32, "gated", lambda m: None)
    srv._check_hold_deadline(now=since + timedelta(seconds=1199))
    assert fired == []
    srv._check_hold_deadline(now=since + timedelta(seconds=1201))
    assert fired == [True]
    assert srv.device_hold_escalation_kind == "gated_hold_handoff_bound"
    srv._check_hold_deadline(now=since + timedelta(seconds=1300))
    assert fired == [True], "one forced pass per episode from the hand-off bound"


def test_the_handoff_knob(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", raising=False)
    assert opc.hold_handoff_sec() == 1200
    for bad in ("0", "-5", "soon"):
        monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", bad)
        assert opc.hold_handoff_sec() == 1200
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", "OFF")
    assert opc.hold_handoff_sec() is None


# ---- goal: no lifted reset releases the device before the full health check, fabric included ----


@pytest.mark.asyncio
async def test_the_lifted_mesh_reset_releases_only_after_a_passing_fabric_check(mesh, monkeypatch, tmp_path, verdict):
    """The real reset path under the lifted gate: the reset records the fabric debt, and only a
    verify that carries a healthy fabric verdict releases; a 77 or no fabric verdict stays held."""
    evidence, released = verdict
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=1300)
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")  # the real reset + verify
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    monkeypatch.setattr(post_reset, "health_event", lambda kind, **f: None)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    srv.device_hold_episode_since = hold_since
    fired = []

    async def mesh_reset(argv, log, *a, **k):
        fired.append(argv)
        FABRIC_DEBT.note_reset(" ".join(argv))
        return 0, ""

    monkeypatch.setattr(srv.galaxy_recovery.mechanism, "reset_with_quiesce", mesh_reset)
    _verify_returning(monkeypatch, evidence)
    await _escalate()  # the hand-off
    assert fired == []
    assert await _escalate() is True
    assert len(fired) == 1, "the lifted pass ran the mesh reset"
    _assert_settled(released)


@pytest.mark.asyncio
async def test_a_reset_the_gate_still_refuses_releases_nothing(mesh, monkeypatch, tmp_path):
    """Within the hand-off the same real reset path is refused by the gate: no reset, no release."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=60)
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    srv.device_hold_episode_since = hold_since
    fired = []

    async def mesh_reset(argv, log, *a, **k):
        fired.append(argv)
        return 0, ""

    monkeypatch.setattr(srv.galaxy_recovery.mechanism, "reset_with_quiesce", mesh_reset)
    _verify_returning(monkeypatch, {"snapshot": {"ok": True}, "fabric": {"ok": True}})
    await _escalate()
    await _escalate()
    assert fired == []
    assert srv.fsm.record is not None and srv.fsm.state is not ServerState.HEALTHY


# ---- spec 04 I27: with a power cycle configured, a whole tray down goes straight to it; a partial ----
# ---- off-bus drop gets the reset ladder (the mesh reset) once first, and the cycle only if that fails ----

CYCLE = "/usr/local/sbin/host-power-cycle --now"


def _beats_off(monkeypatch, off):
    beats = {str(i): 100 for i in range(EXPECTED) if i not in set(off)}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: beats)


@pytest.mark.asyncio
async def test_a_whole_tray_down_goes_straight_to_the_power_cycle(monkeypatch, tmp_path, clear_job_state, galaxy_trays):
    """Chips 24-31 are all of one tray: no reset can bring a tray that lost power back, so the gate stays
    shut past the grace and the power cycle fires with no mesh reset in front of it."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE, off_bus=8)
    _beats_off(monkeypatch, range(24, 32))
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    await _escalate()
    await _escalate()
    assert len(rec["fires"]) == 1, "the power cycle fired"
    assert counters["resets"] == 0 and not any(seen), "no mesh reset before it"
    assert ("offbus_gated_hold", "gate_lifted") not in [(k, f.get("phase")) for k, f in rec["events"]]
    assert opc._pending.get("partial_reset") is None, "no lifted pass was spent"


@pytest.mark.asyncio
async def test_a_partial_drop_gets_the_mesh_reset_first_and_the_power_cycle_only_if_it_fails(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """One chip off, gate ``hold``, power cycle configured: the hold went to the power cycle and the
    mesh reset never ran. Now the pass at the grace lifts the gate once and the
    mesh reset runs; it fails, the watchdog forces one more pass, and that one fires the power cycle
    with the gate shut again. Fails on base, which never lifts the gate with a power cycle set."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE)
    _beats_off(monkeypatch, [17])
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)  # the reset fails: the chip stays off
    await _escalate()  # the first climb, gate shut
    assert counters["resets"] == 0 and rec["fires"] == []
    assert opc.partial_reset_due(hold_since)
    assert await _escalate() is True
    assert counters["resets"] == 1 and seen[-1] is True, "the mesh reset fired with the gate lifted"
    assert rec["fires"] == [], "no power cycle in the same pass"
    assert pcie_guard.host_reset_gate(1)[0] is False, "outside the pass the gate holds again"
    assert not opc.partial_reset_due(hold_since), "one lifted pass per episode"
    assert opc.due_for_watchdog(hold_since), "the watchdog forces the next pass at once"
    opc.mark_spawned(hold_since)
    assert await _escalate() is True
    assert len(rec["fires"]) == 1 and counters["resets"] == 1, "then the power cycle, no second reset"
    assert not opc.due_for_watchdog(hold_since), "re-armed once only"


def _decline(monkeypatch, how):
    """Make the climb's own guards decline the next pass: an active reset scope, a readable tenant, or
    (``below_ceiling``) a non-forced pass while the hold is still under the 600 s ceiling."""
    rec = srv.galaxy_recovery
    if how == "scope":
        monkeypatch.setattr(rec.mechanism, "scope_active", lambda: True)
    elif how == "tenant":
        monkeypatch.setattr(rec, "_tenant_blocker", lambda scan, force=False, **k: {"pid": 4242, "user": "dev"})
        monkeypatch.setattr(rec, "_describe_blocker", lambda blocker: "a tenant holds the device")
    return how != "below_ceiling"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["scope", "tenant", "below_ceiling"])
async def test_a_declined_lifted_pass_is_not_spent_and_the_mesh_reset_still_runs_first(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays, how
):
    """The one lifted pass is spent only when its climb reaches the rungs. A pass the climb's guards
    decline (a reset cycling in its own scope, a tenant, a non-forced pass below the ceiling) runs no
    reset, stays due and re-arms nothing; the next pass that does run gets the mesh reset, and only
    after it fails does the power cycle fire. Fails on the base, which spent the pass in the lift."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE)
    _beats_off(monkeypatch, [17])
    counters = {"resets": 0}
    _gate_seen_by_reset(monkeypatch, counters)  # the reset fails: the chip stays off
    await _escalate()  # the first climb, gate shut
    assert opc.partial_reset_due(hold_since)
    opc.mark_spawned(hold_since)
    with monkeypatch.context() as m:
        force = _decline(m, how)
        assert await _escalate(force=force) is False
    assert counters["resets"] == 0 and rec["fires"] == [], "a declined pass runs no reset and no power cycle"
    assert opc.partial_reset_due(hold_since), "a declined climb leaves the lifted pass due"
    assert not opc._pending.get("partial_rearmed"), "and re-arms nothing"
    assert await _escalate() is True
    assert counters["resets"] == 1 and rec["fires"] == [], "the next pass runs the mesh reset"
    assert not opc.partial_reset_due(hold_since)
    opc.mark_spawned(hold_since)
    assert await _escalate() is True
    assert len(rec["fires"]) == 1 and counters["resets"] == 1, "then the power cycle"


@pytest.mark.asyncio
async def test_a_partial_drop_the_mesh_reset_heals_never_gets_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE)
    _beats_off(monkeypatch, [17])
    counters = {"resets": 0}

    async def reset(indices, log):
        if not pcie_guard.host_reset_gate(1)[0]:
            return False
        counters["resets"] += 1
        _beats_off(monkeypatch, [])
        return True

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    await _escalate()
    await _escalate()
    assert counters["resets"] == 1
    await _escalate()
    assert rec["fires"] == []


@pytest.mark.asyncio
async def test_a_partial_drop_with_no_tray_map_gets_the_mesh_reset_first(monkeypatch, tmp_path, clear_job_state):
    """No tray map: the broker cannot tell a whole tray from a partial drop, so no rung is skipped."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE, off_bus=8)
    _beats_off(monkeypatch, range(24, 32))
    counters = {"resets": 0}
    _gate_seen_by_reset(monkeypatch, counters)
    await _escalate()
    await _escalate()
    assert counters["resets"] == 1 and rec["fires"] == []


@pytest.mark.asyncio
async def test_before_the_grace_a_partial_drop_gets_no_lifted_reset(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=60, cmd=CYCLE)
    _beats_off(monkeypatch, [17])
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    for _ in range(3):
        await _escalate()
    assert counters["resets"] == 0 and not any(seen) and rec["fires"] == []


@pytest.mark.asyncio
async def test_the_partial_drops_mesh_reset_releases_only_after_a_passing_fabric_check(
    mesh, monkeypatch, tmp_path, verdict, galaxy_trays
):
    """Goal: the real reset path in the lifted pass releases only on a verify carrying a healthy fabric
    verdict; on a 77 or no fabric verdict it stays held and the next pass fires the power cycle."""
    evidence, released = verdict
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE)
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    monkeypatch.setattr(post_reset, "health_event", lambda kind, **f: None)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    srv.device_hold_episode_since = hold_since
    _beats_off(monkeypatch, [17])
    fired = []

    async def mesh_reset(argv, log, *a, **k):
        fired.append(argv)
        FABRIC_DEBT.note_reset(" ".join(argv))
        return 0, ""

    monkeypatch.setattr(srv.galaxy_recovery.mechanism, "reset_with_quiesce", mesh_reset)
    _verify_returning(monkeypatch, evidence)
    await _escalate()
    assert fired == []
    assert await _escalate() is True
    assert len(fired) == 1, "the lifted pass ran the mesh reset"
    _assert_settled(released)
    if not released:
        opc.mark_spawned(hold_since)
        await _escalate()
        assert len(rec["fires"]) == 1, "a failed post-reset check goes on to the power cycle"
        assert srv.fsm.record is not None and srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_the_power_cycle_releases_nothing(mesh, monkeypatch, tmp_path, galaxy_trays):
    """The power cycle leaves the device held: only the next boot's startup gate, which waits for the
    hugepage pool and runs the full check, fabric included, releases it."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE, off_bus=8)
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    fsm_dirty(srv, "gate/post-job: 8/32 off-bus", why="job_killed")
    srv.device_hold_episode_since = hold_since
    _beats_off(monkeypatch, range(24, 32))
    _verify_returning(monkeypatch, {"snapshot": {"ok": True}, "fabric": {"ok": True}})
    await _escalate()
    await _escalate()
    assert len(rec["fires"]) == 1
    assert srv.fsm.record is not None and srv.fsm.state is not ServerState.HEALTHY
