# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Port connections missing against the cabling descriptor fail the fabric check (03 I71).

Traffic that passes on the discovered links does not make a lost link healthy: a job that routes
over the whole mesh (e.g. "Could not find any forwarding direction from src (M0, D0) to dst
(M0, D28)") fails on it. So the check FAILS by port name, the hold keeps the reset ladder, the
eth link mark is never lowered to the links that are left, and only an explicit accept list turns a
known-dead port into a lift. No device is opened: the validator, the chip snapshot, the reset and
the alert hook are stand-ins."""

import json
import time
from datetime import datetime, timedelta

import pytest

from tests import test_dispatch_failure_reset as dfr
from tests.test_dispatch_failure_reset import _gate, mesh  # noqa: F401  (fixture)
from tests.test_fabric_topology_mismatch import _LOST, _LOST_PORT, _MISMATCH, topo  # noqa: F401  (fixture)
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import fabric

ACCEPT = "TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS"


def _baseline(tmp_path) -> dict:
    return json.loads((tmp_path / HealthMonitor.ETH_LINK_BASELINE_FILE).read_text())


# --- naming the ports ---------------------------------------------------------------------------


def test_topology_ports_names_each_missing_and_extra_connection():
    assert fabric.topology_ports(_LOST) == ([_LOST_PORT], [])
    missing, extra = fabric.topology_ports(_MISMATCH)
    assert missing == ["tray 1 port ?", "tray 2 port ?"] and extra == ["tray 3 port ?", "tray 4 port ?"]


def test_the_marker_carries_the_names_and_they_parse_back():
    marker = fabric.topology_marker("/x/d.textproto", 1, 0, ([_LOST_PORT], []))
    assert marker.startswith("TOPOLOGY MISMATCH vs /x/d.textproto: 1 missing, 0 extra")
    assert f"missing ports: {_LOST_PORT}" in marker
    assert fabric.marker_ports(marker) == ([_LOST_PORT], [])


def test_a_long_port_list_is_capped_with_a_count():
    names = [f"tray 1 asic {i} QSFP_DD 1" for i in range(fabric._MAX_PORT_NAMES + 3)]
    marker = fabric.topology_marker("d", len(names), 0, (names, []))
    assert "(+3 more)" in marker
    assert fabric._MAX_PORT_NAMES == 16, "spec 03 I71: at most 16 names"


def test_a_wrapper_marker_without_names_takes_them_from_the_validator_output():
    """An override wrapper prints only counts; the validator's own lines above it name the ports."""
    text = f"{_LOST}TOPOLOGY MISMATCH vs d: 1 missing, 0 extra port connections\n"
    marker = fabric.with_ports(fabric.find_topology_marker(text), text)
    assert fabric.marker_ports(marker) == ([_LOST_PORT], [])


# --- the verdict ----------------------------------------------------------------------------------


def test_more_missing_than_extra_is_links_missing_and_a_moved_tray_is_not():
    lost = fabric.topology_marker("d", 1, 0, ([_LOST_PORT], []))
    moved = fabric.topology_marker("d", 2, 2, (["a", "b"], ["c", "d"]))
    assert fabric.links_missing(lost).startswith(fabric.LINKS_MISSING)
    assert _LOST_PORT in fabric.links_missing(lost)
    assert fabric.links_missing(moved) == ""


def test_only_an_explicit_accept_list_naming_every_missing_port_lifts(monkeypatch):
    two = fabric.topology_marker("d", 2, 0, ([_LOST_PORT, "tray 1 QSFP_DD 1"], []))
    monkeypatch.delenv(ACCEPT, raising=False)
    assert fabric.accepted_missing_ports() == frozenset(), "the default accepts nothing"
    monkeypatch.setenv(ACCEPT, f" {_LOST_PORT} ")
    accepted = fabric.accepted_missing_ports()
    assert fabric.links_missing(fabric.topology_marker("d", 1, 0, ([_LOST_PORT], [])), accepted) == ""
    assert fabric.links_missing(two, accepted), "a port not on the list still fails"
    # A count with no names (nothing to match) is never accepted.
    assert fabric.links_missing(fabric.topology_marker("d", 1, 0), accepted)


# --- the monitor ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lost_link_whose_rerun_passes_fails_the_check_and_names_the_port(
    topo,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    m, state = topo
    monkeypatch.delenv(ACCEPT, raising=False)
    state["first"] = _LOST
    m.eth_link_drop(256)
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is False and detail.startswith("fabric check FAILED")
    assert _LOST_PORT in detail and "1 port connection(s) of the cabling descriptor missing, 0 extra" in detail
    assert m.last_fabric_ok is False and _LOST_PORT in m.last_fabric_output
    assert m.topology_lift_pending is False, "no lift: the first job is not the judge"
    assert not (tmp_path / monitor_mod.TOPOLOGY_CACHE_FILE).exists(), "the cache is dropped"
    events = dict(state["events"])
    assert events["fabric_links_missing"]["missing_ports"] == [_LOST_PORT]
    assert "fabric_topology_mismatch" not in events
    assert m.fabric_topology["verdict"] == "failed" and m.fabric_topology["missing_ports"] == [_LOST_PORT]
    # The eth link mark stays at the descriptor's links: 252 of 256 still reads as a drop.
    assert "252 of 256" in m.eth_link_drop(252)
    assert _baseline(tmp_path)["links"] == 256
    # The post-reset check runs the descriptor again: a failure is never answered from the cache.
    state["calls"].clear()
    await m._verify_fabric_health_body(60)
    assert any("--cabling-descriptor-path" in c for c in state["calls"])


@pytest.mark.asyncio
async def test_an_override_wrapper_that_exits_0_on_a_lost_link_still_fails(topo, monkeypatch):  # noqa: F811
    m, _ = topo
    monkeypatch.delenv(ACCEPT, raising=False)

    async def check(argv, env, **kw):
        return 0, f"{_LOST}TOPOLOGY MISMATCH vs d: 1 missing, 0 extra port connections\nall links ok\n"

    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "/opt/fabric-check.sh")
    monkeypatch.setattr(fabric, "check", check)
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is False and _LOST_PORT in detail


