# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The gated off-bus hold (spec 04 I27, I33).

The I25 host-hang latch refuses every reset while a chip is off the bus. With no
``TT_DEVICE_MCP_POWER_CYCLE_CMD`` there is no I27 cycle to end that hold, so it is handed to an
operator; with one, a whole tray down goes straight to it. Without the latch the gate lets a partial
drop's mesh reset run, and that reset still owes the full health check, fabric included, before
anything releases the device.
"""

import time

import pytest

from tests import test_post_reset_gate as prg
from tests.conftest import fsm_dirty, patch_recovery
from tests.test_offbus_power_cycle import _REAL_GATE, _gated
from tests.test_post_reset_gate import _REAL_CLEAR_DIRTY, _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import post_reset
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import offbus_power_cycle as opc
from tt_device_mcp.health.recovery import pcie_guard

EXPECTED = 32
mesh = prg.mesh  # the post-reset gate's fixtures, shared
verdict = prg.verdict


def _hold_gate(monkeypatch, tmp_path, *, held_sec, manual="exit 1", off_bus=1, cmd=None, latched=True):
    """An off-bus hold seen by the real gate (not a stub), under a fresh I25 latch unless ``latched`` is
    False, no power cycle configured unless ``cmd`` names one."""
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=held_sec, cmd=cmd, manual=manual)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", _REAL_GATE)
    monkeypatch.setattr(pcie_guard, "aer_flooding", lambda ports=None: False)
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: off_bus)
    if latched:
        # Well inside its I25 ceiling, so expire_offbus_reset_latch keeps it.
        latch_rec = {"boot_id": "b", "since": hold_since, "latched_at_epoch": time.time()}
        monkeypatch.setattr(pcie_guard, "offbus_reset_latched", lambda: latch_rec)
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
async def test_a_latched_hold_is_handed_to_an_operator_once(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=60)
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    await _escalate()
    await _escalate()
    assert counters["resets"] == 0 and not any(seen)
    pages = [f for k, f in rec["events"] if k == "offbus_gated_hold_page"]
    assert len(pages) == 1, "one page per episode"
    msg = pages[0]["message"]
    assert "TT_DEVICE_MCP_POWER_CYCLE_CMD is not set" in msg
    assert "the hold stands until an operator recovers the host" in msg
    status = opc.hold_handoff_status(hold_since)
    assert status["owner"] == "operator" and status["act_by"] is None
    assert status["operator_seen"] == "none seen"
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    srv.device_hold_episode_since = hold_since
    assert srv._health_payload().get("operator_handoff", {}).get("owner") == "operator"
    opc.end_hold_handoff()
    assert opc.hold_handoff_status(hold_since) is None


@pytest.mark.asyncio
async def test_an_operator_at_work_is_named_on_the_handoff(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=1300, manual="exit 0")
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    await _escalate()
    await _escalate()
    assert counters["resets"] == 0 and not any(seen)
    assert "an operator recovery is in progress" in opc.hold_handoff_status(hold_since)["operator_seen"]


@pytest.mark.asyncio
async def test_a_reset_the_gate_refuses_releases_nothing(mesh, monkeypatch, tmp_path):
    """Under the latch the same real reset path is refused by the gate: no reset, no release."""
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


# ---- spec 04 I27: with a power cycle configured, a latched whole tray down goes straight to it; ----
# ---- without the latch a partial off-bus drop gets the mesh reset, released only on the full check ----

CYCLE = "/usr/local/sbin/host-power-cycle --now"


def _beats_off(monkeypatch, off):
    beats = {str(i): 100 for i in range(EXPECTED) if i not in set(off)}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: beats)


@pytest.mark.asyncio
async def test_a_whole_tray_down_goes_straight_to_the_power_cycle(monkeypatch, tmp_path, clear_job_state, galaxy_trays):
    """Chips 24-31 are all of one tray: no reset can bring a tray that lost power back, so the latched
    gate stays shut past the grace and the power cycle fires with no mesh reset in front of it."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE, off_bus=8)
    _beats_off(monkeypatch, range(24, 32))
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)
    await _escalate()
    await _escalate()
    assert len(rec["fires"]) == 1, "the power cycle fired"
    assert counters["resets"] == 0 and not any(seen), "no mesh reset before it"


@pytest.mark.asyncio
async def test_without_the_latch_a_partial_drop_gets_the_mesh_reset_at_once(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """One chip off, no latch, power cycle configured: the gate lets the ladder's mesh reset run on the
    first climb, with no power cycle in the same pass."""
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE, latched=False)
    _beats_off(monkeypatch, [17])
    counters = {"resets": 0}
    seen = _gate_seen_by_reset(monkeypatch, counters)  # the reset fails: the chip stays off
    assert await _escalate() is True
    assert counters["resets"] == 1 and seen == [True], "the mesh reset fired"
    assert rec["fires"] == [], "no power cycle in the same pass"


@pytest.mark.asyncio
async def test_a_partial_drop_the_mesh_reset_heals_never_gets_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE, latched=False)
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
    assert counters["resets"] == 1
    await _escalate()
    assert rec["fires"] == []


@pytest.mark.asyncio
async def test_the_partial_drops_mesh_reset_releases_only_after_a_passing_fabric_check(
    mesh, monkeypatch, tmp_path, verdict, galaxy_trays
):
    """Goal: the real reset path releases only on a verify carrying a healthy fabric verdict; on a 77 or
    no fabric verdict it stays held."""
    evidence, released = verdict
    hold_since, rec = _hold_gate(monkeypatch, tmp_path, held_sec=400, cmd=CYCLE, latched=False)
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
    assert await _escalate() is True
    assert len(fired) == 1, "the climb ran the mesh reset"
    _assert_settled(released)
    if not released:
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
