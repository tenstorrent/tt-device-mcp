# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I78: no hold lifts unless the full health check, fabric included, has just passed.

Every lift path is driven with a stubbed check that passes, fails, or finds a link the cabling expects
missing (spec 03 I71: fewer links than the cabling expects is a FAILED check). Only the pass lifts; the
other two keep the hold, its episode and its clock as they were, so the 10-min bound's ladder and then
the power cycle (spec 04 I76) stay the way out. Device-free: every check is stubbed."""

import time
from datetime import datetime, timedelta

import pytest

from tests import test_device_safety as safety
from tests.conftest import fsm_dirty, fsm_healthy, patch_health_event, patch_recovery
from tests.test_aiclk_ceiling_goal8 import PATHS as RESET_PATHS
from tests.test_aiclk_ceiling_goal8 import _drive, rig  # noqa: F401  (fixture)
from tests.test_device_safety import _fast_verify_setup, _setup_down_offbus, _setup_selfheal_hold
from tests.test_eth_timeout_reset import ADVANCING
from tests.test_eth_timeout_reset import mesh as eth_mesh  # noqa: F401  (fixture)
from tests.test_fabric_topology_mismatch import _LOST
from tests.test_post_reset_gate import mesh  # noqa: F401
from tests.test_startup_fabric_hugepages import _bound
from tt_device_mcp import alert
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health import post_reset
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.monitors import fabric
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery import offbus_power_cycle as opc

FABRIC = {
    "pass": {"ok": True, "detail": "fabric links healthy (53s)"},
    "fail": {"ok": False, "detail": "fabric check FAILED (exit 1)"},
    "missing": {"ok": False, "detail": f"{fabric.LINKS_MISSING}: 1 port(s) missing vs the cabling descriptor"},
}
VERDICTS = sorted(FABRIC)


def _stub_verify(monkeypatch, verdict, calls=None):
    """enum+ARC healthy; a fabric verdict only on a pass that runs the fabric check, whose failure
    fails the whole verify (as the real one does)."""

    async def verify(expected, log, run_fabric=True, **_):
        if calls is not None:
            calls.append(run_fabric)
        healthy = not (run_fabric and FABRIC[verdict]["ok"] is False)
        return healthy, {"snapshot": {"ok": True}, **({"fabric": dict(FABRIC[verdict])} if run_fabric else {})}

    patch_recovery(monkeypatch, "_verify_device", verify)


def _hold_marks():
    return srv.fsm.state, srv.fsm.record.why, srv.fsm.record.since, srv.device_hold_episode_since


# --- the one test every lift passes (the release funnel and the rung fold) ----------------------


@pytest.mark.parametrize("verdict", VERDICTS)
def test_the_release_funnel_lifts_only_on_a_fresh_passing_fabric_verdict(mesh, verdict):  # noqa: F811
    fsm_dirty(srv, "gate/post-job: exit 1", why="job_killed")
    before = _hold_marks()
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC[verdict])})
    released = srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy")
    assert released is (verdict == "pass")
    if verdict != "pass":
        assert _hold_marks() == before, "a refused lift leaves the hold, its episode and its clock alone"
        assert ("hold_lift_refused" in [k for k, _ in mesh]) and "device_released" not in [k for k, _ in mesh]


@pytest.mark.parametrize("why", ["startup: fabric topology mismatch", "reset_verified: tt-smi -glx_reset"])
def test_the_startup_and_post_reset_releases_refuse_a_missing_link(mesh, why):  # noqa: F811
    """The startup gate and a reset's verify release through the same funnel: a check that found a
    port of the cabling missing FAILED, so neither lifts the hold."""
    fsm_dirty(srv, "gate/startup: held across the restart", why="fabric_unverified")
    before = _hold_marks()
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC["missing"])})
    assert srv._clear_device_dirty(verified=True, why=why) is False
    assert _hold_marks() == before


@pytest.mark.parametrize("stale", ["older_than_the_fault", "too_old"])
def test_a_pass_from_before_the_fault_or_too_long_ago_is_refused(mesh, monkeypatch, stale):  # noqa: F811
    if stale == "older_than_the_fault":
        FABRIC_DEBT.note_fabric(True)
        fsm_dirty(srv, "gate/post-job: exit 1", why="job_killed")
    else:
        fsm_dirty(srv, "gate/post-job: exit 1", why="job_killed")
        FABRIC_DEBT.note_fabric(True)
        monkeypatch.setattr(FABRIC_DEBT, "pass_monotonic", time.monotonic() - srv.HOLD_LIFT_PASS_FRESH_SEC - 1)
    assert srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy") is False
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.parametrize("verdict", VERDICTS)
def test_a_rung_recovered_outcome_lifts_only_on_a_passing_fabric_verdict(mesh, verdict):  # noqa: F811
    fsm_dirty(srv, "gate/post-job: mesh failed the verify after the reset", why="job_killed")
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC[verdict])})
    srv._fold_rung_outcome(galaxy.OUTCOME_RECOVERED, lambda m: None)
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")


# --- each idle lift runs the full check first ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_eth_frozen_hold_lifts_only_on_a_passing_full_check(monkeypatch, tmp_path, clear_job_state, verdict):
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    calls = []
    _stub_verify(monkeypatch, verdict, calls)

    async def eth_advancing(timeout_sec=60.0):
        return True, "every active-eth-core heartbeat advancing"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_advancing)
    before = _hold_marks()
    await srv._attempt_idle_relift()
    assert True in calls, "the full check, fabric included"
    if verdict == "pass":
        assert srv.fsm.state is ServerState.HEALTHY
    else:
        assert _hold_marks() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_off_bus_floor_hold_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    fsm_dirty(srv, "gate/post-job: 3/32 off-bus below the reset floor — held, not reset", why="off_bus")
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    _stub_verify(monkeypatch, verdict)

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)
    await srv._attempt_idle_relift()
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")
    assert (srv.fsm.record.why == "off_bus") is (verdict != "pass")


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_a_down_off_bus_recheck_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    _setup_down_offbus(monkeypatch, tmp_path)
    _stub_verify(monkeypatch, verdict)
    await srv._attempt_down_offbus_recheck()
    assert srv.fsm.state is (ServerState.HEALTHY if verdict == "pass" else ServerState.DOWN)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_a_handed_off_hold_with_no_power_cycle_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """No power cycle is opted in, the hold's one mesh reset ran and failed, and the hold went to an
    operator, latched and paged once (spec 04 I33, I87), past the 10-min bound (I76). The hand-off
    neither blocks nor loosens the lift: once the chips are back, the relift lifts it only on a full
    check whose fabric passed on every cabled link. A failed check or a link short of the cabling keeps
    the hold, its episode and its hand-off, fires no rung, and pages no more."""
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    fsm_dirty(srv, "gate/post-job: 4/32 off-bus below the reset floor — held, not reset", why="off_bus")
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", raising=False)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    _past_the_bound(monkeypatch)
    episode = srv.device_hold_episode_since
    monkeypatch.setattr(srv.recovery_mechanism, "mesh_reset_failed_since_release", True)
    monkeypatch.setattr(srv.recovery_mechanism, "mesh_reset_failed_episode", episode)
    assert srv.recovery_mechanism.mesh_reset_spent(episode)
    alerts, resets, cycles = [], [], []
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: alerts.append(event))
    monkeypatch.setattr(opc, "_pending", {})
    opc.note_hold_handoff(episode, 4, 32, "mesh reset failed", lambda m: None, latched="no power cycle")

    async def reset(indices, log):
        resets.append(indices)
        return True

    async def power_cycle(log, reason):
        cycles.append(reason)

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    _stub_verify(monkeypatch, verdict)

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)
    for _ in range(2):  # a second pass pages nothing new and still fires no rung
        srv.last_relift_monotonic = 0.0
        await srv._attempt_idle_relift()
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")
    assert resets == [] and cycles == [], "the relift fires no rung: no mesh reset, no power cycle"
    assert [a["kind"] for a in alerts] == ["offbus_gated_hold_page"], "the hand-off pages once"
    if verdict != "pass":
        assert srv.fsm.record.why == "off_bus" and srv.device_hold_episode_since == episode
        assert opc.hold_handoff_status(episode)["owner"] == "operator"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_a_foreign_holder_hold_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """The holder left: the hold lifts on the full check, fabric included, never the pre-job verify
    alone. A fabric failure keeps the foreign_holder hold (spec 03 I43)."""
    counters, _calls, _events = _fast_verify_setup(monkeypatch, tmp_path)
    calls = []
    _stub_verify(monkeypatch, verdict, calls)
    await srv._attempt_idle_relift()
    assert calls == [True], "one full check, fabric included"
    assert counters["dirty_cleared"] == (1 if verdict == "pass" else 0)
    assert counters["resets"] == 0, "inside the ceiling a failed check holds; the bound's ladder owns it"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_an_operator_reset_releases_only_on_a_passing_full_check(mesh, monkeypatch, verdict):  # noqa: F811
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    fsm_dirty(srv, "operator reset", why="job_killed")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    _stub_verify(monkeypatch, verdict)
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert (got is True) is (verdict == "pass")
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")


# --- replay: a topology hold with the link still missing ---------------------------------------


@pytest.mark.asyncio
async def test_replay_a_missing_link_hold_survives_every_lift_attempt_until_a_full_pass(
    monkeypatch, tmp_path, clear_job_state
):
    """Replay: an rc-77 topology-mismatch hold whose link stays missing. The I40 bound, an operator
    reset and the bound ladder's fold each try to lift it; none releases a job, and the hold keeps its
    episode and clock. Only a full check that passes on the whole cabling then lifts it. Fails on a
    branch where the I40 bound releases on the startup checks and an advancing eth read."""
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "")
    monkeypatch.setattr(srv.health_monitor, "topology_lift_pending", False)
    fsm_healthy(srv)
    fsm_dirty(srv, "gate/post-job: fabric unverified (could not run): topology mismatch", why="fabric_unverified")
    released = []
    monkeypatch.setattr(srv, "_journal_fsm_transition", lambda why, verified=False: released.append(why))

    async def mismatch_77(expected, log, run_fabric=True, **_):
        detail = "fabric check COULD NOT RUN (19s): topology mismatch; traffic-only rerun: hugepages"
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": detail}}

    patch_recovery(monkeypatch, "_verify_device", mismatch_77)
    before = _hold_marks()

    await srv._attempt_idle_relift()  # the I40 bound: it climbs, it never releases
    assert rungs == ["present"] and _hold_marks() == before and not released

    _stub_verify(monkeypatch, "missing")  # an operator reset: the link is still missing
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert got is not True and srv.fsm.state is not ServerState.HEALTHY
    assert srv.fsm.record.since == before[2] and srv.device_hold_episode_since == before[3]
    assert not released

    srv._fold_rung_outcome(galaxy.OUTCOME_RECOVERED, lambda m: None)  # the bound ladder's fold
    assert srv.fsm.state is not ServerState.HEALTHY and not released
    assert srv.fsm.record.since == before[2]

    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC["pass"])})  # the full check passes on every link
    assert srv._clear_device_dirty(verified=True, why="relift: full check passed") is True
    assert srv.fsm.state is ServerState.HEALTHY and released


@pytest.mark.asyncio
async def test_an_operator_reset_whose_release_the_funnel_refuses_reports_unverified(mesh, monkeypatch):  # noqa: F811
    """The funnel kept the hold: the operator is told the reset is not verified, never OK."""
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    fsm_dirty(srv, "operator reset", why="job_killed")
    _stub_verify(monkeypatch, "pass")
    monkeypatch.setattr(srv, "_hold_lift_unproven", lambda: "the last passing fabric pass is 400s old")
    got, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    assert got is None and srv.fsm.state is ServerState.RECOVERING


# --- the real fabric pass on a wrapper that exits 0 but finds a cabled link missing ---------------

WRAPPER_LOST = f"{_LOST}TOPOLOGY MISMATCH vs d: 1 missing, 0 extra port connections\nall links ok\n"
RELEASES = ("device_dirty_cleared", "device_released", "startup_fabric_released", "reset_verified")


@pytest.fixture
def wrapper(monkeypatch, tmp_path, eth_mesh):  # noqa: F811
    """The eth-timeout tests' mesh (the real probe pass, leaf probes stubbed) whose fabric pass is the
    monitor's own, on an operator wrapper that always exits 0. While ``fabric_bad`` (a reset sets it to
    ``not reset_heals``) its output carries the topology marker of one lost port and nothing extra, so
    the pass FAILS (spec 03 I71). ``fields`` keeps every broker event with its fields."""
    st = eth_mesh
    st.update(eth=ADVANCING, fabric_bad=True, fields=[])
    m = srv.health_monitor
    monkeypatch.delattr(m, "verify_fabric_health")  # the monitor's real pass, not the fixture's stand-in
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "/opt/fabric-check.sh")
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", raising=False)
    for attr, value in (
        ("topology_hw_fault", ""),
        ("topology_lift_pending", False),
        ("fabric_topology", {}),
        ("_topology_alerted", set()),
        ("_fabric_check_cmd", lambda: "/opt/fabric-check.sh"),
        ("_begin_action_row", lambda *a, **k: None),
        ("_write_action_log", lambda *a, **k: None),
    ):
        monkeypatch.setattr(m, attr, value)
    hdir = tmp_path / "health"
    hdir.mkdir()
    monkeypatch.setattr(monitor_mod, "health_dir", lambda: hdir)
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {"a": 0xC1})
    monkeypatch.setattr(monitor_mod, "health_event", lambda ev, **f: None)
    monkeypatch.setattr(monitor_mod.alert, "send_alert", lambda ev, log=None: object())
    monkeypatch.setattr(fabric, "build_command", lambda descriptor=True: (["run_cluster_validation"], {}))
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", "/x/d"))

    async def check(argv, env, **kw):
        st["fabric"].append(len(st["resets"]))
        return 0, WRAPPER_LOST if st["fabric_bad"] else "all links ok\n"

    monkeypatch.setattr(fabric, "check", check)

    def event(name, *a, **f):
        st["events"].append(name)
        st["fields"].append((name, f))

    patch_health_event(monkeypatch, event)
    return st


def _stub_power_cycle(monkeypatch) -> list:
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    return cycles


def _past_the_bound(monkeypatch) -> None:
    since = datetime.now() - timedelta(seconds=galaxy._stuck_hold_ceiling_sec() + 1)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())


def _released(st) -> list:
    """The release events seen. A device_dirty_cleared with verified=False only drops the dirty bit of a
    hold that stays (the gate's hold for self-heal): it releases nothing."""
    return [n for n, f in st["fields"] if n in RELEASES and f.get("verified", True) is not False]


@pytest.mark.asyncio
async def test_a_post_job_wrapper_pass_with_a_port_missing_holds_and_admits_no_job(wrapper):
    """(a) The job ends, the wrapper exits 0 but its marker says 1 missing, 0 extra: that check FAILED.
    The device is held, the next job's door stays shut, and nothing releases it."""
    await srv._device_health_gate(None, phase="post-job", run_fabric=False, force_fabric=True)
    assert wrapper["fabric"], "the post-job gate ran the fabric pass"
    assert srv.health_monitor.fabric_verdict()["links_short"] is True
    assert srv.fsm.state is not ServerState.HEALTHY, "a check short of the cabling never passes"
    assert srv._device_unavailable_for_tenant(), "no next job on a mesh with a cabled link missing"
    assert not _released(wrapper)
    await srv._device_health_gate(None, phase="pre-job", run_fabric=False)  # the next job's door
    assert srv._device_unavailable_for_tenant() and not _released(wrapper)


@pytest.mark.asyncio
async def test_a_kill_reset_whose_full_check_still_finds_the_port_missing_never_releases(wrapper, monkeypatch):
    """(b) A job the broker stopped mid-run owes one reset (spec 03 I69): the kill mesh reset runs
    glx_reset, and the full check after it still finds the cabled port missing. That is
    reset_ineffective: the hold stays and no second reset runs at the door. At the 10-min bound (spec 04
    I76) the ladder runs once more, its full check fails the same way, and the broker asks for exactly
    one power cycle. Nothing releases the device at any step."""
    wrapper["reset_heals"] = False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = _stub_power_cycle(monkeypatch)
    fsm_dirty(srv, "job 7 ended timeout")
    srv._owe_kill_reset("job 7 timeout")
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert wrapper["events"].count("kill_mesh_reset") == 1 and wrapper["resets"] == [["tt-smi", "-glx_reset"]]
    assert min(wrapper["fabric"]) >= 1, "every fabric pass ran after the reset"
    assert "reset_ineffective" in wrapper["events"]
    assert srv.fsm.state is ServerState.RECOVERING and srv._device_unavailable_for_tenant()
    assert not _released(wrapper) and not cycles
    _past_the_bound(monkeypatch)
    await srv._force_escalate_stuck_hold()
    assert len(cycles) == 1, "the bound's ladder did not heal it: one power cycle"
    assert srv.fsm.state is not ServerState.HEALTHY and not _released(wrapper)
    await srv._force_escalate_stuck_hold()
    assert len(cycles) == 1 and not _released(wrapper)


@pytest.mark.asyncio
async def test_a_latched_topology_hold_whose_bound_ladder_leaves_the_port_missing_power_cycles_once(
    wrapper, monkeypatch
):
    """(c) The lift's first job failed on the fabric, so the I42 latch (``topology_hw_fault``) is set and
    the device is held. At the 10-min bound (spec 04 I76) the ladder runs, the full check after each
    reset still finds the cabled port missing, and the broker asks for exactly one power cycle. A second
    bound tick adds no reset, check or cycle. The bound keeps the latch (it clears only
    ``fabric_known_fault``) and nothing releases the device."""
    mon = srv.health_monitor
    wrapper["reset_heals"] = False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = _stub_power_cycle(monkeypatch)
    monkeypatch.setattr(mon, "topology_lift_pending", True)
    why = mon.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert mon.topology_hw_fault, "the I42 latch is set"
    srv._hold_device_fabric_unverified(f"job 9: {why}")
    _past_the_bound(monkeypatch)
    await srv._force_escalate_stuck_hold()
    assert wrapper["resets"] and min(wrapper["fabric"]) >= 1, "every fabric pass ran after a reset"
    verdict = mon.fabric_verdict()
    assert verdict["result"] == "fail" and verdict["links_short"] is True
    assert len(cycles) == 1, "the bound's ladder did not heal it: one power cycle"
    seen = (len(wrapper["resets"]), len(wrapper["fabric"]))
    await srv._force_escalate_stuck_hold()
    assert len(cycles) == 1 and (len(wrapper["resets"]), len(wrapper["fabric"])) == seen
    assert mon.topology_hw_fault, "the bound keeps the I42 latch"
    assert srv.fsm.state is not ServerState.HEALTHY and not _released(wrapper)
    assert "device_released" not in wrapper["events"]
    cleared = [f.get("latch") for n, f in wrapper["fields"] if n == "hold_bound_latch_cleared"]
    assert "topology_hw_fault" not in cleared


@pytest.mark.parametrize("accepted", ["", "tray 1 QSFP_DD 2"])
def test_a_shortfall_the_accept_list_names_reads_excused_not_short(monkeypatch, accepted):
    """The fabric verdict every lift event carries (spec 03 I78). A shortfall whose every missing port
    the operator's accept list names passed the check (I71): ``links_short`` is False and
    ``excused_ports`` names them. Without the list the same marker is short and excuses nothing."""
    m = srv.health_monitor
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", accepted)
    monkeypatch.setattr(m, "_fabric_verdict", {})
    monkeypatch.setattr(m, "last_fabric_ok", bool(accepted))
    marker = fabric.topology_marker("d", 1, 0, (["tray 1 QSFP_DD 2"], []))
    m._note_fabric_verdict(bool(accepted), f"fabric links healthy on discovered links (50s); {marker}")
    v = m.fabric_verdict()
    assert v["missing_vs_cabling"] == 1 and v["links_short"] is (not accepted)
    assert v.get("excused_ports", []) == (["tray 1 QSFP_DD 2"] if accepted else [])


SHORT = (False, f"{fabric.LINKS_MISSING}: {fabric.topology_marker('d', 1, 0, (['tray 1 QSFP_DD 2'], []))}")
REFUSALS = ("post_reset_release_refused", "hold_lift_refused")


def _verdict_events(monkeypatch, r) -> list:
    """The rig's fabric pass, but it records its verdict as the monitor's own pass does, and every event
    is kept with its fields."""
    m = srv.health_monitor
    monkeypatch.setattr(m, "_fabric_verdict", {})
    monkeypatch.setattr(m, "last_fabric_ok", None)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", raising=False)

    async def fabric_pass(*a, **k):
        r.order.append("fabric")
        m.last_fabric_ok = r.fabric[0]
        m._note_fabric_verdict(*r.fabric)
        return r.fabric

    monkeypatch.setattr(m, "verify_fabric_health", fabric_pass)
    fields = []

    def event(name, *a, **f):
        fields.append((name, f))

    patch_health_event(monkeypatch, event)
    monkeypatch.setattr(post_reset, "health_event", event)
    # The stuck-hold arming helpers silence events; keep them recorded.
    monkeypatch.setattr(safety, "patch_health_event", lambda mp, fn: patch_health_event(mp, event))
    return fields


@pytest.mark.asyncio
@pytest.mark.parametrize("short", [False, True], ids=["pass", "link-short"])
@pytest.mark.parametrize("path", list(RESET_PATHS))
async def test_every_release_event_carries_the_passing_fabric_verdict_and_a_short_link_releases_nothing(
    rig, monkeypatch, tmp_path, path, short  # noqa: F811
):
    """Spec 03 I78 on every reset path (spec 03 I77's list). On a pass every release event carries
    ``fabric`` with result pass and no link short. With a cabled link missing nothing releases, and
    each refusal carries the failing verdict with ``links_short`` True. The idle off-bus escalation's
    reset releases through reset_verified/device_dirty_cleared, and the start after a broker restart
    closes its hold with startup_fabric_released, each through ``_full_check_gate``'s full check."""
    if short:
        rig.fabric = SHORT
    fields = _verdict_events(monkeypatch, rig)
    await _drive(path, rig, monkeypatch, tmp_path)
    names = [n for n, _ in fields]
    released = [(n, f) for n, f in fields if n in RELEASES and f.get("verified", True) is not False]
    if short:
        assert not released and srv.fsm.state is not ServerState.HEALTHY, names
        for n, f in fields:
            if n in REFUSALS:
                assert f["fabric"]["result"] == "fail" and f["fabric"]["links_short"] is True, (n, f)
        return
    assert released and srv.fsm.state is ServerState.HEALTHY, names
    for n, f in released:
        assert f["fabric"]["result"] == "pass" and f["fabric"]["links_short"] is False, (n, f)
    if path == "idle-offbus-escalation":
        assert {"reset_verified", "device_dirty_cleared"} & set(names), names
    if path == "startup":
        assert "startup_fabric_released" in names, names


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["kill-reset", "bound-ladder"])
async def test_a_reset_that_brings_the_port_back_releases_on_a_passing_verdict(wrapper, monkeypatch, path):
    """The kill reset at the post-job gate (spec 03 I69) and the 10-min bound's ladder (spec 04 I76),
    on the monitor's own fabric pass: the reset brings the cabled port back, the full check passes, and
    every release event carries that pass with no link short. No power cycle is asked for."""
    wrapper["reset_heals"] = True
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = _stub_power_cycle(monkeypatch)
    if path == "kill-reset":
        fsm_dirty(srv, "job 7 ended timeout")
        srv._owe_kill_reset("job 7 timeout")
        await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    else:
        srv._hold_device_fabric_unverified("job 9: fabric router could not find any forwarding direction")
        _past_the_bound(monkeypatch)
        await srv._force_escalate_stuck_hold()
    released = [(n, f) for n, f in wrapper["fields"] if n in RELEASES and f.get("verified", True) is not False]
    assert released and srv.fsm.state is ServerState.HEALTHY and not cycles, wrapper["events"]
    assert min(wrapper["fabric"]) >= 1, "every fabric pass ran after the reset"
    for n, f in released:
        assert f["fabric"]["result"] == "pass" and f["fabric"]["links_short"] is False, (n, f)
