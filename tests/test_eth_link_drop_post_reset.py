# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Replay of a post-reset sequence seen on a galaxy box: after a reset the eth read measures fewer
up links than the high-water mark (the link-count-drop skip), the descriptor run finds one port
connection missing and none extra, and the traffic-only rerun passes on the links that are left.

The full post-reset check (03 I71, a full check after every reset) must FAIL on the missing port and
keep the device held; the eth mark must stay at the high-water mark (no rebaseline to the degraded
count); and the next between-jobs gate must fail the same way rather than lift. The control: the
same sequence with every port present releases.

The real probe pass (HealthMonitor.update), the real eth read and the real fabric check run here;
only their leaf commands (the eth probe, the validator), the snapshot, host PCI, the reset and the
alert hook are stand-ins. No device is opened."""

import json

import pytest

from tests import test_eth_timeout_reset as etr
from tests.conftest import fsm_dirty
from tests.test_eth_timeout_reset import mesh  # noqa: F401  (fixture)
from tests.test_fabric_topology_mismatch import _LOST, _LOST_PORT
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import ETH_LINK_DROP_SKIP, HealthMonitor
from tt_device_mcp.health.monitors import eth, fabric
from tt_device_mcp.health.post_reset import FABRIC_DEBT

HIGH_WATER = 256
LEFT = 252  # four eth links down: one QSFP_DD port connection's worth on the descriptor


@pytest.fixture
def replay(monkeypatch, tmp_path, mesh):  # noqa: F811
    """``mesh`` from the eth-timeout tests, with the monitor's own eth read and fabric check put back.
    The eth probe measures ``links`` up links; the descriptor run reports one missing port while
    ``lost`` (a reset does not bring it back); the traffic-only rerun always passes."""
    mon = srv.health_monitor
    hdir = tmp_path / "health"
    hdir.mkdir()
    mesh.update(links=LEFT, lost=True, calls=[], fabric_out=[], mon_events=[])
    (hdir / HealthMonitor.ETH_LINK_BASELINE_FILE).write_text(json.dumps({"links": HIGH_WATER}))
    (hdir / monitor_mod.ASIC_BUS_FILE).write_text('{"a": 1}')
    monkeypatch.setattr(monitor_mod, "health_dir", lambda: hdir)
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {"a": 0xC1})
    monkeypatch.setattr(monitor_mod, "health_event", lambda ev, **f: mesh["mon_events"].append((ev, f)))
    monkeypatch.setattr(monitor_mod.alert, "send_alert", lambda ev, log=None: object())
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", raising=False)
    monkeypatch.setattr(srv, "last_relift_monotonic", srv.last_relift_monotonic)
    for attr, value in (
        ("verify_eth_heartbeat", HealthMonitor.verify_eth_heartbeat.__get__(mon)),
        ("verify_fabric_health", HealthMonitor.verify_fabric_health.__get__(mon)),
        ("_eth_heartbeat_cmd", lambda: ""),
        ("_fabric_check_cmd", lambda: ""),
        ("topology_hw_fault", ""),
        ("topology_lift_pending", False),
        ("fabric_topology", {}),
        ("_topology_alerted", set()),
        ("_begin_action_row", lambda *a, **k: None),
        ("_write_action_log", lambda *a, **k: None),
    ):
        monkeypatch.setattr(mon, attr, value)

    async def eth_check(argv, env, **kw):
        mesh["eth_reads"] += 1
        return 0, f"all active-eth cores advancing\neth-links: measured={mesh['links']}\n"

    def build_command(descriptor=True):
        return ["run_cluster_validation"] + (["--cabling-descriptor-path", "/x/d"] if descriptor else []), {}

    async def fabric_check(argv, env, **kw):
        mesh["calls"].append(argv)
        mesh["fabric_out"].append((len(mesh["resets"]), "--cabling-descriptor-path" in argv, mesh["lost"]))
        if "--cabling-descriptor-path" in argv and mesh["lost"]:
            return 134, _LOST
        return 0, "all links ok\n"

    monkeypatch.setattr(eth, "build", lambda: (["eth-probe"], {}))
    monkeypatch.setattr(eth, "check", eth_check)
    monkeypatch.setattr(fabric, "build_command", build_command)
    monkeypatch.setattr(fabric, "check", fabric_check)
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", "/x/d"))
    monkeypatch.setattr(fabric, "validator_missing", lambda: None)
    # The reset stand-in owes the fabric pass as the real reset does (04 I32).
    g = srv.galaxy_recovery
    stub_reset = g.mechanism.reset_with_quiesce

    async def reset(argv, log, *a, **k):
        FABRIC_DEBT.note_reset(" ".join(argv))
        if mesh.get("heals"):
            mesh.update(lost=False, links=mesh["links_after"])
        return await stub_reset(argv, log, *a, **k)

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    mesh["hdir"] = hdir
    return mesh


def _mark(st) -> int:
    return json.loads((st["hdir"] / HealthMonitor.ETH_LINK_BASELINE_FILE).read_text())["links"]


def _mon_events(st) -> list[str]:
    return [ev for ev, _ in st["mon_events"]]


async def _hold_then_reset(st, resets=1):
    """The post-job gate measures the lost port and holds (no reset on a present mesh below the
    floor); the idle relift resets the held mesh with no grace (03 I56) and runs the full check.
    A check that still fails runs the last-chance sweep too (spec 04 I79): two resets."""
    fsm_dirty(srv, "job 7 crashed: could not find any forwarding direction", why="job_killed")
    await etr._gate()
    assert st["resets"] == [] and srv.fsm.state is ServerState.RECOVERING
    st["log"].said.clear()
    srv.last_relift_monotonic = 0.0
    await srv._attempt_idle_relift()
    assert st["resets"] == [["tt-smi", "-glx_reset"]] * resets, "the held mesh takes the present-mesh reset"


def _post_reset_descriptor_runs(st) -> list[bool]:
    """Whether the port was lost on each descriptor run made after the reset."""
    return [lost for resets, descriptor, lost in st["fabric_out"] if resets and descriptor]


@pytest.mark.asyncio
async def test_a_post_reset_link_drop_skip_with_a_missing_port_stays_held(replay):
    """03 I71 with a full check after every reset: the eth read's link-count-drop skip (252 of 256)
    reaches no verdict, the descriptor run finds one port missing and none extra, and the
    traffic-only rerun passes. The post-reset check fails, the device stays held, the eth mark
    stays at 256, and the next between-jobs gate fails the same way instead of lifting."""
    await _hold_then_reset(replay, resets=2)
    said = replay["log"].text()
    assert f"eth-heartbeat: SKIPPED — {ETH_LINK_DROP_SKIP}: {LEFT} of {HIGH_WATER}" in said, "the real skip verdict"
    assert _post_reset_descriptor_runs(replay) == [
        True,
        True,
    ], "the post-reset and post-sweep checks ran the descriptor"
    assert replay["calls"][-1] == ["run_cluster_validation"], "and its traffic-only rerun"
    assert "fabric: UNHEALTHY — fabric check FAILED" in said and fabric.LINKS_MISSING in said and _LOST_PORT in said
    assert srv.health_monitor.last_fabric_ok is False and srv.health_monitor.topology_lift_pending is False
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed post-reset check never releases"
    assert srv._device_unavailable_for_tenant(), "the hold is kept: no tenant reaches the mesh"
    assert srv.device_hold_episode_since, "the episode stays open for the next rung"
    assert "reset_ineffective" in replay["events"] and "host_rung_not_deferred" in replay["events"]
    assert _mark(replay) == HIGH_WATER, "the eth mark is never lowered to the links that are left"
    assert "eth_link_rebaselined" not in _mon_events(replay)

    # The next between-jobs gate: the reset still owes a passing fabric pass, so it runs the
    # descriptor again and fails on the same port rather than lifting.
    replay["log"].said.clear()
    await etr._gate()
    said = replay["log"].text()
    assert _post_reset_descriptor_runs(replay) == [True, True, True], "the gate ran the descriptor again"
    assert f"{ETH_LINK_DROP_SKIP}: {LEFT} of {HIGH_WATER}" in said and _LOST_PORT in said
    assert srv.fsm.state is not ServerState.HEALTHY and srv._device_unavailable_for_tenant()
    assert "device_released" not in replay["events"]
    assert _mark(replay) == HIGH_WATER and "eth_link_rebaselined" not in _mon_events(replay)


@pytest.mark.asyncio
@pytest.mark.parametrize("links_after", [LEFT, HIGH_WATER], ids=["eth-skip-stays", "all-links-back"])
async def test_the_same_sequence_with_no_missing_port_releases(replay, links_after):
    """Control: the reset brings the port back, so the post-reset descriptor run is clean and the
    full check releases the device, whether or not the eth read still skips on the link count. The
    mark stays at the high-water mark either way."""
    replay.update(heals=True, links_after=links_after)
    await _hold_then_reset(replay)
    assert _post_reset_descriptor_runs(replay) == [False], "a clean descriptor run after the reset"
    assert srv.health_monitor.last_fabric_ok is True
    assert srv.fsm.state is ServerState.HEALTHY, "a passing full post-reset check releases"
    assert not srv._device_unavailable_for_tenant()
    assert _mark(replay) == HIGH_WATER and "eth_link_rebaselined" not in _mon_events(replay)


async def _no_reset_hold_then_second_gate(st):
    """A crashed job's post-job gate runs the full check (no reset yet, so no fabric debt) and its
    fabric pass decides; then a second post-job gate inside FABRIC_CHECK_MIN_INTERVAL_SEC, whose eth
    read takes the link-count-drop skip again."""
    fsm_dirty(srv, "job 7 crashed: could not find any forwarding direction", why="job_killed")
    await etr._gate()
    assert st["resets"] == [] and not FABRIC_DEBT.owed, "no reset, so no fabric debt"
    first = len(st["fabric_out"])
    st["log"].said.clear()
    await etr._gate()
    said = st["log"].text()
    assert f"eth-heartbeat: SKIPPED — {ETH_LINK_DROP_SKIP}: {LEFT} of {HIGH_WATER}" in said
    return first, said


@pytest.mark.asyncio
async def test_a_failed_fabric_pass_is_never_cover_for_a_link_drop_skip(replay):
    """03 I30/I71: the link-drop skip's rate limit leans on a recent PASSING fabric pass only. A hold
    on a missing port with no reset yet failed its fabric pass; a second post-job gate inside the
    1200 s window must run the pass again (and fail on the same port) rather than release on the
    strength of the failed one."""
    first, said = await _no_reset_hold_then_second_gate(replay)
    assert "a recent passing fabric pass covers it" not in said, "a failed pass was taken as cover"
    assert len(replay["fabric_out"]) > first and replay["fabric_out"][first][1:] == (True, True)
    assert _LOST_PORT in said and srv.health_monitor.last_fabric_ok is False
    assert srv.fsm.state is not ServerState.HEALTHY and srv._device_unavailable_for_tenant()
    assert "device_released" not in replay["events"]


@pytest.mark.asyncio
async def test_a_passing_fabric_pass_still_covers_a_link_drop_skip(replay):
    """Control: with every port present the first gate's fabric pass is green and releases; the
    second gate inside the window takes that green pass as cover (no second pass) and releases."""
    replay["lost"] = False
    first, said = await _no_reset_hold_then_second_gate(replay)
    assert "a recent passing fabric pass covers it" in said
    assert len(replay["fabric_out"]) == first, "a fresh green pass is not repeated"
    assert srv.health_monitor.last_fabric_ok is True
    assert srv.fsm.state is ServerState.HEALTHY and not srv._device_unavailable_for_tenant()
