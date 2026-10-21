# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Every PCI rescan is scoped to the Tenstorrent root ports (spec 04 I95).

A global ``/sys/bus/pci/rescan`` walks every root bus of the host, and has reassigned the resources
of root ports that have nothing to do with the device (a host's NVMe drives). Each rescan path here
runs for real against a fake sysfs holding one Tenstorrent root port and one NVMe root port: the
write reaches only the Tenstorrent port's ``pci_bus/<bus>/rescan``, never the NVMe port's and never the
global file. With no Tenstorrent root port to rescan, the rescan fails (its rung fails and recovery
goes on), and nothing at all is written.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from tests.test_below_floor_offbus_ladder import _bound_ladder, _rig
from tt_device_mcp import health_probe as hp
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.pci_writer import PCI_WRITER
from tt_device_mcp.health.recovery import (
    OUTCOME_TERMINAL,
    _sysfs_rescan,
    base,
    bridge_reset_call_timeout,
    galaxy,
    pcie_guard,
)
from tt_device_mcp.health.recovery.stages import bridge_reset as bridge

# Captured at import, before conftest's autouse stub replaces it for every test.
REAL_GALAXY_RESCAN = galaxy._pci_rescan

TT_PORT, TT_EP, TT_BUS = "0000:00:01.0", "0000:01:00.0", "0000:01"
NVME_PORT, NVME_EP, NVME_BUS = "0000:00:02.0", "0000:02:00.0", "0000:02"
SRC = Path(__file__).resolve().parent.parent / "src" / "tt_device_mcp"


class FakeSysfs:
    """One Tenstorrent root port and one NVMe root port under ``root``; every rescan file starts empty."""

    def __init__(self, root: Path, tt_endpoint: bool = True, tt_port: bool = True):
        self.root = root
        devices = root / "sys/bus/pci/devices"
        devices.mkdir(parents=True)
        self.global_rescan = root / "sys/bus/pci/rescan"
        self.global_rescan.write_text("")
        self.rescan_files = {}
        ports = [(NVME_PORT, NVME_EP, NVME_BUS, "0x144d", "0xa808")]
        if tt_port:
            ports.append((TT_PORT, TT_EP, TT_BUS, "0x1e52", "0xb140"))
        for port, ep, bus, vendor, device in ports:
            port_dir = root / "sys/devices/pci0000:00" / port
            port_dir.mkdir(parents=True)
            (port_dir / "vendor").write_text("0x8086\n")
            (port_dir / "pci_bus" / bus).mkdir(parents=True)
            self.rescan_files[port] = port_dir / "pci_bus" / bus / "rescan"
            self.rescan_files[port].write_text("")
            (devices / port).symlink_to(port_dir)
            if vendor == "0x1e52" and not tt_endpoint:
                continue  # the chip is off the bus: its port stays, its function is gone
            (port_dir / ep).mkdir()
            (port_dir / ep / "vendor").write_text(vendor + "\n")
            (port_dir / ep / "device").write_text(device + "\n")
            (devices / ep).symlink_to(port_dir / ep)

    def writes(self) -> dict:
        """Every rescan file that was written: {"global" or port: content}."""
        out = {p: f.read_text() for p, f in self.rescan_files.items() if f.read_text()}
        if self.global_rescan.read_text():
            out["global"] = self.global_rescan.read_text()
        return out


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    fake = FakeSysfs(tmp_path / "root")
    monkeypatch.setattr(pcie_guard, "SYS_ROOT", fake.root)
    monkeypatch.setattr(pcie_guard, "_SEEN", None)
    PCI_WRITER.reset_for_tests()
    yield fake
    PCI_WRITER.reset_for_tests()


@pytest.fixture
def no_tt_sysfs(tmp_path, monkeypatch):
    fake = FakeSysfs(tmp_path / "root", tt_port=False)
    monkeypatch.setattr(pcie_guard, "SYS_ROOT", fake.root)
    monkeypatch.setattr(pcie_guard, "_SEEN", None)
    PCI_WRITER.reset_for_tests()
    yield fake
    PCI_WRITER.reset_for_tests()


SCOPED = {TT_PORT: "1"}


def test_no_source_string_names_the_global_rescan_file():
    """No code string in the package names /sys/bus/pci/rescan (docstrings may say why not)."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "bus/pci/rescan" in node.value
            ):
                found.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert found == []


def test_the_scoped_rescan_writes_only_the_tenstorrent_root_ports_bus(sysfs):
    assert pcie_guard.rescan_tt_root_ports() == [TT_PORT]
    assert sysfs.writes() == SCOPED


def test_an_off_bus_chips_remembered_root_port_is_still_rescanned(sysfs):
    pcie_guard.record_topology()  # a look while the chip is on the bus
    (sysfs.root / "sys/bus/pci/devices" / TT_EP).unlink()  # then it drops off
    assert pcie_guard.rescan_tt_root_ports() == [TT_PORT]
    assert sysfs.writes() == SCOPED


def test_at_startup_with_the_tray_already_off_the_bus_the_recorded_port_is_rescanned(tmp_path, monkeypatch):
    """A broker that starts with the chip off the bus knows its root port from the recorded topology."""
    fake = FakeSysfs(tmp_path / "root", tt_endpoint=False)
    monkeypatch.setattr(pcie_guard, "SYS_ROOT", fake.root)
    monkeypatch.setattr(pcie_guard, "_SEEN", None)
    pcie_guard.health_dir().mkdir(parents=True, exist_ok=True)
    (pcie_guard.health_dir() / pcie_guard.TOPOLOGY_FILE).write_text(
        json.dumps({"root_ports": [TT_PORT], "devices": [0xB140]})
    )
    pcie_guard.record_topology()  # the broker's start
    assert pcie_guard.rescan_tt_root_ports() == [TT_PORT]
    assert fake.writes() == SCOPED


def test_with_no_tenstorrent_root_port_the_rescan_fails_and_writes_nothing(no_tt_sysfs):
    with pytest.raises(OSError):
        pcie_guard.rescan_tt_root_ports()
    assert no_tt_sysfs.writes() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["reset path rescan", "bridge reset batch rescan", "bare rescan of dead chips", "tray-down and below-floor rescan"],
)
async def test_every_rescan_rung_reaches_only_the_tenstorrent_root_port(sysfs, path):
    """Each rung's rescan goes through the one in-flight PCI writer, to the scoped target."""
    if path == "reset path rescan":
        await base.pci_rescan()
    elif path == "tray-down and below-floor rescan":
        await base.pci_rescan(path, lambda: REAL_GALAXY_RESCAN())
    else:
        await base.pci_rescan(path, _sysfs_rescan)
    assert sysfs.writes() == SCOPED
    assert not PCI_WRITER.latched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["reset path rescan", "bridge reset batch rescan", "bare rescan of dead chips", "tray-down and below-floor rescan"],
)
async def test_with_no_tenstorrent_root_port_every_rescan_rung_fails_without_a_global_write(no_tt_sysfs, path):
    """The rung fails (OSError, which each caller handles as a failed rung) and does not latch
    PCI-wedged: nothing blocked, so the ladder goes on to its next step."""
    with pytest.raises(OSError):
        if path == "reset path rescan":
            await base.pci_rescan()
        elif path == "tray-down and below-floor rescan":
            await base.pci_rescan(path, lambda: REAL_GALAXY_RESCAN())
        else:
            await base.pci_rescan(path, _sysfs_rescan)
    assert no_tt_sysfs.writes() == {}
    assert not PCI_WRITER.latched