@pytest.mark.asyncio
async def test_a_moved_tray_still_lifts_and_never_lowers_the_mark(topo, tmp_path, monkeypatch):  # noqa: F811
    m, state = topo
    monkeypatch.delenv(ACCEPT, raising=False)
    m.eth_link_drop(256)
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is True and m.topology_lift_pending is True
    assert m.fabric_topology["verdict"] == "lift"
    assert m.fabric_topology["extra_ports"] == ["tray 3 port ?", "tray 4 port ?"]
    assert "252 of 256" in m.eth_link_drop(252), "a lift without the accept list keeps the mark"
    assert _baseline(tmp_path)["links"] == 256


def test_a_mark_lowered_before_this_fix_is_restored(topo, tmp_path):  # noqa: F811
    """A baseline an earlier lift wrote at 252 (no accept flag) reads 252 as a drop again."""
    m, _ = topo
    (tmp_path / HealthMonitor.ETH_LINK_BASELINE_FILE).write_text(
        json.dumps({"links": 252, "high_water": 256, "asic_bus": {"a": 0xC1}})
    )
    assert "252 of 256" in m.eth_link_drop(252)
    assert _baseline(tmp_path)["links"] == 256


# --- the broker -----------------------------------------------------------------------------------

LOST_DETAIL = (
    "fabric check FAILED (66s): fabric links missing: 1 port connection(s) of the cabling descriptor "
    f"missing, 0 extra, after traffic passed on the discovered links: {_LOST_PORT}"
)
LOST_OUT = (
    f"{_LOST}TOPOLOGY MISMATCH vs d: 1 missing, 0 extra port connections; missing ports: {_LOST_PORT}\n"
    f"{LOST_DETAIL.split(': ', 1)[1]}\n"
)


def test_a_links_missing_hold_is_not_a_no_reset_topology_hold(monkeypatch):
    assert srv._names_topology_mismatch(f"gate/post-job: {LOST_DETAIL}") is False
    assert srv._validator_line_names_topology(LOST_OUT) is False
    assert srv._names_topology_mismatch("what(): Connection mismatch detected") is True
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", LOST_OUT)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", False)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "device_hold_episode_reason", "")
    srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail=f"gate: {LOST_DETAIL}", dirty=False)
    assert srv._fabric_no_reset_hold() == "", "the measured fault keeps the reset ladder (04 I30)"


