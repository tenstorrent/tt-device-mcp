# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""No timeout except on contention (PREEMPTABLE jobs).

While nobody waits the job is never stopped for time. Contention = its limit has passed AND a
job waits. The broker sends no signal until contention has lasted PREEMPT_GRACE_SEC (a minute)
unbroken, so the job can see the waiter and end itself; a waiter that leaves inside the minute
causes nothing. When the minute runs out the job's whole process tree is killed (SIGTERM, then
SIGKILL PREEMPT_KILL_GRACE_SEC later at the latest) and the job ends PREEMPTED. If the kernel's
exit records show every process exited through its own code on SIGTERM, only the fast post-job
check runs; otherwise (SIGKILL, a signal death, no witness) it is a wedge-risk exit and the device
is reset and fully checked, fabric included, before the waiting job is dispatched. Every clock is
time.monotonic(). Spec 01 I20.
"""

import asyncio
import errno
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

import tt_device_mcp.server as srv
from tests.conftest import fsm_healthy, patch_recovery
from tt_device_mcp import exit_witness
from tt_device_mcp.cli import overview_lines
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.exit_witness import (
    PROC_CN_MCAST_IGNORE,
    PROC_CN_MCAST_LISTEN,
    PROC_EVENT_EXIT,
    PROC_EVENT_FORK,
    ExitWitness,
    parse_events,
)
from tt_device_mcp.fsm import ServerState


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
    assert job.preempt_yielded is False
    assert gate == [{"job_failed": False}], "an uncontended end keeps the fast check after every job"


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
@pytest.mark.parametrize("rc", [0, 3])
async def test_a_job_that_ends_itself_inside_the_minute_is_a_normal_end(monkeypatch, tmp_path, clear_job_state, rc):
    """The point of the minute: a job that sees the waiter and shuts down ends as any job does,
    never signalled. A self-yield that returns 0 gets no post-job check and no reset (spec 01 I20);
    a non-zero one takes the ordinary gate for its exit."""
    fsm_healthy(srv)
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
        command=f"echo serving; while [ ! -e {seen} ]; do sleep 0.1; done; echo shutting-down; exit {rc}",
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

    assert job.status is (srv.JobStatus.COMPLETED if rc == 0 else srv.JobStatus.FAILED), job.status.value
    assert job.exit_code == rc and "shutting-down" in job.output
    assert job.preempt_yielded is True
    assert sent == [], f"a job that ended itself inside the minute was signalled: {sent}"
    assert gate == ([] if rc == 0 else [{"job_failed": True}]), gate
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
        # The first signal is due about 2 s in (1 s limit + 1 s grace); a loaded host runs late.
        deadline = time.monotonic() + 15
        while not sent and time.monotonic() < deadline:
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
    """A preempted job not proven to have ended cleanly: whatever its exit status the device gets
    the reset + full check, fabric included, before the waiting job. Proven clean, it does not."""
    assert srv._is_wedge_risk_exit(srv.JobStatus.PREEMPTED, exit_code) is True
    assert srv._is_wedge_risk_exit(srv.JobStatus.PREEMPTED, exit_code, preempt_clean=True) is False


def _witness(monkeypatch, *, clean, ok=True, why="", status=None):
    """Stand in for the kernel's exit records: the verdict a test needs, without CAP_NET_ADMIN.
    ``status`` is the job's own exit status the witness reports (None: it cannot say)."""
    made = []

    class _Witness:
        def __init__(self, pgid, scope):
            self.ok, self.why, self.closed = ok, why, False
            made.append(self)

        def drain(self):
            pass

        def clean(self):
            return ok and clean

        def exit_status(self):
            return status if (ok and clean) else None

        def close(self):
            self.closed = True

    monkeypatch.setattr(srv, "ExitWitness", _Witness)
    return made


async def _preempt(
    monkeypatch, job_id, command, *, kill_grace, while_running=None, scope_sent=None, contender=_waiting_job
):
    """Run a preemptable job past its 1 s limit with a job waiting, through the minute (1 s here)
    and the kill, and let the runner reach the post-job gate. ``while_running`` is called once the
    job runs. ``scope_sent``, a list: the job runs in a live scope (_live_scope) and the list gets
    the signals sent to it. ``contender`` makes the contention once the job runs (default: a queued
    job). Returns the job, the signals sent to its group or a pid, the gate's calls and the dirty
    marks."""
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    owed = []
    monkeypatch.setattr(srv, "_owe_kill_reset", owed.append)
    _fast_preempt(monkeypatch, grace=1, kill_grace=kill_grace)
    job = srv.Job(
        id=job_id, owner="server", workspace="/tmp", command=command, queued_at="t", timeout_sec=1, preemptable=True
    )
    srv.jobs[job_id] = job
    sent = _recording_signals(monkeypatch, job)
    if scope_sent is not None:
        _live_scope(monkeypatch, job_id, lambda: job.pid, sent=scope_sent)
    await srv.get_job_queue().put(job_id)
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        if while_running is not None:
            while_running()
        contender()
        await _until_finished(job, limit_sec=15)
        for _ in range(20):
            await asyncio.sleep(0.05)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.PREEMPTED, job.status.value
    assert job_id not in srv.preempt_forced_job_ids, "the job's end must drop its preemption mark"
    return job, [sig for _, sig in sent], gate, dirty, owed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [1, 143, None])
async def test_a_preempted_job_that_ends_cleanly_on_sigterm_takes_the_fast_check(monkeypatch, clear_job_state, status):
    """Case (b): SIGTERM was sent and the kernel saw every process of the tree exit through its own
    code inside the 10 s grace. The job is still PREEMPTED (it did not end itself), but nothing
    was cut mid-transaction: no SIGKILL, no reset, only the fast post-job check. That holds for a
    non-zero own status and for one the witness cannot state (None): only a proven 0 skips it."""
    fsm_healthy(srv)
    made = _witness(monkeypatch, clean=True, status=status)
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "961", "sleep 60", kill_grace=5)
    assert sent and sent[0] is signal.SIGTERM and signal.SIGKILL not in sent, sent
    assert job.preempt_clean is True
    assert job.preempt_exit_status == status
    assert job.exit_code == 143, f"only a proven own 0 replaces the wrapper's 143: {job.exit_code}"
    assert made and made[0].closed, "the witness must be closed"
    assert dirty == [], "a clean end on SIGTERM must not owe a reset"
    assert owed == [], f"a clean end on SIGTERM must not owe the I69 reset: {owed}"
    assert gate == [{"job_failed": False}], "a clean end on SIGTERM takes the fast check"
    assert "ended cleanly on SIGTERM" in job.error


@pytest.mark.asyncio
async def test_a_preempted_job_that_returns_0_on_sigterm_gets_no_check_and_no_reset(monkeypatch, clear_job_state):
    """Spec 01 I20, case (b) with exit 0: the witness proves every process exited by its own code
    and the job's own status is 0. Nothing runs before the waiter: no fast check, no full check,
    no reset."""
    fsm_healthy(srv)
    made = _witness(monkeypatch, clean=True, status=0)
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "966", "sleep 60", kill_grace=5)
    assert sent and sent[0] is signal.SIGTERM and signal.SIGKILL not in sent, sent
    assert job.preempt_clean is True and job.preempt_exit_status == 0
    assert job.exit_code == 0, "a handled SIGTERM with exit 0 must reach the broker as exit 0"
    assert made and made[0].closed
    assert dirty == [] and owed == [], (dirty, owed)
    assert gate == [], f"a preempted job that returned 0 was checked: {gate}"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["held", "debt"])
