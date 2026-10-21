# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""No reset releases the device until a fabric pass has passed since (spec 04 I32).

Every reset path settles through ``_clear_device_dirty(verified=True)``; while a reset owes a fabric
pass, that funnel holds the device fabric-unverified instead of releasing it.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_device_safety import (
    DEFAULT_POLLERS,
    _arm_offbus_stuck_hold,
    _arm_stuck_hold,
    _clean_post_job_gate,
    _reset_that_fails,
    _restart_broker,
    _setup_selfheal_hold,
    _systemctl_with_units,
)
from tests.test_startup_fabric_hugepages import _bound
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
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
    FABRIC_DEBT.note_fabric(True)  # the full check, fabric included, just passed (03 I78)
    assert srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy") is True
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence, verdict",
    [(F77, None), ({"snapshot": {"ok": True}}, None)],  # held, so never reported OK (03 I78)
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


async def _operator_reset_on_a_healthy_mesh(monkeypatch, evidence):
    """An operator reset (rc 0) on a HEALTHY 32-chip mesh, then its I13 verify returning ``evidence``.
    Returns (systemctl calls, the poller starts seen while the verify ran)."""
    monkeypatch.setattr(srv, "DEVICE_POLLER_SERVICES", DEFAULT_POLLERS)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: FABRIC_DEBT.owed)  # a mesh with a fabric check
    calls = []
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _systemctl_with_units(set(DEFAULT_POLLERS), calls))
    to_thread, sleep = srv.asyncio.to_thread, srv.asyncio.sleep
    _reset_that_fails(monkeypatch, rc=0)
    assert srv.fsm.state is ServerState.HEALTHY

    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)
    assert rc == 0 and srv.fsm.state is ServerState.HEALTHY
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", sleep)
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.extend(c for c in calls if c[0] == "start")
        return True, dict(evidence)

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    await asyncio.gather(*list(srv._pollers_restore_tasks))
    return calls, seen


@pytest.mark.asyncio
async def test_an_operator_reset_starts_the_pollers_only_once_its_fabric_verify_passes(mesh, monkeypatch):
    """An operator reset leaves the FSM HEALTHY, so its restore started the pollers before the I13
    fabric verify, which then ran beside their MMIO. The restore waits for the post-reset debt, and
    the passing fabric pass that settles it starts them, once."""
    calls, seen = await _operator_reset_on_a_healthy_mesh(monkeypatch, FOK)

    assert seen == [], "the pollers ran during the fabric verify"
    assert FABRIC_DEBT.owed == "" and srv.fsm.state is ServerState.HEALTHY
    assert [c for c in calls if c[0] == "start"] == [("start", u) for u in DEFAULT_POLLERS]
    assert srv._pollers_owed is None
    assert "pollers_kept_quiesced" in [k for k, _ in mesh]


@pytest.mark.asyncio
async def test_an_operator_reset_whose_fabric_stays_unverified_keeps_the_pollers_stopped(mesh, monkeypatch):
    """A 77 after the retries holds the mesh: the pollers stay stopped and owed, for the close."""
    calls, seen = await _operator_reset_on_a_healthy_mesh(monkeypatch, F77)

    assert seen == [] and not [c for c in calls if c[0] == "start"], calls
    assert _held() and FABRIC_DEBT.owed and srv._pollers_owed


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
    """It runs the full check, fabric included (03 I78): a pass that brings no fabric verdict cannot
    settle a reset's debt, so nothing is released and the debt stays owed."""
    fsm_dirty(srv, "foreign holder", why="foreign_holder", dirty=False)
    FABRIC_DEBT.note_reset("adopted ttdev-reset-x.scope")
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan: "")
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    runs = []

    async def verify(expected, log, run_fabric=True, **_):
        runs.append(run_fabric)
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._foreign_holder_fast_verify(32, lambda m: None)
    assert runs == [True], "the full check, fabric included"
    assert srv.fsm.state is ServerState.RECOVERING and FABRIC_DEBT.owed, "held, the debt still owed"


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


