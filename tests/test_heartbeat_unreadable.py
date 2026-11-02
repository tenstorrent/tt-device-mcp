# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Chips that are in sysfs but whose heartbeat cannot be read are enumerated, not absent.

The case: after a failed mesh reset, 24 of 32 chips keep their ``tenstorrent!N`` class node (with
``tt_heartbeat`` present) but every heartbeat read fails. ``read_heartbeats()`` leaves such chips
out, so anything built on it alone sees zero chips: the startup chip-to-PCI-address map came out
empty (a later bridge reset then has no address to reach), the verdict said "no chips exposed in
sysfs", and the heartbeat detector latched itself off for the life of the process as if the driver
had no counter."""

import pytest

from tests.test_arc_not_ready import _bdf, _fake_sysfs
from tt_device_mcp import server as srv
from tt_device_mcp.health import chip_state
from tt_device_mcp.health import evidence as health
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitors import heartbeat, pci

TRAY_2 = range(8, 16)
ON_BUS = [i for i in range(32) if i not in TRAY_2]


@pytest.fixture
def unreadable_24(monkeypatch, tmp_path):
    """Tray 2 off the bus, the other 24 chips listed in sysfs with every heartbeat read failing."""
    monkeypatch.setattr(heartbeat, "_heartbeat_supported", None)
    monkeypatch.setattr(heartbeat, "_heartbeat_absent_journaled", False)
    return _fake_sysfs(monkeypatch, tmp_path, missing=TRAY_2, eio=ON_BUS)


def test_sysfs_chip_ids_lists_chips_whose_heartbeat_is_unreadable(unreadable_24):
    assert heartbeat.read_heartbeats() == {}
    assert pci.sysfs_chip_ids() == [str(i) for i in ON_BUS]


def test_sysfs_chip_ids_is_empty_without_a_class_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", tmp_path / "absent")
    assert pci.sysfs_chip_ids() == []


def test_startup_pci_map_keeps_every_listed_chips_address(unreadable_24):
    """The map the bridge rung reads holds all 24 listed chips, keyed like the heartbeat read."""
    assert srv._startup_pci_map() == {str(i): _bdf(i) for i in ON_BUS}


def test_startup_pci_map_matches_a_healthy_read(monkeypatch, tmp_path):
    _fake_sysfs(monkeypatch, tmp_path)
    assert set(srv._startup_pci_map()) == set(heartbeat.read_heartbeats())


def test_verdict_names_unreadable_chips_not_an_empty_sysfs(unreadable_24):
    verdict, detail, ev = heartbeat.heartbeat_verdict(32, settle_sec=0)
    assert verdict is Verdict.UNHEALTHY
    assert "no chips exposed" not in detail
    assert detail.startswith("24 chip(s) in sysfs, heartbeat unreadable on 24 ")
    assert ev["in_sysfs"] == 24
    assert ev["heartbeat_unreadable"] == [str(i) for i in ON_BUS]


def test_verdict_names_a_partly_unreadable_mesh(monkeypatch, tmp_path):
    _fake_sysfs(monkeypatch, tmp_path, eio=(3, 4))
    verdict, detail, ev = heartbeat.heartbeat_verdict(32, settle_sec=0)
    assert verdict is Verdict.UNHEALTHY
    assert detail == "32 chip(s) in sysfs, heartbeat unreadable on 2 [3,4], expected 32"
    assert ev["heartbeat_unreadable"] == ["3", "4"]


def test_verdict_names_an_all_enodata_mesh_arc_not_ready(monkeypatch, tmp_path):
    """Every listed chip ENODATA on a bound endpoint: ARC not ready, and only the 8 really
    missing chips are off the bus."""
    _fake_sysfs(monkeypatch, tmp_path, missing=TRAY_2, enodata=ON_BUS)
    verdict, detail, ev = heartbeat.heartbeat_verdict(32, settle_sec=0)
    assert verdict is Verdict.UNHEALTHY
    assert "ARC not ready" in detail and detail.endswith("8 off the bus")
    assert ev["off_bus"] == 8


def test_verdict_still_says_no_chips_on_an_empty_sysfs(monkeypatch, tmp_path):
    _fake_sysfs(monkeypatch, tmp_path, missing=range(32))
    verdict, detail, _ = heartbeat.heartbeat_verdict(32, settle_sec=0)
    assert verdict is Verdict.UNHEALTHY
    assert detail == "no chips exposed in sysfs (driver wedged or chips off the bus)"


def test_verdict_short_read_without_unreadable_chips_keeps_its_detail(monkeypatch, tmp_path):
    _fake_sysfs(monkeypatch, tmp_path, missing=TRAY_2)
    verdict, detail, _ = heartbeat.heartbeat_verdict(32, settle_sec=0)
    assert verdict is Verdict.UNHEALTHY
    assert detail == "24 chip(s) in sysfs, expected 32 (chips dropped off the bus)"


def test_unreadable_counter_does_not_latch_the_detector_off(unreadable_24):
    """The counter exists, the chips do not answer: report unsupported for now, journal why, and
    re-probe, so the detector comes back once the chips do (no broker restart needed)."""
    assert heartbeat.heartbeat_absence_reason() == "attr_unreadable"
    assert heartbeat.heartbeat_supported(refresh=True) is False
    assert heartbeat._heartbeat_supported is None
    events = health.read_health_events(kinds={"heartbeat_unsupported"})
    assert [e["reason"] for e in events] == ["attr_unreadable"], events
    unreadable_24["eio"].clear()
    assert heartbeat.heartbeat_supported() is True


def test_liveness_reason_names_unreadable_chips(unreadable_24, monkeypatch):
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)
    assert srv._device_liveness_reason() == "24 chip(s) in sysfs, heartbeat unreadable on 24"


# --- heartbeat-fence: the class A reads skip a chip the broker fenced (spec 03 I97)


def _reads(monkeypatch):
    """Every heartbeat read that went to a chip, by index."""
    seen = []
    real = heartbeat._read_beat

    def read_beat(node):
        seen.append(node.name.rsplit("!", 1)[-1])
        return real(node)

    monkeypatch.setattr(heartbeat, "_read_beat", read_beat)
    return seen


def _block(how, idx):
    if how == "fenced":
        chip_state.fence([idx], "test")
    else:
        chip_state.mark_lost([idx], "test")
    assert chip_state.read_blocked(idx)


@pytest.mark.parametrize("how", ["fenced", "lost"])
def test_the_class_a_read_never_reads_a_fenced_chip(unreadable_24, monkeypatch, how):
    """A chip the broker fenced (or marked lost) is not read by the class A decision read: a read of a
    dead chip's attribute stalls its PCI remove. It counts as reading all-ones, off the bus: never an
    on-bus survivor, never healthy."""
    _block(how, "3")
    seen = _reads(monkeypatch)
    out = heartbeat.unreadable_on_bus_chips(ON_BUS)
    assert "3" not in seen, f"the fenced chip was read: {seen}"
    assert out == {str(i) for i in ON_BUS} - {"3"}
    assert "3" in heartbeat.offbus_chip_ids({}, 32), "the fenced chip counts off the bus"


def test_the_broker_s_own_reset_step_still_reads_a_fenced_chip(unreadable_24, monkeypatch):
    """The fence gates the pollers, not the broker's own reset step: its reads are ungated."""
    _block("fenced", "3")
    seen = _reads(monkeypatch)
    chip_state.set_recovering("op:reset", True)
    try:
        assert heartbeat.unreadable_on_bus_chips(ON_BUS) == {str(i) for i in ON_BUS}
        assert "3" in seen
    finally:
        chip_state.set_recovering("op:reset", False)


@pytest.mark.parametrize("how", ["fenced", "lost"])
def test_the_absence_reason_never_reads_a_fenced_chip_nor_counts_it_answering(monkeypatch, tmp_path, how):
    """Only chip 0 answers and it is fenced: the probe is not read and does not report the counter
    working (a fenced chip is never counted healthy)."""
    _fake_sysfs(monkeypatch, tmp_path, eio=range(1, 32))
    _block(how, "0")
    seen = _reads(monkeypatch)
    assert heartbeat.heartbeat_absence_reason() == "attr_unreadable"
    assert "0" not in seen, f"the fenced chip was read: {seen}"
