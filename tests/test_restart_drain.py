# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A job outlives a broker restart with its output intact (spec 01 I17), and an update holds
dispatch across its own restart (spec 01 I18).

A job used to write into pipes. Re-adoption kept the job's process across an auto-update, but
the pipes' read end died with the old broker, so a job that kept printing failed at exit (a
Python job exits 120 when its last stdout flush hits EPIPE). And a job queued behind one that had
just ended could be dispatched seconds before the update restarted the broker under it.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

import tt_device_mcp.job_spool as spool
import tt_device_mcp.server as srv
from tests.test_device_safety import _free_device_lock, _quiet_post_job_gate


def _spool_dir() -> Path:
    return spool.job_spool_dir()


def _record_spawn(monkeypatch) -> list:
    """Wrap the shell spawn and record what the job's stdout/stderr were bound to."""
    seen: list = []
    real = asyncio.create_subprocess_shell

    async def spawn(cmd, **kw):
        seen.append((kw.get("stdout"), kw.get("stderr")))
        return await real(cmd, **kw)

    monkeypatch.setattr(srv.asyncio, "create_subprocess_shell", spawn)
    return seen


async def _run_until_done(job: "srv.Job", *, ticks: int = 500) -> None:
    runner = asyncio.create_task(srv.job_runner())
    try:
        for _ in range(ticks):
            if job.status in (srv.JobStatus.COMPLETED, srv.JobStatus.FAILED) and job.finished_at:
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


def _quiet_runner(monkeypatch, tmp_path) -> str:
    """Keep the runner off the device; return a workspace whose default python env exists."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    ws = tmp_path / "ws"
    (ws / "tt-metal" / "python_env" / "bin").mkdir(parents=True)
    (ws / "tt-metal" / "python_env" / "bin" / "activate").write_text("")
    return str(ws)


def _job(tmp_path, ws: str, job_id: str, command: str) -> "srv.Job":
    job = srv.Job(
        id=job_id,
        owner="tenant",
        workspace=ws,
        command=command,
        queued_at="t",
        log_file=str(tmp_path / f"{job_id}.log"),
    )
    srv.jobs[job_id] = job
    return job


# --- I17: the spool -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_job_writes_its_output_to_a_spool_not_a_pipe(monkeypatch, tmp_path, clear_job_state):
    ws = _quiet_runner(monkeypatch, tmp_path)
    seen = _record_spawn(monkeypatch)

    job = _job(tmp_path, ws, "950", "echo to-out; echo to-err >&2")
    await srv.get_job_queue().put("950")
    await _run_until_done(job)

    assert job.status is srv.JobStatus.COMPLETED, job.error
    out, err = seen[0]
    assert isinstance(out, int) and isinstance(err, int), f"the job was given pipes, not spool files: {seen[0]}"
    assert out != asyncio.subprocess.PIPE and err != asyncio.subprocess.PIPE
    log = Path(job.log_file).read_text()
    assert "[stdout] to-out" in log and "[stderr] to-err" in log, log
    assert not any(_spool_dir().glob("950.*")), "a finalized job's spool was left behind"


@pytest.mark.asyncio
async def test_a_readopted_job_keeps_printing_and_its_log_gets_every_line_once(monkeypatch, tmp_path, clear_job_state):
    """The broker that spawned the job copied line1 and died; the job printed line2 while no
    broker ran and line3 after the next one re-adopted it. The log gets each line once."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path / "exit"))
    (tmp_path / "exit").mkdir()
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.05)
    monkeypatch.setattr(spool, "SPOOL_POLL_SEC", 0.01)

    fds = spool.open_job_spool("960")
    assert fds, "spool dir unusable in the test environment"
    os.write(fds["stdout"], b"line1\n")
    offsets = spool.SpoolOffsets("960")
    offsets.set("stdout", len(b"line1\n"))  # the old broker's last record
    offsets.close()
    log = tmp_path / "960.log"
    log.write_text("[00:00:00] [stdout] line1\n")
    os.write(fds["stdout"], b"line2\n")  # no broker running

    srv.jobs["960"] = srv.Job(
        id="960",
        owner="tenant",
        workspace="/w",
        command="python long.py",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        log_file=str(log),
    )
    alive = {"n": 0}

    def scope_active(scope):
        alive["n"] += 1
        if alive["n"] == 3:
            os.write(fds["stdout"], b"line3\n")  # still printing after the restart
        if alive["n"] == 6:
            (tmp_path / "exit" / "960.exit").write_text("0")
            return False
        return True

    monkeypatch.setattr(srv, "_scope_active", scope_active)
    await srv._monitor_readopted_scope("960", "ttdev-job-960.scope")
    for fd in fds.values():
        os.close(fd)

    text = log.read_text()
    for line in ("line1", "line2", "line3"):
        assert text.count(f"] {line}\n") == 1, f"{line} is not in the log exactly once:\n{text}"
    assert srv.jobs["960"].status is srv.JobStatus.COMPLETED
    assert not any(_spool_dir().glob("960.*")), "the re-adopted job's spool was left behind"