async def test_a_preempted_exit_0_on_a_held_or_owing_device_keeps_its_gate(monkeypatch, clear_job_state, held):
    """The exit-0 skip never lifts a hold: a device held while the job ran, or owing an I69 reset
    for another reason, still goes through the post-job gate, where only a passing full check
    releases it."""
    srv._clear_device_dirty()
    fsm_healthy(srv)
    hold = None
    if held == "held":
        # Held during the job: a hold from before it would be cleared (or the job refused) at the door.
        def hold():
            srv.fsm.on_fault("probe_unhealthy", detail="fault while the job ran")
            assert srv.fsm.state is not ServerState.HEALTHY

    else:
        monkeypatch.setattr(srv, "_kill_reset_debt", lambda: "job 1 timeout")
    _witness(monkeypatch, clean=True, status=0)
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "967", "sleep 60", kill_grace=5, while_running=hold)
    assert job.preempt_clean is True and job.preempt_exit_status == 0
    assert gate and gate[-1] == {"job_failed": False}, f"the post-job gate was skipped on a held device: {gate}"


@pytest.mark.asyncio
async def test_a_preempted_exit_0_that_left_chips_at_the_busy_clock_still_owes_the_reset(monkeypatch, clear_job_state):
    """Reporting the proven 0 as exit code 0 changes no check: the post-job AI clock read still
    runs for a preempted job, and chips left at the busy clock still owe the I69 reset (whose
    debt then keeps the post-job gate), as they did while the job reported the wrapper's 143."""
    srv._clear_device_dirty()
    fsm_healthy(srv)
    reads = []
    monkeypatch.setattr(srv, "_aiclk_left_busy", lambda *a, **k: reads.append(1) or {"0": 1350})
    _witness(monkeypatch, clean=True, status=0)
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "968", "sleep 60", kill_grace=5)
    assert job.preempt_clean is True and job.preempt_exit_status == 0
    assert job.exit_code == 0
    assert reads, "the clock read was skipped for a preempted exit 0"
    assert owed and "busy AI clock" in owed[0], owed


@pytest.mark.asyncio
async def test_a_preempted_job_with_a_signal_death_is_reset_and_fully_checked(monkeypatch, clear_job_state):
    """Case (c): the tree was gone inside the 10 s grace, but a process of it died from the signal
    (the kernel's exit record says so): reset + full check, fabric included."""
    _witness(monkeypatch, clean=False, why="pid 4242 of the job died from a signal")
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "962", "sleep 60", kill_grace=5)
    assert signal.SIGKILL not in sent, sent
    assert job.preempt_clean is False
    assert dirty, "a signal death must owe the reset + full check"
    assert owed == ["job 962 preempted"], f"a killed preemption must owe the I69 reset: {owed}"
    assert gate == [{"job_failed": True}]


@pytest.mark.asyncio
async def test_a_preempted_job_that_needed_sigkill_is_reset_and_fully_checked(monkeypatch, clear_job_state):
    """Case (c): the tree outlived SIGTERM, so SIGKILL was sent. Whatever the witness would say,
    that is a kill: reset + full check."""
    fsm_healthy(srv)
    _witness(monkeypatch, clean=True, status=0)
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "963", "trap '' INT TERM; sleep 60", kill_grace=1)
    assert signal.SIGKILL in sent, sent
    assert job.preempt_clean is False
    assert job.exit_code in (-9, 137), f"a SIGKILLed job keeps its signal death: {job.exit_code}"
    assert dirty
    assert owed == ["job 963 preempted"], f"a killed preemption must owe the I69 reset: {owed}"
    assert gate == [{"job_failed": True}]


@pytest.mark.asyncio
async def test_an_unwitnessed_preemption_is_reset_and_fully_checked(monkeypatch, clear_job_state):
    """Case (c): the exit could not be observed (no connector permission, lost events, unreadable
    tree). In doubt, the job counts as killed: reset + full check."""
    _witness(monkeypatch, clean=True, ok=False, why="cannot subscribe to process events")
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "964", "sleep 60", kill_grace=5)
    assert signal.SIGKILL not in sent, sent
    assert job.preempt_clean is False
    assert dirty
    assert owed == ["job 964 preempted"], f"a killed preemption must owe the I69 reset: {owed}"
    assert gate == [{"job_failed": True}]


class _Boom(RuntimeError):
    pass


def _broken_witness(monkeypatch, where):
    """A witness that raises an unexpected error in ``where`` (init, drain or clean)."""
    made = []

    class _Witness:
        def __init__(self, pgid, scope):
            if where == "init":
                raise _Boom("connector exploded")
            self.ok, self.why, self.closed = True, "", False
            made.append(self)

        def drain(self):
            if where == "drain":
                raise _Boom("drain exploded")

        def clean(self):
            if where == "clean":
                raise _Boom("clean exploded")
            return True

        def exit_status(self):
            if where == "exit_status":
                raise _Boom("exit_status exploded")
            return 0

        def close(self):
            self.closed = True

    monkeypatch.setattr(srv, "ExitWitness", _Witness)
    return made


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["init", "drain", "clean", "exit_status"])
@pytest.mark.parametrize("ignores_term", [False, True])
async def test_a_witness_error_keeps_the_reset_debt_and_the_kill_completes(
    monkeypatch, clear_job_state, where, ignores_term
):
    """Case (c) by doubt: any unexpected error while witnessing (building it, draining it or
    reading its verdict or the job's status) makes the end not clean. The I69 debt stays (reset + full check, fabric
    included, before the waiter) and the kill still runs to the end: SIGTERM, then SIGKILL for a
    tree that outlives it."""
    made = _broken_witness(monkeypatch, where)
    command = "trap '' INT TERM; sleep 60" if ignores_term else "sleep 60"
    fsm_healthy(srv)
    job_id = f"97{['init', 'drain', 'clean', 'exit_status'].index(where)}{int(ignores_term)}"
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, job_id, command, kill_grace=1)
    assert sent and sent[0] is signal.SIGTERM, sent
    assert (signal.SIGKILL in sent) is ignores_term, sent
    assert job.preempt_clean is False and job.preempt_exit_status is None
    assert all(w.closed for w in made), "the witness must be closed"
    assert dirty, "a witness error must keep the reset + full check"
    assert owed == [f"job {job_id} preempted"], f"a witness error must keep the I69 reset: {owed}"
    assert gate == [{"job_failed": True}]


