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
import json
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
    real_euid = srv.os.geteuid
    monkeypatch.setattr(srv.os, "geteuid", lambda: 0)  # the broker runs as root
    monkeypatch.setattr(srv.os, "fchown", lambda fd, uid, gid: chowned.append((uid, gid)))

    assert srv.prepare_job_exit_file("043", 4321)
    monkeypatch.setattr(srv.os, "geteuid", real_euid)  # the faked chown left the file ours

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
    assert list(tmp_path.iterdir()) == [], "the half-made fresh file was left behind"


def test_a_co_tenant_cannot_win_the_path_while_the_fresh_file_is_made(monkeypatch, tmp_path, caplog):
    """Review #1864 F1: in the sticky shared dir another user may create ``<id>.exit`` at any moment.
    Removing the old file and then creating the new one leaves a window where theirs wins and the
    job starts on a file it cannot write; the fresh file must replace the path in one step."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    path = tmp_path / "046.exit"
    path.write_text("0")  # the co-tenant's planted clean exit
    real_unlink, real_path_unlink = srv.os.unlink, srv.Path.unlink

    def replant(p):
        if Path(p) == path:
            path.write_text("0")  # the co-tenant is always quicker than the next syscall

    def os_unlink(p, *a, **k):
        real_unlink(p, *a, **k)
        replant(p)

    def path_unlink(self, *a, **k):
        real_path_unlink(self, *a, **k)
        replant(self)

    monkeypatch.setattr(srv.os, "unlink", os_unlink)
    monkeypatch.setattr(srv.Path, "unlink", path_unlink)
    planted = path.stat().st_ino

    assert srv.prepare_job_exit_file("046", os.geteuid()), "the job started on a file it did not get"
    assert path.read_text() == "", "the co-tenant's status sits in the job's exit file"
    assert path.stat().st_ino != planted
    assert [p.name for p in tmp_path.iterdir()] == ["046.exit"], "the fresh file's temp name was left"


def test_a_failed_prepare_warns_once_and_leaves_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    (tmp_path / "047.exit").write_text("0")
    warnings: list = []
    monkeypatch.setattr(srv, "logger", type("L", (), {"warning": lambda self, m: warnings.append(m)})())

    def no_rename(src, dst):
        raise PermissionError("sticky")

    monkeypatch.setattr(srv.os, "rename", no_rename)
    assert not srv.prepare_job_exit_file("047", os.geteuid())
    assert len(warnings) == 1 and "047" in warnings[0], warnings
    assert list(tmp_path.iterdir()) == [], "a stale file or the temp file survived a failed prepare"


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


def test_the_sweep_removes_fresh_files_a_dead_broker_never_renamed(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    (tmp_path / ".062.exit.abc123.tmp").write_text("")
    (tmp_path / "063.exit").write_text("0")
    assert srv.sweep_job_exit_files(lambda jid: jid == "063") == []
    assert [p.name for p in tmp_path.iterdir()] == ["063.exit"]


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


# --- the read side trusts only a plain file the job could have written --------------------------


def _symlink(path: Path) -> None:
    target = path.parent / "elsewhere"
    target.write_text("0")
    path.symlink_to(target)


def _hardlink(path: Path) -> None:
    target = path.parent / "elsewhere"
    target.write_text("0")
    os.link(target, path)


def test_an_exit_file_that_is_not_one_plain_file_reads_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    for jid, make in (("075", _symlink), ("076", _hardlink)):
        make(tmp_path / f"{jid}.exit")
        assert srv.read_job_exit_code(jid, _ago(60)) is None, f"{make.__name__} read as a status"
        for p in tmp_path.iterdir():
            p.unlink()
    fifo = tmp_path / "077.exit"
    os.mkfifo(fifo)  # a FIFO must neither block the read nor pass for a status
    assert srv.read_job_exit_code("077", _ago(60)) is None


def test_an_exit_file_another_user_owns_reads_as_unknown(monkeypatch, tmp_path):
    """A file owned by neither the broker nor the job's submitter is a co-tenant's (review #1864 F1)."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    (tmp_path / "078.exit").write_text("0")
    mine = os.geteuid()
    assert srv.read_job_exit_code("078", _ago(60), owner_uid=mine) == 0  # the job's submitter wrote it
    monkeypatch.setattr(srv.os, "geteuid", lambda: mine + 1)  # now neither broker nor submitter
    assert srv.read_job_exit_code("078", _ago(60)) is None
    assert srv.read_job_exit_code("078", _ago(60), owner_uid=mine + 2) is None
    assert srv.read_job_exit_code("078", _ago(60), owner_uid=mine) == 0


@pytest.mark.asyncio
async def test_a_co_tenants_exit_0_never_releases_a_readopted_job_without_a_reset(
    monkeypatch, tmp_path, clear_job_state
):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    marks = _recording_dirty(monkeypatch)
    (tmp_path / "079.exit").write_text("0")  # owned by "someone else" once geteuid moves
    mine = os.geteuid()
    job = _readopted("079", _ago(60))
    job.peer_uid = mine + 2
    monkeypatch.setattr(srv.os, "geteuid", lambda: mine + 1)

    await srv._monitor_readopted_scope("079", "ttdev-job-079.scope")

    assert job.status is srv.JobStatus.FAILED and job.exit_code is None, (job.status, job.exit_code)
    assert len(marks) == 1 and "079" in marks[0] and "unknown" in marks[0], marks


# --- the submitter's uid survives a broker restart ------------------------------------------------


async def _submit_with_a_real_header(monkeypatch, tmp_path, peer_uid: int) -> str:
    """Queue a job over the socket path as ``peer_uid`` and mark it started the way the runner
    does, so its log on disk carries the header the broker really writes."""
    from tt_device_mcp.socket_transport import current_peer_uid, current_via_mcp

    srv.device_op_lock = None
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "job_runner_task", None)
    monkeypatch.setattr(srv, "job_runner", lambda: asyncio.Event().wait())
    mcp = srv.create_mcp_server()
    tok, mtok = current_peer_uid.set(peer_uid), current_via_mcp.set(False)
    try:
        res = await mcp.call_tool(
            "tt_device_job_run_bg", {"params": {"workspace": str(tmp_path), "command": "sleep 100"}}
        )
    finally:
        current_peer_uid.reset(tok)
        current_via_mcp.reset(mtok)
    job_id = json.loads(res.content[0].text)["job_id"]
    with open(srv.jobs[job_id].log_file, "a") as f:
        f.write(f"[Started at {_ago(60)}]\n\n")
    # The broker restarts: nothing in memory survives, only the scope and the log.
    srv.jobs.clear()
    srv.job_queue = asyncio.Queue()
    return job_id