@pytest.mark.asyncio
async def test_a_tenant_deferred_off_bus_one_shot_runs_on_the_next_idle_tick(mesh, monkeypatch, verdict):
    """Spec 03 I23: the off-bus one-shot that stood down on a live tenant is given back, not spent. The
    first sampler tick with no tenant runs it, and its reset still releases only through the full
    post-reset check, fabric included (spec 04 I32). Spending it left a one-chip drop held until the
    general ceiling window, eight minutes after the tenant had gone."""
    evidence, released = verdict
    _arm_offbus_stuck_hold(monkeypatch, isolated={"8"})
    patch_health_event(monkeypatch, lambda kind, **f: mesh.append((kind, f)))
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")
    monkeypatch.setattr(srv, "device_op_lock", None)  # bind a fresh lock to this test's loop
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    tenant = HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True)
    scan = {"now": tenant}
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: scan["now"])
    bridges = []

    async def bridge(log):
        bridges.append("8")
        FABRIC_DEBT.note_reset("bridge reset of chip 8")
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", bridge)
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    since = datetime.fromisoformat(srv.device_hold_episode_since)
    one_shot = srv._offbus_hold_ceiling_sec()

    def count(kind):
        return sum(k == kind for k, _ in mesh)

    async def tick(sec):
        srv._check_hold_deadline(now=since + timedelta(seconds=sec))
        if srv._forced_escalation_task is not None:
            await srv._forced_escalation_task

    await tick(one_shot + 1)
    assert count("hold_deadline_forced_escalation") == 1 and bridges == []
    assert srv.device_hold_offbus_escalated is False, "a run that stood down on a tenant spent the one-shot"

    await tick(one_shot + 11)  # the tenant is still there
    assert bridges == [] and count("hold_deadline_forced_escalation") == 1
    assert count("device_op_begin") == 1, "a retry under a live tenant must check it quietly, not open a device op"
    assert srv.device_hold_offbus_escalated is False

    scan["now"] = HolderScan(holders=[], complete=True)
    await tick(one_shot + 21)
    assert bridges == ["8"], "the first idle tick must run the deferred one-shot"
    assert count("hold_deadline_forced_escalation") == 2
    assert srv.device_hold_offbus_escalated is True and srv.device_hold_tenant_deferred is False
    assert calls and all(calls), "the retried run's post-reset verify skipped the fabric pass"
    if released:
        _assert_settled(released)
    else:  # the forced climb may leave it DOWN, but never released over an unproven fabric
        assert srv.fsm.state is not ServerState.HEALTHY and FABRIC_DEBT.owed == "bridge reset of chip 8"
        assert count("post_reset_release_refused") == 1


@pytest.mark.parametrize("given_back", [True, False], ids=["given-back", "spent"])
def test_a_given_back_one_shot_gives_back_the_ceiling_window_it_claimed(monkeypatch, given_back):
    """Spec 03 I23: a one-shot that waited out a tenant past the ceiling claims that ceiling window as
    its own run. Given back (it stood down on the tenant), the window goes back with it: when the chips
    return meanwhile (no one-shot left to retry), the ceiling window runs on the next tick, not one
    whole window (600 s) later. A one-shot that ran keeps the claim: one forced climb, not two."""
    spawned = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: None)
    monkeypatch.setattr(
        srv, "_maybe_spawn_forced_escalation", lambda: spawned.append(srv.device_hold_escalation_kind) or True
    )
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_offbus_prior_bucket", None)
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry", None)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)
    srv.fsm.set_latch("escalated", False)
    monkeypatch.setattr(srv, "isolated_chips", {"13"})
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chip(s) 13 left the PCIe bus")
    since = datetime(2026, 10, 15, 18, 43, 41)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    ceiling = srv._stuck_hold_ceiling_sec()

    srv._check_hold_deadline(now=since + timedelta(seconds=ceiling + 50))  # the tenant held it past the ceiling
    assert spawned == ["offbus_one_shot"] and srv.device_hold_escalate_bucket == 1
    if given_back:
        assert srv._rearm_tenant_deferred_window("offbus_one_shot")
    monkeypatch.setattr(srv, "isolated_chips", set())  # the chip came back; the hold stays
    srv._check_hold_deadline(now=since + timedelta(seconds=ceiling + 60))
    if given_back:
        assert spawned == ["offbus_one_shot", "ceiling_window"], "the given-back window must run now"
        assert srv.device_hold_escalate_bucket == 1
    else:
        assert spawned == ["offbus_one_shot"], "the one-shot that ran is window 1's run"
    srv._check_hold_deadline(now=since + timedelta(seconds=ceiling + 70))
    assert len(spawned) == (2 if given_back else 1), "one forced run per window"


