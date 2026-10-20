# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A cabling-descriptor topology mismatch is never a reset: the built-in path reruns traffic-only,
an OK names the mismatch, and the lift is bounded by the next job (03 I42). No device is opened:
the validator, the chip snapshot and the alert hook are stand-ins."""

import json

import pytest

from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import HealthMonitor, moved_trays
from tt_device_mcp.health.monitors import fabric

# A moved tray renames its links: as many extra port connections as missing ones (I42).
_MISMATCH = (
    "Physical Discovery found 2 missing port/cable connections:\n"
    "  PhysicalPortEndpoint{tray_id=1}\n"
    "  PhysicalPortEndpoint{tray_id=2}\n"
    "Physical Discovery found 2 extra port/cable connections:\n"
    "  PhysicalPortEndpoint{tray_id=3}\n"
    "  PhysicalPortEndpoint{tray_id=4}\n"
    "  what():  Connection mismatch detected. Check the log above for details.\n"
)
# One QSFP-DD cable's port connection gone, nothing extra: links are lost, not renamed (I71).
_LOST = (
    "Physical Discovery found 1 missing port/cable connections:\n"
    "  - PhysicalPortEndpoint{tray_id=3, asic_location=7, port_type=QSFP_DD, port_id=2}"
    " <-> PhysicalPortEndpoint{tray_id=4, asic_location=6, port_type=QSFP_DD, port_id=2}\n"
    "\n"
    "  what():  Connection mismatch detected. Check the log above for details.\n"
)
_LOST_PORT = "tray 3 asic 7 QSFP_DD 2 <-> tray 4 asic 6 QSFP_DD 2"
_DESC = "/x/desc.textproto"


def test_moved_trays_names_only_chips_whose_tray_changed():
    before = {"a": 0x01, "b": 0x41, "c": 0x81, "e": 0x02}
    now = {"a": 0xC1, "b": 0x41, "c": 0x81, "d": 0x05, "e": 0xC2}
    assert moved_trays(before, now) == ["tray 1 -> tray 3 (2 chips)"]
    assert moved_trays({}, now) == []


@pytest.fixture
def topo(monkeypatch, health_deps, tmp_path):
    """A HealthMonitor on the built-in path whose validator mismatches with the descriptor and
    prints ``state['rerun']`` without it."""
    m = HealthMonitor(health_deps)
    state = {"first": _MISMATCH, "rerun": (0, "all links ok\n"), "calls": [], "alerts": [], "events": []}

    def build_command(descriptor=True):
        argv = ["run_cluster_validation"] + (["--cabling-descriptor-path", _DESC] if descriptor else [])
        return argv, {}

    async def check(argv, env, **kw):
        state["calls"].append(argv)
        return (134, state["first"]) if "--cabling-descriptor-path" in argv else state["rerun"]

    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "")
    monkeypatch.setattr(m, "_begin_action_row", lambda *a, **k: None)
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(fabric, "build_command", build_command)
    monkeypatch.setattr(fabric, "check", check)
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", _DESC))
    monkeypatch.setattr(monitor_mod, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {"a": 0xC1})
    monkeypatch.setattr(monitor_mod, "health_event", lambda ev, **f: state["events"].append((ev, f)))
    monkeypatch.setattr(monitor_mod.alert, "send_alert", lambda ev, log=None: state["alerts"].append(ev))
    (tmp_path / monitor_mod.ASIC_BUS_FILE).write_text('{"a": 1}')
    return m, state


@pytest.mark.asyncio
async def test_a_mismatch_reruns_traffic_only_and_an_ok_names_it_and_pages_once(topo):
    m, state = topo
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is True
    assert "TOPOLOGY MISMATCH vs /x/desc.textproto: 2 missing, 2 extra" in detail
    assert [("--cabling-descriptor-path" in c) for c in state["calls"]] == [True, False]
    assert m.topology_lift_pending is True
    assert state["events"][0][0] == "fabric_topology_mismatch"
    assert state["events"][0][1]["moved_trays"] == ["tray 1 -> tray 3 (1 chips)"]
    await m._verify_fabric_health_body(60)
    assert len(state["alerts"]) == 1


@pytest.mark.asyncio
async def test_a_mismatch_whose_rerun_measures_bad_links_is_unhealthy(topo):
    m, state = topo
    state["rerun"] = (1, "TT_THROW: Encountered unhealthy ethernet connections, listed above\n")
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is False
    assert m.topology_lift_pending is False


@pytest.mark.asyncio
async def test_a_mismatch_whose_rerun_cannot_measure_stays_a_topology_skip(topo):
    m, state = topo
    state["rerun"] = (134, "  what():  Hugepages are not allocated for channel: 0\n")
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is None
    assert f"{fabric.TOPOLOGY_MISMATCH}; traffic-only rerun: " in detail


@pytest.mark.asyncio
async def test_the_lift_is_bounded_by_the_first_job_only(topo):
    m, state = topo
    await m._verify_fabric_health_body(60)
    assert m.note_job_after_topology_lift(False, "") is None
    # The second job is outside the bound, however it ended.
    assert m.note_job_after_topology_lift(True, "fabric router sync: timeout") is None
    assert m.topology_hw_fault == ""


@pytest.mark.asyncio
async def test_a_fabric_failure_on_the_first_job_fails_the_next_mismatch_and_never_withholds_the_check(topo):
    """I42: the latch never withholds the check (that hold could not reset or escalate). It reads the
    fabric bad, drops the cached mismatch and turns the next mismatch into a FAILED check, so the
    ladder runs. It pages nobody: the operator is the ladder's end, after a power cycle."""
    m, state = topo
    await m._verify_fabric_health_body(60)
    alerts = len(state["alerts"])
    why = m.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert why and "hardware fault: the first job after a topology-mismatch lift" in why
    assert m.topology_hw_fault == why and m.last_fabric_ok is False
    assert len(state["alerts"]) == alerts
    n = len(state["calls"])
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is False and fabric.names_links_missing(detail) and "TOPOLOGY MISMATCH" in detail
    assert "--cabling-descriptor-path" in state["calls"][n], "the descriptor runs again, not the cache"
    assert m.topology_lift_pending is False and m.topology_hw_fault == why