def test_the_spool_frees_what_the_broker_has_copied(monkeypatch, tmp_path):
    monkeypatch.setattr(spool, "SPOOL_PUNCH_MIN_BYTES", 64 * 1024)
    path = tmp_path / "j.stdout"
    line = b"x" * 1023 + b"\n"
    path.write_bytes(line * 512)  # 512 KiB
    size = path.stat().st_size
    tail = spool.SpoolTail(path, lambda: True)

    async def drain():
        while await tail.readline():
            tail.release()

    asyncio.run(drain())
    tail.close()
    st = path.stat()
    assert st.st_size == size, "punching must not move later offsets"
    assert st.st_blocks * 512 < size // 2, f"the copied spool still holds {st.st_blocks * 512} of {size} bytes"


def test_a_spool_reader_ends_as_the_job_exits_not_a_poll_later(monkeypatch, tmp_path):
    """A pipe reads EOF the moment the job closes it; a spool only learns the job ended by
    polling. The reader waits on the job's exit as well, so a short job is not held a poll longer."""
    monkeypatch.setattr(spool, "SPOOL_POLL_SEC", 5.0)
    path = tmp_path / "j.stdout"
    path.write_bytes(b"last\n")

    async def drain():
        exited = asyncio.get_running_loop().create_future()
        tail = spool.SpoolTail(path, exited.done, exited=exited)
        assert await tail.readline() == b"last\n"
        asyncio.get_running_loop().call_later(0.05, exited.set_result, 0)
        t0 = time.monotonic()
        assert await tail.readline() == b""
        tail.close()
        return time.monotonic() - t0

    took = asyncio.run(drain())
    assert took < 1.0, f"the reader saw the job's exit {took:.2f}s late, on its next poll"


@pytest.mark.parametrize("why", ["not-a-dir", "world-writable"])
@pytest.mark.asyncio
async def test_an_unusable_spool_dir_falls_back_to_pipes(monkeypatch, tmp_path, clear_job_state, why):
    ws = _quiet_runner(monkeypatch, tmp_path)
    if why == "not-a-dir":
        (tmp_path / "file").write_text("")
        monkeypatch.setenv("TT_DEVICE_MCP_JOB_SPOOL_DIR", str(tmp_path / "file" / "spool"))
    else:
        d = tmp_path / "open"
        d.mkdir()
        d.chmod(0o777)
        monkeypatch.setenv("TT_DEVICE_MCP_JOB_SPOOL_DIR", str(d))
    seen = _record_spawn(monkeypatch)

    job = _job(tmp_path, ws, "970", "echo still-ran")
    await srv.get_job_queue().put("970")
    await _run_until_done(job)

    assert seen and seen[0] == (asyncio.subprocess.PIPE, asyncio.subprocess.PIPE)
    assert job.status is srv.JobStatus.COMPLETED, "a job was refused for want of a spool"
    assert "[stdout] still-ran" in Path(job.log_file).read_text()


def test_startup_sweeps_spools_of_jobs_not_readopted(monkeypatch):
    for jid in ("980", "981"):
        fds = spool.open_job_spool(jid)
        for fd in fds.values():
            os.close(fd)
        spool.SpoolOffsets(jid).close()
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "list_active_job_scopes", lambda: {"981": "ttdev-job-981.scope"})
    monkeypatch.setattr(srv, "_job_from_log", lambda jid: None)
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: coro.close())

    asyncio.run(srv.reconcile_running_scopes())

    assert not any(_spool_dir().glob("980.*")), "the spool of a job that ended while no broker ran was kept"
    assert {p.name for p in _spool_dir().glob("981.*")} == {"981.stdout", "981.stderr", "981.offsets"}


def test_a_spool_line_with_no_newline_is_still_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(spool, "SPOOL_MAX_LINE_BYTES", 4096)
    path = tmp_path / "j.stdout"
    path.write_bytes(b"y" * 20000 + b"\nend")
    tail = spool.SpoolTail(path, lambda: True)

    async def lines():
        out = []
        while chunk := await tail.readline():
            out.append(chunk)
        return out

    got = asyncio.run(lines())
    tail.close()
    assert max(len(c) for c in got) <= 4096, "the reader buffered an unbounded line"
    assert b"".join(got) == b"y" * 20000 + b"\nend", "bounding the line lost or reordered bytes"


# --- I18: the dispatch hold -----------------------------------------------------------------------


def _hold(text: str) -> Path:
    path = srv.dispatch_hold()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.asyncio
