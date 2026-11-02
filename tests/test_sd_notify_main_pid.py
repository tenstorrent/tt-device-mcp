# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Only the broker's main pid notifies systemd (spec 03 I95).

The unit runs with NotifyAccess=main, so systemd drops a READY/WATCHDOG from any other pid and
logs "only permitted for main PID". The broker refuses one from a non-main pid itself, and takes
NOTIFY_SOCKET out of its environment at startup so nothing it spawns inherits the socket.
"""

import os
import socket
import subprocess
import sys
import uuid

import pytest

import tt_device_mcp.server as srv


class _Log:
    def __init__(self):
        self.msgs = []

    def warning(self, m, *a):
        self.msgs.append(m % a if a else m)

    info = error = debug = warning


@pytest.fixture
def notify_sock(monkeypatch):
    name = f"tt-notify-test-{uuid.uuid4().hex}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind("\0" + name)
    s.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
    monkeypatch.setattr(srv, "_sd_notify_main_pid", os.getpid())
    monkeypatch.setattr(srv, "_sd_notify_addr", "")
    log = _Log()
    monkeypatch.setattr(srv, "logger", log)
    yield s, log
    s.close()


def _received(s):
    try:
        return s.recv(256).decode()
    except BlockingIOError:
        return None


def test_the_main_pid_notifies(notify_sock):
    s, _ = notify_sock
    srv._sd_notify("WATCHDOG=1")
    assert _received(s) == "WATCHDOG=1"


def test_a_non_main_pid_is_refused_and_said_once(notify_sock, monkeypatch):
    s, log = notify_sock
    monkeypatch.setattr(srv, "_sd_notify_main_pid", os.getpid() + 1)
    srv._sd_notify("WATCHDOG=1")
    srv._sd_notify("WATCHDOG=1")
    assert _received(s) is None, "a non-main pid sent a notification systemd would reject"
    refused = [m for m in log.msgs if "refused" in m]
    assert len(refused) == 1, log.msgs


def test_the_claim_keeps_the_socket_from_children(notify_sock):
    s, _ = notify_sock
    srv._sd_notify_claim()
    assert "NOTIFY_SOCKET" not in os.environ
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('NOTIFY_SOCKET', ''))"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.stdout.strip() == "", "a spawned child inherited NOTIFY_SOCKET"
    srv._sd_notify("READY=1")  # the broker itself still reaches systemd
    assert _received(s) == "READY=1"
