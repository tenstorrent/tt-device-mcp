# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I76: a chip's PCI remove that returns past its bound but did finish is a late completion, not a wedge.

A remove still running at its bound gets a grace. One that returns in it, with the chip's KMD and PCI nodes gone, is
removed: no PCI-wedged latch, no power-cycle request, and the hold keeps its partial off-bus path (the ladder, then the
full check with fabric). Only a writer still blocked at the grace's end latches. Every remove here is gated on an
event the writer's own ``pci_write_slow`` record sets, so a "late" remove returns only after its bound passed, with no
sleep standing in for the kernel.
"""

import asyncio
import threading

import pytest

from tests.test_below_floor_offbus_ladder import GONE, _bound_ladder, _rig
from tests.test_deadchip_detach_before_kill import valve  # noqa: F401 - the fixture
from tests.test_inflight_reader_cut import _World
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import ALL_ONES, pci_writer, read_health_events
from tt_device_mcp.health.pci_writer import PciSysfsWriter, PciWedged


@pytest.fixture
def slow(monkeypatch):
    """Record every writer event; ``slow["past_bound"]`` is set when a write passes its bound."""
    past = threading.Event()
    seen: list = []
    real = pci_writer.health_event

    def record(kind, **fields):
        seen.append((kind, fields))
        if kind == "pci_write_slow":
            past.set()
        real(kind, **fields)

    monkeypatch.setattr(pci_writer, "health_event", record)
    monkeypatch.setattr(pci_writer, "SETTLE_POLL_SEC", 0.01)
    return {"past_bound": past, "events": seen}


def _kinds(slow):
    return [k for k, _ in slow["events"]]


def _late_remove(slow, release=None):
    """A remove that returns True only once its bound passed (or ``release`` is set)."""

    def remove(*_a):
        slow["past_bound"].wait(10)
        if release is not None:
            release.wait(10)
        return True

    return remove


@pytest.mark.asyncio
async def test_a_write_that_returns_in_the_grace_with_its_device_gone_is_a_late_completion(slow):
    w = PciSysfsWriter()
    latched = []
    w.on_latch = lambda reason: latched.append(reason)
    ok = await w.write("remove", _late_remove(slow), timeout=0.05, settle=lambda: True, grace=5.0)
    assert ok is True
    assert not w.latched and latched == [], "a late completion is not a wedge"
    assert _kinds(slow) == ["pci_write_slow", "pci_write_late"]
    late = slow["events"][-1][1]
    assert late["took_s"] >= 0.0 and late["completed_at"] > 0


@pytest.mark.asyncio
async def test_a_write_still_blocked_at_the_end_of_the_grace_latches(slow):
    w = PciSysfsWriter()
    latched = []
    w.on_latch = lambda reason: latched.append(reason)
    kernel = threading.Event()
    try:
        with pytest.raises(PciWedged, match="no answer"):
            await w.write("remove", kernel.wait, 30, timeout=0.05, settle=lambda: True, grace=0.1)
    finally:
        kernel.set()
    assert w.latched and len(latched) == 1, "the blocked writer latches once the grace ends"
    assert "pci_write_late" not in _kinds(slow)


@pytest.mark.asyncio
async def test_a_write_that_returns_late_with_its_device_still_listed_latches(slow):
    w = PciSysfsWriter()
    w.on_latch = None
    with pytest.raises(PciWedged, match="still listed"):
        await w.write("remove", _late_remove(slow), timeout=0.05, settle=lambda: False, grace=0.2)
    assert w.latched


@pytest.mark.asyncio
async def test_a_write_with_no_settle_check_latches_at_its_bound(slow):
    """A rescan or a bridge reset has no proof it finished: no grace, the bound alone."""
    w = PciSysfsWriter()
    w.on_latch = None
    kernel = threading.Event()
    try:
        with pytest.raises(PciWedged):
            await w.write("rescan", kernel.wait, 30, timeout=0.05, grace=5.0)
    finally:
        kernel.set()
    assert w.latched and "pci_write_slow" not in _kinds(slow)


def test_the_watch_threads_write_has_the_same_grace(slow):
    w = PciSysfsWriter()
    w.on_latch = None
    assert w.write_sync("remove", _late_remove(slow), timeout=0.05, settle=lambda: True, grace=5.0) is True
    assert not w.latched and _kinds(slow) == ["pci_write_slow", "pci_write_late"]
    kernel = threading.Event()
    try:
        with pytest.raises(PciWedged, match="no answer"):
            w.write_sync("remove", kernel.wait, 30, timeout=0.05, settle=lambda: True, grace=0.1)
    finally:
        kernel.set()
    assert w.latched


@pytest.mark.asyncio
async def test_the_valve_keeps_a_late_remove_and_asks_for_no_power_cycle(valve, slow, monkeypatch):  # noqa: F811
    """A dead chip's remove that returns past DETACH_TIMEOUT_SEC with its nodes gone: the chip is removed, nothing
    latches, no power cycle is asked for, and the device stays held for its ladder."""
    log, _ = valve
    monkeypatch.setattr(telemetry, "DETACH_TIMEOUT_SEC", 0.05)
    remove = _late_remove(slow)
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: log.append(("detach", idx)) or remove())
    monkeypatch.setattr(srv, "chip_removed", lambda idx, bdf: bdf == "0000:46:00.13")
    asked = []
    monkeypatch.setattr(srv.PCI_WRITER, "on_latch", lambda reason: asked.append(reason))

    await srv.sampler.isolate_dead_chips(["13"])

    assert not srv.PCI_WRITER.latched and asked == [], "a late remove asked for the power cycle"
    assert "13" in srv.sampler.valve_removed
    assert not read_health_events(kinds={"chip_detach_timeout", "chip_isolation_incomplete"})
    assert read_health_events(kinds={"chip_isolated"})[-1]["chips"] == ["13"]
    assert _kinds(slow) == ["pci_write_slow", "pci_write_late"]
    assert srv.fsm.state is not ServerState.HEALTHY, "the device stays held"


@pytest.mark.asyncio
@pytest.mark.parametrize("heals", [True, False], ids=["the ladder heals", "the ladder does not heal"])
async def test_after_a_late_remove_the_hold_runs_its_ladder_and_lifts_only_on_the_full_check(
    monkeypatch, tmp_path, clear_job_state, slow, heals
):
    """The in-flight cut's remove returns late: no power cycle at once. The bound then runs the ladder: a full check
    with fabric that passes ends the hold with no power cycle; one that fails still ends in the power cycle, after
    the ladder."""
    order, _events, counters = _rig(monkeypatch, tmp_path, verdicts=(True,) if heals else ())
    _World(monkeypatch, detach_ok=lambda idx: _late_remove(slow)(), beats={GONE: ALL_ONES})
    monkeypatch.setattr(srv.sampler._deps, "chip_pci_bdf", lambda idx: f"bdf-{idx}")
    monkeypatch.setattr(srv.sampler._deps, "chip_removed", lambda idx, bdf: bdf == f"bdf-{idx}", raising=False)
    monkeypatch.setattr(telemetry, "INFLIGHT_DETACH_TIMEOUT_SEC", 0.05)

    await srv.sampler.cut_post_reset_failed([GONE])
    assert not srv.PCI_WRITER.latched, "a late remove latched PCI-wedged"
    assert "pci_write_late" in _kinds(slow)
    assert order == [], f"nothing ran before the bound: {order}"

    await asyncio.wait_for(_bound_ladder(), 10)
    assert "full check" in order, order
    first_check = order.index("full check")
    assert "power cycle" not in order[:first_check], f"a power cycle came before the ladder: {order}"
    if heals:
        assert "power cycle" not in order, f"a passed full check still power-cycled: {order}"
        assert counters["dirty_cleared"] == 1, "lifted once, on the passing full check"
    else:
        assert order[-1] == "power cycle", f"the bound still escalates when the ladder fails: {order}"
        assert counters["dirty_cleared"] == 0, "no lift without a passed full check"