@pytest.mark.asyncio
async def test_a_lift_the_accept_list_excused_names_the_accepted_ports(topo, monkeypatch):  # noqa: F811
    """Review #1495 F5: a lost link the operator's accept list excuses lifts, and the event, /health and
    the broker log name the port the list excused; a moved tray excuses nothing."""
    m, state = topo
    lines = []

    class _Log:
        def warning(self, msg):
            lines.append(msg)

        info = error = warning

    monkeypatch.setattr(m, "_logger", lambda: _Log())
    monkeypatch.setenv(ACCEPT, f"{_LOST_PORT}, tray 9 port 9")
    state["first"] = _LOST
    m.eth_link_drop(256)
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is True and m.fabric_topology["verdict"] == "lift"
    assert m.fabric_topology["accepted_ports"] == [_LOST_PORT], "only the missing port, not the whole list"
    assert dict(state["events"])["fabric_topology_mismatch"]["accepted_ports"] == [_LOST_PORT]
    assert any("TOPOLOGY MISMATCH" in ln and f"ACCEPT_MISSING_PORTS: {_LOST_PORT}" in ln for ln in lines)
    monkeypatch.setattr(srv.health_monitor, "fabric_topology", m.fabric_topology)
    assert srv._health_payload()["fabric_topology"]["accepted_ports"] == [_LOST_PORT]


@pytest.mark.asyncio
async def test_a_moved_tray_lift_excuses_no_port(topo, monkeypatch):  # noqa: F811
    m, state = topo
    monkeypatch.setenv(ACCEPT, _LOST_PORT)
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is True and m.fabric_topology["accepted_ports"] == []
    assert dict(state["events"])["fabric_topology_mismatch"]["accepted_ports"] == []


def test_health_names_the_ports(monkeypatch):
    topology = {"verdict": "failed", "missing_ports": [_LOST_PORT], "extra_ports": [], "marker": "m"}
    monkeypatch.setattr(srv.health_monitor, "fabric_topology", topology)
    assert srv._health_payload()["fabric_topology"]["missing_ports"] == [_LOST_PORT]


def test_health_shows_the_accepted_ports(monkeypatch):
    monkeypatch.delenv(ACCEPT, raising=False)
    assert srv._health_payload()["fabric_accepted_missing_ports"] == [], "nothing is accepted by default"
    monkeypatch.setenv(ACCEPT, f"{_LOST_PORT}, b")
    assert srv._health_payload()["fabric_accepted_missing_ports"] == sorted([_LOST_PORT, "b"])


@pytest.fixture
def lost_mesh(monkeypatch, mesh):  # noqa: F811
    """The mesh fixture failing on a lost link instead of a dispatch failure. Read-only (no fabric
    pass) the mesh reads healthy, as a lost link does: every chip present, ARC ticking, the eth read
    unable to tell. The fabric pass, as in the fixture, fails until a reset heals it."""
    monkeypatch.setattr(dfr, "FABRIC_OUT", LOST_OUT)
    monkeypatch.setattr(dfr, "DISPATCH_LINE", LOST_DETAIL)
    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    fabric_verify = srv.galaxy_recovery._verify_device

    async def verify(expected, log, run_fabric=True, **kw):
        if run_fabric:
            return await fabric_verify(expected, log, run_fabric=True, **kw)
        mesh["verifies"].append(False)
        return True, {"snapshot": {"ok": True, "detail": "32/32"}}

    async def eth_unreadable(timeout_sec=60.0):
        return None, "eth read timed out"

    for r in (srv.galaxy_recovery, srv.per_target_recovery):
        monkeypatch.setattr(r, "_verify_device", verify)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unreadable)
    dfr.fsm_dirty(srv, f"gate/post-job: {LOST_DETAIL}")
    return mesh


async def _gate_then_relift():
    """The post-job gate measures the FAIL and holds; the idle relift (SELFHEAL_RELIFT_INTERVAL_SEC
    later) resets it with no grace, a measured fault (03 I56)."""
    await _gate()
    assert srv.fsm.state is ServerState.RECOVERING and srv.health_monitor.last_fabric_ok is False
    assert srv._fabric_no_reset_hold() == "", "not a no-reset topology hold"
    await srv._attempt_idle_relift()


@pytest.mark.asyncio
async def test_a_lost_link_resets_and_only_a_full_post_reset_pass_releases(lost_mesh):
    """03 I71, a full check after every reset: the reset runs, the post-reset check runs the fabric pass, and only its
    pass releases the device."""
    await _gate_then_relift()
    assert lost_mesh["resets"] == [["tt-smi", "-glx_reset"]], "a measured fabric fault resets"
    assert lost_mesh["verifies"][-1] is True, "the post-reset check runs the fabric pass"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_lost_link_that_a_reset_does_not_heal_stays_held(lost_mesh):
    """No release on a failed post-reset check; the last-chance sweep runs and the hold climbs to the host
    rung at once, not at the 600 s ceiling (spec 04 I79)."""
    lost_mesh["reset_heals"] = False
    await _gate_then_relift()
    assert lost_mesh["resets"] and lost_mesh["verifies"][-1] is True
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed post-reset check never releases"
    assert srv.device_hold_episode_since, "the episode stays open: the hold ceiling forces the next rung"
    assert "reset_ineffective" in lost_mesh["events"]
    assert "host_rung_not_deferred" in lost_mesh["events"], "the host rung follows the sweep at once (04 I79)"
    assert "host_rung_deferred_until_ladder_end" not in lost_mesh["events"]


