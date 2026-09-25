# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The two durable-state defaults (health journal, Prometheus textfile) resolve per deployment:
root gets the system path unchanged, anyone else gets a writable path under the same per-user
state base every other per-user daemon file already uses (constants.user_state_dir), and an
explicit env override always wins over both. A per-user daemon that silently kept the root-only
default lost its FSM's durability with no error at all — the genuine regression test below proves
the per-user default is not just a path string, but an actual place ServerFsm can round-trip
through."""

import pytest

from tt_device_mcp import constants, metrics
from tt_device_mcp.fsm import ServerFsm, ServerState
from tt_device_mcp.health import evidence

# ---- evidence._default_health_dir() -------------------------------------------------------------


def test_root_health_dir_default_is_unchanged(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.setattr(evidence.os, "geteuid", lambda: 0)
    assert evidence._default_health_dir() == evidence.Path("/var/lib/tt-device-broker/health")


def test_non_root_health_dir_default_lands_under_user_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(evidence.os, "geteuid", lambda: 1000)

    resolved = evidence._default_health_dir()

    assert resolved == constants.user_state_dir() / "health"
    resolved.mkdir(parents=True)
    (resolved / "probe").write_text("ok")  # would raise if this default were not writable
    assert (resolved / "probe").read_text() == "ok"


@pytest.mark.parametrize("euid", [0, 1000])
def test_health_dir_env_override_beats_both_defaults(monkeypatch, tmp_path, euid):
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_DIR", str(tmp_path / "mine"))
    monkeypatch.setattr(evidence.os, "geteuid", lambda: euid)
    assert evidence._default_health_dir() == tmp_path / "mine"


# ---- metrics._textfile_dir() ---------------------------------------------------------------------


def test_root_textfile_dir_default_is_unchanged(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_TEXTFILE_DIR", raising=False)
    monkeypatch.setattr(metrics.os, "geteuid", lambda: 0)
    assert metrics._textfile_dir() == metrics.Path("/var/lib/prometheus/node-exporter")


def test_non_root_textfile_dir_default_lands_under_user_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TT_DEVICE_MCP_TEXTFILE_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(metrics.os, "geteuid", lambda: 1000)
    assert metrics._textfile_dir() == constants.user_state_dir() / "metrics"


@pytest.mark.parametrize("euid", [0, 1000])
def test_textfile_dir_env_override_beats_both_defaults(monkeypatch, tmp_path, euid):
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(tmp_path / "mine"))
    monkeypatch.setattr(metrics.os, "geteuid", lambda: euid)
    assert metrics._textfile_dir() == tmp_path / "mine"


def test_non_root_textfile_writer_writes_and_leaves_no_tmp(monkeypatch, tmp_path):
    """write_textfile() itself, pointed at the non-root default rather than an explicit
    TT_DEVICE_MCP_TEXTFILE_DIR override — the path this module actually reaches for absent
    root and absent an operator override, not just the resolver in isolation."""
    monkeypatch.delenv("TT_DEVICE_MCP_TEXTFILE_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(metrics.os, "geteuid", lambda: 1000)
    metrics.reset_for_tests()

    metrics.state_entered("healthy", now=0.0)
    metrics.write_textfile()

    target_dir = constants.user_state_dir() / "metrics"
    target = target_dir / metrics.TEXTFILE_NAME
    assert target.exists()
    assert target.read_bytes() == metrics.render()
    assert not any(target_dir.glob(f"{metrics.TEXTFILE_NAME}.*.tmp"))
    metrics.reset_for_tests()


# ---- genuine durability: ServerFsm round-trips through the per-user default ----------------------


def test_fsm_survives_restart_at_the_per_user_default_health_dir(monkeypatch, tmp_path):
    """The regression this whole fix is about: a per-user daemon that silently kept the root-only
    default reported fsm_state over /health but had NO fsm.json anywhere, so a restart forgot a
    non-HEALTHY episode with no error to point at. Pointing a real ServerFsm at the resolved
    per-user default (not an arbitrary tmp_path) and reloading it from a second instance is the
    only way to prove the default is actually durable, not merely a plausible-looking path."""
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(evidence.os, "geteuid", lambda: 1000)

    health_dir = evidence._default_health_dir()
    fsm_path = health_dir / "fsm.json"
    assert not fsm_path.exists()

    first = ServerFsm(fsm_path)
    first.on_fault("eth_frozen", detail="active-eth heartbeat frozen")

    assert fsm_path.is_file(), "a non-HEALTHY episode must be fsync'd to disk, not held in-memory only"

    second = ServerFsm(fsm_path)
    assert second.state is ServerState.RECOVERING
    assert second.record.why == "eth_frozen"
    assert second.record.detail == "active-eth heartbeat frozen"
