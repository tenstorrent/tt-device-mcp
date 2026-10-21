# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Cabling-descriptor links missing with none extra are a missing cable: a hardware verdict, held as a
known fabric fault and paged once, never a 77 the broker resets and climbs the ladder for (03 I47).
No device is opened: the validator, the alert hook and the wrapper's validator are stand-ins."""

import subprocess
from pathlib import Path

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC, FABRIC_CHECK_HARDWARE_FAULT_RC
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import fabric

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-fabric-check.sh"
_DESC = "/x/desc.textproto"

# The validator's output for one QSFP-DD cable gone between two trays: its two channels and its one
# port connection are missing, and nothing extra was discovered.
_CABLE = (
    "Physical Discovery found 2 missing channel connections:\n"
    "  - PhysicalChannelEndpoint{tray_id=1, asic_channel=AsicChannel{asic_location=5, channel_id=2}}"
    " <-> PhysicalChannelEndpoint{tray_id=2, asic_channel=AsicChannel{asic_location=5, channel_id=2}}\n"
    "  - PhysicalChannelEndpoint{tray_id=1, asic_channel=AsicChannel{asic_location=5, channel_id=3}}"
    " <-> PhysicalChannelEndpoint{tray_id=2, asic_channel=AsicChannel{asic_location=5, channel_id=3}}\n"
    "\n"
    "Physical Discovery found 1 missing port/cable connections:\n"
    "  - PhysicalPortEndpoint{tray_id=1, port_type=QSFP_DD, port_id=1}"
    " <-> PhysicalPortEndpoint{tray_id=2, port_type=QSFP_DD, port_id=1}\n"
    "\n"
    "terminate called after throwing an instance of 'std::runtime_error'\n"
    "  what():  Connection mismatch detected. Check console output for details.\n"
)
# A moved tray renames its links: missing ones and extra ones together.
_MOVED = (
    "Physical Discovery found 1 missing port/cable connections:\n"
    "  - PhysicalPortEndpoint{tray_id=1, port_type=QSFP_DD, port_id=1}\n"
    "Physical Discovery found 1 extra port/cable connections:\n"
    "  - PhysicalPortEndpoint{tray_id=3, port_type=QSFP_DD, port_id=1}\n"
    "  what():  Connection mismatch detected. Check console output for details.\n"
)
_NO_VERDICT = (134, "  what():  Hugepages are not allocated for channel: 0\n")


def test_missing_cables_counts_only_missing_links_with_none_extra():
    assert fabric.missing_cables(fabric.topology_marker(_DESC, 1, 0)) == 1
    assert fabric.missing_cables(fabric.topology_marker(_DESC, 2, 1)) == 0
    assert fabric.missing_cables(fabric.topology_marker(_DESC, 0, 2)) == 0
    assert fabric.missing_cables("") == 0