async def test_no_job_dispatches_while_the_update_holds_dispatch(monkeypatch, tmp_path, clear_job_state):
    ws = _quiet_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "DISPATCH_HOLD_POLL_SEC", 0.02)
    hold = _hold(f"{os.getpid()} {int(time.time())}\n")
    marker = tmp_path / "ran"

    job = _job(tmp_path, ws, "990", f"touch {marker}")
    srv._persist_queued_job(job)
    await srv.get_job_queue().put("990")

    runner = asyncio.create_task(srv.job_runner())
    try:
        await asyncio.sleep(0.5)
        assert job.status is srv.JobStatus.QUEUED, f"job 990 reached {job.status.value} under the update's hold"
        assert not marker.exists()
        assert (tmp_path / srv.QUEUED_SPEC_DIR / "990.json").exists(), "the held job's spec left the disk"

        hold.unlink()  # the update is done (or decided not to restart)
        for _ in range(250):
            if job.finished_at:
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.COMPLETED, job.error
    assert marker.exists()


@pytest.mark.parametrize("hold", ["dead-pid", "overdue", "garbage"])
@pytest.mark.asyncio
async def test_a_dead_or_overdue_hold_is_ignored(monkeypatch, tmp_path, clear_job_state, hold):
    ws = _quiet_runner(monkeypatch, tmp_path)
    if hold == "dead-pid":
        import subprocess

        p = subprocess.Popen(["true"])
        p.wait()
        path = _hold(f"{p.pid} {int(time.time())}\n")
    elif hold == "overdue":
        path = _hold(f"{os.getpid()} {int(time.time() - srv.DISPATCH_HOLD_MAX_SEC - 60)}\n")
    else:
        path = _hold("not a hold\n")

    job = _job(tmp_path, ws, "995", "echo ran")
    await srv.get_job_queue().put("995")
    await _run_until_done(job, ticks=150)

    assert job.status is srv.JobStatus.COMPLETED, f"a {hold} hold wedged the queue ({job.status.value})"
    assert not path.exists(), f"the {hold} hold was left in place"


@pytest.mark.asyncio
async def test_a_job_held_by_the_update_is_gated_again_before_it_runs(monkeypatch, tmp_path, clear_job_state):
    """The hold can stand for minutes and the device can go bad under it, so the verdict the gate
    gave before the wait is stale: the job is admitted again once the hold ends, and refused here."""
    ws = _quiet_runner(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD", "0")  # a degraded verdict refuses at once
    monkeypatch.setattr(srv, "DISPATCH_HOLD_POLL_SEC", 0.02)
    hold = _hold(f"{os.getpid()} {int(time.time())}\n")
    held_at_verdict = []

    async def admission(job, job_log_file):
        held_at_verdict.append(hold.exists())
        return "" if len(held_at_verdict) == 1 else "device degraded: a fault opened during the hold"

    monkeypatch.setattr(srv, "_admission_verdict", admission)
    marker = tmp_path / "ran"
    job = _job(tmp_path, ws, "996", f"touch {marker}")
    await srv.get_job_queue().put("996")

    runner = asyncio.create_task(srv.job_runner())
    try:
        await asyncio.sleep(0.3)
        assert job.status is srv.JobStatus.QUEUED and held_at_verdict == [True]
        hold.unlink()
        for _ in range(250):
            if job.finished_at:
                break
            await asyncio.sleep(0.02)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert held_at_verdict == [True, False], "the job was not admitted again once the hold ended"
    assert job.status is srv.JobStatus.FAILED and "degraded" in (job.error or ""), job.error
    assert not marker.exists(), "the job ran on the verdict from before the hold"


@pytest.mark.asyncio
async def test_a_step_that_reserves_the_device_during_the_hold_is_waited_out(monkeypatch, tmp_path, clear_job_state):
    """A scheduler step that reserves the device while the update holds dispatch: once the hold ends
    the job waits for the step to release it, rather than starting under the step's own gate."""
    ws = _quiet_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "DISPATCH_HOLD_POLL_SEC", 0.02)
    hold = _hold(f"{os.getpid()} {int(time.time())}\n")
    marker = tmp_path / "ran"
    job = _job(tmp_path, ws, "997", f"touch {marker}")
    await srv.get_job_queue().put("997")

    runner = asyncio.create_task(srv.job_runner())
    token = None
    try:
        await asyncio.sleep(0.3)
        assert job.status is srv.JobStatus.QUEUED
        token = srv._reserve_external_step("pre-step")
        hold.unlink()
        await asyncio.sleep(0.4)
        assert job.status is srv.JobStatus.QUEUED, f"job 997 reached {job.status.value} under a step's reservation"
        assert not marker.exists()
        srv._release_external_step(token)
        token = None
        for _ in range(250):
            if job.finished_at:
                break
            await asyncio.sleep(0.02)
    finally:
        if token is not None:
            srv._release_external_step(token)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.COMPLETED, job.error
    assert marker.exists()