@pytest.mark.asyncio
async def test_a_clean_pass_clears_the_first_job_latch(topo):
    """Only a clean pass on the full topology ends the I42 latch (an operator reset keeps it:
    tests/test_fabric_links_missing.py::test_an_operator_reset_keeps_the_latch_and_only_a_clean_fabric_pass_ends_it)."""
    m, state = topo
    await m._verify_fabric_health_body(60)
    m.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    m.clear_topology_hw_fault("a clean fabric pass")
    assert m.topology_hw_fault == ""
    assert state["events"][-1][0] == "fabric_topology_hw_fault_cleared"
    m.topology_hw_fault = "hardware fault: x"
    assert m.adopt_fabric_pass("fabric links healthy") == ""
    assert m.topology_hw_fault == ""


@pytest.mark.asyncio
async def test_a_first_job_that_fails_off_the_fabric_is_not_a_topology_fault(topo):
    m, _ = topo
    await m._verify_fabric_health_body(60)
    assert m.note_job_after_topology_lift(True, "ValueError: shapes do not match") is None
    assert m.topology_hw_fault == ""


# ---- I45: a lift re-baselines the eth link mark per chip-to-bus map; the mismatch is cached ----


def _baseline(tmp_path) -> dict:
    return json.loads((tmp_path / HealthMonitor.ETH_LINK_BASELINE_FILE).read_text())


@pytest.fixture
def accepted(topo, monkeypatch):
    """The descriptor run loses one port connection the operator accepted as dead (I71)."""
    topo[1]["first"] = _LOST
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", _LOST_PORT)
    return topo