@pytest.fixture
def cable(monkeypatch, health_deps, tmp_path):
    """A HealthMonitor on the built-in path: the descriptor run prints ``state['first']`` (exit 134),
    the traffic-only rerun answers ``state['rerun']``; ``state['first_rc']`` overrides that exit."""
    m = HealthMonitor(health_deps)
    state = {"first": _CABLE, "rerun": _NO_VERDICT, "alerts": [], "events": []}

    def build_command(descriptor=True):
        return ["run_cluster_validation"] + (["--cabling-descriptor-path", _DESC] if descriptor else []), {}

    async def check(argv, env, **kw):
        return (state.get("first_rc", 134), state["first"]) if "--cabling-descriptor-path" in argv else state["rerun"]

    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "")
    monkeypatch.setattr(m, "_begin_action_row", lambda *a, **k: None)
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: state.setdefault("status", a[3]))
    monkeypatch.setattr(fabric, "build_command", build_command)
    monkeypatch.setattr(fabric, "check", check)
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", _DESC))
    monkeypatch.setattr(monitor_mod, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {"a": 0xC1})
    monkeypatch.setattr(monitor_mod, "_save_asic_bus_map", lambda: None)
    monkeypatch.setattr(monitor_mod, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(monitor_mod, "health_event", lambda ev, **f: state["events"].append((ev, f)))
    monkeypatch.setattr(monitor_mod.alert, "send_alert", lambda ev, log=None: state["alerts"].append(ev))
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_KNOWN_FAULT_HOLD_SEC", raising=False)
    return m, state


@pytest.mark.asyncio
async def test_a_missing_cable_is_a_hardware_verdict_held_and_paged_once(cable):
    """The field case: one cable gone, the traffic-only rerun reaches no verdict. Without the fix this
    is a 77 ("topology mismatch; traffic-only rerun: ...") and the broker resets and climbs."""
    m, state = cable
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is None
    assert detail.startswith("fabric HARDWARE FAULT")
    assert "COULD NOT RUN" not in detail
    assert f"1 port/cable connection(s) of {_DESC} missing and none extra" in detail
    assert m.known_fabric_fault().startswith(f"{fabric.CABLE_FAULT}: 1 port/cable")
    assert state["status"] == "hardware_fault"
    assert [ev for ev, _ in state["events"]] == ["fabric_known_fault"]
    assert state["events"][0][1]["cause"] == "missing_cable"
    assert [(a["kind"], a["cause"]) for a in state["alerts"]] == [("fabric_known_fault", "missing_cable")]
    # The idle relift re-runs the pass (the cached mismatch skips the descriptor run): no second
    # page and no second event while the same fault is open.
    await m._verify_fabric_health_body(60)
    assert len(state["alerts"]) == 1 and len(state["events"]) == 1


@pytest.mark.asyncio
async def test_a_missing_cable_paged_once_even_after_its_hold_expires(cable, monkeypatch):
    m, state = cable
    await m._verify_fabric_health_body(60)
    monkeypatch.setattr(m, "_fabric_known_fault_at", m._fabric_known_fault_at - 10**6)
    assert m.known_fabric_fault() == ""
    await m._verify_fabric_health_body(60)
    assert m.known_fabric_fault().startswith(fabric.CABLE_FAULT)
    assert len(state["alerts"]) == 1


@pytest.mark.asyncio
async def test_a_missing_cable_whose_rerun_passes_fails_the_check_by_port_name(cable):
    """03 I71: traffic passing on the links that are left does not make a lost cable healthy; a job
    routing over the whole mesh fails. The check FAILS (a measured fault, reset path), never lifts."""
    m, state = cable
    state["rerun"] = (0, "all links ok\n")
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is False and "fabric check FAILED" in detail
    assert "1 port connection(s) of the cabling descriptor missing, 0 extra" in detail
    assert "tray 1 QSFP_DD 1 <-> tray 2 QSFP_DD 1" in detail
    assert m.known_fabric_fault() == ""
    assert m.topology_lift_pending is False


@pytest.mark.asyncio
async def test_a_missing_cable_whose_rerun_measures_bad_links_stays_unhealthy(cable):
    m, state = cable
    state["rerun"] = (1, "TT_THROW: Encountered unhealthy ethernet connections, listed above\n")
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is False
    assert m.known_fabric_fault() == ""


@pytest.mark.asyncio
async def test_a_moved_tray_with_no_rerun_verdict_stays_a_topology_skip(cable):
    m, state = cable
    state["first"] = _MOVED
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is None
    assert f"{fabric.TOPOLOGY_MISMATCH}; traffic-only rerun: " in detail
    assert m.known_fabric_fault() == ""
    assert state["alerts"] == []


@pytest.mark.asyncio
async def test_a_healthy_pass_ends_the_missing_cable_fault(cable):
    m, state = cable
    await m._verify_fabric_health_body(60)
    state["first"], state["first_rc"] = "all links ok\n", 0
    monitor_mod._forget_topology_mismatch()
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is True
    assert m.known_fabric_fault() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rc,known",
    [(FABRIC_CHECK_HARDWARE_FAULT_RC, True), (FABRIC_CHECK_CANNOT_CHECK_RC, False)],
)
async def test_an_override_wrapper_exit_78_is_the_hardware_verdict(cable, monkeypatch, rc, known):
    """An operator's wrapper reports by exit code only: 78 is the hardware verdict, 77 stays a skip,
    and neither is the measured-bad verdict that resets."""
    m, state = cable
    line = "fabric-check: HARDWARE FAULT: 1 port/cable connection(s) of d missing and none extra"

    async def check(argv, env, **kw):
        return rc, f"  what():  Connection mismatch detected.\n{line}\n"

    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "/opt/fabric-check.sh")
    monkeypatch.setattr(fabric, "check", check)
    ok, detail = await m._verify_fabric_health_body(60)
    assert ok is None
    assert bool(m.known_fabric_fault()) is known
    if known:
        assert detail.startswith("fabric HARDWARE FAULT") and line.split(": ", 1)[1] in detail
        assert len(state["alerts"]) == 1


