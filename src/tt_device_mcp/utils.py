# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Shared utilities for tt-device-mcp."""

import http.client
import json
import os
import socket
import sys
import time

from tt_device_mcp.constants import resolve_socket

# A broker restart (e.g. an autoupdate) leaves its socket gone for ~2 s. Retry only the
# connect step, and only on a refused connect or a missing socket file: in both cases
# nothing was sent, so a retried submit can never create a duplicate job.
CONNECT_RETRIES_ENV = "TT_DEVICE_MCP_CONNECT_RETRIES"
CONNECT_DELAY_ENV = "TT_DEVICE_MCP_CONNECT_DELAY_SEC"
DEFAULT_CONNECT_RETRIES = 5
DEFAULT_CONNECT_DELAY_SEC = 1.0


def _env_num(name: str, default, cast):
    try:
        val = cast(os.environ.get(name, default))
    except ValueError:
        return default
    return val if val >= 0 else default


def retry_unreachable(attempt, *, sleep=time.sleep):
    """Call ``attempt()`` (a connect step); on ConnectionRefusedError or FileNotFoundError
    only, wait and retry up to TT_DEVICE_MCP_CONNECT_RETRIES times (default 5 x 1 s,
    TT_DEVICE_MCP_CONNECT_DELAY_SEC). Prints one line to stderr on the first wait. After
    the last retry the original exception is raised. Other errors propagate at once."""
    retries = _env_num(CONNECT_RETRIES_ENV, DEFAULT_CONNECT_RETRIES, int)
    delay = _env_num(CONNECT_DELAY_ENV, DEFAULT_CONNECT_DELAY_SEC, float)
    for n in range(retries + 1):
        try:
            return attempt()
        except (ConnectionRefusedError, FileNotFoundError):
            if n >= retries:
                raise
            if n == 0:
                print(
                    f"tt-device-mcp: broker not reachable; a broker restart may be in progress, "
                    f"retrying up to {retries} x {delay:g}s",
                    file=sys.stderr,
                    flush=True,
                )
            sleep(delay)


def _connect_uds(socket_path: str, timeout) -> socket.socket:
    """Connect to a unix socket, retrying a refused connect / missing socket (see
    retry_unreachable). Each attempt uses a fresh socket."""

    def attempt():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(socket_path)
        except BaseException:
            sock.close()
            raise
        return sock

    return retry_unreachable(attempt)


def _uds_api_call(socket_path: str, endpoint: str, method: str, data: dict, timeout: int) -> dict:
    """Same request/response as api_call, but over a unix domain socket (the broker)."""
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    # http.client speaks HTTP; we just swap its TCP connect for an AF_UNIX one.
    try:
        conn.sock = _connect_uds(socket_path, timeout)
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
        # The socket file may be briefly absent during a broker restart.
        def _find():
            path = resolve_socket()
            if not path:
                raise FileNotFoundError("no broker socket")
            return path

        try:
            socket_path = retry_unreachable(_find)
        except FileNotFoundError:
            socket_path = None
    if not socket_path:
        return {
            "error": "no tt-device-mcp server reachable (have an admin start the broker on a shared host, or run 'tt-device-mcp daemon start' on a reserved box)"
        }
    return _uds_api_call(socket_path, endpoint, method, data, timeout)