@pytest.mark.asyncio
async def test_a_failed_fast_check_after_a_clean_end_keeps_the_hold_and_escalates(monkeypatch, tmp_path):
    """After case (a) or (b) the post-job gate runs the same fast check as after any clean exit
    (job_failed=False). If it fails, the device is not handed on: it is held (RECOVERING) and the
    hold goes to the broker's bounded recovery, which lifts it only after a full check passes."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "exit 0")
    _free_device_lock(monkeypatch)
    srv._clear_device_dirty()
    fsm_healthy(srv)
    snapshots, resets = [], []

    def snapshot(n, **k):
        snapshots.append(n)
        return False, "chip 0: ARC not responding"

    async def reset(indices, log):
        resets.append(indices)
        return True

    monkeypatch.setattr(srv.health_monitor, "verify_device_health", snapshot)
    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    await srv._verify_device_after_job(None, job_failed=False)

    assert snapshots, "the fast check never ran"
    assert srv.fsm.state is not ServerState.HEALTHY, "a device that failed its fast check was released"
    assert srv.fsm.state is ServerState.RECOVERING, srv.fsm.state


def _event(what, data):
    body = struct.pack("=IIQ", what, 0, 0) + data
    cn = struct.pack("=IIIIHH", 1, 1, 0, 0, len(body), 0) + body
    return struct.pack("=IHHII", 16 + len(cn), 3, 0, 0, 0) + cn


def _fork(parent, child):
    return _event(PROC_EVENT_FORK, struct.pack("=iiii", parent, parent, child, child))


def _exit(pid, status, tgid=None):
    return _event(PROC_EVENT_EXIT, struct.pack("=iiII", pid, pid if tgid is None else tgid, status, 17))


class _Sock:
    def __init__(self, *bufs, error=None):
        self.bufs, self.error = list(bufs), error

    def recv(self, n):
        if self.bufs:
            return self.bufs.pop(0)
        if self.error:
            raise OSError(self.error, os.strerror(self.error))
        raise BlockingIOError

    def close(self):
        pass


def _fed(tree, *bufs, error=None):
    w = ExitWitness.__new__(ExitWitness)
    w.ok, w.why, w.tree, w.exits, w.signalled = True, "", set(tree), {}, set()
    w.pgid = min(tree) if tree else None
    w._lock, w._stop, w._reader = threading.RLock(), threading.Event(), None
    w._sock = _Sock(*bufs, error=error)
    return w


def test_the_witness_reads_kernel_exit_records():
    """The verdict from crafted connector messages: forks from the tree join it, an own-code exit
    (low 7 bits zero) is clean, a signal death or an unseen process is not, a lost event voids it."""
    assert list(parse_events(_fork(10, 11) + _exit(11, 0) + _exit(12, 9, tgid=10))) == [
        ("fork", (10, 11)),
        ("exit", (11, 11, 0)),
        ("exit", (12, 10, 9)),
    ]
    clean = _fed({10}, _fork(10, 11) + _fork(99, 98), _exit(11, 0) + _exit(10, 143 << 8) + _exit(98, 9))
    assert clean.clean() is True and clean.tree == {10, 11}
    killed = _fed({10}, _fork(10, 11), _exit(11, 15) + _exit(10, 143 << 8))
    assert killed.clean() is False and "signal" in killed.why
    thread = _fed({10}, _exit(12, 9, tgid=10) + _exit(10, 0))
    assert thread.clean() is False, "a thread of the job died from a signal"
    unseen = _fed({10}, _fork(10, 11), _exit(10, 0))
    assert unseen.clean() is False and "no exit record" in unseen.why
    lost = _fed({10}, _exit(10, 0), error=errno.ENOBUFS)
    assert lost.clean() is False and "lost" in lost.why


def test_the_witness_reports_the_jobs_own_exit_status():
    """Spec 01 I20: the job's own status, from the kernel's exit records. The broker's job shell
    (the group leader, 10 here) exits 143 by its TERM trap, which is not the job's status; 0 only
    when every other process exited 0. None when the tree is not clean or only the shell was seen."""
    zero = _fed({10}, _fork(10, 11) + _fork(11, 12), _exit(12, 0) + _exit(11, 0) + _exit(10, 143 << 8))
    assert zero.exit_status() == 0
    one = _fed({10}, _fork(10, 11), _exit(11, 1 << 8) + _exit(10, 143 << 8))
    assert one.exit_status() == 1
    child_fails = _fed({10}, _fork(10, 11) + _fork(11, 12), _exit(12, 2 << 8) + _exit(11, 0) + _exit(10, 143 << 8))
    assert child_fails.exit_status() == 2, "any process of the job exiting non-zero is not 0"
    shell_only = _fed({10}, _exit(10, 143 << 8))
    assert shell_only.exit_status() is None, "only the broker's shell exited: the job's status is unknown"
    odd_shell = _fed({10}, _fork(10, 11), _exit(11, 0) + _exit(10, 7 << 8))
    assert odd_shell.exit_status() == 7, "a shell status other than its trap's is the job's"
    killed = _fed({10}, _fork(10, 11), _exit(11, 15) + _exit(10, 143 << 8))
    assert killed.exit_status() is None
    unseen = _fed({10}, _fork(10, 11), _exit(10, 143 << 8))
    assert unseen.exit_status() is None
    lost = _fed({10}, _fork(10, 11), _exit(11, 0), error=errno.ENOBUFS)
    assert lost.exit_status() is None
    no_group = _fed({10}, _fork(10, 11), _exit(11, 0) + _exit(10, 143 << 8))
    no_group.pgid = None
    assert no_group.exit_status() == 143, "with no known shell every status is the job's"


def test_closing_the_witness_unsubscribes_first():
    """close() tells the connector to stop (PROC_CN_MCAST_IGNORE) before closing the socket, so the
    kernel's host-wide listener count drops; a send error does not stop the close."""
    for error in (None, OSError(errno.EPERM, "no")):
        calls = []

        class _S:
            def send(self, msg):
                calls.append(("send", struct.unpack_from("=I", msg, len(msg) - 4)[0]))
                if error:
                    raise error

            def close(self):
                calls.append(("close", None))

        w = ExitWitness.__new__(ExitWitness)
        w._lock, w._stop, w._reader = threading.RLock(), threading.Event(), None
        w._sock = _S()
        w.close()
        assert calls == [("send", PROC_CN_MCAST_IGNORE), ("close", None)], calls
        assert w._sock is None
        w.close()  # idempotent


def test_the_witness_on_a_real_tree():
    """The kernel's own records, where this host lets the test subscribe (the broker runs as root):
    a process that exits on SIGTERM by its own handler is clean; one the signal kills is not."""
    handles = "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); time.sleep(30)"
    for argv, want in (([sys.executable, "-c", handles], True), (["sleep", "30"], False)):
        p = subprocess.Popen(argv, start_new_session=True)
        try:
            time.sleep(0.3)
            w = ExitWitness(p.pid, None)
            if not w.ok:
                pytest.skip(f"no process events here: {w.why}")
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=10)
            deadline = time.monotonic() + 2
            while w.clean() is not want and time.monotonic() < deadline:
                time.sleep(0.05)
            assert w.clean() is want, (argv[0], w.why)
            w.close()
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()


