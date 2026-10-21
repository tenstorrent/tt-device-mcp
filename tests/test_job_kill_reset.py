# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 01 I66: a tt_device_job_run whose client goes away (a cancel or a disconnect) kills its job,
whole process group included, on its own task. Spec 03 I69: a job the broker stopped mid-run (its
timeout, or a requested kill) owes the mesh one reset, then the full check, before release; a failed
post-reset check stays held and climbs, never releases.

The gate tests run the real probe pass on the eth-timeout tests' mesh (only leaf probes stubbed)."""

import asyncio
import logging
import os
import signal

import httpx2
import pytest

from tests.conftest import fsm_dirty
from tests.test_eth_timeout_reset import ADVANCING, OUTAGE_HOLD, mesh  # noqa: F401 (fixture)
from tt_device_mcp import constants
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


def _runner_done(job) -> bool:
    """The runner has finished the job's bookkeeping, where it takes the spec 03 I69 debt.
    ``finished_at`` alone is not that: a requested kill stamps it before the runner sees the exit,
    and stopping the runner in between reads as a broker shutdown that leaves the job to re-adoption."""
    return job.finished_at is not None and srv.current_job_id != job.id


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


def _runner_done(job):
    """The runner has booked the job's end. A kill sets ``finished_at`` before the runner sees the
    exit, and a runner cancelled before then leaves the job for re-adoption, owing no reset."""
    return job.finished_at is not None and srv.current_job_id != job.id


async def _stop(runner):
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


def _slow_exit(monkeypatch):
    """A loaded host: the runner sees the job's streams close well after its processes are gone."""
    real = srv.aio.wait_for

    async def late(aw, timeout):
        result = await real(aw, timeout)
        await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(srv.aio, "wait_for", late)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True], ids=["prompt", "slow-exit"])
async def test_a_requested_kill_of_a_running_job_owes_the_reset(runner_env, monkeypatch, slow):
    if slow:
        _slow_exit(monkeypatch)
    job, runner = await _start("931", "sleep 600 | sleep 600")
    try:
        assert await _until(lambda: job.status is srv.JobStatus.RUNNING and job.pid)
        assert await _until(lambda: len(_pgid_members(job.pid)) >= 2)
        mcp = srv.create_mcp_server()
        await mcp.call_tool("tt_device_job_kill", {"params": {"job_id": "931", "owner": job.owner}})
        assert await _until(lambda: _runner_done(job))
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
        assert await _until(lambda: _runner_done(job))
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.COMPLETED and srv._kill_reset_debt() == ""

    job, runner = await _start("933", "sleep 600")
    try:
        assert await _until(lambda: job.status is srv.JobStatus.RUNNING and job.pid)
        srv.reset_killed_job_ids.add("933")  # our own reset took the device from it
        os.killpg(job.pid, 9)
        assert await _until(lambda: _runner_done(job))
    finally:
        await _stop(runner)
    assert srv._kill_reset_debt() == "", "a death our own reset caused owes no second reset"


def test_the_test_reset_drops_every_kill_marker(clear_job_state):
    """A runner stopped mid-job can leave its markers behind, and job ids recycle (001-999): a
    leftover reset-killed id would excuse a later test's job from the debt it owes."""
    srv.reset_killed_job_ids.add("001")
    srv.request_killed_job_ids.add("001")
    srv._owe_kill_reset("job 001 killed")
    srv._reset_for_testing()
    assert not srv.reset_killed_job_ids and not srv.request_killed_job_ids
    assert srv._kill_reset_debt() == ""


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
@pytest.mark.parametrize("slow", [False, True], ids=["prompt", "slow-exit"])
async def test_a_disconnect_kills_the_whole_process_group(runner_env, monkeypatch, slow):
    if slow:
        _slow_exit(monkeypatch)
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
        assert await _until(lambda: _runner_done(job)), "the job outlived its client"
        assert await _until(lambda: not _pgid_members(job.pid))
        assert await _until(srv._kill_reset_debt), "the runner reaps after the group is gone"
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


# ---------------------------------------------------------------- spec 03 I69: every broker kill


