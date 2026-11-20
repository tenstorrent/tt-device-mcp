# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A non-root server's fabric check: its own writable scratch, host-side throws as a skip (never a pass),
no core dumps, and a server without reset/power-cycle rights saying so."""

from __future__ import annotations

import asyncio
import os
import resource

import pytest

from tests.conftest import patch_health_event
from tests.test_startup_fabric_hugepages import _validator
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitors import fabric


def _nonroot(tmp_path, monkeypatch):
    current = _validator(tmp_path, monkeypatch)
    (current / "tt_metal").mkdir()
    (current / "generated").mkdir()
    monkeypatch.delenv("TTDEV_FABRIC_CACHE", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(fabric, "_is_root", lambda: False)
    return current


class _Warned:
    def __init__(self, into):
        self.into = into

    def warning(self, m, *a, **k):
        self.into.append(m)

    def __getattr__(self, _):
        return lambda *a, **k: None


async def _noop(pid):
    pass


def _tree(path):
    return sorted((p.relative_to(path), p.is_symlink()) for p in path.rglob("*"))


def test_nonroot_writes_only_under_its_own_state_dir(tmp_path, monkeypatch):
    """nonroot-writable-paths: a read-only validator tree is linked, never written."""
    current = _nonroot(tmp_path, monkeypatch)
    before = _tree(current)
    for p in [current, *current.rglob("*")]:
        if p.is_dir():
            p.chmod(0o555)
    try:
        argv, env = fabric.build_command()
    finally:
        for p in [current, *current.rglob("*")]:
            if p.is_dir():
                p.chmod(0o755)
    state = str(tmp_path / "state")
    assert fabric.NOT_WRITABLE_ENV not in env
    for k in ("TT_METAL_RUNTIME_ROOT", "TT_METAL_HOME", "TT_METAL_LOGS_PATH", "TT_METAL_CACHE"):
        assert env[k].startswith(state), k
    root = env["TT_METAL_RUNTIME_ROOT"]
    assert os.path.realpath(os.path.join(root, "tt_metal")) == str(current / "tt_metal")
    gen = os.path.join(root, "generated")
    assert os.path.isdir(gen) and not os.path.islink(gen)
    assert argv[argv.index("--output-path") + 1].startswith(state)
    assert argv[0] == str(current / "build/tools/scaleout/run_cluster_validation")
    assert _tree(current) == before


def test_nonroot_unwritable_state_fails_closed_without_starting(tmp_path, monkeypatch):
    """The fail-closed case: no writable dir means no validator run and a skip verdict, never a pass."""
    _nonroot(tmp_path, monkeypatch)
    (tmp_path / "blocker").write_text("")
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "blocker" / "state"))
    _, env = fabric.build_command()
    assert env[fabric.NOT_WRITABLE_ENV].startswith(fabric.NOT_WRITABLE)
    started = []

    async def probe(*a, **kw):
        started.append(a)
        return 0, b""

    monkeypatch.setattr(fabric, "run_probe", probe)
    with pytest.raises(PermissionError, match="not writable"):
        argv, env = fabric.build_command()
        asyncio.run(fabric.check(argv, env, timeout_sec=1, track=lambda p: None, terminate=_noop))
    assert started == []


def test_root_path_unchanged(tmp_path, monkeypatch):
    """root-path-unchanged: a root broker runs from the pinned tree with no scratch and no logs override."""
    current = _nonroot(tmp_path, monkeypatch)
    monkeypatch.setattr(fabric, "_is_root", lambda: True)
    monkeypatch.setenv("TTDEV_FABRIC_CACHE", str(tmp_path / "cache"))
    _, env = fabric.build_command()
    assert env["TT_METAL_RUNTIME_ROOT"] == env["TT_METAL_HOME"] == str(current)
    assert "TT_METAL_LOGS_PATH" not in os.environ or env["TT_METAL_LOGS_PATH"] == os.environ["TT_METAL_LOGS_PATH"]
    assert fabric.NOT_WRITABLE_ENV not in env
    assert not (tmp_path / "state" / "fabric-check").exists()


@pytest.mark.parametrize(
    "out",
    [
        "TT_THROW @ watcher_server.cpp:120: tt::exception\ninfo:\nWatcher failed to create kernel name file\n",
        "terminate called after throwing an instance of 'std::filesystem::filesystem_error'\n"
        "  what():  filesystem error: cannot create directories: Permission denied [/opt/x/generated]\n",
    ],
)
def test_host_side_throw_is_unavailable_not_a_device_fault(out):
    """host-throw-unavailable"""
    ok, detail = fabric.classify(-6, out)
    assert ok is None
    assert fabric.HOST_SIDE_UNAVAILABLE in detail


def test_host_side_words_never_mask_a_device_fault():
    assert fabric.classify(None, "Permission denied\n")[0] is False  # a timeout stays a fault
    assert fabric.classify(0, "Permission denied\n")[0] is True


def test_check_child_gets_no_core_dump(monkeypatch):
    """core-limit-zero"""
    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda *a: calls.append(a))
    fabric._no_core_dump()
    assert calls == [(resource.RLIMIT_CORE, (0, 0))]
    seen = {}

    async def probe(argv, **kw):
        seen.update(kw)
        return 0, b""

    monkeypatch.setattr(fabric, "run_probe", probe)
    asyncio.run(fabric.check(["/bin/true"], dict(os.environ), timeout_sec=1, track=lambda p: None, terminate=_noop))
    assert seen["preexec_fn"] is fabric._no_core_dump


def test_no_rights_is_reported_at_startup_health_and_hold_bound(monkeypatch):
    """no-rights-reported: the hold stays, the cause is named, no silent loop."""
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", raising=False)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: False)
    why = srv._recovery_unavailable()
    assert why.startswith(srv.RECOVERY_UNAVAILABLE)
    warned = []
    monkeypatch.setattr(srv, "logger", _Warned(warned))
    srv.log_rung_inventory()
    assert any(srv.RECOVERY_UNAVAILABLE in m for m in warned)
    events, lines = [], []
    patch_health_event(monkeypatch, lambda kind, **kw: events.append((kind, kw)))
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-01-01T00:00:00")
    monkeypatch.setattr(srv, "_hold_bound_unarmed_paged", None)
    asyncio.run(srv._hold_bound_host_rung(601, lines.append))
    assert events[0][0] == "hold_bound_no_host_rung" and srv.RECOVERY_UNAVAILABLE in events[0][1]["why"]
    assert srv.RECOVERY_UNAVAILABLE in lines[-1]

    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    assert srv._recovery_unavailable() == ""
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE")
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    assert srv._recovery_unavailable() == ""
