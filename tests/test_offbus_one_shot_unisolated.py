# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I96: a below-floor off-bus hold arms the off-bus one-shot for every chip the gate counted off
the bus, isolated or not. A chip that enumerates but whose heartbeat read fails (not ENODATA) is counted
off the bus yet never isolated, and a broker restart empties ``isolated_chips``; either way the hold used
to wait for the 10-min bound instead of one sampler tick. The chip is still never SBR'd (I89)."""

import errno
from datetime import datetime, timedelta

import pytest

from tests import test_post_reset_gate as prg
from tests.test_offbus_one_shot_idle import IDLE, _below_floor_hold
from tests.test_post_reset_gate import _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import heartbeat, pci
from tt_device_mcp.health.post_reset import FABRIC_DEBT

mesh = prg.mesh  # the post-reset gate's fixtures, shared
verdict = prg.verdict

DEAD = "8"


def _dead_arc_on_bus(monkeypatch):
    """Chip 8 enumerates (its node is there) but its heartbeat read fails with EIO, so the gate counts
    1/32 off the bus; nothing is isolated and no removal is recorded."""
    (pci.SYSFS_CLASS_DIR / f"tenstorrent!{DEAD}").mkdir(exist_ok=True)
    beats = {str(i): 100 + i for i in range(32) if str(i) != DEAD}
    monkeypatch.setattr(srv, "read_heartbeats", lambda *a, **k: dict(beats))

    def read_beat(node):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(heartbeat, "_read_beat", read_beat)
    srv.isolated_chips = set()
    srv.device_pci_map = {}


async def _gate_below_floor_pass(monkeypatch):
    """The gate's own pass over that mesh: nothing heals it, so it holds BELOW_FLOOR."""

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": f"chip {DEAD} heartbeat unreadable"}}

    prg.patch_recovery(monkeypatch, "_verify_device", unhealthy)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)


def _real_mesh_reset(monkeypatch):
    """The ladder's own mesh reset and its full check run for real; only the reset command is stubbed."""
    resets = []

    async def reset_cmd(argv, log, *a, **k):
        resets.append("mesh reset")
        return 0, ""

    for rec in (srv.galaxy_recovery, srv.per_target_recovery):
        monkeypatch.delattr(rec, "_reset_and_verify_device", raising=False)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", reset_cmd)
    return resets


async def tick(sec):
    """One sampler pass ``sec`` into the episode the gate's hold opened."""
    srv._check_hold_deadline(now=datetime.fromisoformat(srv.device_hold_episode_since) + timedelta(seconds=sec))
    if srv._forced_escalation_task is not None:
        await srv._forced_escalation_task


def _assert_one_shot_ran(count, calls, released):
    assert count("hold_deadline_forced_escalation") == 1, "the unisolated off-bus hold waited for the 10-min bound"
    assert srv.device_hold_escalation_kind == "offbus_one_shot"
    assert srv.device_hold_offbus_escalated is True
    assert calls and all(calls), "the one-shot's post-reset verify skipped the fabric pass"
    assert srv.isolated_chips == set(), "the enumerated chip was isolated (I89: it is never SBR'd)"
    if released:
        _assert_settled(released)
    else:
        assert srv.fsm.state is not ServerState.HEALTHY and FABRIC_DEBT.owed


@pytest.mark.asyncio
async def test_an_enumerated_dead_arc_chip_arms_the_one_shot_on_its_short_clock(mesh, monkeypatch, verdict):
    """Chip 8 enumerates with a dead ARC and is not isolated: the gate counts it off the bus, holds below
    the floor, and the next sampler tick runs the one-shot. Its reset frees the device only through the
    full post-reset check, fabric included."""
    evidence, released = verdict
    _, bridges, count = _below_floor_hold(monkeypatch, mesh, {"now": IDLE})
    monkeypatch.setattr(srv, "_offbus_hold_chips", set())
    _dead_arc_on_bus(monkeypatch)
    await _gate_below_floor_pass(monkeypatch)
    assert count("below_floor_kept_rungs") == 1, "the gate did not hold the mesh below the floor"
    assert srv._offbus_hold_chips == {DEAD} and srv.isolated_chips == set()
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    resets = _real_mesh_reset(monkeypatch)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert resets == ["mesh reset"], "the one-shot did not run the below-floor mesh reset"
    _assert_one_shot_ran(count, calls, released)
    assert bridges == [], "the enumerated chip was bridge-reset (I89)"
