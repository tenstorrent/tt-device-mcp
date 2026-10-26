# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I69: a chip the broker removed is reset before anything re-enumerates it.

The bridge rung used to run a bare PCI rescan after every pass, even when no reset reached a chip
the broker had removed while it was hung. The rescan re-enumerates that chip and the driver probes
it. These tests build a fake sysfs (one bridge per chip) and record the order of setpci writes and
rescans."""

import asyncio

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy, pcie_guard
from tt_device_mcp.health.recovery.stages import bridge_reset as bridge

# chip index -> (its BDF, its bridge's BDF); the bridge's secondary bus is the chip's bus.
CHIPS = {"13": ("0000:46:00.0", "0000:40:01.1"), "14": ("0000:47:00.0", "0000:40:01.2")}


class FakeBus:
    """Fake /sys/bus/pci/devices and /sys/class/tenstorrent. ``trace`` records ("sbr", bridge) for
    each Secondary Bus Reset assert and ("rescan",) for each scoped rescan. A rescan brings back
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

        def rescan():
            self.trace.append(("rescan",))
            for idx in sorted(self.reset_since_remove) if self.come_back else []:
                self.enumerate(idx)
            return ["0000:40:01.0"]

        monkeypatch.setattr(pcie_guard, "rescan_tt_root_ports", rescan)

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
async def test_a_chip_the_bridge_rung_did_not_bring_back_is_not_promised_a_heal_without_a_power_cycle(
    tmp_path, monkeypatch, rung
):
    """The escalation line names what follows honestly: the galaxy reset re-powers the trays, and a chip
    still off the bus after it goes to the power cycle. It never says no power cycle is needed."""
    bus = FakeBus(tmp_path, monkeypatch, come_back=False)
    bus.remove("13")
    srv.isolated_chips = {"13"}
    logs: list = []
    assert await srv.galaxy_recovery._recover_isolated_chips(logs.append) is False
    line = next(m for m in logs if "did NOT come back from a bridge reset" in m)
    assert "no power-cycle required" not in line
    assert "power cycle" in line


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
async def test_a_partial_round_resets_only_the_new_chip_and_the_spent_one_does_not_withhold_the_rescan(
    tmp_path, monkeypatch, rung
):
    """Spec 04 I98 with I69: chip 13 had its SBR round this hold and is still off the bus; chip 14 drops
    later. The round writes no bridge reset to 13, resets 14, and the rescan still runs: 13's bridge
    was already reset this hold, so it does not count as a removed chip left unreset."""
    monkeypatch.setattr(galaxy, "health_event", lambda kind, **f: rung.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-01-01T00:00:00")
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    bus.remove("14")
    srv.isolated_chips = {"13", "14"}
    srv.galaxy_recovery._sbr_hold = None
    srv.galaxy_recovery._note_sbr_fired({"13"})
    assert await srv.galaxy_recovery._sbr(lambda m: None) is False, "13 is still off the bus"
    assert [t for t in bus.trace if t[0] == "sbr"] == [("sbr", "0000:40:01.2")], bus.trace
    assert ("rescan",) in bus.trace, bus.trace
    assert not _kinds(rung, "pci_rescan_withheld")
    assert srv.isolated_chips == {"13"}
    assert pci.removed_chip("14") is None
    assert srv.galaxy_recovery.last_bridge_reset_reasons.get("13") == {"reason": "sbr_spent_this_hold"}


@pytest.mark.asyncio
async def test_no_rescan_for_a_removed_chip_without_privilege(tmp_path, monkeypatch, rung):
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    monkeypatch.setattr(recovery_pkg, "bridge_reset_enabled", lambda: False)
    assert await _run({"13"}) is False
    assert bus.trace == []
    assert _kinds(rung, "pci_rescan_withheld")[0]["reason"] == "removed_chip_not_reset"
    assert srv.isolated_chips == {"13"}


@pytest.mark.asyncio
async def test_no_rescan_when_the_host_reset_gate_refuses(tmp_path, monkeypatch, rung):
    """The gate refused the reset of a removed chip, so it is still hung: no rescan."""
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda n: (False, "too many resets"))
    assert await _run({"13"}) is False
    assert bus.trace == []
    assert _kinds(rung, "pci_rescan_withheld")[0]["reason"] == "host_reset_gated"


