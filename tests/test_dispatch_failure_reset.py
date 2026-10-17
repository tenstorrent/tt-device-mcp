# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I55, 04 I28/I30: a dispatch failure on a green fabric over a present mesh earns one
present-mesh reset per hold episode, and only the full check after it releases the device."""

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tt_device_mcp import alert
from tt_device_mcp import device_holders as dh
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import fabric
from tt_device_mcp.health.recovery import base, pcie_guard

CHIPS = 32
DISPATCH_LINE = "dispatch FAILED on a green fabric (rc=1): Timed out while waiting for active ethernet core 27-25"
# The wrapper's output on the measured case: the descriptor run's mismatch, the marker for the rerun
# on the discovered graph, then the dispatch probe that failed over links that passed.
FABRIC_OUT = (
    "                 what(): Connection mismatch detected\n"
    "TOPOLOGY MISMATCH vs galaxy_8x4: 2 missing, 0 extra port connections — rerunning on the discovered graph\n"
    f"{DISPATCH_LINE}\n"
)
OUTAGE_HOLD = {
    "TT_DEVICE_MCP_AUTO_REBOOT": "0",
    "TT_DEVICE_MCP_AUTO_POWER_CYCLE": "0",
    "TT_DEVICE_MCP_AUTO_UBB_RESET": "0",
}


class _Logger:
    def __init__(self):
        self.said = []

    def info(self, msg):
        self.said.append(("info", msg))

    def warning(self, msg):
        self.said.append(("warning", msg))

    def error(self, msg):
        self.said.append(("error", msg))

    def text(self) -> str:
        return "\n".join(m for _, m in self.said)


@pytest.fixture
def mesh(monkeypatch, tmp_path, clear_job_state, galaxy_trays):
    """A 32-chip Galaxy, every chip on the bus, whose fabric pass moves traffic but cannot dispatch.
    ``bad`` keeps the dispatch failing; the reset records its argv and the verify records whether
    it ran the fabric pass."""
    st = {"bad": True, "resets": [], "verifies": [], "reboots": 0, "events": [], "alerts": []}
    for i in range(CHIPS):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")  # not the outage hold; the reboot rung opted in
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    monkeypatch.setattr(base, "_mesh_hold_paged", {})
    g = srv.galaxy_recovery
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: CHIPS)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", "")
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(CHIPS)})
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda action, *, tenant_active: (True, "ok"))
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_failed", False)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", srv.time.monotonic() - 100_000.0)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 0)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)

    async def verify(expected, log, run_fabric=True, **_):
        st["verifies"].append(run_fabric)
        if st["bad"]:
            srv.health_monitor.last_fabric_output = FABRIC_OUT
            srv.health_monitor.last_fabric_ok = False
            return False, {
                "snapshot": {"ok": True, "detail": "32/32"},
                "fabric": {"ok": False, "detail": DISPATCH_LINE},
            }
        srv.health_monitor.last_fabric_output = "all links healthy; dispatch OK"
        srv.health_monitor.last_fabric_ok = True
        return True, {"snapshot": {"ok": True, "detail": "32/32"}, "fabric": {"ok": True, "detail": "links healthy"}}

    async def no_scope(log):
        return False

    async def reset(argv, log, *a, **k):
        st["resets"].append(list(argv))
        st["bad"] = not st.get("reset_heals", True)
        return 0, ""

    async def reboot(log, reason):
        st["reboots"] += 1

    for r in (g, srv.per_target_recovery):
        monkeypatch.setattr(r, "_verify_device", verify)
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", no_scope)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    monkeypatch.setattr(g, "_auto_reboot_host", reboot)
    patch_health_event(monkeypatch, lambda name, *a, **k: st["events"].append(name))
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: st["alerts"].append(event) or object())
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    log = _Logger()
    monkeypatch.setattr(srv, "logger", log)
    st["log"] = log
    fsm_dirty(srv, "job failed: Timed out while waiting for active ethernet core 27-25")
    return st


async def _gate():
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)


@pytest.mark.asyncio
async def test_dispatch_failure_on_a_present_mesh_resets_once_then_runs_the_full_check(mesh):
    """Base held this mesh for a self-heal that never comes (a 100-min eth_frozen hold measured):
    below the galaxy-reset floor with every chip present, no rung fired. Now one glx_reset fires,
    the post-reset check runs the fabric pass, and only its pass releases the device."""
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]], "one present-mesh reset"
    assert "dispatch_failure_mesh_reset" in mesh["events"]
    assert mesh["verifies"][-1] is True, "the post-reset check runs the fabric pass (dispatch included)"
    assert srv.fsm.state is ServerState.HEALTHY, "a passing full check releases the device"
    # The next admission ends the hold episode (device_released); the next episode earns its own reset.
    srv._note_tenant_gate_verdict("")
    assert srv._dispatch_reset_spent is False, "a released episode re-arms the next one's reset"


@pytest.mark.asyncio
async def test_a_second_dispatch_failure_in_the_episode_holds_instead_of_resetting_again(mesh):
    mesh["reset_heals"] = False
    await _gate()
    assert len(mesh["resets"]) == 1 and srv._dispatch_reset_spent is True
    # The cooldown elapsed; the same hold episode fails dispatch again.
    srv.recovery_mechanism.last_reset_monotonic = srv.time.monotonic() - 100_000.0
    srv.recovery_mechanism.last_reset_failed = False
    await _gate()
    assert len(mesh["resets"]) == 1, "a second dispatch failure in the episode never resets again"
    assert srv.fsm.state is ServerState.RECOVERING, "it holds"
    assert "this episode's present-mesh reset already ran" in srv.fsm.record.detail


@pytest.mark.asyncio
async def test_a_failed_post_reset_check_stays_held_and_climbs(mesh):
    """A prior cycle already failed (was_failing): the dispatch reset fires, its post-reset check
    fails, the device stays held and the ladder climbs to the opted-in host rung."""
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    await _gate()
    # The dispatch reset, then the ladder's own last-chance sweep before the host rung (04 I22).
    assert mesh["events"].count("dispatch_failure_mesh_reset") == 1
    assert "last_chance_reset_sweep" in mesh["events"]
    assert mesh["resets"][0] == ["tt-smi", "-glx_reset"]
    assert mesh["verifies"][-1] is True, "the post-reset check ran the fabric pass"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed post-reset check never releases"
    assert mesh["reboots"] == 1, "it climbs to the opted-in host rung"


@pytest.mark.asyncio
async def test_outage_hold_holds_and_pages_without_a_reset(monkeypatch, mesh):
    for var, value in OUTAGE_HOLD.items():
        monkeypatch.setenv(var, value)
    await _gate()
    assert mesh["resets"] == [] and mesh["reboots"] == 0, "the outage hold forbids the reset"
    assert srv.fsm.state is ServerState.RECOVERING
    assert "mesh_reset_held" in mesh["events"] and "dispatch_failure_mesh_reset" not in mesh["events"]
    assert [a["kind"] for a in mesh["alerts"]] == ["mesh_reset_held"]
    said = mesh["log"].text()
    assert "present-mesh reset is HELD" in said and "present-mesh reset is HELD" in srv.fsm.record.detail
    assert "escalates" not in said, "no escalation is promised under the hold"
    assert srv._dispatch_reset_spent is False, "a refused reset is not spent"


def test_first_reason_names_the_dispatch_failure_over_a_downgraded_mismatch():
    assert fabric.first_reason(FABRIC_OUT) == DISPATCH_LINE
    # Without the marker the descriptor run's line is still read.
    assert fabric.first_reason("what(): Connection mismatch detected\n") == "what(): Connection mismatch detected"


def test_a_dispatch_failure_hold_is_not_a_topology_mismatch(monkeypatch):
    assert srv._names_topology_mismatch(f"gate/post-job: fabric failed: {DISPATCH_LINE}") is False
    assert srv._names_topology_mismatch("what(): Connection mismatch detected") is True
    assert srv._validator_line_names_topology(FABRIC_OUT) is False
    assert srv._validator_line_names_topology("Connection mismatch detected") is True
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", FABRIC_OUT)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", False)
    fsm_dirty(srv, "gate/post-job: enum+ARC healthy, fabric unverified", why=srv.FABRIC_RELIFT_WHY)
    monkeypatch.setattr(srv, "device_hold_episode_reason", f"fabric failed: {DISPATCH_LINE}")
    assert srv._fabric_no_reset_hold() != "topology mismatch"


def test_rung_gap_boot_warning_and_health_field(monkeypatch):
    log = _Logger()
    monkeypatch.setattr(srv, "logger", log)
    for var in ("TT_DEVICE_MCP_SELFHEAL_RELIFT", "TT_DEVICE_MCP_FORCE_ESCALATE", "TT_DEVICE_MCP_AUTO_MESH_RESET"):
        monkeypatch.delenv(var, raising=False)
    for var in OUTAGE_HOLD:
        monkeypatch.delenv(var, raising=False)
    srv.log_rung_inventory()
    assert not any("RUNG GAP" in m for _, m in log.said)
    assert srv._health_payload()["hold_exit_gap"] is None

    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    log.said.clear()
    srv.log_rung_inventory()
    gaps = [m for lvl, m in log.said if lvl == "warning" and "RUNG GAP" in m]
    assert len(gaps) == 1 and "TT_DEVICE_MCP_FORCE_ESCALATE=0" in gaps[0]
    assert "TT_DEVICE_MCP_SELFHEAL_RELIFT=0" in srv._health_payload()["hold_exit_gap"]

    monkeypatch.delenv("TT_DEVICE_MCP_SELFHEAL_RELIFT")
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "0")
    assert "present-mesh reset held: TT_DEVICE_MCP_AUTO_MESH_RESET=0" in srv._health_payload()["hold_exit_gap"]


def test_floor_message_promises_escalation_only_when_armed(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", raising=False)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(srv, "isolated_chips", set())
    assert "~2 min" not in srv._hold_escalation_note(off_bus=1), "no 2-min rung for a drop with no isolated chip"
    assert "climbs at the 10-min hold ceiling" in srv._hold_escalation_note(off_bus=1)
    assert "escalates at the 10-min hold ceiling" in srv._hold_escalation_note()
    monkeypatch.setattr(srv, "isolated_chips", {7})
    assert "escalates in ~2 min (isolated chips)" in srv._hold_escalation_note(off_bus=1)
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    for off_bus in (0, 1):
        note = srv._hold_escalation_note(off_bus)
        assert "escalates" not in note and "only the idle self-heal relift" in note
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "0")
    note = srv._hold_escalation_note(1)
    assert "escalates" not in note and "NO automatic exit" in note


@pytest.mark.asyncio
async def test_a_uid0_telemetry_holder_does_not_block_the_reset(monkeypatch, mesh):
    """tt_telemetry_server runs as uid 0 in system.slice and holds the device for good: it is
    infrastructure, so it never blocks the reset a dispatch failure earns."""
    poller = DeviceHolder(pid=4242, uid=0)
    assert dh.is_tenant(poller) is False
    scan = HolderScan(holders=[poller], complete=True)
    assert srv.galaxy_recovery._tenant_active(scan) is False
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: scan)
    monkeypatch.setattr(dh, "enumerate_device_holders", lambda: scan)
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]]
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_frozen_chip_outage_hold_does_not_claim_a_reset_ran(monkeypatch, mesh):
    """An ARC-frozen chip under the outage hold: no reset ran, so the hold must not say one did."""
    for var, value in OUTAGE_HOLD.items():
        monkeypatch.setenv(var, value)
    tick = {"n": 0}

    def beats():
        tick["n"] += 1
        return {str(i): (100 if i == 5 else 100 + tick["n"]) for i in range(CHIPS)}

    monkeypatch.setattr(srv, "read_heartbeats", beats)
    await _gate()
    assert mesh["resets"] == [] and srv._dispatch_reset_spent is False
    assert srv.fsm.state is ServerState.RECOVERING
    assert "already ran" not in srv.fsm.record.detail


@pytest.mark.asyncio
async def test_gate_refused_reset_leaves_the_latch_unspent(monkeypatch, mesh):
    """The per-host gate refuses the reset: nothing started, so the episode's reset is not used up
    and the device stays held (never released without a passing check)."""
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (False, "gate says no"))
    await _gate()
    assert mesh["resets"] == []
    assert srv._dispatch_reset_spent is False, "a gate-refused reset is not spent"
    assert srv.fsm.state is ServerState.RECOVERING
    assert "already ran" not in srv.fsm.record.detail


@pytest.mark.asyncio
async def test_a_clean_exit_dispatch_failure_whose_reset_fails_stays_held(mesh):
    """The same reset from a clean exit (no episode open yet): the gate opens one before it fires,
    so a failed post-reset check holds the door instead of leaving the FSM HEALTHY."""
    srv.fsm.on_readings(srv._healthy_reading())
    assert srv.fsm.state is ServerState.HEALTHY
    mesh["reset_heals"] = False
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]]
    assert mesh["verifies"][-1] is True, "the post-reset check ran the fabric pass"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed post-reset check never releases"
    assert srv._device_unavailable_for_tenant()