@pytest.mark.asyncio
async def test_a_hung_job_owes_the_reset(runner_env, monkeypatch):
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)
    job, runner = await _start("934", "echo working; sleep 600")
    try:
        assert await _until(lambda: _runner_done(job))
        assert await _until(srv._kill_reset_debt)
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.HUNG
    assert srv._kill_reset_debt().startswith("job 934"), "the hung reaper's kill owes the reset too"


def _recording_ladder(monkeypatch, graces):
    """_terminate_job stand-in: records the SIGINT window it was given, then ends the group
    through the real ladder with a short window so the test does not wait it out."""

    async def _ladder(job_id, pid, grace_sec=srv.GRACEFUL_KILL_GRACE_SEC):
        graces.append(grace_sec)
        return await srv._terminate_process_group(pid, grace_sec=0.2)

    monkeypatch.setattr(srv, "_terminate_job", _ladder)


@pytest.mark.asyncio
async def test_a_hung_reap_gives_sigint_the_short_window(runner_env, monkeypatch):
    """01 I10: a job silent for HUNG_SILENCE_SEC does not unwind on SIGINT (on a galaxy box both silence
    reaps ran the full 60 s and ended at SIGTERM), and it owes the reset either way. Its SIGINT
    window is HUNG_SIGINT_GRACE_SEC, not GRACEFUL_KILL_GRACE_SEC, and the reset debt stays."""
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)
    graces = []
    _recording_ladder(monkeypatch, graces)
    job, runner = await _start("936", "echo working; sleep 600")
    try:
        assert await _until(lambda: _runner_done(job))
        assert await _until(srv._kill_reset_debt)
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.HUNG
    assert graces == [srv.HUNG_SIGINT_GRACE_SEC], f"hung reap SIGINT window {graces}, want the short one"
    assert srv._kill_reset_debt().startswith("job 936"), "the short window must not drop the I69 reset"


@pytest.mark.asyncio
async def test_a_runner_timeout_keeps_the_full_sigint_window(runner_env, monkeypatch):
    """Only the silence reap is shortened: a job stopped at its time limit may be mid-run and
    unwinding, so it keeps GRACEFUL_KILL_GRACE_SEC."""
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)
    graces = []
    _recording_ladder(monkeypatch, graces)
    job, runner = await _start("937", "sleep 600", timeout_sec=1)
    try:
        assert await _until(lambda: _runner_done(job))
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.TIMEOUT
    assert graces == [srv.GRACEFUL_KILL_GRACE_SEC]


def test_the_hung_sigint_window_is_short_and_the_rest_of_the_ladder_unchanged():
    assert constants.HUNG_SIGINT_GRACE_SEC == 10
    assert constants.HUNG_SIGINT_GRACE_SEC < constants.GRACEFUL_KILL_GRACE_SEC
    assert constants.GRACEFUL_KILL_GRACE_SEC == 60, "non-hung kills keep the full SIGINT window"
    assert constants.SIGTERM_GRACE_SEC == 15
    assert constants.PREEMPT_GRACE_SEC == 60, "preemption's contention grace is not this change"


@pytest.mark.asyncio
async def test_a_runner_timeout_owes_the_reset(runner_env):
    job, runner = await _start("935", "sleep 600", timeout_sec=1)
    try:
        assert await _until(lambda: _runner_done(job))
        assert await _until(srv._kill_reset_debt)
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.TIMEOUT
    assert srv._kill_reset_debt().startswith("job 935")


@pytest.mark.asyncio
async def test_a_failed_job_that_left_the_clock_busy_owes_the_reset(runner_env, monkeypatch):
    """A child killed from outside the broker: the job's shell exits a plain 1, but its chips still
    read the busy AI clock, so it died with the device open and the gate resets first."""
    monkeypatch.setattr(srv, "_aiclk_left_busy", lambda *a, **k: {"0": 1350, "1": 1350})
    job, runner = await _start("936", "exit 1")
    try:
        assert await _until(lambda: _runner_done(job))
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.FAILED and job.exit_code == 1
    debt = srv._kill_reset_debt()
    assert debt.startswith("job 936 failed with 2 chip(s) left at the busy AI clock (max 1350 MHz)")
    assert "kill_reset_owed" in runner_env["events"]


