# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 09 I9: a test can signal only processes its own pytest process spawned.

The broker's kill paths act on whatever the host's /proc shows. A test that reached one
unstubbed on a device host SIGKILLed real jobs holding the real device, so the autouse
`signal_guard` refuses any os.kill/os.killpg aimed elsewhere, delivers nothing, and fails the test.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.conftest import _SPAWN_TOKEN_ENV, _carries_spawn_token, _real_kill, _spawned_by_this_run
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan

_WAIT_FOR_EOF = "import sys; sys.stdin.read()"


def _stdin_child(**kw) -> subprocess.Popen:
    """A child that exits on its own once its stdin closes, so cleanup needs no signal."""
    return subprocess.Popen([sys.executable, "-c", _WAIT_FOR_EOF], stdin=subprocess.PIPE, **kw)


def _end(child: subprocess.Popen) -> None:
    child.stdin.close()
    child.wait(timeout=10)


def test_a_signal_to_a_process_the_test_did_not_spawn_is_refused_and_delivers_nothing(signal_guard, monkeypatch):
    decoy = _stdin_child(start_new_session=True)
    try:
        # Stands in for a host process: the guard sees it as foreign. Were the guard broken, the
        # signals below would land on this test's own child, never on a host process.
        monkeypatch.setattr(signal_guard, "owned", lambda pid: pid != decoy.pid)
        for send in (
            lambda: os.kill(decoy.pid, signal.SIGKILL),
            lambda: os.killpg(decoy.pid, signal.SIGKILL),
            lambda: os.kill(-decoy.pid, signal.SIGTERM),
        ):
            with pytest.raises(PermissionError):
                send()
        time.sleep(0.2)
        assert decoy.poll() is None, "a refused signal was delivered"
        assert len(signal_guard.refused) == 3, signal_guard.refused
        assert all(str(decoy.pid) in r for r in signal_guard.refused)
    finally:
        signal_guard.refused.clear()  # this test's refusals are the assertion, not a failure
        _end(decoy)


def test_the_dead_chip_holder_kill_cannot_reach_a_host_process(signal_guard, monkeypatch):
    """The path that killed real jobs: the dead-chip valve SIGKILLs every holder its scan finds."""
    decoy = _stdin_child()
    try:
        monkeypatch.setattr(signal_guard, "owned", lambda pid: pid != decoy.pid)
        holder = DeviceHolder(pid=decoy.pid, uid=os.getuid())
        monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[holder], complete=True))
        asyncio.run(srv._kill_device_holders("chip 13 left the bus"))
        time.sleep(0.2)
        assert decoy.poll() is None
        assert any(str(decoy.pid) in r for r in signal_guard.refused), signal_guard.refused
    finally:
        signal_guard.refused.clear()
        _end(decoy)


def test_a_child_the_test_spawned_can_be_signalled_even_after_it_left_the_session(signal_guard):
    child = _stdin_child(start_new_session=True)
    os.killpg(child.pid, signal.SIGKILL)
    assert child.wait(timeout=10) == -signal.SIGKILL
    assert not signal_guard.refused

    # A grandchild whose parent exited is re-parented away from this process; it still carries
    # this run's token, so the reap paths' tests may end it.
    fork = (
        "import os, sys\n"
        "pid = os.fork()\n"
        "if pid:\n"
        "    print(pid, flush=True)\n"
        "    os._exit(0)\n"
        "sys.stdin.read()\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", fork], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    grandchild = int(parent.stdout.readline())
    parent.wait(timeout=10)
    try:
        assert _spawned_by_this_run(grandchild)
    finally:
        parent.stdin.close()


# A session leader that keeps one exited child unreaped (a zombie in its group), then waits on stdin.
_LEADER_WITH_EXITED_CHILD = (
    "import os, sys\n"
    "pid = os.fork()\n"
    "if not pid:\n"
    "    os._exit(0)\n"
    "print(pid, flush=True)\n"
    "sys.stdin.read()\n"
)


def _zombie_of(leader: subprocess.Popen) -> int:
    pid = int(leader.stdout.readline())
    for _ in range(200):
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return pid
        time.sleep(0.01)
    raise AssertionError(f"pid {pid} never exited")


def _no_token_env() -> dict:
    return {k: v for k, v in os.environ.items() if k != _SPAWN_TOKEN_ENV}


def test_a_group_sweep_over_a_member_that_already_exited_and_was_re_parented_passes(signal_guard, monkeypatch):
    """The race behind a preempted job's flaky teardown: SIGKILL ends the job's shell and its
    `sleep`; the shell is reaped first, its exited child is re-parented to init (ppid no longer
    ours) and, a zombie, shows an empty /proc environ (no token). The survivor reap's group sweep
    then met that zombie. A signal reaches nothing in it, so it is not a foreign process."""
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_WITH_EXITED_CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        zombie = _zombie_of(leader)
        assert not _carries_spawn_token(zombie), "a zombie keeps no environ"
        # The re-parent to init cuts the ppid walk; only the token is left to prove ownership.
        monkeypatch.setattr(signal_guard, "owned", _carries_spawn_token)
        os.killpg(leader.pid, signal.SIGKILL)
        assert leader.wait(timeout=10) == -signal.SIGKILL
        assert not signal_guard.refused, signal_guard.refused
    finally:
        if leader.poll() is None:
            _real_kill(leader.pid, signal.SIGKILL)
            leader.wait(timeout=10)


def test_a_live_foreign_member_beside_an_exited_one_is_still_refused(signal_guard, monkeypatch):
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_WITH_EXITED_CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=_no_token_env(),
    )
    try:
        zombie = _zombie_of(leader)
        monkeypatch.setattr(signal_guard, "owned", _carries_spawn_token)
        with pytest.raises(PermissionError):
            os.killpg(leader.pid, signal.SIGKILL)
        time.sleep(0.2)
        assert leader.poll() is None, "a refused signal was delivered"
        assert len(signal_guard.refused) == 1 and f"[{leader.pid}]" in signal_guard.refused[0], signal_guard.refused
        assert str(zombie) not in signal_guard.refused[0]
    finally:
        signal_guard.refused.clear()
        _end(leader)


def test_a_foreign_process_whose_main_thread_exited_is_still_refused(signal_guard, monkeypatch):
    """Its leader reads 'Z' while another thread runs: a signal would reach that thread."""
    code = (
        "import ctypes, sys, threading\n"
        "threading.Thread(target=sys.stdin.read).start()\n"
        "ctypes.CDLL(None).pthread_exit(None)\n"
    )
    decoy = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, start_new_session=True, env=_no_token_env()
    )
    try:
        for _ in range(200):
            with open(f"/proc/{decoy.pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            time.sleep(0.01)
        else:
            raise AssertionError("the decoy's main thread never exited")
        monkeypatch.setattr(signal_guard, "owned", _carries_spawn_token)
        for send in (lambda: os.kill(decoy.pid, signal.SIGKILL), lambda: os.killpg(decoy.pid, signal.SIGKILL)):
            with pytest.raises(PermissionError):
                send()
        time.sleep(0.2)
        assert decoy.poll() is None, "a refused signal was delivered"
        assert len(signal_guard.refused) == 2, signal_guard.refused
    finally:
        signal_guard.refused.clear()
        _end(decoy)


def test_a_pid_that_does_not_exist_is_passed_through(signal_guard):
    with open("/proc/sys/kernel/pid_max") as f:
        absent = int(f.read()) + 1
    with pytest.raises(ProcessLookupError):
        os.kill(absent, signal.SIGKILL)
    assert not signal_guard.refused