@pytest.mark.asyncio
async def test_a_gate_refusal_keeps_the_bare_rescan_for_a_chip_the_broker_did_not_remove(tmp_path, monkeypatch, rung):
    """A gate refusal stops the SBR, not the rescan: with no removed chip in the pass the 3 s rescan
    still runs, as before I69, and a chip whose /dev node only went missing comes back on it."""
    bus = FakeBus(tmp_path, monkeypatch)
    (bus.cls / "tenstorrent!13" / "device").unlink()
    (bus.cls / "tenstorrent!13").rmdir()
    bus.reset_since_remove.add("13")  # the rescan re-creates its node
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda n: (False, "too many resets"))
    assert await _run({"13"}) is True
    assert bus.trace == [("rescan",)], "no SBR past the gate, one bare rescan"
    assert not _kinds(rung, "pci_rescan_withheld")
    assert srv.isolated_chips == set()


@pytest.mark.asyncio
async def test_a_removed_chip_reset_owes_the_aiclk_ceiling(tmp_path, monkeypatch, rung):
    """Spec 03 I35: an SBR clears the firmware clock cap like any reset, including the batched reset
    of a removed chip that returns on the pass's one rescan."""
    owed = []
    monkeypatch.setattr(recovery_pkg.CEILING, "mark_owed", lambda reason: owed.append(reason))
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    assert await _run({"13"}) is True
    assert bus.trace[:2] == [("sbr", "0000:40:01.1"), ("rescan",)], bus.trace
    assert owed == ["bridge reset of chip 13"]


@pytest.mark.asyncio
async def test_the_link_retrains_after_the_last_sbr_before_the_rescan(tmp_path, monkeypatch, rung):
    """PCIe needs the link retrained before config reads: one SBR_RETRAIN_SETTLE_SEC wait follows the
    last SBR release of the batch and precedes the pass's one rescan (not one wait per chip)."""
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    bus.remove("14")
    real_sleep = asyncio.sleep

    async def traced_sleep(delay, *a, **k):
        bus.trace.append(("sleep", delay))
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", traced_sleep)
    assert await _run({"13", "14"}) is True
    first_rescan = bus.trace.index(("rescan",))
    assert bus.trace[first_rescan - 1] == ("sleep", bridge.SBR_RETRAIN_SETTLE_SEC), bus.trace
    assert bridge.SBR_RETRAIN_SETTLE_SEC >= 1.0
    before = bus.trace[:first_rescan]
    assert [t[0] for t in before] == ["sbr", "sbr", "sleep"], before


@pytest.mark.asyncio
async def test_a_removed_chip_rung_stays_inside_its_stated_worst_case(tmp_path, monkeypatch, rung):
    """The I69 time bound for a removed chip that never returns: three SBRs, each followed by the 1 s
    retrain settle and the 10 s re-bind poll, with the 4 s settle between tries, then the 3 s rescan
    the pass ends with (the chip was reset this pass, so it may run): 44.3 s of waits."""
    bus = FakeBus(tmp_path, monkeypatch, come_back=False)
    bus.remove("13")
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 4)
    waited = []
    real_sleep = asyncio.sleep

    async def timed_sleep(delay, *a, **k):
        waited.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", timed_sleep)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: waited.append(s))
    assert await _run({"13"}) is False
    tries = recovery_pkg.BRIDGE_RESET_MAX_TRIES
    assert [t[0] for t in bus.trace].count("sbr") == tries
    bound = tries * (0.1 + bridge.SBR_RETRAIN_SETTLE_SEC + 20 * 0.5) + (tries - 1) * 4 + 3
    assert sum(waited) == pytest.approx(bound) and bound == pytest.approx(44.3), waited


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


# --------------------------------------------------------------------------- record lifetime (I69)


def _restart():
    """What a broker restart does to the records: the process memory is gone, then startup reloads."""
    pci.REMOVED_CHIPS.clear()
    return pci.load_removed_chips()


def test_a_removed_chip_record_survives_a_broker_restart(tmp_path, monkeypatch):
    """A chip the broker removed stays hung and un-reset across a broker restart in the same boot:
    without its record the bare and prelude rescans would re-enumerate it before any reset."""
    bus = FakeBus(tmp_path, monkeypatch)
    boot = tmp_path / "boot_id"
    boot.write_text("boot-a\n")
    monkeypatch.setattr(pci, "BOOT_ID_FILE", boot)
    bus.remove("13")
    assert _restart() == ["13"]
    assert pci.removed_chip("13") == {"bdf": "0000:46:00.0", "bridge": "0000:40:01.1"}
    assert bridge.recorded_bridge("13") == "0000:40:01.1"


