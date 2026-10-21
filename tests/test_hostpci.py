# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The host-side PCI usability probe (spec 03).

Every test here stages a fake `/sys/bus/pci/devices` and reads it, rather than mocking the
probe: the whole value of this check is that it parses sysfs correctly, and a mocked parse
proves nothing. The BAR rule in particular is easy to get backwards in a way that reports every
healthy device as broken.
"""

import json
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
        for attr in ("current_link_speed", "current_link_width"):
            if attr in spec:
                (d / attr).write_text(spec[attr] + "\n")
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


@pytest.mark.parametrize(
    "link, down",
    [
        ({"current_link_speed": "Unknown", "current_link_width": "63"}, True),
        ({"current_link_speed": "Unknown"}, True),
        ({"current_link_speed": "16.0 GT/s PCIe", "current_link_width": "63"}, True),
        ({"current_link_speed": "16.0 GT/s PCIe", "current_link_width": "16"}, False),
        ({"current_link_speed": "2.5 GT/s PCIe", "current_link_width": "1"}, False),  # degraded, not down
        ({}, False),  # unreadable: no evidence
    ],
)
def test_a_link_that_reads_down_fails_the_verdict(tmp_path, monkeypatch, link, down):
    """A dead link reads all-ones from config space: speed Unknown, width 63. Its node is still
    listed and its driver still bound, so only the link read shows it is off the bus."""
    _bus(
        tmp_path,
        monkeypatch,
        {"0000:31:00.0": {"driver": "tenstorrent", **link}, "0000:4b:00.0": {"driver": "tenstorrent"}},
    )
    assert hostpci.link_down("0000:31:00.0") is down
    ok, detail, _ = hostpci.host_pci_verdict()
    assert ok is (not down), detail
    if down:
        assert "PCIe link down on 1/2: 0000:31:00.0" in detail


BLACKHOLE = "0xb140"
WORMHOLE = "0x401e"
GEN5 = "32.0 GT/s PCIe"


def _ports(tmp_path, monkeypatch, links):
    """Stage endpoints nested under their upstream ports, the way real sysfs nests them, so ``..``
    is the port. ``links`` maps endpoint BDF -> dict(port=, port_width=, port_speed=, width=, speed=,
    max_width=, max_speed=, device=); a port attribute left out is not written (unreadable)."""
    root = tmp_path / "pcidevices"
    root.mkdir(exist_ok=True)
    tree = tmp_path / "devices"
    driver = tmp_path / "tenstorrent"
    driver.mkdir(exist_ok=True)
    for bdf, spec in links.items():
        port = tree / spec.get("port", "0000:00:01.1")
        port.mkdir(parents=True, exist_ok=True)
        (port / "vendor").write_text("0x1022\n")
        for key, attr in (("port_width", "max_link_width"), ("port_speed", "max_link_speed")):
            if key in spec:
                (port / attr).write_text(spec[key] + "\n")
        dev = port / bdf
        dev.mkdir()
        (dev / "vendor").write_text(hostpci.TT_VENDOR_ID + "\n")
        (dev / "device").write_text(spec.get("device", BLACKHOLE) + "\n")
        (dev / "resource").write_text(HEALTHY_RESOURCE)
        (dev / "driver").symlink_to(driver)
        for key, attr in (
            ("width", "current_link_width"),
            ("speed", "current_link_speed"),
            ("max_width", "max_link_width"),
            ("max_speed", "max_link_speed"),
        ):
            if key in spec:
                (dev / attr).write_text(spec[key] + "\n")
        (root / bdf).symlink_to(dev)
    monkeypatch.setattr(pci_mod, "PCI_DEVICES_DIR", root)
    return root


def _link(width="8", speed=GEN5, port_width="8", port_speed=GEN5, **kw):
    return {
        "width": width,
        "speed": speed,
        "max_width": "8",
        "max_speed": GEN5,
        "port_width": port_width,
        "port_speed": port_speed,
        **kw,
    }


def test_a_link_x1_by_design_is_healthy(tmp_path, monkeypatch):
    """A Galaxy's x8 chips behind x1 root ports train x1 by design: expected is the lower cap."""
    _ports(
        tmp_path,
        monkeypatch,
        {
            "0000:01:00.0": _link(width="1", port="0000:00:01.1", port_width="1"),
            "0000:02:00.0": _link(width="8", port="0000:00:01.2"),
        },
    )

    ok, detail, evidence = hostpci.host_pci_verdict()

    assert ok is True, detail
    rec = evidence["devices"]["0000:01:00.0"]
    assert rec["link_width_expected"] == 1 and rec["link_width_port_max"] == "1"
    assert evidence["devices"]["0000:02:00.0"]["link_width_expected"] == 8