@pytest.mark.asyncio
async def test_the_measured_fabric_failure_reset_on_the_first_relift(mesh, monkeypatch, tmp_path, verdict):
    """Spec 03 I56: a post-job hold on a measured fabric FAIL resets on the first idle relift, with no
    grace. The real reset-and-verify path runs (only the reset command and the verify are stubbed): its
    verify runs the fabric pass, and the hold clears only on a healthy fabric verdict."""
    evidence, released = verdict
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    _arm_stuck_hold(monkeypatch, held_minutes_ago=0.05)
    g = srv.galaxy_recovery
    monkeypatch.delattr(g, "_reset_and_verify_device")  # the real one, not _arm_stuck_hold's counter
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    srv.health_monitor.last_fabric_ok = False
    calls, resets = [], []

    async def glx_reset(argv, log):
        resets.append(len(calls))
        FABRIC_DEBT.note_reset(" ".join(argv))
        return 0, ""

    async def eth_unreadable(timeout_sec=60.0):
        return None, "eth read timed out"

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", glx_reset)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unreadable)
    _verify_returning(monkeypatch, evidence, calls)

    await srv._attempt_idle_relift()

    assert len(resets) == 1, "the measured fabric failure resets on the first relift, seconds into the hold"
    after = calls[resets[0] :]
    assert after and all(after), "every verify after the reset runs the fabric pass"
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
async def test_the_arc_not_ready_retry_reset(mesh, monkeypatch, tmp_path, verdict):
    """A -glx_reset that left every chip on the bus but ARC not ready gets one more reset; that retry is
    a reset too, and only a fabric pass after it releases."""
    await _arc_not_ready_retry(monkeypatch, tmp_path, *verdict)


@pytest.mark.asyncio
async def test_the_arc_not_ready_retry_never_releases_on_fewer_links_than_the_cabling(mesh, monkeypatch, tmp_path):
    """The retried reset exits 0 but its fabric pass finds a port connection the cabling descriptor
    expects missing: a failed check, not a pass on the links it could see. The hold stays."""
    from tt_device_mcp.health.monitors import fabric

    cable = f"{fabric.CABLE_FAULT}: 1 port/cable connection(s) of the cabling descriptor missing and none extra"
    evidence = {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": cable}}
    out = await _arc_not_ready_retry(monkeypatch, tmp_path, evidence, False)
    assert out is not True


async def _arc_not_ready_retry(monkeypatch, tmp_path, evidence, released):
    from tests.test_arc_not_ready import _fake_sysfs

    fsm_dirty(srv, "gate/post-job: reset", why="job_killed")
    handle = _fake_sysfs(monkeypatch, tmp_path)
    g = srv.galaxy_recovery
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_ARGS", "tt-smi -glx_reset")
    monkeypatch.setattr(recovery_pkg, "ARC_NOT_READY_RETRY_SETTLE_SEC", 0)
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)
    rcs = iter([1, 0])

    async def mesh_reset(argv, log, *a, **k):
        FABRIC_DEBT.note_reset(" ".join(argv))
        rc = next(rcs)
        handle["nodata"].clear()
        handle["nodata"].update(str(i) for i in range(1, 32) if rc)
        return rc, ""

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh_reset)
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    out = await g._reset_and_verify_device([str(i) for i in range(32)], lambda m: None)
    assert next(rcs, None) is None, "the retry reset fired"
    assert calls and all(calls), "every verify after the retry runs the fabric pass"
    if out:
        srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")
    _assert_settled(released)
    return out


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


# ---- the last-chance sweep before a host rung (04 I22/I32): a verify that proves no fabric holds ----

# The fabric check could not run at all (spawn failure): no verdict, and the pass journals
# fabric_check_unavailable. Like a 77, it must hold, never release.
FUNAVAIL = {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "skipped (fabric check not runnable: x)"}}


@pytest.fixture(params=["fabric-77", "no-fabric-verdict", "fabric-unavailable", "fabric-ok"])
def sweep_verdict(request):
    if request.param == "fabric-ok":
        return FOK, True
    return (FUNAVAIL if request.param == "fabric-unavailable" else UNPROVEN[request.param]), False


