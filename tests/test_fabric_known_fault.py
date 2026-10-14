# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Jobs that keep dying on the fabric's routing after fabric passes that could not run are a known
fabric fault: held without a reset, paged once, bounded (03 I46). No device is opened: the
validator, the alert hook and the job's command are stand-ins."""

import asyncio
from datetime import datetime

import pytest

from tests.test_device_safety import _free_device_lock, _no_workspace_activation, _wait_for
from tt_device_mcp import server as srv
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import fabric

_ROUTE = "TT_THROW: Could not find any forwarding direction from chip 3 to chip 17"


@pytest.fixture
def mon(monkeypatch, health_deps):
    """A HealthMonitor on the built-in path whose validator answers ``state['run']``."""
    m = HealthMonitor(health_deps)
    state = {"run": (77, "COULD NOT RUN\n"), "alerts": [], "events": []}

    async def check(argv, env, **kw):
        return state["run"]

    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "")
    monkeypatch.setattr(m, "_begin_action_row", lambda *a, **k: None)
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(fabric, "build_command", lambda descriptor=True: (["run_cluster_validation"], {}))
    monkeypatch.setattr(fabric, "check", check)
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", ""))
    monkeypatch.setattr(monitor_mod, "_save_asic_bus_map", lambda: None)
    monkeypatch.setattr(monitor_mod, "health_event", lambda ev, **f: state["events"].append(ev))
    monkeypatch.setattr(monitor_mod.alert, "send_alert", lambda ev, log=None: state["alerts"].append(ev))
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_ROUTE_FAULT_JOBS", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_KNOWN_FAULT_HOLD_SEC", raising=False)
    return m, state


@pytest.mark.asyncio
async def test_two_jobs_dying_on_routing_after_77_passes_are_a_known_fault(mon):
    """The field loop: a 77 pass, a job dies "could not find any forwarding direction", a reset, another
    77, the next job dies the same way. The second such job declares the fault and pages once."""
    m, state = mon
    assert (await m._verify_fabric_health_body(60))[0] is None
    assert m.last_fabric_skipped is True
    assert m.note_job_fabric_route_fault(True, f"... {_ROUTE} ...") is None
    await m._verify_fabric_health_body(60)
    why = m.note_job_fabric_route_fault(True, _ROUTE)
    assert why and why.startswith("known fabric fault: 2 jobs in a row") and "forwarding direction" in why.lower()
    assert m.known_fabric_fault() == why
    assert [a["kind"] for a in state["alerts"]] == ["fabric_known_fault"]
    assert "fabric_known_fault" in state["events"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run, failed, tail",
    [
        ((77, "COULD NOT RUN\n"), False, ""),  # a job that completed
        ((77, "COULD NOT RUN\n"), True, "E   AssertionError"),  # a failure off the fabric
        ((0, "all links ok\n"), True, _ROUTE),  # the pass before it measured the fabric healthy
    ],
)
async def test_anything_else_breaks_the_streak(mon, run, failed, tail):
    m, state = mon
    await m._verify_fabric_health_body(60)
    assert m.note_job_fabric_route_fault(True, _ROUTE) is None
    state["run"] = run
    await m._verify_fabric_health_body(60)
    assert m.note_job_fabric_route_fault(failed, tail) is None
    state["run"] = (77, "COULD NOT RUN\n")
    await m._verify_fabric_health_body(60)
    assert m.note_job_fabric_route_fault(True, _ROUTE) is None, "the streak restarted"
    assert state["alerts"] == []


@pytest.mark.asyncio
async def test_a_healthy_fabric_verdict_ends_the_known_fault(mon):
    m, state = mon
    m.last_fabric_skipped = True
    m._fabric_route_fault_streak = 1
    assert m.note_job_fabric_route_fault(True, _ROUTE)
    state["run"] = (0, "all links ok\n")
    assert (await m._verify_fabric_health_body(60))[0] is True
    assert m.known_fabric_fault() == "" and m._fabric_route_fault_streak == 0
    assert "fabric_known_fault_cleared" in state["events"]


def test_the_known_fault_is_bounded_and_rearms_one_short(mon, monkeypatch):
    """Past its bound the fault expires so the usual 77 path gets one more try; the streak is left one
    short, so the next job that dies the same way declares it again at once."""
    m, state = mon
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_KNOWN_FAULT_HOLD_SEC", "600")
    m.last_fabric_skipped = True
    m._fabric_route_fault_streak = 1
    assert m.note_job_fabric_route_fault(True, _ROUTE)
    m._fabric_known_fault_at -= 599
    assert m.known_fabric_fault()
    m._fabric_known_fault_at -= 2
    assert m.known_fabric_fault() == ""
    assert "fabric_known_fault_expired" in state["events"]
    assert m.note_job_fabric_route_fault(True, _ROUTE), "one more such job declares it again"


@pytest.mark.asyncio
async def test_the_runner_holds_a_known_fabric_fault_without_the_dirty_reset(monkeypatch, clear_job_state, tmp_path):
    """A failed job normally marks the device dirty, and the post-job gate then resets it: on the field
    host that was a galaxy reset per job, each ending in another 77 (24 resets in 2.5 h). Once the
    fault is known the job's end holds the device fabric-unverified with the reason and marks nothing
    dirty. Fails without the guard: the job is flagged dirty."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv.alert, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", True)
    monkeypatch.setattr(srv.health_monitor, "_fabric_route_fault_streak", 1)
    monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "")
    dirty, held, gates = [], [], []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **k: dirty.append(reason))
    monkeypatch.setattr(srv, "_hold_device_fabric_unverified", lambda reason, **k: held.append(reason))

    async def _gate(job_log_file, job_failed=False, noop_failure=False, exit1_failure=False):
        gates.append(job_failed)

    monkeypatch.setattr(srv, "_verify_device_after_job", _gate)
    log = tmp_path / "448.log"
    log.write_text("")
    job = srv.Job(
        id="448",
        owner="tenant",
        workspace="/tmp",
        command=f"echo '{_ROUTE}'; exit 1",
        queued_at=datetime.now().isoformat(),
    )
    job.log_file = str(log)
    srv.jobs["448"] = job
    await srv.get_job_queue().put("448")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _wait_for(lambda: bool(gates))
        assert gates == [True]
        assert len(held) == 1 and held[0].startswith("job 448: known fabric fault"), held
        assert dirty == [], "no dirty flag, so no post-job reset"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
