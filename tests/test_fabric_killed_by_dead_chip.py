# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A fabric check the broker's own dead-chip isolation killed is no verdict (03 I82).

The field case: a chip left the bus during the startup fabric pass, ``_kill_device_holders``
SIGKILLed the validator with every other holder, and the check "exited -9" was recorded as an
UNHEALTHY fabric. That -9 is the broker's, not the links': the run is neither a pass nor a link
fault, the fabric is unknown, and a full pass is owed before anything is released. No process is
spawned here: the fabric runner is stubbed.
"""

import signal

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import evidence
from tt_device_mcp.health import monitor as mon
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import fabric
from tt_device_mcp.health.post_reset import FABRIC_DEBT

REASON = "chip(s) 15 left the PCIe bus"
_BAD_LINKS = "fabric-check: running run_cluster_validation --send-traffic\nunhealthy ethernet connections: 3"
_PARTIAL = "fabric-check: running run_cluster_validation --send-traffic"
_MISMATCH = "Connection mismatch detected\nfound 2 missing port/cable connections\nfound 0 extra port/cable connections"


@pytest.fixture(params=["wrapper", "built-in"])
def fab(request, monkeypatch, health_deps):
    """Both fabric paths: an operator wrapper (its exit code is the verdict, so a bare -9 reads
    UNHEALTHY) and the built-in validator (its output is classified, here a partial run that already
    printed bad links)."""
    m = HealthMonitor(health_deps)
    state = {"rc": -signal.SIGKILL, "kill": True, "text": _BAD_LINKS}

    async def check(argv, env, **kw):
        if state["kill"]:
            m.note_fabric_check_killed(REASON)  # the dead-chip isolation fires mid-run
        return state["rc"], state["text"]

    override = "fabric-wrapper" if request.param == "wrapper" else ""
    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: override)
    monkeypatch.setattr(m, "_begin_action_row", lambda *a, **k: None)
    rows = []
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: rows.append(a))
    monkeypatch.delenv("TTDEV_FABRIC_DESCRIPTOR", raising=False)
    monkeypatch.setattr(fabric, "build_command", lambda descriptor=True: (["run_cluster_validation"], {}))
    monkeypatch.setattr(fabric, "check", check)
    state["rows"] = rows
    return m, state


@pytest.mark.asyncio
async def test_a_fabric_check_killed_by_dead_chip_isolation_is_no_verdict(fab):
    m, state = fab
    m.last_fabric_ok = True  # an earlier pass
    FABRIC_DEBT.note_fabric(True)
    ok, detail = await m.verify_fabric_health(60)
    assert ok is None, f"a killed check is neither a pass nor a link fault: {detail}"
    assert "KILLED by dead-chip isolation" in detail and REASON in detail and "exit -9" in detail
    assert m.last_fabric_ok is None, "the fabric is unknown: no failed verdict, and no earlier pass covers it"
    assert m.last_fabric_skipped is False, "not a 77: it feeds no I46 route-fault streak"
    assert m.fabric_verdict()["result"] == "skipped", "never recorded as a fail (no link-fault class)"
    assert FABRIC_DEBT.owed and REASON in FABRIC_DEBT.owed, "a full pass is owed after recovery"
    assert FABRIC_DEBT.pass_monotonic == 0.0, "the earlier pass no longer vouches for a lift"
    assert state["rows"][-1][3] == "killed", "the jobs-list row says killed, not failed"
    evs = [x for x in evidence.read_health_events() if x["kind"] == "fabric_check_no_verdict"]
    assert evs and evs[-1]["cause"] == "killed_by_dead_chip_isolation" and evs[-1]["rc"] == -9
    assert not [
        x for x in evidence.read_health_events() if x["kind"] == "fabric_check_unavailable"
    ], "not reported as a host with no fabric coverage"


@pytest.mark.asyncio
async def test_the_kill_mark_is_read_once_and_a_later_pass_settles_the_debt(fab):
    m, state = fab
    await m.verify_fabric_health(60)
    assert FABRIC_DEBT.owed
    state.update(kill=False, rc=0, text="all links ok")
    ok, _ = await m.verify_fabric_health(60)
    assert ok is True and m.last_fabric_ok is True, "the next run is judged on its own"
    assert not FABRIC_DEBT.owed, "only a passing full check settles what the kill left owed"


@pytest.mark.asyncio
async def test_a_minus_9_the_isolation_did_not_cause_keeps_its_old_verdict(fab):
    """Only the broker's own dead-chip kill is voided: a -9 from anywhere else is judged as before."""
    m, state = fab
    state["kill"] = False
    ok, _ = await m.verify_fabric_health(60)
    assert ok is False, "a -9 the broker did not cause still reads as unhealthy"
    assert not FABRIC_DEBT.owed