@pytest.mark.asyncio
async def test_a_lost_link_at_the_startup_gate_fails_and_keeps_the_carried_hold(
    monkeypatch, tmp_path, mesh  # noqa: F811
):
    """A restart is never trusted healthy (03 I4): at the startup gate a descriptor run with one lost
    port whose traffic-only rerun passes FAILS the check by name (I71), so a carried hold stays held
    and takes the ladder instead of a release onto the lost link."""
    mesh["descriptor_out"] = _LOST
    mesh["reset_heals"] = False
    _monitor_fabric(monkeypatch, tmp_path, mesh)
    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    dfr.fsm_dirty(srv, f"gate/post-job: {LOST_DETAIL}")
    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert mesh["fabric"], "the startup gate runs the fabric pass"
    ok, detail = mesh["fabric"][-1]
    assert ok is False and fabric.LINKS_MISSING in detail and _LOST_PORT in detail
    assert srv.health_monitor.topology_lift_pending is False, "a lost link is never a lift"
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed startup check never releases"


# --- the I42 latch through the broker -------------------------------------------------------------

_JOB_TAIL = "... could not find any forwarding direction from src (M0, D0) to dst (M0, D28) ..."


def _monitor_fabric(monkeypatch, tmp_path, st):
    """The broker's own monitor runs its fabric pass (the latch's path) on a stand-in validator: the
    descriptor run reports ``st['descriptor_out']`` (default a moved-tray mismatch, alone a lift) while ``st['bad']``, a clean run once
    a reset cleared it; the traffic-only rerun passes. ``st['calls']`` records each argv."""
    m = srv.health_monitor
    hdir = tmp_path / "health"
    hdir.mkdir()
    st.setdefault("calls", [])

    def build_command(descriptor=True):
        return ["run_cluster_validation"] + (["--cabling-descriptor-path", "/x/d"] if descriptor else []), {}

    async def check(argv, env, **kw):
        st["calls"].append(argv)
        if "--cabling-descriptor-path" in argv and st["bad"]:
            return 134, st.get("descriptor_out", _MISMATCH)
        return 0, "all links ok\n"

    for attr, value in (
        ("topology_hw_fault", ""),
        ("topology_lift_pending", False),
        ("fabric_topology", {}),
        ("_topology_alerted", set()),
        ("_fabric_check_cmd", lambda: ""),
        ("_begin_action_row", lambda *a, **k: None),
        ("_write_action_log", lambda *a, **k: None),
    ):
        monkeypatch.setattr(m, attr, value)
    monkeypatch.setattr(fabric, "build_command", build_command)
    monkeypatch.setattr(fabric, "check", check)
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", "/x/d"))
    monkeypatch.setattr(monitor_mod, "health_dir", lambda: hdir)
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {"a": 0xC1})
    monkeypatch.setattr(monitor_mod, "health_event", lambda ev, **f: st.setdefault("events", []).append(ev))
    monkeypatch.setattr(monitor_mod.alert, "send_alert", lambda ev, log=None: object())
    (hdir / monitor_mod.ASIC_BUS_FILE).write_text('{"a": 1}')
    monkeypatch.delenv(ACCEPT, raising=False)

    async def verify(expected, log, run_fabric=True, **kw):
        st.setdefault("verifies", []).append(run_fabric)
        snapshot = {"ok": True, "detail": f"all {expected} chips"}
        if not run_fabric:
            return True, {"snapshot": snapshot}
        ok, detail = await m._verify_fabric_health_body(60)
        st.setdefault("fabric", []).append((ok, detail))
        return ok is not False, {"snapshot": snapshot, "fabric": {"ok": ok, "detail": detail}}

    for r in (srv.galaxy_recovery, srv.per_target_recovery):
        monkeypatch.setattr(r, "_verify_device", verify)
    return m


