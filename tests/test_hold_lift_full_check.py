# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I78: no hold lifts unless the full health check, fabric included, has just passed.

Every lift path is driven with a stubbed check that passes, fails, or finds a link the cabling expects
missing (spec 03 I71: fewer links than the cabling expects is a FAILED check). Only the pass lifts; the
other two keep the hold, its episode and its clock as they were, so the 10-min bound's ladder and then
the power cycle (spec 04 I76) stay the way out. Device-free: every check is stubbed."""

import time

import pytest

from tests.conftest import fsm_dirty, fsm_healthy, patch_recovery
from tests.test_device_safety import _fast_verify_setup, _setup_down_offbus, _setup_selfheal_hold
from tests.test_post_reset_gate import mesh  # noqa: F401
from tests.test_startup_fabric_hugepages import _bound
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.monitors import fabric
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import galaxy

FABRIC = {
    "pass": {"ok": True, "detail": "fabric links healthy (53s)"},
    "fail": {"ok": False, "detail": "fabric check FAILED (exit 1)"},
    "missing": {"ok": False, "detail": f"{fabric.LINKS_MISSING}: 1 port(s) missing vs the cabling descriptor"},
}
VERDICTS = sorted(FABRIC)


def _stub_verify(monkeypatch, verdict, calls=None):
    """enum+ARC healthy; a fabric verdict only on a pass that runs the fabric check, whose failure
    fails the whole verify (as the real one does)."""

    async def verify(expected, log, run_fabric=True, **_):
        if calls is not None:
            calls.append(run_fabric)
        healthy = not (run_fabric and FABRIC[verdict]["ok"] is False)
        return healthy, {"snapshot": {"ok": True}, **({"fabric": dict(FABRIC[verdict])} if run_fabric else {})}

    patch_recovery(monkeypatch, "_verify_device", verify)


def _hold_marks():
    return srv.fsm.state, srv.fsm.record.why, srv.fsm.record.since, srv.device_hold_episode_since


# --- the one test every lift passes (the release funnel and the rung fold) ----------------------


@pytest.mark.parametrize("verdict", VERDICTS)
def test_the_release_funnel_lifts_only_on_a_fresh_passing_fabric_verdict(mesh, verdict):  # noqa: F811
    fsm_dirty(srv, "gate/post-job: exit 1", why="job_killed")
    before = _hold_marks()
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC[verdict])})
    released = srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")
    assert released is (verdict == "pass")
    if verdict != "pass":
        assert _hold_marks() == before, "a refused lift leaves the hold, its episode and its clock alone"
        assert ("hold_lift_refused" in [k for k, _ in mesh]) and "device_released" not in [k for k, _ in mesh]


@pytest.mark.parametrize("why", ["startup: fabric topology mismatch", "reset_verified: tt-smi -glx_reset"])
def test_the_startup_and_post_reset_releases_refuse_a_missing_link(mesh, why):  # noqa: F811
    """The startup gate and a reset's verify release through the same funnel: a check that found a
    port of the cabling missing FAILED, so neither lifts the hold."""
    fsm_dirty(srv, "gate/startup: held across the restart", why="fabric_unverified")
    before = _hold_marks()
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC["missing"])})
    assert srv._clear_device_dirty(verified=True, why=why) is False
    assert _hold_marks() == before


@pytest.mark.parametrize("stale", ["older_than_the_fault", "too_old"])
def test_a_pass_from_before_the_fault_or_too_long_ago_is_refused(mesh, monkeypatch, stale):  # noqa: F811
    if stale == "older_than_the_fault":
        FABRIC_DEBT.note_fabric(True)
        fsm_dirty(srv, "gate/post-job: exit 1", why="job_killed")
    else:
        fsm_dirty(srv, "gate/post-job: exit 1", why="job_killed")
        FABRIC_DEBT.note_fabric(True)
        monkeypatch.setattr(FABRIC_DEBT, "pass_monotonic", time.monotonic() - srv.HOLD_LIFT_PASS_FRESH_SEC - 1)
    assert srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy") is False
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.parametrize("verdict", VERDICTS)
def test_a_rung_recovered_outcome_lifts_only_on_a_passing_fabric_verdict(mesh, verdict):  # noqa: F811
    fsm_dirty(srv, "gate/post-job: mesh failed the verify after the reset", why="job_killed")
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC[verdict])})
    srv._fold_rung_outcome(galaxy.OUTCOME_RECOVERED, lambda m: None)
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")


# --- each idle lift runs the full check first ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_eth_frozen_hold_lifts_only_on_a_passing_full_check(monkeypatch, tmp_path, clear_job_state, verdict):
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    calls = []
    _stub_verify(monkeypatch, verdict, calls)

    async def eth_advancing(timeout_sec=60.0):
        return True, "every active-eth-core heartbeat advancing"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_advancing)
    before = _hold_marks()
    await srv._attempt_idle_relift()
    assert True in calls, "the full check, fabric included"
    if verdict == "pass":
        assert srv.fsm.state is ServerState.HEALTHY
    else:
        assert _hold_marks() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_off_bus_floor_hold_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    fsm_dirty(srv, "gate/post-job: 3/32 off-bus below the reset floor — held, not reset", why="off_bus")
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    _stub_verify(monkeypatch, verdict)

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)
    await srv._attempt_idle_relift()
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")
    assert (srv.fsm.record.why == "off_bus") is (verdict != "pass")


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_a_down_off_bus_recheck_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    _setup_down_offbus(monkeypatch, tmp_path)
    _stub_verify(monkeypatch, verdict)
    await srv._attempt_down_offbus_recheck()
    assert srv.fsm.state is (ServerState.HEALTHY if verdict == "pass" else ServerState.DOWN)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_a_foreign_holder_hold_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """The holder left: the hold lifts on the full check, fabric included, never the pre-job verify
    alone. A fabric failure keeps the foreign_holder hold (spec 03 I43)."""
    counters, _calls, _events = _fast_verify_setup(monkeypatch, tmp_path)
    calls = []
    _stub_verify(monkeypatch, verdict, calls)
    await srv._attempt_idle_relift()
    assert calls == [True], "one full check, fabric included"
    assert counters["dirty_cleared"] == (1 if verdict == "pass" else 0)
    assert counters["resets"] == 0, "inside the ceiling a failed check holds; the bound's ladder owns it"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_operator_reset_releases_only_on_a_passing_full_check(mesh, monkeypatch, verdict):  # noqa: F811
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    fsm_dirty(srv, "operator reset", why="job_killed")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    _stub_verify(monkeypatch, verdict)
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert (got is True) is (verdict == "pass")
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")


# --- replay: a topology hold with the link still missing ---------------------------------------


@pytest.mark.asyncio
async def test_replay_a_missing_link_hold_survives_every_lift_attempt_until_a_full_pass(
    monkeypatch, tmp_path, clear_job_state
):
    """Replay: an rc-77 topology-mismatch hold whose link stays missing. The I40 bound, an operator
    reset and the bound ladder's fold each try to lift it; none releases a job, and the hold keeps its
    episode and clock. Only a full check that passes on the whole cabling then lifts it. Fails on a
    branch where the I40 bound releases on the startup checks and an advancing eth read."""
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "")
    monkeypatch.setattr(srv.health_monitor, "topology_lift_pending", False)
    fsm_healthy(srv)
    fsm_dirty(srv, "gate/post-job: fabric unverified (could not run): topology mismatch", why="fabric_unverified")
    released = []
    monkeypatch.setattr(srv, "_journal_fsm_transition", lambda why, verified=False: released.append(why))

    async def mismatch_77(expected, log, run_fabric=True, **_):
        detail = "fabric check COULD NOT RUN (19s): topology mismatch; traffic-only rerun: hugepages"
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": detail}}

    patch_recovery(monkeypatch, "_verify_device", mismatch_77)
    before = _hold_marks()

    await srv._attempt_idle_relift()  # the I40 bound: it climbs, it never releases
    assert rungs == ["present"] and _hold_marks() == before and not released

    _stub_verify(monkeypatch, "missing")  # an operator reset: the link is still missing
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert got is not True and srv.fsm.state is not ServerState.HEALTHY
    assert srv.fsm.record.since == before[2] and srv.device_hold_episode_since == before[3]
    assert not released

    srv._fold_rung_outcome(galaxy.OUTCOME_RECOVERED, lambda m: None)  # the bound ladder's fold
    assert srv.fsm.state is not ServerState.HEALTHY and not released
    assert srv.fsm.record.since == before[2]

    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC["pass"])})  # the full check passes on every link
    assert srv._clear_device_dirty(verified=True, why="relift: full check passed") is True
    assert srv.fsm.state is ServerState.HEALTHY and released


@pytest.mark.asyncio
async def test_an_operator_reset_whose_release_the_funnel_refuses_reports_unverified(mesh, monkeypatch):  # noqa: F811
    """The funnel kept the hold: the operator is told the reset is not verified, never OK."""
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    fsm_dirty(srv, "operator reset", why="job_killed")
    _stub_verify(monkeypatch, "pass")
    monkeypatch.setattr(srv, "_hold_lift_unproven", lambda: "the last passing fabric pass is 400s old")
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert got is None and srv.fsm.state is ServerState.RECOVERING
