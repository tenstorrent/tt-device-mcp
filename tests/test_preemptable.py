# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""No timeout except on contention (PREEMPTABLE jobs).

While nobody waits the job is never stopped for time. Once its limit has passed and a job
waits, it is stopped (SIGINT, SIGTERM) and SIGKILLed PREEMPT_GRACE_SEC after the later of the
limit and the waiter's arrival. The job then ends through the normal completion path, so the
post-job gate (fabric included) runs before the waiting job is dispatched. Spec 01 I20.
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

    async def _gate(job_log_file, job_failed=False, **_):
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
        for _ in range(100):  # let it pass its limit with nobody waiting
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
    assert job.exit_code == 0, "a job that shut down inside the minute was killed anyway"
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
@pytest.mark.parametrize("key", ["no_timeout_except_on_contention", "preemptable"])
async def test_rest_submit_carries_the_option_into_the_job_and_its_log(monkeypatch, clear_job_state, tmp_path, key):
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
                key: True,  # "preemptable" is the earlier name and still accepted
            },
        ).json()
        assert "job_id" in res, res
        status = client.post("/api/tt_device_job_status", json={"job_id": res["job_id"]}).json()

    job = srv.jobs[res["job_id"]]
    assert job.preemptable is True and job.timeout_sec == 300
    assert status["no_timeout_except_on_contention"] is True and status["preempt"] == "PREEMPTABLE 5min"
    assert status["preemptable"] is True
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

    async def fake_preempt(job_id, pid, kill_at):
        stopped.append(job_id)
        active["v"] = False

    monkeypatch.setattr(srv, "_preempt_job", fake_preempt)
    _readopted("021", served_sec=2400, timeout_sec=300)
    _waiting_job("951")

    await asyncio.wait_for(srv._monitor_readopted_scope("021", "ttdev-job-021.scope"), timeout=5)
    assert stopped == ["021"]
    assert srv.jobs["021"].status is srv.JobStatus.PREEMPTED


class _RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *a, **k):
        self.warnings.append(msg)

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.mark.asyncio
async def test_the_stop_window_runs_to_its_end_after_the_shell_exits(monkeypatch, clear_job_state):
    """The shell exits on the SIGINT while a child that ignores it still holds the device. The
    streams close at once, but the window is not cut short: the child gets the window's SIGKILL,
    not just the survivor sweep's much shorter grace."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _recording_post_job_gate(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=3)
    log = _RecordingLogger()
    monkeypatch.setattr(srv, "logger", log)
    _waiting_job()

    job = srv.Job(
        id="945",
        owner="t",
        workspace="/tmp",
        command="(trap '' INT TERM; sleep 30) & trap 'exit 0' INT; while true; do sleep 0.1; done",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["945"] = job
    await srv.get_job_queue().put("945")
    await _run_until_finished(job, limit_sec=20)

    assert job.status is srv.JobStatus.PREEMPTED
    assert any("PREEMPT job_id=945 still running" in w for w in log.warnings), log.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("rc", ["130", "-2", None])
async def test_a_preempted_readopted_job_never_releases_the_device_clean(monkeypatch, tmp_path, clear_job_state, rc):
    """A re-adopted job has no post-job gate: the runner that would have proved the fabric died with
    the old broker. Its SIGINT exit (130 / -2) is a clean release only where that gate follows, so
    here the device must be flagged for the reset + full check (fabric included) before the queue
    moves on, whatever the exit status."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    marks = _recording_dirty(monkeypatch)
    monkeypatch.setattr(srv, "_mark_device_reported_fault", lambda *a, **k: marks.append("fault"))
    active = {"v": True}
    monkeypatch.setattr(srv, "_scope_active", lambda scope: active["v"])

    async def fake_preempt(job_id, pid, kill_at):
        if rc is not None:
            (tmp_path / f"{job_id}.exit").write_text(rc)
        active["v"] = False

    monkeypatch.setattr(srv, "_preempt_job", fake_preempt)
    _readopted("022", served_sec=2400, timeout_sec=300)
    _waiting_job("952")

    await asyncio.wait_for(srv._monitor_readopted_scope("022", "ttdev-job-022.scope"), timeout=5)
    assert srv.jobs["022"].status is srv.JobStatus.PREEMPTED
    assert len(marks) == 1 and "022" in marks[0], f"a preempted re-adopted job released the device clean: {marks}"


def _waiting_since(job_id, ago_sec):
    j = _waiting_job(job_id)
    j.queued_at = (srv.datetime.now() - srv.timedelta(seconds=ago_sec)).isoformat()
    j.queued_monotonic = time.monotonic() - ago_sec
    return j


def _secs_left(kill_at):
    return kill_at - time.monotonic()


def test_no_kill_deadline_while_nobody_waits_even_long_past_the_limit(clear_job_state):
    assert srv._preempt_kill_deadline(time.monotonic() - 3600) is None


def test_no_kill_deadline_before_the_limit_even_with_a_waiter(clear_job_state):
    _waiting_since("960", ago_sec=30)
    assert srv._preempt_kill_deadline(time.monotonic() + 5) is None


def test_a_waiter_after_the_limit_starts_the_minute_at_its_arrival(clear_job_state):
    # Limit passed 10 s ago; the waiter arrived 3 s ago: kill 60 s after the arrival, 57 s from now,
    # however late the poll noticed.
    _waiting_since("961", ago_sec=3)
    left = _secs_left(srv._preempt_kill_deadline(time.monotonic() - 10))
    assert 56 <= left <= 57.5, left


