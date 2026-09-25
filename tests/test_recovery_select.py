# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""select_recovery's platform derivation: a declared TT_DEVICE_MCP_RESET_MODE wins outright;
absent that, an unidentifiable machine type falls back to the conservative per-target ladder
rather than guessing Galaxy."""

import tt_device_mcp.server as srv
from tt_device_mcp.health.recovery import select_recovery
from tt_device_mcp.health.recovery.galaxy import GalaxyRecovery
from tt_device_mcp.health.recovery.per_target import PerTargetRecovery


def test_declared_mode_wins(monkeypatch, health_deps):
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    assert isinstance(select_recovery(None, srv.recovery_mechanism, health_deps), GalaxyRecovery)


def test_loudbox_declares_per_target(monkeypatch, health_deps):
    # The operator names the host class; a loudbox resets per PCIe target. Must count as a
    # DECLARATION (no reset_mode_invalid journal row), not fall through to derivation.
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "loudbox")
    calls = []
    health_deps.journal_skip_once = lambda key, detail: calls.append(key)
    assert isinstance(select_recovery(None, srv.recovery_mechanism, health_deps), PerTargetRecovery)
    assert calls == []


def test_unknown_falls_to_per_target_loud(monkeypatch, health_deps):
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    # no cached board types -> derivation returns None -> per-target + reset_mode stays unknown
    r = select_recovery(None, srv.recovery_mechanism, health_deps)
    assert isinstance(r, PerTargetRecovery)
