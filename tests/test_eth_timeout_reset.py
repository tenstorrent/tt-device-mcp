# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I56: a clean post-job eth read that timed out with no verdict, on a present mesh, skips the
pre-reset fabric pass and takes the one present-mesh reset of the hold episode (shared with I55's
dispatch reset). Only the full check after it releases the device. Every other shape keeps the pass.

The real probe pass runs here (HealthMonitor.update); only its leaf probes are stubbed."""

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tt_device_mcp import alert
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitor import ETH_COULD_NOT_CHECK, ETH_LINK_DROP_SKIP
from tt_device_mcp.health.monitors import eth, hostpci
from tt_device_mcp.health.recovery import base, pcie_guard

CHIPS = 32
TIMED_OUT = (None, f"{ETH_COULD_NOT_CHECK}: {eth.READ_TIMED_OUT}")
ADVANCING = (True, "all active-eth cores advancing (1s)")
OUTAGE_HOLD = {
    "TT_DEVICE_MCP_AUTO_REBOOT": "0",
    "TT_DEVICE_MCP_AUTO_POWER_CYCLE": "0",
    "TT_DEVICE_MCP_AUTO_UBB_RESET": "0",
}


class _Logger:
    def __init__(self):
        self.said = []

    def info(self, msg):
        self.said.append(msg)

    warning = error = info

    def text(self) -> str:
        return "\n".join(self.said)


@pytest.fixture
def mesh(monkeypatch, tmp_path, clear_job_state, galaxy_trays):
    """A 32-chip Galaxy, every chip on the bus and ARC healthy, after a clean job exit whose eth read
    times out. ``fabric`` records how many resets had run when each fabric pass ran; a reset makes the
    eth read advance and, unless ``reset_heals`` is False, the fabric pass green."""
    st = {
        "eth": TIMED_OUT,
        "fabric_bad": False,
        "fabric_77": False,
        "snapshot": (True, "32/32 chips, ARC ok"),
        "hostpci": (True, "all chips bound"),
        "resets": [],
        "fabric": [],
        "eth_reads": 0,
        "reboots": 0,
        "events": [],
    }
    for i in range(CHIPS):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.setattr(base, "_mesh_hold_paged", {})
    monkeypatch.setattr(srv, "eth_check_armed", True)
    monkeypatch.setattr(srv, "last_fabric_check_monotonic", srv.time.monotonic())
    mon = srv.health_monitor
    monkeypatch.setattr(mon, "expected", lambda present: CHIPS)
    monkeypatch.setattr(mon, "last_fabric_output", "")
    monkeypatch.setattr(mon, "last_fabric_ok", None)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(CHIPS)})
    monkeypatch.setattr(srv, "heartbeat_supported", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda action, *, tenant_active: (True, "ok"))
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_failed", False)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", srv.time.monotonic() - 100_000.0)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 0)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)
    monkeypatch.setattr(pcie_guard, "record_topology", lambda: None)
    monkeypatch.setattr(pcie_guard, "expire_offbus_reset_latch", lambda *a: None)
    monkeypatch.setattr(hostpci, "host_pci_verdict", lambda: (*st["hostpci"], {}))

    async def snapshot(expected):
        return st["snapshot"]

    async def eth_read(timeout_sec=None):
        st["eth_reads"] += 1
        return st["eth"]

    async def fabric_pass():
        st["fabric"].append(len(st["resets"]))
        if st["fabric_77"]:
            return None, "fabric check could not check (77)"
        if st["fabric_bad"]:
            mon.last_fabric_ok = False
            return False, "fabric failed: link 3-4 moved no traffic"
        mon.last_fabric_ok = True
        return True, "all links healthy; dispatch OK"

    monkeypatch.setattr(mon, "_verify_device", snapshot)
    monkeypatch.setattr(mon, "verify_eth_heartbeat", eth_read)
    monkeypatch.setattr(mon, "verify_fabric_health", fabric_pass)

    async def no_scope(log):
        return False

    async def reset(argv, log, *a, **k):
        st["resets"].append(list(argv))
        st["eth"] = ADVANCING
        st["fabric_bad"] = not st.get("reset_heals", True)
        return 0, ""

    async def reboot(log, reason):
        st["reboots"] += 1

    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", no_scope)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    monkeypatch.setattr(g, "_auto_reboot_host", reboot)
    patch_health_event(monkeypatch, lambda name, *a, **k: st["events"].append(name))
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: object())
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    captured = []
    monkeypatch.setattr(srv, "capture_incident", lambda kind, **k: captured.append((kind, k.get("evidence"))))
    st["captured"] = captured
    log = _Logger()
    monkeypatch.setattr(srv, "logger", log)
    st["log"] = log
    return st


async def _gate():
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)


@pytest.mark.asyncio
async def test_a_timed_out_eth_read_resets_once_with_no_pre_reset_fabric_pass(mesh):
    """Base ran the ~45-100 s fabric pass first, which failed or skipped after every such timeout
    measured. Now no pass runs before the one glx_reset; the full check after it releases."""
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]], "one present-mesh reset"
    assert mesh["fabric"] and min(mesh["fabric"]) == 1, "no fabric pass ran before the reset"
    assert mesh["events"].count("eth_timeout_mesh_reset") == 1
    assert "eth-heartbeat timed out after a clean exit" in mesh["log"].text()
    assert srv.fsm.state is ServerState.HEALTHY, "the full check after the reset passed"
    assert srv._dispatch_reset_spent is True, "the reset spends the episode's shared latch"


@pytest.mark.asyncio
async def test_the_incident_is_captured_with_the_eth_reason_before_the_reset(mesh):
    await _gate()
    kind, evidence = mesh["captured"][0]
    assert kind == "eth_timeout"
    assert eth.READ_TIMED_OUT in evidence["eth_heartbeat"]["detail"]
    assert evidence["snapshot"]["ok"] is True and "fabric" not in evidence


@pytest.mark.asyncio
async def test_the_full_check_after_the_reset_gates_the_release(mesh):
    """The post-reset check reads the eth cores and runs the fabric pass; only its pass releases."""
    await _gate()
    assert mesh["eth_reads"] == 2, "the clean-exit read, then the full check's own read"
    assert mesh["fabric"] == [1], "the full check after the reset ran the fabric pass"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_failed_post_reset_check_stays_held_and_climbs(mesh):
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True  # a prior cycle failed: the host rung is armed
    await _gate()
    assert mesh["resets"][0] == ["tt-smi", "-glx_reset"]
    assert mesh["events"].count("eth_timeout_mesh_reset") == 1
    assert mesh["fabric"] and min(mesh["fabric"]) >= 1, "every fabric pass ran after the reset"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed post-reset check never releases"
    assert mesh["reboots"] == 1, "it climbs to the opted-in host rung"


@pytest.mark.asyncio
async def test_a_failed_post_reset_check_without_a_host_rung_stays_held(mesh):
    mesh["reset_heals"] = False
    await _gate()
    assert len(mesh["resets"]) == 1
    assert srv.fsm.state is ServerState.RECOVERING, "held, never released"


@pytest.mark.asyncio
async def test_a_second_timeout_in_the_episode_gets_no_second_reset(mesh):
    mesh["reset_heals"] = False
    await _gate()
    assert len(mesh["resets"]) == 1 and srv._dispatch_reset_spent is True
    # Same episode, cooldown elapsed, a clean exit times out again.
    srv.recovery_mechanism.last_reset_monotonic = srv.time.monotonic() - 100_000.0
    srv.recovery_mechanism.last_reset_failed = False
    mesh["eth"] = TIMED_OUT
    fabric_before = len(mesh["fabric"])
    await _gate()
    assert mesh["events"].count("eth_timeout_mesh_reset") == 1, "no second eth-timeout reset"
    assert len(mesh["fabric"]) > fabric_before


@pytest.mark.asyncio
async def test_a_spent_dispatch_reset_leaves_the_timeout_on_todays_path(mesh, monkeypatch):
    """The latch is shared with spec 03 I55: a dispatch reset this episode leaves none for the
    timeout, which then runs the fabric pass as before."""
    monkeypatch.setattr(srv, "_dispatch_reset_spent", True)
    await _gate()
    assert mesh["resets"] == [] and "eth_timeout_mesh_reset" not in mesh["events"]
    assert mesh["fabric"] == [0], "the timeout ran today's fabric pass"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_release_with_no_open_hold_rearms_the_latch(mesh):
    await _gate()
    assert srv._dispatch_reset_spent is True
    assert srv.device_hold_logged is False
    srv._note_tenant_gate_verdict("")  # the next tenant is admitted; no hold episode is open
    assert srv._dispatch_reset_spent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "eth_read",
    [
        (None, f"{ETH_LINK_DROP_SKIP}: measured 11 links, mark 12"),
        (None, f"{ETH_COULD_NOT_CHECK}: probe exited 1 (not a frozen verdict)"),
        (None, f"{ETH_COULD_NOT_CHECK}: probe exited 77 (not a frozen verdict)"),
    ],
    ids=["link-drop-skip", "crash", "probe-77"],
)
async def test_other_no_verdict_reads_keep_todays_fabric_pass(mesh, monkeypatch, eth_read):
    monkeypatch.setattr(srv, "last_fabric_check_monotonic", 0.0)  # stale: the link-drop skip may run it
    mesh["eth"] = eth_read
    await _gate()
    assert mesh["resets"] == [] and "eth_timeout_mesh_reset" not in mesh["events"]
    assert mesh["fabric"] == [0], "today's pass ran"


@pytest.mark.asyncio
async def test_a_frozen_read_keeps_todays_hold(mesh):
    mesh["eth"] = (False, "a frozen active-eth core (2s): heartbeat not advancing")
    await _gate()
    assert mesh["resets"] == [] and mesh["fabric"] == []
    assert "eth_timeout_mesh_reset" not in mesh["events"]
    assert srv.fsm.state is ServerState.RECOVERING, "held, not reset"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [(False, "31 of 32 chips enumerated"), (False, "chip 7: ARC not responding")],
    ids=["off-bus-chip", "arc-unhealthy"],
)
async def test_an_off_bus_chip_or_unhealthy_arc_keeps_todays_path(mesh, snapshot):
    mesh["snapshot"] = snapshot
    await _gate()
    assert "eth_timeout_mesh_reset" not in mesh["events"]
    assert all(c == 0 for c in mesh["fabric"][:1]), "nothing fabric-related was skipped for a reset"


@pytest.mark.asyncio
async def test_host_pci_not_ok_keeps_todays_fabric_pass(mesh):
    mesh["hostpci"] = (None, "host-pci read skipped")
    await _gate()
    assert mesh["resets"] == [] and "eth_timeout_mesh_reset" not in mesh["events"]
    assert mesh["fabric"] == [0]


@pytest.mark.asyncio
async def test_an_outage_hold_refuses_the_reset_and_keeps_todays_pass(mesh, monkeypatch):
    for var, value in OUTAGE_HOLD.items():
        monkeypatch.setenv(var, value)
    await _gate()
    assert mesh["resets"] == [] and "eth_timeout_mesh_reset" not in mesh["events"]
    assert mesh["fabric"] == [0], "today's fabric pass ran instead"
    assert srv._dispatch_reset_spent is False


@pytest.mark.asyncio
async def test_a_dirty_exit_still_runs_the_fabric_pass(mesh):
    fsm_dirty(srv, "job failed: Timed out while waiting for active ethernet core 27-25")
    await _gate()
    assert "eth_timeout_mesh_reset" not in mesh["events"]
    assert mesh["fabric"] and mesh["fabric"][0] == 0, "the dirty exit's fabric pass ran first"


@pytest.mark.asyncio
async def test_the_probe_pass_skips_the_fabric_pass_only_when_asked(mesh):
    """HealthMonitor.update alone: the timed-out read skips the pass with eth_timeout_reset, and
    runs it without (today's path)."""
    mon = srv.health_monitor
    state = await mon.update("post-job", run_fabric=False, run_eth=True, eth_timeout_reset=True, expected=CHIPS)
    assert mesh["fabric"] == [] and state.fabric_ran is False
    state = await mon.update("post-job", run_fabric=False, run_eth=True, expected=CHIPS)
    assert mesh["fabric"] == [0] and state.fabric_ran is True
