# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The survivor sweep that runs after every job (spec 01 I4).

These spawn real, harmless processes (bash, sleep) in their own sessions and check that the
sweep finds and ends the ones the job's process group no longer reaches, SIGTERM before
SIGKILL, and that whatever outlives SIGKILL is reported rather than silently left holding the
device. No device is touched.
"""

import os
import signal
import subprocess
import time

import pytest

from tt_device_mcp import job_reap


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _spawn_job(script: str, tag: str | None = None) -> subprocess.Popen:
    """A stand-in for the runner's job shell: its own session, so pid == pgid == sid."""
    env = dict(os.environ)
    if tag:
        env[job_reap.JOB_TAG_ENV] = tag
    return subprocess.Popen(
        ["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, env=env
    )


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _escaped_child(tag: str) -> tuple[int, int]:
    """A job shell that starts a child in its OWN session (setsid) and exits: the child is out of
    the job's process group and session, and its parent chain is gone. Returns (job pid, child pid)."""
    job = _spawn_job("setsid sleep 60 >/dev/null 2>&1 < /dev/null & echo $!", tag=tag)
    out, _ = job.communicate(timeout=5)
    return job.pid, int(out.strip())


@pytest.mark.asyncio
async def test_a_child_that_left_the_session_is_found_by_its_tag_and_ended():
    """The common escape on a host without privsep: the ladder and the final killpg reach only
    the job's process group, and a child that called setsid is not in it. Its parent has exited,
    so no parent chain leads to it either; only the job's tag in its environment does."""
    job_pid, child = _escaped_child("t-escaped-1")
    try:
        assert _alive(child)
        # What the runner used to rely on: the group is empty, so killpg cannot reach the child.
        assert child not in job_reap.job_processes(job_pid)
        with pytest.raises(ProcessLookupError):
            os.killpg(job_pid, 0)

        survivors = await job_reap.reap_job_survivors(
            "t1", job_pid, None, "t-escaped-1", term_grace_sec=2, kill_wait_sec=1, log=lambda _m: None
        )
        assert survivors == []
        assert _wait_dead(child), "a child that left the job's session outlived the sweep"
    finally:
        _kill_quietly(child)


@pytest.mark.asyncio
async def test_a_descendant_that_left_the_session_is_found_through_its_parent():
    """No tag (a child that scrubbed its environment): while its parent is still alive the
    parent chain leads to it."""
    job = _spawn_job("setsid sleep 60 >/dev/null 2>&1 < /dev/null & echo $!; wait")
    child = int(job.stdout.readline().strip())
    try:
        assert child in job_reap.job_processes(job.pid)
        survivors = await job_reap.reap_job_survivors(
            "t2", job.pid, None, None, term_grace_sec=2, kill_wait_sec=1, log=lambda _m: None
        )
        assert survivors == []
        assert _wait_dead(child)
        job.wait(timeout=5)
    finally:
        _kill_quietly(child)
        _kill_quietly(job.pid)
        job.wait(timeout=5)


@pytest.mark.asyncio
async def test_survivors_get_sigterm_before_sigkill(tmp_path):
    """SIGTERM first, so a leftover that handles it gets to clean up."""
    marker = tmp_path / "got-term"
    # A child that records SIGTERM and exits on it.
    job = _spawn_job(
        f"setsid bash -c 'trap \"touch {marker}; exit 0\" TERM; while :; do sleep 0.1; done' "
        ">/dev/null 2>&1 < /dev/null & echo $!",
        tag="t-term-2",
    )
    out, _ = job.communicate(timeout=5)
    child = int(out.strip())
    try:
        time.sleep(0.3)  # let the trap install
        await job_reap.reap_job_survivors(
            "t3", job.pid, None, "t-term-2", term_grace_sec=3, kill_wait_sec=1, log=lambda _m: None
        )
        assert _wait_dead(child)
        assert marker.exists(), "the survivor was SIGKILLed without a SIGTERM first"
    finally:
        _kill_quietly(child)


@pytest.mark.asyncio
async def test_a_survivor_that_ignores_sigterm_is_sigkilled_after_the_grace():
    job = _spawn_job(
        "setsid bash -c \"trap '' TERM; exec sleep 60\" >/dev/null 2>&1 < /dev/null & echo $!", tag="t-ignore"
    )
    out, _ = job.communicate(timeout=5)
    child = int(out.strip())
    try:
        time.sleep(0.2)
        start = time.monotonic()
        survivors = await job_reap.reap_job_survivors(
            "t4", job.pid, None, "t-ignore", term_grace_sec=0.5, kill_wait_sec=1, log=lambda _m: None
        )
        assert survivors == []
        assert _wait_dead(child)
        assert time.monotonic() - start >= 0.5, "SIGKILL landed before the SIGTERM grace ran out"
    finally:
        _kill_quietly(child)


@pytest.mark.asyncio
async def test_a_process_that_outlives_sigkill_is_named(monkeypatch):
    """A process stuck in the kernel ignores SIGKILL. It cannot be made in a test, so the
    signals are swallowed instead: the sweep must report it, with whether it holds the device."""
    job_pid, child = _escaped_child("t-stuck")
    monkeypatch.setattr(job_reap.os, "kill", lambda *a: None)
    monkeypatch.setattr(job_reap.os, "killpg", lambda *a: None)
    monkeypatch.setattr(job_reap, "_holds_device", lambda pid: True)
    lines = []
    try:
        survivors = await job_reap.reap_job_survivors(
            "086", job_pid, None, "t-stuck", term_grace_sec=0.2, kill_wait_sec=0.2, log=lines.append
        )
        assert [s.pid for s in survivors] == [child]
        assert survivors[0].holds_device is True
        assert "holds the device" in survivors[0].describe()
        assert any("survived SIGKILL" in line and str(child) in line for line in lines)
    finally:
        monkeypatch.undo()
        _kill_quietly(child)


@pytest.mark.asyncio
async def test_nothing_left_behind_costs_no_wait():
    job = _spawn_job("true")
    job.wait(timeout=5)
    start = time.monotonic()
    survivors = await job_reap.reap_job_survivors("t6", job.pid, None, "t-none", term_grace_sec=5, log=lambda _m: None)
    assert survivors == []
    assert time.monotonic() - start < 2


def test_scope_membership_reads_the_cgroup(tmp_path, monkeypatch):
    """Under privsep the scope's cgroup is the membership no child can leave on its own."""
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))

    def proc(pid, cgroup, pgid, sid, ppid=1):
        d = tmp_path / str(pid)
        d.mkdir()
        rest = ["S", str(ppid), str(pgid), str(sid)] + ["0"] * 15 + ["1000"]
        (d / "stat").write_text(f"{pid} (x y) " + " ".join(rest) + "\n")
        (d / "cgroup").write_text(cgroup)

    proc(500, "0::/system.slice/ttdev-job-086.scope\n", pgid=500, sid=500)
    proc(501, "0::/system.slice/ttdev-job-086.scope/sub\n", pgid=501, sid=501)
    proc(502, "0::/system.slice/ttdev-job-0861.scope\n", pgid=502, sid=502)
    proc(503, "0::/user.slice/user-1000.slice\n", pgid=503, sid=503)

    found = job_reap.job_processes(None, "ttdev-job-086.scope")
    assert sorted(found) == [500, 501]


def test_the_broker_and_init_are_never_job_processes(monkeypatch):
    monkeypatch.setattr(job_reap, "_has_tag", lambda pid, tag: True)
    found = job_reap.job_processes(None, None, "anything")
    assert os.getpid() not in found
    assert 1 not in found


@pytest.mark.asyncio
async def test_the_sweep_never_raises(monkeypatch):
    def boom(*a):
        raise RuntimeError("proc unreadable")

    monkeypatch.setattr(job_reap, "job_processes", boom)
    lines = []
    assert await job_reap.reap_job_survivors("t9", 123456, None, None, log=lines.append) == []
    assert any("sweep failed" in line for line in lines)


def test_tagged_processes_get_the_tag_from_the_runner_shell():
    """The tag is exported by the job shell, so children see it in their environment."""
    job = _spawn_job("sleep 5", tag="t-inherit")
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not job_reap._has_tag(job.pid, "t-inherit"):
            time.sleep(0.05)
        assert job_reap._has_tag(job.pid, "t-inherit")
        assert not job_reap._has_tag(job.pid, "t-other")
    finally:
        _kill_quietly(job.pid)
        job.wait(timeout=5)
