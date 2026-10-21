# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""No timeout except on contention (PREEMPTABLE jobs).

While nobody waits the job is never stopped for time. Contention = its limit has passed AND a
job waits. The broker sends no signal until contention has lasted PREEMPT_GRACE_SEC (a minute)
unbroken, so the job can see the waiter and end itself; a waiter that leaves inside the minute
causes nothing. When the minute runs out the job's whole process tree is killed (SIGTERM, then
SIGKILL PREEMPT_KILL_GRACE_SEC later at the latest), the job ends PREEMPTED (a wedge-risk exit),
and the device is reset and fully checked, fabric included, before the waiting job is
dispatched. Every clock is time.monotonic(). Spec 01 I16.
"""

import asyncio
import os
import signal
import time

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.cli import overview_lines


def _free_device_lock(monkeypatch):
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")


def _no_workspace_activation(monkeypatch):
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", None))


def _recording_post_job_gate(monkeypatch):
    """Stand-in for the post-job gate that records how it was asked to run, so a test can show the
    preempted job's end still reaches the full gate (job_failed forces the fabric pass)."""
    calls = []

    async def _gate(job_log_file, job_failed=False):
        calls.append({"job_failed": job_failed})

    monkeypatch.setattr(srv, "_verify_device_after_job", _gate)
    return calls


def _recording_dirty(monkeypatch):
    marks = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **k: marks.append(reason))
    return marks


def _fast_preempt(monkeypatch, grace=60, kill_grace=1):
    monkeypatch.setattr(srv, "PREEMPT_POLL_SEC", 0.05)
    monkeypatch.setattr(srv, "PREEMPT_GRACE_SEC", grace)
    monkeypatch.setattr(srv, "PREEMPT_KILL_GRACE_SEC", kill_grace, raising=False)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)


def _recording_signals(monkeypatch, job):
    """Record every real signal (not the 0 probe) the broker sends to a process or group while
    ``job`` runs, with when (time.monotonic) it was sent, and pass it on. The runner's sweep of a
    finished job's group (after its exit status is in) is not a signal to the job."""
    sent = []
    real_killpg, real_kill = os.killpg, os.kill

    def killpg(pgid, sig):
        if sig and job.exit_code is None:
            sent.append((time.monotonic(), signal.Signals(sig)))
        return real_killpg(pgid, sig)

    def kill(pid, sig):
        if sig and job.exit_code is None:
            sent.append((time.monotonic(), signal.Signals(sig)))
        return real_kill(pid, sig)

    monkeypatch.setattr(srv.os, "killpg", killpg)
    monkeypatch.setattr(srv.os, "kill", kill)
    return sent


def _waiting_job(job_id="950"):
    """A job in the QUEUED state: contention. Kept out of the runner's queue so it is never
    dispatched; contention is read from the jobs table."""
    j = srv.Job(id=job_id, owner="other", workspace="/tmp", command="true", queued_at="t")
    j.queued_monotonic = time.monotonic()
    srv.jobs[job_id] = j
    return j


async def _until_running(job):
    for _ in range(100):
        await asyncio.sleep(0.05)
        if job.status is srv.JobStatus.RUNNING and job.started_at:
            return
    raise AssertionError(f"job never started: {job.status.value}")


async def _until_finished(job, limit_sec):
    t0 = time.monotonic()
    while job.finished_at is None and time.monotonic() - t0 < limit_sec:
        await asyncio.sleep(0.05)
    # The gate runs after finished_at is stamped; let the runner reach it.
    for _ in range(20):
        await asyncio.sleep(0.05)


def _gone(pid):
    """True once ``pid`` no longer runs (absent, or a zombie waiting to be reaped)."""
    for _ in range(40):
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


