# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Stdio MCP adapter that forwards to the device broker over its unix socket.

An MCP client (Claude) spawns the bare ``tt-device-mcp`` binary with a piped
stdin and speaks JSON-RPC over it; ``cli.main`` routes that case here. This
serves a stdio MCP server (the client's view) and transparently proxies tool
listing and tool calls to the broker's unix socket — where the broker reads the
caller's real uid via SO_PEERCRED and runs jobs as that user under privsep, so
identity rides through for free.

Upstream resolution (socket-only — no TCP): TT_DEVICE_MCP_SOCKET, else the host
broker socket, else this user's standalone daemon (lazy-started if none is up).
"""

import asyncio
import os
import sys

import anyio
import httpx2
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED

from tt_device_mcp.constants import resolve_socket, user_socket_path

# Host part is ignored for a UDS connection but must be a syntactically valid URL.
_UDS_URL = "http://tt-device-broker/mcp"

# A blocking tool call holds the channel open for a job's whole runtime, so the read
# budget is a gap between reads. Filling that gap is sse-starlette's 15s keepalive, not
# anything the broker sends.
_DEFAULT_TIMEOUT_SEC = 30.0  # connect, write and pool alike
_SSE_READ_TIMEOUT_SEC = 300.0


def _log(msg: str) -> None:
    # stderr only — stdout is the MCP stream and must not be polluted.
    print(f"[tt-device-mcp stdio] {msg}", file=sys.stderr, flush=True)


def _lazy_start_user_daemon():
    """No broker and no user daemon — start a per-user daemon so the MCP just
    works (tt-buddy is MCP-by-design). Returns the user socket once it's up."""
    import subprocess
    import time

    sock = user_socket_path()
    if os.path.exists(sock):
        return sock
    _log("no server reachable; starting a per-user daemon")
    try:
        subprocess.Popen(
            ["tt-device-mcp", "daemon", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _log(f"could not launch per-user daemon: {exc}")
        return None
    for _ in range(30):  # up to ~15s for the socket to appear
        if os.path.exists(sock):
            return sock
        time.sleep(0.5)
    return sock if os.path.exists(sock) else None


def resolve_upstream(cli_socket=None):
    """Pick the upstream socket: explicit/env, the host broker, or this user's
    standalone daemon (lazy-started if none is up). Socket-only — no TCP.

    Returns (url, uds_client_factory) for the SDK's streamable-http client over
    the chosen unix socket.
    """
    socket_path = resolve_socket(cli_socket) or _lazy_start_user_daemon()
    if not socket_path or not os.path.exists(socket_path):
        _log("no tt-device-mcp server reachable and could not start one")
        socket_path = user_socket_path()  # connect anyway; surfaces a clear error
    else:
        _log(f"using socket {socket_path}")

    def uds_client_factory() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=_UDS_URL,
            transport=httpx2.AsyncHTTPTransport(uds=socket_path),
            follow_redirects=True,
            timeout=httpx2.Timeout(_DEFAULT_TIMEOUT_SEC, read=_SSE_READ_TIMEOUT_SEC),
        )

    return _UDS_URL, uds_client_factory


def _quiet_teardown_handler(loop, context):
    # The MCP streamable-http client races a Future.set_result against task
    # cancellation on context exit, surfacing a benign InvalidStateError after the
    # response is already delivered. Swallow only that; defer everything else.
    if isinstance(context.get("exception"), asyncio.InvalidStateError):
        return
    loop.default_exception_handler(context)


# Reconnect budget for a broker that's briefly gone (e.g. a systemd restart):
# retry the upstream connection so the broker bouncing only blips the shim's
# link, never tears down the stdio session Claude is attached to.
_RECONNECT_TRIES = 12
_RECONNECT_BACKOFF = 1.0  # seconds; ~12s covers a service restart window


async def _call_upstream(url: str, client_factory, op, resend_safe: bool = True):
    """Run ``op(session)`` against a fresh upstream session, retrying transient
    connection failures (broker mid-restart / socket briefly absent).

    Only the connect and initialize phase is retried unless ``resend_safe``. Once a
    tools/call is on the wire the broker may already be running it (a job submit, a
    reset), so a stream cut after that point is reported to the caller, never re-sent.
    """
    last_exc = None
    for attempt in range(_RECONNECT_TRIES):
        sent = False
        answer = None  # (result, error) once the broker has replied
        try:
            # Ours to close: the SDK only closes a client it created itself.
            async with client_factory() as http_client:
                async with streamable_http_client(url, http_client=http_client) as (read, write):
                    async with ClientSession(read, write) as upstream:
                        await upstream.initialize()
                        sent = True
                        try:
                            answer = (await op(upstream), None)
                        except MCPError as exc:
                            if exc.code == CONNECTION_CLOSED:
                                raise
                            answer = (None, exc)  # the broker's own error reply is final
        except Exception as exc:  # noqa: BLE001 - transport errors are retryable; re-raised below
            if answer is None:
                if sent and not resend_safe:
                    _log(f"broker connection lost after the call was sent ({type(exc).__name__}); not re-sending")
                    return _lost_call_result(exc)
                last_exc = exc
                _log(f"broker unavailable ({type(exc).__name__}); retry {attempt + 1}/{_RECONNECT_TRIES}")
                await anyio.sleep(_RECONNECT_BACKOFF)
                continue
            # The answer arrived; only the teardown failed.
        result, error = answer
        if error is not None:
            raise error
        return result
    raise RuntimeError(f"broker unreachable after {_RECONNECT_TRIES} retries: {last_exc!r}")


def _lost_call_result(exc: BaseException) -> types.CallToolResult:
    """The tool call reached the broker but its answer did not come back. Re-sending could run a
    job or a reset twice, so the caller gets an error and decides after checking state."""
    msg = (
        f"lost the connection to the device broker after this tool call was sent ({type(exc).__name__}: {exc}). "
        "The broker may have run it, or may still be running it. It was not re-sent. "
        "Check tt_device_queue_status or tt_device_recent_jobs before calling it again."
    )
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], is_error=True)


async def run_shim(url: str, client_factory) -> None:
    """Serve a transparent stdio proxy that reconnects to ``url`` per call.

    The stdio server (Claude's view) lives for the whole session; each tool call
    opens a fresh upstream connection with retry, so a broker restart is invisible
    to Claude — only the next call's connect blips and retries.
    """
    asyncio.get_running_loop().set_exception_handler(_quiet_teardown_handler)

    # Upstream results are already the wire types the client expects, so returning them
    # unaltered preserves is_error, structured content and the pagination cursor.
    async def _on_list_tools(_ctx, params):
        return await _call_upstream(url, client_factory, lambda s: s.list_tools(params=params))

    async def _on_call_tool(_ctx, params):
        # `_meta` carries the progress token, and from protocol 2026-07-28 the log level.
        # Without it report_progress and ctx.info are no-ops broker-side. What they emit
        # lands on this upstream session; nothing forwards it to the stdio client.
        return await _call_upstream(
            url,
            client_factory,
            lambda s: s.call_tool(params.name, params.arguments, meta=params.meta),
            resend_safe=False,
        )

    proxy = Server("tt-device-mcp", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)

    async with stdio_server() as (stdin, stdout):
        await proxy.run(stdin, stdout, proxy.create_initialization_options())


def serve_stdio(cli_socket=None) -> None:
    """Entry point for the stdio MCP adapter (called from cli.main when a client
    attaches a piped stdin)."""
    url, factory = resolve_upstream(cli_socket)
    anyio.run(run_shim, url, factory)


if __name__ == "__main__":
    serve_stdio()