@pytest.fixture
def mismatch(monkeypatch, health_deps, tmp_path):
    """The built-in path with a cabling descriptor whose first run stops on a 2-missing-cable
    mismatch, so the traffic-only rerun (I45) follows; ``state["kill_on"]`` is the run the dead-chip
    isolation kills (1 = the descriptor run, 2 = the rerun)."""
    m = HealthMonitor(health_deps)
    desc = tmp_path / "desc.textproto"
    desc.write_text("x")
    monkeypatch.setenv("TTDEV_FABRIC_DESCRIPTOR", str(desc))
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", str(desc)))
    monkeypatch.setattr(mon, "_cached_topology_mismatch", lambda d: None)
    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "")
    monkeypatch.setattr(m, "_begin_action_row", lambda *a, **k: None)
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(fabric, "build_command", lambda descriptor=True: (["run_cluster_validation"], {}))
    state = {"calls": 0, "kill_on": 2, "text": _BAD_LINKS}

    async def check(argv, env, **kw):
        state["calls"] += 1
        if state["calls"] == state["kill_on"]:
            m.note_fabric_check_killed(REASON)
            return -signal.SIGKILL, state["text"] if state["calls"] == 2 else _MISMATCH
        return 1, _MISMATCH

    monkeypatch.setattr(fabric, "check", check)
    return m, state


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [_BAD_LINKS, _PARTIAL], ids=["bad-links", "partial"])
async def test_a_kill_during_the_traffic_only_rerun_is_no_verdict_either(mismatch, text):
    """The kill lands in the rerun that follows a descriptor mismatch: its -9 is neither the bad
    links it already printed (a link fault) nor, with only missing cables, an I47 cable fault."""
    m, state = mismatch
    state["text"] = text
    m.last_fabric_ok = True
    FABRIC_DEBT.note_fabric(True)
    ok, detail = await m.verify_fabric_health(60)
    assert state["calls"] == 2, "the kill landed in the rerun"
    assert ok is None and "KILLED by dead-chip isolation" in detail, detail
    assert fabric.CABLE_FAULT not in detail and not m.fabric_known_fault, "no I47 cable fault declared"
    assert m.last_fabric_ok is None and m.last_fabric_skipped is False
    assert m.fabric_verdict()["result"] == "skipped"
    assert FABRIC_DEBT.owed and REASON in FABRIC_DEBT.owed and FABRIC_DEBT.pass_monotonic == 0.0
    assert not [x for x in evidence.read_health_events() if x["kind"] == "fabric_known_fault"]


@pytest.mark.asyncio
async def test_a_kill_during_the_descriptor_run_starts_no_rerun(mismatch):
    """Killed in the descriptor run, the check ends there: no ~45-100 s rerun on a chip that left
    the bus, and no cable fault from the mismatch the killed run printed."""
    m, state = mismatch
    state["kill_on"] = 1
    ok, detail = await m.verify_fabric_health(60)
    assert state["calls"] == 1, "no traffic-only rerun after a killed run"
    assert ok is None and "KILLED by dead-chip isolation" in detail, detail
    assert not m.fabric_known_fault and FABRIC_DEBT.owed


@pytest.mark.asyncio
async def test_dead_chip_isolation_marks_the_fabric_check_killed_and_no_lift_follows(monkeypatch, health_deps):
    """server._kill_device_holders, the one place that kills the in-flight fabric pass, marks it, and the
    hold it leaves cannot lift without a fresh passing full check (goal 11, I78), while the 10-min
    bound (04 I76) runs on the hold's age, not on a fabric verdict."""
    monkeypatch.setattr(srv, "health_monitor", HealthMonitor(health_deps))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    killed = []
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "fabric-wrapper")
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")

    class FakeProc:
        pid = 3210

    srv.health_monitor.fabric_check_proc = FakeProc()
    FABRIC_DEBT.note_fabric(True)
    await srv._kill_device_holders(REASON)

    assert (3210, signal.SIGKILL) in killed
    assert srv.health_monitor._fabric_killed == REASON
    assert FABRIC_DEBT.owed and FABRIC_DEBT.pass_monotonic == 0.0
    # Even a release that verified enum+ARC (the chip came back) is refused: the debt holds it
    # fabric-unverified, so only a fresh passing fabric pass can free it.
    assert srv._clear_device_dirty(verified=True, why="test lift") is False, "the release funnel refuses"
    assert srv.fsm.state is not ServerState.HEALTHY
    assert srv._hold_lift_unproven(), "no passing fabric pass since the kill"


@pytest.mark.asyncio
async def test_isolation_with_no_fabric_check_in_flight_marks_nothing(monkeypatch, health_deps):
    monkeypatch.setattr(srv, "health_monitor", HealthMonitor(health_deps))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(srv, "current_process", None)
    await srv._kill_device_holders(REASON)
    assert srv.health_monitor._fabric_killed == ""
    assert not FABRIC_DEBT.owed