def test_the_witness_keeps_up_with_a_busy_host_between_polls():
    """A burst of process events larger than the receive buffer while the caller does not poll
    (an unprivileged socket holds a few hundred) must not void the witness: a reader thread
    drains it as events arrive."""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        w = ExitWitness(p.pid, None)
        if not w.ok:
            pytest.skip(f"no process events here: {w.why}")
        try:
            for _ in range(400):
                subprocess.run(["true"], check=True)
            time.sleep(0.5)
            assert w.ok, w.why
            p.terminate()
            p.wait(timeout=10)
            deadline = time.monotonic() + 2
            while p.pid not in w.exits and time.monotonic() < deadline:
                time.sleep(0.05)
            assert w.ok, w.why
            assert w.clean() is False and "signal" in w.why, "sleep dies from SIGTERM"
        finally:
            w.close()
        assert w._sock is None and not w._reader.is_alive()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_the_witness_fails_safe_in_a_pid_namespace(monkeypatch, tmp_path):
    """In a PID namespace below the host's (its namespace inode is not the host's fixed one, or
    NSpid lists more than one pid) the connector's events
    carry host pids that never match the tree read from the namespace's /proc. The witness says
    so (ok=False, before any subscription) instead of a misleading "no exit record", and its
    verdict is "not proven clean" with no exit status, which the broker treats as a killed job."""
    real = os.stat
    for ino, want in ((exit_witness._PROC_PID_INIT_INO, False), (0xF00010D0, True)):

        def stat(path, ino=ino):
            return type("_Stat", (), {"st_ino": ino}) if path == "/proc/self/ns/pid" else real(path)

        monkeypatch.setattr(exit_witness.os, "stat", stat)
        assert exit_witness._in_child_pid_namespace() is want, hex(ino)
    monkeypatch.setattr(exit_witness.os, "stat", real)
    # no namespace link: the NSpid line decides
    missing, status = str(tmp_path / "missing"), tmp_path / "status"
    for line, want in (
        ("NSpid:\t4242\n", False),
        ("NSpid:\t4242\t7\n", True),
        ("NSpid:\t4242\t7\t1\n", True),
        ("", False),  # no NSpid line (kernel before 4.1)
    ):
        status.write_text("Name:\tpython3\nPid:\t4242\n" + line)
        assert exit_witness._in_child_pid_namespace(missing, str(status)) is want, line
    assert exit_witness._in_child_pid_namespace(missing, missing) is False

    def _no_socket(*a):
        raise AssertionError("a namespaced witness must not subscribe")

    monkeypatch.setattr(exit_witness, "_in_child_pid_namespace", lambda: True)
    monkeypatch.setattr(exit_witness, "socket", type("_NoSockets", (), {"socket": staticmethod(_no_socket)}))
    w = ExitWitness(os.getpgid(0), None)
    why = "process events report host pids; the broker runs in a PID namespace"
    assert w.ok is False and w.why == why
    assert w.clean() is False and w.exit_status() is None and w.why == why
    w.close()


class _EventPipe:
    """A selectable stand-in for the connector socket: the test writes crafted events into the
    other end of a datagram socketpair. Records what the witness sends (its subscription)."""

    def __init__(self, end):
        self._end, self.sent = end, []

    def setsockopt(self, *a):
        pass

    def bind(self, addr):
        pass

    def send(self, msg):
        self.sent.append(struct.unpack_from("=I", msg, len(msg) - 4)[0])
        return len(msg)

    def setblocking(self, flag):
        self._end.setblocking(flag)

    def recv(self, n):
        return self._end.recv(n)

    def fileno(self):
        return self._end.fileno()

    def close(self):
        self._end.close()


def test_the_witness_matches_a_real_tree_against_injected_events(monkeypatch):
    """The witness's own setup and matching on a real process tree, with the kernel's connector
    replaced by injected events, so it runs anywhere (PID namespaces included, where the real
    events cannot match): it snapshots the job's group from /proc, its reader thread drains events
    as they arrive without the caller polling, a fork from the tree joins it, every process exiting
    by its own code is clean and a signal death is not; close() unsubscribes and stops the reader."""
    monkeypatch.setattr(exit_witness, "_in_child_pid_namespace", lambda: False)
    reads_stdin = [sys.executable, "-c", "import sys; sys.stdin.read()"]
    for argv, want in ((reads_stdin, True), (["sleep", "30"], False)):
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        pipe = _EventPipe(ours)
        fake = type("_Sockets", (), {"socket": staticmethod(lambda *a: pipe)})
        for name in ("AF_NETLINK", "SOCK_DGRAM", "SOL_SOCKET", "SO_RCVBUF"):
            setattr(fake, name, getattr(socket, name))
        monkeypatch.setattr(exit_witness, "socket", fake)
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, start_new_session=True)
        try:
            w = ExitWitness(p.pid, None)
            try:
                assert w.ok, w.why
                assert w.tree == {p.pid} and pipe.sent == [PROC_CN_MCAST_LISTEN]
                child = p.pid + 1_000_000  # a fork the test reports; no such process exists
                theirs.send(_fork(p.pid, child))
                if want:
                    p.stdin.close()
                else:
                    os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=10)
                # the kernel's record of the leader, in waitpid form, from its real end
                status = -p.returncode if p.returncode < 0 else p.returncode << 8
                theirs.send(_exit(child, 0) + _exit(p.pid, status))
                deadline = time.monotonic() + 5
                while p.pid not in w.exits and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert w.exits == {child: 0, p.pid: status}, "the reader thread drains as events arrive"
                assert w.clean() is want, (argv[0], w.why)
                assert w.exit_status() == (0 if want else None)
            finally:
                w.close()
            assert pipe.sent == [PROC_CN_MCAST_LISTEN, PROC_CN_MCAST_IGNORE]
            assert w._sock is None and not w._reader.is_alive()
        finally:
            theirs.close()
            if p.poll() is None:
                p.kill()
                p.wait()


_HANDLES_TERM = "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: sys.exit({rc})); time.sleep(30)"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "program, want",
    [
        # Handles SIGTERM with exit 0: the job returned 0, so no check of any kind and no reset.
        ("handles_0", "exit0"),
        # Handles SIGTERM with another status: a clean end, the fast check, no reset.
        ("handles_3", "fast"),
        # SIGTERM's default action kills it: a signal death, the I69 reset + full check.
        ("sleep", "full"),
    ],
)
async def test_the_real_job_wrapper_reports_a_handled_sigterm_by_the_jobs_own_status(
    monkeypatch, clear_job_state, program, want
):
    """The real job wrapper (its TERM trap is `exit 143`, not overridden here) around a program,
    preempted within the 10 s grace and judged from the kernel's own exit records. The wrapper's
    trap runs after the program returns, so the shell exits 143 whatever the program did; the
    broker reads the job's own status instead. A handled SIGTERM with exit 0 gets no check and no
    reset."""
    probe = ExitWitness(os.getpgid(0), None)
    probe.close()
    if not probe.ok:
        pytest.skip(f"no process events here: {probe.why}")
    fsm_healthy(srv)
    if program == "sleep":
        command = "sleep 30"
    else:
        command = f"{sys.executable} -c '{_HANDLES_TERM.format(rc=program.split('_')[1])}'"
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "970", command, kill_grace=5)
    assert sent and sent[0] is signal.SIGTERM and signal.SIGKILL not in sent, sent
    if want == "exit0":
        assert job.preempt_clean is True and job.preempt_exit_status == 0
        assert job.exit_code == 0, "a handled SIGTERM with exit 0 must reach the broker as exit 0"
        assert dirty == [] and owed == [] and gate == [], "exit 0 on SIGTERM: no check and no reset"
    elif want == "fast":
        assert job.preempt_clean is True and job.preempt_exit_status == 3
        assert job.exit_code == 143
        assert dirty == [] and owed == [] and gate == [{"job_failed": False}]
    else:
        assert job.preempt_clean is False
        assert dirty and owed == ["job 970 preempted"] and gate == [{"job_failed": True}]


