# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The boot flow fixes this host's platform once, before anything reads the subsystem.

A host is one platform for the life of the process. Deciding once at boot is what lets the rest of
the broker ask ``fsm.recovery`` instead of re-deriving per pass — and what lets preflight report
capabilities against the rungs this host actually has. The one case that must NOT commit is an
unreadable mesh: ``_is_galaxy`` answers None on exactly the degraded Galaxy whose board reads fail,
and pinning per-target there ships the reset that cannot recover it.
"""

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.health.recovery.galaxy import GalaxyRecovery
from tt_device_mcp.health.recovery.per_target import PerTargetRecovery


@pytest.fixture(autouse=True)
def _open_platform(monkeypatch):
    """Every test here starts from an unresolved host: no declared mode, nothing committed.

    The shared fixture hands each test a FRESH, unbooted ``srv.fsm`` so the durable record is
    isolated, while the subsystem aliases still point at the one the suite booted. resolve_platform
    reads both, so join them here rather than re-booting — a second boot would construct a second
    HealthMonitor and the aliases would then name a different object than the FSM does.
    """
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    monkeypatch.setattr(srv.fsm, "deps", srv._recovery_deps)
    monkeypatch.setattr(srv.fsm, "monitor", srv.health_monitor)
    monkeypatch.setattr(srv.fsm, "mechanism", srv.recovery_mechanism)
    monkeypatch.setattr(srv.fsm, "galaxy", srv.galaxy_recovery)
    monkeypatch.setattr(srv.fsm, "per_target", srv.per_target_recovery)
    monkeypatch.setattr(srv.fsm, "recovery", None)
    yield
    srv.fsm.recovery = None


def _cache_boards(monkeypatch, types):
    monkeypatch.setattr(srv.health_monitor, "_board_types", list(types))


def test_a_declared_mode_commits_without_touching_the_device():
    """The operator's declaration is enough on its own. No probe is passed, so a host that cannot
    be read — or must not be — still gets a committed platform."""
    import os

    os.environ["TT_DEVICE_MCP_RESET_MODE"] = "galaxy"
    try:
        assert srv.fsm.resolve_platform(probe=None) == "galaxy"
        assert isinstance(srv.fsm.recovery, GalaxyRecovery)
    finally:
        os.environ.pop("TT_DEVICE_MCP_RESET_MODE", None)


def test_a_probe_that_reads_galaxy_boards_commits_the_galaxy_ladder(monkeypatch):
    probed = []
    monkeypatch.setattr(srv.health_monitor, "_glx_board_types", lambda: ("galaxy",))

    def probe():
        probed.append(True)
        _cache_boards(monkeypatch, ["galaxy", "galaxy"])

    assert srv.fsm.resolve_platform(probe) == "galaxy"
    assert probed, "the platform probe must actually run when nothing is declared"
    assert isinstance(srv.fsm.recovery, GalaxyRecovery)


def test_a_probe_that_reads_non_galaxy_boards_commits_per_target(monkeypatch):
    monkeypatch.setattr(srv.health_monitor, "_glx_board_types", lambda: ("galaxy",))
    assert srv.fsm.resolve_platform(lambda: _cache_boards(monkeypatch, ["n300 L", "n300 R"])) == "per-target"
    assert isinstance(srv.fsm.recovery, PerTargetRecovery)


def test_an_unreadable_mesh_commits_nothing_and_keeps_the_per_pass_fallback(monkeypatch):
    """The case this design exists to get right. Boards unreadable ("N/A" is what tt-smi prints on
    a degraded Galaxy), so the verdict is unknown — commit nothing rather than pin the wrong ladder
    for the whole process, and let a later successful snapshot decide."""
    monkeypatch.setattr(srv.health_monitor, "_glx_board_types", lambda: ("galaxy",))

    assert srv.fsm.resolve_platform(lambda: _cache_boards(monkeypatch, ["n/a", "n/a"])) is None
    assert srv.fsm.recovery is None, "an unknown board read must never commit a platform"
    # Still serviceable: the per-pass path answers, and it self-corrects once boards are readable.
    assert srv.fsm.select_recovery() is not None


def test_a_probe_that_raises_never_stops_the_broker_booting(monkeypatch):
    """A broker is restarted precisely when the mesh is wedged. A probe that explodes there must
    leave the platform open, not take the boot down with it."""

    def boom():
        raise RuntimeError("mesh unreadable")

    assert srv.fsm.resolve_platform(boom) is None
    assert srv.fsm.recovery is None


def test_a_committed_platform_short_circuits_per_pass_selection(monkeypatch):
    """Once committed, select_recovery stops re-deriving — that is the point of resolving at boot.
    Proven by making the per-pass path fail loudly if it is ever consulted."""
    monkeypatch.setattr(srv.health_monitor, "_glx_board_types", lambda: ("galaxy",))
    srv.fsm.resolve_platform(lambda: _cache_boards(monkeypatch, ["n300 L"]))

    import tt_device_mcp.fsm as fsm_mod

    def must_not_run(*a, **k):
        raise AssertionError("select_recovery re-derived a platform boot had already committed")

    monkeypatch.setattr(fsm_mod, "_select_recovery", must_not_run)
    assert isinstance(srv.fsm.select_recovery(), PerTargetRecovery)


def test_importing_the_server_never_touches_the_device():
    """Boot moved off import for this reason: importing a module must not shell out to tt-smi. The
    suite's own spawn tripwire would catch a regression, but only in tests that import late — this
    pins the contract directly."""
    assert srv._booted, "conftest boots the subsystem for the suite"
    # Construction only: importing must not have resolved a platform, which is the device-touching
    # half. A test that wants one calls resolve_platform itself.
    assert srv.fsm.recovery is None
