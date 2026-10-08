# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the stdio adapter's upstream resolution — socket-only (no TCP):
explicit/env, host broker, then a lazy-started per-user daemon."""

import asyncio
import builtins
import contextlib
import importlib
import json
import time

import httpx2
import mcp.types as types
import pytest
from mcp.shared.exceptions import MCPError

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


def _scripted_session(monkeypatch, replies: list) -> list:
    """Stand in for the upstream session: each attempt's tools/call takes the next reply (raised if
    it is an exception). Returns the list of calls the fake broker saw."""
    seen = []

    class _Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def send_request(self, request, result_type):
            seen.append(request.params.name)
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

    @contextlib.asynccontextmanager
    async def _no_transport(url, http_client):
        yield None, None

    monkeypatch.setattr(stdio_shim, "ClientSession", _Session)
    monkeypatch.setattr(stdio_shim, "streamable_http_client", _no_transport)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, message",
    [
        (types.INVALID_PARAMS, "bad arguments"),
        # Same code as an unknown session, but the SDK also uses it after the broker took the request.
        (types.INVALID_REQUEST, "Unexpected content type: text/html"),
    ],
)
async def test_an_error_reply_from_the_broker_is_passed_on_not_retried(monkeypatch, code, message):
    """A JSON-RPC error is the broker's answer, so the shim hands it on rather than asking again."""
    seen = _scripted_session(monkeypatch, [MCPError(code=code, message=message)])
    handlers = await _shim_handlers(monkeypatch, stdio_shim._UDS_URL, contextlib.nullcontext)

    with pytest.raises(MCPError, match=message):
        await handlers["call_tool"](None, _call_params("tt_device_submit_job"))
    assert seen == ["tt_device_submit_job"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        # The broker's socket went away between initialize and the call: no byte of it was sent.
        lambda: httpx2.ConnectError("[Errno 2] No such file or directory"),
        # A restarted broker does not know the session, so it refuses the call without running it.
        lambda: MCPError(code=types.INVALID_REQUEST, message="Session not found"),
        # The SDK's spelling of the same 404.
        lambda: MCPError(code=types.INVALID_REQUEST, message="Session terminated"),
    ],
    ids=["connect_refused", "unknown_session", "session_terminated"],
)
async def test_a_call_that_never_reached_a_tool_is_retried(monkeypatch, refusal):
    done = types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
    seen = _scripted_session(monkeypatch, [refusal(), done])
    handlers = await _shim_handlers(monkeypatch, stdio_shim._UDS_URL, contextlib.nullcontext)

    assert await handlers["call_tool"](None, _call_params("tt_device_reset")) is done
    assert seen == ["tt_device_reset", "tt_device_reset"]


@pytest.mark.asyncio
async def test_a_wrapped_failure_is_retried_only_if_every_part_says_never_delivered(monkeypatch):
    """The SDK's task groups can wrap several errors. One refused connect beside a cut read does not
    prove the call stayed home, so it is reported, not re-sent."""
    group = getattr(builtins, "ExceptionGroup", None) or importlib.import_module("exceptiongroup").ExceptionGroup
    mixed = group("tg", [httpx2.ConnectError("refused"), httpx2.ReadError("cut")])
    seen = _scripted_session(monkeypatch, [mixed])
    handlers = await _shim_handlers(monkeypatch, stdio_shim._UDS_URL, contextlib.nullcontext)

    result = await handlers["call_tool"](None, _call_params("tt_device_submit_job"))
    assert seen == ["tt_device_submit_job"]
    assert result.is_error and "not re-sent" in result.content[0].text


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