_REAL_KILLPG = os.killpg

_PLAIN_HANDLER = (
    "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); "
    "print('ready', flush=True); time.sleep(30)"
)


def _live_scope(monkeypatch, job_id, pgid_of, *, sent=None, fails=False):
    """Stand in for a live systemd scope around the job: ``systemctl kill --signal=X <scope>``
    delivers X to the job's process group (``pgid_of()``), as systemd does to the scope's cgroup,
    and the scope is active while that group has a process. ``fails``: systemctl kill reports a
    failure and delivers nothing. Returns the list of signals systemctl was asked to send."""
    scope = srv.job_scope_unit(job_id)
    sent = [] if sent is None else sent
    real_run = subprocess.run

    def alive():
        pgid = pgid_of()
        if not pgid:
            return False
        try:
            _REAL_KILLPG(pgid, 0)
            return True
        except OSError:
            return False

    def run(argv, *a, **k):
        if list(argv[:2]) == ["systemctl", "kill"] and argv[-1] == scope:
            sig = signal.Signals[argv[2].split("=", 1)[1]]
            sent.append(sig)
            if fails:
                return subprocess.CompletedProcess(argv, 1, b"", b"Failed to kill unit")
            if alive():
                try:
                    _REAL_KILLPG(pgid_of(), sig)
                except OSError:
                    pass
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(srv.subprocess, "run", run)
    monkeypatch.setattr(srv, "_scope_active", lambda unit: unit == scope and alive())
    return sent


def _group_signals(monkeypatch):
    """Record every real signal (not the 0 probe) sent to a process group, and pass it on."""
    sent = []

    def killpg(pgid, sig):
        if sig:
            sent.append(signal.Signals(sig))
        return _REAL_KILLPG(pgid, sig)

    monkeypatch.setattr(srv.os, "killpg", killpg)
    return sent


def _spawn_reaped(argv, **kw):
    """Start ``argv`` in its own session and reap it on a thread, as the runner would, so its
    group is gone (not a zombie) once it exits."""
    p = subprocess.Popen(argv, start_new_session=True, **kw)
    reaper = threading.Thread(target=p.wait, daemon=True)
    reaper.start()
    return p, reaper


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["live", "none", "kill_fails"])
async def test_sigterm_reaches_a_preempted_job_once_never_by_scope_and_group_both(monkeypatch, clear_job_state, scope):
    """Spec 01 I20: SIGTERM goes once, to the live scope, else to the process group, never to
    both. A second SIGTERM a few ms after the first kills a job whose handler already returned 0
    (its interpreter is shutting down with handlers back at the default): a clean exit 0 then
    reads as a signal death. The group gets it after the scope only when systemctl kill failed."""
    monkeypatch.setattr(srv, "PREEMPT_KILL_GRACE_SEC", 5)
    p, reaper = _spawn_reaped(["sleep", "30"])
    try:
        pgid_of = (lambda: None) if scope == "none" else (lambda: p.pid)
        by_scope = _live_scope(monkeypatch, "981", pgid_of, fails=scope == "kill_fails")
        by_group = _group_signals(monkeypatch)
        clean = await srv._preempt_kill("981", p.pid)
        reaper.join(5)
        assert p.returncode == -signal.SIGTERM
        assert clean is False and "981" in srv.preempt_forced_job_ids, "no witness: the I69 debt stays"
        want = {
            "live": ([signal.SIGTERM], []),
            "none": ([], [signal.SIGTERM]),
            "kill_fails": ([signal.SIGTERM], [signal.SIGTERM]),
        }[scope]
        assert (by_scope, by_group) == want, "SIGTERM was sent to the scope and the group both"
    finally:
        srv.preempt_forced_job_ids.discard("981")
        if p.poll() is None:
            p.kill()
        reaper.join(5)


@pytest.mark.asyncio
async def test_a_plain_python_handler_that_exits_0_ends_with_0_and_takes_the_skip(monkeypatch, clear_job_state):
    """Spec 01 I20 exit-0 skip: a job in a live scope whose plain Python SIGTERM handler calls
    sys.exit(0) gets one SIGTERM, exits 0 (not -15), and takes the skip: no check of any kind and
    no reset. The witness here reads the job's real end (the test is its parent). Run five times:
    with SIGTERM sent to the scope and the group both, about one run in three died of the second
    signal."""
    fsm_healthy(srv)
    srv._clear_device_dirty()
    monkeypatch.setattr(srv, "PREEMPT_KILL_GRACE_SEC", 5)
    job_id = "985"
    cur = {}
    by_scope = _live_scope(monkeypatch, job_id, lambda: cur["p"].pid)
    by_group = _group_signals(monkeypatch)

    class _OwnEnd:
        """The job's own process, reaped by the test: clean when it exited by its own code."""

        def __init__(self, pgid, scope):
            pass

        def drain(self):
            pass

        def clean(self):
            cur["reaper"].join(5)
            return cur["p"].returncode is not None and cur["p"].returncode >= 0

        def exit_status(self):
            return cur["p"].returncode

        def close(self):
            pass

    monkeypatch.setattr(srv, "ExitWitness", _OwnEnd)
    for _ in range(5):
        p, reaper = _spawn_reaped([sys.executable, "-c", _PLAIN_HANDLER], stdout=subprocess.PIPE)
        cur.update(p=p, reaper=reaper)
        try:
            assert p.stdout.readline() == b"ready\n"
            del by_scope[:], by_group[:]
            job = srv.Job(
                id=job_id,
                owner="server",
                workspace="/tmp",
                command="python",
                queued_at="t",
                timeout_sec=1,
                preemptable=True,
            )
            job.status = srv.JobStatus.RUNNING
            srv.jobs[job_id] = job
            clean = await srv._preempt_kill(job_id, p.pid, witness=True)
            reaper.join(5)
            assert p.returncode == 0, f"the handler's exit 0 became {p.returncode} (a second SIGTERM?)"
            assert (by_scope, by_group) == ([signal.SIGTERM], []), (by_scope, by_group)
            assert clean is True and job.preempt_exit_status == 0
            assert job_id not in srv.preempt_forced_job_ids, "a clean exit 0 owes no reset"
            job.status, job.preempt_clean = srv.JobStatus.PREEMPTED, clean
            assert srv._preempted_exit0_needs_no_check(job), "exit 0 on SIGTERM takes the skip"
            assert srv._is_wedge_risk_exit(job.status, 143, preempt_clean=clean) is False
        finally:
            srv.preempt_forced_job_ids.discard(job_id)
            if p.poll() is None:
                p.kill()
            reaper.join(5)
            p.stdout.close()


