# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I92: the kernel facts the pre-reset zombie classification rests on, checked on real processes.

Each test spawns its own child, makes it a zombie without reaping it, reads /proc through
``device_holders.zombie_info`` and then reaps it. No device is opened: a file in a temp dir stands in for
the device node (``DEVICE_DIR`` is patched). Nothing but the test's own child is signalled.

- A child killed with SIGKILL and not reaped is a zombie (state Z) whose thread group is just the leader:
  do_exit (kernel/exit.c) runs exit_mm and exit_files, and the deferred fput task work that ends in the
  driver's release, before exit_notify sets EXIT_ZOMBIE. It holds no fd and no mapping: ``exited``.
- A leader whose main thread exits while another thread lives stays a zombie (delay_group_leader), and the
  live thread still shares the fd table (CLONE_FILES): a device fd shows under /proc/<pid>/task/<tid>/fd,
  so it is ``holds``.

The broker's invariants stand whatever the verdict: the full health check with fabric (all cabled links)
must have just passed before any hold lifts; every hold gets the full ladder at 10 min, then the power
cycle; nothing new is signalled and a zombie's parent is never killed."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time

import pytest

from tt_device_mcp import device_holders

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

_SYS_EXIT = {"x86_64": 60, "aarch64": 93}.get(platform.machine())


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    return cond()


def _proc_read(fn):
    try:
        return fn()
    except PermissionError:
        return None


def _reap(proc):
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def test_a_killed_unreaped_child_is_a_zombie_with_no_thread_left_and_no_holder():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        os.kill(proc.pid, signal.SIGKILL)
        assert _until(lambda: device_holders.holder_state(proc.pid) == "Z"), "SIGKILL'd and not reaped: a zombie"
        z = device_holders.zombie_info(proc.pid)
        assert z is not None and z.state == "Z"
        assert z.threads == 1 and z.live_tids == [], "only the leader is left in /proc/<pid>/task"
        assert z.verdict == "exited"
        assert z.ppid == os.getpid() and z.parent_state in ("R", "S")
        # exit_files and exit_mm ran before EXIT_ZOMBIE: the leader's fd table and maps are gone. /proc shows
        # that as empty or as EACCES (no mm to check access against), never as an open file.
        assert _proc_read(lambda: os.listdir(f"/proc/{proc.pid}/fd")) in ([], None)
        assert _proc_read(lambda: open(f"/proc/{proc.pid}/maps").read()) in ("", None)
    finally:
        _reap(proc)


@pytest.mark.skipif(_SYS_EXIT is None, reason="raw exit syscall number known for x86_64 and aarch64 only")
def test_a_zombie_leader_with_a_live_thread_holding_the_node_is_a_holder(monkeypatch, tmp_path):
    node_dir = tmp_path / "dev"
    node_dir.mkdir()
    (node_dir / "0").write_text("")
    monkeypatch.setattr(device_holders, "DEVICE_DIR", str(node_dir))
    child = (
        "import ctypes, sys, threading, time\n"
        f"fd = open({str(node_dir / '0')!r})\n"
        "threading.Thread(target=time.sleep, args=(60,)).start()\n"
        "print('ready', flush=True)\n"
        f"ctypes.CDLL(None).syscall({_SYS_EXIT}, 0)\n"  # only this thread exits; the leader turns zombie
    )
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert _until(lambda: device_holders.holder_state(proc.pid) == "Z"), "the leader is a zombie"
        z = _until(lambda: (lambda z: z if z and z.live_tids else None)(device_holders.zombie_info(proc.pid)))
        assert z is not None and z.threads == 2 and len(z.live_tids) == 1, "delay_group_leader: a thread lives"
        assert z.holds is True and z.verdict == "holds", "the live thread still shares the open node"
        assert z.row()["live_threads"] == 1 and z.row()["ppid"] == os.getpid()
    finally:
        _reap(proc)


def test_a_process_that_is_no_zombie_has_no_zombie_info():
    assert device_holders.zombie_info(os.getpid()) is None
