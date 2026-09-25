# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Peer-credential (SO_PEERCRED) identity for the unix-socket transport.

When a request arrives over a unix domain socket, the kernel can tell us the
*real* uid/gid/pid of the connecting process via SO_PEERCRED. That identity is
unspoofable (unlike the self-reported ``owner`` request field used over HTTP),
so it is the authoritative basis for authz on the socket transport.

This module keeps the credential parsing as small, dependency-free helpers so
they can be unit-tested against a mock socket without a running server.
"""

import logging
import pwd
import socket
import struct
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("tt-device-mcp")

# struct ucred = { pid_t pid; uid_t uid; gid_t gid; } -> three native ints.
_UCRED_FMT = "3i"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)


@dataclass(frozen=True)
class PeerCredentials:
    """Real identity of a unix-socket peer, from SO_PEERCRED."""

    pid: int
    uid: int
    gid: int

    @property
    def username(self) -> str:
        """Resolve uid to a username, falling back to ``uid:<n>`` if unknown."""
        return username_for_uid(self.uid)


def username_for_uid(uid: int) -> str:
    """Resolve a uid to a login name, or ``uid:<n>`` when there is no entry."""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid:{uid}"


def read_peer_credentials(sock: socket.socket) -> Optional[PeerCredentials]:
    """Read SO_PEERCRED from a connected unix-domain socket.

    Returns ``None`` (best-effort) if the platform/socket does not support it,
    so callers can degrade gracefully rather than crash a connection.
    """
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_SIZE)
    except (OSError, AttributeError) as exc:  # AttributeError: SO_PEERCRED missing
        logger.debug("SO_PEERCRED unavailable on socket: %s", exc)
        return None

    if len(raw) < _UCRED_SIZE:
        logger.debug("SO_PEERCRED returned %d bytes (<%d)", len(raw), _UCRED_SIZE)
        return None

    pid, uid, gid = struct.unpack(_UCRED_FMT, raw[:_UCRED_SIZE])
    return PeerCredentials(pid=pid, uid=uid, gid=gid)