@pytest.mark.asyncio
async def test_a_job_in_a_live_scope_that_needs_sigkill_is_reset_and_fully_checked(monkeypatch, clear_job_state):
    """Spec 01 I20 / 03 I69 with a live scope: the job ignores its one SIGTERM, so after the grace
    SIGKILL goes to the scope and the group both, and the device owes the reset plus the full
    check, fabric included (job_failed=True), before the waiting job."""
    fsm_healthy(srv)
    _witness(monkeypatch, clean=True, status=0)
    by_scope = []
    job, sent, gate, dirty, owed = await _preempt(
        monkeypatch, "986", "trap '' INT TERM; sleep 60", kill_grace=1, scope_sent=by_scope
    )
    assert by_scope == [signal.SIGTERM, signal.SIGKILL], by_scope
    assert signal.SIGTERM not in sent and signal.SIGKILL in sent, f"SIGTERM went to the group as well: {sent}"
    assert job.preempt_clean is False
    assert dirty
    assert owed == ["job 986 preempted"], f"a killed preemption must owe the I69 reset: {owed}"
    assert gate == [{"job_failed": True}]


@pytest.mark.asyncio
async def test_a_preemption_witnessed_from_a_pid_namespace_is_reset_and_fully_checked(monkeypatch, clear_job_state):
    """A broker in a PID namespace cannot witness its job's exits: the real witness says so
    (ok=False). Even a job that handles SIGTERM with exit 0 within the grace then gets the I69
    reset and the full check, fabric included, never the exit-0 skip or the fast check."""
    monkeypatch.setattr(exit_witness, "_in_child_pid_namespace", lambda: True)
    fsm_healthy(srv)
    command = f"{sys.executable} -c '{_HANDLES_TERM.format(rc=0)}'"
    job, sent, gate, dirty, owed = await _preempt(monkeypatch, "972", command, kill_grace=5)
    assert sent and sent[0] is signal.SIGTERM and signal.SIGKILL not in sent, sent
    assert job.preempt_clean is False and job.preempt_exit_status is None
    assert dirty and owed == ["job 972 preempted"] and gate == [{"job_failed": True}]


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


def test_the_earlier_field_name_is_still_accepted():
    params = srv.JobSubmitInput(workspace="/tmp", command="true", preemptable=True)
    assert params.no_timeout_except_on_contention is True


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

    async def _gate(job_log_file, job_failed=False, **_):
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


def _waiting_since(job_id, ago_sec):
    j = _waiting_job(job_id)
    j.queued_at = (srv.datetime.now() - srv.timedelta(seconds=ago_sec)).isoformat()
    j.queued_monotonic = time.monotonic() - ago_sec
    return j


def _secs_left(kill_at):
    return kill_at - time.monotonic()


def test_a_restored_waiter_is_read_from_its_queued_at_only_once(monkeypatch, clear_job_state):
    # A job restored from the queue spool has no monotonic arrival: its queued_at is read once, and
    # from then on the arrival is monotonic, so a later wall-clock step does not move the minute.
    j = _waiting_since("966", ago_sec=3)
    j.queued_monotonic = None
    limit_end = time.monotonic() - 10
    assert 56 <= _secs_left(srv._preempt_kill_at(_pjob(), limit_end)) <= 57.5
    real = time.time
    monkeypatch.setattr(srv.time, "time", lambda: real() - 3600)
    left = _secs_left(srv._preempt_kill_at(_pjob(), limit_end))
    assert 56 <= left <= 57.5, left


def test_an_unparsable_restored_waiter_starts_the_minute_at_its_first_read(clear_job_state):
    j = _waiting_job("967")
    j.queued_monotonic = None
    j.queued_at = "not a time"
    first = srv._preempt_kill_at(_pjob(), time.monotonic() - 10)
    time.sleep(0.2)
    assert srv._preempt_kill_at(_pjob(), time.monotonic() - 10) == first


@pytest.mark.asyncio
async def test_a_readopted_job_with_a_waiter_from_before_the_restart_still_gets_its_minute(
    monkeypatch, tmp_path, clear_job_state
):
    """A waiter queued long before a slow restart: the new broker never saw that contention, so the
    minute counts from the monitor's start, never from before it (no kill on the first poll)."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "PREEMPT_GRACE_SEC", 1)
    _recording_dirty(monkeypatch)
    active = {"v": True}
    stopped = []
    monkeypatch.setattr(srv, "_scope_active", lambda scope: active["v"])

    async def fake_kill(job_id, pid):
        stopped.append(time.monotonic())
        active["v"] = False

    monkeypatch.setattr(srv, "_preempt_kill", fake_kill)
    _readopted("023", served_sec=2400, timeout_sec=300)
    _waiting_since("952", ago_sec=600)

    t0 = time.monotonic()
    await asyncio.wait_for(srv._monitor_readopted_scope("023", "ttdev-job-023.scope"), timeout=5)
    assert len(stopped) == 1
    assert stopped[0] - t0 >= 1 - 0.05, f"killed {stopped[0] - t0:.2f}s after the restart"
    assert srv.jobs["023"].status is srv.JobStatus.PREEMPTED


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
    """With a job waiting the silence reaper applies as to any job, before the time limit: a hung
    job must not hold the device the waiter needs. The reap is a failed end, so the full gate
    (fabric included) runs before the waiter is dispatched."""
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
    assert job.preempt_killed_monotonic is None, "reaped for silence, not preempted for time"
    assert gate == [{"job_failed": True}]


_TERM_EXITS_CLEAN = """
import signal, sys, time

def on_term(*_):
    with open(sys.argv[1], "a") as f:
        f.write("TERM\\n")
    sys.exit(0)


signal.signal(signal.SIGTERM, on_term)
print("serving", flush=True)
while True:
    time.sleep(0.05)
