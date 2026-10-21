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
import types

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


def _unique_tag(name: str) -> str:
    """A job tag no other run shares. The sweep matches the tag host-wide, so a fixed one would
    also find the child of a second test run of the same test on the host (the real runner's
    tags carry a random suffix too)."""
    return f"{name}-{os.getpid()}-{os.urandom(4).hex()}"


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
    tag = _unique_tag("t-escaped-1")
    job_pid, child = _escaped_child(tag)
    try:
        assert _alive(child)
        # What the runner used to rely on: the group is empty, so killpg cannot reach the child.
        assert child not in job_reap.job_processes(job_pid)
        with pytest.raises(ProcessLookupError):
            os.killpg(job_pid, 0)

        survivors = await job_reap.reap_job_survivors(
            "t1", job_pid, None, tag, term_grace_sec=2, kill_wait_sec=1, log=lambda _m: None
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
    tag = _unique_tag("t-term-2")
    job = _spawn_job(
        f"setsid bash -c 'trap \"touch {marker}; exit 0\" TERM; while :; do sleep 0.1; done' "
        ">/dev/null 2>&1 < /dev/null & echo $!",
        tag=tag,
    )
    out, _ = job.communicate(timeout=5)
    child = int(out.strip())
    try:
        time.sleep(0.3)  # let the trap install
        await job_reap.reap_job_survivors(
            "t3", job.pid, None, tag, term_grace_sec=3, kill_wait_sec=1, log=lambda _m: None
        )
        assert _wait_dead(child)
        assert marker.exists(), "the survivor was SIGKILLed without a SIGTERM first"
    finally:
        _kill_quietly(child)


@pytest.mark.asyncio
async def test_a_survivor_that_ignores_sigterm_is_sigkilled_after_the_grace():
    tag = _unique_tag("t-ignore")
    job = _spawn_job("setsid bash -c \"trap '' TERM; exec sleep 60\" >/dev/null 2>&1 < /dev/null & echo $!", tag=tag)
    out, _ = job.communicate(timeout=5)
    child = int(out.strip())
    try:
        time.sleep(0.2)
        start = time.monotonic()
        survivors = await job_reap.reap_job_survivors(
            "t4", job.pid, None, tag, term_grace_sec=0.5, kill_wait_sec=1, log=lambda _m: None
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
    tag = _unique_tag("t-stuck")
    job_pid, child = _escaped_child(tag)
    monkeypatch.setattr(job_reap.os, "kill", lambda *a: None)
    monkeypatch.setattr(job_reap.os, "killpg", lambda *a: None)
    monkeypatch.setattr(job_reap, "_holds_device", lambda pid: True)
    lines = []
    try:
        survivors = await job_reap.reap_job_survivors(
            "086", job_pid, None, tag, term_grace_sec=0.2, kill_wait_sec=0.2, log=lines.append
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
    tag = _unique_tag("t-none")
    job = _spawn_job("true", tag=tag)
    job.wait(timeout=5)
    start = time.monotonic()
    survivors = await job_reap.reap_job_survivors("t6", job.pid, None, tag, term_grace_sec=5, log=lambda _m: None)
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


def _stage_proc(root, pid, *, environ: bytes, state="S", flags=0):
    """A fake /proc/<pid> in its own session and group, found only by its environ (the tag)."""
    d = root / str(pid)
    d.mkdir()
    rest = [state, "1", str(pid), str(pid), "0", "0", str(flags)] + ["0"] * 12 + ["1000"]
    (d / "stat").write_text(f"{pid} (x y) " + " ".join(rest) + "\n")
    (d / "environ").write_bytes(environ)


def _count_sleeps(monkeypatch, on_sleep=lambda: None) -> list[float]:
    sleeps: list[float] = []

    def sleep(sec):
        sleeps.append(sec)
        on_sleep()

    monkeypatch.setattr(job_reap, "time", types.SimpleNamespace(sleep=sleep), raising=False)
    return sleeps


def test_an_escaped_child_caught_mid_exec_is_found_on_the_re_read(tmp_path, monkeypatch):
    """Inside execve a process's environ reads empty, so an escaped child (own session, parent
    gone) looks untagged. The scan reads it once more after a short delay instead of missing it."""
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    tag = "t-exec"
    _stage_proc(tmp_path, 600, environ=b"")
    _stage_proc(tmp_path, 601, environ=b"PATH=/bin\0")
    filled = f"PATH=/bin\0{job_reap.JOB_TAG_ENV}={tag}\0".encode()
    sleeps = _count_sleeps(monkeypatch, lambda: (tmp_path / "600" / "environ").write_bytes(filled))

    assert sorted(job_reap.job_processes(None, None, tag)) == [600]
    assert len(sleeps) == 1 and sleeps[0] <= 0.1


def test_an_environ_that_stays_empty_costs_one_bounded_re_read(tmp_path, monkeypatch):
    """A process really started with no environment is not the job's, and the many of them cost
    one delay per scan, not one each."""
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    for pid in (610, 611, 612):
        _stage_proc(tmp_path, pid, environ=b"")
    sleeps = _count_sleeps(monkeypatch)

    assert job_reap.job_processes(None, None, "t-empty") == {}
    assert len(sleeps) == 1 and sleeps[0] <= 0.1


def test_readable_environs_kernel_threads_and_zombies_add_no_wait(tmp_path, monkeypatch):
    """The normal path pays nothing: every user process's environ reads, and the empty environ
    of a kernel thread or a zombie is expected, not a process caught mid-exec."""
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    tag = "t-normal"
    _stage_proc(tmp_path, 620, environ=f"{job_reap.JOB_TAG_ENV}={tag}\0".encode())
    _stage_proc(tmp_path, 621, environ=b"PATH=/bin\0")
    _stage_proc(tmp_path, 622, environ=b"", flags=0x00200040)
    _stage_proc(tmp_path, 623, environ=b"", state="Z")
    sleeps = _count_sleeps(monkeypatch)

    assert sorted(job_reap.job_processes(None, None, tag)) == [620]
    assert sleeps == []


@pytest.mark.asyncio
async def test_the_sweep_ends_a_child_its_first_scan_caught_mid_exec(tmp_path, monkeypatch):
    """The sweep returns at once when its first scan finds nothing, so that scan must not miss a
    child caught mid-exec: it is signalled, not left behind (spec 01 I4)."""
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    tag = "t-exec-sweep"
    _stage_proc(tmp_path, 630, environ=b"")
    filled = f"{job_reap.JOB_TAG_ENV}={tag}\0".encode()
    _count_sleeps(monkeypatch, lambda: (tmp_path / "630" / "environ").write_bytes(filled))
    signalled = []
    monkeypatch.setattr(
        job_reap, "_signal", lambda targets, sig, pgid: signalled.append((sorted(t.pid for t in targets), sig))
    )
    monkeypatch.setattr(job_reap, "_alive", lambda info: False)

    assert await job_reap.reap_job_survivors("t10", None, None, tag, log=lambda _m: None) == []
    assert signalled[0] == ([630], signal.SIGTERM)


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
