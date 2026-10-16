# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 01 I66: a tt_device_job_run whose client goes away (a cancel or a disconnect) kills its job,
whole process group included, on its own task. Spec 03 I69: a job the broker stopped mid-run (its
timeout, or a requested kill) owes the mesh one reset, then the full check, before release; a failed
post-reset check stays held and climbs, never releases.

The gate tests run the real probe pass on the eth-timeout tests' mesh (only leaf probes stubbed)."""

import asyncio
import os

import pytest

from tests.conftest import fsm_dirty
from tests.test_eth_timeout_reset import ADVANCING, OUTAGE_HOLD, mesh  # noqa: F401 (fixture)
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.recovery import STAGE_SMI_RESET
from tt_device_mcp.health.recovery.galaxy import _route

# ---------------------------------------------------------------- spec 03 I69: the gate


@pytest.fixture
def killed(mesh):  # noqa: F811
    """The mesh after a job the broker stopped mid-run: the passive eth read advances (a kill leaves
    no timed-out read to flag), the runner marked the device dirty and owes the reset."""
    mesh["eth"] = ADVANCING
    fsm_dirty(srv, "job 7 ended timeout")
    srv._owe_kill_reset("job 7 timeout")
    return mesh


async def _gate():
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)


@pytest.mark.asyncio
async def test_a_timeout_resets_then_runs_the_full_check_before_release(killed):
    await _gate()
    assert killed["resets"] == [["tt-smi", "-glx_reset"]], "one present-mesh reset"
    assert killed["fabric"] == [1], "no pre-reset fabric pass; the full check after the reset ran it"
    assert killed["eth_reads"] == 1, "no pre-reset eth read; the full check read the eth cores"
    assert killed["events"].count("kill_mesh_reset") == 1
    assert killed["captured"][0][0] == "job_killed"
    assert srv.fsm.state is ServerState.HEALTHY, "the full check after the reset released it"
    assert srv._dispatch_reset_spent is True, "the reset spends the shared I55/I61 latch"
    assert srv._kill_reset_debt() == ""


