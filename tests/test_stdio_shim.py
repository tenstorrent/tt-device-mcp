# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the stdio adapter's upstream resolution — socket-only (no TCP):
explicit/env, host broker, then a lazy-started per-user daemon."""

import asyncio
import json
import time

import pytest

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


@pytest.mark.asyncio
async def test_a_tool_call_longer_than_the_read_timeout_completes_on_keepalives(tmp_path, monkeypatch, clear_job_state):
    """The shim's SSE read timeout bounds the gap between reads, not the call: a reset on a mesh can
    run for minutes with nothing to send, and the broker's SSE keepalives are what hold the line open.
    Scaled down: the reset tool takes 3x the shim's read timeout and must still return its result."""
    import sse_starlette
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from tt_device_mcp import server
    from tt_device_mcp.device_holders import HolderScan
    from tt_device_mcp.socket_transport import serve_unix_socket

    read_timeout = 0.5
    monkeypatch.setattr(stdio_shim, "_SSE_READ_TIMEOUT_SEC", read_timeout)
    monkeypatch.setattr(sse_starlette.EventSourceResponse, "DEFAULT_PING_INTERVAL", 0.1)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    server.device_op_lock = None
    monkeypatch.setattr(server, "enumerate_device_holders", lambda *a, **k: HolderScan())
    monkeypatch.setattr(server, "_present_chip_indices", lambda: ["0"])

    async def slow_reset(argv, log, owner="", on_output=None):
        await asyncio.sleep(3 * read_timeout)
        return 1, "reset ran long\n"

    monkeypatch.setattr(server.recovery_mechanism, "reset_with_quiesce", slow_reset)

    socket_path = str(tmp_path / "broker.sock")
    uv_server = await serve_unix_socket(server.build_asgi_app(server.create_mcp_server()), socket_path)
    for _ in range(50):
        if uv_server.started:
            break
        await asyncio.sleep(0.05)
    assert uv_server.started, "unix-socket server did not start"

    url, factory = stdio_shim.resolve_upstream(cli_socket=socket_path)
    try:
        async with factory() as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    t0 = time.monotonic()
                    result = await asyncio.wait_for(
                        session.call_tool("tt_device_reset", {"params": {"force": False}}), timeout=10
                    )
                    elapsed = time.monotonic() - t0
    finally:
        uv_server.should_exit = True
        task = getattr(uv_server, "_tt_serve_task", None)
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

    assert elapsed > 2 * read_timeout, "the call must outlast the read timeout to prove anything"
    assert not result.is_error, result
    payload = result.structured_content or json.loads(result.content[0].text)
    payload = payload.get("result", payload)
    assert payload["status"] == "reset_failed" and payload["returncode"] == 1, payload
