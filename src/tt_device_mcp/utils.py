# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Shared utilities for tt-device-mcp."""

import http.client
import json
import socket

from tt_device_mcp.constants import resolve_socket


def _uds_api_call(socket_path: str, endpoint: str, method: str, data: dict, timeout: int) -> dict:
    """Same request/response as api_call, but over a unix domain socket (the broker)."""
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    # http.client speaks HTTP; we just swap its TCP connect for an AF_UNIX one.
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.settimeout(timeout)
    try:
        conn.sock.connect(socket_path)
        body = json.dumps(data).encode("utf-8") if data else None
        conn.request(method, endpoint, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode()
        if resp.status >= 400:
            return {"error": f"HTTP {resp.status}: {resp.reason}"}
        return json.loads(raw)
    except (OSError, ValueError) as e:
        return {"error": f"Connection failed: {e}"}
    finally:
        conn.close()


def api_call(
    port: int,
    endpoint: str,
    method: str = "POST",
    data: dict = None,
    host: str = "localhost",
    timeout: int = 30,
) -> dict:
    """Call the server's REST API over its unix socket.

    Socket-only: the host broker socket if present, else this user's standalone
    daemon socket (``port``/``host`` are ignored — kept for signature compat).
    Returns the response as a dict, or ``{"error": "..."}`` on failure.
    """
    socket_path = resolve_socket()
    if not socket_path:
        return {
            "error": "no tt-device-mcp server reachable (have an admin start the broker on a shared host, or run 'tt-device-mcp daemon start' on a reserved box)"
        }
    return _uds_api_call(socket_path, endpoint, method, data, timeout)
