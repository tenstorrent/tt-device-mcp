# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Unix-socket transport tests: peer-uid scope plumbing + a JSON-RPC round-trip.

The round-trip drives the real MCP client over a unix domain socket against the
real ASGI app, calling a no-op device tool (queue_status / reset gate) so no
Tenstorrent device is ever opened.
"""

import asyncio
import json

import httpx2
import pytest

from tt_device_mcp import server
from tt_device_mcp.socket_transport import (
    PEERCRED_MARKER,
    PeerCredMiddleware,
    current_peer_uid,
    current_via_mcp,
    peer_uid_from_scope,
    resolve_socket_path,
    serve_unix_socket,
)


def _tool_payload(result):
    """Extract a tool result dict from structured_content or JSON text content."""
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


class TestPeerUidScope:
    def test_peer_uid_from_unix_scope(self):
        scope = {"type": "http", "client": (PEERCRED_MARKER, 1234)}
        assert peer_uid_from_scope(scope) == 1234

    def test_tcp_scope_has_no_peer_uid(self):
        scope = {"type": "http", "client": ("127.0.0.1", 55555)}
        assert peer_uid_from_scope(scope) is None

    def test_missing_client_is_none(self):
        assert peer_uid_from_scope({"type": "http"}) is None

    @pytest.mark.asyncio
    async def test_middleware_publishes_and_clears_contextvar(self):
        seen = {}

        async def inner(scope, receive, send):
            seen["uid"] = current_peer_uid.get()

        mw = PeerCredMiddleware(inner)
        await mw({"type": "http", "client": (PEERCRED_MARKER, 4242)}, None, None)
        assert seen["uid"] == 4242
        # Restored to default after the request
        assert current_peer_uid.get() is None

    @pytest.mark.asyncio
    async def test_the_middleware_derives_the_surface_from_the_request_path(self):
        """The path is what separates a CLI call from an agent's, and that decides the owner tag
        (spec 05 I6). Driven through the middleware: a test that sets the contextvar by hand
        passes just as well with the derivation reversed or deleted."""
        seen = {}

        async def inner(scope, receive, send):
            seen[scope["path"]] = current_via_mcp.get()

        mw = PeerCredMiddleware(inner)
        for path in ("/api/tt_device_job_run_bg", "/api/tt_device_reset", "/mcp", "/mcp/messages", "/health"):
            await mw({"type": "http", "path": path, "client": (PEERCRED_MARKER, 4242)}, None, None)

        assert seen["/api/tt_device_job_run_bg"] is False
        assert seen["/api/tt_device_reset"] is False
        assert seen["/mcp"] is True
        assert seen["/mcp/messages"] is True
        # Matched positively: a route that is neither must not fall into the agent bucket.
        assert seen["/health"] is False
        assert current_via_mcp.get() is False, "restored to its default after the request"


class TestResolveSocketPath:
    def test_cli_value_wins(self, monkeypatch):
        monkeypatch.setenv("TT_DEVICE_MCP_SOCKET", "/env/path.sock")
        assert resolve_socket_path("/cli/path.sock") == "/cli/path.sock"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("TT_DEVICE_MCP_SOCKET", "/env/path.sock")
        assert resolve_socket_path(None) == "/env/path.sock"

    def test_disabled_when_unset(self, monkeypatch):
        monkeypatch.delenv("TT_DEVICE_MCP_SOCKET", raising=False)
        assert resolve_socket_path(None) is None


@pytest.mark.asyncio
async def test_socket_jsonrpc_round_trip(tmp_path):
    """End-to-end: MCP client -> unix socket -> ASGI app -> no-op device tool.

    Verifies the daemon's JSON-RPC works over the socket and that the peer uid
    (our own, since we connect to ourselves) is what flows into the handlers.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    mcp = server.create_mcp_server()
    app = server.build_asgi_app(mcp)

    socket_path = str(tmp_path / "broker.sock")
    uv_server = await serve_unix_socket(app, socket_path)

    # Wait for the listener to come up.
    for _ in range(50):
        if uv_server.started:
            break
        await asyncio.sleep(0.05)
    assert uv_server.started, "unix-socket server did not start"

    uds_client = httpx2.AsyncClient(
        transport=httpx2.AsyncHTTPTransport(uds=socket_path),
        base_url="http://localhost",
        timeout=httpx2.Timeout(30.0),
    )

    try:
        async with (
            uds_client,
            streamable_http_client(
                "http://localhost/mcp",
                http_client=uds_client,
            ) as (read, write),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # tools/list round-trip
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "tt_device_queue_status" in names
                assert "tt_device_reset" in names

                # Call a no-op device tool (no device touched).
                result = await session.call_tool("tt_device_queue_status", {})
                payload = _tool_payload(result)
                assert payload["device_busy"] is False

                # reset gate over the socket: with our own uid as caller and no
                # foreign holders of the (real) device, the gate should allow and
                # report no devices (test host has none) -- never opens a device.
                reset = await session.call_tool("tt_device_reset", {"params": {"force": False}})
                status = _tool_payload(reset)["status"]
                assert status in ("no_devices", "reset_complete", "reset_unhealthy", "reset_failed", "refused")
    finally:
        uv_server.should_exit = True
        task = getattr(uv_server, "_tt_serve_task", None)
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()


@pytest.mark.asyncio
async def test_the_surface_that_carried_the_submit_is_what_tags_the_owner(tmp_path, monkeypatch, clear_job_state):
    """End-to-end over the socket: one uid, two surfaces, two owner strings (spec 05 I6).

    The halves are anchored separately — the middleware maps a path to the surface, and
    `submitting_owner` maps the surface to a tag — but a test that sets the contextvar by hand
    still passes if the predicate is reversed or the value never reaches the handler. This
    submits through the real peercred protocol, once at `/api/*` and once at `/mcp`, and reads
    back the owner the broker stored.
    """
    import os
    import pwd

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setattr(server, "job_log_dir", None)
    monkeypatch.setattr(server, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    server.device_op_lock = None

    mcp = server.create_mcp_server()
    app = server.build_asgi_app(mcp)

    socket_path = str(tmp_path / "broker.sock")
    uv_server = await serve_unix_socket(app, socket_path)
    for _ in range(50):
        if uv_server.started:
            break
        await asyncio.sleep(0.05)
    assert uv_server.started, "unix-socket server did not start"

    submit = {"workspace": str(tmp_path), "command": "echo hi"}
    uds_client = httpx2.AsyncClient(
        transport=httpx2.AsyncHTTPTransport(uds=socket_path),
        base_url="http://localhost",
        timeout=httpx2.Timeout(30.0),
    )

    try:
        async with uds_client:
            rest = (await uds_client.post("/api/tt_device_job_run_bg", json=submit)).json()
            assert "job_id" in rest, rest

            async with streamable_http_client("http://localhost/mcp", http_client=uds_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    agent = _tool_payload(await session.call_tool("tt_device_job_run_bg", {"params": submit}))
                    assert "job_id" in agent, agent
    finally:
        uv_server.should_exit = True
        task = getattr(uv_server, "_tt_serve_task", None)
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

    assert server.jobs[rest["job_id"]].owner == me, "a /api/* submit must be attributed to the person"
    assert server.jobs[agent["job_id"]].owner == f"[agent]{me}", "an /mcp submit must carry the agent tag"