@pytest.mark.asyncio
async def test_a_lift_rebaselines_the_eth_mark_at_the_discovered_links(accepted, tmp_path):
    m, _ = accepted
    m.eth_link_drop(256)  # the pre-lift high-water mark
    assert "252 of 256" in m.eth_link_drop(252)
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is True
    # The first read after the lift sets the mark at the links it found, keyed to the bus map.
    assert m.eth_link_drop(252) == ""
    assert _baseline(tmp_path) == {"links": 252, "high_water": 256, "asic_bus": {"a": 0xC1}, "accepted": True}
    assert m.eth_link_drop(252) == ""
    # Still a high-water rule: a link lost after the re-baseline is a drop.
    assert "250 of 252" in m.eth_link_drop(250)
    # A second lift on the same map keeps that mark rather than re-baselining onto the loss.
    await m._verify_fabric_health_body(60)
    assert "250 of 252" in m.eth_link_drop(250)


@pytest.mark.asyncio
async def test_trays_moving_again_drop_the_rebaseline_back_to_the_high_water_mark(accepted, tmp_path, monkeypatch):
    m, _ = accepted
    m.eth_link_drop(256)
    await m._verify_fabric_health_body(60)
    assert m.eth_link_drop(252) == ""
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {"a": 0x01})
    assert "252 of 256" in m.eth_link_drop(252)
    assert _baseline(tmp_path) == {"links": 256}


@pytest.mark.asyncio
async def test_a_clean_pass_without_the_marker_restores_the_high_water_rule(accepted, tmp_path, monkeypatch):
    m, state = accepted
    m.eth_link_drop(256)
    await m._verify_fabric_health_body(60)
    assert m.eth_link_drop(252) == ""
    assert (tmp_path / monitor_mod.TOPOLOGY_CACHE_FILE).exists()

    async def clean(argv, env, **kw):
        state["calls"].append(argv)
        return 0, "all links ok\n"

    monkeypatch.setattr(fabric, "check", clean)
    # The cached mismatch is forgotten once the link count moves, so the descriptor runs again.
    assert m.eth_link_drop(256) == ""
    assert not (tmp_path / monitor_mod.TOPOLOGY_CACHE_FILE).exists()
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is True and "TOPOLOGY" not in detail
    assert "--cabling-descriptor-path" in state["calls"][-1]
    assert _baseline(tmp_path) == {"links": 256}
    assert "252 of 256" in m.eth_link_drop(252)


@pytest.mark.asyncio
async def test_a_cached_mismatch_skips_the_descriptor_run_on_the_same_bus_map_only(topo, monkeypatch):
    m, state = topo
    state["rerun"] = (134, "  what():  Hugepages are not allocated for channel: 0\n")
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is None
    # Cached even when its rerun could not measure: later gates skip the doomed descriptor run.
    state["rerun"] = (0, "all links ok\n")
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is True and "TOPOLOGY MISMATCH vs /x/desc.textproto: 2 missing, 2 extra" in detail
    assert [("--cabling-descriptor-path" in c) for c in state["calls"]] == [True, False, False]
    assert m.topology_lift_pending is True
    # Another bus map, or another descriptor, runs the descriptor again.
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {"a": 0x41})
    await m._verify_fabric_health_body(60)
    assert [("--cabling-descriptor-path" in c) for c in state["calls"][3:]] == [True, False]
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", "/x/other.textproto"))
    await m._verify_fabric_health_body(60)
    assert "--cabling-descriptor-path" in state["calls"][5]


@pytest.mark.asyncio
async def test_a_rerun_that_cannot_measure_never_lowers_the_mark_for_the_bounded_fallback(topo, tmp_path):
    m, state = topo
    m.eth_link_drop(256)
    state["rerun"] = (134, "  what():  Hugepages are not allocated for channel: 0\n")
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is None and m.topology_lift_pending is False
    # The fallback's eth read is keyed to this mesh, but never re-baselined down to the links that
    # are left (I71): a lost link still reads as a drop, so the fallback cannot release on it.
    assert m.eth_link_drop(256) == ""
    assert "252 of 256" in m.eth_link_drop(252)
    assert _baseline(tmp_path)["links"] == 256