def _latch(m) -> str:
    """The lift's first job dies on the fabric: the latch sets, as the job runner calls it."""
    m.topology_lift_pending = True
    why = m.note_job_after_topology_lift(True, _JOB_TAIL)
    assert why and m.topology_hw_fault == why and m.last_fabric_ok is False
    return why


@pytest.fixture
def latched_mesh(monkeypatch, tmp_path, mesh):  # noqa: F811
    """The mesh fixture held on the I42 latch: the job runner's fabric-unverified hold, idle, with the
    monitor's own fabric pass behind the verify."""
    m = _monitor_fabric(monkeypatch, tmp_path, mesh)
    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    dfr.fsm_dirty(srv, f"job 7: {_latch(m)}", why=srv.FABRIC_RELIFT_WHY)
    # The hold episode opened just now: a measured fault resets with no grace (03 I56).
    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.now().isoformat())
    return mesh


@pytest.mark.asyncio
@pytest.mark.parametrize("rung", ["power-cycle"])
async def test_a_latched_hold_resets_and_a_post_reset_mismatch_fails_and_climbs(
    monkeypatch, latched_mesh, rung
):
    """I42: the idle relift resets the latched hold with no grace; the post-reset check runs the
    descriptor, the latched mismatch FAILS it (never withheld, never a lift), the last-chance sweep
    runs and the hold climbs to the host rung at once (spec 04 I79): bounded, never an open-ended
    hold. A later forced window asks for no second host rung and still releases nothing."""
    from tt_device_mcp.health.recovery import galaxy

    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    async def host_reboot(log, reason):
        latched_mesh["reboots"] += 1
        srv.recovery_mechanism.host_action_requested = ("reboot", time.monotonic())

    # Like the real rungs, each stand-in leaves its request pending.
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    latched_mesh["reset_heals"] = False
    await srv._attempt_idle_relift()
    assert latched_mesh["resets"] == [["tt-smi", "-glx_reset"]] * 2, "the present-mesh rung, then the sweep (04 I79)"
    assert latched_mesh["verifies"][-1] is True, "the post-reset check runs the fabric pass"
    ok, detail = latched_mesh["fabric"][-1]
    assert ok is False and fabric.LINKS_MISSING in detail and "withheld" not in detail
    assert any("--cabling-descriptor-path" in c for c in latched_mesh["calls"]), "the descriptor ran again"
    assert srv.health_monitor.topology_lift_pending is False, "no second lift"
    assert srv.health_monitor.topology_hw_fault, "a failed check leaves the latch standing"
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed post-reset check never releases"
    assert "reset_ineffective" in latched_mesh["events"]
    assert "host_rung_not_deferred" in latched_mesh["events"], "the host rung follows the sweep at once"
    fired = "reboot" if latched_mesh["reboots"] else "power-cycle" if cycles else None
    assert fired == rung, f"the relift's ladder fires the {rung} rung"
    # The ceiling: the hold-deadline watchdog's forced window asks for no second host rung.
    ceiling = galaxy._stuck_hold_ceiling_sec()
    since = datetime.now() - timedelta(seconds=ceiling + 1)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    await srv._force_escalate_stuck_hold()
    assert latched_mesh["reboots"] + len(cycles) == 1, "the host rung is asked for once"
    assert srv.fsm.state is not ServerState.HEALTHY, "no release without a passing full check"


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [True, False])
@pytest.mark.parametrize("rung", ["power-cycle"])
async def test_a_latched_hold_no_relift_reset_takes_the_ladder_at_the_ceiling(
    monkeypatch, tmp_path, mesh, dirty, reboot, rung  # noqa: F811
):
    """I42 with the 10-min bound: a real lift leaves the validator's mismatch line in the last fabric
    output, and its first job latches the fault. When no idle relift reset has run first (the device
    dirty, a tenant, a host action pending), the watchdog's forced window still runs the present-mesh
    reset, never a no-reset hold; its full post-reset check fails, and the same window climbs to the
    host rung. Never released without a passing full check."""
    from tt_device_mcp.health.recovery import galaxy

    m = _monitor_fabric(monkeypatch, tmp_path, mesh)
    ok, _detail = await m._verify_fabric_health_body(60)
    assert ok is not False and srv._TOPOLOGY_VALIDATOR_LINE in (m.last_fabric_output or ""), "a real lift"
    dfr.fsm_dirty(srv, f"job 7: {_latch(m)}", why=srv.FABRIC_RELIFT_WHY, dirty=dirty)
    assert srv._fabric_no_reset_hold() == "", "a latched fault is measured: never a no-reset hold"
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    mesh["reset_heals"] = False
    ceiling = galaxy._stuck_hold_ceiling_sec()
    since = datetime.now() - timedelta(seconds=ceiling + 1)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    await srv._force_escalate_stuck_hold()
    assert "hold_deadline_no_reset" not in mesh["events"], "the ceiling never parks a latched hold"
    assert mesh["resets"] and all(r == ["tt-smi", "-glx_reset"] for r in mesh["resets"]), "the present-mesh rung"
    assert mesh["verifies"][-1] is True and mesh["fabric"][-1][0] is False, "the full post-reset check fails"
    fired = "reboot" if mesh["reboots"] else "power-cycle" if cycles else None
    assert fired == rung, f"the same window fires the {rung} rung once the ladder's check fails"
    assert srv.fsm.state is not ServerState.HEALTHY, "no release without a passing full check"


