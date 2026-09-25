# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Unix-domain-socket transport for the daemon, alongside HTTP.

The same Starlette ASGI app (MCP/JSON-RPC at /mcp + the /api/* REST surface)
is served over both a TCP port (legacy, back-compat) and, optionally, a unix
domain socket. The socket transport additionally stamps each request scope with
the peer's real identity from SO_PEERCRED, which downstream authz treats as the
authoritative ``owner`` (see server.py).

Identity injection trick: uvicorn copies its per-connection ``self.client``
tuple verbatim into ``scope["client"]``. For an AF_UNIX connection uvicorn
leaves that as ``None`` (no peer name). We subclass the HTTP protocol and, at
connection time, set ``self.client`` to ``("peercred", uid)`` so the uid rides
through to the ASGI scope without copying uvicorn's large request-parsing code.
The middleware in server.py reads it back from ``scope["client"]``.
"""

import asyncio
import contextvars
import logging
import os
from pathlib import Path

from tt_device_mcp.peercred import read_peer_credentials

logger = logging.getLogger("tt-device-mcp")

# Per-request authoritative peer uid (set for unix-socket requests, None for
# TCP/HTTP). A pure-ASGI middleware sets this at scope entry, in the same task
# that later runs the MCP tool handler, so the value propagates to tools and the
# /api/* handlers alike.
current_peer_uid: contextvars.ContextVar[int | None] = contextvars.ContextVar("tt_device_mcp_peer_uid", default=None)

# The MCP endpoint's mount point. Matched positively below: classifying by "not /api/*" would
# make every route that is neither — /health today, anything added later — read as an agent.
MCP_PATH = "/mcp"

# Whether this request arrived on the MCP surface. The CLI calls /api/<tool>; an agent's stdio
# shim speaks MCP at MCP_PATH. Both land on the same handlers, so the path is the only thing that
# separates them, and it is read here rather than self-reported.
current_via_mcp: contextvars.ContextVar[bool] = contextvars.ContextVar("tt_device_mcp_via_mcp", default=False)


class PeerCredMiddleware:
    """Pure-ASGI middleware that publishes the peer uid for the request.

    Reads the uid stamped onto ``scope["client"]`` by the unix-socket protocol
    and exposes it via the ``current_peer_uid`` contextvar for the duration of
    the request. HTTP/TCP requests leave it as None (legacy behavior).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        uid = peer_uid_from_scope(scope)
        token = current_peer_uid.set(uid)
        mcp_token = current_via_mcp.set(_is_mcp_path(scope.get("path", "")))
        try:
            await self.app(scope, receive, send)
        finally:
            current_via_mcp.reset(mcp_token)
            current_peer_uid.reset(token)


# Sentinel placed in scope["client"][0] to mark a unix-socket peer whose uid we
# resolved. ("host", port) tuples from TCP will never use this string.
PEERCRED_MARKER = "peercred"


def _is_mcp_path(path: str) -> bool:
    """Whether ``path`` is the MCP endpoint or a child of it."""
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


def peer_uid_from_scope(scope) -> int | None:
    """Extract the SO_PEERCRED uid stamped onto a unix-socket request scope.

    Returns the uid for connections that arrived over the unix socket, or None
    for HTTP/TCP connections (which keep legacy self-reported-owner behavior).
    """
    client = scope.get("client")
    if client and len(client) == 2 and client[0] == PEERCRED_MARKER:
        return client[1]
    return None


def _build_peercred_protocol():
    """Build a uvicorn HTTP protocol subclass that injects peer uid into scope.

    Imported lazily and built at runtime so importing this module never pulls in
    uvicorn (keeps the unit tests for the pure helpers dependency-light).
    """
    from uvicorn.protocols.http.h11_impl import H11Protocol

    class PeerCredH11Protocol(H11Protocol):
        def connection_made(self, transport):  # type: ignore[override]
            super().connection_made(transport)
            sock = transport.get_extra_info("socket")
            if sock is None:
                return
            creds = read_peer_credentials(sock)
            if creds is None:
                return
            # Overwrite uvicorn's client tuple (None for AF_UNIX) so the uid
            # propagates into scope["client"] for the authz middleware.
            self.client = (PEERCRED_MARKER, creds.uid)
            logger.debug(
                "unix-socket connection from uid=%s (%s) pid=%s",
                creds.uid,
                creds.username,
                creds.pid,
            )

    return PeerCredH11Protocol


async def serve_unix_socket(app, socket_path: str) -> "object":
    """Start a uvicorn server bound to a unix domain socket.

    Returns the uvicorn Server (already serving via a background task) so the
    caller can keep it alive / shut it down. Removes any stale socket first.
    """
    import uvicorn

    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Clear a stale socket from a previous (crashed) run.
    if path.exists() or path.is_symlink():
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("could not remove stale socket %s: %s", socket_path, exc)

    config = uvicorn.Config(
        app,
        uds=socket_path,
        http=_build_peercred_protocol(),
        log_level="info",
    )
    server = uvicorn.Server(config)
    # Keep a handle to the serve task so callers/tests can cancel it cleanly.
    server._tt_serve_task = asyncio.ensure_future(server.serve())  # type: ignore[attr-defined]
    logger.info("unix-socket transport listening on %s", socket_path)
    return server


def resolve_socket_path(cli_value: str | None) -> str | None:
    """Resolve the socket path from CLI flag or TT_DEVICE_MCP_SOCKET env.

    Returns None when neither is set (socket transport disabled, HTTP only).
    """
    if cli_value:
        return cli_value
    return os.environ.get("TT_DEVICE_MCP_SOCKET") or None
