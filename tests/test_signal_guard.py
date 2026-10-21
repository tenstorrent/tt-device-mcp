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

from tests.conftest import _spawned_by_this_run
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


def test_a_pid_that_does_not_exist_is_passed_through(signal_guard):
    with open("/proc/sys/kernel/pid_max") as f:
        absent = int(f.read()) + 1
    with pytest.raises(ProcessLookupError):
        os.kill(absent, signal.SIGKILL)
    assert not signal_guard.refused
