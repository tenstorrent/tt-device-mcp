# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The host-side PCI usability probe (spec 03).

Every test here stages a fake `/sys/bus/pci/devices` and reads it, rather than mocking the
probe: the whole value of this check is that it parses sysfs correctly, and a mocked parse
proves nothing. The BAR rule in particular is easy to get backwards in a way that reports every
healthy device as broken.
"""

import os

import pytest

from tt_device_mcp.health.monitors import hostpci
from tt_device_mcp.health.monitors import pci as pci_mod

# A healthy 64-bit BAR pair plus a 32-bit BAR, exactly as sysfs renders it: the second slot of a
# 64-bit BAR is all zeros, and so is every BAR the device does not implement.
HEALTHY_RESOURCE = (
    "0x0000202fc0000000 0x0000202fdfffffff 0x000000000014220c\n"
    "0x0000000000000000 0x0000000000000000 0x0000000000000000\n"
    "0x00000000b0e00000 0x00000000b0efffff 0x0000000000040200\n"
    "0x0000000000000000 0x0000000000000000 0x0000000000000000\n"
    "0x0000000000000000 0x0000000000000000 0x0000000000000000\n"
    "0x0000000000000000 0x0000000000000000 0x0000000000000000\n"
)
# BAR0 implemented (flags set) but never placed (start 0) — what a failed assignment looks like.
UNPLACED_RESOURCE = (
    "0x0000000000000000 0x0000000000000000 0x000000000014220c\n"
    "0x0000000000000000 0x0000000000000000 0x0000000000000000\n"
    "0x00000000b0e00000 0x00000000b0efffff 0x0000000000040200\n"
)


def _bus(tmp_path, monkeypatch, devices):
    """Stage a fake PCI bus. `devices` maps BDF -> dict(vendor=, driver=, resource=)."""
    root = tmp_path / "pcidevices"
    root.mkdir()
    drivers = tmp_path / "drivers"
    drivers.mkdir()
    for bdf, spec in devices.items():
        d = root / bdf
        d.mkdir()
        (d / "vendor").write_text(spec.get("vendor", hostpci.TT_VENDOR_ID) + "\n")
        (d / "resource").write_text(spec.get("resource", HEALTHY_RESOURCE))
        driver = spec.get("driver")
        if driver is not None:
            target = drivers / driver
            target.mkdir(exist_ok=True)
            (d / "driver").symlink_to(target)
    monkeypatch.setattr(pci_mod, "PCI_DEVICES_DIR", root)
    return root


def test_a_bus_with_no_tenstorrent_function_has_no_opinion(tmp_path, monkeypatch):
    """SKIPPED, not HEALTHY: an empty bus must never vouch for a device."""
    _bus(tmp_path, monkeypatch, {"0000:00:1f.0": {"vendor": "0x8086", "driver": "ahci"}})

    ok, detail, evidence = hostpci.host_pci_verdict()

    assert ok is None
    assert "no Tenstorrent function" in detail
    assert evidence == {}


def test_every_function_bound_with_placed_bars_is_healthy(tmp_path, monkeypatch):
    _bus(
        tmp_path,
        monkeypatch,
        {
            "0000:31:00.0": {"driver": "tenstorrent"},
            "0000:4b:00.0": {"driver": "tenstorrent"},
        },
    )

    ok, detail, evidence = hostpci.host_pci_verdict()

    assert ok is True, detail
    assert "2/2" in detail
    assert set(evidence["devices"]) == {"0000:31:00.0", "0000:4b:00.0"}


def test_an_unimplemented_bar_is_not_an_unplaced_one(tmp_path, monkeypatch):
    """The rule that is easy to invert.

    A 64-bit BAR's second slot and every BAR the device does not implement read as all zeros. A
    start of zero only means "unplaced" when the flags say there was something to place; judging
    on the address alone reports every healthy device as broken and holds the device.
    """
    _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": "tenstorrent", "resource": HEALTHY_RESOURCE}})

    ok, _detail, _evidence = hostpci.host_pci_verdict()

    assert ok is True, "an all-zero unimplemented BAR was read as unassigned"


def test_an_implemented_but_unplaced_bar_is_unhealthy(tmp_path, monkeypatch):
    """A BAR the kernel could not place makes the chip unreachable, however alive its ARC is."""
    _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": "tenstorrent", "resource": UNPLACED_RESOURCE}})

    ok, detail, evidence = hostpci.host_pci_verdict()

    assert ok is False
    assert "unassigned BAR" in detail and "BAR0" in detail
    assert evidence["devices"]["0000:31:00.0"]["unassigned_bars"] == [0]


def test_a_chip_bound_to_another_driver_is_unhealthy(tmp_path, monkeypatch):
    """vfio-pci or nothing at all: either way tt-kmd cannot drive it, so the broker cannot use it."""
    _bus(
        tmp_path,
        monkeypatch,
        {
            "0000:31:00.0": {"driver": "tenstorrent"},
            "0000:4b:00.0": {"driver": "vfio-pci"},
        },
    )

    ok, detail, _evidence = hostpci.host_pci_verdict()

    assert ok is False
    assert "not bound" in detail and "vfio-pci" in detail and "1/2" in detail


def test_a_chip_with_no_driver_at_all_is_named(tmp_path, monkeypatch):
    """The fault that has no /sys/class/tenstorrent entry, so no other probe can even see it."""
    _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": None}})

    ok, detail, evidence = hostpci.host_pci_verdict()

    assert ok is False
    assert "nothing" in detail
    assert evidence["devices"]["0000:31:00.0"]["driver"] is None


def test_both_faults_are_reported_together(tmp_path, monkeypatch):
    """One pass names everything wrong, so an operator is not fixing these one reset at a time."""
    _bus(
        tmp_path,
        monkeypatch,
        {
            "0000:31:00.0": {"driver": None},
            "0000:4b:00.0": {"driver": "tenstorrent", "resource": UNPLACED_RESOURCE},
        },
    )

    ok, detail, _evidence = hostpci.host_pci_verdict()

    assert ok is False
    assert "not bound" in detail and "unassigned BAR" in detail


def test_an_unreadable_resource_file_is_not_a_fault(tmp_path, monkeypatch):
    """Absent BAR data is not evidence of an unplaced BAR — it is evidence of nothing."""
    root = _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": "tenstorrent"}})
    (root / "0000:31:00.0" / "resource").unlink()

    ok, _detail, _evidence = hostpci.host_pci_verdict()

    assert ok is True


def test_aer_counters_are_evidence_and_never_a_verdict(tmp_path, monkeypatch):
    """A correctable count is a trend, not a state.

    Non-zero is ordinary on a healthy link, so a probe that held the device over one would be
    wrong far more often than right — but the counters still have to be recorded, because the
    documented path from a wedged chip to an ungraceful host reboot runs through them.
    """
    root = _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": "tenstorrent"}})
    (root / "0000:31:00.0" / "aer_dev_correctable").write_text("RxErr 5\nBadTLP 2\nTOTAL_ERR_COR 7\n")

    ok, _detail, evidence = hostpci.host_pci_verdict()

    assert ok is True, "a correctable AER count held the device"
    # 7, not 14: real sysfs ends with the kernel's own TOTAL_ERR_* line, and totalling every line
    # counts it twice — reporting exactly double the errors that happened.
    assert evidence["devices"]["0000:31:00.0"]["aer"]["correctable"] == 7


def test_upstream_bridge_aer_is_recorded(tmp_path, monkeypatch):
    """Errors logged on the bridge never appear on the endpoint, and were not being recorded."""
    root = tmp_path / "pcidevices"
    root.mkdir()
    bridge = root / "0000:30:02.0"
    bridge.mkdir()
    (bridge / "vendor").write_text("0x8086\n")
    (bridge / "aer_dev_fatal").write_text("TOTAL_ERR_FATAL 3\n")
    # The endpoint lives under the bridge, so `..` resolves to it the way real sysfs nests them.
    dev = bridge / "0000:31:00.0"
    dev.mkdir()
    (dev / "vendor").write_text(hostpci.TT_VENDOR_ID + "\n")
    (dev / "resource").write_text(HEALTHY_RESOURCE)
    driver = tmp_path / "tenstorrent"
    driver.mkdir()
    (dev / "driver").symlink_to(driver)
    (root / "0000:31:00.0").symlink_to(dev)
    monkeypatch.setattr(pci_mod, "PCI_DEVICES_DIR", root)

    ok, _detail, evidence = hostpci.host_pci_verdict()

    assert ok is True
    assert evidence["bridges"]["0000:30:02.0"]["aer"]["fatal"] == 3


def test_the_iommu_mode_is_recorded_but_never_alerts(tmp_path, monkeypatch):
    """A passthrough box and a translated box are both legitimate, so this is a fact, not a fault."""
    _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": "tenstorrent"}})

    ok, _detail, evidence = hostpci.host_pci_verdict()

    assert ok is True
    assert isinstance(evidence["iommu"], str) and evidence["iommu"]


@pytest.mark.parametrize("bad", ["", "garbage\n", "0x1 0x2\n", "not hex nope\n"])
def test_malformed_resource_lines_do_not_raise(tmp_path, monkeypatch, bad):
    """This probe runs on a wedged host, where sysfs is exactly where garbage shows up."""
    root = _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": "tenstorrent"}})
    (root / "0000:31:00.0" / "resource").write_text(bad)

    ok, _detail, _evidence = hostpci.host_pci_verdict()

    assert ok is True


def test_the_probe_opens_no_device(tmp_path, monkeypatch):
    """Structural: it must stay a sysfs-only read, since it runs ahead of everything else.

    Asserted by counting this process's own open fds across the call rather than by reading the
    source — a future edit that opens something would pass a source-grep and fail this.
    """
    _bus(tmp_path, monkeypatch, {"0000:31:00.0": {"driver": "tenstorrent"}})
    before = set(os.listdir(f"/proc/{os.getpid()}/fd"))

    hostpci.host_pci_verdict()

    leaked = set(os.listdir(f"/proc/{os.getpid()}/fd")) - before
    assert not leaked, f"the probe left {len(leaked)} fd(s) open"


class TestVersionFloors:
    """Host software below what the broker's probe and reset semantics were matched against.

    Warnings, never a refusal to serve: an old bundle is a configuration gap, not a missing
    recovery capability. Their value is explanatory — below bundle 19.9 the eth link-status
    telemetry is unpopulated, so the eth probe SKIPs, and without this the skip reads as a broken
    probe rather than a host that cannot answer the question.
    """

    @staticmethod
    def _chips(tmp_path, monkeypatch, versions):
        sysfs = tmp_path / "class-tt"
        sysfs.mkdir()
        for idx, ver in versions.items():
            c = sysfs / f"tenstorrent!{idx}"
            c.mkdir()
            (c / "tt_fw_bundle_ver").write_text(ver + "\n")
        monkeypatch.setattr(pci_mod, "SYSFS_CLASS_DIR", sysfs)
        return sysfs

    def test_dotted_versions_compare_element_wise_not_as_strings(self):
        """`19.7.1.0` is below `19.11`. String comparison says the opposite.

        Wrong in the one direction that matters: it would silently clear a host that is below the
        floor, which is the whole thing the floor exists to catch.
        """
        assert hostpci._below_floor("19.7.1.0", "19.11") is True
        assert "19.7.1.0" > "19.11", "the string comparison this avoids"
        assert hostpci._below_floor("19.11", "19.11") is False
        assert hostpci._below_floor("19.12.0", "19.11") is False
        assert hostpci._below_floor("2.10.0", "2.9.0") is False
        assert hostpci._below_floor("2.8.9", "2.9.0") is True

    def test_an_unreadable_version_is_not_a_violation(self, tmp_path, monkeypatch):
        """Absent data is evidence of nothing, and must not be reported as being below a floor."""
        assert hostpci._below_floor("", "19.11") is False
        assert hostpci._below_floor("unknown", "19.11") is False
        monkeypatch.setattr(hostpci, "KMD_VERSION_PATH", tmp_path / "absent")
        self._chips(tmp_path, monkeypatch, {})

        assert hostpci.version_floor_warnings() == []

    def test_firmware_below_the_floor_warns_once_naming_the_eth_consequence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostpci, "KMD_VERSION_PATH", tmp_path / "absent")
        monkeypatch.setattr(hostpci, "FW_BUNDLE_VERSION_FLOOR", "19.11")
        self._chips(tmp_path, monkeypatch, {"0": "19.7.1.0", "1": "19.7.1.0"})

        warns = hostpci.version_floor_warnings()

        assert len(warns) == 1, warns
        assert "2 chip(s)" in warns[0] and "19.7.1.0" in warns[0]
        assert "19.9" in warns[0], "the warning must name why the eth probe cannot answer"

    def test_firmware_at_or_above_the_floor_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostpci, "KMD_VERSION_PATH", tmp_path / "absent")
        monkeypatch.setattr(hostpci, "FW_BUNDLE_VERSION_FLOOR", "19.11")
        self._chips(tmp_path, monkeypatch, {"0": "19.11", "1": "19.12.0.0"})

        assert hostpci.version_floor_warnings() == []

    def test_the_kmd_floor_reads_the_module_version(self, tmp_path, monkeypatch):
        kmd = tmp_path / "kmdver"
        kmd.write_text("2.8.0\n")
        monkeypatch.setattr(hostpci, "KMD_VERSION_PATH", kmd)
        monkeypatch.setattr(hostpci, "KMD_VERSION_FLOOR", "2.9.0")
        self._chips(tmp_path, monkeypatch, {})

        warns = hostpci.version_floor_warnings()

        assert len(warns) == 1 and "tt-kmd 2.8.0" in warns[0] and "2.9.0" in warns[0]

    def test_the_floors_never_spawn_anything(self, tmp_path, monkeypatch):
        """tt-smi's version is deliberately not checked here.

        Reading it means spawning tt-smi, and a boot-time probe that shells the device tool is the
        one thing a wedged host cannot afford. The spawn tripwire would catch a regression, but
        this pins the intent rather than relying on a fixture to notice.
        """
        import subprocess

        monkeypatch.setattr(hostpci, "KMD_VERSION_PATH", tmp_path / "absent")
        self._chips(tmp_path, monkeypatch, {"0": "19.7.1.0"})
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))

        hostpci.version_floor_warnings()

        assert calls == [], "the version floors spawned a subprocess"