@pytest.mark.asyncio
async def test_a_failed_post_kill_check_stays_held_and_climbs(killed):
    killed["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True  # a prior cycle failed: the host rung is armed
    await _gate()
    assert killed["resets"][0] == ["tt-smi", "-glx_reset"]
    assert killed["fabric"] and min(killed["fabric"]) >= 1, "every fabric pass ran after the reset"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed post-kill check never releases"
    assert killed["reboots"] == 1, "it climbs to the opted-in host rung"


@pytest.mark.asyncio
async def test_a_failed_post_kill_check_without_a_host_rung_stays_held(killed):
    killed["reset_heals"] = False
    await _gate()
    assert len(killed["resets"]) == 1
    assert srv.fsm.state is ServerState.RECOVERING, "held, never released"
    assert srv._device_unavailable_for_tenant(), "no tenant runs on a mesh the post-kill check failed"


@pytest.mark.asyncio
async def test_a_spent_latch_skips_the_kill_reset_onto_todays_path(killed, monkeypatch):
    monkeypatch.setattr(srv, "_dispatch_reset_spent", True)
    await _gate()
    assert "kill_mesh_reset" not in killed["events"] and "kill_reset_skipped" in killed["events"]
    assert killed["fabric"] and killed["fabric"][0] == 0, "the dirty exit's fabric pass ran as before"
    assert srv._kill_reset_debt() == "", "the debt is cleared, not carried"


@pytest.mark.asyncio
async def test_an_outage_hold_skips_the_kill_reset(killed, monkeypatch):
    for var, value in OUTAGE_HOLD.items():
        monkeypatch.setenv(var, value)
    await _gate()
    assert killed["resets"] == [] and "kill_mesh_reset" not in killed["events"]
    assert "kill_reset_skipped" in killed["events"]
    assert killed["fabric"] and killed["fabric"][0] == 0, "the dirty exit's fabric pass ran instead"
    assert srv._dispatch_reset_spent is False


@pytest.mark.asyncio
async def test_a_reset_since_the_kill_settles_the_debt(killed):
    srv.recovery_mechanism.last_reset_monotonic = srv.time.monotonic() + 1.0
    assert srv._kill_reset_debt() == ""
    await _gate()
    assert "kill_mesh_reset" not in killed["events"] and "kill_reset_skipped" not in killed["events"]


@pytest.mark.asyncio
async def test_no_debt_without_a_kill_keeps_todays_path(mesh):  # noqa: F811
    mesh["eth"] = ADVANCING
    fsm_dirty(srv, "job 7 ended failed (exit 1)")
    await _gate()
    assert "kill_mesh_reset" not in mesh["events"]
    assert mesh["fabric"] and mesh["fabric"][0] == 0, "a plain dirty exit keeps its pre-reset fabric pass"


_PRESENT = dict(
    healthy=False,
    fault_reported=False,
    fabric_ok=None,
    fabric_ran=False,
    dirty=True,
    fabric_forced=False,
    off_bus=0,
    frozen_chips=0,
    expected=32,
    sbr_candidates=0,
    galaxy_floor=True,
    reboot_floor=False,
    eth_frozen=False,
    cooling=False,
    reset_scope_active=False,
    was_failing=False,
    last_action=None,
    last_action_recovered=False,
    host_escalation=False,
    reboot_blocked=False,
)


def test_router_kill_reset_needs_a_present_unfrozen_mesh():
    assert _route(**_PRESENT, kill_reset_due=True) == STAGE_SMI_RESET
    for shape in ({"off_bus": 1, "sbr_candidates": 1}, {"frozen_chips": 2}):
        ev = {**_PRESENT, **shape}
        assert _route(**ev, kill_reset_due=True) == _route(**ev), f"{shape}: the debt changes nothing"


# ---------------------------------------------------------------- spec 03 I69: the runner owes it


def _pgid_members(pgid: int) -> list[int]:
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != "Z" and int(fields[2]) == pgid:
            out.append(int(name))
    return out


@pytest.fixture
def runner_env(monkeypatch, tmp_path, clear_job_state):
    """The real runner on a plain (non-privsep) host, its post-job gate off the device."""
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "0")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)

    async def _noop(*a, **k):
        return None

    async def _free_gate(job_log_file):
        return ""

    monkeypatch.setattr(srv, "_verify_device_after_job", _noop)
    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _free_gate)
    monkeypatch.setattr(srv, "_tenant_holder_reason", lambda: "")
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **kw: ("", {}))
    monkeypatch.setattr(srv, "privsep_prefix_for", lambda uid, unit=None: None)
    # The lifespan and job_run start a runner when none is live; a test owns its runner (or has
    # none, so a submitted job stays QUEUED).
    monkeypatch.setattr(srv, "job_runner_task", _HeldRunner())
    events = []
    monkeypatch.setattr(srv, "health_event", lambda name, *a, **k: events.append(name))
    return {"tmp": tmp_path, "events": events}


class _HeldRunner:
    """Stands in for a live singleton runner so the server starts none of its own."""

    def done(self):
        return False


async def _until(pred, tries=400):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.025)
    return False


async def _start(job_id: str, command: str, timeout_sec: int = 600):
    job = srv.Job(
        id=job_id,
        owner=srv.submitting_owner(),
        workspace="/tmp",
        command=command,
        queued_at="t",
        timeout_sec=timeout_sec,
    )
    srv.jobs[job_id] = job
    await srv.get_job_queue().put(job_id)
    runner = asyncio.create_task(srv.job_runner())
    return job, runner


async def _stop(runner):
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_requested_kill_of_a_running_job_owes_the_reset(runner_env):
    job, runner = await _start("931", "sleep 600 | sleep 600")
    try:
        assert await _until(lambda: job.status is srv.JobStatus.RUNNING and job.pid)
        assert await _until(lambda: len(_pgid_members(job.pid)) >= 2)
        mcp = srv.create_mcp_server()
        await mcp.call_tool("tt_device_job_kill", {"params": {"job_id": "931", "owner": job.owner}})
        assert await _until(lambda: job.finished_at is not None)
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.KILLED
    assert srv._kill_reset_debt().startswith("job 931"), "the next post-job gate owes one reset"
    assert "kill_reset_owed" in runner_env["events"]
    assert "931" not in srv.request_killed_job_ids


