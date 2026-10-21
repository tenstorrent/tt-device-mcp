# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A job's exit file is always its own (spec 01 I8).

Ids recycle ("000".."999"), and the shared exit dir is sticky 1777. A file an earlier job left at
`<id>.exit` could be read after a restart as the new job's status, and when another user owns it,
fs.protected_regular stops even a root job's EXIT trap from opening it, so the job's status is
lost. A re-adopted job whose status is missing, unreadable or stale is unknown: never a clean exit,
always a reset plus the full check before the next job.
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

import tt_device_mcp.server as srv
from tests.test_restart_drain import _job, _quiet_runner, _run_until_done


def _ago(sec: float) -> str:
    return srv.datetime.fromtimestamp(time.time() - sec).isoformat()


def _recording_dirty(monkeypatch) -> list:
    marks: list = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, job=None, **k: marks.append(reason))
    return marks


def _readopted(job_id: str, started_at) -> "srv.Job":
    job = srv.Job(
        id=job_id,
        owner="tenant",
        workspace="/w",
        command="pytest",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        started_at=started_at,
    )
    srv.jobs[job_id] = job
    return job


# --- before the job starts ----------------------------------------------------------------------


def test_a_stale_exit_file_is_replaced_and_given_to_the_jobs_uid(monkeypatch, tmp_path):
    """A file left by an earlier job (owned by another user in the real dir) is removed and a fresh
    empty one is created for the job's uid, so the job's own trap can write it."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    stale = tmp_path / "043.exit"
    stale.write_text("0")
    chowned = []
    monkeypatch.setattr(srv.os, "geteuid", lambda: 0)  # the broker runs as root
    monkeypatch.setattr(srv.os, "fchown", lambda fd, uid, gid: chowned.append((uid, gid)))

    assert srv.prepare_job_exit_file("043", 4321)

    assert stale.read_text() == "", "the earlier job's status survived into the new job's file"
    assert chowned == [(4321, -1)], f"the fresh file was not given to the job's uid: {chowned}"
    assert srv.read_job_exit_code("043", _ago(5)) is None, "an empty file read as a status"

    # The job's own EXIT trap writes into it.
    script = srv._exit_trap_preamble(str(stale)) + "exit 3\n"
    subprocess.run(["/bin/bash", "-c", script], check=False)
    assert stale.read_text() == "3"
    assert srv.read_job_exit_code("043", _ago(5)) == 3


def test_no_chown_for_a_job_running_as_the_brokers_own_uid(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    chowned = []
    monkeypatch.setattr(srv.os, "fchown", lambda *a: chowned.append(a))
    assert srv.prepare_job_exit_file("044", os.geteuid())
    assert (tmp_path / "044.exit").read_text() == ""
    assert chowned == []


def test_a_file_the_job_could_not_write_is_not_left_behind(monkeypatch, tmp_path):
    """A root-owned file a non-root job cannot write is worse than none: the trap creates its own."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    (tmp_path / "045.exit").write_text("0")
    monkeypatch.setattr(srv.os, "geteuid", lambda: 0)

    def denied(fd, uid, gid):
        raise PermissionError("chown")

    monkeypatch.setattr(srv.os, "fchown", denied)
    assert not srv.prepare_job_exit_file("045", 4321)
    assert not (tmp_path / "045.exit").exists()


@pytest.mark.asyncio
async def test_the_runner_starts_every_job_on_a_fresh_exit_file_its_trap_writes(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path / "exit"))
    (tmp_path / "exit").mkdir()
    ws = _quiet_runner(monkeypatch, tmp_path)
    exit_file = tmp_path / "exit" / "970.exit"
    exit_file.write_text("0")  # an earlier job 970's clean exit

    uids, seen = [], []
    real_prepare, real_clear = srv.prepare_job_exit_file, srv.clear_job_exit_file

    def prepare(job_id, uid=None):
        uids.append(uid)
        return real_prepare(job_id, uid)

    def clear(job_id):
        if job_id == "970" and exit_file.exists():
            seen.append(exit_file.read_text())
        real_clear(job_id)

    monkeypatch.setattr(srv, "prepare_job_exit_file", prepare)
    monkeypatch.setattr(srv, "clear_job_exit_file", clear)

    # Exits 7 if the earlier job's status is still there when it starts.
    job = _job(tmp_path, ws, "970", f"test ! -s {exit_file} || exit 7; exit 3")
    await srv.get_job_queue().put("970")
    await _run_until_done(job)

    assert uids == [os.geteuid()], f"the exit file was not prepared for the job's uid: {uids}"
    assert job.exit_code == 3, f"the job saw the earlier job's exit file (exit {job.exit_code})"
    assert seen and seen[0] == "3", f"the job's trap did not write its status: {seen}"
    assert not exit_file.exists()