@pytest.mark.parametrize(
    "link, reason",
    [
        (_link(width="1"), "0000:01:00.0 x1 (expected x8)"),
        (_link(width="4", port_width="16"), "0000:01:00.0 x4 (expected x8)"),  # endpoint cap is the lower
        (_link(speed="2.5 GT/s PCIe"), "0000:01:00.0 2.5 GT/s (expected 32 GT/s)"),
        (_link(width="1", speed="8.0 GT/s PCIe"), "0000:01:00.0 x1 (expected x8), 8 GT/s (expected 32 GT/s)"),
        (_link(speed="16.0 GT/s PCIe", port_speed="16.0 GT/s PCIe"), None),  # a Gen4 port caps it
    ],
)
def test_a_link_below_the_lower_cap_fails_the_verdict(tmp_path, monkeypatch, link, reason):
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": link, "0000:02:00.0": _link(port="0000:00:01.2")})

    ok, detail, _ = hostpci.host_pci_verdict()

    if reason is None:
        assert ok is True, detail
    else:
        assert ok is False
        assert f"PCIe link degraded on 1/2: {reason}" in detail


def test_speed_is_judged_only_where_it_cannot_drop_on_its_own(tmp_path, monkeypatch):
    """A device not known to hold its trained speed is judged on width only; its speed stays
    evidence. Its width is still judged."""
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link(speed="2.5 GT/s PCIe", device=WORMHOLE)})
    ok, detail, evidence = hostpci.host_pci_verdict()
    assert ok is True, detail
    rec = evidence["devices"]["0000:01:00.0"]
    assert rec["link_speed"] == "2.5 GT/s PCIe" and "link_speed_expected" not in rec


def test_width_is_judged_on_a_device_whose_speed_is_not(tmp_path, monkeypatch):
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link(width="2", speed="2.5 GT/s PCIe", device=WORMHOLE)})
    ok, detail, _ = hostpci.host_pci_verdict()
    assert ok is False
    assert "0000:01:00.0 x2 (expected x8)" in detail and "GT/s" not in detail


def test_an_unreadable_port_cap_leaves_the_endpoint_cap(tmp_path, monkeypatch):
    link = _link(width="1")
    del link["port_width"], link["port_speed"]
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": link})
    ok, detail, _ = hostpci.host_pci_verdict()
    assert ok is False and "x1 (expected x8)" in detail


def test_a_link_with_no_readable_reading_or_cap_is_no_evidence(tmp_path, monkeypatch):
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": {}})
    ok, detail, _ = hostpci.host_pci_verdict()
    assert ok is True, detail


def test_a_down_link_is_reported_down_not_degraded(tmp_path, monkeypatch):
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link(width="63", speed="Unknown")})
    ok, detail, _ = hostpci.host_pci_verdict()
    assert ok is False
    assert "PCIe link down on 1/1" in detail and "degraded" not in detail


def _baseline():
    from tt_device_mcp.health import evidence

    path = evidence.health_dir() / hostpci.PCIE_LINK_BASELINE_FILE
    return json.loads(path.read_text())["links"] if path.exists() else None


def _drop_caps(root, bdf):
    dev = root / bdf
    for attr in ("max_link_width", "max_link_speed"):
        (dev / attr).unlink()
        (dev.resolve().parent / attr).unlink()


