# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The eth rung vouches only for the links it has seen, and a failed self-test is retried (spec 03 I28).

The probe reads only links that are up. A link that went down is one core fewer, and every
remaining core still reads advancing, so the read passed a mesh with a dead link. The broker now
keeps a high-water mark of the measured count and treats a drop as "could not vouch", which sends
the pass on to the traffic pass. Separately, a startup self-test that missed its budget left the
rung off until the next restart; an idle broker now retries it.
"""

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitors import eth

from .conftest import fsm_dirty, fsm_healthy


def _count(n: int) -> str:
    return f"eth-links: measured={n} down=0 unreadable=0"


def _stub_probe(monkeypatch, rc: int, text: str) -> None:
    """The built-in probe, armed and runnable, answering ``rc`` with ``text`` — no spawn."""
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.setattr(eth, "build", lambda: (["probe"], {}))

    async def _check(argv, env, *, timeout_sec, track, cwd=None):
        return rc, text

    monkeypatch.setattr(eth, "check", _check)


def test_the_link_count_is_parsed_from_the_probe_output():
    assert eth.parse_link_count(f"{_count(12)}\nall 12 active-eth core heartbeat(s) advancing") == 12
    assert eth.parse_link_count("all 12 active-eth core heartbeat(s) advancing") is None
    assert eth.parse_link_count("") is None


def test_the_link_baseline_keeps_its_high_water_mark():
    mon = srv.health_monitor
    assert mon.eth_link_drop(8) == "", "the first count seeds the mark"
    assert mon.eth_link_drop(10) == "", "a higher count ratchets the mark up"
    drop = mon.eth_link_drop(8)
    assert "8 of 10" in drop, "the mark never drops back to a lower count"
    assert mon.eth_link_drop(10) == ""


def test_an_unreadable_link_baseline_fails_closed_once_then_rebaselines():
    mon = srv.health_monitor
    path = srv.health_dir() / mon.ETH_LINK_BASELINE_FILE
    path.write_text("not json")
    assert "unreadable" in mon.eth_link_drop(6)
    assert mon.eth_link_drop(6) == ""


@pytest.mark.asyncio
async def test_an_advancing_read_that_lost_a_link_is_not_a_pass(monkeypatch):
    srv.health_monitor.eth_link_drop(12)
    _stub_probe(monkeypatch, 0, f"{_count(11)}\nall 11 active-eth core heartbeat(s) advancing")
    ok, detail = await srv.health_monitor.verify_eth_heartbeat()
    assert ok is None, "a link that went down must not pass as all-advancing"
    assert "11 of 12" in detail


@pytest.mark.asyncio
async def test_an_advancing_read_at_the_mark_passes_and_ratchets(monkeypatch):
    srv.health_monitor.eth_link_drop(12)
    _stub_probe(monkeypatch, 0, f"{_count(14)}\nall 14 active-eth core heartbeat(s) advancing")
    ok, _ = await srv.health_monitor.verify_eth_heartbeat()
    assert ok is True
    assert "12 of 14" in srv.health_monitor.eth_link_drop(12)


@pytest.mark.asyncio
async def test_a_frozen_read_stays_frozen_whatever_the_count(monkeypatch):
    srv.health_monitor.eth_link_drop(12)
    _stub_probe(monkeypatch, 3, f"{_count(4)}\nNo heartbeat detected on 1/4 active-eth core(s)")
    ok, _ = await srv.health_monitor.verify_eth_heartbeat()
    assert ok is False


@pytest.mark.asyncio
async def test_an_override_is_judged_on_its_exit_code_alone(monkeypatch):
    srv.health_monitor.eth_link_drop(12)
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "exit 0")
    monkeypatch.setattr(eth, "build", lambda: (["probe"], {}))

    async def _check(argv, env, *, timeout_sec, track, cwd=None):
        return 0, _count(1)

    monkeypatch.setattr(eth, "check", _check)
    ok, _ = await srv.health_monitor.verify_eth_heartbeat()
    assert ok is True


@pytest.mark.asyncio
async def test_a_link_drop_sends_the_pass_on_to_the_traffic_pass(monkeypatch):
    srv.health_monitor.eth_link_drop(12)
    _stub_probe(monkeypatch, 0, f"{_count(11)}\nall 11 active-eth core heartbeat(s) advancing")
    mon = srv.health_monitor

    async def _snapshot_ok(expected):
        return True, "ok"

    fabric_calls = []

    async def _fabric():
        fabric_calls.append(1)
        return True, "all links healthy"

    monkeypatch.setattr(mon, "_verify_device", _snapshot_ok)
    monkeypatch.setattr(mon, "verify_fabric_health", _fabric)
    state = await mon.update(phase="post_job", run_fabric=True, indices=[0, 1], expected=2)
    eth_obs = [o for o in state.observations if o.monitor == "eth_heartbeat"]
    assert eth_obs and eth_obs[0].verdict is Verdict.SKIPPED
    assert fabric_calls == [1], "an unverified link count must not stop the traffic pass"


# --- the self-test seeds the mark, and a failed self-test is retried while idle ---------------


@pytest.fixture
def rearm_state(monkeypatch, clear_job_state):
    monkeypatch.setattr(srv, "eth_check_armed", False)
    monkeypatch.setattr(srv, "eth_check_disarm_reason", "read hung past the 10s self-test budget")
    monkeypatch.setattr(srv, "eth_selftest_ran", True)
    monkeypatch.setattr(srv, "_last_eth_rearm_monotonic", 0.0)
    monkeypatch.setattr(srv, "_eth_rearm_task", None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "readopted_scopes", {})
    fsm_healthy(srv)
    calls = []

    async def _selftest():
        calls.append(1)
        srv.eth_check_armed = True

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _selftest)
    return calls


@pytest.mark.asyncio
async def test_the_selftest_seeds_the_link_baseline(monkeypatch):
    monkeypatch.setattr(srv, "eth_check_armed", False)
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.setattr(
        eth,
        "build",
        lambda: (["/bin/sh", "-c", f"echo '{_count(16)}'; echo 'all 16 advancing'; exit 0"], {}),
    )
    await srv.selftest_eth_heartbeat()
    assert srv.eth_check_armed is True
    assert "15 of 16" in srv.health_monitor.eth_link_drop(15)


@pytest.mark.asyncio
async def test_a_failed_selftest_rearms_from_the_idle_tick(rearm_state):
    srv._maybe_spawn_eth_rearm()
    assert srv._eth_rearm_task is not None
    await srv._eth_rearm_task
    assert rearm_state == [1]
    assert srv.eth_check_armed is True


@pytest.mark.asyncio
async def test_the_idle_relift_tick_drives_the_rearm(rearm_state):
    srv._maybe_spawn_idle_relift()
    assert srv._eth_rearm_task is not None
    await srv._eth_rearm_task
    assert rearm_state == [1]


@pytest.mark.asyncio
async def test_the_rearm_is_rate_limited(rearm_state, monkeypatch):
    async def _still_failing():
        rearm_state.append(1)

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _still_failing)
    srv._maybe_spawn_eth_rearm()
    await srv._eth_rearm_task
    srv._maybe_spawn_eth_rearm()
    if srv._eth_rearm_task is not None:
        await srv._eth_rearm_task
    assert rearm_state == [1], "a second try inside ETH_CHECK_REARM_INTERVAL_SEC must not run"


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["never_ran", "armed", "not_healthy", "queued_job", "device_op", "running"])
async def test_the_rearm_waits_for_an_idle_device(rearm_state, monkeypatch, why):
    if why == "never_ran":
        monkeypatch.setattr(srv, "eth_selftest_ran", False)  # startup has not tried yet, or a per-user daemon
    elif why == "armed":
        monkeypatch.setattr(srv, "eth_check_armed", True)
    elif why == "not_healthy":
        fsm_dirty(srv, "job killed")
    elif why == "queued_job":
        srv.jobs["001"] = srv.Job(
            id="001", owner="u", workspace="/tmp", command="true", queued_at="2026-10-03T00:00:00"
        )
    elif why == "device_op":
        monkeypatch.setattr(srv, "device_op_active", "health-gate/post_job")
    elif why == "running":
        monkeypatch.setattr(srv, "current_process", object())
    srv._maybe_spawn_eth_rearm()
    if srv._eth_rearm_task is not None:
        await srv._eth_rearm_task
    assert rearm_state == []


@pytest.mark.asyncio
async def test_a_job_queued_before_the_lock_is_taken_cancels_the_rearm(rearm_state):
    srv._maybe_spawn_eth_rearm()
    srv.jobs["001"] = srv.Job(id="001", owner="u", workspace="/tmp", command="true", queued_at="2026-10-03T00:00:00")
    await srv._eth_rearm_task
    assert rearm_state == [], "the idle check is repeated under the device-op lock"
