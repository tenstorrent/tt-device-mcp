# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The advisory device-op flock (spec 06 I8).

The broker holds LOCK_EX on device-op.flock for the whole of every device op, so an external tool
that writes to the device can take LOCK_EX|LOCK_NB just before its write and skip it on busy,
instead of landing the write in the middle of a reset.
"""

import asyncio
import errno
import fcntl
import os
import subprocess
import sys

import pytest

import tt_device_mcp.server as srv


def _try_lock(path) -> int:
    """What an external tool does: one non-blocking LOCK_EX. 0 if it got it, else the errno."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 0
    except OSError as e:
        return e.errno
    finally:
        os.close(fd)


@pytest.fixture
def flock_path(monkeypatch, tmp_path):
    path = tmp_path / "device-op.flock"
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", str(path))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(tmp_path / "device-op.lock"))
    return path


def test_the_flock_defaults_to_a_sibling_of_the_inhibit_file(monkeypatch, tmp_path):
    # Not the inhibit file itself: that one is unlinked after every op, and a flock on an unlinked
    # inode excludes nobody who opens the path afterwards.
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(tmp_path / "device-op.lock"))
    assert srv.device_op_flock() == tmp_path / "device-op.flock"
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_LOCK")
    assert str(srv.device_op_flock()) == "/run/tt-device-broker/device-op.flock"
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", str(tmp_path / "x"))
    assert srv.device_op_flock() == tmp_path / "x"


@pytest.mark.asyncio
async def test_the_flock_is_held_for_the_op_and_released_after(flock_path):
    async with srv._device_op("reset"):
        assert flock_path.exists()
        assert _try_lock(flock_path) in (errno.EWOULDBLOCK, errno.EAGAIN)
    assert _try_lock(flock_path) == 0
    # Never removed: an external tool's open fd must stay on the file the broker locks next time.
    assert flock_path.exists()
    async with srv._device_op("health-gate/post-job"):
        assert _try_lock(flock_path) in (errno.EWOULDBLOCK, errno.EAGAIN)
    assert _try_lock(flock_path) == 0


@pytest.mark.asyncio
async def test_the_flock_is_released_when_the_op_raises(flock_path):
    with pytest.raises(RuntimeError):
        async with srv._device_op("reset"):
            raise RuntimeError("reset tool crashed")
    assert _try_lock(flock_path) == 0
    assert srv.device_op_active == ""


@pytest.mark.asyncio
async def test_the_flock_is_released_when_the_op_is_cancelled(flock_path):
    entered = asyncio.Event()

    async def op():
        async with srv._device_op("reset"):
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(op())
    await entered.wait()
    assert _try_lock(flock_path) in (errno.EWOULDBLOCK, errno.EAGAIN)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _try_lock(flock_path) == 0


@pytest.mark.asyncio
async def test_children_never_inherit_the_flock(flock_path):
    # A reset tool or job that inherited the fd would keep the lock alive after the op ended (a
    # flock belongs to the open file description, which the child would share).
    script = (
        "import os, sys\n"
        "for n in os.listdir('/proc/self/fd'):\n"
        "    try:\n"
        "        if os.readlink('/proc/self/fd/' + n) == sys.argv[1]:\n"
        "            sys.exit(3)\n"
        "    except OSError:\n"
        "        pass\n"
    )
    async with srv._device_op("reset"):
        held = [int(n) for n in os.listdir("/proc/self/fd") if _readlink(f"/proc/self/fd/{n}") == str(flock_path)]
        assert held, "the broker holds no fd on the flock file during the op"
        for fd in held:
            assert not os.get_inheritable(fd)
            assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        # close_fds=False: even a spawn that keeps every inheritable fd must not carry this one.
        rc = subprocess.run([sys.executable, "-c", script, str(flock_path)], close_fds=False).returncode
        assert rc == 0, "a child process inherited the device-op flock"


def _readlink(p):
    try:
        return os.readlink(p)
    except OSError:
        return ""


@pytest.mark.asyncio
async def test_a_short_external_hold_only_delays_the_op(flock_path, monkeypatch):
    flock_path.touch()
    fd = os.open(flock_path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
    events = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append(kind))
    t0 = loop.time()
    async with srv._device_op("reset"):
        assert loop.time() - t0 >= 0.15
        assert _try_lock(flock_path) in (errno.EWOULDBLOCK, errno.EAGAIN)
    assert "device_op_flock_timeout" not in events


@pytest.mark.asyncio
async def test_a_holder_that_outlasts_the_timeout_does_not_stall_the_op(flock_path, monkeypatch, caplog):
    # An external hold is one short write. One that outlasts the timeout breaks the contract, and
    # must not be able to hold back a reset: the op goes ahead, with a warning naming the holder
    # and a journal event, rather than failing (which would itself escalate the ladder).
    monkeypatch.setattr(srv, "DEVICE_OP_FLOCK_TIMEOUT_SEC", 0.2)
    flock_path.touch()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n",
            str(flock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        events = []
        monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append((kind, f)))
        ran = False
        with caplog.at_level("INFO"):
            async with srv._device_op("reset"):
                ran = True
        assert ran
        timeouts = [f for k, f in events if k == "device_op_flock_timeout"]
        assert timeouts and timeouts[0]["op"] == "reset"
        assert timeouts[0]["holders"] == [holder.pid]
        if srv.logger:
            assert f"pid {holder.pid}" in caplog.text
    finally:
        holder.kill()
        holder.wait()
    # A crashed holder releases the lock: the kernel drops a flock with its last fd.
    assert _try_lock(flock_path) == 0


@pytest.mark.asyncio
async def test_an_unopenable_flock_path_changes_nothing(monkeypatch):
    # A per-user daemon without the /run directory, or any unwritable path: the op runs as before.
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", "/proc/nonexistent/device-op.flock")
    ran = False
    async with srv._device_op("reset"):
        ran = True
    assert ran
