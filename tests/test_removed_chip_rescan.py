# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I69: a chip the broker removed is reset before anything re-enumerates it.

The bridge rung used to run a bare PCI rescan after every pass, even when no reset reached a chip
the broker had removed while it was hung. The rescan re-enumerates that chip and the driver probes
it. These tests build a fake sysfs (one bridge per chip) and record the order of setpci writes and
rescans."""

import asyncio
import pathlib

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy, pcie_guard
from tt_device_mcp.health.recovery.stages import bridge_reset as bridge

RESCAN = "/sys/bus/pci/rescan"
# chip index -> (its BDF, its bridge's BDF); the bridge's secondary bus is the chip's bus.
CHIPS = {"13": ("0000:46:00.0", "0000:40:01.1"), "14": ("0000:47:00.0", "0000:40:01.2")}


class FakeBus:
    """Fake /sys/bus/pci/devices and /sys/class/tenstorrent. ``trace`` records ("sbr", bridge) for
    each Secondary Bus Reset assert and ("rescan",) for each global rescan. A rescan brings back
    every chip whose bridge was reset since its remove (``come_back`` False: none)."""

    def __init__(self, tmp_path, monkeypatch, come_back=True):
        self.devices = tmp_path / "devices"
        self.real = tmp_path / "real"
        self.cls = tmp_path / "class"
        for d in (self.devices, self.real, self.cls):
            d.mkdir()
        self.trace, self.come_back, self.reset_since_remove = [], come_back, set()
        monkeypatch.setattr(pci, "PCI_DEVICES_DIR", self.devices)
        monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", self.cls)
        for idx, (bdf, br) in CHIPS.items():
            (self.real / br / bdf).mkdir(parents=True)
            (self.devices / br).symlink_to(self.real / br)
            (self.real / br / "secondary_bus_number").write_text(str(int(bdf[5:7], 16)) + "\n")
            self.enumerate(idx)
        monkeypatch.setattr(bridge.subprocess, "run", self.setpci)
        monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
        real_write = pathlib.Path.write_text

        def write_text(path, data, *a, **k):
            if str(path) == RESCAN:
                self.trace.append(("rescan",))
                for idx in sorted(self.reset_since_remove) if self.come_back else []:
                    self.enumerate(idx)
                return len(data)
            return real_write(path, data, *a, **k)

        monkeypatch.setattr(pathlib.Path, "write_text", write_text)

    def enumerate(self, idx):
        bdf, br = CHIPS[idx]
        if not (self.devices / bdf).exists():
            (self.devices / bdf).symlink_to(self.real / br / bdf)
        node = self.cls / f"tenstorrent!{idx}"
        node.mkdir(exist_ok=True)
        if not (node / "device").exists():
            (node / "device").symlink_to(self.real / br / bdf)
        self.reset_since_remove.discard(idx)

    def remove(self, idx):
        """isolate_chip, then what the kernel does on the remove write."""
        assert pci.isolate_chip(idx) is True
        bdf, _ = CHIPS[idx]
        (self.devices / bdf).unlink()
        (self.cls / f"tenstorrent!{idx}" / "device").unlink()
        (self.cls / f"tenstorrent!{idx}").rmdir()

    def setpci(self, argv, *a, **k):
        br, arg = argv[2], argv[-1]
        if arg == "BRIDGE_CONTROL":
            return bridge.subprocess.CompletedProcess(argv, 0, stdout="0000", stderr="")
        if arg.endswith("=0040"):
            self.trace.append(("sbr", br))
            self.reset_since_remove |= {i for i, (_, b) in CHIPS.items() if b == br}
        return bridge.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture
def rung(monkeypatch):
    """The bridge rung with root+setpci, the host reset gate open and no real waits."""
    events = []

    def record(kind, **f):
        events.append((kind, f))

    for mod in (recovery_pkg, bridge):
        monkeypatch.setattr(mod, "health_event", record)
    monkeypatch.setattr(recovery_pkg, "bridge_reset_enabled", lambda: True)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda n: (True, ""))
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    srv.device_pci_map = {i: bdf for i, (bdf, _) in CHIPS.items()}
    return events


def _kinds(events, kind):
    return [f for k, f in events if k == kind]


async def _run(chips):
    srv.isolated_chips = set(chips)
    return await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)


def test_isolate_records_the_parent_bridge_before_the_remove(tmp_path, monkeypatch):
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    assert pci.removed_chip("13") == {"bdf": "0000:46:00.0", "bridge": "0000:40:01.1"}
    # Its node is gone, so the parent walk alone finds no bridge any more: the record is the only way.
    assert pci.parent_bridge("0000:46:00.0") is None
    assert bridge.recorded_bridge("13") == "0000:40:01.1"


def test_a_failed_remove_leaves_no_record(tmp_path, monkeypatch):
    FakeBus(tmp_path, monkeypatch)
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(pci.evidence, "health_event", lambda *a, **k: None)
    assert pci.isolate_chip("13") is False
    assert pci.removed_chip("13") is None


@pytest.mark.asyncio
async def test_a_removed_chip_is_reset_through_its_recorded_bridge_then_rescanned(tmp_path, monkeypatch, rung):
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    assert await _run({"13"}) is True
    assert bus.trace[:2] == [("sbr", "0000:40:01.1"), ("rescan",)], bus.trace
    assert srv.isolated_chips == set()
    assert pci.removed_chip("13") is None, "a chip back on the bus keeps no removal record"
    assert not _kinds(rung, "pci_rescan_withheld")


@pytest.mark.asyncio
async def test_a_removed_chip_that_does_not_return_is_retried_through_the_same_bridge(tmp_path, monkeypatch, rung):
    bus = FakeBus(tmp_path, monkeypatch, come_back=False)
    bus.remove("13")
    assert await _run({"13"}) is False
    sbrs = [t for t in bus.trace if t[0] == "sbr"]
    assert len(sbrs) == recovery_pkg.BRIDGE_RESET_MAX_TRIES, bus.trace
    assert bus.trace[0][0] == "sbr", "the first rescan of the pass follows a reset"
    assert srv.isolated_chips == {"13"}


@pytest.mark.asyncio
async def test_every_removed_chip_is_reset_before_the_first_rescan(tmp_path, monkeypatch, rung):
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    bus.remove("14")
    assert await _run({"13", "14"}) is True
    first_rescan = bus.trace.index(("rescan",))
    assert {t[1] for t in bus.trace[:first_rescan]} == {"0000:40:01.1", "0000:40:01.2"}, bus.trace


@pytest.mark.parametrize("stale", ["bridge_gone", "bridge_renumbered", "bus_occupied"])
@pytest.mark.asyncio
async def test_a_stale_recorded_bridge_is_refused(tmp_path, monkeypatch, rung, stale):
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    br = "0000:40:01.1"
    if stale == "bridge_gone":
        (bus.devices / br).unlink()
    elif stale == "bridge_renumbered":
        (bus.real / br / "secondary_bus_number").write_text("99\n")
    else:
        (bus.devices / "0000:46:00.1").mkdir()  # live silicon now on the chip's old bus
    assert await _run({"13"}) is False
    assert bus.trace == [], "no reset through a stale bridge, and no rescan of the un-reset chip"
    assert [f["reason"] for f in _kinds(rung, "recorded_bridge_refused")] == [stale]
    assert srv.galaxy_recovery.last_bridge_reset_reasons == {"13": {"reason": "no_bridge"}}
    assert _kinds(rung, "pci_rescan_withheld")[0]["reason"] == "removed_chip_not_reset"
    assert srv.isolated_chips == {"13"}


@pytest.mark.asyncio
async def test_no_rescan_when_a_removed_chip_has_no_usable_bridge(tmp_path, monkeypatch, rung):
    """One removed chip cannot be reset: any rescan this pass, including the one inside another
    chip's bridge reset, would re-enumerate it hung. So none runs; both hold and escalate."""
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    bus.remove("14")
    (bus.devices / "0000:40:01.2").unlink()
    assert await _run({"13", "14"}) is False
    assert ("rescan",) not in bus.trace, bus.trace
    assert srv.galaxy_recovery.last_bridge_reset_reasons == {
        "13": {"reason": "rescan_withheld"},
        "14": {"reason": "no_bridge"},
    }
    withheld = _kinds(rung, "pci_rescan_withheld")
    assert withheld and withheld[0]["unreset"] == ["14"]
    assert srv.isolated_chips == {"13", "14"}