async def _readopt_after_restart(monkeypatch, job_id: str, *, broker_euid: int) -> "srv.Job":
    """reconcile_running_scopes re-adopts the job, then the (root) broker reads its exit file."""
    monitors: list = []
    monkeypatch.setattr(srv, "list_active_job_scopes", lambda: {job_id: f"ttdev-job-{job_id}.scope"})
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: monitors.append(coro))
    monkeypatch.setattr(srv, "readopted_scopes", {})
    await srv.reconcile_running_scopes()
    assert len(monitors) == 1
    # Under privsep the broker is root and the exit file belongs to the submitter: the test's own
    # uid plays the submitter, so the broker's must be another one.
    monkeypatch.setattr(srv.os, "geteuid", lambda: broker_euid)
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    await monitors[0]
    return srv.jobs[job_id]


@pytest.mark.asyncio
async def test_a_readopted_privsep_jobs_clean_exit_is_still_its_own(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    marks = _recording_dirty(monkeypatch)
    submitter = os.geteuid()
    job_id = await _submit_with_a_real_header(monkeypatch, tmp_path, submitter)
    (tmp_path / f"{job_id}.exit").write_text("0")  # the job's trap, as its submitter

    job = await _readopt_after_restart(monkeypatch, job_id, broker_euid=submitter + 1)

    assert job.peer_uid == submitter, "the submitter's uid did not survive the restart"
    assert job.status is srv.JobStatus.COMPLETED and job.exit_code == 0, (job.status, job.exit_code)
    assert marks == [], f"a verified clean exit was flagged for a reset: {marks}"


@pytest.mark.asyncio
async def test_a_co_tenants_file_is_still_unknown_for_a_readopted_job(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    marks = _recording_dirty(monkeypatch)
    co_tenant = os.geteuid()
    job_id = await _submit_with_a_real_header(monkeypatch, tmp_path, co_tenant + 2)
    (tmp_path / f"{job_id}.exit").write_text("0")  # written by neither the broker nor the submitter

    job = await _readopt_after_restart(monkeypatch, job_id, broker_euid=co_tenant + 1)

    assert job.peer_uid == co_tenant + 2
    assert job.status is srv.JobStatus.FAILED and job.exit_code is None, (job.status, job.exit_code)
    assert len(marks) == 1 and job_id in marks[0] and "unknown" in marks[0], marks


@pytest.mark.asyncio
async def test_a_readopted_job_with_no_log_has_no_uid_and_its_status_stays_unknown(
    monkeypatch, tmp_path, clear_job_state
):
    """The fallback Job of reconcile_running_scopes has no header to read a uid from: a file the
    broker did not write is then no status (fail-safe), never a clean exit."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "job_log_dir", tmp_path / "logs")  # no log for this job
    marks = _recording_dirty(monkeypatch)
    srv.jobs.clear()
    (tmp_path / "083.exit").write_text("0")
    real = os.geteuid()

    job = await _readopt_after_restart(monkeypatch, "083", broker_euid=real + 1)

    assert job.command == "(re-adopted ttdev-job-083.scope)" and job.peer_uid is None
    assert job.status is srv.JobStatus.FAILED and job.exit_code is None, (job.status, job.exit_code)
    assert len(marks) == 1 and "083" in marks[0] and "unknown" in marks[0], marks


def _header(job_id: str, *lines: str) -> str:
    return "=" * 70 + "\n" + f"JOB ID:      {job_id}\n" + "".join(f"{ln}\n" for ln in lines)


@pytest.mark.parametrize(
    "text",
    [
        # A log from before the uid line: a tenant's command or env text must not stand in for it.
        _header("084", "OWNER:       tenant", "COMMAND:     x", "SUBMITTER UID: 4242"),
        _header("084", "OWNER:       tenant", "COMMAND:     echo\nSUBMITTER UID: 4242"),
        # The line is only ever right after JOB ID, for this job.
        _header("085", "SUBMITTER UID: 4242"),
        "JOB ID:      084\nSUBMITTER UID: 4242\n",
        _header("084", "SUBMITTER UID: -1"),
        _header("084", "SUBMITTER UID: ٤٢"),
    ],
    ids=["old-header", "command-text", "other-job", "no-rule-line", "negative", "non-ascii-digits"],
)
def test_a_uid_anywhere_but_the_brokers_own_header_line_is_ignored(monkeypatch, tmp_path, text):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    (tmp_path / "2026-10-20_100000_084.log").write_text(text + "[Started at 2026-10-20T10:00:00]\n")

    job = srv._job_from_log("084")

    assert job is not None and job.peer_uid is None, job and job.peer_uid


def test_the_brokers_header_line_names_the_submitter(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    (tmp_path / "2026-10-20_100000_086.log").write_text(_header("086", "SUBMITTER UID: 4242", "OWNER:       t"))

    assert srv._job_from_log("086").peer_uid == 4242


@pytest.mark.parametrize("how", ["another-owner", "world-writable", "group-writable-foreign-group"])
def test_a_log_another_user_could_have_written_names_no_submitter(monkeypatch, tmp_path, how):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    log = tmp_path / "2026-10-20_100000_087.log"
    log.write_text(_header("087", "SUBMITTER UID: 4242"))
    log.chmod(0o644)
    real = os.geteuid()
    if how == "another-owner":
        monkeypatch.setattr(srv.os, "geteuid", lambda: real + 1)
    elif how == "world-writable":
        log.chmod(0o646)
    else:
        log.chmod(0o664)
        egid = os.getegid()
        monkeypatch.setattr(srv.os, "getegid", lambda: egid + 1)

    assert srv._job_from_log("087").peer_uid is None