def test_a_waiter_before_the_limit_starts_the_minute_at_the_limit(clear_job_state):
    # Waiting for 30 s; the limit passed 10 s ago: kill 60 s after the limit, 50 s from now.
    _waiting_since("962", ago_sec=30)
    left = _secs_left(srv._preempt_kill_deadline(time.monotonic() - 10))
    assert 49 <= left <= 50.5, left


def test_the_earliest_waiter_sets_the_deadline(clear_job_state):
    _waiting_since("963", ago_sec=2)
    _waiting_since("964", ago_sec=8)
    left = _secs_left(srv._preempt_kill_deadline(time.monotonic() - 20))
    assert 51 <= left <= 52.5, left


def test_a_wall_clock_step_does_not_move_the_kill_deadline(monkeypatch, clear_job_state):
    # The clock is monotonic: an NTP step or a DST change an hour forward or back while the job
    # runs neither kills it early nor gives it an extra hour.
    _waiting_since("965", ago_sec=3)
    limit_end = time.monotonic() - 10
    real = time.time
    for step in (3600, -3600):
        monkeypatch.setattr(srv.time, "time", lambda s=step: real() + s)
        left = _secs_left(srv._preempt_kill_deadline(limit_end))
        assert 56 <= left <= 57.5, (step, left)


def test_a_waiter_restored_after_a_restart_is_read_from_its_queued_at(clear_job_state):
    # A job restored from the queue spool has no monotonic arrival: its queued_at is read once.
    j = _waiting_since("966", ago_sec=30)
    j.queued_monotonic = None
    left = _secs_left(srv._preempt_kill_deadline(time.monotonic() - 10))
    assert 49 <= left <= 50.5, left


def test_the_earlier_field_name_is_still_accepted():
    params = srv.JobSubmitInput(workspace="/tmp", command="true", preemptable=True)
    assert params.no_timeout_except_on_contention is True


@pytest.mark.asyncio
async def test_a_waiter_after_the_limit_shows_the_countdown_then_sigkills_at_the_minute(monkeypatch, clear_job_state):
    """Past the limit, nobody waiting: PREEMPTABLE. A job queues: PREEMPTING <s>s counting down in
    queue status and /health, then SIGKILL exactly one window after the arrival for a job that
    ignores the stop. The device is flagged and the full post-job gate runs."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=3)

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
        _waiting_since("966", ago_sec=0)
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
    assert 2.5 <= elapsed <= 4.5, f"SIGKILL {elapsed:.1f}s after the waiter arrived, window is 3 s"
    counting = [lbl for lbl in labels if lbl.startswith("PREEMPTING ")]
    assert counting and counting[0] in ("PREEMPTING 3s", "PREEMPTING 2s"), labels
    secs = [int(lbl.split()[1][:-1]) for lbl in counting]
    assert secs == sorted(secs, reverse=True) and secs[-1] <= 1, f"countdown did not count down: {labels}"
    assert dirty, "a SIGKILLed job never released the device; it must be flagged for reset"
    assert gate == [{"job_failed": True}]


@pytest.mark.asyncio
async def test_a_waiter_before_the_limit_starts_the_countdown_at_the_limit(monkeypatch, clear_job_state):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _recording_post_job_gate(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=1)
    _waiting_since("967", ago_sec=0)

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
        while job.preempt_deadline_monotonic is None:
            await asyncio.sleep(0.02)
        seen["kill_wall"] = time.time() + (job.preempt_deadline_monotonic - time.monotonic())
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
    srv.jobs["971"].preempt_deadline_monotonic = time.monotonic() + 59.2
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


@pytest.mark.asyncio
async def test_a_silent_preemptable_job_is_not_reaped_while_nobody_waits(monkeypatch, clear_job_state):
    """No timeout except on contention covers silence too: a quiet job (a server between
    requests) past both its limit and the silence limit runs on while nobody waits."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    _fast_preempt(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)

    job = srv.Job(
        id="970",
        owner="t",
        workspace="/tmp",
        command="echo working; sleep 3; echo done",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["970"] = job
    await srv.get_job_queue().put("970")
    await _run_until_finished(job, limit_sec=10)

    assert job.status is srv.JobStatus.COMPLETED, f"a silent uncontended job ended {job.status.value}"
    assert "done" in job.output
    assert gate == [{"job_failed": False}]


@pytest.mark.asyncio
async def test_a_silent_preemptable_job_is_reaped_once_a_job_waits(monkeypatch, clear_job_state):
    """With a job waiting the silence reaper applies as to any job, even before the time limit:
    a hung job must not hold the device the waiter needs. The reap is a failed end, so the full
    gate (fabric included) runs before the waiter is dispatched."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)
    _waiting_job()

    job = srv.Job(
        id="971",
        owner="t",
        workspace="/tmp",
        command="echo working; sleep 60",
        queued_at="t",
        timeout_sec=600,
        preemptable=True,
    )
    srv.jobs["971"] = job
    await srv.get_job_queue().put("971")
    await _run_until_finished(job, limit_sec=15)

    assert job.status is srv.JobStatus.HUNG, f"a silent contended job ended {job.status.value}"
    assert job.preempt_deadline_monotonic is None, "reaped for silence, not preempted for time"
    assert gate == [{"job_failed": True}]
