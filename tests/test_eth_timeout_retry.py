# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I61: a clean post-job eth read that timed out with no verdict is read once more after a short
settle. A measured retry carries on as a first-try measurement; a retry still unmeasured runs the fabric
pass instead of a present-mesh reset, and only that pass's pass releases the device. The device's readers
are stopped for the reads and restored on every exit.

The real probe pass runs here (HealthMonitor.update); only its leaf probes are stubbed."""

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import ETH_COULD_NOT_CHECK, ETH_LINK_DROP_SKIP
from tt_device_mcp.health.monitors import eth, hostpci
from tt_device_mcp.health.recovery import pcie_guard

CHIPS = 32
TIMED_OUT = (None, f"{ETH_COULD_NOT_CHECK}: {eth.READ_TIMED_OUT}")
ADVANCING = (True, "all active-eth cores advancing (1s)")
FROZEN = (False, "a frozen active-eth core (2s): heartbeat not advancing")


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
    """A 32-chip Galaxy, every chip on the bus and ARC healthy, after a clean job exit whose eth read times out."""
    st = {
        "eth": TIMED_OUT,
        "eth_seq": [],
        "pollers": [],
        "eth_raises": False,
        "fabric_bad": False,
        "fabric_77": False,
        "snapshot": (True, "32/32 chips, ARC ok"),
        "hostpci": (True, "all chips bound"),
        "resets": [],
        "fabric": [],
        "eth_reads": 0,
        "events": [],
    }
    for i in range(CHIPS):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
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
        st["pollers"].append("read")
        if st["eth_raises"]:
            raise RuntimeError("probe blew up")
        return st["eth_seq"].pop(0) if st["eth_seq"] else st["eth"]

    async def pollers(active, log):
        st["pollers"].append("start" if active else "stop")
        return [] if active else ["tt-telemetry"]

    # The production wiring: the broker's own quiesce around a reader's work.
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv._recovery_deps, "quiesce_readers", lambda what, log: srv._pollers_quiesced_for(what, log))
    monkeypatch.setattr(monitor_mod, "ETH_POST_JOB_RETRY_SETTLE_SEC", 0.0)

    async def fabric_pass():
        st["pollers"].append("fabric")
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

    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", no_scope)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    patch_health_event(monkeypatch, lambda name, *a, **k: st["events"].append(name))
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
async def test_a_timed_out_read_that_measures_on_retry_needs_no_reset_or_fabric_pass(mesh):
    """The healthy-mesh case: the read timed out once and answers on the retry. Nothing resets, no fabric pass
    runs, the gate goes on as for a first-try measurement."""
    mesh["eth_seq"] = [TIMED_OUT, ADVANCING]
    await _gate()
    assert mesh["eth_reads"] == 2, "the timed-out read, then one retry"
    assert mesh["resets"] == [] and mesh["fabric"] == []
    assert srv.fsm.state is ServerState.HEALTHY
    assert srv._dispatch_reset_spent is False, "no present-mesh reset was spent"
    assert "reading once more" in mesh["log"].text()


@pytest.mark.asyncio
async def test_a_frozen_retry_holds_as_a_first_try_frozen_read_does(mesh):
    mesh["eth_seq"] = [TIMED_OUT, FROZEN]
    await _gate()
    assert mesh["eth_reads"] == 2
    assert mesh["resets"] == [] and mesh["fabric"] == [], "a frozen core never gets the traffic pass"
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_two_timeouts_then_a_passing_fabric_pass_releases_with_no_reset(mesh):
    mesh["eth_seq"] = [TIMED_OUT, TIMED_OUT]
    await _gate()
    assert mesh["eth_reads"] == 2, "the read is retried once, never twice"
    assert mesh["fabric"] == [0], "the fabric pass ran, with no reset before it"
    assert mesh["resets"] == [], "no glx_reset"
    assert srv.fsm.state is ServerState.HEALTHY
    assert srv._dispatch_reset_spent is False


@pytest.mark.asyncio
async def test_two_timeouts_then_a_failing_fabric_pass_stays_held(mesh):
    """A failed pass takes today's reset path; a reset that does not heal leaves the device held."""
    mesh["eth_seq"] = [TIMED_OUT, TIMED_OUT]
    mesh["fabric_bad"] = True
    mesh["reset_heals"] = False
    await _gate()
    assert mesh["fabric"][0] == 0, "the fabric pass ran before any reset"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed check never releases"
    assert srv._device_unavailable_for_tenant(), "no tenant runs on a mesh the check failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "no-verdict"])
