# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The privsep identity guard: under privsep, identity-less submissions are refused."""

from tt_device_mcp import server
from tt_device_mcp.socket_transport import current_peer_uid


def test_no_guard_when_privsep_disabled(monkeypatch):
    monkeypatch.setattr(server, "privsep_enabled", lambda: False)
    token = current_peer_uid.set(None)
    try:
        assert server.privsep_identity_error() is None
    finally:
        current_peer_uid.reset(token)


def test_guard_refuses_identity_less_under_privsep(monkeypatch):
    monkeypatch.setattr(server, "privsep_enabled", lambda: True)
    token = current_peer_uid.set(None)  # HTTP: no peer uid
    try:
        err = server.privsep_identity_error()
        assert err is not None and "socket" in err["error"]
    finally:
        current_peer_uid.reset(token)


def test_guard_allows_when_peer_uid_present(monkeypatch):
    monkeypatch.setattr(server, "privsep_enabled", lambda: True)
    token = current_peer_uid.set(1016)  # socket peer identified
    try:
        assert server.privsep_identity_error() is None
    finally:
        current_peer_uid.reset(token)
