# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A cabling-descriptor topology mismatch is never a reset: the built-in path reruns traffic-only,
an OK names the mismatch, and the lift is bounded by the next job (03 I40). No device is opened:
the validator, the chip snapshot and the alert hook are stand-ins."""

import pytest

from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import HealthMonitor, moved_trays
from tt_device_mcp.health.monitors import fabric

_MISMATCH = (
    "Physical Discovery found 2 missing port/cable connections:\n"
    "  PhysicalPortEndpoint{tray_id=1}\n"
    "  PhysicalPortEndpoint{tray_id=2}\n"
    "Physical Discovery found 1 extra port/cable connections:\n"
    "  PhysicalPortEndpoint{tray_id=3}\n"
    "  what():  Connection mismatch detected. Check the log above for details.\n"
)
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
    state = {"rerun": (0, "all links ok\n"), "calls": [], "alerts": [], "events": []}

    def build_command(descriptor=True):
        argv = ["run_cluster_validation"] + (["--cabling-descriptor-path", _DESC] if descriptor else [])
        return argv, {}

    async def check(argv, env, **kw):
        state["calls"].append(argv)
        return (134, _MISMATCH) if "--cabling-descriptor-path" in argv else state["rerun"]

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
    assert "TOPOLOGY MISMATCH vs /x/desc.textproto: 2 missing, 1 extra" in detail
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
async def test_a_fabric_failure_on_the_first_job_holds_the_check_without_a_reset(topo):
    m, state = topo
    await m._verify_fabric_health_body(60)
    why = m.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert why and "hardware fault: the first job after a topology-mismatch lift" in why
    assert m.topology_hw_fault == why
    assert state["alerts"][-1]["kind"] == "fabric_topology_hw_fault"
    n = len(state["calls"])
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is None and why in detail
    assert len(state["calls"]) == n


@pytest.mark.asyncio
async def test_a_first_job_that_fails_off_the_fabric_is_not_a_topology_fault(topo):
    m, _ = topo
    await m._verify_fabric_health_body(60)
    assert m.note_job_after_topology_lift(True, "ValueError: shapes do not match") is None
    assert m.topology_hw_fault == ""
