# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A hung eth self-test during a fault episode is evidence, and a full pass re-arms the rung (spec 03 I28).

Measured: every broker start over a frozen eth core hung the self-test's read past its 10s budget,
and the broker disarmed the rung for its whole life, so no gate or post-reset verify ever read the
core the rung exists to name. After the reboot the same read took 0.79s. A hung self-test inside an
open fault episode now reads FROZEN and keeps the device held, and every full pass (startup gate,
post-reset verify, the eth_frozen relift) retries the self-test before it reads.
"""

import time

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.core import Verdict

from .conftest import fsm_dirty, fsm_healthy, patch_health_event, patch_recovery


@pytest.fixture
def unarmed(monkeypatch):
    """The rung left off by a failed startup self-test, retryable, last tried a moment ago."""
    monkeypatch.setattr(srv, "eth_check_armed", False)
    monkeypatch.setattr(srv, "eth_check_disarm_reason", "read hung past the 10s self-test budget")
    monkeypatch.setattr(srv, "eth_rearm_retryable", True)
    monkeypatch.setattr(srv, "_last_eth_rearm_monotonic", time.monotonic())
    monkeypatch.delenv("TTDEV_ETH_CHECK_ARMED", raising=False)
    monkeypatch.setattr(srv, "ETH_CHECK_SELFTEST_BUDGET_SEC", 0.3)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    return events


def _hung_reader(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "sleep 5")


def _clean_reader(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "exit 0")


def _stub_mesh(monkeypatch):
    """Snapshot healthy and a counted traffic pass, on the server's own monitor."""
    mon = srv.health_monitor
    fabric_calls = []

    async def _snapshot_ok(expected):
        return True, "ok"

    async def _fabric():
        fabric_calls.append(1)
        return True, "all links healthy"

    monkeypatch.setattr(mon, "_verify_device", _snapshot_ok)
    monkeypatch.setattr(mon, "verify_fabric_health", _fabric)
    return fabric_calls


def _eth(state):
    obs = [o for o in state.observations if o.monitor == "eth_heartbeat"]
    assert obs, "the pass recorded no eth read"
    return obs[0]


# --- the self-test itself ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hung_selftest_in_a_fault_episode_is_frozen_evidence(monkeypatch, unarmed):
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    _hung_reader(monkeypatch)
    detail = await srv.selftest_eth_heartbeat()
    assert "hung" in detail and "job_killed" in detail
    assert srv.eth_check_armed is False
    assert srv.eth_rearm_retryable is True, "a hung read must stay retryable, never off for the broker's life"
    assert "eth_selftest_hung" in unarmed


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["healthy", "startup_unverified"])
async def test_a_hung_selftest_outside_a_fault_episode_only_leaves_the_rung_off(monkeypatch, unarmed, where):
    # A broker start's own startup_unverified hold is not a fault: a slow read there proves nothing.
    if where == "healthy":
        fsm_healthy(srv)
    else:
        fsm_dirty(srv, "broker start", why="startup_unverified", dirty=False)
    _hung_reader(monkeypatch)
    assert await srv.selftest_eth_heartbeat() == ""
    assert srv.eth_check_armed is False
    assert "eth_selftest_hung" not in unarmed


# --- a full pass retries the self-test before it reads ----------------------------------------


@pytest.mark.asyncio
async def test_a_full_pass_rearms_the_rung_before_it_reads(monkeypatch, unarmed):
    """Fails on base: the rung stayed off, the pass's eth read skipped, and the mesh was released on
    the traffic pass alone. Unpaced: the last attempt was a moment ago."""
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    _clean_reader(monkeypatch)
    fabric_calls = _stub_mesh(monkeypatch)
    state = await srv.health_monitor.update(phase="verify_device", run_fabric=True, indices=[0, 1], expected=2)
    assert srv.eth_check_armed is True
    assert _eth(state).verdict is Verdict.HEALTHY
    assert fabric_calls == [1]


@pytest.mark.asyncio
async def test_a_full_pass_in_a_fault_episode_holds_on_a_hung_selftest(monkeypatch, unarmed):
    """The frozen core hangs the self-test: the pass reads FROZEN and never pushes traffic over it."""
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    _hung_reader(monkeypatch)
    fabric_calls = _stub_mesh(monkeypatch)
    state = await srv.health_monitor.update(phase="verify_device", run_fabric=True, indices=[0, 1], expected=2)
    assert _eth(state).verdict is Verdict.UNHEALTHY
    assert state.eth_frozen is True and state.healthy is False
    assert fabric_calls == [], "pushed traffic across a core whose read hangs"


@pytest.mark.asyncio
async def test_a_full_pass_outside_an_episode_still_runs_the_traffic_pass_on_a_hung_selftest(monkeypatch, unarmed):
    fsm_healthy(srv)
    _hung_reader(monkeypatch)
    fabric_calls = _stub_mesh(monkeypatch)
    state = await srv.health_monitor.update(phase="post-job", run_fabric=True, indices=[0, 1], expected=2)
    assert _eth(state).verdict is Verdict.SKIPPED
    assert fabric_calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["armed", "not_retryable"])
