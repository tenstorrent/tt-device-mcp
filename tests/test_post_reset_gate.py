# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""No reset releases the device until a fabric pass has passed since (spec 04 I32).

Every reset path settles through ``_clear_device_dirty(verified=True)``; while a reset owes a fabric
pass, that funnel holds the device fabric-unverified instead of releasing it.
"""

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_startup_fabric_hugepages import _bound
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import post_reset
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.post_reset import FABRIC_DEBT

F77 = {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}
FOK = {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "fabric links healthy"}}


@pytest.fixture
def mesh(monkeypatch, tmp_path, clear_job_state):
    """A 32-chip host with a fabric check configured (nothing spawns: every verify is stubbed)."""
    for i in range(32):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(post_reset, "health_event", lambda kind, **f: events.append((kind, f)))
    return events


def test_the_debt_settles_only_on_a_healthy_fabric_verdict(mesh):
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    for ev in ({}, F77, {"fabric": {"ok": False}}, {"snapshot": {"ok": True}}):
        FABRIC_DEBT.note_evidence(ev)
        assert FABRIC_DEBT.owed, f"{ev} is no passing fabric pass"
    FABRIC_DEBT.note_fabric(None)
    assert FABRIC_DEBT.owed
    FABRIC_DEBT.note_evidence(FOK)
    assert FABRIC_DEBT.owed == ""
    kinds = [k for k, _ in mesh]
    assert kinds == ["post_reset_fabric_owed", "post_reset_fabric_settled"]


def test_a_single_chip_or_a_host_with_no_fabric_check_is_exempt(mesh, monkeypatch):
    FABRIC_DEBT.note_reset("tt-smi -r 0")
    assert FABRIC_DEBT.blocks_release(32) == "tt-smi -r 0"
    assert FABRIC_DEBT.blocks_release(1) == "", "a single chip has no fabric to prove"
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD")  # the conftest sandbox has no validator either
    assert FABRIC_DEBT.blocks_release(32) == "", "nothing on this host could ever settle it"


def test_a_verified_release_after_a_reset_holds_fabric_unverified(mesh):
    fsm_dirty(srv, "gate/post-job: reset", why="job_killed")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    assert srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy") is False
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == srv.FABRIC_RELIFT_WHY
    assert "tt-smi -glx_reset" in srv.fsm.record.detail
    assert ("post_reset_release_refused") in [k for k, _ in mesh]
    FABRIC_DEBT.note_fabric(True)
    assert srv._clear_device_dirty(verified=True, why="idle relift: fabric healthy") is True
    assert srv.fsm.state is ServerState.HEALTHY


def test_no_debt_releases_as_before(mesh):
    fsm_dirty(srv, "gate/post-job: exit 1", why="job_killed")
    assert srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy") is True
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence, verdict",
    [(F77, None), ({"snapshot": {"ok": True}}, True)],
    ids=["fabric-77", "no-fabric-verdict"],
)
async def test_an_operator_reset_without_a_passing_fabric_pass_holds(mesh, monkeypatch, evidence, verdict):
    """The tool and the stream both settle through _verify_operator_reset. A 77, or a verify that
    carries no fabric verdict at all, never releases the reset mesh."""
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    fsm_dirty(srv, "operator reset", why="job_killed")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")

    async def verify(expected, log, run_fabric=True, **_):
        return True, dict(evidence)

    patch_recovery(monkeypatch, "_verify_device", verify)
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert got is verdict
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == srv.FABRIC_RELIFT_WHY
    assert FABRIC_DEBT.owed


@pytest.mark.asyncio
async def test_an_operator_reset_with_a_passing_fabric_pass_releases(mesh, monkeypatch):
    fsm_dirty(srv, "operator reset", why="job_killed")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")

    async def verify(expected, log, run_fabric=True, **_):
        return True, dict(FOK)

    patch_recovery(monkeypatch, "_verify_device", verify)
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert got is True and srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""


@pytest.mark.asyncio
async def test_the_77_fallback_never_releases_a_reset_with_no_passing_fabric_pass(mesh, monkeypatch, tmp_path):
    """03 I40's startup-checks release is for a 77 alone; after a reset it climbs instead."""
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    await srv._attempt_idle_relift()
    assert rungs == ["present"]
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_the_foreign_holder_fast_verify_after_a_reset_holds(mesh, monkeypatch):
    """Its enum+ARC-only pass cannot settle a reset's debt: the hold turns fabric-unverified."""
    from tt_device_mcp.device_holders import HolderScan

    fsm_dirty(srv, "foreign holder", why="foreign_holder", dirty=False)
    FABRIC_DEBT.note_reset("adopted ttdev-reset-x.scope")
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan: "")
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)

    async def verify(expected, log, run_fabric=True, **_):
        assert not run_fabric
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    assert await srv._foreign_holder_fast_verify(32, lambda m: None) is True
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == srv.FABRIC_RELIFT_WHY
    assert srv._idle_relift_armed()[1], "the fabric relift owns it: bounded, never an open-ended hold"
