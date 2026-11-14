# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I101: no chip-side PCIe config-space read while a check, a device start or a reset runs.

Every window type is driven here against a fake sysfs whose chip-side config reads fail the test: a link status
attribute (``current_link_*``, ``max_link_*``) or the ``config`` file of the chip, or an ``lspci -vv`` of it. Inside
the window the readers answer from the cache and from the chip's upstream port; outside it they read again."""

import builtins
import contextlib
import contextvars
import os
import time
from pathlib import Path

import pytest

from tt_device_mcp.health import bmc_capture, chip_state, pcie_quiet
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import hostpci, pci, pcie_aer

PORT = "0000:40:01.1"
CHIP = "0000:41:00.0"


def _port_config(capable: bool, link_up: bool) -> bytes:
    cfg = bytearray(256)
    cfg[0:2] = (0x1022).to_bytes(2, "little")
    cfg[0x06] = 0x10  # capability list
    cfg[0x34] = 0x40
    cfg[0x40] = 0x10  # PCI Express capability, last in the list
    cfg[0x4C:0x50] = ((1 << 20) if capable else 0).to_bytes(4, "little")  # Link Capabilities
    cfg[0x52:0x54] = (0x2000 if link_up else 0).to_bytes(2, "little")  # Link Status
    return bytes(cfg)


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """A root port with one chip below it, laid out as the kernel does (the chip's node is a link into the port's
    directory), and a recorder that fails on any chip-side config read while the window is open."""
    devices = tmp_path / "devices"
    port = devices / PORT
    chip = port / CHIP
    chip.mkdir(parents=True)
    (port / "config").write_bytes(_port_config(capable=True, link_up=True))
    (port / "vendor").write_text("0x1022\n")
    (chip / "vendor").write_text("0x1e52\n")
    (chip / "current_link_speed").write_text("32.0 GT/s PCIe\n")
    (chip / "current_link_width").write_text("1\n")
    (chip / "max_link_speed").write_text("32.0 GT/s PCIe\n")
    (chip / "max_link_width").write_text("8\n")
    (chip / "config").write_bytes((0x1E52).to_bytes(2, "little") + bytes(254))
    os.symlink(chip, devices / CHIP)
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", devices)

    reads: list = []
    real_read_text, real_open = Path.read_text, builtins.open

    def _chip_side(p: str) -> bool:
        p = os.path.realpath(p)
        name = os.path.basename(p)
        return f"/{CHIP}" in p and (name in pcie_quiet.CONFIG_SPACE_ATTRS or name == "config")

    def read_text(self, *a, **k):
        if _chip_side(str(self)):
            reads.append(str(self))
        return real_read_text(self, *a, **k)

    def fake_open(file, *a, **k):
        if isinstance(file, (str, Path)) and _chip_side(str(file)):
            reads.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(builtins, "open", fake_open)
    return {"devices": devices, "port": port, "chip": chip, "reads": reads}


@contextlib.contextmanager
def _window_of_another_task(owner: str):
    """A window some other task opened (a check, a reset step): the reads here are a poller's, such as /health."""
    pcie_quiet.open_window(owner)
    try:
        yield
    finally:
        pcie_quiet.close_window(owner)


def _warm():
    """One read of everything outside the window, as the check's own host-PCI stage takes."""
    assert hostpci.link_down(CHIP) is False
    assert hostpci.config_vendor_state(CHIP) == hostpci.CONFIG_PRESENT


def _assert_quiet_reads(sysfs, down: bool = False):
    sysfs["reads"].clear()
    assert hostpci.link_down(CHIP) is down
    assert hostpci.config_vendor_state(CHIP) == (hostpci.CONFIG_MISSING if down else hostpci.CONFIG_PRESENT)
    assert sysfs["reads"] == [], f"chip config read inside the window: {sysfs['reads']}"


def test_outside_any_window_the_chip_is_read_live(sysfs):
    assert pcie_quiet.owners() == []
    _warm()
    assert any("current_link_speed" in r for r in sysfs["reads"])
    assert any(r.endswith("/config") for r in sysfs["reads"])


def test_a_check_window_serves_the_cache_and_reads_resume_after_it(sysfs):
    _warm()
    with _window_of_another_task("check"):
        _assert_quiet_reads(sysfs)
        assert pcie_quiet.status()["quiet"] is True and pcie_quiet.status()["why"] == ["check"]
    sysfs["reads"].clear()
    assert hostpci.link_down(CHIP) is False
    assert sysfs["reads"], "reads did not resume once the window closed"


def test_the_fabric_window_type(sysfs):
    _warm()
    with _window_of_another_task("fabric"):
        _assert_quiet_reads(sysfs)


def test_a_reset_step_and_its_settle_are_windows(sysfs, monkeypatch):
    _warm()
    chip_state.set_recovering("op:test", True)
    assert "reset" in pcie_quiet.owners()
    _assert_quiet_reads(sysfs)
    chip_state.set_recovering("op:test", False)
    assert pcie_quiet.owners() == ["settle"]
    _assert_quiet_reads(sysfs)
    ended = chip_state.step_ended_at()
    monkeypatch.setattr(time, "monotonic", lambda: ended + pcie_quiet.SETTLE_SEC + 1)
    assert pcie_quiet.owners() == []


def test_the_job_start_window_ends_by_itself(sysfs, monkeypatch):
    _warm()
    pcie_quiet.open_window("job_start", for_sec=pcie_quiet.JOB_START_SEC)
    _assert_quiet_reads(sysfs)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + pcie_quiet.JOB_START_SEC + 1)
    assert pcie_quiet.owners() == []


def test_a_chip_that_drops_inside_the_window_reads_down_from_its_port(sysfs):
    _warm()
    with _window_of_another_task("fabric"):
        (sysfs["port"] / "config").write_bytes(_port_config(capable=True, link_up=False))
        _assert_quiet_reads(sysfs, down=True)
    assert sysfs["reads"] == []


