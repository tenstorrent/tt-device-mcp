# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A chip whose heartbeat read fails with ENODATA on a bound endpoint is on the bus with its ARC not
ready, not off it (spec 04 I24), and a chip that came back leaves ``isolated_chips`` after every
reset, re-power and rescan (spec 03, the /health live off-bus fields).

Every test runs on a fake 32-chip sysfs: a KMD class node per chip whose ``device`` link points at an
endpoint dir under ``pci.PCI_DEVICES_DIR``, bound to the driver unless a test says otherwise. A plain
file cannot raise ENODATA, so ``heartbeat._read_beat`` raises it for the chips a test names."""

import errno

import pytest

from tests.conftest import fsm_dirty, fsm_healthy, patch_health_event, patch_recovery
from tests.test_device_safety import _setup_down_offbus
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitors import heartbeat, pci
from tt_device_mcp.health.recovery import base, galaxy, pcie_guard

EXPECTED = 32


def _bdf(i: int) -> str:
    return f"0000:{0x01 + i:02x}:00.0"


def _fake_sysfs(monkeypatch, tmp_path, *, missing=(), unbound=(), enodata=(), eio=()) -> dict:
    """32 chips; ``missing`` have no class node, ``unbound`` an endpoint with no driver, ``enodata``
    (``eio``) a heartbeat read that fails with ENODATA (EIO). Returns a handle to bring a chip back."""
    cls_dir = tmp_path / "sys/class/tenstorrent"
    devs = tmp_path / "sys/bus/pci/devices"
    cls_dir.mkdir(parents=True)
    devs.mkdir(parents=True)
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", cls_dir)
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", devs)
    nodata = {str(i) for i in enodata}
    io_error = {str(i) for i in eio}

    def add(i: int, *, bound: bool = True) -> None:
        ep = devs / _bdf(i)
        ep.mkdir(exist_ok=True)
        if bound:
            (ep / "driver").mkdir(exist_ok=True)
        node = cls_dir / f"tenstorrent!{i}"
        node.mkdir()
        (node / "device").symlink_to(ep)
        (node / "tt_heartbeat").write_text("100\n")

    for i in range(EXPECTED):
        if i not in missing:
            add(i, bound=i not in unbound)
    real = heartbeat._read_beat

    def read_beat(node):
        if node.name.rsplit("!", 1)[-1] in nodata:
            raise OSError(errno.ENODATA, "No data available")
        if node.name.rsplit("!", 1)[-1] in io_error:
            raise OSError(errno.EIO, "Input/output error")
        return real(node)

    monkeypatch.setattr(heartbeat, "_read_beat", read_beat)
    return {"add": add, "nodata": nodata}


def _live_health(monkeypatch) -> None:
    """The broker reads the heartbeat detector and a 32-chip baseline."""
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: EXPECTED)
    monkeypatch.setattr(srv.health_monitor, "_health_check_enabled", lambda: True)


# ---------------------------------------------------------------- the count every rung uses


def test_enodata_on_31_bound_chips_is_not_off_the_bus(monkeypatch, tmp_path):
    """Spec 04 I24: 31 chips mid-boot after a re-power (ENODATA, endpoints bound) are on the bus. No
    off-bus rung, floor or tray plan may count them; the verdict still reads UNHEALTHY and names them."""
    _fake_sysfs(monkeypatch, tmp_path, enodata=range(1, EXPECTED))
    monkeypatch.setattr(heartbeat.time, "sleep", lambda s: None)
    beats = heartbeat.read_heartbeats()
    assert beats == {"0": 100}

    assert heartbeat.offbus_count(beats, EXPECTED) == 0
    assert galaxy._offbus_chip_ids(beats, EXPECTED) == set()
    verdict, detail, evidence = heartbeat.heartbeat_verdict(expected_count=EXPECTED)
    assert verdict is Verdict.UNHEALTHY, detail
    assert evidence["off_bus"] == 0
    assert evidence["arc_not_ready"] == sorted((str(i) for i in range(1, EXPECTED)), key=int)


@pytest.mark.parametrize("case", ["unbound endpoint", "missing node", "other errno"])
def test_an_unbound_or_missing_chip_still_counts_off_the_bus(monkeypatch, tmp_path, case):
    """ENODATA on a bound endpoint is the only not-ready shape: an endpoint the driver let go of, a node
    that is gone, or a read failing any other way (EIO) is off the bus."""
    if case == "unbound endpoint":
        _fake_sysfs(monkeypatch, tmp_path, unbound=[6], enodata=[6])
    elif case == "other errno":
        _fake_sysfs(monkeypatch, tmp_path, eio=[6])
    else:
        _fake_sysfs(monkeypatch, tmp_path, missing=[6])
    beats = heartbeat.read_heartbeats()

    assert heartbeat.arc_not_ready_chips(beats) == set()
    assert heartbeat.offbus_count(beats, EXPECTED) == 1
    assert galaxy._offbus_chip_ids(beats, EXPECTED) == {"6"}


def test_health_reports_arc_not_ready_chips_as_on_the_bus_and_degraded(monkeypatch, tmp_path, clear_job_state):
    """/health reads the live state: off_bus 0, the 31 chips named arc_not_ready, and the device degraded
    (the gate would fail on them), so a poller sees neither a drop nor a healthy box."""
    _fake_sysfs(monkeypatch, tmp_path, enodata=range(1, EXPECTED))
    _live_health(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "isolated_chips", set())

    payload = srv._health_payload()

    assert payload["off_bus"] == 0
    assert len(payload["arc_not_ready"]) == 31
    assert payload["expected_chips"] == EXPECTED
    assert payload["status"] == "degraded"
    assert "ARC not ready" in payload["device_degraded"]


# ---------------------------------------------------------------- returned chips leave isolated_chips


@pytest.mark.parametrize("state", ["counting", "enodata bound"])
def test_drop_returned_isolated_takes_back_chips_out(monkeypatch, tmp_path, state):
    """A chip back on the bus (a live counter, or ARC-not-ready on a bound endpoint) leaves the set;
    one still missing or reading all-ones stays."""
    _fake_sysfs(monkeypatch, tmp_path, missing=[9], enodata=[6] if state == "enodata bound" else [])
    (pci.SYSFS_CLASS_DIR / "tenstorrent!7" / "tt_heartbeat").write_text(f"{heartbeat.ALL_ONES}\n")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    isolated = {"6", "7", "9"}

    assert base.drop_returned_isolated(isolated, "test", lambda m: None) == ["6"]
    assert isolated == {"7", "9"}


@pytest.mark.asyncio
async def test_a_tray_re_power_drops_a_returned_chip_even_when_the_verify_fails(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """Chip 6 comes back on its tray's re-power but the mesh does not verify: the walk still takes it
    out of isolated_chips, so /health and the next gate stop reading it as isolated."""
    sysfs = _fake_sysfs(monkeypatch, tmp_path, missing=[6])
    _live_health(monkeypatch)
    monkeypatch.setattr(pcie_guard, "_boot_id", lambda: "boot-a")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(galaxy, "UBB_WALK_RESCAN_SETTLE_SEC", 0)
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_UBB_SINGLE_TRAY_WALK", "1")  # the single-tray walk under test is opt-in
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "isolated_chips", {"6"})

    async def unhealthy(expected, log, **_):
        return False, {}, 0

    patch_recovery(monkeypatch, "_verify_device_after_reset", unhealthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: sysfs["add"](6), raising=False)
    beats = heartbeat.read_heartbeats()
    assert "6" not in beats

    assert await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 1, EXPECTED, lambda m: None) is False
    assert srv.isolated_chips == set()
    assert srv._health_payload()["isolated_chips"] == []


@pytest.mark.asyncio
async def test_a_reset_drops_a_returned_chip_after_its_rescan(monkeypatch, tmp_path, clear_job_state):
    """Chip 6 comes back on the reset's PCI rescan: the reset takes it out of isolated_chips right
    there, before any verify runs; chip 9, still missing, stays."""
    _fake_sysfs(monkeypatch, tmp_path, missing=[9])
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "isolated_chips", {"6", "9"})
    ran = []

    async def pollers(active, log):
        return []

    async def run(argv, log, owner="[broker]health-gate"):
        return 0, "out"

    async def to_thread(fn, *a, **k):
        if fn is base.drop_returned_isolated:  # the real /sys/bus/pci/rescan write never runs here
            ran.append(a[1])
            return fn(*a, **k)

    async def no_sleep(_):
        return None

    async def no_ceiling(*a, **k):
        return True, "ok", {}

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(base.CEILING, "apply", no_ceiling)

    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)

    assert rc == 0 and ran == ["reset"]
    assert srv.isolated_chips == {"9"}


# ---------------------------------------------------------------- goal 8: nothing here releases


@pytest.mark.asyncio
async def test_a_down_offbus_recheck_holds_on_arc_not_ready_chips_without_the_gate(
    monkeypatch, tmp_path, clear_job_state
):
    """Every node is back but chip 6's ARC is not ready: the full gate would fail on it, so the re-check
    holds without running it, and never releases the DOWN episode."""
    _setup_down_offbus(monkeypatch, tmp_path)
    _fake_sysfs(monkeypatch, tmp_path / "fake", enodata=[6])
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)

    async def no_gate(*a, **k):
        pytest.fail("the full gate must not run while a chip's ARC is not ready")

    monkeypatch.setattr(srv, "_offbus_return_full_gate", no_gate)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda *a, **k: pytest.fail("released"))

    await srv._attempt_down_offbus_recheck()

    assert srv.fsm.state is ServerState.DOWN and srv.fsm.record.why == "off_bus"


def test_dropping_a_returned_chip_never_clears_the_hold(monkeypatch, tmp_path, clear_job_state):
    """A chip back on the bus leaves isolated_chips, but the hold stands: only the full health check,
    fabric included, releases the device (spec 03 I52)."""
    _fake_sysfs(monkeypatch, tmp_path)
    _live_health(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "isolated_chips", {"6"})
    fsm_dirty(srv, "gate/post-job: chip 6 off the bus", why="off_bus")
    before = (srv.fsm.state, srv.fsm.record.since, srv.fsm.record.why)

    assert base.drop_returned_isolated(srv.isolated_chips, "tray 1 re-power") == ["6"]

    assert srv.isolated_chips == set()
    assert (srv.fsm.state, srv.fsm.record.since, srv.fsm.record.why) == before
    assert srv._health_payload()["held"] is True