@pytest.mark.asyncio
@pytest.mark.parametrize("latched", [False, True], ids=["no-latch", "i42-latch"])
async def test_replay_a_held_77_whose_bound_ladder_leaves_a_port_missing_power_cycles_once(
    monkeypatch, tmp_path, mesh, latched  # noqa: F811
):
    """Replay of a held rc 77 (spec 04 I76 with spec 03 I71): the hold reaches the 10-min bound, the
    bound's ladder runs glx_reset and it exits 0, but the full check after it still finds one
    descriptor port missing while the traffic-only rerun passes. That check FAILS: the device is
    never released and the broker asks for exactly one power cycle; a later watchdog tick asks for
    no second one while the first is pending."""
    from tt_device_mcp.health.recovery import galaxy

    m = _monitor_fabric(monkeypatch, tmp_path, mesh)
    if latched:
        ok, _detail = await m._verify_fabric_health_body(60)
        assert ok is not False, "a real lift"
        dfr.fsm_dirty(srv, f"job 7: {_latch(m)}", why=srv.FABRIC_RELIFT_WHY)
    else:
        dfr.fsm_dirty(srv, "post-job: fabric unverified (rc 77)", why=srv.FABRIC_RELIFT_WHY)
    mesh["descriptor_out"] = _LOST
    mesh["reset_heals"] = False
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    since = datetime.now() - timedelta(seconds=galaxy._stuck_hold_ceiling_sec() + 1)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    await srv._force_escalate_stuck_hold()
    assert mesh["resets"] and all(r == ["tt-smi", "-glx_reset"] for r in mesh["resets"]), "the bound's ladder"
    ok, detail = mesh["fabric"][-1]
    assert ok is False and fabric.LINKS_MISSING in detail and _LOST_PORT in detail, "one port missing FAILS"
    assert mesh["calls"][-1] == ["run_cluster_validation"], "even though the traffic-only rerun passed"
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed post-ladder check never releases"
    assert "device_released" not in mesh["events"]
    assert mesh["reboots"] == 0 and len(cycles) == 1, "exactly one power cycle is asked for"
    await srv._force_escalate_stuck_hold()
    assert len(cycles) == 1, "no second power cycle while the first is pending"
    assert srv.fsm.state is not ServerState.HEALTHY and "device_released" not in mesh["events"]


@pytest.mark.asyncio
async def test_a_latched_hold_whose_reset_heals_releases_only_on_a_full_pass(latched_mesh):
    """Goal of 03 I42 with the post-reset rule: the reset that brings the links back releases the device
    only once its full post-reset check, the descriptor run included, passes; that pass ends the latch."""
    await srv._attempt_idle_relift()
    assert latched_mesh["resets"] == [["tt-smi", "-glx_reset"]]
    assert latched_mesh["verifies"][-1] is True and latched_mesh["fabric"][-1][0] is True
    assert "--cabling-descriptor-path" in latched_mesh["calls"][-1], "a clean descriptor run, not the cache"
    assert srv.health_monitor.topology_hw_fault == "", "the clean pass ends the latch"
    assert "fabric_topology_hw_fault_cleared" in latched_mesh["events"]
    assert srv.fsm.state is ServerState.HEALTHY