OFF_BUS = 0xFFFFFFFF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clocks, want_debt",
    [
        ({"0": OFF_BUS, "1": 800}, ""),
        ({"0": 6000, "1": 800}, ""),
        ({"0": OFF_BUS, "1": 1350}, "job 938 failed with 1 chip(s) left at the busy AI clock (max 1350 MHz)"),
    ],
    ids=["off-bus-chip", "implausible-clock", "off-bus-beside-a-busy-chip"],
)
async def test_an_off_bus_clock_read_is_not_a_busy_clock(runner_env, monkeypatch, clocks, want_debt):
    """An off-bus chip reads 0xFFFFFFFF for its AI clock. That is not a chip left open: it owes no
    kill reset and no aiclk_busy_after_job record, and takes the off-bus path instead. A real busy
    chip beside it still owes the reset, counted and maxed without the off-bus read."""
    monkeypatch.setattr(srv, "chip_snapshot", lambda: {i: {"tt_aiclk": v} for i, v in clocks.items()})
    job, runner = await _start("938", "exit 1")
    try:
        assert await _until(lambda: _runner_done(job))
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.FAILED and job.exit_code == 1
    assert srv._kill_reset_debt().startswith(want_debt) and bool(srv._kill_reset_debt()) == bool(want_debt)
    assert ("kill_reset_owed" in runner_env["events"]) == bool(want_debt)
    assert ("aiclk_busy_after_job" in runner_env["events"]) == bool(want_debt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, busy, reset_killed",
    [("exit 1", {}, False), ("true", {"0": 1350}, False), ("exit 1", {"0": 1350}, True)],
    ids=["failed-idle-clock", "exit0-busy-clock", "our-reset-killed-it"],
)
async def test_no_clock_debt_for_a_closed_device_an_exit0_or_our_own_kill(
    runner_env, monkeypatch, command, busy, reset_killed
):
    """An idle clock means the job closed the device (the forced pass stays); an exit-0 job is never
    read and owes no reset; a death our own reset caused owes no second reset."""
    reads = []
    monkeypatch.setattr(srv, "_aiclk_left_busy", lambda *a, **k: reads.append(1) or dict(busy))
    if reset_killed:
        srv.reset_killed_job_ids.add("937")
    job, runner = await _start("937", command)
    try:
        assert await _until(lambda: _runner_done(job))
    finally:
        await _stop(runner)
    assert srv._kill_reset_debt() == ""
    if command == "true":
        assert reads == [], "an exit-0 job's clock is never read"


def _readopted_job(job_id, served_sec, timeout_sec):
    started = (srv.datetime.now() - srv.timedelta(seconds=served_sec)).isoformat()
    job = srv.Job(
        id=job_id,
        owner="o",
        workspace="/w",
        command="c",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        started_at=started,
    )
    job.timeout_sec = timeout_sec
    srv.jobs[job_id] = job
    return job


