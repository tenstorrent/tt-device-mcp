# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A holder the dead-chip valve refused to kill does not stop the 10-min bound (spec 03 I84, 04 I76).

When the valve cannot detach a chip that left the bus, it kills nobody: a holder's fd close would make
the driver reach the chip. Those holders (the running broker job, a tenant process) then read as tenants
to every forced ladder, and a running job with no waiter never times out, so the hold had no bound. At
the bound they are no tenants: the broker skips the ladder (whose reset would kill them) and asks for the
power cycle, still killing nobody. A tenant the valve never refused still blocks, and no lift happens
without a just-passed full check with fabric. No process is spawned and no device touched."""

from datetime import datetime

import signal
import pytest

from tests.conftest import fsm_dirty
from tests.test_classac_power_cycle_direct import rig  # noqa: F401 - the fixture
from tests.test_hold_bound import _drain, _present_hold_at_the_bound
from tests.test_hold_lift_full_check import FABRIC, VERDICTS
from tests.test_post_reset_gate import mesh  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.post_reset import FABRIC_DEBT

JOB_PID = 7070
JOB_CHILD = 7071
TENANT = 5151
NEWCOMER = 6161


class _JobProc:
    pid = JOB_PID
    returncode = None


def _holders(r, *pids):
    r["scan"] = HolderScan(holders=[DeviceHolder(pid=p, uid=1001, scoped=p == JOB_CHILD) for p in pids], complete=True)


def _arm(r, monkeypatch, holder):
    """A chip off the bus whose PCI remove fails, under a running broker job or a tenant process."""
    kills = []
    monkeypatch.setattr(srv.os, "kill", lambda pid, sig: kills.append(pid))
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: sig == signal.SIGSTOP or kills.append(pid))
    monkeypatch.setattr(srv, "_read_proc_starttime", lambda pid: "100", raising=False)
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: False)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: f"0000:46:00.{idx}")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    if holder == "job":
        job = srv.Job(id="7", owner="alice", workspace="/w", command="pytest", queued_at="")
        job.status = srv.JobStatus.RUNNING
        monkeypatch.setattr(srv, "jobs", {"7": job})
        monkeypatch.setattr(srv, "current_process", _JobProc())
        monkeypatch.setattr(srv, "current_job_id", "7")
        _holders(r, JOB_CHILD)
    else:
        monkeypatch.setattr(srv, "current_process", None)
        _holders(r, TENANT)
    # The real governor's first rule: a tenant refuses a host rung.
    monkeypatch.setattr(
        r["g"].mechanism,
        "auto_recovery_allowed",
        lambda action, tenant_active=False, **k: (False, "a tenant holds the device") if tenant_active else (True, ""),
    )
    return kills


async def _refuse():
    await srv.sampler.isolate_dead_chips(["13"])


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", ["job", "tenant"])
async def test_a_refused_holder_gets_the_power_cycle_at_the_bound(
    rig, monkeypatch, clear_job_state, holder  # noqa: F811
):
    kills = _arm(rig, monkeypatch, holder)
    await _refuse()
    kinds = [k for k, _ in rig["events"]]
    assert "chip_dead_kill_refused" in kinds and not kills, "the valve killed nobody"

    _present_hold_at_the_bound(rig, monkeypatch, why="gate_error", detail="chip 13 off the bus")
    assert srv.galaxy_recovery.tenant_holds(), "the refused holder still reads as a tenant to other ladders"
    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    kinds = [k for k, _ in rig["events"]]
    order = rig["order"]
    assert "hold_bound_tenant_blocked" not in kinds, "a refused holder is not a tenant at the bound"
    assert order.count("power_cycle") == 1, "the bound asked for the power cycle"
    assert "glx_reset" not in order and "sweep" not in order, "no ladder reset runs over a refused holder"
    assert not kills, "nobody is killed on a chip still attached"
    assert "released" not in order


@pytest.mark.asyncio
async def test_a_tenant_the_valve_never_refused_still_blocks_the_bound(rig, monkeypatch, clear_job_state):  # noqa: F811
    """A process that opened the device after the refusal, or one whose pid was recycled, is a live
    tenant: the bound pages it and runs nothing over it."""
    kills = _arm(rig, monkeypatch, "tenant")
    await _refuse()
    _present_hold_at_the_bound(rig, monkeypatch, why="gate_error", detail="chip 13 off the bus")
    _holders(rig, TENANT, NEWCOMER)
    monkeypatch.setattr(srv, "_read_proc_starttime", lambda pid: "100" if pid == TENANT else "200", raising=False)

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    kinds = [k for k, _ in rig["events"]]
    assert "power_cycle" not in rig["order"] and not kills
    assert kinds.count("hold_bound_tenant_blocked") == 1
    assert dict(rig["events"])["hold_bound_tenant_blocked"]["pid"] == NEWCOMER

    monkeypatch.setattr(srv, "_read_proc_starttime", lambda pid: "300", raising=False)  # TENANT's pid recycled
    _holders(rig, TENANT)
    assert srv._bound_tenant_holds(True), "a recycled pid is not the refused holder"


@pytest.mark.parametrize("verdict", VERDICTS)
def test_the_refusal_record_lifts_nothing_and_only_a_passing_full_check_clears_it(
    mesh, monkeypatch, verdict  # noqa: F811
):
    """Goal of spec 03 I78: the record routes the bound, never a lift. A failed full check, or one that
    found fewer links than cabled, keeps the hold and the record; only a passing one clears both."""
    monkeypatch.setattr(srv, "_kill_refused_pids", {TENANT: "100"}, raising=False)
    monkeypatch.setattr(srv, "_kill_refused_job_pid", JOB_PID, raising=False)
    fsm_dirty(srv, "chip(s) 13 fell off the PCIe bus", why="heartbeat")
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC[verdict])})

    released = srv._clear_device_dirty(verified=True, why="gate/idle: verified healthy")

    assert released is (verdict == "pass")
    if verdict == "pass":
        assert srv._kill_refused_pids == {} and srv._kill_refused_job_pid is None
        assert srv.fsm.state is ServerState.HEALTHY
    else:
        assert srv._kill_refused_pids == {TENANT: "100"} and srv._kill_refused_job_pid == JOB_PID
        assert srv.fsm.state is not ServerState.HEALTHY
