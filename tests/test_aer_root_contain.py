# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I112 with 03 I92/I95/I97: during the broker's own check, one uncorrectable AER record on a chip's root
port stops the check readers and fences the chips under that port on the same poll, without waiting for an all-ones
read; the containment writes its trigger-and-stop record before the PCI remove; a chip whose config vendor ID reads
all-ones is never under a fabric check or a starting poller. No device is touched and no process is spawned."""

import time

import pytest

from tests.conftest import fsm_healthy
from tests.test_aer_check_stop import FABRIC, PATHS, _sig, rig  # noqa: F401 - the fixture
from tests.test_aer_stop_contain import _contain, _rows, _sample, watch  # noqa: F401 - the fixture
from tests.test_deadchip_detach_before_kill import valve  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.health import chip_state
from tt_device_mcp.health.monitors import pci


def _poll_after_rise(w, clock, feed, sig_after):
    feed(_sig(0), sig_after)
    assert w.poll_once() is None
    clock.t += telemetry.LINK_WATCH_SEC
    t0 = time.monotonic()
    reason = w.poll_once()
    return reason, time.monotonic() - t0


def test_one_root_port_record_during_the_check_stops_and_fences_within_a_poll(rig):
    """The incident's shape: one uncorrectable record on the root port, no burst, no all-ones read yet."""
    w, clock, log, feed = rig
    assert telemetry.LINK_WATCH_SEC <= 1.0 and telemetry.AER_CHECK_ROOT_STOP_COUNT == 1
    reason, took = _poll_after_rise(w, clock, feed, _sig(1))
    assert reason and "root port" in reason, reason
    assert took < 1.0, f"the stop is on the poll that read the rise: {took:.3f}s"
    stops = [e for e in log if e[0] in ("stop", "kill")]
    assert stops == [("stop", FABRIC)], f"SIGSTOP, no SIGKILL while the chip is attached: {log}"
    assert chip_state.is_fenced("13"), "the sampler reads nothing under that port"
    assert not chip_state.is_fenced("14"), "a chip under another port is untouched"
    assert w.pending_stop and w.pending_stop["chips"] == ["13"]
    assert _rows("check_aer_stop"), "the stop record is on disk once the poll returned (I95)"


def test_a_root_port_record_with_no_broker_check_running_stops_and_fences_nothing(rig, monkeypatch):
    w, clock, log, feed = rig
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    reason, _ = _poll_after_rise(w, clock, feed, _sig(1))
    assert reason is None and not [e for e in log if e[0] in ("stop", "kill")]
    assert not chip_state.is_fenced("13")


def test_an_endpoint_record_below_the_burst_count_stops_nothing_during_the_check(rig):
    w, clock, log, feed = rig
    endpoint = PATHS["13"][0]
    rise = _sig(0)
    rise["aer"] = {**rise["aer"], endpoint: telemetry.AER_STOP_COUNT - 1}
    base = _sig(0)
    base["aer"] = {**base["aer"], endpoint: 0}
    feed(base, rise)
    assert w.poll_once() is None
    clock.t += telemetry.LINK_WATCH_SEC
    assert w.poll_once() is None, "only the root port triggers at one"
    assert not [e for e in log if e[0] in ("stop", "kill")] and not chip_state.is_fenced("13")


@pytest.mark.asyncio
async def test_the_trigger_and_stop_record_is_on_disk_before_the_remove(watch, monkeypatch):
    _w, log, _detach_ok, burst = watch
    journal = srv.sampler._journal_now
    waits = {}

    def recorded(kind, wait=None, **fields):
        ok = journal(kind, wait=wait, **fields)
        log.append(("journal", kind))
        waits[kind] = wait
        return ok

    monkeypatch.setattr(srv.sampler, "_journal_now", recorded)
    _sample("13")
    await _contain(burst)
    trig = log.index(("journal", "chip_contain_trigger"))
    assert trig < log.index(("detach", "13")), f"the record came after the remove: {log}"
    assert log.index(("stop", FABRIC)) < trig, "the SIGSTOP is never behind a disk write"
    assert waits["chip_contain_trigger"] == telemetry.PRE_REMOVE_RECORD_WAIT_SEC
    assert telemetry.PRE_REMOVE_RECORD_WAIT_SEC < telemetry.WATCH_HARD_BOUND_SEC
    row = _rows("chip_contain_trigger")[-1]
    assert row["chips"] == ["13"] and row["readers_stopped"], row


def _fake_chip(monkeypatch, root, idx, bdf):
    dev = root / "pci" / bdf
    dev.mkdir(parents=True)
    (root / "class").mkdir(exist_ok=True)
    (root / "class" / f"tenstorrent!{idx}").mkdir()
    (root / "class" / f"tenstorrent!{idx}" / "device").symlink_to(dev)


def test_chips_off_bus_reads_only_config_vendor_ids(monkeypatch, tmp_path):
    _fake_chip(monkeypatch, tmp_path, "19", "0000:c4:00.0")
    _fake_chip(monkeypatch, tmp_path, "3", "0000:04:00.0")
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", tmp_path / "class")
    read = []

    def vendor(bdf):
        read.append(bdf)
        return 0xFFFF if bdf == "0000:c4:00.0" else 0x1E52

    monkeypatch.setattr(pci, "config_vendor_id", vendor)
    assert pci.chips_off_bus() == ["19"]
    assert sorted(read) == ["0000:04:00.0", "0000:c4:00.0"]


@pytest.mark.asyncio
async def test_a_chip_off_the_bus_is_never_under_the_fabric_check(monkeypatch):
    monkeypatch.setattr(pci, "chips_off_bus", lambda: ["19"])
    quiesced = []
    monkeypatch.setattr(srv._recovery_deps, "quiesce_readers", lambda what, log: quiesced.append(what))

    async def ran(*a, **k):
        raise AssertionError("the fabric check ran over a chip off the bus")

    monkeypatch.setattr(srv.health_monitor, "_verify_fabric_health_timed", ran)
    ok, detail = await srv.health_monitor.verify_fabric_health()
    assert ok is False and "19" in detail and "not run" in detail, detail
    assert not quiesced, "nothing is stopped for a check that does not run"


def test_a_poller_never_starts_over_a_chip_off_the_bus(monkeypatch):
    fsm_healthy(srv)
    monkeypatch.setattr(pci, "chips_off_bus", lambda: [])
    assert srv._pollers_unfit_reason() is None
    monkeypatch.setattr(pci, "chips_off_bus", lambda: ["19"])
    why = srv._pollers_unfit_reason()
    assert why and "19" in why and "all-ones" in why, why