def _arm_last_chance_sweep(monkeypatch, mesh, evidence):
    """Stub every reset the sweep fires (the mesh reset records the debt, as reset_with_quiesce
    does) and the host rungs; return the list of host rungs that fired."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)
    monkeypatch.setattr(g.deps, "isolated_chips", lambda: set())
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: None)
    host = []

    async def mesh_reset(argv, log, *a, **k):
        FABRIC_DEBT.note_reset("tt-smi -glx_reset")
        return 0, ""

    async def power_cycle(log, reason):
        host.append("power-cycle")

    async def reboot(log, reason):
        host.append("reboot")

    async def verify(expected, log, run_fabric=True, **_):
        if (evidence.get("fabric") or {}).get("detail", "").startswith("skipped"):
            mesh.append(("fabric_check_unavailable", {"detail": "not_runnable"}))
        return True, dict(evidence)

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh_reset)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(g, "_auto_reboot_host", reboot)
    patch_recovery(monkeypatch, "_verify_device", verify)
    return g, host


def _assert_no_release(mesh):
    assert _held(), "the sweep released a reset mesh no fabric pass has passed on since"
    assert FABRIC_DEBT.owed == "tt-smi -glx_reset"
    kinds = [k for k, _ in mesh]
    # The funnel's own hold journals the dropped dirty bit as unverified; a verified clear is a release.
    assert not any(k == "device_dirty_cleared" and f.get("verified") for k, f in mesh), "released as verified"
    assert not any(k == "gate" and f.get("healthy") for k, f in mesh), "journaled as a healthy gate"
    assert "post_reset_release_refused" in kinds


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [galaxy.STAGE_HOST_REBOOT, galaxy.STAGE_POWER_CYCLE])
async def test_the_gate_ladders_last_chance_sweep(mesh, monkeypatch, galaxy_trays, sweep_verdict, stage):
    """The gate's host rung runs the sweep; its verify passes enum+ARC. Folded into the FSM the way the
    gate's ``_fire`` does, the device is released only on a passing fabric pass; otherwise it stays
    held fabric-unverified (the relift owns it), and no host rung fires on a verify that passed."""
    evidence, released = sweep_verdict
    fsm_dirty(srv, "gate/post-job: mesh failed the verify after the reset", why="job_killed")
    g, host = _arm_last_chance_sweep(monkeypatch, mesh, evidence)
    out = await g._fire_gate_rung("post-job", stage, ["0"], 32, lambda m: None, ev=None, beats={})
    assert out == galaxy.OUTCOME_RECOVERED, "the gate stops climbing this pass: no further reset or host rung"
    srv._fold_rung_outcome(out, lambda m: None)
    assert host == [], "no reboot or power cycle on a mesh whose verify passed enum+ARC"
    if released:
        assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""
        assert any(k == "gate" and f.get("recovered_via") == "last-chance-reset-sweep" for k, f in mesh)
    else:
        _assert_no_release(mesh)
        assert srv._idle_relift_armed()[1], "the fabric relift owns the hold: bounded, never open-ended"


@pytest.mark.parametrize(
    "owed, outcome, expect",
    [
        (True, galaxy.OUTCOME_RECOVERED, "held"),  # the funnel refused: never reopened by the fold
        (False, galaxy.OUTCOME_RECOVERED, "healthy"),
        (None, galaxy.OUTCOME_RECOVERED, "held"),  # no fabric pass since the hold: refused (03 I78)
        (True, galaxy.OUTCOME_TERMINAL, "held"),  # a 77-only hold measured no fault: not DOWN
        (False, galaxy.OUTCOME_WAITING, "held"),
    ],
)
def test_no_rung_outcome_lifts_a_fabric_unverified_hold(mesh, owed, outcome, expect):
    """The gate, the idle relift, the dirty idle gate and the hold escalation fold a rung's outcome through one
    helper, so none of them reopens the door the release funnel held (I32), nor parks a 77-only hold in DOWN."""
    if owed:
        FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    srv._hold_device_fabric_unverified("fabric check exited 77")
    if owed is False and outcome == galaxy.OUTCOME_RECOVERED:
        FABRIC_DEBT.note_fabric(True)  # the rung's verify ran the full check and it passed
    srv._fold_rung_outcome(outcome, lambda m: None)
    if expect == "healthy":
        assert srv.fsm.state is ServerState.HEALTHY
    else:
        assert _held()


def test_a_terminal_rung_on_a_measured_fault_still_goes_down(mesh):
    fsm_dirty(srv, "gate/post-job: mesh failed the verify after the reset", why="job_killed")
    srv._fold_rung_outcome(galaxy.OUTCOME_TERMINAL, lambda m: None)
    assert srv.fsm.state is ServerState.DOWN


def test_every_rung_outcome_folds_through_the_helper():
    import inspect

    src = inspect.getsource(srv)
    body = src.replace(inspect.getsource(srv._fold_rung_outcome), "")
    assert "fsm.on_outcome(" not in body, "a rung outcome folded around the I32 guard"
    assert src.count("_fold_rung_outcome(outcome, ") == 4, "the gate, the idle relift, the dirty idle gate (I68) and the hold escalation"


# The 10-17 class-J episode (job killed, glx_reset 62 s): the first pass proved all 128 links, then its
# dispatch probe hung in UMD start-up until the wrapper's 90 s cap (77, 133 s); the re-check passed (57 s).
DISPATCH_CAP_77 = {
    "snapshot": {"ok": True},
    "fabric": {
        "ok": None,
        "detail": "fabric check COULD NOT RUN (133s; exit 77, output in /x/fabric.log): fabric-check: dispatch "
        "probe hit its 90s cap without a verdict (typical is 14s) -> CANNOT CHECK (exit 77; broker will not "
        "reset on this)",
    },
}
NO_TRAINED_LINK_77 = {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "no link was tested (77)"}}


async def _replay_post_reset_verify(monkeypatch, passes):
    """Run ``_verify_device_after_reset`` on a fake clock. ``passes`` is ``[(seconds, evidence), ...]``,
    one per verify. Returns (healthy, seconds spent, sleeps, run_fabric of each verify)."""
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 60.0)
    clock, sleeps, fabric_asked = [0.0], [], []
    todo = list(passes)

    async def verify(expected, log, run_fabric=True, **_):
        fabric_asked.append(run_fabric)
        took, evidence = todo.pop(0)
        clock[0] += took
        return True, dict(evidence)

    async def sleep(sec, *_a, **_k):
        sleeps.append(sec)
        clock[0] += sec

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(recovery_pkg.asyncio, "sleep", sleep)
    healthy, _, _ = await srv.galaxy_recovery._verify_device_after_reset(32, lambda m: None)
    return healthy, clock[0], sleeps, fabric_asked


@pytest.mark.asyncio
async def test_a_dispatch_cap_77_after_a_reset_rechecks_at_once_and_class_j_fits_its_budget(mesh, monkeypatch):
    """Spec 04 I84. The validator had moved traffic across every link, so the links were trained: the
    eth-training wait cannot help, and the re-check runs at once. Class J (budget 300 s) replayed: kill to
    reset 1 s + glx_reset 62 s + rescan settle 3 s + the verify + release 5 s took 321 s with the wait."""
    healthy, spent, sleeps, fabric_asked = await _replay_post_reset_verify(
        monkeypatch, [(133.0, DISPATCH_CAP_77), (57.0, FOK)]
    )
    assert healthy is True
    assert sleeps == [], "no eth-training wait after a dispatch-cap 77"
    assert fabric_asked == [True, True], "both passes are the full check, fabric included (goal 8)"
    ttr = 1 + 62 + 3 + spent + 5
    assert ttr == 261 and ttr < 300


@pytest.mark.asyncio
async def test_a_no_trained_link_77_after_a_reset_still_waits_for_eth_training(mesh, monkeypatch):
    healthy, spent, sleeps, _ = await _replay_post_reset_verify(monkeypatch, [(40.0, NO_TRAINED_LINK_77), (45.0, FOK)])
    assert healthy is True and sleeps == [60.0] and spent == 145.0


@pytest.mark.asyncio
async def test_a_dispatch_cap_77_that_persists_never_releases(mesh, monkeypatch):
    """Goal 11: a re-check that still reaches no verdict is UNKNOWN, held, not a pass."""
    healthy, _, sleeps, fabric_asked = await _replay_post_reset_verify(
        monkeypatch, [(133.0, DISPATCH_CAP_77), (133.0, DISPATCH_CAP_77)]
    )
    assert healthy is None and sleeps == [] and fabric_asked == [True, True]


def test_the_wrapper_reason_names_the_dispatch_cap_over_a_validator_error_line():
    """The I84 test reads the reason; an error line the validator logged above must not hide the cap."""
    from tt_device_mcp.health.monitor import _override_reason
    from tt_device_mcp.health.monitors import fabric

    text = (
        "2026-10-17 15:09:16.6 | error    |     Distributed | link retry on chip 3 (x.cpp:1)\n"
        "fabric-check: fabric healthy\n"
        "fabric-check: dispatch probe hit its 90s cap without a verdict (typical is 14s) -> CANNOT CHECK\n"
    )
    assert fabric.names_dispatch_cap(_override_reason(text))
    assert not fabric.names_dispatch_cap("no link was tested (77)")