@pytest.mark.asyncio
async def test_no_rescan_for_a_removed_chip_without_privilege(tmp_path, monkeypatch, rung):
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    monkeypatch.setattr(recovery_pkg, "bridge_reset_enabled", lambda: False)
    assert await _run({"13"}) is False
    assert bus.trace == []
    assert _kinds(rung, "pci_rescan_withheld")[0]["reason"] == "removed_chip_not_reset"
    assert srv.isolated_chips == {"13"}


@pytest.mark.parametrize("removed", [True, False])
@pytest.mark.asyncio
async def test_no_rescan_when_the_host_reset_gate_refuses(tmp_path, monkeypatch, rung, removed):
    bus = FakeBus(tmp_path, monkeypatch)
    if removed:
        bus.remove("13")
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda n: (False, "too many resets"))
    assert await _run({"13"}) is False
    assert bus.trace == []
    assert _kinds(rung, "pci_rescan_withheld")[0]["reason"] == "host_reset_gated"


@pytest.mark.asyncio
async def test_the_bare_rescan_stays_for_a_chip_the_broker_did_not_remove(tmp_path, monkeypatch, rung):
    """A chip whose node vanished without a broker remove (measured: still bound, only its /dev node
    gone) keeps the cheap rescan: nothing says it is hung."""
    bus = FakeBus(tmp_path, monkeypatch)
    (bus.cls / "tenstorrent!13" / "device").unlink()
    (bus.cls / "tenstorrent!13").rmdir()
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: None)
    assert await _run({"13"}) is False
    assert ("rescan",) in bus.trace
    assert not _kinds(rung, "pci_rescan_withheld")