def test_a_port_that_does_not_report_link_active_falls_back_to_the_cache(sysfs):
    """A port without Data Link Layer Link Active reporting reads the bit as 0 always: it is never taken as down."""
    (sysfs["port"] / "config").write_bytes(_port_config(capable=False, link_up=False))
    _warm()
    with _window_of_another_task("check"):
        _assert_quiet_reads(sysfs, down=False)


def test_with_nothing_cached_the_window_reads_nothing_and_claims_nothing(sysfs):
    (sysfs["port"] / "config").write_bytes(_port_config(capable=False, link_up=False))
    with _window_of_another_task("check"):
        sysfs["reads"].clear()
        assert hostpci.link_down(CHIP) is False
        assert hostpci.config_vendor_state(CHIP) == hostpci.CONFIG_UNREADABLE
        assert sysfs["reads"] == []


def test_the_pcie_snapshot_skips_the_chip_link_inside_the_window(sysfs, monkeypatch):
    class Ep:
        chip, root_port, bus = "0", PORT, 0x41

    monkeypatch.setattr(pcie_aer.pcie_guard, "tt_endpoints", lambda: {CHIP: Ep()})
    monkeypatch.setattr(pcie_aer.pcie_guard, "_pci_dir", lambda: sysfs["devices"])
    with _window_of_another_task("check"):
        sysfs["reads"].clear()
        rec = pcie_aer.snapshot()["0"]
        assert rec["link_skipped"] == "quiet" and "link" not in rec
        assert sysfs["reads"] == []
    assert "link" in pcie_aer.snapshot()["0"]


def test_the_bmc_capture_reads_only_the_bridge_inside_the_window(sysfs):
    assert bmc_capture.lspci_targets([CHIP]) == [PORT, CHIP]
    with _window_of_another_task("fabric"):
        assert bmc_capture.lspci_targets([CHIP]) == [PORT]


def test_health_status_names_the_window_and_the_age_of_what_it_served(sysfs):
    _warm()
    with _window_of_another_task("check"):
        hostpci.link_down(CHIP)
        (sysfs["port"] / "config").write_bytes(_port_config(capable=False, link_up=True))
        hostpci.link_down(CHIP)
        st = pcie_quiet.status()
        assert st["quiet"] is True and st["why"] == ["check"] and st["since"]
        assert st["served_from_cache"] >= 2 and st["oldest_cached_age_sec"] is not None
    assert pcie_quiet.status()["quiet"] is False


@pytest.mark.asyncio
async def test_a_check_pass_opens_the_window_after_its_host_pci_stage(monkeypatch, health_deps):
    m = HealthMonitor(health_deps)
    seen = {}

    def host_pci():
        seen["hostpci"] = pcie_quiet.owners()
        return True, "ok", {}

    async def snapshot(*a, **k):
        seen["snapshot"] = pcie_quiet.owners()
        return True, "ok"

    monkeypatch.setattr(hostpci, "host_pci_verdict", host_pci)
    monkeypatch.setattr(m, "_verify_device", snapshot)
    await m.update("post-job", run_fabric=False)
    assert seen == {"hostpci": [], "snapshot": ["check"]}
    assert pcie_quiet.owners() == []


@pytest.mark.asyncio
async def test_the_fabric_traffic_pass_runs_inside_the_window(monkeypatch, health_deps):
    m = HealthMonitor(health_deps)
    seen = {}

    async def timed(timeout_sec):
        seen["fabric"] = pcie_quiet.owners()
        return True, "healthy"

    monkeypatch.setattr(pci, "chips_off_bus", lambda: [])
    monkeypatch.setattr(m, "_verify_fabric_health_timed", timed)
    assert await m.verify_fabric_health() == (True, "healthy")
    assert seen["fabric"] == ["fabric"] and pcie_quiet.owners() == []


@pytest.mark.asyncio
async def test_a_job_start_opens_the_window_and_its_end_closes_it(monkeypatch):
    from tt_device_mcp import server as srv

    async def snap():
        return {}

    monkeypatch.setattr(srv, "_pcie_snapshot", snap)
    await srv._pcie_job_start("1")
    assert pcie_quiet.owners() == ["job_start"]
    await srv._pcie_job_end("1")
    assert pcie_quiet.owners() == []


def test_the_owner_of_a_scoped_window_keeps_its_own_reads_live_and_others_are_gated(sysfs):
    """A reset step's or a check's own sequential reads stay live (a stale presence answer there could read a dead
    chip); a reader in another task, such as /health, is served from the cache."""
    _warm()
    with pcie_quiet.window("check"):
        sysfs["reads"].clear()
        assert hostpci.link_down(CHIP) is False
        assert sysfs["reads"], "the window's owner was not read live"
        sysfs["reads"].clear()

        # A reader in another task: a fresh context owns no window.
        assert contextvars.Context().run(hostpci.link_down, CHIP) is False
        assert sysfs["reads"] == []


def test_a_reset_step_owns_its_reads_and_pollers_stay_gated(sysfs):
    from tt_device_mcp.health.recovery.base import RecoveryMechanism

    _warm()
    step = RecoveryMechanism.__new__(RecoveryMechanism)
    step._reset_in_flight = False
    step.reset_in_flight = True
    try:
        sysfs["reads"].clear()
        hostpci.link_down(CHIP)
        assert sysfs["reads"], "the step's own read was gated"
        sysfs["reads"].clear()
        assert contextvars.Context().run(hostpci.link_down, CHIP) is False
        assert sysfs["reads"] == []
    finally:
        step.reset_in_flight = False
    assert pcie_quiet.owners() == ["settle"]