# --- startup ------------------------------------------------------------------------------------


def test_the_startup_sweep_removes_orphans_and_keeps_readopted_jobs_files(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "list_active_job_scopes", lambda: {"050": "ttdev-job-050.scope"})
    monkeypatch.setattr(srv, "_job_from_log", lambda jid: _readopted(jid, _ago(60)))
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: coro.close())
    for jid in ("050", "051", "052"):
        (tmp_path / f"{jid}.exit").write_text("0")

    asyncio.run(srv.reconcile_running_scopes())

    assert (tmp_path / "050.exit").exists(), "the re-adopted job's exit file was swept"
    assert (
        not (tmp_path / "051.exit").exists() and not (tmp_path / "052.exit").exists()
    ), "an orphan exit file survived startup; a recycled id would inherit its status"


def test_the_sweep_keeps_only_what_it_is_told_to(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    for jid in ("060", "061"):
        (tmp_path / f"{jid}.exit").write_text("1")
    (tmp_path / "unrelated.txt").write_text("x")
    assert srv.sweep_job_exit_files(lambda jid: jid == "060") == ["061"]
    assert (tmp_path / "060.exit").exists() and (tmp_path / "unrelated.txt").exists()


# --- re-adoption: unknown is never exit 0 -------------------------------------------------------


def _missing(path: Path) -> None:
    pass


def _unreadable(path: Path) -> None:
    path.mkdir()  # reading it fails as an OSError, like a file root cannot open


def _malformed(path: Path) -> None:
    path.write_text("")


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_missing, _unreadable, _malformed], ids=["missing", "unreadable", "empty"])
async def test_a_readopted_job_with_no_usable_exit_status_gets_reset_and_full_check(
    monkeypatch, tmp_path, clear_job_state, make
):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    marks = _recording_dirty(monkeypatch)
    _readopted("071", _ago(60))
    make(tmp_path / "071.exit")

    await srv._monitor_readopted_scope("071", "ttdev-job-071.scope")

    job = srv.jobs["071"]
    assert job.status is srv.JobStatus.FAILED and job.exit_code is None
    assert (
        len(marks) == 1 and "071" in marks[0] and "unknown" in marks[0]
    ), f"a re-adopted job with no known exit status released the device without a reset: {marks}"


@pytest.mark.asyncio
async def test_an_unknown_exit_status_really_flags_the_device(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    _readopted("072", _ago(60))

    await srv._monitor_readopted_scope("072", "ttdev-job-072.scope")

    assert srv._device_unavailable_for_tenant(), "the next tenant would be dispatched with no reset"


@pytest.mark.asyncio
async def test_a_stale_exit_0_from_an_earlier_job_is_never_this_jobs_status(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    marks = _recording_dirty(monkeypatch)
    stale = tmp_path / "073.exit"
    stale.write_text("0")
    hour_ago = time.time() - 3600
    os.utime(stale, (hour_ago, hour_ago))  # written by the job 073 of an hour ago
    _readopted("073", _ago(60))

    await srv._monitor_readopted_scope("073", "ttdev-job-073.scope")

    job = srv.jobs["073"]
    assert (
        job.exit_code is None and job.status is srv.JobStatus.FAILED
    ), f"an earlier job's exit 0 was read as this job's status: {job.status} {job.exit_code}"
    assert len(marks) == 1 and "073" in marks[0]


def test_an_exit_file_is_read_only_against_a_known_start(monkeypatch, tmp_path):
    """No start time cannot prove the file is this job's; one written before the start is not."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    (tmp_path / "074.exit").write_text("0")
    assert srv.read_job_exit_code("074", None) is None
    assert srv.read_job_exit_code("074", "not a time") is None
    assert srv.read_job_exit_code("074", _ago(60)) == 0
    assert srv.read_job_exit_code("074", _ago(-3600)) is None  # the job started after the write