def test_a_returned_chip_drops_its_removal_record(tmp_path, monkeypatch):
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    monkeypatch.setattr(recovery_base, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "returned_chips", lambda ids: ["13"])
    isolated = {"13"}
    recovery_base.drop_returned_isolated(isolated, "test")
    assert isolated == set() and pci.removed_chip("13") is None


@pytest.mark.asyncio
async def test_gate_bridge_stage_verifies_with_fabric_before_clearing_dirty(monkeypatch):
    """Goal of 03 I4 on this rung: a chip back from the bridge reset passes the full check, fabric
    included, before the dirty mark clears."""
    order = []

    async def recovered(log):
        return True

    async def verify(expected, log, run_fabric=True, **_):
        order.append(("verify", run_fabric))
        return True, {"fabric": {"ok": True}}

    monkeypatch.setattr(srv.galaxy_recovery, "_recover_isolated_chips", recovered)
    monkeypatch.setattr(srv.galaxy_recovery, "_verify_device", verify)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **kw: order.append(("clear", kw.get("verified"))))
    monkeypatch.setattr(galaxy, "health_event", lambda *a, **k: None)
    out = await srv.galaxy_recovery._fire_gate_rung(
        "post-job", galaxy.STAGE_BRIDGE_RESET, ["13"], 32, lambda m: None, ev=None, beats=None
    )
    assert out == galaxy.OUTCOME_RECOVERED
    assert order == [("verify", True), ("clear", True)]


@pytest.mark.parametrize("rung_ok", [True, False])
@pytest.mark.asyncio
async def test_gate_bridge_stage_failed_verify_holds(monkeypatch, rung_ok):
    """A rung that did not bring the chips back, or a failed check after it, holds the device
    (WAITING escalates through the next rungs) and never clears the dirty mark."""
    cleared = []

    async def recovered(log):
        return rung_ok

    async def verify(expected, log, run_fabric=True, **_):
        return False, {"fabric": {"ok": False}}

    monkeypatch.setattr(srv.galaxy_recovery, "_recover_isolated_chips", recovered)
    monkeypatch.setattr(srv.galaxy_recovery, "_verify_device", verify)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **kw: cleared.append(kw))
    monkeypatch.setattr(galaxy, "health_event", lambda *a, **k: None)
    out = await srv.galaxy_recovery._fire_gate_rung(
        "post-job", galaxy.STAGE_BRIDGE_RESET, ["13"], 32, lambda m: None, ev=None, beats=None
    )
    assert out == galaxy.OUTCOME_WAITING
    assert cleared == []
