# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""No reset releases the device until a fabric pass has passed since (spec 04 I32).

Every reset path settles through ``_clear_device_dirty(verified=True)``; while a reset owes a fabric
pass, that funnel holds the device fabric-unverified instead of releasing it.
"""

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold, _clean_post_job_gate, _restart_broker
from tests.test_startup_fabric_hugepages import _bound
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import post_reset
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import galaxy

F77 = {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}
FOK = {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "fabric links healthy"}}
# A verify every path below reads as healthy (enum+ARC) but that proves no fabric: each must hold.
UNPROVEN = {"fabric-77": F77, "no-fabric-verdict": {"snapshot": {"ok": True}}}
_REAL_CLEAR_DIRTY = srv._clear_device_dirty


def _held():
    return srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == srv.FABRIC_RELIFT_WHY


def _verify_returning(monkeypatch, evidence, calls=None):
    """Every verify returns enum+ARC healthy with ``evidence``; ``calls`` counts them."""

    async def verify(expected, log, run_fabric=True, **_):
        if calls is not None:
            calls.append(run_fabric)
        return True, dict(evidence)

    patch_recovery(monkeypatch, "_verify_device", verify)


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


# ---- every reset path, one at a time: an unproven verify holds, a passing fabric pass releases ----


@pytest.fixture(params=["fabric-77", "no-fabric-verdict", "fabric-ok"])
def verdict(request):
    """(evidence the path's verify returns, whether the release may go through)."""
    return (FOK, True) if request.param == "fabric-ok" else (UNPROVEN[request.param], False)


def _assert_settled(released: bool):
    if released:
        assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""
    else:
        assert _held(), "a reset no fabric pass has passed on since was released"
        assert FABRIC_DEBT.owed


@pytest.mark.asyncio
async def test_the_bridge_rung(mesh, monkeypatch, verdict):
    evidence, released = verdict
    fsm_dirty(srv, "gate/post-job: chip 8 off the bus", why="job_killed")
    g = srv.galaxy_recovery

    async def bridge(log):
        FABRIC_DEBT.note_reset("bridge reset of chip 8")
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", bridge)
    _verify_returning(monkeypatch, evidence)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_BRIDGE_RESET, ["8"], 32, lambda m: None, ev=None, beats=None)
    assert out == galaxy.OUTCOME_RECOVERED
    _assert_settled(released)


@pytest.mark.asyncio
async def test_the_tray_down_no_window_sweep(mesh, monkeypatch, galaxy_trays, verdict):
    evidence, released = verdict
    fsm_dirty(srv, "gate/post-job: tray 3 off the bus", why="job_killed")
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)

    async def no_bridge(log):
        return False

    async def mesh_reset(argv, log, *a, **k):
        return 0, ""

    patch_recovery(monkeypatch, "_recover_isolated_chips", no_bridge)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: None)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh_reset)
    _verify_returning(monkeypatch, evidence)
    offbus = {str(i) for i in range(24, 32)}
    out = await g._fire_tray_down_no_window(offbus, 32, lambda m: None, gate_phase="post-job", ev=None)
    assert out == galaxy.OUTCOME_RECOVERED
    _assert_settled(released)
    if not released:
        assert FABRIC_DEBT.owed.startswith("tray re-power"), "the sweep's own re-power records the debt"


@pytest.mark.asyncio
async def test_the_idle_off_bus_bridge_reset(mesh, monkeypatch, verdict):
    evidence, released = verdict
    _arm_offbus_stuck_hold(monkeypatch, isolated={"8"})
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")

    async def bridge(log):
        FABRIC_DEBT.note_reset("bridge reset of chip 8")
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", bridge)
    _verify_returning(monkeypatch, evidence)
    assert await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None) is True
    _assert_settled(released)


class _FastAsyncio:
    """``asyncio`` for recovery.base with its 2 s scope poll made instant."""

    def __getattr__(self, name):
        import asyncio

        return getattr(asyncio, name)

    @staticmethod
    async def sleep(_):
        return None


@pytest.mark.asyncio
async def test_a_reset_adopted_from_a_foreign_scope(mesh, monkeypatch, verdict):
    """A reset another process (or the last broker) started and this one waited out is a reset too."""
    from tt_device_mcp.health.recovery import base

    evidence, released = verdict
    fsm_dirty(srv, "gate/post-job: reset", why="job_killed")
    g = srv.galaxy_recovery
    scopes = iter(["ttdev-reset-1.scope"])
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: next(scopes, None))
    monkeypatch.setattr(base, "asyncio", _FastAsyncio())
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    assert await g._reset_and_verify_device([str(i) for i in range(32)], lambda m: None) is True
    assert calls == [True], "the adopted reset's verify runs the fabric pass"
    srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")
    _assert_settled(released)


# ---- a re-power owes the AICLK ceiling too: the next job-door apply runs (spec 03 I35) ----------


@pytest.fixture
def ceiling(monkeypatch):
    """An armed ceiling verified before the path runs. No real helper runs: every apply is recorded
    and verifies, as the door's would on a healthy host. Returns the applies, by ``where``."""
    from tt_device_mcp.health import aiclk_ceiling as ac
    from tt_device_mcp.health.aiclk_ceiling import CEILING

    monkeypatch.setenv(ac.ENV_MHZ, "900")
    monkeypatch.setattr(ac, "health_event", lambda kind, **f: None)
    monkeypatch.setattr(CEILING, "owed", False)
    monkeypatch.setattr(CEILING, "disarmed", "")
    applies = []

    async def apply(where, **_):
        applies.append(where)
        CEILING.owed = False
        return True, "ceiling 900 MHz applied", {}

    monkeypatch.setattr(CEILING, "apply", apply)
    return applies


