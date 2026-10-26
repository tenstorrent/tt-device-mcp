"""Spec 04 I103: only a running broker job holds an automatic power cycle; a holder outside every
broker job or exec scope never vetoes or delays it, and a queued or waiting job does not either.

Nothing here releases a device: the boot after a cycle runs the startup gate's full check, fabric
included (03 I4), and a partial drop still gets the full ladder and sweep first (04 I100)."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests import test_classac_power_cycle_direct as _direct
from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off
from tests.test_pre_reset_holder_scan import READER, _Box
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health.recovery import galaxy

FOREIGN = SimpleNamespace(pid=4242, uid=1000, scoped=False)  # a host process outside every broker scope
JOB_HOLDER = SimpleNamespace(pid=4343, uid=1000, scoped=True)  # a broker job's own process
rig = _direct.rig  # the class A/C rig fixture, shared


def _holders(rig, monkeypatch, *holders):
    monkeypatch.setattr(galaxy, "is_tenant", lambda h: True)
    monkeypatch.setattr(galaxy, "holder_comm", lambda pid: "python")
    rig["scan"] = HolderScan(holders=list(holders), complete=True)


def _job(monkeypatch, status):
    job = srv.Job(id="7", owner="alice", workspace="/w", command="pytest", queued_at="")
    job.status = status
    monkeypatch.setattr(srv, "jobs", {"7": job})
    return job


def _whole_tray(rig):
    rig["beats"] = _beats_without(_off(rig["map"], {2: 8}))
    rig["g"]._td_reset_seen = True  # no prelude: only the hold-off is under test


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["offbus", "offbus-forced"])
async def test_a_foreign_holder_never_delays_the_all_chips_off_cycle(rig, monkeypatch, phase):
    _holders(rig, monkeypatch, FOREIGN)
    _whole_tray(rig)
    assert await rig["g"].escalate(phase, [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["power_cycle"], "straight to the cycle, no wait on the foreign holder"


@pytest.mark.asyncio
async def test_a_foreign_holder_at_the_bound_gets_the_ladder_and_sweep_then_the_cycle(rig, monkeypatch):
    """A partial drop climbing to the host rung with a foreign holder: the full ladder and the
    last-chance sweep run, the sweep's full check fails, and the cycle fires; the holder holds none of it."""
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=10)).isoformat())
    _holders(rig, monkeypatch, FOREIGN)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    await _gate(rig, galaxy.STAGE_POWER_CYCLE)
    assert rig["order"] == ["glx_reset", "sweep", "power_cycle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", [None, JOB_HOLDER])
async def test_a_running_broker_job_defers_the_cycle_until_it_ends(rig, monkeypatch, holder):
    _holders(rig, monkeypatch, *([FOREIGN] + ([holder] if holder else [])))
    _whole_tray(rig)
    job = _job(monkeypatch, srv.JobStatus.RUNNING)
    assert await rig["g"].escalate("offbus", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_WAITING
    assert rig["order"] == [], "the running job holds the cycle"
    job.status = srv.JobStatus.COMPLETED
    rig["scan"] = HolderScan(holders=[FOREIGN], complete=True)  # the job's processes are gone
    assert await rig["g"].escalate("offbus", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["power_cycle"], "the cycle fires once the job ends"


@pytest.mark.asyncio
async def test_a_waiting_or_queued_job_does_not_defer_the_cycle(rig, monkeypatch):
    """A job queued behind the hold, or waiting on a foreign holder to let go, is not running: it
    never holds the cycle that would free the device for it."""
    _holders(rig, monkeypatch, FOREIGN)
    _whole_tray(rig)
    _job(monkeypatch, srv.JobStatus.QUEUED)
    assert await rig["g"].escalate("offbus", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["power_cycle"]


@pytest.mark.asyncio
async def test_the_pre_cycle_scan_clears_a_foreign_holder_and_refuses_a_job_holder(monkeypatch):
    foreign = DeviceHolder(pid=4202, uid=1001)
    box = _Box(monkeypatch, [foreign, READER], tenants=[foreign])
    result = await box.run(tenants_block=True, foreign_yield=True)
    assert box.sent == {foreign.pid: ["TERM"], READER.pid: ["TERM"]}
    assert result.outcome == "killed" and result.refusal() == ""

    job_holder = DeviceHolder(pid=4303, uid=1001, scoped=True)
    box = _Box(monkeypatch, [job_holder, READER], tenants=[job_holder])
    result = await box.run(tenants_block=True, foreign_yield=True)
    assert box.sent == {} and result.outcome == "tenant_blocked"
    assert f"pid {job_holder.pid}" in result.refusal()