async def test_a_full_pass_does_not_retry_an_armed_or_unretryable_rung(monkeypatch, unarmed, why):
    if why == "armed":
        monkeypatch.setattr(srv, "eth_check_armed", True)
    else:
        monkeypatch.setattr(srv, "eth_rearm_retryable", False)
    calls = []

    async def _selftest():
        calls.append(1)
        return ""

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _selftest)
    assert await srv._rearm_eth_before_check() == ""
    assert calls == []


@pytest.mark.asyncio
async def test_the_post_reset_verify_holds_on_a_hung_selftest(monkeypatch, unarmed):
    """Goal of 04 I32 seen from the eth rung: a reset's verify re-arms first, and a frozen core that
    still hangs the read fails the verify instead of passing on the traffic pass."""
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    _hung_reader(monkeypatch)
    fabric_calls = _stub_mesh(monkeypatch)
    healthy, evidence, _retries = await srv.galaxy_recovery._verify_device_after_reset(2, lambda m: None)
    assert not healthy
    assert evidence["eth_heartbeat"]["ok"] is False
    assert fabric_calls == []


@pytest.mark.asyncio
async def test_the_post_reset_verify_reads_eth_once_rearmed(monkeypatch, unarmed):
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    _clean_reader(monkeypatch)
    _stub_mesh(monkeypatch)
    healthy, evidence, _retries = await srv.galaxy_recovery._verify_device_after_reset(2, lambda m: None)
    assert healthy
    assert evidence["eth_heartbeat"]["ok"] is True
    assert srv.eth_check_armed is True


# --- the eth_frozen relift re-arms before its read --------------------------------------------


def _selfheal_hold(monkeypatch, tmp_path):
    for i in range(32):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "should_privsep", lambda: True)
    srv.device_op_lock = None
    srv.device_op_active = ""
    fsm_dirty(srv, "gate/startup: eth-core heartbeat frozen — held, not reset", why="eth_frozen")
    srv.last_relift_monotonic = 0.0
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "1")

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)


@pytest.mark.asyncio
async def test_the_eth_frozen_relift_holds_while_the_selftest_still_hangs(
    monkeypatch, tmp_path, clear_job_state, unarmed
):
    _selfheal_hold(monkeypatch, tmp_path)
    reads = []

    async def _hung():
        return "eth self-test read hung past its 10s budget during an open fault episode (eth_frozen)"

    async def _read(timeout_sec=60.0):
        reads.append(1)
        return True, "advancing"

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _hung)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", _read)
    await srv._attempt_idle_relift()
    assert srv.fsm.state is not ServerState.HEALTHY
    assert srv.fsm.record.why == "eth_frozen"
    assert reads == [], "read past a self-test that just hung"


@pytest.mark.asyncio
async def test_the_eth_frozen_relift_rearms_then_reads(monkeypatch, tmp_path, clear_job_state, unarmed):
    """Fails on base: the unarmed read skipped, so the hold could never lift on a healed core."""
    _selfheal_hold(monkeypatch, tmp_path)
    calls = []

    async def _arms():
        calls.append("selftest")
        srv.eth_check_armed = True
        return ""

    async def _read(timeout_sec=60.0):
        calls.append("read")
        return (True, "advancing") if srv.eth_check_armed else (None, "skipped (not armed)")

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _arms)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", _read)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", True)
    await srv._attempt_idle_relift()
    assert calls[:2] == ["selftest", "read"]
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_retry_outside_an_episode_is_paced_to_the_rearm_interval(monkeypatch, unarmed):
    """Idle full passes share the idle tick's pace; the fixture's last attempt was a moment ago."""
    fsm_healthy(srv)
    attempts = []

    async def _selftest():
        attempts.append(1)
        return ""

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _selftest)
    assert await srv._rearm_eth_before_check() == ""
    assert attempts == [], "retried inside ETH_CHECK_REARM_INTERVAL_SEC with no fault episode"
    monkeypatch.setattr(srv, "_last_eth_rearm_monotonic", time.monotonic() - 2 * srv.ETH_CHECK_REARM_INTERVAL_SEC)
    await srv._rearm_eth_before_check()
    assert attempts == [1]


@pytest.mark.asyncio
async def test_a_retry_inside_a_fault_episode_is_never_paced(monkeypatch, unarmed):
    """Goal 8: a reset's post-check re-arms at once, however recent the last attempt."""
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    attempts = []

    async def _selftest():
        attempts.append(1)
        return ""

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _selftest)
    await srv._rearm_eth_before_check()
    await srv._rearm_eth_before_check()
    assert attempts == [1, 1]


@pytest.mark.asyncio
async def test_the_77_fallback_skips_a_second_retry_after_the_relift_retried(monkeypatch, unarmed):
    fsm_dirty(srv, "fabric check unrunnable", why="fabric_unverified")
    rearmed = []

    async def _rearmed():
        rearmed.append(1)
        return None, "no verdict"

    async def _verify():
        return None, "no verdict"

    async def _escalate(kind):
        return False

    monkeypatch.setattr(srv, "_eth_read_rearmed", _rearmed)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", _verify)
    await srv._fabric_77_fallback({}, lambda m: None, _escalate, retried=True)
    assert rearmed == []
    await srv._fabric_77_fallback({}, lambda m: None, _escalate)
    assert rearmed == [1]