def _restart_broker():
    """A broker restart as boot_broker runs it: memory gone, the isolated set rebuilt from sysfs (where
    a removed chip has no node), then the records reloaded and their chips isolated again."""
    pci.REMOVED_CHIPS.clear()
    srv.isolated_chips = set()
    return srv._reload_removed_chips()


def test_boot_broker_reloads_and_isolates_the_removed_chips():
    """boot_broker takes the reload path that isolates the reloaded chips, not the bare load."""
    import inspect

    body = inspect.getsource(srv.boot_broker)
    assert "_reload_removed_chips()" in body and "load_removed_chips()" not in body.replace("_reload_", "")


@pytest.mark.asyncio
async def test_a_reloaded_record_still_withholds_the_bare_rescan(tmp_path, monkeypatch, rung):
    """After a restart the removed chip is isolated again and reset through its recorded bridge before
    the rescan, never re-enumerated un-reset."""
    bus = FakeBus(tmp_path, monkeypatch)
    boot = tmp_path / "boot_id"
    boot.write_text("boot-a\n")
    monkeypatch.setattr(pci, "BOOT_ID_FILE", boot)
    bus.remove("13")
    assert _restart_broker() == ["13"]
    assert srv.isolated_chips == {"13"}, "the reloaded chip is isolated, so the bridge rung targets it"
    assert await srv.galaxy_recovery._recover_isolated_chips(lambda m: None) is True
    assert bus.trace[:2] == [("sbr", "0000:40:01.1"), ("rescan",)], bus.trace
    assert srv.isolated_chips == set() and pci.removed_chip_ids() == []


@pytest.mark.asyncio
async def test_a_pass_for_another_chip_resets_the_reloaded_chip_before_any_rescan(tmp_path, monkeypatch, rung):
    """After a restart, a rung pass asked only for another chip still resets the reloaded chip through
    its recorded bridge first: every rescan is global and would re-enumerate it."""
    bus = FakeBus(tmp_path, monkeypatch, come_back=False)
    boot = tmp_path / "boot_id"
    boot.write_text("boot-a\n")
    monkeypatch.setattr(pci, "BOOT_ID_FILE", boot)
    bus.remove("13")
    pci.REMOVED_CHIPS.clear()
    pci.load_removed_chips()  # a record the isolated set does not name
    assert await _run({"14"}) is False
    first_rescan = bus.trace.index(("rescan",))
    assert ("sbr", "0000:40:01.1") in bus.trace[:first_rescan], bus.trace
    assert "13" in srv.isolated_chips, "a removed chip still off the bus stays isolated"


@pytest.mark.asyncio
async def test_an_unreset_record_outside_the_targets_withholds_the_bare_rescan(tmp_path, monkeypatch, rung):
    """The bare-rescan withhold counts every removal record not reset this pass, not only the pass's
    targets: with the host reset gate closed nothing is reset, so no rescan runs at all."""
    bus = FakeBus(tmp_path, monkeypatch, come_back=False)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda n: (False, "test gate"))
    bus.remove("13")
    assert await _run({"14"}) is False
    assert ("rescan",) not in bus.trace, bus.trace
    (withheld,) = _kinds(rung, "pci_rescan_withheld")
    assert withheld["unreset"] == ["13"]


def test_a_record_from_an_earlier_boot_is_dropped_at_start(tmp_path, monkeypatch):
    """A boot resets every endpoint and may renumber the bus: an earlier boot's record is stale."""
    bus = FakeBus(tmp_path, monkeypatch)
    boot = tmp_path / "boot_id"
    boot.write_text("boot-a\n")
    monkeypatch.setattr(pci, "BOOT_ID_FILE", boot)
    bus.remove("13")
    boot.write_text("boot-b\n")
    assert _restart() == []
    assert pci.removed_chip("13") is None