def _bridge_world(monkeypatch, tmp_path):
    cls = tmp_path / "class"
    (cls / "tenstorrent!0").mkdir(parents=True)  # the chip re-binds after the rescan
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", cls)
    monkeypatch.setattr(bridge.pci, "parent_bridge", lambda bdf: TT_PORT)
    monkeypatch.setattr(bridge, "secondary_bus_reset", lambda idx, br: True)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)


@pytest.mark.asyncio
async def test_the_bridge_reset_rescans_only_the_tenstorrent_root_port(sysfs, monkeypatch, tmp_path):
    _bridge_world(monkeypatch, tmp_path)
    result = await PCI_WRITER.write(
        "bridge reset of chip 0", bridge.reset_chip_via_bridge, "0", TT_EP, timeout=bridge_reset_call_timeout()
    )
    assert result is True
    assert sysfs.writes() == SCOPED


@pytest.mark.asyncio
async def test_a_bridge_reset_with_no_tenstorrent_root_port_fails_without_a_global_write(
    no_tt_sysfs, monkeypatch, tmp_path
):
    _bridge_world(monkeypatch, tmp_path)
    result = await PCI_WRITER.write(
        "bridge reset of chip 0", bridge.reset_chip_via_bridge, "0", TT_EP, timeout=bridge_reset_call_timeout()
    )
    assert result is False, "the SBR fired but nothing was rescanned: the rung failed"
    assert no_tt_sysfs.writes() == {}


def test_the_health_probe_rescans_only_the_tenstorrent_root_port(sysfs, monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(tmp_path / "device-op.lock"))
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", raising=False)
    assert hp.Probe(sysfs.root).rescan() is True
    assert sysfs.writes() == SCOPED


def test_the_health_probe_with_no_tenstorrent_root_port_writes_nothing(no_tt_sysfs, monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(tmp_path / "device-op.lock"))
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", raising=False)
    with pytest.raises(OSError):
        hp.Probe(no_tt_sysfs.root).rescan()
    assert no_tt_sysfs.writes() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("tt_port", [True, False], ids=["tt-port", "no-tt-port"])
async def test_the_10_min_ladders_rescan_is_scoped_and_a_failed_one_still_reaches_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state, tt_port
):
    """The bound ladder (goal 10) with the real rescan: it writes only the Tenstorrent root port, or,
    with none to rescan, fails that rung and still climbs to the mesh reset, the full check and the
    power cycle. A failed check never lifts the hold."""
    order, _events, counters = _rig(monkeypatch, tmp_path)
    fake = FakeSysfs(tmp_path / "root", tt_port=tt_port)
    monkeypatch.setattr(pcie_guard, "SYS_ROOT", fake.root)
    monkeypatch.setattr(pcie_guard, "_SEEN", None)
    monkeypatch.setattr(galaxy, "_pci_rescan", REAL_GALAXY_RESCAN)
    PCI_WRITER.reset_for_tests()
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert order == ["mesh reset", "full check", "power cycle"]
    assert fake.writes() == (SCOPED if tt_port else {})
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed check never lifts"
    assert not PCI_WRITER.latched
    assert srv.device_hold_episode_since
    await asyncio.sleep(0)