async def _run_until_finished(job, *, limit_sec):
    runner = asyncio.create_task(srv.job_runner())
    try:
        deadline = time.monotonic() + limit_sec
        while job.finished_at is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        # The gate runs after finished_at is stamped; let the runner reach it.
        for _ in range(20):
            await asyncio.sleep(0.05)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_preemptable_job_runs_past_its_limit_while_nobody_waits(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    _fast_preempt(monkeypatch)

    job = srv.Job(
        id="940",
        owner="tenant",
        workspace="/tmp",
        command="sleep 2; echo done",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["940"] = job
    await srv.get_job_queue().put("940")
    await _run_until_finished(job, limit_sec=10)

    assert job.status is srv.JobStatus.COMPLETED, f"uncontended preemptable job ended {job.status.value}"
    assert "done" in job.output
    assert gate == [{"job_failed": False}]


@pytest.mark.asyncio
async def test_an_ordinary_job_still_times_out_at_its_limit(monkeypatch, clear_job_state):
    """The contrast: without the option the same run is a TIMEOUT, as before."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _recording_post_job_gate(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch)

    job = srv.Job(id="941", owner="t", workspace="/tmp", command="sleep 3", queued_at="t", timeout_sec=1)
    srv.jobs["941"] = job
    await srv.get_job_queue().put("941")
    await _run_until_finished(job, limit_sec=10)

    assert job.status is srv.JobStatus.TIMEOUT


@pytest.mark.asyncio
async def test_contention_inside_the_limit_does_not_preempt(monkeypatch, clear_job_state):
    """The limit is still a guarantee: a job queued before it passes waits it out."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _recording_post_job_gate(monkeypatch)
    _fast_preempt(monkeypatch)
    _waiting_job()

    job = srv.Job(
        id="942",
        owner="t",
        workspace="/tmp",
        command="sleep 1; echo done",
        queued_at="t",
        timeout_sec=3,
        preemptable=True,
    )
    srv.jobs["942"] = job
    await srv.get_job_queue().put("942")
    await _run_until_finished(job, limit_sec=10)

    assert job.status is srv.JobStatus.COMPLETED
    assert job.preempt_killed_monotonic is None


@pytest.mark.asyncio
async def test_no_signal_before_the_minute_then_the_whole_tree_is_killed(monkeypatch, tmp_path, clear_job_state):
    """Review finding 1: nothing is sent to the job until contention has lasted the full minute.
    Then SIGTERM, SIGKILL at most PREEMPT_KILL_GRACE_SEC later, and every process of the job,
    a background child included, is gone. The device is flagged and the full gate runs."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=2, kill_grace=1)
    child = tmp_path / "child.pid"

    job = srv.Job(
        id="943",
        owner="server",
        workspace="/tmp",
        command=f"trap '' INT TERM; (trap '' INT TERM; sleep 60) & echo $! > {child}; echo serving; sleep 60",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["943"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("943")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        await asyncio.sleep(1.5)  # past the limit, nobody waiting
        assert sent == [], f"signalled with nobody waiting: {sent}"
        t0 = time.monotonic()
        _waiting_job()
        await _until_finished(job, limit_sec=10)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert job.status is srv.JobStatus.PREEMPTED, job.status.value
    assert sent, "never killed"
    first_at, first_sig = sent[0]
    assert first_at - t0 >= 2 - 0.15, f"signal {first_sig.name} {first_at - t0:.2f}s into the 2 s minute"
    assert first_sig is signal.SIGTERM, sent
    kills = [at for at, sig in sent if sig is signal.SIGKILL]
    assert kills and kills[0] - first_at <= 1 + 0.5, f"SIGKILL not within the kill bound: {sent}"
    assert job.exit_code in (-9, 137), job.exit_code
    assert _gone(int(child.read_text())), "the job's background child survived the preemption"
    assert "PREEMPTED" in job.error
    assert dirty, "a preempted job must be flagged for the reset + full check"
    assert gate == [{"job_failed": True}], "a preempted job must reach the full (fabric) post-job gate"


@pytest.mark.asyncio
async def test_a_job_that_ends_itself_inside_the_minute_is_a_normal_end(monkeypatch, tmp_path, clear_job_state):
    """The point of the minute: a job that sees the waiter and shuts down ends as any job does,
    never signalled, through the normal post-job gate."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=3)
    seen = tmp_path / "waiter-seen"

    job = srv.Job(
        id="944",
        owner="server",
        workspace="/tmp",
        # Stands in for a job polling the broker's queue: it shuts down when it sees a waiter.
        command=f"echo serving; while [ ! -e {seen} ]; do sleep 0.1; done; echo shutting-down",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["944"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("944")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        await asyncio.sleep(1.5)
        _waiting_job()
        await asyncio.sleep(1.0)  # well inside the 3 s minute
        assert srv.preempt_label(job).startswith("PREEMPTING "), srv.preempt_label(job)
        seen.touch()
        await _until_finished(job, limit_sec=10)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert job.status is srv.JobStatus.COMPLETED, job.status.value
    assert job.exit_code == 0 and "shutting-down" in job.output
    assert sent == [], f"a job that ended itself inside the minute was signalled: {sent}"
    assert gate == [{"job_failed": False}]
    assert dirty == []


@pytest.mark.asyncio
async def test_a_waiter_that_leaves_inside_the_minute_causes_no_preemption(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=2)

    job = srv.Job(
        id="947",
        owner="server",
        workspace="/tmp",
        command="sleep 4.5; echo done",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["947"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("947")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        await asyncio.sleep(1.2)
        waiter = _waiting_job()
        await asyncio.sleep(0.8)
        assert srv.preempt_label(job).startswith("PREEMPTING "), srv.preempt_label(job)
        waiter.status = srv.JobStatus.KILLED  # it gave up waiting
        await asyncio.sleep(0.2)
        assert srv.preempt_label(job) == "PREEMPTABLE 0.0min", "the countdown did not clear"
        assert job.contention_since_monotonic is None
        await _until_finished(job, limit_sec=10)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert job.status is srv.JobStatus.COMPLETED, job.status.value
    assert "done" in job.output
    assert sent == [], f"preempted after the waiter left: {sent}"
    assert gate == [{"job_failed": False}] and dirty == []


@pytest.mark.asyncio
async def test_a_waiter_that_leaves_after_the_kill_started_does_not_stop_it(monkeypatch, clear_job_state):
    """A kill once begun completes: SIGKILL, the device flagged, the full gate, whatever the
    waiter does. Msg: a timeout never leaves the device hung."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=1, kill_grace=1)
    waiter = _waiting_job()

    job = srv.Job(
        id="948",
        owner="server",
        workspace="/tmp",
        command="trap '' INT TERM; sleep 60",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["948"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("948")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.02)
        assert sent, "never killed"
        waiter.status = srv.JobStatus.KILLED
        assert srv.preempt_label(job) == "PREEMPTING 0s"
        await _until_finished(job, limit_sec=10)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert job.status is srv.JobStatus.PREEMPTED, job.status.value
    assert job.exit_code in (-9, 137), job.exit_code
    assert any(sig is signal.SIGKILL for _, sig in sent), sent
    assert dirty and gate == [{"job_failed": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("step_sec", [7200, -7200])
async def test_a_wall_clock_step_moves_no_deadline(monkeypatch, clear_job_state, step_sec):
    """Review finding 2: a wall-clock step (NTP, DST, an operator) neither starts nor delays a
    preemption. Forward: a 1 h limit is not 'passed'. Backward: a passed limit still is."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _recording_post_job_gate(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=1, kill_grace=1)
    real_time = time.time
    offset = {"v": 0}
    monkeypatch.setattr(srv.time, "time", lambda: real_time() + offset["v"])

    limit = 3600 if step_sec > 0 else 1
    job = srv.Job(
        id="949",
        owner="server",
        workspace="/tmp",
        command="sleep 60",
        queued_at="t",
        timeout_sec=limit,
        preemptable=True,
    )
    srv.jobs["949"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("949")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        _waiting_job()
        offset["v"] = step_sec
        t0 = time.monotonic()
        while job.finished_at is None and time.monotonic() - t0 < (2.5 if step_sec > 0 else 6):
            await asyncio.sleep(0.05)
        if step_sec > 0:
            assert job.status is srv.JobStatus.RUNNING, "a forward clock step preempted a job inside its limit"
            assert sent == [], sent
            assert srv.preempt_label(job) == "PREEMPTABLE 60min"
        else:
            assert job.status is srv.JobStatus.PREEMPTED, "a backward clock step delayed the preemption"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        if job.pid:
            try:
                os.killpg(job.pid, signal.SIGKILL)
            except OSError:
                pass


def test_the_kill_instant_ignores_the_wall_clock(monkeypatch, clear_job_state):
    job = srv.Job(id="1", owner="o", workspace="/", command="c", queued_at="", preemptable=True)
    w = _waiting_job("955")
    w.queued_monotonic = 110.0
    before = srv._preempt_kill_at(job, 100.0, now=120.0)
    monkeypatch.setattr(srv.time, "time", lambda: 0.0)  # the wall clock jumps to 1970
    assert srv._preempt_kill_at(job, 100.0, now=125.0) == before == 110.0 + srv.PREEMPT_GRACE_SEC


def test_preempt_labels(clear_job_state):
    job = srv.Job(id="1", owner="o", workspace="/", command="c", queued_at="", timeout_sec=300)
    assert srv.preempt_label(job) is None
    job.preemptable = True
    assert srv.preempt_label(job) == "PREEMPTABLE 5min"
    job.timeout_sec = 90
    assert srv.preempt_label(job) == "PREEMPTABLE 1.5min"
    job.status = srv.JobStatus.RUNNING
    assert srv.preempt_label(job) == "PREEMPTABLE 1.5min", "a countdown with nobody waiting"
    _waiting_job("957")
    job.contention_since_monotonic = time.monotonic() - srv.PREEMPT_GRACE_SEC + 41.5
    job.contention_waiters = frozenset({"957"})
    assert srv.preempt_label(job) == "PREEMPTING 42s"
    job.contention_since_monotonic -= 20
    assert srv.preempt_label(job) == "PREEMPTING 22s"
    srv.jobs["957"].status = srv.JobStatus.KILLED
    assert srv.preempt_label(job) == "PREEMPTABLE 1.5min", "the countdown outlived the waiter"
    job.preempt_killed_monotonic = time.monotonic()
    assert srv.preempt_label(job) == "PREEMPTING 0s"


def test_queue_status_and_cli_show_the_preempt_label(clear_job_state):
    job = srv.Job(
        id="945",
        owner="o",
        workspace="/w",
        command="serve",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        timeout_sec=300,
        preemptable=True,
    )
    srv.jobs["945"] = job
    row = next(r for r in srv._get_queue_status()["running"] if r["id"] == "945")
    assert row["preempt"] == "PREEMPTABLE 5min"

    _waiting_job("958")
    job.contention_since_monotonic = time.monotonic() - 30
    job.contention_waiters = frozenset({"958"})
    row = next(r for r in srv._get_queue_status()["running"] if r["id"] == "945")
    assert row["preempt"].startswith("PREEMPTING ")

    text = "\n".join(overview_lines([{**row, "owner": "o"}], [], []))
    assert "[PREEMPTING " in text and "serve" in text


def test_an_ordinary_job_row_has_no_preempt_label(clear_job_state):
    srv.jobs["946"] = srv.Job(
        id="946", owner="o", workspace="/w", command="c", queued_at="", status=srv.JobStatus.RUNNING
    )
    row = next(r for r in srv._get_queue_status()["running"] if r["id"] == "946")
    assert "preempt" not in row


@pytest.mark.parametrize("exit_code", [0, 1, -2, 130, -9, 137, -15, 143, None])
def test_preempted_wedge_risk(exit_code):
    """A preempted job was killed: whatever its exit status the device gets the reset + full check,
    fabric included, before the waiting job (msg: a timeout never leaves the device hung)."""
    assert srv._is_wedge_risk_exit(srv.JobStatus.PREEMPTED, exit_code) is True


@pytest.mark.asyncio
async def test_rest_submit_carries_the_option_into_the_job_and_its_log(monkeypatch, clear_job_state, tmp_path):
    srv.device_op_lock = None
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    # The option is recorded at submit time; nothing here needs the job to run. A real runner
    # would spawn it on the TestClient's loop and die with that loop, leaving current_process
    # set for later tests (and the job's process orphaned), so the runner stays idle.
    monkeypatch.setattr(srv, "job_runner_task", None)
    monkeypatch.setattr(srv, "job_runner", lambda: asyncio.Event().wait())
    app = srv.build_asgi_app(srv.create_mcp_server())

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        res = client.post(
            "/api/tt_device_job_run_bg",
            json={
                "workspace": "/tmp",
                "command": "sleep 100",
                "timeout_sec": 300,
                "no_timeout_except_on_contention": True,
            },
        ).json()
        assert "job_id" in res, res
        status = client.post("/api/tt_device_job_status", json={"job_id": res["job_id"]}).json()

    job = srv.jobs[res["job_id"]]
    assert job.preemptable is True and job.timeout_sec == 300
    assert status["no_timeout_except_on_contention"] is True and status["preempt"] == "PREEMPTABLE 5min"
    assert "PREEMPTABLE: yes" in open(job.log_file).read()
    # A restart re-reads the option from the header.
    assert srv._job_from_log(res["job_id"]).preemptable is True


def test_a_log_without_the_header_is_an_ordinary_job(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    (tmp_path / "2026-07-17_000956_012.log").write_text("JOB ID:      012\nTIMEOUT:     300s\n")
    assert srv._job_from_log("012").preemptable is False


def _readopted(job_id, served_sec, timeout_sec):
    started = (srv.datetime.now() - srv.timedelta(seconds=served_sec)).isoformat()
    job = srv.Job(
        id=job_id,
        owner="o",
        workspace="/w",
        command="serve",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        started_at=started,
        timeout_sec=timeout_sec,
        preemptable=True,
    )
    srv.jobs[job_id] = job
    return job


@pytest.mark.asyncio
async def test_a_readopted_preemptable_job_past_its_limit_is_left_alone_while_nobody_waits(
    monkeypatch, tmp_path, clear_job_state
):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    killed = []
    calls = {"n": 0}

    def scope_active(scope):
        calls["n"] += 1
        return calls["n"] < 20  # ends on its own after a few polls

    async def fake_terminate(*a, **k):
        killed.append(a)

    monkeypatch.setattr(srv, "_scope_active", scope_active)
    monkeypatch.setattr(srv, "_terminate_scope", fake_terminate)
    monkeypatch.setattr(srv, "_terminate_job", fake_terminate)
    _readopted("020", served_sec=2400, timeout_sec=300)

    await asyncio.wait_for(srv._monitor_readopted_scope("020", "ttdev-job-020.scope"), timeout=5)
    assert killed == [], "an uncontended preemptable job was stopped after a restart"
    assert srv.jobs["020"].status is not srv.JobStatus.TIMEOUT


@pytest.mark.asyncio
async def test_a_readopted_preemptable_job_is_killed_a_full_minute_after_a_job_waits(
    monkeypatch, tmp_path, clear_job_state
):
    """After a restart the limit is re-derived from the header once, and the minute starts afresh:
    nothing is sent before a full minute of contention, then the whole scope is killed."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "PREEMPT_GRACE_SEC", 1)
    _recording_dirty(monkeypatch)
    active = {"v": True}
    stopped = []
    monkeypatch.setattr(srv, "_scope_active", lambda scope: active["v"])

    async def fake_kill(job_id, pid):
        stopped.append((job_id, time.monotonic()))
        active["v"] = False

    monkeypatch.setattr(srv, "_preempt_kill", fake_kill)
    _readopted("021", served_sec=2400, timeout_sec=300)
    _waiting_job("951")

    t0 = time.monotonic()
    await asyncio.wait_for(srv._monitor_readopted_scope("021", "ttdev-job-021.scope"), timeout=5)
    assert [j for j, _ in stopped] == ["021"]
    assert stopped[0][1] - t0 >= 1 - 0.05, "killed before a full minute of contention after the restart"
    assert srv.jobs["021"].status is srv.JobStatus.PREEMPTED


@pytest.mark.asyncio
async def test_a_readopted_job_whose_waiter_leaves_inside_the_minute_is_left_alone(
    monkeypatch, tmp_path, clear_job_state
):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "PREEMPT_GRACE_SEC", 1)
    stopped = []
    calls = {"n": 0}
    waiter = _waiting_job("953")

    def scope_active(scope):
        calls["n"] += 1
        if calls["n"] == 20:
            waiter.status = srv.JobStatus.KILLED
        return calls["n"] < 200  # ends on its own, well after the minute would have run out

    async def fake_kill(job_id, pid):
        stopped.append(job_id)

    monkeypatch.setattr(srv, "_scope_active", scope_active)
    monkeypatch.setattr(srv, "_preempt_kill", fake_kill)
    monkeypatch.setattr(srv, "_terminate_scope", fake_kill)
    _readopted("023", served_sec=2400, timeout_sec=300)

    await asyncio.wait_for(srv._monitor_readopted_scope("023", "ttdev-job-023.scope"), timeout=10)
    assert stopped == []
    assert srv.jobs["023"].status is not srv.JobStatus.PREEMPTED


@pytest.mark.asyncio
@pytest.mark.parametrize("rc", ["130", "-2", None])
async def test_a_preempted_readopted_job_never_releases_the_device_clean(monkeypatch, tmp_path, clear_job_state, rc):
    """A re-adopted job has no post-job gate: the runner that would have proved the fabric died with
    the old broker. Whatever its exit status (a job may trap the signal and exit 130 / -2), the
    device must be flagged for the reset + full check (fabric included) before the queue
    moves on, whatever the exit status."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    marks = _recording_dirty(monkeypatch)
    monkeypatch.setattr(srv, "_mark_device_reported_fault", lambda *a, **k: marks.append("fault"))
    active = {"v": True}
    monkeypatch.setattr(srv, "_scope_active", lambda scope: active["v"])

    monkeypatch.setattr(srv, "PREEMPT_GRACE_SEC", 0)

    async def fake_kill(job_id, pid):
        if rc is not None:
            (tmp_path / f"{job_id}.exit").write_text(rc)
        active["v"] = False

    monkeypatch.setattr(srv, "_preempt_kill", fake_kill)
    _readopted("022", served_sec=2400, timeout_sec=300)
    _waiting_job("952")

    await asyncio.wait_for(srv._monitor_readopted_scope("022", "ttdev-job-022.scope"), timeout=5)
    assert srv.jobs["022"].status is srv.JobStatus.PREEMPTED
    assert len(marks) == 1 and "022" in marks[0], f"a preempted re-adopted job released the device clean: {marks}"


def _queued_at(job_id, mono):
    j = _waiting_job(job_id)
    j.queued_monotonic = mono
    return j


def _pjob():
    return srv.Job(id="1", owner="o", workspace="/", command="c", queued_at="", preemptable=True)


def test_no_kill_instant_while_nobody_waits_even_long_past_the_limit(clear_job_state):
    assert srv._preempt_kill_at(_pjob(), 100.0, now=3700.0) is None


def test_no_kill_instant_before_the_limit_even_with_a_waiter(clear_job_state):
    _queued_at("960", 70.0)
    job = _pjob()
    assert srv._preempt_kill_at(job, 100.0, now=95.0) is None
    assert job.contention_since_monotonic is None


def test_a_waiter_after_the_limit_starts_the_minute_at_its_arrival(clear_job_state):
    # Limit at 100; the waiter arrived at 107; noticed late, at 110: the minute still runs from 107.
    _queued_at("961", 107.0)
    assert srv._preempt_kill_at(_pjob(), 100.0, now=110.0) == 107.0 + srv.PREEMPT_GRACE_SEC


def test_a_waiter_before_the_limit_starts_the_minute_at_the_limit(clear_job_state):
    _queued_at("962", 80.0)
    assert srv._preempt_kill_at(_pjob(), 100.0, now=110.0) == 100.0 + srv.PREEMPT_GRACE_SEC


def test_the_earliest_waiter_sets_the_minute(clear_job_state):
    _queued_at("963", 118.0)
    _queued_at("964", 112.0)
    assert srv._preempt_kill_at(_pjob(), 100.0, now=120.0) == 112.0 + srv.PREEMPT_GRACE_SEC


def test_a_later_waiter_does_not_restart_a_running_minute(clear_job_state):
    job = _pjob()
    _queued_at("965", 110.0)
    assert srv._preempt_kill_at(job, 100.0, now=111.0) == 110.0 + srv.PREEMPT_GRACE_SEC
    _queued_at("966", 130.0)
    assert srv._preempt_kill_at(job, 100.0, now=131.0) == 110.0 + srv.PREEMPT_GRACE_SEC


def test_the_minute_restarts_with_a_new_waiter_after_the_last_one_left(clear_job_state):
    job = _pjob()
    a = _queued_at("967", 110.0)
    assert srv._preempt_kill_at(job, 100.0, now=120.0) == 110.0 + srv.PREEMPT_GRACE_SEC
    a.status = srv.JobStatus.KILLED
    assert srv._preempt_kill_at(job, 100.0, now=130.0) is None
    assert job.contention_since_monotonic is None and srv.preempt_label(job).startswith("PREEMPTABLE ")
    _queued_at("968", 140.0)
    assert srv._preempt_kill_at(job, 100.0, now=145.0) == 140.0 + srv.PREEMPT_GRACE_SEC


def test_a_waiter_swap_between_polls_restarts_the_minute(clear_job_state):
    """A leaves and B arrives between two polls: the poll never saw the queue empty, but none of
    the waiters it knew remains, so the minute starts afresh at B's arrival (the running job is
    never cut short by a gap the broker did not see)."""
    job = _pjob()
    a = _queued_at("969", 110.0)
    assert srv._preempt_kill_at(job, 100.0, now=120.0) == 110.0 + srv.PREEMPT_GRACE_SEC
    a.status = srv.JobStatus.KILLED
    _queued_at("970", 125.0)
    assert srv._preempt_kill_at(job, 100.0, now=130.0) == 125.0 + srv.PREEMPT_GRACE_SEC


@pytest.mark.asyncio
async def test_a_waiter_after_the_limit_shows_the_countdown_then_sigkills_at_the_minute(monkeypatch, clear_job_state):
    """Past the limit, nobody waiting: PREEMPTABLE. A job queues: PREEMPTING <s>s counting down in
    queue status and /health, then the kill one minute after the arrival; a job that ignores
    SIGTERM is SIGKILLed PREEMPT_KILL_GRACE_SEC later. The device is flagged and the full
    post-job gate runs."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=3, kill_grace=0.5)

    job = srv.Job(
        id="965",
        owner="server",
        workspace="/tmp",
        command="trap '' INT TERM; echo serving; sleep 60",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["965"] = job
    await srv.get_job_queue().put("965")

    runner = asyncio.create_task(srv.job_runner())
    labels = []
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if job.status is srv.JobStatus.RUNNING and job.started_at:
                break
        await asyncio.sleep(2.0)  # well past the 1 s limit, nobody waiting
        assert job.status is srv.JobStatus.RUNNING, "stopped for time with nobody waiting"
        assert srv._health_payload()["preempt"] == "PREEMPTABLE 0.0min"

        t0 = time.monotonic()
        _waiting_job("966")
        while job.finished_at is None and time.monotonic() - t0 < 10:
            row = next((r for r in srv._get_queue_status()["running"] if r["id"] == "965"), None)
            if row:
                labels.append(row["preempt"])
            await asyncio.sleep(0.1)
        elapsed = time.monotonic() - t0
        for _ in range(20):
            await asyncio.sleep(0.05)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert job.status is srv.JobStatus.PREEMPTED
    assert job.exit_code in (-9, 137), f"expected a SIGKILL, got exit {job.exit_code}"
    assert 3.3 <= elapsed <= 4.5, f"SIGKILL {elapsed:.1f}s after the waiter arrived; minute 3 s + kill 0.5 s"
    counting = [lbl for lbl in labels if lbl.startswith("PREEMPTING ")]
    assert counting and counting[0] in ("PREEMPTING 3s", "PREEMPTING 2s"), labels
    secs = [int(lbl.split()[1][:-1]) for lbl in counting]
    assert secs == sorted(secs, reverse=True) and secs[-1] <= 1, f"countdown did not count down: {labels}"
    assert "PREEMPTING 0s" in labels, f"the kill's SIGTERM window did not show PREEMPTING 0s: {labels}"
    assert dirty, "a SIGKILLed job never released the device; it must be flagged for reset"
    assert gate == [{"job_failed": True}]


@pytest.mark.asyncio
async def test_a_waiter_before_the_limit_starts_the_countdown_at_the_limit(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _recording_post_job_gate(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=1)
    _waiting_job("967")

    job = srv.Job(
        id="968",
        owner="t",
        workspace="/tmp",
        command="trap '' INT TERM; sleep 60",
        queued_at="t",
        timeout_sec=2,
        preemptable=True,
    )
    srv.jobs["968"] = job
    await srv.get_job_queue().put("968")
    seen = {}

    async def watch():
        while job.contention_since_monotonic is None:
            await asyncio.sleep(0.02)
        seen["kill_wall"] = time.time() + (job.contention_since_monotonic + srv.PREEMPT_GRACE_SEC - time.monotonic())
        seen["noticed_wall"] = time.time()

    watcher = asyncio.create_task(watch())
    await _run_until_finished(job, limit_sec=15)
    watcher.cancel()

    started = srv.datetime.fromisoformat(job.started_at).timestamp()
    assert job.status is srv.JobStatus.PREEMPTED
    assert seen["noticed_wall"] - started >= 1.9, "preempted before its limit"
    # Kill deadline = start + limit + window (the waiter was already there).
    assert abs(seen["kill_wall"] - (started + 2 + 1)) < 0.5, seen["kill_wall"] - started


@pytest.mark.asyncio
async def test_the_full_post_job_gate_runs_before_the_waiting_job_gets_the_device(monkeypatch, clear_job_state):
    """Goal: after a preempted job, the full gate (fabric forced) passes before the next job starts."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=1)
    order = []

    async def _gate(job_log_file, job_failed=False):
        order.append(("gate", job_failed, waiter.status))

    monkeypatch.setattr(srv, "_verify_device_after_job", _gate)

    job = srv.Job(
        id="969",
        owner="t",
        workspace="/tmp",
        command="trap 'exit 0' INT; while true; do sleep 0.1; done",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["969"] = job
    await srv.get_job_queue().put("969")
    waiter = srv.Job(id="970", owner="o", workspace="/tmp", command="echo next", queued_at="t")
    srv.jobs["970"] = waiter
    await srv.get_job_queue().put("970")
    await _run_until_finished(waiter, limit_sec=15)

    assert job.status is srv.JobStatus.PREEMPTED
    assert waiter.status is srv.JobStatus.COMPLETED
    assert order[0] == ("gate", True, srv.JobStatus.QUEUED), order


def test_health_shows_the_label_only_for_a_running_preemptable_job(clear_job_state):
    srv.jobs["971"] = srv.Job(
        id="971", owner="o", workspace="/w", command="c", queued_at="", status=srv.JobStatus.RUNNING
    )
    assert "preempt" not in srv._health_payload()
    srv.jobs["971"].preemptable = True
    srv.jobs["971"].timeout_sec = 600
    payload = srv._health_payload()
    assert payload["preempt"] == "PREEMPTABLE 10min" and payload["preempt_job_id"] == "971"
    _waiting_job("972")
    srv.jobs["971"].contention_since_monotonic = time.monotonic() - 0.8
    srv.jobs["971"].contention_waiters = frozenset({"972"})
    assert srv._health_payload()["preempt"] == "PREEMPTING 60s"


@pytest.mark.parametrize("flag", ["--no-timeout-except-on-contention", "--preemptable"])
@pytest.mark.parametrize("sub", ["run-bg", "run"])
def test_the_cli_flag_reads_no_timeout_except_on_contention(monkeypatch, flag, sub):
    import sys

    import tt_device_mcp.cli as cli

    sent = {}

    def fake_call(port, tool, payload):
        sent.update(payload)
        return {"error": "stop here"}

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    monkeypatch.setattr(cli, "mcp_tool_call", fake_call)
    monkeypatch.setattr(sys, "argv", ["tt-device-mcp", sub, flag, "-t", "120", "true"])
    try:
        cli.main()
    except SystemExit:
        pass
    assert sent.get("no_timeout_except_on_contention") is True, sent
    assert sent.get("timeout_sec") == 120


def test_the_option_is_documented_under_its_name():
    import tt_device_mcp.cli as cli

    doc = srv.JobSubmitInput.model_fields["no_timeout_except_on_contention"].description
    for text in (doc, cli._NO_TIMEOUT_HELP):
        assert text.lower().startswith("no timeout except on contention")
        assert "watch the broker" in text.lower()
        assert "PREEMPTABLE <x>min" in text and "PREEMPTING <s>s" in text


def test_sigkill_comes_ten_seconds_after_sigterm_and_the_docs_say_so():
    """The job gets 10 s between SIGTERM and SIGKILL, not 5: the broker's constant, the spec and the
    README state the same number."""
    from pathlib import Path

    from tt_device_mcp import constants

    assert constants.PREEMPT_KILL_GRACE_SEC == 10
    assert srv.PREEMPT_KILL_GRACE_SEC == constants.PREEMPT_KILL_GRACE_SEC
    root = Path(__file__).resolve().parents[1]
    readme = " ".join((root / "README.md").read_text().split())
    spec = " ".join((root / "specs" / "01-jobs.md").read_text().split())
    assert "(SIGTERM, then SIGKILL at most 10 s later)" in readme
    assert "SIGKILL `PREEMPT_KILL_GRACE_SEC`, 10 s, later at the latest" in spec
