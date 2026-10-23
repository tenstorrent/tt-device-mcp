# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I78 at the gate: the pre-job, post-job and kill-reset gates lift a hold only through the
release funnel, so only on a full check, fabric included, that has just passed. A pass that finds a
port of the cabling missing (I71) or a funnel that refuses keeps the hold, its episode and its clock.

Pre-job, a dirty hold with no fresh pass on record runs the full check itself (once the 1G hugepages
exist): without it the funnel refuses the gate's release on every job and nothing runs a fabric pass
until the 10-min bound (spec 04 I76). Runs the real probe pass on the eth-timeout tests' mesh with
only the leaf probes stubbed. Device-free."""

import pytest

from tests.conftest import fsm_dirty
from tests.test_eth_timeout_reset import ADVANCING, mesh  # noqa: F401 (fixture)
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import fabric
from tt_device_mcp.health.post_reset import FABRIC_DEBT

VERDICTS = ["pass", "fail", "missing"]


@pytest.fixture
def held(mesh, monkeypatch):  # noqa: F811
    """A dirty job_killed hold on a 32-chip mesh with a fabric check configured, the hugepage pool
    ready and no passing fabric pass on record. ``mesh["verdict"]`` picks what the fabric pass finds."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    mesh["eth"] = ADVANCING
    mesh["verdict"] = "pass"
    mon = srv.health_monitor

    async def fabric_pass():
        mesh["fabric"].append(len(mesh["resets"]))
        v = mesh["verdict"]
        if v == "pass":
            mon.last_fabric_ok = True
            return True, "all links healthy; dispatch OK"
        mon.last_fabric_ok = False
        if v == "missing":
            return False, f"{fabric.LINKS_MISSING}: 1 port(s) missing vs the cabling descriptor"
        return False, "fabric failed: link 3-4 moved no traffic"

    monkeypatch.setattr(mon, "verify_fabric_health", fabric_pass)
    FABRIC_DEBT.note_fabric(None)
    fsm_dirty(srv, "job 7 ended exit 1", why="job_killed")
    return mesh


# ---------------------------------------------------------------- F1: the pre-job gate


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_a_dirty_hold_with_no_fresh_pass_runs_the_full_check_pre_job(held, verdict):
    """Without the full check here the funnel refuses the pre-job release on every job and the hold
    sits until the 10-min bound. With it the gate runs one fabric pass and lifts only on a pass."""
    held["verdict"] = verdict
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)
    assert held["fabric"][:1] == [0], "the full check ran in the pre-job gate, before any reset"
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")
    if verdict != "pass":
        assert srv._device_unavailable_for_tenant(), "a failed or short-link check never admits the job"


@pytest.mark.asyncio
async def test_a_dirty_hold_with_a_fresh_pass_pays_no_fabric_pass_pre_job(held):
    """A passing full check newer than the fault is already on record: the pre-job gate adds no
    fabric pass (spec 03 I12). Only a hold the funnel would refuse pays for one."""
    FABRIC_DEBT.note_fabric(True)
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)
    assert held["fabric"] == []


@pytest.mark.asyncio
async def test_a_healthy_mesh_pays_no_fabric_pass_pre_job(held):
    srv.fsm.on_readings(srv._healthy_reading())
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)
    assert held["fabric"] == [], "spec 03 I12: nothing slow before a job on a healthy mesh"


@pytest.mark.asyncio
async def test_the_pre_job_full_check_waits_for_hugepages(held, monkeypatch):
    """Goal 8: no full check before the 1G pool exists (it could only 77). The hold stays; the 10-min
    bound's own check waits the same way and is bounded by its window (spec 04 I76)."""
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: (0, 32))
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)
    assert held["fabric"] == []
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stays until a full check can run"


# ---------------------------------------------------------------- F7: each gate lifts only via the funnel


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["pre-job", "post-job", "kill-reset"])
async def test_a_gate_whose_check_passed_still_lifts_only_through_the_funnel(held, monkeypatch, gate):
    """The check passes but the funnel finds no fresh pass on the full cabling: the gate keeps the
    hold. A gate path that released around the funnel fails here."""
    if gate == "kill-reset":
        srv._owe_kill_reset("job 7 timeout")
    refused = []
    monkeypatch.setattr(srv, "_hold_lift_unproven", lambda: "the last passing fabric pass is 400s old")
    monkeypatch.setattr(srv, "_journal_fsm_transition", lambda why, verified=False: refused.append(why))
    phase = "pre-job" if gate == "pre-job" else "post-job"
    await srv._device_health_gate(None, phase=phase, run_fabric=False)
    assert srv.fsm.state is not ServerState.HEALTHY
    assert "hold_lift_refused" in held["events"]
    assert not refused, "no release was journaled"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_the_post_job_gate_lifts_a_dirty_hold_only_on_a_passing_full_check(held, verdict):
    held["verdict"] = verdict
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert held["fabric"], "a dirty post-job gate runs the full check"
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_the_kill_reset_gate_lifts_only_on_a_passing_full_check_after_the_reset(held, verdict):
    """Spec 03 I69: one reset, then the full check; a check after it that fails or finds a port
    missing keeps the hold (and climbs), never releases."""
    held["verdict"] = verdict
    srv._owe_kill_reset("job 7 timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert held["resets"][:1] == [["tt-smi", "-glx_reset"]]
    assert held["fabric"] and min(held["fabric"]) >= 1, "the full check ran after the reset"
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")
