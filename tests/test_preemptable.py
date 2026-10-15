# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""PREEMPTABLE jobs: timeout_sec is a soft limit, enforced only while another job waits.

Past the limit a preemptable job runs on for as long as nobody is queued. The first queued
job starts a bounded stop window (PREEMPT_GRACE_SEC): SIGINT now, SIGKILL at the window's end.
The job then ends through the normal completion path, so the post-job gate (fabric included)
runs before the waiting job is dispatched. Spec 01 I16.
"""

import asyncio
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


def _fast_preempt(monkeypatch, grace=60):
    monkeypatch.setattr(srv, "PREEMPT_POLL_SEC", 0.05)
    monkeypatch.setattr(srv, "PREEMPT_GRACE_SEC", grace)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 0)


def _waiting_job(job_id="950"):
    """A job in the QUEUED state: contention. Kept out of the runner's queue so it is never
    dispatched; contention is read from the jobs table."""
    j = srv.Job(id=job_id, owner="other", workspace="/tmp", command="true", queued_at="t")
    srv.jobs[job_id] = j
    return j


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
    assert job.preempt_deadline_monotonic is None


@pytest.mark.asyncio
async def test_contention_past_the_limit_stops_the_job_and_still_runs_the_full_gate(monkeypatch, clear_job_state):
    """A waiting job + a passed limit = a graceful stop. The job's clean exit is PREEMPTED, not a
    wedge, and the post-job gate still runs with the fabric forced before anyone else gets on."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch)

    job = srv.Job(
        id="943",
        owner="server",
        workspace="/tmp",
        # Shuts down on SIGINT, the way a job watching the broker should.
        command="trap 'echo shutting-down; exit 0' INT; echo serving; while true; do sleep 0.1; done",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["943"] = job
    await srv.get_job_queue().put("943")

    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(100):  # let it pass its soft limit with nobody waiting
            await asyncio.sleep(0.05)
            if job.status is srv.JobStatus.RUNNING and job.started_at:
                break
        await asyncio.sleep(1.5)
        assert job.status is srv.JobStatus.RUNNING, "preempted with nobody waiting"
        assert srv.preempt_label(job) == "PREEMPTABLE 0.0min"

        t0 = time.monotonic()
        _waiting_job()
        for _ in range(200):
            if job.finished_at is not None:
                break
            await asyncio.sleep(0.05)
        for _ in range(20):
            await asyncio.sleep(0.05)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert job.status is srv.JobStatus.PREEMPTED, job.status.value
    assert time.monotonic() - t0 < 5, "the stop window did not start when the job queued"
    assert "shutting-down" in job.output
    assert "PREEMPTED" in job.error
    assert gate == [{"job_failed": True}], "a preempted job must reach the full (fabric) post-job gate"
    assert dirty == [], "a job that shut down on the stop was treated as a wedge"


@pytest.mark.asyncio
async def test_a_job_that_ignores_the_stop_is_killed_at_the_window_end(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=1)
    _waiting_job()

    job = srv.Job(
        id="944",
        owner="t",
        workspace="/tmp",
        command="trap '' INT TERM; sleep 30",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["944"] = job
    await srv.get_job_queue().put("944")
    t0 = time.monotonic()
    await _run_until_finished(job, limit_sec=15)

    assert job.status is srv.JobStatus.PREEMPTED
    assert time.monotonic() - t0 < 8, "a job ignoring the stop held the device past the window"
    assert dirty, "a job SIGKILLed at the window end never released the device; it must be flagged"
    assert gate == [{"job_failed": True}]


def test_preempt_labels():
    job = srv.Job(id="1", owner="o", workspace="/", command="c", queued_at="", timeout_sec=300)
    assert srv.preempt_label(job) is None
    job.preemptable = True
    assert srv.preempt_label(job) == "PREEMPTABLE 5min"
    job.timeout_sec = 90
    assert srv.preempt_label(job) == "PREEMPTABLE 1.5min"
    job.status = srv.JobStatus.RUNNING
    job.preempt_deadline_monotonic = time.monotonic() + 41.5
    assert srv.preempt_label(job) == "PREEMPTING 42s"
    job.preempt_deadline_monotonic = time.monotonic() - 5
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

    job.preempt_deadline_monotonic = time.monotonic() + 30
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


@pytest.mark.parametrize(
    "exit_code,risk",
    [(0, False), (1, False), (-2, False), (130, False), (-9, True), (137, True), (143, True), (None, True)],
)
def test_preempted_wedge_risk(exit_code, risk):
    """SIGINT is the stop we send and the one a job unwinds on; any other signal, or no status at
    all, means the device was not released."""
    assert srv._is_wedge_risk_exit(srv.JobStatus.PREEMPTED, exit_code) is risk


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
            json={"workspace": "/tmp", "command": "sleep 100", "timeout_sec": 300, "preemptable": True},
        ).json()
        assert "job_id" in res, res
        status = client.post("/api/tt_device_job_status", json={"job_id": res["job_id"]}).json()

    job = srv.jobs[res["job_id"]]
    assert job.preemptable is True and job.timeout_sec == 300
    assert status["preemptable"] is True and status["preempt"] == "PREEMPTABLE 5min"
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
async def test_a_readopted_preemptable_job_is_preempted_once_a_job_waits(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    active = {"v": True}
    stopped = []
    monkeypatch.setattr(srv, "_scope_active", lambda scope: active["v"])

    async def fake_preempt(job_id, pid):
        stopped.append(job_id)
        active["v"] = False

    monkeypatch.setattr(srv, "_preempt_job", fake_preempt)
    _readopted("021", served_sec=2400, timeout_sec=300)
    _waiting_job("951")

    await asyncio.wait_for(srv._monitor_readopted_scope("021", "ttdev-job-021.scope"), timeout=5)
    assert stopped == ["021"]
    assert srv.jobs["021"].status is srv.JobStatus.PREEMPTED
