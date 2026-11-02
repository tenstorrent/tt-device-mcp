# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A job whose tt-metal output dirs its user cannot write fails at the door (spec 01 I68).

Motivating incident: a tt-metal session run as root left ``<workspace>/generated/`` and its
subdirs root-owned. The next job of the workspace's owner went through the broker, died in each
run on ``Permission denied [<workspace>/generated/watcher/]``, and sat until its timeout.
"""

import asyncio
import os
import stat
from pathlib import Path

import pytest

import tt_device_mcp.job_preflight as pf
import tt_device_mcp.server as srv
from tests.test_restart_drain import _job, _quiet_runner, _run_until_done

OTHER_UID = os.geteuid() + 12345  # a uid that is neither us nor root: the mode-bit path


def _owned_by(monkeypatch, path: Path, uid: int, gid: int = 0) -> None:
    """Make ``os.stat`` report ``path`` as owned by ``uid:gid``, as a root-run session leaves it."""
    real = os.stat

    def fake(p, *a, **kw):
        st = real(p, *a, **kw)
        if Path(p) == path:
            fields = list(st)
            fields[stat.ST_UID], fields[stat.ST_GID] = uid, gid
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(pf.os, "stat", fake)


@pytest.fixture
def no_root():
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits")


# --- check_dir ----------------------------------------------------------------------------------


def test_a_root_owned_existing_dir_is_refused_with_its_owner_and_mode(monkeypatch, tmp_path):
    gen = tmp_path / "generated"
    gen.mkdir(mode=0o755)
    gen.chmod(0o755)
    _owned_by(monkeypatch, gen, 0, 0)
    problem = pf.check_dir(gen, OTHER_UID, (OTHER_UID,))
    assert problem is not None
    assert str(gen) in problem and "mode 0755" in problem and f"uid {OTHER_UID}" in problem
    assert "owner root:" in problem


def test_an_unwritable_dir_of_our_own_is_refused(no_root, tmp_path):
    gen = tmp_path / "generated"
    gen.mkdir()
    gen.chmod(0o555)
    try:
        assert "not writable" in pf.check_dir(gen, os.geteuid(), ())
    finally:
        gen.chmod(0o755)


def test_a_writable_dir_passes(tmp_path):
    gen = tmp_path / "generated"
    gen.mkdir()
    assert pf.check_dir(gen, os.geteuid(), ()) is None


def test_a_dir_writable_through_its_group_passes(monkeypatch, tmp_path):
    gen = tmp_path / "generated"
    gen.mkdir()
    gen.chmod(0o775)
    _owned_by(monkeypatch, gen, 0, 4242)
    assert pf.check_dir(gen, OTHER_UID, (OTHER_UID, 4242)) is None
    assert pf.check_dir(gen, OTHER_UID, (OTHER_UID,)) is not None


def test_a_dir_with_an_acl_is_not_refused_on_its_mode_bits(monkeypatch, tmp_path):
    gen = tmp_path / "generated"
    gen.mkdir()
    gen.chmod(0o755)
    _owned_by(monkeypatch, gen, 0, 0)
    monkeypatch.setattr(pf.os, "listxattr", lambda p: [pf._ACL_XATTR])
    assert pf.check_dir(gen, OTHER_UID, (OTHER_UID,)) is None


def test_a_missing_dir_with_a_writable_parent_passes_and_is_not_created(tmp_path):
    gen = tmp_path / "generated"
    assert pf.check_dir(gen / "watcher", os.geteuid(), ()) is None
    assert not gen.exists(), "the preflight must create nothing"


def test_a_missing_dir_with_an_unwritable_parent_is_refused_naming_the_parent(monkeypatch, tmp_path):
    _owned_by(monkeypatch, tmp_path, 0, 0)
    tmp_path.chmod(0o755)
    problem = pf.check_dir(tmp_path / "generated", OTHER_UID, (OTHER_UID,))
    assert problem is not None
    assert "is missing and cannot be created" in problem and str(tmp_path) in problem and "mode 0755" in problem


def test_a_missing_dir_with_an_unwritable_parent_of_our_own_is_refused(no_root, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ws.chmod(0o555)
    try:
        assert "cannot be created" in pf.check_dir(ws / "generated", os.geteuid(), ())
    finally:
        ws.chmod(0o755)


def test_a_file_where_the_dir_should_be_is_refused(tmp_path):
    (tmp_path / "generated").write_text("")
    assert "not a directory" in pf.check_dir(tmp_path / "generated", os.geteuid(), ())
    assert "not a directory" in pf.check_dir(tmp_path / "generated" / "watcher", os.geteuid(), ())


# --- which dirs ---------------------------------------------------------------------------------


def test_the_dirs_are_generated_and_its_subdirs_under_the_workspace_by_default(tmp_path):
    dirs = pf.output_dirs(pf.tt_metal_logs_dir(str(tmp_path), {}, {}))
    assert dirs[0] == tmp_path / "generated"
    for sub in ("watcher", "dprint", "inspector", "fabric", "profiler", "noc_data"):
        assert tmp_path / "generated" / sub in dirs


def test_a_missing_workspace_is_left_to_the_job_not_called_an_output_dir_problem(tmp_path):
    # The job cannot cd there and fails with that error; "generated/ cannot be created under
    # <the nearest existing ancestor>" would send the reader after the wrong thing.
    assert pf.preflight_output_dirs(str(tmp_path / "no-such-ws"), {}, None, 4242) == []
    # an explicit TT_METAL_LOGS_PATH is still checked when the workspace is missing
    logs = tmp_path / "logs"
    assert pf.preflight_output_dirs(str(tmp_path / "no-such-ws"), {pf.LOGS_PATH_ENV: str(logs)}, None, 0) == []


def test_tt_metal_logs_path_overrides_the_workspace(tmp_path):
    ws, logs = tmp_path / "ws", tmp_path / "logs"
    # the job's own env wins over the env it inherits
    assert pf.tt_metal_logs_dir(str(ws), {pf.LOGS_PATH_ENV: str(logs)}, {pf.LOGS_PATH_ENV: "/elsewhere"}) == logs
    # the inherited env counts when the job's env leaves it out ...
    assert pf.tt_metal_logs_dir(str(ws), {}, {pf.LOGS_PATH_ENV: str(logs)}) == logs
    # ... and not when the job inherits nothing (a job scoped to another uid)
    assert pf.tt_metal_logs_dir(str(ws), {}, None) == ws
    # a relative value is taken from where the job starts
    assert pf.tt_metal_logs_dir(str(ws), {pf.LOGS_PATH_ENV: "out"}, None) == ws / "out"


def test_the_override_is_what_gets_checked(monkeypatch, tmp_path):
    ws, logs = tmp_path / "ws", tmp_path / "logs"
    (ws / "generated").mkdir(parents=True)
    logs.mkdir()
    uid = OTHER_UID
    monkeypatch.setattr(pf, "groups_for_uid", lambda u: (u,))
    _owned_by_many(monkeypatch, {ws / "generated": 0, ws: uid, logs: uid})
    assert pf.preflight_output_dirs(str(ws), {}, None, uid)  # the workspace's root-owned dir fails
    assert pf.preflight_output_dirs(str(ws), {pf.LOGS_PATH_ENV: str(logs)}, None, uid) == []


def _owned_by_many(monkeypatch, owners: dict) -> None:
    real = os.stat

    def fake(p, *a, **kw):
        st = real(p, *a, **kw)
        if Path(p) in owners:
            fields = list(st)
            fields[stat.ST_UID] = fields[stat.ST_GID] = owners[Path(p)]
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(pf.os, "stat", fake)


def test_one_root_owned_generated_is_one_problem_not_seven(monkeypatch, tmp_path):
    gen = tmp_path / "generated"
    (gen / "watcher").mkdir(parents=True)
    gen.chmod(0o755)
    (gen / "watcher").chmod(0o755)
    monkeypatch.setattr(pf, "groups_for_uid", lambda u: (u,))
    _owned_by_many(monkeypatch, {tmp_path: OTHER_UID, gen: 0, gen / "watcher": 0})
    problems = pf.preflight_output_dirs(str(tmp_path), {}, None, OTHER_UID)
    # generated/ itself, and the root-owned watcher/ that exists under it; not the missing ones
    assert len(problems) == 2, problems
    assert str(gen) in problems[0] and str(gen / "watcher") in problems[1]


def test_root_and_a_writable_workspace_pass(tmp_path):
    assert pf.preflight_output_dirs(str(tmp_path), {}, None, 0) == []
    assert pf.preflight_output_dirs(str(tmp_path), {}, None, os.geteuid()) == []


# --- the runner ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_runner_fails_the_job_at_the_door_and_spawns_nothing(monkeypatch, tmp_path, clear_job_state):
    ws = _quiet_runner(monkeypatch, tmp_path)
    marker = tmp_path / "ran"
    job = _job(tmp_path, ws, "981", f"touch {marker}")
    job.timeout_sec = 600
    problem = f"{ws}/generated (owner root:root, mode 0755) is not writable by uid 1000"
    monkeypatch.setattr(srv, "preflight_output_dirs", lambda *a: [problem])
    srv._persist_queued_job(job)
    await srv.get_job_queue().put("981")
    t0 = asyncio.get_running_loop().time()
    await _run_until_done(job)
    assert asyncio.get_running_loop().time() - t0 < 5, "the refusal burned time"
    assert job.status is srv.JobStatus.FAILED
    assert problem in job.error and "not writable" in job.error
    assert job.pid is None and not marker.exists(), "a refused job must never spawn"
    assert not (tmp_path / srv.QUEUED_SPEC_DIR / "981.json").exists(), "a refused job must not be revived"
    log = Path(job.log_file).read_text()
    assert "[REFUSED at" in log and problem in log and "TT_METAL_LOGS_PATH" in log


@pytest.mark.asyncio
async def test_the_runner_checks_as_the_privsep_submitter_with_only_its_env(monkeypatch, tmp_path, clear_job_state):
    seen = {}

    def spy(workspace, job_env, inherited, uid):
        seen.update(workspace=workspace, inherited=inherited, uid=uid)
        return []

    monkeypatch.setattr(srv, "preflight_output_dirs", spy)
    job = srv.Job(id="982", owner="t", workspace=str(tmp_path), command="true", queued_at="t", peer_uid=4321)
    monkeypatch.setattr(srv, "privsep_prefix_for", lambda uid, **kw: ["systemd-run"])
    assert await srv._job_output_dir_problems(job) == []
    assert seen == {"workspace": str(tmp_path), "inherited": None, "uid": 4321}
    monkeypatch.setattr(srv, "privsep_prefix_for", lambda uid, **kw: None)
    await srv._job_output_dir_problems(job)
    assert seen["uid"] == os.geteuid() and seen["inherited"] is not None


@pytest.mark.asyncio
async def test_a_preflight_that_errors_or_hangs_lets_the_job_through(monkeypatch, tmp_path):
    job = srv.Job(id="983", owner="t", workspace=str(tmp_path), command="true", queued_at="t")

    def boom(*a):
        raise RuntimeError("bug")

    monkeypatch.setattr(srv, "preflight_output_dirs", boom)
    assert await srv._job_output_dir_problems(job) == []

    def slow(*a):
        import time

        time.sleep(0.5)
        return ["late"]

    monkeypatch.setattr(srv, "preflight_output_dirs", slow)
    monkeypatch.setattr(srv, "JOB_OUTPUT_PREFLIGHT_TIMEOUT_SEC", 0.05)
    assert await srv._job_output_dir_problems(job) == []


@pytest.mark.asyncio
async def test_a_cancel_that_lands_as_the_preflight_ends_is_not_dropped(monkeypatch, tmp_path):
    """The preflight runs at the start of every job, on the runner task. On Python 3.10 a plain
    asyncio.wait_for returns the result when the cancel lands in the same loop step as the work
    finishing, so the runner ran the job, went back to its queue and never ended (spec 01, broker
    shutdown). Land the cancel at every loop step while the call is still pending: it always wins."""
    job = srv.Job(id="984", owner="t", workspace=str(tmp_path), command="true", queued_at="t")

    def finished_at_once(func, *args):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(func(*args))
        return fut

    monkeypatch.setattr(srv, "preflight_output_dirs", lambda *a: [])
    monkeypatch.setattr(srv.asyncio, "to_thread", finished_at_once)
    landed = 0
    for steps in range(8):
        task = asyncio.ensure_future(srv._job_output_dir_problems(job))
        for _ in range(steps):
            await asyncio.sleep(0)
        if task.done():
            break
        task.cancel()
        landed += 1
        with pytest.raises(asyncio.CancelledError):
            await task
    assert landed >= 3, "the sweep never reached the step where the work has finished"