@pytest.mark.asyncio
async def test_the_broker_holds_a_missing_cable_outside_the_77_bound_without_a_reset(cable, monkeypatch):
    """The verdict reaches the hold logic: past the 77 bound no fallback release or ladder climb, and
    the deadline escalation holds a fabric-unverified hold without a reset (03 I40, 04 I30)."""
    m, _ = cable
    await m._verify_fabric_health_body(60)
    monkeypatch.setattr(srv, "health_monitor", m)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="gate: fabric unverified", dirty=False)
    assert srv._known_fabric_fault_hold() is True
    assert srv._fabric_no_reset_hold() == "known fabric fault"


def _run_wrapper(tmp_path, first: str, rerun_output: str, rerun_rc: int):
    """The wrapper with a descriptor set: the stand-in validator prints ``first`` (exit 134) when
    handed the descriptor, and ``rerun_output`` (exit ``rerun_rc``) without it."""
    root = tmp_path / "root"
    root.mkdir()
    desc = tmp_path / "desc.textproto"
    desc.write_text("# cabling\n")
    (tmp_path / "first.txt").write_text(first)
    (tmp_path / "rerun.txt").write_text(rerun_output)
    fake = tmp_path / "run_cluster_validation"
    fake.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *cabling*) cat '{tmp_path / 'first.txt'}'; exit 134 ;; esac\n"
        f"cat '{tmp_path / 'rerun.txt'}'\nexit {rerun_rc}\n"
    )
    fake.chmod(0o755)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TTDEV_FABRIC_BIN": str(fake),
        "TTDEV_FABRIC_RUNTIME_ROOT": str(root),
        "TTDEV_FABRIC_CACHE": str(tmp_path / "cache"),
        "TTDEV_FABRIC_DESCRIPTOR": str(desc),
        "TTDEV_DISPATCH_BIN": str(tmp_path / "absent"),
        "TTDEV_PCI_DEVICES_DIR": str(tmp_path / "no-pci"),
    }
    r = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    return r, desc


def test_wrapper_exits_78_on_a_missing_cable_whose_rerun_cannot_measure(tmp_path):
    r, desc = _run_wrapper(tmp_path, _CABLE, *reversed(_NO_VERDICT))
    assert r.returncode == FABRIC_CHECK_HARDWARE_FAULT_RC, r.stderr
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert f"HARDWARE FAULT: 1 port/cable connection(s) of {desc} missing and none extra" in last
    assert "Hugepages are not allocated" in last


def test_wrapper_keeps_77_for_a_moved_tray_whose_rerun_cannot_measure(tmp_path):
    r, _ = _run_wrapper(tmp_path, _MOVED, *reversed(_NO_VERDICT))
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr


def test_wrapper_lifts_a_missing_cable_whose_rerun_passes(tmp_path):
    r, desc = _run_wrapper(tmp_path, _CABLE, "all links ok\n", 0)
    assert r.returncode == 0, r.stderr
    assert f"TOPOLOGY MISMATCH vs {desc}: 1 missing, 0 extra" in r.stderr


@pytest.mark.parametrize("rc", [FABRIC_CHECK_CANNOT_CHECK_RC, FABRIC_CHECK_HARDWARE_FAULT_RC])
def test_wrapper_measured_bad_links_never_exit_on_a_sentinel(tmp_path, rc):
    """A validator that measured bad links and happened to exit 77 or 78 is UNHEALTHY (exit 1)."""
    r, _ = _run_wrapper(tmp_path, _CABLE, "TT_THROW: Encountered unhealthy ethernet connections\n", rc)
    assert r.returncode == 1, r.stderr