def _idle_queue(monkeypatch):
    """Nothing queued, running or owed, no tenant, leftover, reset scope or off-bus chip: the idle
    tick's own guards for the dirty idle gate (03 I68)."""
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan())
    monkeypatch.setattr(srv.galaxy_recovery, "_tenant_active", lambda scan, **k: False)
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: "")
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(srv, "_down_offbus_recheck_armed", lambda: False)
    monkeypatch.setattr(srv, "_maybe_spawn_eth_rearm", lambda: None)
    monkeypatch.setattr(srv, "_relift_task", None)
    monkeypatch.setattr(srv, "_dirty_idle_gate_monotonic", 0.0)
    monkeypatch.setattr(srv, "_dirty_idle_gate_spent_since", None)
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "post_job_gate_pending", False)
    monkeypatch.setattr(srv, "device_op_active", "")
    srv.readopted_scopes.clear()


async def _idle_tick():
    srv._maybe_spawn_idle_relift()
    if srv._relift_task is not None:
        await srv._relift_task


@pytest.mark.asyncio
@pytest.mark.parametrize("heals", [True, False], ids=["reset-heals", "ports-stay-missing"])
async def test_a_dirty_latched_hold_with_an_empty_queue_takes_the_idle_gate_at_once(monkeypatch, latched_mesh, heals):
    """03 I42 with I68 (class D time to recover): a dirty mark on the latched hold (the failed job's own)
    keeps its why, so the read-only relift bails on it and, with nothing queued, it waited for the
    600 s bound. Now the first idle tick runs the present-mesh rung: the reset with no grace, then
    the full check with the descriptor. Only its pass releases (and ends the latch); a
    failed check never releases and the ladder climbs to the host rung at once."""
    _idle_queue(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    srv._mark_device_dirty("job 7 ended failed")
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY and srv.fsm.record.dirty, "the latched hold, now dirty"
    latched_mesh["reset_heals"] = heals
    await _idle_tick()
    assert latched_mesh["resets"] and latched_mesh["resets"][0] == ["tt-smi", "-glx_reset"], "reset at once"
    assert latched_mesh["verifies"][-1] is True, "the check after the reset runs the fabric pass"
    assert any("--cabling-descriptor-path" in c for c in latched_mesh["calls"]), "the descriptor ran again"
    ok, detail = latched_mesh["fabric"][-1]
    if heals:
        assert ok is True and srv.fsm.state is ServerState.HEALTHY, "only a passing full check releases"
        assert srv.health_monitor.topology_hw_fault == "", "the clean pass ends the latch"
        assert not cycles
        return
    assert ok is False and fabric.LINKS_MISSING in detail, "the latched mismatch FAILS the check"
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed post-reset check never releases"
    assert "device_released" not in latched_mesh["events"]
    assert srv.health_monitor.topology_hw_fault, "a failed check leaves the latch standing"
    assert len(cycles) == 1, "the ladder climbs to the power cycle at once, not at 600 s"
    # One gate per episode; the 10-min bound (04 I76) still owns the hold and asks for no second cycle.
    monkeypatch.setattr(srv, "_dirty_idle_gate_monotonic", 0.0)
    srv._relift_task = None
    resets = len(latched_mesh["resets"])
    await _idle_tick()
    assert len(latched_mesh["resets"]) == resets, "no second idle gate on the same episode"
    from tt_device_mcp.health.recovery import galaxy

    since = datetime.now() - timedelta(seconds=galaxy._stuck_hold_ceiling_sec() + 1)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    await srv._force_escalate_stuck_hold()
    assert len(cycles) == 1, "no second power cycle while the first is pending"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize("heals", [True, False], ids=["reset-heals", "ports-stay-missing"])
async def test_a_paced_present_rung_does_not_spend_the_dirty_latched_idle_gate(monkeypatch, latched_mesh, heals):
    """03 I68 class D, paced: a mesh reset started 60 s ago, so the present-mesh rung waits out its
    grace (WAITING, no reset). That tick must not spend the episode's one gate, or the hold waits for
    the 600 s bound. The first tick after the pacing runs exactly one reset and the full check with
    the descriptor; only its pass releases, and a failure takes exactly one power cycle."""
    from tt_device_mcp.health.recovery import galaxy

    _idle_queue(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", time.monotonic() - 60)
    srv._mark_device_dirty("job 7 ended failed")
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY and srv.fsm.record.dirty, "the latched hold, now dirty"
    latched_mesh["reset_heals"] = heals
    await _idle_tick()
    assert latched_mesh["resets"] == [], "paced: no reset 60 s after the last one"
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == srv.FABRIC_RELIFT_WHY, "still held"
    assert "device_released" not in latched_mesh["events"] and not cycles
    assert srv._dirty_idle_gate_armed(), "a tick that ran no reset leaves the episode's gate armed"
    # The grace has passed since that reset started: the next recheck tick takes the rung.
    srv.recovery_mechanism.last_reset_monotonic = time.monotonic() - galaxy._present_mesh_reset_grace_sec() - 1
    monkeypatch.setattr(srv, "_dirty_idle_gate_monotonic", 0.0)
    srv._relift_task = None
    await _idle_tick()
    assert latched_mesh["resets"] and latched_mesh["resets"][0] == ["tt-smi", "-glx_reset"], "reset once paced"
    assert latched_mesh["verifies"][-1] is True, "the check after the reset runs the fabric pass"
    assert any("--cabling-descriptor-path" in c for c in latched_mesh["calls"]), "the descriptor ran again"
    ok, detail = latched_mesh["fabric"][-1]
    if heals:
        assert latched_mesh["resets"] == [["tt-smi", "-glx_reset"]]
        assert ok is True and srv.fsm.state is ServerState.HEALTHY, "only a passing full check releases"
        assert srv.health_monitor.topology_hw_fault == "" and not cycles
        return
    assert ok is False and fabric.LINKS_MISSING in detail, "the latched mismatch FAILS the check"
    assert srv.fsm.state is not ServerState.HEALTHY and "device_released" not in latched_mesh["events"]
    assert len(cycles) == 1, "a failed check takes exactly one power cycle"
    # The episode's gate is spent now: no second reset from the idle gate on the same episode.
    resets = len(latched_mesh["resets"])
    monkeypatch.setattr(srv, "_dirty_idle_gate_monotonic", 0.0)
    srv._relift_task = None
    await _idle_tick()
    assert len(latched_mesh["resets"]) == resets and len(cycles) == 1


@pytest.mark.asyncio
async def test_an_unlatched_dirty_fabric_hold_does_not_take_the_idle_gate(monkeypatch, tmp_path, mesh):  # noqa: F811
    """The gate is for the I42 latch only: a dirty fabric-unverified hold with no latch (a 77) keeps
    today's owners (no reset on a pass that could not run, 03 I29/I40)."""
    _monitor_fabric(monkeypatch, tmp_path, mesh)
    _idle_queue(monkeypatch)
    dfr.fsm_dirty(srv, "post-job: fabric unverified (rc 77)", why=srv.FABRIC_RELIFT_WHY, dirty=True)
    assert not srv._dirty_idle_gate_armed()


@pytest.mark.parametrize("route", ["/api/tt_device_reset", "/api/tt_device_reset_stream"])
@pytest.mark.parametrize("heals", [True, False])
def test_an_operator_reset_keeps_the_latch_and_only_a_clean_fabric_pass_ends_it(monkeypatch, tmp_path, route, heals):
    """Spec 03 I78: both operator reset routes keep the I42 latch through the reset, and the post-reset
    check runs the fabric pass (never withheld): a clean descriptor run ends the latch and releases, a
    mismatch still there FAILS under the latch and keeps the hold (never a watched lift)."""
    from starlette.testclient import TestClient

    from tests.conftest import fsm_dirty

    dev = tmp_path / "dev"
    dev.mkdir()
    for i in range(2):
        (dev / str(i)).write_text("")
    st = {"bad": not heals}
    m = _monitor_fabric(monkeypatch, tmp_path, st)
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: dfr.HolderScan(holders=[], complete=True))

    async def run(argv, log, owner="[broker]health-gate", on_output=None):
        st.setdefault("resets", []).append(list(argv))
        return 0, ""

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    fsm_dirty(srv, f"job 7: {_latch(m)}", why=srv.FABRIC_RELIFT_WHY)
    TestClient(srv.build_asgi_app(srv.create_mcp_server())).post(route, json={"force": False})
    assert st["resets"], "the reset ran"
    assert st["verifies"] == [True], "the post-reset check runs the fabric pass"
    ok, detail = st["fabric"][-1]
    assert "withheld" not in detail
    assert m.topology_lift_pending is False, "never a watched lift onto the links in doubt"
    if heals:
        assert ok is True and srv.fsm.state is ServerState.HEALTHY
        assert m.topology_hw_fault == "", "the clean pass ends the latch"
        assert "fabric_topology_hw_fault_cleared" in st["events"]
    else:
        assert ok is False, "a mismatch under the latch FAILS"
        assert srv.fsm.state is not ServerState.HEALTHY, "the hold stays"
        assert m.topology_hw_fault, "the latch stands"
        assert "fabric_topology_hw_fault_cleared" not in st["events"]