@pytest.mark.asyncio
async def test_a_readopted_job_past_its_timeout_owes_the_reset(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    active = {"v": True}
    monkeypatch.setattr(srv, "_scope_active", lambda scope: active["v"])

    async def fake_terminate(scope, grace_sec=0):
        active["v"] = False
        return False

    monkeypatch.setattr(srv, "_terminate_scope", fake_terminate)
    _readopted_job("940", 2400, 1500)
    await asyncio.wait_for(srv._monitor_readopted_scope("940", "ttdev-job-940.scope"), timeout=5)
    assert srv.jobs["940"].status is srv.JobStatus.TIMEOUT
    assert srv._kill_reset_debt().startswith("re-adopted job 940"), "a re-adopted timeout owes the reset"


@pytest.mark.asyncio
async def test_a_requested_kill_of_a_readopted_job_owes_the_reset_and_leaves_no_id(
    monkeypatch, tmp_path, clear_job_state
):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    job = _readopted_job("941", 10, 1500)
    job.status = srv.JobStatus.KILLED  # what _kill_job stamps before it stops the scope
    srv.request_killed_job_ids.add("941")
    await asyncio.wait_for(srv._monitor_readopted_scope("941", "ttdev-job-941.scope"), timeout=5)
    assert srv._kill_reset_debt().startswith("re-adopted job 941")
    assert "941" not in srv.request_killed_job_ids, "the id leaked past the job's end"


@pytest.mark.asyncio
async def test_the_runner_pays_an_owed_debt_before_a_queued_job(runner_env, monkeypatch):
    calls = []

    async def gate(job_log_file, job_failed=False, **k):
        calls.append((job_failed, srv.jobs["952"].status))
        srv._clear_kill_reset_debt()

    monkeypatch.setattr(srv, "_verify_device_after_job", gate)
    srv.readopted_scopes["950"] = "ttdev-job-950.scope"
    srv._owe_kill_reset("re-adopted job 950 timeout")
    job, runner = await _start("952", "true")
    try:
        await asyncio.sleep(0.3)
        assert calls == [] and job.status is srv.JobStatus.QUEUED, "nothing runs beside the re-adopted job"
        srv.readopted_scopes.pop("950")
        assert await _until(lambda: _runner_done(job))
    finally:
        await _stop(runner)
    assert calls[0] == (True, srv.JobStatus.QUEUED), "paid by the post-job gate, before the job's door"
    assert job.status is srv.JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_the_door_payment_waits_out_an_external_step(runner_env, monkeypatch):
    """The debt is paid after the external-step wait, just before the pre-job gate: a pre-step gate
    holding the device is never overlapped, and the payment's holder scan is taken after it."""
    calls = []

    async def gate(job_log_file, job_failed=False, **k):
        calls.append(srv.external_step_active)
        srv._clear_kill_reset_debt()

    monkeypatch.setattr(srv, "_verify_device_after_job", gate)
    srv._owe_kill_reset("re-adopted job 954 timeout")
    token = srv._reserve_external_step("pre-step 1.0")
    job, runner = await _start("955", "true")
    try:
        await asyncio.sleep(0.3)
        assert calls == [] and job.status is srv.JobStatus.QUEUED, "paid under a pre-step gate's holder scan"
        srv._release_external_step(token)
        assert await _until(lambda: job.finished_at is not None)
    finally:
        srv._release_external_step(token)
        await _stop(runner)
    assert calls[0] == "", "paid once the external step released"
    assert job.status is srv.JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_broker_kill_logs_the_process_tree_it_stopped(runner_env, monkeypatch, caplog):
    log = logging.getLogger("tt-device-mcp-test-kill")
    monkeypatch.setattr(srv, "logger", log)
    job, runner = await _start("953", "sleep 600 | sleep 600", timeout_sec=1)
    with caplog.at_level(logging.INFO, logger=log.name):
        try:
            assert await _until(lambda: _runner_done(job))
        finally:
            await _stop(runner)
    lines = [r.getMessage() for r in caplog.records if "KILLED process tree" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "job_id=953" in lines[0] and "reason=timeout after 1s" in lines[0]
    assert f"pgid {job.pid}" in lines[0] and "ladder ended at SIGINT" in lines[0]


@pytest.mark.asyncio
async def test_a_rest_run_bg_timeout_owes_the_reset(runner_env):
    app = srv.build_asgi_app(srv.create_mcp_server())
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://broker") as client:
        res = (
            await client.post(
                "/api/tt_device_job_run_bg", json={"workspace": "/tmp", "command": "sleep 600", "timeout_sec": 1}
            )
        ).json()
    job = srv.jobs[res["job_id"]]
    runner = asyncio.create_task(srv.job_runner())
    try:
        assert await _until(lambda: _runner_done(job))
        assert await _until(srv._kill_reset_debt)
    finally:
        await _stop(runner)
    assert job.status is srv.JobStatus.TIMEOUT
    assert srv._kill_reset_debt().startswith(f"job {job.id}"), "REST run-bg is the same runner: same debt"


@pytest.mark.asyncio
async def test_a_reaped_leftover_defers_the_debt_with_no_reset_and_no_wait(killed, monkeypatch):
    leftover = "device held by 1 leftover process(es) of reaped job(s): pid 4242 (job 7)"
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: leftover)
    started = srv.time.monotonic()
    await _gate()
    assert srv.time.monotonic() - started < 2.0, "the skip adds no wait"
    assert killed["resets"] == [] and killed["fabric"] == [] and killed["eth_reads"] == 0
    assert srv._kill_reset_debt() == "job 7 timeout", "the debt is kept for the gate after the leftover"
    assert srv.fsm.record.dirty, "still dirty: no tenant runs before the verify"
    # A host reboot ends the process, and the debt with it (memory only): the startup gate re-proves
    # the mesh with the full check, fabric included, and pays no kill reset.
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: "")
    srv._clear_kill_reset_debt()
    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert killed["resets"] == [] and killed["fabric"] == [0], "the startup full check ran its fabric pass"
    assert "kill_mesh_reset" not in killed["events"]


