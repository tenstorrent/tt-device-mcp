# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for SO_PEERCRED peer-uid derivation (mocked socket creds)."""

import socket
import struct

from tt_device_mcp.peercred import (
    PeerCredentials,
    read_peer_credentials,
    username_for_uid,
)


class _FakeSock:
    """Minimal socket stub returning a canned SO_PEERCRED blob."""

    def __init__(self, blob=None, raise_exc=None):
        self._blob = blob
        self._raise = raise_exc

    def getsockopt(self, level, optname, buflen):
        assert level == socket.SOL_SOCKET
        assert optname == socket.SO_PEERCRED
        if self._raise is not None:
            raise self._raise
        return self._blob[:buflen]


def _ucred(pid, uid, gid):
    return struct.pack("3i", pid, uid, gid)


class TestReadPeerCredentials:
    def test_parses_pid_uid_gid(self):
        sock = _FakeSock(_ucred(4242, 1000, 1000))
        creds = read_peer_credentials(sock)
        assert creds == PeerCredentials(pid=4242, uid=1000, gid=1000)

    def test_distinct_uid(self):
        sock = _FakeSock(_ucred(7, 31337, 100))
        creds = read_peer_credentials(sock)
        assert creds.uid == 31337
        assert creds.gid == 100
        assert creds.pid == 7

    def test_returns_none_on_oserror(self):
        sock = _FakeSock(raise_exc=OSError("not a unix socket"))
        assert read_peer_credentials(sock) is None

    def test_returns_none_when_attribute_missing(self):
        sock = _FakeSock(raise_exc=AttributeError("SO_PEERCRED"))
        assert read_peer_credentials(sock) is None

    def test_returns_none_on_short_blob(self):
        sock = _FakeSock(b"\x00\x00")  # too short for ucred
        assert read_peer_credentials(sock) is None


class TestUsernameForUid:
    def test_root_resolves(self):
        # uid 0 is always present
        assert username_for_uid(0) == "root"

    def test_unknown_uid_falls_back(self):
        # An almost-certainly-unused high uid
        assert username_for_uid(4000000000) == "uid:4000000000"

    def test_credentials_username_property(self):
        creds = PeerCredentials(pid=1, uid=0, gid=0)
        assert creds.username == "root"