"""


@pytest.mark.asyncio
async def test_a_silent_job_past_its_limit_is_preempted_not_reaped(monkeypatch, tmp_path, clear_job_state):
    """Past its limit with a job waiting, the preemption owns the stop. The silence reaper must
    neither signal the job inside its minute nor relabel it hung. The kill ends it PREEMPTED. The
    job exits 0 through its own SIGTERM handler, so (witness pinned to that verdict, whatever this
    host's connector allows) it is case (b): no spec 03 I69 reset, only the fast post-job check.
    Spec 01 I10/I20."""
    import sys

    made = _witness(monkeypatch, clean=True)
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    dirty = _recording_dirty(monkeypatch)
    owed = []
    monkeypatch.setattr(srv, "_owe_kill_reset", owed.append)
    _fast_preempt(monkeypatch, grace=2, kill_grace=2)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.1)
    script = tmp_path / "serve.py"
    script.write_text(_TERM_EXITS_CLEAN)
    terms = tmp_path / "terms"

    job = srv.Job(
        id="972",
        owner="t",
        workspace="/tmp",
        command=f"exec {sys.executable} -I {script} {terms}",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["972"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("972")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        await asyncio.sleep(2.5)  # past both its limit and the silence limit, nobody waiting
        assert job.status is srv.JobStatus.RUNNING, f"a silent uncontended job ended {job.status.value}"
        t0 = time.monotonic()
        _waiting_job()
        await _until_finished(job, limit_sec=10)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    assert job.status is srv.JobStatus.PREEMPTED, f"a silent preempted job ended {job.status.value}"
    assert sent and sent[0][0] - t0 >= 2 - 0.15, f"signalled inside the minute: {sent}"
    assert terms.read_text().splitlines() == ["TERM"]
    assert job.exit_code == 0
    assert job.preempt_killed_monotonic is not None, "preempted for time, not reaped for silence"
    assert made and job.preempt_clean is True
    assert owed == [], f"a clean end on SIGTERM must not owe the I69 reset: {owed}"
    assert dirty == [], "a clean end on SIGTERM must not flag the device"
    assert gate == [{"job_failed": False}], "a clean end on SIGTERM takes the fast post-job check"


def test_sigkill_comes_ten_seconds_after_sigterm_and_the_docs_say_so():
    """The job gets 10 s between SIGTERM and SIGKILL, not 5: the broker's constant, the spec and the
    README, the CLI help and the submit field state the same number."""
    from pathlib import Path

    from tt_device_mcp import constants

    assert constants.PREEMPT_KILL_GRACE_SEC == 10
    assert srv.PREEMPT_KILL_GRACE_SEC == constants.PREEMPT_KILL_GRACE_SEC
    root = Path(__file__).resolve().parents[1]
    readme = " ".join((root / "README.md").read_text().split())
    spec = " ".join((root / "specs" / "01-jobs.md").read_text().split())
    assert "(SIGTERM, then SIGKILL at most 10 s later)" in readme
    assert "SIGKILL to both at most `PREEMPT_KILL_GRACE_SEC` (10 s) later" in spec
    import tt_device_mcp.cli as cli

    doc = srv.JobSubmitInput.model_fields["no_timeout_except_on_contention"].description
    assert "SIGKILL 10s later" in doc, doc
    assert "10s later, SIGKILL" in cli._NO_TIMEOUT_HELP, cli._NO_TIMEOUT_HELP


# A pending broker update is contention too (spec 01 I20, spec 08 I20): the auto-updater's live
# dispatch hold counts as one waiter for a preemptable job past its limit, with the same minute and
# kill as a queued job, so an update reaches a box whose preemptable job would otherwise run on
# with no end. The waiter is never dispatched and never counted as queued.


def _update_hold(pid=None, age_sec=0.0):
    """Write the auto-updater's dispatch hold as it does (`<pid> <epoch>`), held for `age_sec`."""
    path = srv.dispatch_hold()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid() if pid is None else pid} {int(time.time() - age_sec)}\n")
    return path


def _reaped_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.fixture
def no_seen_hold(monkeypatch):
    monkeypatch.setattr(srv, "_dispatch_hold_waiter_seen", None)


def test_a_live_update_hold_past_the_limit_starts_the_minute(clear_job_state, no_seen_hold):
    """Past the limit with nothing queued, a live hold starts the minute when the broker first sees
    it (never back-dated to the file's epoch): PREEMPTING, cause 'update', the kill one minute later."""
    job = _pjob()
    job.status = srv.JobStatus.RUNNING
    job.timeout_sec = 1
    _update_hold(age_sec=5)
    now = time.monotonic()
    kill_at = srv._preempt_kill_at(job, now - 100, now)
    assert kill_at == now + srv.PREEMPT_GRACE_SEC, (kill_at, now)
    assert srv.preempt_label(job).startswith("PREEMPTING ")
    assert srv.preempt_cause(job) == "update"
    # A queued job beside it: the cause names the queue.
    _waiting_job("980")
    srv._preempt_kill_at(job, now - 100, time.monotonic())
    assert srv.preempt_cause(job) == "queued"


@pytest.mark.parametrize("hold", ["none", "dead", "overdue", "malformed"])
def test_no_live_update_hold_is_no_contention(clear_job_state, no_seen_hold, hold):
    if hold == "dead":
        _update_hold(pid=_reaped_pid())
    elif hold == "overdue":
        _update_hold(age_sec=srv.DISPATCH_HOLD_MAX_SEC + 60)
    elif hold == "malformed":
        srv.dispatch_hold().parent.mkdir(parents=True, exist_ok=True)
        srv.dispatch_hold().write_text("garbage\n")
    job = _pjob()
    job.status = srv.JobStatus.RUNNING
    now = time.monotonic()
    assert srv._preempt_kill_at(job, now - 100, now) is None
    assert srv._waiter_arrivals(now) == {}
    assert srv.preempt_cause(job) is None and srv.preempt_label(job).startswith("PREEMPTABLE ")


def test_an_update_hold_before_the_limit_is_no_contention(clear_job_state, no_seen_hold):
    _update_hold(age_sec=30)
    job = _pjob()
    job.status = srv.JobStatus.RUNNING
    now = time.monotonic()
    assert srv._preempt_kill_at(job, now + 10, now) is None
    assert job.contention_since_monotonic is None
    # Once the limit passes, the minute starts at the first poll that sees the hold.
    assert srv._preempt_kill_at(job, now + 10, now + 11) == now + 11 + srv.PREEMPT_GRACE_SEC


def test_a_refreshed_hold_keeps_the_minute_and_a_new_holder_starts_a_fresh_one(clear_job_state, no_seen_hold):
    """The updater rewrites its hold's epoch every poll: the same pid is the same waiter, its
    arrival kept from the first sight. A later lap (another pid) is a new waiter: a fresh minute."""
    job = _pjob()
    job.status = srv.JobStatus.RUNNING
    _update_hold(age_sec=20)
    now = time.monotonic()
    first = srv._preempt_kill_at(job, now - 100, now)
    _update_hold(age_sec=0)  # refreshed
    assert srv._preempt_kill_at(job, now - 100, now + 5) == first
    other = subprocess.Popen(["sleep", "30"])
    try:
        _update_hold(pid=other.pid, age_sec=0)
        fresh = srv._preempt_kill_at(job, now - 100, now + 6)
        assert fresh == now + 6 + srv.PREEMPT_GRACE_SEC, (first, fresh)
    finally:
        other.kill()
        other.wait()


def test_a_pending_update_is_not_counted_as_queued(clear_job_state, no_seen_hold):
    """The hold is a waiter for the preemption only: /health 'queued' (read by health probes and
    monitors) and queue status count no job for it; /health names the cause."""
    srv.jobs["981"] = srv.Job(
        id="981",
        owner="o",
        workspace="/w",
        command="c",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        timeout_sec=1,
        preemptable=True,
    )
    before = srv._health_payload()
    assert before["queued"] == 0 and "preempt_cause" not in before
    _update_hold()
    srv._preempt_kill_at(srv.jobs["981"], time.monotonic() - 100)
    payload = srv._health_payload()
    assert payload["queued"] == 0 and payload["running"] == 1, payload
    assert payload["preempt"].startswith("PREEMPTING ") and payload["preempt_cause"] == "update", payload
    assert set(srv.jobs) == {"981"}


def test_a_pending_update_never_makes_a_silent_job_reapable_before_its_limit(clear_job_state, no_seen_hold):
    """The hung reaper spares a preemptable job while no JOB waits: a pending update preempts only
    past the limit, through the minute, never by a reap."""
    _update_hold()
    assert srv._first_waiter_arrival() is not None
    assert srv._first_waiter_arrival(update=False) is None