@pytest.mark.asyncio
async def test_no_debt_for_a_reset_killed_or_completed_job(runner_env):
    job, runner = await _start("932", "true")
    try:
        assert await _until(lambda: job.finished_at is not None)
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.COMPLETED and srv._kill_reset_debt() == ""

    job, runner = await _start("933", "sleep 600")
    try:
        assert await _until(lambda: job.status is srv.JobStatus.RUNNING and job.pid)
        srv.reset_killed_job_ids.add("933")  # our own reset took the device from it
        os.killpg(job.pid, 9)
        assert await _until(lambda: job.finished_at is not None)
    finally:
        await _stop(runner)
    assert srv._kill_reset_debt() == "", "a death our own reset caused owes no second reset"


# ---------------------------------------------------------------- spec 01 I66: the client goes away


class _Recorded(set):
    """``_client_gone_kills`` that remembers every task it held."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def add(self, t):
        self.seen.append(t)
        super().add(t)


async def _job_run_then_cancel(params: dict, wait_for):
    """Drive tt_device_job_run over a real MCP session, cancel the call once ``wait_for(job)`` holds,
    then close the session (the disconnect)."""
    from mcp.client import Client

    async with Client(srv.create_mcp_server()) as client:
        call = asyncio.create_task(client.call_tool("tt_device_job_run", {"params": params}))
        assert await _until(lambda: len(srv.jobs) == 1 and wait_for(next(iter(srv.jobs.values()))))
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
    return next(iter(srv.jobs.values()))


def _params(tmp, command, **extra):
    return {"owner": "tenant", "workspace": str(tmp), "command": command, "timeout_sec": 600, **extra}


@pytest.mark.asyncio
async def test_a_disconnect_kills_the_whole_process_group(runner_env, monkeypatch):
    monkeypatch.setattr(srv, "_client_gone_kills", _Recorded())
    runner = asyncio.create_task(srv.job_runner())
    try:
        members = []

        def running(job):
            if job.status is srv.JobStatus.RUNNING and job.pid and len(_pgid_members(job.pid)) >= 2:
                members.extend(_pgid_members(job.pid))
                return True
            return False

        job = await _job_run_then_cancel(_params(runner_env["tmp"], "sleep 600 | sleep 600"), running)
        assert await _until(lambda: job.finished_at is not None), "the job outlived its client"
        assert await _until(lambda: not _pgid_members(job.pid))
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.KILLED
    assert len(members) >= 2 and not any(
        os.path.exists(f"/proc/{p}/stat") and p in _pgid_members(job.pid) for p in members
    )
    assert "job_run_client_gone" in runner_env["events"]
    assert srv._kill_reset_debt().startswith(f"job {job.id}"), "a kill mid-run owes the reset (spec 03 I69)"


@pytest.mark.asyncio
async def test_the_kill_runs_on_its_own_task_outside_the_cancelled_scope(runner_env, monkeypatch):
    rec = _Recorded()
    monkeypatch.setattr(srv, "_client_gone_kills", rec)
    job = await _job_run_then_cancel(_params(runner_env["tmp"], "true"), lambda j: True)
    assert len(rec.seen) == 1, "one kill task, created outside the cancelled handler"
    result = await asyncio.wait_for(rec.seen[0], 5)
    assert result == {"status": "cancelled", "job_id": job.id} and job.status is srv.JobStatus.KILLED
    assert not rec, "the task drops its own reference once done"


@pytest.mark.asyncio
async def test_a_cancel_while_queued_cancels_the_job(runner_env, monkeypatch):
    rec = _Recorded()
    monkeypatch.setattr(srv, "_client_gone_kills", rec)
    job = await _job_run_then_cancel(_params(runner_env["tmp"], "true"), lambda j: True)
    await asyncio.gather(*rec.seen)
    assert job.status is srv.JobStatus.KILLED, "a queued job nobody waits for never runs"
    assert srv._kill_reset_debt() == "", "a job that never ran owes no reset"


@pytest.mark.asyncio
async def test_keep_running_on_disconnect_leaves_the_job_running(runner_env, monkeypatch):
    rec = _Recorded()
    monkeypatch.setattr(srv, "_client_gone_kills", rec)
    job = await _job_run_then_cancel(
        _params(runner_env["tmp"], "true", keep_running_on_disconnect=True), lambda j: True
    )
    assert rec.seen == [] and job.status is srv.JobStatus.QUEUED, "the opt-out keeps the job"
    assert "job_run_client_gone" in runner_env["events"]
