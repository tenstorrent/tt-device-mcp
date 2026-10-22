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
    child = int(out.strip())
    _wait_escaped(child, tag)
    return job.pid, child


def _wait_escaped(child: int, tag: str, timeout: float = 5.0) -> None:
    """The job shell exits as soon as it has forked, before the child has run setsid and exec'd
    sleep. A sweep that ran then would test a race, not the escape: before setsid the job's
    group still reaches the child, and while it execs sleep its environment reads empty, so for
    that instant no route finds it (seen about 1 in 100 in a PID namespace, whose short /proc
    scan lands inside the exec). Wait until it is in its own session and carries the tag."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = job_reap._read_stat(child)
        if info is not None and info.sid == child and job_reap._has_tag(child, tag):
            return
        time.sleep(0.005)
    _kill_quietly(child)
    raise AssertionError(f"escaped child {child} never left the job's session with its tag")


def test_an_escaped_child_is_found_by_the_first_scan():
    """The helper hands the sweep a child that has finished escaping: the first scan after the
    job exits finds it by its tag. Repeated, since the race it closes is short."""
    for i in range(100):
        tag = _unique_tag(f"t-settled-{i}")
        job_pid, child = _escaped_child(tag)
        try:
            assert child in job_reap.job_processes(job_pid, None, tag)
            assert child not in job_reap.job_processes(job_pid)
        finally:
            _kill_quietly(child)


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


def test_the_broker_and_init_are_never_job_processes(monkeypatch):
    monkeypatch.setattr(job_reap, "_read_tag", lambda pid, tag: True)
    found = job_reap.job_processes(None, None, "anything")
    assert os.getpid() not in found
    assert 1 not in found


def _stage_proc(root, pid: int, environ: bytes = b"", state: str = "S", flags: int = 0) -> None:
    """A /proc/<pid> of a process in its own session whose parent is gone."""
    d = root / str(pid)
    d.mkdir()
    rest = [state, "1", str(pid), str(pid), "0", "-1", str(flags)] + ["0"] * 12 + ["1000"]
    (d / "stat").write_text(f"{pid} (sleep) " + " ".join(rest) + "\n")
    (d / "environ").write_bytes(environ)


def test_a_child_scanned_inside_execve_is_still_found_by_its_tag(tmp_path, monkeypatch):
    """A process inside execve reads an empty environ until the new image's is mapped (seen as
    comm 'sleep' with 0 environ bytes when the sweep missed a just-spawned setsid child). Ruled
    out on that one read, it would outlive the sweep and touch the device under the next job."""
    tag = _unique_tag("t-exec")
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    _stage_proc(tmp_path, 3999001)
    reads = []

    def exec_done(_sec):
        reads.append(1)
        (tmp_path / "3999001" / "environ").write_bytes(f"PATH=/bin\0{job_reap.JOB_TAG_ENV}={tag}\0".encode())

    monkeypatch.setattr(job_reap, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=exec_done))
    assert sorted(job_reap.job_processes(None, None, tag)) == [3999001]
    assert reads, "found on a re-read, not the first one"


def test_a_kernel_thread_or_zombie_with_no_environ_is_not_re_read(tmp_path, monkeypatch):
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    _stage_proc(tmp_path, 3999002, flags=job_reap._PF_KTHREAD | 0x40)
    _stage_proc(tmp_path, 3999003, state="Z")
    _stage_proc(tmp_path, 3999004, environ=b"PATH=/bin\0")

    def no_wait(_sec):
        pytest.fail("only an empty environ of a live user process is re-read")

    monkeypatch.setattr(job_reap, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=no_wait))
    assert job_reap.job_processes(None, None, "t-none") == {}


def test_empty_environs_that_stay_empty_cost_one_bounded_window_per_scan(tmp_path, monkeypatch):
    """A process started with an empty environment reads empty for good: each scan waits for
    such processes once, together, never once each."""
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    for pid in range(3999010, 3999040):
        _stage_proc(tmp_path, pid)
    start = time.monotonic()
    assert job_reap.job_processes(None, None, "t-empty") == {}
    assert time.monotonic() - start < 10 * job_reap.TAG_REREAD_SEC


@pytest.mark.asyncio
async def test_the_sweep_ends_a_child_its_first_scan_caught_inside_execve(tmp_path, monkeypatch):
    """The sweep returns at once when its first scan finds nothing, so that scan must not miss an
    escaped child (own session, parent gone) caught inside execve: it is signalled, not left
    behind to outlive the job."""
    tag = _unique_tag("t-exec-sweep")
    monkeypatch.setattr(job_reap, "PROC_DIR", str(tmp_path))
    _stage_proc(tmp_path, 3999050)

    def exec_done(_sec):
        (tmp_path / "3999050" / "environ").write_bytes(f"{job_reap.JOB_TAG_ENV}={tag}\0".encode())

    monkeypatch.setattr(job_reap, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=exec_done))
    signalled = []
    monkeypatch.setattr(
        job_reap, "_signal", lambda targets, sig, pgid: signalled.append((sorted(t.pid for t in targets), sig))
    )
    monkeypatch.setattr(job_reap, "_alive", lambda info: False)

    assert await job_reap.reap_job_survivors("t10", None, None, tag, log=lambda _m: None) == []
    assert signalled[0] == ([3999050], signal.SIGTERM)


@pytest.mark.asyncio
async def test_a_found_process_whose_tag_stops_reading_is_still_a_survivor(monkeypatch):
    """Past exit_mm (a SIGKILLed process stuck in the driver's release) a process's environ
    reads empty, so the rescan's tag match no longer finds it. It is the same live process the
    first scan found, so it is still named, and the device stays held."""
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        info = job_reap._read_stat(proc.pid)
        scans = iter([{proc.pid: info}, {}])
        monkeypatch.setattr(job_reap, "job_processes", lambda *a: next(scans))
        monkeypatch.setattr(job_reap.os, "kill", lambda *a: None)
        monkeypatch.setattr(job_reap.os, "killpg", lambda *a: None)
        monkeypatch.setattr(job_reap, "_holds_device", lambda pid: True)
        survivors = await job_reap.reap_job_survivors(
            "t10", None, None, "t-exit", term_grace_sec=0.1, kill_wait_sec=0.1, release_wait_sec=0, log=lambda _m: None
        )
        assert [s.pid for s in survivors] == [proc.pid]
        assert survivors[0].holds_device is True
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait()


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