@pytest.mark.asyncio
async def test_an_update_hold_preempts_a_job_past_its_limit_then_sigkills(monkeypatch, clear_job_state, no_seen_hold):
    """Nothing queued, past the limit, the updater holds dispatch: PREEMPTING with cause 'update'
    for the minute and no signal inside it, then SIGTERM, SIGKILL the kill grace later. The job
    ends PREEMPTED, owes the reset and gets the full gate; the hold's waiter is never dispatched."""
    fsm_healthy(srv)
    _witness(monkeypatch, clean=False)
    seen = []

    def watch():
        seen.append(srv._health_payload())

    def hold():
        _update_hold()
        t0 = time.monotonic()
        seen.append(t0)

    job, sent, gate, dirty, owed = await _preempt(
        monkeypatch, "982", "trap '' INT TERM; sleep 60", kill_grace=1, contender=hold
    )
    assert sent[:1] == [signal.SIGTERM] and signal.SIGKILL in sent, sent
    assert job.exit_code in (-9, 137), job.exit_code
    assert dirty and owed == ["job 982 preempted"], (dirty, owed)
    assert gate == [{"job_failed": True}], "a SIGKILLed preemption gets the reset and the full gate"
    assert set(srv.jobs) == {"982"} and srv.get_job_queue().qsize() == 0, "the update waiter was dispatched"
    assert srv.dispatch_hold().exists(), "the broker must never remove a live hold"


@pytest.mark.asyncio
async def test_an_update_hold_preemption_shows_the_cause_and_waits_the_minute(
    monkeypatch, clear_job_state, no_seen_hold
):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    _recording_dirty(monkeypatch)
    _fast_preempt(monkeypatch, grace=2, kill_grace=0.5)
    job = srv.Job(
        id="983",
        owner="server",
        workspace="/tmp",
        command="trap '' INT TERM; echo serving; sleep 60",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["983"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("983")
    runner = asyncio.create_task(srv.job_runner())
    causes = []
    try:
        await _until_running(job)
        await asyncio.sleep(1.5)  # past the 1 s limit, nothing queued, no hold
        assert job.status is srv.JobStatus.RUNNING and "preempt_cause" not in srv._health_payload()
        _update_hold()
        t0 = time.monotonic()
        while job.finished_at is None and time.monotonic() - t0 < 10:
            h = srv._health_payload()
            if job.status is srv.JobStatus.RUNNING:
                causes.append((h.get("preempt"), h.get("preempt_cause"), h["queued"]))
            await asyncio.sleep(0.1)
        await _until_finished(job, limit_sec=1)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.PREEMPTED
    first_signal = sent[0][0] - t0
    assert first_signal >= 2 - 0.15, f"a signal came {first_signal:.2f}s into the 2 s minute"
    assert any(p and p.startswith("PREEMPTING ") and c == "update" and q == 0 for p, c, q in causes), causes
    assert all(q == 0 for _, _, q in causes), causes
    assert gate == [{"job_failed": True}]


@pytest.mark.asyncio
async def test_a_clean_sigterm_end_on_an_update_keeps_the_fast_check(monkeypatch, clear_job_state, no_seen_hold):
    fsm_healthy(srv)
    _witness(monkeypatch, clean=True, status=143)
    job, sent, gate, dirty, owed = await _preempt(
        monkeypatch, "984", "sleep 60", kill_grace=5, contender=lambda: _update_hold()
    )
    assert sent and sent[0] is signal.SIGTERM and signal.SIGKILL not in sent, sent
    assert dirty == [] and owed == []
    assert gate == [{"job_failed": False}], "a clean end on SIGTERM takes the fast check"


@pytest.mark.asyncio
async def test_a_job_that_ends_itself_with_0_on_an_update_is_a_yield(monkeypatch, clear_job_state, no_seen_hold):
    """It sees PREEMPTING (cause 'update') and exits 0 inside the minute: never signalled, a yield."""
    fsm_healthy(srv)
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    gate = _recording_post_job_gate(monkeypatch)
    _fast_preempt(monkeypatch, grace=30)
    flag = srv.dispatch_hold().with_name("go")
    job = srv.Job(
        id="985",
        owner="server",
        workspace="/tmp",
        command=f"while [ ! -e {flag} ]; do sleep 0.05; done; exit 0",
        queued_at="t",
        timeout_sec=1,
        preemptable=True,
    )
    srv.jobs["985"] = job
    sent = _recording_signals(monkeypatch, job)
    await srv.get_job_queue().put("985")
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _until_running(job)
        await asyncio.sleep(1.2)
        _update_hold()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if srv._health_payload().get("preempt_cause") == "update":
                break
        flag.write_text("")
        await _until_finished(job, limit_sec=5)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.COMPLETED and job.preempt_yielded, (job.status, job.preempt_yielded)
    assert sent == [], sent
    assert gate == [], "an exit 0 under contention gets no post-job check (spec 01 I20)"


@pytest.mark.asyncio
async def test_a_readopted_preemptable_job_is_killed_a_full_minute_after_an_update_holds(
    monkeypatch, tmp_path, clear_job_state, no_seen_hold
):
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
    _readopted("986", served_sec=2400, timeout_sec=300)
    _update_hold(age_sec=600)  # held long before the restart: the minute still starts afresh

    t0 = time.monotonic()
    await asyncio.wait_for(srv._monitor_readopted_scope("986", "ttdev-job-986.scope"), timeout=5)
    assert [j for j, _ in stopped] == ["986"]
    assert stopped[0][1] - t0 >= 1 - 0.05, "killed before a full minute of contention after the restart"
    assert srv.jobs["986"].status is srv.JobStatus.PREEMPTED


@pytest.mark.asyncio
async def test_a_readopted_preemptable_job_is_left_alone_with_no_live_hold(
    monkeypatch, tmp_path, clear_job_state, no_seen_hold
):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    calls = {"n": 0}
    killed = []

    def scope_active(scope):
        calls["n"] += 1
        return calls["n"] < 20

    async def fake(*a, **k):
        killed.append(a)

    monkeypatch.setattr(srv, "_scope_active", scope_active)
    monkeypatch.setattr(srv, "_preempt_kill", fake)
    monkeypatch.setattr(srv, "_terminate_scope", fake)
    _readopted("987", served_sec=2400, timeout_sec=300)
    _update_hold(pid=_reaped_pid())
    await asyncio.wait_for(srv._monitor_readopted_scope("987", "ttdev-job-987.scope"), timeout=5)
    assert killed == [], "a dead hold preempted a re-adopted job"


def test_health_shows_the_seconds_left_before_a_preemptable_jobs_limit(clear_job_state):
    """The auto-updater bounds its drain by the time a preemptable job has before its limit."""
    srv.jobs["988"] = job = srv.Job(
        id="988", owner="o", workspace="/w", command="c", queued_at="", status=srv.JobStatus.RUNNING, preemptable=True
    )
    assert "preempt_limit_left_sec" not in srv._health_payload()
    job.preempt_limit_monotonic = time.monotonic() + 120
    assert 118 <= srv._health_payload()["preempt_limit_left_sec"] <= 120
    job.preempt_limit_monotonic = time.monotonic() - 5
    assert srv._health_payload()["preempt_limit_left_sec"] == 0
