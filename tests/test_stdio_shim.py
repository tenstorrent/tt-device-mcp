# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the stdio adapter's upstream resolution — socket-only (no TCP):
explicit/env, host broker, then a lazy-started per-user daemon."""

import asyncio
import contextlib

import mcp.types as types
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


# --- reconnect: retry the connect, never a sent tools/call ----------------------------------------


async def _shim_handlers(monkeypatch, url, factory) -> dict:
    """Build run_shim's real list/call handlers without a stdio stream attached."""
    handlers = {}

    class _CapturingServer:
        def __init__(self, name, on_list_tools, on_call_tool):
            handlers.update(list_tools=on_list_tools, call_tool=on_call_tool)

        def create_initialization_options(self):
            return None

        async def run(self, *_args):
            return None

    @contextlib.asynccontextmanager
    async def _no_stdio():
        yield None, None

    monkeypatch.setattr(stdio_shim, "Server", _CapturingServer)
    monkeypatch.setattr(stdio_shim, "stdio_server", _no_stdio)
    monkeypatch.setattr(stdio_shim, "_RECONNECT_BACKOFF", 0.1)
    await stdio_shim.run_shim(url, factory)
    return handlers


class _FakeBroker:
    """A minimal MCP server on a unix socket that records every tools/call it receives."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.received: list[str] = []
        self.entered = asyncio.Event()

    def _app(self):
        from mcp.server.mcpserver import MCPServer

        mcp = MCPServer(name="fake-broker")

        @mcp.tool()
        async def slow_reset() -> str:
            self.received.append("slow_reset")
            self.entered.set()
            if len(self.received) == 1:
                await asyncio.sleep(30)  # still running when the broker goes away
            return "done"

        return mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=False, host="0.0.0.0")

    async def start(self):
        from tt_device_mcp.socket_transport import serve_unix_socket

        server = await serve_unix_socket(self._app(), self.socket_path)
        for _ in range(100):
            if server.started:
                return server
            await asyncio.sleep(0.02)
        raise AssertionError("unix-socket server did not start")

    @staticmethod
    async def stop(server):
        # A broker restart closes its sockets with the process; uvicorn's own shutdown would wait.
        for conn in list(server.server_state.connections):
            conn.transport.abort()
        server.should_exit = server.force_exit = True
        try:
            await asyncio.wait_for(server._tt_serve_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server._tt_serve_task.cancel()


def _call_params(name: str) -> types.CallToolRequestParams:
    return types.CallToolRequestParams(name=name, arguments={})


@pytest.mark.asyncio
async def test_a_broker_restart_mid_call_does_not_re_send_the_call(tmp_path, monkeypatch):
    """A broker restart cuts the stream of a tool call already sent. The broker may be running it (a
    job submit, a reset), so the shim reports the lost call and does not send it to the restarted
    broker."""
    broker = _FakeBroker(str(tmp_path / "broker.sock"))
    first = await broker.start()
    handlers = await _shim_handlers(monkeypatch, *stdio_shim.resolve_upstream(cli_socket=broker.socket_path))

    call = asyncio.ensure_future(handlers["call_tool"](None, _call_params("slow_reset")))
    await asyncio.wait_for(broker.entered.wait(), timeout=10)
    await broker.stop(first)
    second = await broker.start()  # back well inside the shim's reconnect budget
    try:
        result = await asyncio.wait_for(call, timeout=20)
    finally:
        await broker.stop(second)

    assert broker.received == ["slow_reset"], "the call was sent again after the stream was cut"
    assert result.is_error
    text = result.content[0].text
    assert "not re-sent" in text and "tt_device_queue_status" in text, text


@pytest.mark.asyncio
async def test_a_call_made_while_the_broker_is_down_is_sent_once_it_is_back(tmp_path, monkeypatch):
    """The connect phase is still retried: a call made during a broker restart waits for the broker
    and reaches it exactly once."""
    sock = tmp_path / "broker.sock"
    sock.write_text("")  # resolve against the path, then take it away: the broker is mid-restart
    handlers = await _shim_handlers(monkeypatch, *stdio_shim.resolve_upstream(cli_socket=str(sock)))
    sock.unlink()
    broker = _FakeBroker(str(sock))
    broker.received.append("warm")  # the fake's first call blocks; this one should not

    call = asyncio.ensure_future(handlers["call_tool"](None, _call_params("slow_reset")))
    await asyncio.sleep(0.3)  # a few failed connects
    assert not call.done()
    server = await broker.start()
    try:
        result = await asyncio.wait_for(call, timeout=10)
    finally:
        await broker.stop(server)

    assert broker.received == ["warm", "slow_reset"]
    assert not result.is_error, result


@pytest.mark.asyncio
async def test_an_error_reply_from_the_broker_is_passed_on_not_retried(monkeypatch):
    """A JSON-RPC error is the broker's answer, so the shim hands it on rather than asking again."""
    from mcp.shared.exceptions import MCPError

    calls = []

    class _RefusingSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments, meta=None):
            calls.append(name)
            raise MCPError(code=types.INVALID_PARAMS, message="bad arguments")

    @contextlib.asynccontextmanager
    async def _no_transport(url, http_client):
        yield None, None

    monkeypatch.setattr(stdio_shim, "ClientSession", _RefusingSession)
    monkeypatch.setattr(stdio_shim, "streamable_http_client", _no_transport)
    handlers = await _shim_handlers(monkeypatch, stdio_shim._UDS_URL, contextlib.nullcontext)

    with pytest.raises(MCPError, match="bad arguments"):
        await handlers["call_tool"](None, _call_params("tt_device_submit_job"))
    assert calls == ["tt_device_submit_job"]
