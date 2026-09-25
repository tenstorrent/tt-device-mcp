# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the stdio adapter's upstream resolution — socket-only (no TCP):
explicit/env, host broker, then a lazy-started per-user daemon."""

import tt_device_mcp.constants as constants
from tt_device_mcp import stdio_shim


def test_resolve_prefers_explicit_socket(tmp_path):
    sock = tmp_path / "broker.sock"
    sock.write_text("")  # any existing path counts for resolution
    url, factory = stdio_shim.resolve_upstream(cli_socket=str(sock))
    assert url == stdio_shim._UDS_URL
    assert factory is not None  # always a UDS-bound client factory now


def test_resolve_auto_discovers_broker_socket(tmp_path, monkeypatch):
    sock = tmp_path / "broker.sock"
    sock.write_text("")
    monkeypatch.setattr(constants, "DEFAULT_SOCKET", str(sock))
    monkeypatch.delenv("TT_DEVICE_MCP_SOCKET", raising=False)
    url, factory = stdio_shim.resolve_upstream()
    assert url == stdio_shim._UDS_URL
    assert factory is not None


def test_resolve_lazy_starts_user_daemon_when_none(tmp_path, monkeypatch):
    # No broker socket, no user daemon -> the shim lazy-starts a per-user daemon
    # (that's what makes the MCP "just work" for tt-buddy). No TCP fallback.
    monkeypatch.setattr(constants, "DEFAULT_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setattr(constants, "user_socket_path", lambda: str(tmp_path / "absent-user.sock"))
    monkeypatch.delenv("TT_DEVICE_MCP_SOCKET", raising=False)

    started = {}
    user_sock = tmp_path / "user.sock"
    user_sock.write_text("")  # pretend the lazy-started daemon brought its socket up

    def fake_lazy():
        started["called"] = True
        return str(user_sock)

    monkeypatch.setattr(stdio_shim, "_lazy_start_user_daemon", fake_lazy)
    url, factory = stdio_shim.resolve_upstream()
    assert started.get("called") is True  # tried to start a daemon, not HTTP
    assert url == stdio_shim._UDS_URL and factory is not None
