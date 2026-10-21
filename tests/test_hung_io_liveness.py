# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 01 I10: the hung reaper counts the job's storage IO as liveness, never its CPU time, and a
job may set its own bounded silence limit. A silent job with no IO is reaped at its limit exactly as
before, and a reap still owes the device the spec 03 I69 reset and full check."""

import asyncio
import subprocess
import sys
import threading
import time

import pytest

import tt_device_mcp.cli as cli
from tests.test_job_kill_reset import _runner_done, _start, _stop, _until, runner_env  # noqa: F401 (fixture)
from tt_device_mcp import job_reap
from tt_device_mcp import server as srv


class _Io:
    """Stands in for job_io_bytes: one process whose storage counter grows on each sample while
    ``advancing`` is set."""

    def __init__(self, advancing=True):
        self.advancing = advancing
        self.n = 0
        self.calls = 0

    def __call__(self, pid, scope=None, tag=None):
        self.calls += 1
        if self.advancing:
            self.n += 4096
        return {(pid or 0, "1"): self.n}


@pytest.fixture
def fast(monkeypatch, runner_env):  # noqa: F811
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_POLL_SEC", 0.05)
    monkeypatch.setattr(srv, "HUNG_NOTICE_SEC", 0)
    monkeypatch.setattr(srv, "HUNG_IO_CAP_SEC", 600)
    return runner_env


async def _run(job, runner, tries=400):
    try:
        assert await _until(lambda: _runner_done(job), tries=tries)
    finally:
        await _stop(runner)


@pytest.mark.asyncio
async def test_a_silent_job_with_storage_io_advancing_is_not_reaped(fast, monkeypatch):
    io = _Io(advancing=True)
    monkeypatch.setattr(srv, "job_io_bytes", io)
    job, runner = await _start("960", "echo start; sleep 2.5; echo done")
    await _run(job, runner)
    assert job.status is srv.JobStatus.COMPLETED, f"a job still doing IO was reaped as {job.status.value}"
    assert "done" in job.output
    assert io.calls > 5, "the reaper sampled the job's IO every poll"


@pytest.mark.asyncio
async def test_a_silent_idle_job_is_reaped_at_the_limit(fast, monkeypatch):
    monkeypatch.setattr(srv, "job_io_bytes", _Io(advancing=False))
    t0 = time.monotonic()
    job, runner = await _start("961", "echo start; sleep 600")
    await _run(job, runner)
    assert job.status is srv.JobStatus.HUNG
    assert time.monotonic() - t0 < 10, "no IO: reaped at the 1 s limit, not later"
    assert "no storage IO" in job.error, job.error
    assert srv._kill_reset_debt().startswith("job 961"), "the reap owes the reset and the full check"


@pytest.mark.asyncio
async def test_unreadable_io_is_no_signal_and_the_silent_job_is_reaped(fast, monkeypatch):
    monkeypatch.setattr(srv, "job_io_bytes", lambda *a, **k: None)
    job, runner = await _start("962", "sleep 600")
    await _run(job, runner)
    assert job.status is srv.JobStatus.HUNG


@pytest.mark.asyncio
async def test_a_silent_job_spinning_the_cpu_without_io_is_reaped(fast):
    """The real /proc read: a busy-polling process (what a tt-metal host process does on a hung
    device) burns CPU and moves no storage bytes, so it is reaped at the limit."""
    t0 = time.monotonic()
    job, runner = await _start("963", "echo start; while :; do :; done")
    await _run(job, runner)
    assert job.status is srv.JobStatus.HUNG, f"a spinning silent job ended {job.status.value}"
    assert time.monotonic() - t0 < 10
    assert srv._kill_reset_debt().startswith("job 963")


@pytest.mark.asyncio
async def test_the_per_job_limit_is_honoured(fast, monkeypatch):
    monkeypatch.setattr(srv, "job_io_bytes", _Io(advancing=False))
    # 2.5 s of silence: past the host's 1 s limit, inside the job's own 4 s.
    job, runner = await _start("964", "echo start; sleep 2.5; echo done")
    job.hung_silence_sec = 4
    await _run(job, runner)
    assert job.status is srv.JobStatus.COMPLETED, job.status.value

    job, runner = await _start("965", "echo start; sleep 600")
    job.hung_silence_sec = 2
    t0 = time.monotonic()
    await _run(job, runner)
    elapsed = time.monotonic() - t0
    assert job.status is srv.JobStatus.HUNG
    assert 1.5 < elapsed < 10, f"reaped at {elapsed:.1f}s, not at its own 2 s limit"
    assert "no storage IO" in job.error


@pytest.mark.asyncio
async def test_the_hard_cap_reaps_a_job_with_io_but_no_output(fast, monkeypatch):
    monkeypatch.setattr(srv, "HUNG_IO_CAP_SEC", 2)
    monkeypatch.setattr(srv, "job_io_bytes", _Io(advancing=True))
    t0 = time.monotonic()
    job, runner = await _start("966", "echo start; sleep 600")
    await _run(job, runner)
    elapsed = time.monotonic() - t0
    assert job.status is srv.JobStatus.HUNG
    assert 1.5 < elapsed < 10, f"reaped at {elapsed:.1f}s, not at the 2 s cap"
    assert "only storage IO" in job.error, job.error
    assert srv._kill_reset_debt().startswith("job 966"), "a cap reap owes the reset and full check too"


@pytest.mark.asyncio
async def test_the_cap_is_never_below_the_jobs_own_limit(fast, monkeypatch):
    monkeypatch.setattr(srv, "HUNG_IO_CAP_SEC", 1)
    monkeypatch.setattr(srv, "job_io_bytes", _Io(advancing=True))
    job, runner = await _start("967", "echo start; sleep 2.5; echo done")
    job.hung_silence_sec = 4
    await _run(job, runner)
    assert job.status is srv.JobStatus.COMPLETED, job.status.value


@pytest.mark.asyncio
async def test_the_notice_names_the_signal_keeping_the_job_alive(fast, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "HUNG_NOTICE_SEC", 1)
    monkeypatch.setattr(srv, "HUNG_SILENCE_SEC", 3)
    io = _Io(advancing=True)
    monkeypatch.setattr(srv, "job_io_bytes", io)
    job, runner = await _start("968", "echo start; sleep 1.6; echo mid; sleep 1.6; echo done")
    job.log_file = str(tmp_path / "968.log")
    await _run(job, runner)
    log = open(job.log_file).read()
    assert "storage IO" in log and "keeps it alive" in log, log

    io.advancing = False
    job, runner = await _start("969", "echo start; sleep 1.6; echo done")
    job.log_file = str(tmp_path / "969.log")
    await _run(job, runner)
    log = open(job.log_file).read()
    assert "no storage IO; reaping as hung at 3s" in log, log


# ---------------------------------------------------------------- the bound on every submit path


def test_the_per_job_limit_is_bounded():
    assert srv._bounded_hung_silence(None) is None
    assert srv._bounded_hung_silence(0) is None, "0 keeps the host's limit, never disables the reaper"
    assert srv._bounded_hung_silence(5) == srv.HUNG_SILENCE_MIN_SEC
    assert srv._bounded_hung_silence(99999) == srv.HUNG_SILENCE_MAX_SEC == 3600
    assert srv._bounded_hung_silence("900") == 900
    with pytest.raises(ValueError):
        srv._bounded_hung_silence("soon")


def test_the_tool_input_refuses_a_limit_out_of_bounds():
    base = {"workspace": "/w", "command": "c"}
    assert srv.JobSubmitInput(**base).hung_silence_sec is None
    assert srv.JobSubmitInput(**base, hung_silence_sec=1200).hung_silence_sec == 1200
    for bad in (3601, 10):
        with pytest.raises(ValueError):
            srv.JobSubmitInput(**base, hung_silence_sec=bad)


@pytest.mark.asyncio
async def test_a_queued_spec_keeps_the_limit_across_a_restart(clear_job_state, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    job = srv.Job(id="970", owner="o", workspace="/w", command="c", queued_at="t", hung_silence_sec=900)
    srv._persist_queued_job(job)
    srv.jobs.pop("970", None)
    await srv._restore_queued_jobs()
    assert srv.jobs["970"].hung_silence_sec == 900


@pytest.mark.parametrize("sub", ["run-bg", "run"])
def test_the_cli_flag_sends_the_limit(monkeypatch, sub):
    sent = {}

    def fake_call(port, tool, payload):
        sent.update(payload)
        return {"error": "stop here"}

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    monkeypatch.setattr(cli, "mcp_tool_call", fake_call)
    monkeypatch.setattr(sys, "argv", ["tt-device-mcp", sub, "--hung-silence", "900", "true"])
    try:
        cli.main()
    except SystemExit:
        pass
    assert sent.get("hung_silence_sec") == 900, sent


# ---------------------------------------------------------------- the /proc sampler


def test_job_io_bytes_reads_the_jobs_tree_and_sees_writes(tmp_path):
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        sample = job_reap.job_io_bytes(proc.pid)
        assert sample is not None and any(pid == proc.pid for pid, _ in sample), sample
    finally:
        proc.kill()
        proc.wait()


def test_io_advanced_counts_growth_and_new_movers_only():
    a = {(10, "1"): 100}
    assert not job_reap.io_advanced(None, a), "no baseline is no signal"
    assert not job_reap.io_advanced(a, None)
    assert not job_reap.io_advanced(a, {(10, "1"): 100})
    assert job_reap.io_advanced(a, {(10, "1"): 101})
    assert not job_reap.io_advanced(a, {(10, "2"): 0}), "a fresh process that moved nothing is no IO"
    assert job_reap.io_advanced(a, {(11, "9"): 4096})
    assert not job_reap.io_advanced(a, {}), "an exited process is no IO"


def test_io_sampler_is_not_cpu(tmp_path):
    """A process that only burns CPU leaves its storage counters still."""
    proc = subprocess.Popen([sys.executable, "-c", "while True: pass"], start_new_session=True)
    try:
        time.sleep(0.2)
        first = job_reap.job_io_bytes(proc.pid)
        time.sleep(0.3)
        assert not job_reap.io_advanced(first, job_reap.job_io_bytes(proc.pid))
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------- a /proc read that never returns


class _BlockedIo:
    """Stands in for job_io_bytes when a /proc read blocks for good (a process in D state on a hung
    device driver): every call waits on ``release``. Counts calls and the most ever in flight."""

    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    def __call__(self, pid, scope=None, tag=None):
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.release.wait(60)
            return None
        finally:
            with self.lock:
                self.in_flight -= 1


@pytest.fixture
def blocked_io(monkeypatch):
    io = _BlockedIo()
    monkeypatch.setattr(srv, "job_io_bytes", io)
    yield io
    io.release.set()


@pytest.mark.asyncio
async def test_a_blocked_io_sample_does_not_stall_the_reaper(fast, blocked_io):
    t0 = time.monotonic()
    job, runner = await _start("971", "echo start; sleep 600")
    await _run(job, runner)
    assert job.status is srv.JobStatus.HUNG, f"a blocked IO sample kept the job {job.status.value}"
    assert time.monotonic() - t0 < 10, "the bounded sample is no signal: reaped at the 1 s limit"
    assert "storage IO unreadable" in job.error, job.error
    assert srv._kill_reset_debt().startswith("job 971"), "the reap still owes the reset and the full check"
    assert blocked_io.calls == 1 and blocked_io.max_in_flight == 1, (
        f"{blocked_io.calls} samples started, {blocked_io.max_in_flight} in flight at once: "
        "a stuck sample must not be joined by another"
    )


@pytest.mark.asyncio
async def test_a_late_sample_is_dropped_and_sampling_resumes(fast, blocked_io):
    """A sample that returns after its bound is stale: it is never counted as IO, and the next poll
    samples afresh, still one at a time."""
    job, runner = await _start("972", "echo start; sleep 600")
    assert await _until(lambda: blocked_io.calls == 1)
    await asyncio.sleep(0.3)  # several polls pass with the sample stuck
    assert blocked_io.calls == 1
    blocked_io.release.set()
    assert await _until(lambda: blocked_io.calls > 1), "sampling resumes once the stuck read returns"
    await _run(job, runner)
    assert job.status is srv.JobStatus.HUNG
    assert blocked_io.max_in_flight == 1