async def _door_applies_the_owed_ceiling(applies):
    from tt_device_mcp.health.aiclk_ceiling import CEILING

    assert CEILING.owed, "a re-power left the ceiling marked verified: the job door would skip it"
    assert applies == [], "nothing in the path re-applied it, so only the door can"
    await srv._ensure_aiclk_ceiling(None)
    assert applies == ["pre-job"] and not CEILING.owed


@pytest.mark.asyncio
async def test_a_real_bridge_reset_owes_the_ceiling(mesh, monkeypatch, verdict, ceiling):
    evidence, released = verdict
    fsm_dirty(srv, "gate/post-job: chip 8 off the bus", why="job_killed")
    monkeypatch.setattr(srv, "isolated_chips", {"8"})
    monkeypatch.setattr(srv, "device_pci_map", {"8": "0000:46:00.0"})
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: True)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    _verify_returning(monkeypatch, evidence)
    g = srv.galaxy_recovery
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_BRIDGE_RESET, ["8"], 32, lambda m: None, ev=None, beats=None)
    assert out == galaxy.OUTCOME_RECOVERED
    _assert_settled(released)
    await _door_applies_the_owed_ceiling(ceiling)


@pytest.mark.asyncio
async def test_a_tray_re_power_owes_the_ceiling(mesh, monkeypatch, galaxy_trays, verdict, ceiling):
    evidence, released = verdict
    fsm_dirty(srv, "gate/post-job: tray 3 off the bus", why="job_killed")
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)
    fired = []

    async def no_bridge(log):
        return False

    async def mesh_reset(argv, log, *a, **k):
        return 0, ""

    patch_recovery(monkeypatch, "_recover_isolated_chips", no_bridge)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append(bitmap))
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh_reset)
    _verify_returning(monkeypatch, evidence)
    offbus = {str(i) for i in range(24, 32)}
    out = await g._fire_tray_down_no_window(offbus, 32, lambda m: None, gate_phase="post-job", ev=None)
    assert out == galaxy.OUTCOME_RECOVERED and fired
    _assert_settled(released)
    await _door_applies_the_owed_ceiling(ceiling)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", list(UNPROVEN.values()), ids=list(UNPROVEN))
async def test_a_tray_walk_re_power_owes_the_ceiling(mesh, monkeypatch, galaxy_trays, evidence, ceiling):
    monkeypatch.setenv("TT_DEVICE_MCP_UBB_SINGLE_TRAY_WALK", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    fired = []
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append(bitmap))
    _verify_returning(monkeypatch, evidence)
    beats = {str(i): 100 for i in range(24)}
    # Recovered or not by the walk's own verify, the tray was re-powered.
    await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)
    assert fired
    assert FABRIC_DEBT.owed, "the walk's re-power owes a fabric pass before release"
    await _door_applies_the_owed_ceiling(ceiling)


@pytest.mark.asyncio
async def test_an_adopted_reset_owes_the_ceiling(mesh, monkeypatch, verdict, ceiling):
    from tt_device_mcp.health.recovery import base

    evidence, released = verdict
    fsm_dirty(srv, "gate/post-job: reset", why="job_killed")
    g = srv.galaxy_recovery
    scopes = iter(["ttdev-reset-1.scope"])
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: next(scopes, None))
    monkeypatch.setattr(base, "asyncio", _FastAsyncio())
    _verify_returning(monkeypatch, evidence)
    assert await g._reset_and_verify_device([str(i) for i in range(32)], lambda m: None) is True
    srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")
    _assert_settled(released)
    await _door_applies_the_owed_ceiling(ceiling)


@pytest.mark.asyncio
async def test_a_broker_start_owes_a_fabric_pass_before_any_release(mesh, monkeypatch, tmp_path):
    """Startup after a boot or a broker restart: the debt is noted before the startup gate, whose
    own pass (run only once the hugepage pool is ready, tests/test_startup_fabric_hugepages.py)
    settles it."""
    await _restart_broker(monkeypatch, tmp_path / "fsm.json")
    assert FABRIC_DEBT.owed == "broker start"
    assert srv.fsm.state is not ServerState.HEALTHY
    assert srv._clear_device_dirty(verified=True, why="gate/startup: verified healthy") is False
    assert _held()
    FABRIC_DEBT.note_fabric(True)
    assert srv._clear_device_dirty(verified=True, why="idle relift: fabric healthy") is True


# ---- the post-job gate: no added pass at a clean boundary, the full pass while a reset owes one ----


@pytest.mark.asyncio
async def test_a_clean_post_job_gate_with_no_debt_runs_no_fabric_pass(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    calls = _clean_post_job_gate(monkeypatch, tmp_path, eth=(True, "all active eth cores advancing"), chips=32)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert calls["fabric"] == 0 and calls["eth"] == 1
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_post_job_gate_owed_a_fabric_pass_runs_it_and_settles(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    calls = _clean_post_job_gate(monkeypatch, tmp_path, eth=(True, "all active eth cores advancing"), chips=32)
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert calls["fabric"] == 1, "a reset with no fabric pass since must pay for one here"
    assert calls["resets"] == 0
    assert FABRIC_DEBT.owed == "" and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_pre_job_gate_never_runs_the_owed_pass(monkeypatch, tmp_path):
    """Nothing slow before a job (03 I12): pre-job the debt holds on the funnel, never on a pass."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    calls = _clean_post_job_gate(monkeypatch, tmp_path, eth=(True, "all active eth cores advancing"), chips=32)
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)
    assert calls["fabric"] == 0