async def test_nothing_releases_the_device_without_a_passed_fabric_pass(mesh, outcome):
    """With the eth read unmeasured twice, only a fabric pass that has just passed releases the device."""
    mesh["eth_seq"] = [TIMED_OUT, TIMED_OUT]
    mesh["reset_heals"] = False
    mesh["fabric_bad"] = outcome == "failed"
    mesh["fabric_77"] = outcome == "no-verdict"
    await _gate()
    assert srv.fsm.state is not ServerState.HEALTHY
    assert srv._device_unavailable_for_tenant()


@pytest.mark.asyncio
async def test_the_readers_are_stopped_for_the_read_and_restored(mesh):
    """The telemetry poller is stopped before the post-job read and started again after it (and its
    retry), as for the fabric check."""
    mesh["eth_seq"] = [TIMED_OUT, ADVANCING]
    await _gate()
    events = mesh["pollers"]
    assert events[:4] == ["stop", "read", "read", "start"], events
    assert events.count("stop") == events.count("start"), "every stop is paired with a start"


@pytest.mark.asyncio
async def test_the_readers_are_restored_when_the_read_raises(mesh):
    mesh["eth_raises"] = True
    with pytest.raises(RuntimeError, match="probe blew up"):
        await srv.health_monitor.update("post-job", run_fabric=False, run_eth=True, expected=CHIPS)
    assert mesh["pollers"] == ["stop", "read", "start"]


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
async def test_other_no_verdict_reads_are_not_retried(mesh, monkeypatch, eth_read):
    monkeypatch.setattr(srv, "last_fabric_check_monotonic", 0.0)  # stale: the link-drop skip may run it
    mesh["eth"] = eth_read
    await _gate()
    assert mesh["eth_reads"] == 1
    assert mesh["resets"] == [] and mesh["fabric"] == [0], "today's pass ran"


@pytest.mark.asyncio
async def test_host_pci_not_ok_is_not_retried(mesh):
    mesh["hostpci"] = (None, "host-pci read skipped")
    await _gate()
    assert mesh["eth_reads"] == 1
    assert mesh["resets"] == [] and mesh["fabric"] == [0]


@pytest.mark.asyncio
async def test_an_asked_for_fabric_pass_does_not_retry_the_read(mesh):
    """HealthMonitor.update with the fabric pass asked for: the read is the full check's own, never retried."""
    state = await srv.health_monitor.update("post-job", run_fabric=True, expected=CHIPS)
    assert mesh["eth_reads"] == 1
    assert mesh["fabric"] == [0] and state.fabric_ran is True


@pytest.mark.asyncio
async def test_a_frozen_read_keeps_todays_hold(mesh):
    mesh["eth"] = FROZEN
    await _gate()
    assert mesh["eth_reads"] == 1, "a measured read is never retried"
    assert mesh["resets"] == [] and mesh["fabric"] == []
    assert srv.fsm.state is ServerState.RECOVERING, "held, not reset"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [(False, "31 of 32 chips enumerated"), (False, "chip 7: ARC not responding")],
    ids=["off-bus-chip", "arc-unhealthy"],
)
async def test_an_off_bus_chip_or_unhealthy_arc_reads_no_eth(mesh, snapshot):
    mesh["snapshot"] = snapshot
    await _gate()
    assert mesh["eth_reads"] == 0


@pytest.mark.asyncio
async def test_a_degraded_pcie_link_holds_and_never_releases(mesh):
    """Spec 03 I9: a link trained below its expected width fails host-pci like any other host-pci
    fault. The gate holds and recovers; the device is never released while the check fails."""
    mesh["eth"] = ADVANCING
    mesh["hostpci"] = (False, "PCIe link degraded on 1/32: 0000:01:00.0 x1 (expected x8)")
    await _gate()
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed host-pci check never releases"
    assert "PCIe link degraded" in mesh["log"].text()


@pytest.mark.asyncio
async def test_a_dirty_exit_still_runs_the_fabric_pass(mesh):
    fsm_dirty(srv, "job failed: Timed out while waiting for active ethernet core 27-25")
    await _gate()
    assert mesh["fabric"] and mesh["fabric"][0] == 0, "the dirty exit's fabric pass ran first"