@pytest.mark.asyncio
async def test_a_debt_inside_a_failed_resets_cooldown_is_skipped_with_no_wait(killed, monkeypatch):
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_failed", True)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", srv._kill_reset_owed_at - 1.0)
    assert srv.recovery_mechanism.cooling() and srv._kill_reset_debt()
    started = srv.time.monotonic()
    await _gate()
    assert srv.time.monotonic() - started < 2.0, "the skip adds no wait"
    assert "kill_reset_skipped" in killed["events"] and "kill_mesh_reset" not in killed["events"]
    assert killed["resets"] == [], "no reset inside the cooldown"
    assert killed["fabric"] and killed["fabric"][0] == 0, "the dirty exit's full check ran instead"
    assert srv._kill_reset_debt() == "", "the debt is cleared, not carried"


# ------------------------------------------- spec 03 I69: preemption kills and re-adopted resets


async def _preempt_kill_a_group(argv, job_id):
    proc = await asyncio.create_subprocess_exec(*argv, start_new_session=True)
    try:
        await asyncio.sleep(0.2)  # let the shell set its traps
        await srv._preempt_kill(job_id, proc.pid)
        return await asyncio.wait_for(proc.wait(), timeout=5), proc.pid
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_a_preempt_kill_that_outlives_sigterm_logs_the_process_tree(monkeypatch, caplog, clear_job_state):
    """A preemption kill that has to go to SIGKILL is a kill like any other: one KILLED line naming
    the group and the step, and a warning that the tree outlived SIGTERM."""
    log = logging.getLogger("tt-device-mcp-test-preempt-kill")
    monkeypatch.setattr(srv, "logger", log)
    monkeypatch.setattr(srv, "PREEMPT_KILL_GRACE_SEC", 0.3)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    with caplog.at_level(logging.INFO, logger=log.name):
        rc, pid = await _preempt_kill_a_group(["sh", "-c", "trap '' TERM; sleep 600"], "960")
    assert rc == -9, "SIGKILL reached the group"
    lines = [r.getMessage() for r in caplog.records if "KILLED process tree" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "job_id=960" in lines[0] and "reason=preempted (a job waited 60s past its time limit)" in lines[0]
    assert f"pgid {pid}" in lines[0] and "ladder ended at SIGKILL (preempt kill)" in lines[0]
    assert any("survived SIGTERM" in r.getMessage() for r in caplog.records)
    assert "960" in srv.preempt_forced_job_ids, "a preemption kill is a broker-side timeout"


@pytest.mark.asyncio
async def test_every_preemption_kill_owes_the_reset(monkeypatch, caplog, clear_job_state):
    """Spec 03 I69: a job that had its minute and did not end itself was killed mid-run. Unless the
    kernel witnessed every process exit by its own code (witness=True, spec 01 I20), even a tree
    that goes on the first SIGTERM owes the mesh one reset plus the full check. Here nothing
    witnesses it (the re-adopted path), and SIGTERM's default action is a signal death anyway."""
    log = logging.getLogger("tt-device-mcp-test-preempt-term")
    monkeypatch.setattr(srv, "logger", log)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    with caplog.at_level(logging.INFO, logger=log.name):
        rc, _ = await _preempt_kill_a_group(["sleep", "600"], "962")
    assert rc == -signal.SIGTERM
    lines = [r.getMessage() for r in caplog.records if "KILLED process tree" in r.getMessage()]
    assert len(lines) == 1 and "ladder ended at SIGTERM (preempt kill)" in lines[0], lines
    assert "962" in srv.preempt_forced_job_ids


@pytest.mark.asyncio
async def test_a_reset_that_stops_a_readopted_job_logs_the_process_tree(monkeypatch, caplog, clear_job_state):
    log = logging.getLogger("tt-device-mcp-test-readopted-reset")
    monkeypatch.setattr(srv, "logger", log)

    async def reaped(scope, grace_sec=0):
        return True  # outlived SIGINT; `systemctl stop` reaped it

    monkeypatch.setattr(srv, "_terminate_scope", reaped)
    with caplog.at_level(logging.INFO, logger=log.name):
        await srv._stop_readopted_job_for_reset("961", "ttdev-job-961.scope")
    lines = [r.getMessage() for r in caplog.records if "KILLED process tree" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "job_id=961" in lines[0] and "reason=device reset" in lines[0]
    assert "stopped unit ttdev-job-961.scope; ladder ended at systemctl stop (survived SIGINT)" in lines[0]


@pytest.mark.asyncio
async def test_a_preemption_owes_and_pays_the_reset_before_the_next_job(runner_env, monkeypatch):
    """Spec 03 I69: a preemption kill owes one reset, then the full check, paid by the preempted
    job's own post-job gate before the waiting job is dispatched."""
    monkeypatch.setattr(srv, "PREEMPT_POLL_SEC", 0.05)
    monkeypatch.setattr(srv, "PREEMPT_GRACE_SEC", 1)
    monkeypatch.setattr(srv, "PREEMPT_KILL_GRACE_SEC", 0.5)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)
    calls = []

    async def gate(job_log_file, job_failed=False, **k):
        calls.append((job_failed, srv._kill_reset_debt(), srv.jobs["971"].status))
        srv._clear_kill_reset_debt()  # the gate's reset is what settles it

    monkeypatch.setattr(srv, "_verify_device_after_job", gate)
    preempted = srv.Job(
        id="970",
        owner="t",
        workspace="/tmp",
        command="trap '' INT TERM; sleep 30",  # ignores SIGTERM: only the kill's SIGKILL ends it
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["970"] = preempted
    await srv.get_job_queue().put("970")
    waiting, runner = await _start("971", "true")
    try:
        assert await _until(lambda: waiting.finished_at is not None, tries=600)
    finally:
        await _stop(runner)
    assert preempted.status is srv.JobStatus.PREEMPTED
    assert calls[0][0] is True and calls[0][1].startswith("job 970 preempted"), calls
    assert calls[0][2] is srv.JobStatus.QUEUED, "paid before the waiting job's door"
    assert "kill_reset_owed" in runner_env["events"]
    assert "970" not in srv.preempt_forced_job_ids, "the id leaked past the job's end"
    assert waiting.status is srv.JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_preemption_debt_resets_then_runs_the_full_check_before_release(mesh):  # noqa: F811
    """The gate pays a preemption's debt like a timeout's: one reset, then the full check, fabric included."""
    mesh["eth"] = ADVANCING
    fsm_dirty(srv, "job 8 ended preempted (exit -9)")
    srv._owe_kill_reset("job 8 preempted")
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]], "one present-mesh reset"
    assert mesh["fabric"] == [1], "the full check after the reset ran the fabric pass"
    assert srv.fsm.state is ServerState.HEALTHY
    assert srv._kill_reset_debt() == ""