def test_a_passing_check_banks_each_link_and_a_later_drop_below_it_fails(tmp_path, monkeypatch):
    """With the caps gone, only the banked mark can say the link used to be wider and faster."""
    root = _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link()})
    assert hostpci.host_pci_verdict()[0] is True
    assert _baseline()["0000:01:00.0"]["width"] == 8

    _drop_caps(root, "0000:01:00.0")
    (root / "0000:01:00.0" / "current_link_width").write_text("4\n")
    (root / "0000:01:00.0" / "current_link_speed").write_text("8.0 GT/s PCIe\n")
    # The mark belongs to the link it was taken on: re-key it to these (now unreadable) caps.
    links = _baseline()
    links["0000:01:00.0"].update(width_cap=None, speed_cap=None)
    from tt_device_mcp.health import evidence

    (evidence.health_dir() / hostpci.PCIE_LINK_BASELINE_FILE).write_text(json.dumps({"links": links}))

    ok, detail, _ = hostpci.host_pci_verdict()

    assert ok is False
    assert "x4 (x8 on an earlier passing check), 8 GT/s (32 GT/s on an earlier passing check)" in detail
    assert _baseline()["0000:01:00.0"]["width"] == 8, "a failing check must not bank its reading"


def test_the_bank_only_ratchets_up(tmp_path, monkeypatch):
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link()})
    hostpci.host_pci_verdict()
    from tt_device_mcp.health import evidence

    path = evidence.health_dir() / hostpci.PCIE_LINK_BASELINE_FILE
    links = _baseline()
    links["0000:01:00.0"]["width"] = 16  # a mark above the current reading on the same link
    path.write_text(json.dumps({"links": links}))

    assert hostpci.host_pci_verdict()[0] is False  # judged against the higher mark
    assert _baseline()["0000:01:00.0"]["width"] == 16


def test_a_mark_taken_on_another_link_is_replaced_not_judged(tmp_path, monkeypatch):
    """Chips re-enumerated behind a different port (or a port re-bifurcated) carry a mark that no
    longer describes their link. Judging it would hold a healthy device forever."""
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link(width="1", port="0000:00:01.3", port_width="1")})
    from tt_device_mcp.health import evidence

    path = evidence.health_dir() / hostpci.PCIE_LINK_BASELINE_FILE
    path.write_text(
        json.dumps(
            {
                "links": {
                    "0000:01:00.0": {
                        "port": "0000:00:01.1",
                        "width_cap": 8,
                        "speed_cap": 32.0,
                        "width": 8,
                        "speed": 32.0,
                    }
                }
            }
        )
    )

    ok, detail, _ = hostpci.host_pci_verdict()

    assert ok is True, detail
    assert _baseline()["0000:01:00.0"] == {
        "port": "0000:00:01.3",
        "width_cap": 1,
        "speed_cap": 32.0,
        "width": 1,
        "speed": 32.0,
    }


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"links": 3}'])
def test_an_unreadable_bank_is_no_evidence_and_is_rewritten(tmp_path, monkeypatch, raw):
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link()})
    from tt_device_mcp.health import evidence

    (evidence.health_dir() / hostpci.PCIE_LINK_BASELINE_FILE).write_text(raw)
    ok, detail, _ = hostpci.host_pci_verdict()
    assert ok is True, detail
    assert _baseline()["0000:01:00.0"]["width"] == 8


def test_the_bank_is_not_rewritten_when_nothing_changed(tmp_path, monkeypatch):
    _ports(tmp_path, monkeypatch, {"0000:01:00.0": _link()})
    hostpci.host_pci_verdict()
    from tt_device_mcp.health import evidence

    path = evidence.health_dir() / hostpci.PCIE_LINK_BASELINE_FILE
    before = path.stat().st_mtime_ns
    os.utime(path, ns=(1, 1))
    hostpci.host_pci_verdict()
    assert path.stat().st_mtime_ns == 1 != before