@pytest.mark.parametrize("how", ["mesh reset", "tray re-power", "power cycle"])
def test_a_record_clears_whatever_brings_the_chip_back(tmp_path, monkeypatch, how):
    """Any path that re-enumerates the chip (not only the bridge rung) drops its record, in memory and
    on disk, so it can neither withhold a later rescan nor aim a bridge reset at live silicon."""
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    bus.remove("14")
    bus.enumerate("13")  # what any of those paths leaves behind: the chip is enumerated again
    assert pci.removed_chip_ids() == ["14"]
    assert pci.removed_chip("13") is None
    assert _restart() == ["14"], "the on-disk record of the returned chip is gone too"


def test_a_record_clears_when_its_address_is_enumerated_again(tmp_path, monkeypatch):
    """A device back at the recorded address, even before its tenstorrent node binds, is not the
    un-enumerated chip the record describes."""
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    bdf, br = CHIPS["13"]
    (bus.devices / bdf).symlink_to(bus.real / br / bdf)
    assert pci.removed_chip("13") is None and pci.removed_chip_ids() == []


@pytest.mark.asyncio
async def test_the_removed_chip_rung_worst_case_for_two_chips(tmp_path, monkeypatch, rung):
    """The N-chip bound: the first pass asserts each chip's SBR (0.1 s each), then one 1 s retrain
    settle and one 10 s re-bind poll before the batched rescan; the retries then run serially per
    chip (4 + 11.1 s, twice: 30.2 s each); then the closing 3 s rescan. 14 + 30.3*N s in all."""
    bus = FakeBus(tmp_path, monkeypatch, come_back=False)
    bus.remove("13")
    bus.remove("14")
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 4)
    waited = []
    real_sleep = asyncio.sleep

    async def timed_sleep(delay, *a, **k):
        waited.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", timed_sleep)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: waited.append(s))
    assert await _run({"13", "14"}) is False
    assert [t[0] for t in bus.trace].count("sbr") == 2 * recovery_pkg.BRIDGE_RESET_MAX_TRIES
    assert sum(waited) == pytest.approx(14 + 30.3 * 2) == pytest.approx(74.6), waited


def test_a_record_is_kept_while_only_its_index_is_reused(tmp_path, monkeypatch):
    """The driver may give the removed chip's index to another chip; the record is about the address,
    so a node with that index and nothing at the recorded address does not clear it."""
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    (bus.cls / "tenstorrent!13").mkdir()
    assert pci.removed_chip_ids() == ["13"]


def test_an_unchanged_reload_does_not_rewrite_the_file(tmp_path, monkeypatch):
    """A broker start that drops no record leaves removed_chips.json as it was."""
    bus = FakeBus(tmp_path, monkeypatch)
    boot = tmp_path / "boot_id"
    boot.write_text("boot-a\n")
    monkeypatch.setattr(pci, "BOOT_ID_FILE", boot)
    bus.remove("13")
    saves = []
    monkeypatch.setattr(pci, "_save_removed_chips", lambda: saves.append(1))
    assert _restart() == ["13"]
    assert saves == []


def test_a_failed_save_never_breaks_recovery(tmp_path, monkeypatch):
    """Persistence is best effort: any error writing the records keeps the in-memory record."""
    bus = FakeBus(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("no state dir")

    monkeypatch.setattr(pci.evidence, "health_dir", boom)
    bus.remove("13")
    assert pci.removed_chip_ids() == ["13"]


@pytest.mark.asyncio
async def test_status_shows_the_bridge_rung_as_a_device_reset(tmp_path, monkeypatch, rung):
    """Spec 04 I90: the bridge rung narrates as a device reset, and /status carries that as a field
    (``device_resetting``, the op row's ``resetting``), so a reader needs no row-text match."""
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("13")
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    seen = []

    def detail(d):
        srv.device_op_detail = d
        seen.append((d, srv._get_queue_status()))

    monkeypatch.setattr(srv, "_set_device_op_detail", detail)
    async with srv._device_op("health-gate/post-job"):
        assert srv._get_queue_status()["device_resetting"] is False, "a held op is not a reset"
        assert await _run({"13"}) is True
    d, status = seen[0]
    assert d.startswith("device reset: bridge reset of chip(s) 13"), d
    assert status["device_resetting"] is True
    assert status["running"][0]["resetting"] is True and status["running"][0]["owner"] == "[broker]"
    assert srv._get_queue_status()["device_resetting"] is False, "over once the op ends"