@pytest.mark.asyncio
async def test_a_preemption_debt_whose_full_check_fails_stays_held(mesh):  # noqa: F811
    """A killed preemption whose full check after the reset fails (the fabric pass moves no
    traffic) never releases the device to the waiting job: it stays held and goes on to the
    bounded recovery."""
    mesh["eth"] = ADVANCING
    mesh["reset_heals"] = False
    fsm_dirty(srv, "job 9 ended preempted (exit -9)")
    srv._owe_kill_reset("job 9 preempted")
    await _gate()
    assert mesh["resets"][0] == ["tt-smi", "-glx_reset"], "the debt's reset ran first"
    assert mesh["fabric"] and min(mesh["fabric"]) >= 1, "every fabric pass ran after the reset"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed post-kill full check never releases"
    assert srv._device_unavailable_for_tenant(), "the waiting job must not get a mesh that failed its check"


@pytest.mark.asyncio
async def test_a_preempted_readopted_job_owes_the_reset(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    job = _readopted_job("963", 10, 1500)
    job.preempt_killed_monotonic = 1.0  # the monitor's preemption killed it
    srv.preempt_forced_job_ids.add("963")
    await asyncio.wait_for(srv._monitor_readopted_scope("963", "ttdev-job-963.scope"), timeout=5)
    assert srv.jobs["963"].status is srv.JobStatus.PREEMPTED
    assert srv._kill_reset_debt().startswith("re-adopted job 963")
    assert "963" not in srv.preempt_forced_job_ids, "the id leaked past the job's end"
