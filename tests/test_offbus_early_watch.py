# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The sampler's early off-bus watch (spec 03 I79): kernel AER counters and the chip node list,
read every second between samples, bring the next sample forward and keep a fast cadence for a
while. The signal decides nothing on its own: the two-sample proof still confirms, and on the
fast cadence the bad reading must also have lasted FAST_CONFIRM_SEC. Everything here runs on a
fake sysfs tree and an injected clock; no device is touched."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import heartbeat, pci

LIVE = [100, 800, 800, 0, 0, 0]
DEAD = [heartbeat.ALL_ONES, 0, 0, 0, 0, 0]


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def watch(monkeypatch):
    """A clean sampler on a fake clock, with the dead-chip and journal side effects captured."""
    clock = _Clock()
    s = srv.sampler
    monkeypatch.setattr(s, "_clock", clock)
    monkeypatch.setattr(s, "fast_until", 0.0)
    monkeypatch.setattr(s, "_link_prev", None)
    monkeypatch.setattr(s, "bad_since", {})
    monkeypatch.setattr(s, "_housekeeping_at", 0.0)
    monkeypatch.setattr(s, "dead_chip_strikes", {})
    monkeypatch.setattr(s, "all_chips_gone_strikes", 0)
    monkeypatch.setattr(s, "short_count_strikes", 0)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    isolated: list = []

    async def fake_isolate(dead):
        isolated.extend(dead)

    monkeypatch.setattr(s, "isolate_dead_chips", fake_isolate)
    events: list = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    return s, clock, isolated, events


def _kinds(events) -> list:
    return [k for k, _ in events]


# --- the signal source -------------------------------------------------------------------------


def test_link_signals_reads_uncorrectable_aer_up_the_chip_path(tmp_path):
    """Endpoint, switch and root port are all read, keyed by address, and only uncorrectable
    counters count: correctable errors are routine on a healthy link."""
    root = tmp_path / "pci" / "0000:00:01.1"
    switch = root / "0000:01:00.0"
    ep = switch / "0000:02:00.0"
    ep.mkdir(parents=True)
    (ep / "aer_dev_correctable").write_text("RxErr 40\nTOTAL_ERR_COR 40\n")
    (ep / "aer_dev_nonfatal").write_text("Undefined 0\nCmpltTO 2\nTOTAL_ERR_NONFATAL 2\n")
    (ep / "aer_dev_fatal").write_text("DLP 0\nTOTAL_ERR_FATAL 0\n")
    (root / "aer_rootport_total_err_nonfatal").write_text("5\n")
    (root / "aer_rootport_total_err_fatal").write_text("1\n")

    c = pci.SYSFS_CLASS_DIR / "tenstorrent!0"
    c.mkdir()
    (c / "device").symlink_to(ep)

    sig = pci.link_signals()
    assert sig["chips"] == ["0"]
    assert sig["aer"]["0000:02:00.0"] == 2, "correctable errors must not count"
    assert sig["aer"]["0000:01:00.0"] is None, "no readable counter is unknown, not zero"
    assert sig["aer"]["0000:00:01.1"] == 6
    assert "pci" not in sig["aer"], "the walk stops at the first non-PCI directory"


def test_link_signals_with_no_class_dir_reads_as_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", tmp_path / "absent")
    assert pci.link_signals() == {"chips": [], "aer": {}}


def test_link_signal_baselines_then_reports_a_rise_or_a_vanished_node(watch):
    s, *_ = watch
    first = {"chips": ["0", "1"], "aer": {"0000:02:00.0": 2, "0000:00:01.1": None}}
    assert s.link_signal(first) is None, "the first read is a baseline, not a rise"
    assert s.link_signal(dict(first)) is None, "nothing moved"

    rose = s.link_signal({"chips": ["0", "1"], "aer": {"0000:02:00.0": 3, "0000:00:01.1": 7}})
    assert rose is not None and "0000:02:00.0" in rose
    assert "0000:00:01.1" not in rose, "an unreadable baseline is not a rise"

    gone = s.link_signal({"chips": ["0"], "aer": {"0000:02:00.0": 3, "0000:00:01.1": 7}})
    assert gone is not None and "1" in gone and "left sysfs" in gone


@pytest.mark.asyncio
async def test_an_aer_rise_brings_the_next_sample_forward(watch, monkeypatch):
    """The watch polls every LINK_WATCH_SEC between samples; a rise arms the fast cadence and the
    wait returns at once, so the off-bus check runs within a second, not up to 10 s later."""
    s, clock, _, events = watch
    reads = iter(
        [
            {"chips": ["0"], "aer": {"0000:00:01.1": 0}},  # baseline
            {"chips": ["0"], "aer": {"0000:00:01.1": 4}},  # the NonFatalErr storm starts
        ]
    )
    monkeypatch.setattr(srv, "link_signals", lambda: next(reads))
    slept: list = []

    async def fake_sleep(sec):
        slept.append(sec)
        clock.t += sec

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    start = clock.t
    await s.wait_for_next_sample()

    assert clock.t - start == 2 * telemetry.LINK_WATCH_SEC, "the wait must end on the signal"
    assert s.fast_active() and s.fast_until == clock.t + telemetry.FAST_WINDOW_SEC
    assert _kinds(events) == ["offbus_watch_armed"]

    # While armed the wait is the fast interval, and a storm journals once per window.
    s.arm_fast("again")
    assert _kinds(events) == ["offbus_watch_armed"], "one event per window, not one per second"
    monkeypatch.setattr(srv, "link_signals", lambda: {"chips": ["0"], "aer": {"0000:00:01.1": 4}})
    start = clock.t
    await s.wait_for_next_sample()
    assert clock.t - start == telemetry.FAST_SAMPLE_SEC


@pytest.mark.asyncio
async def test_a_quiet_link_keeps_the_regular_cadence(watch, monkeypatch):
    s, clock, _, events = watch
    monkeypatch.setattr(srv, "link_signals", lambda: {"chips": ["0"], "aer": {"0000:00:01.1": 3}})

    async def fake_sleep(sec):
        clock.t += sec

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    start = clock.t
    await s.wait_for_next_sample()
    assert clock.t - start == telemetry.SAMPLE_INTERVAL_SEC
    assert not s.fast_active() and events == []


@pytest.mark.asyncio
async def test_a_failing_signal_read_never_stops_the_sampler(watch, monkeypatch):
    s, clock, _, _ = watch

    def boom():
        raise OSError("sysfs went away")

    monkeypatch.setattr(srv, "link_signals", boom)

    async def fake_sleep(sec):
        clock.t += sec

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    await s.wait_for_next_sample()
    assert not s.fast_active()


# --- early detection ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_armed_watch_confirms_a_dead_chip_after_the_confirm_bound(watch):
    """Class B1 (one chip off the bus): with the watch armed, samples 2 s apart confirm the dead
    chip once it has read dead for FAST_CONFIRM_SEC, instead of a full regular interval later."""
    s, clock, isolated, _ = watch
    s.arm_fast("uncorrectable AER errors on 0000:00:01.1")
    bad = {"0": LIVE, "13": DEAD, "31": LIVE}

    await s.check_for_dead_chips(bad)  # t=0: strike 1
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(bad)  # t=2: two samples, but not FAST_CONFIRM_SEC yet
    assert isolated == [], "two fast samples 2 s apart are not the debounce"
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(bad)  # t=4: lasted the bound
    assert isolated == ["13"]


@pytest.mark.asyncio
async def test_an_armed_watch_confirms_a_whole_mesh_drop_after_the_confirm_bound(watch, monkeypatch):
    """Class A (every chip off the bus, an empty sample): the same bound, into the same
    all_chips_off_bus classification and dirty mark as on the regular cadence."""
    s, clock, _, events = watch
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    monkeypatch.setattr(srv, "device_op_active", "")
    s.arm_fast("chip node(s) 0, 1 left sysfs")

    await s.check_for_dead_chips({})
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips({})
    assert "all_chips_off_bus" not in _kinds(events)
    assert srv.fsm.state is ServerState.HEALTHY
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips({})
    assert "all_chips_off_bus" in _kinds(events)
    assert srv.fsm.state is not ServerState.HEALTHY


# --- debounce ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_aer_with_live_heartbeats_never_flags_the_device(watch):
    """A burst of AER errors on a link that keeps working arms the watch and nothing else: the
    heartbeats answer, so no strike, no isolation, no dirty mark, no reset."""
    s, clock, isolated, events = watch
    s.arm_fast("uncorrectable AER errors on 0000:00:01.1")
    healthy = {"0": LIVE, "13": LIVE, "31": LIVE}
    for _ in range(int(telemetry.FAST_WINDOW_SEC // telemetry.FAST_SAMPLE_SEC)):
        await s.check_for_dead_chips(healthy)
        clock.t += telemetry.FAST_SAMPLE_SEC
    assert isolated == []
    assert srv.fsm.state is ServerState.HEALTHY
    assert _kinds(events) == ["offbus_watch_armed"]


@pytest.mark.asyncio
async def test_a_blip_shorter_than_the_confirm_bound_is_not_confirmed(watch, monkeypatch):
    """On the fast cadence two samples come 2 s apart; a chip that reads dead on both and answers
    on the third must not be isolated, and an empty sample that clears the same way must not
    dirty the device. The fast cadence never makes the debounce shorter than FAST_CONFIRM_SEC."""
    s, clock, isolated, events = watch
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "3")
    monkeypatch.setattr(srv, "device_op_active", "")
    s.arm_fast("uncorrectable AER errors on 0000:00:01.1")
    bad = {"0": LIVE, "13": DEAD, "31": LIVE}
    healthy = {"0": LIVE, "13": LIVE, "31": LIVE}

    for sample in (bad, bad, healthy, {}, {}, healthy):
        await s.check_for_dead_chips(sample)
        clock.t += telemetry.FAST_SAMPLE_SEC

    assert isolated == []
    assert srv.fsm.state is ServerState.HEALTHY
    assert "all_chips_off_bus" not in _kinds(events)
    assert s.bad_since == {}, "a cleared reading leaves no stale start time behind"


# --- the lift gate is unchanged ----------------------------------------------------------------


def test_the_fast_cadence_leaves_housekeeping_and_relift_on_the_regular_cadence(watch, monkeypatch):
    """Only the off-bus check gets faster. The idle hold ledger, the idle relift (the only path
    that re-verifies a hold with no job flowing) and the holder scan keep SAMPLE_INTERVAL_SEC,
    so the watch adds no lift attempt and no extra full health check."""
    s, clock, _, _ = watch
    samples, ledger, relift = [], [], []
    monkeypatch.setattr(srv, "chip_sample", lambda: samples.append(clock.t) or {"0": LIVE})
    monkeypatch.setattr(srv, "append_trace", lambda sample: None)
    monkeypatch.setattr(srv, "_refresh_idle_hold_ledger", lambda: ledger.append(clock.t))
    monkeypatch.setattr(srv, "_maybe_spawn_idle_relift", lambda: relift.append(clock.t))
    monkeypatch.setattr(srv, "_maybe_spawn_idle_holder_watch", lambda: None)
    # The counter keeps rising: the watch stays armed for the whole run.
    count = iter(range(1000))
    monkeypatch.setattr(srv, "link_signals", lambda: {"chips": ["0"], "aer": {"0000:00:01.1": next(count)}})

    class _Stop(Exception):
        pass

    end = clock.t + 30

    async def fake_sleep(sec):
        clock.t += sec
        if clock.t >= end:
            raise _Stop()

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(s.run())

    assert len(samples) >= 10, "the armed watch must sample fast"
    assert len(relift) <= 30 // telemetry.SAMPLE_INTERVAL_SEC + 1, "relift must keep the regular cadence"
    assert len(ledger) == len(relift)
    gaps = [b - a for a, b in zip(relift, relift[1:])]
    assert all(g >= telemetry.SAMPLE_INTERVAL_SEC for g in gaps)


@pytest.mark.asyncio
async def test_an_early_detection_lifts_nothing(watch, monkeypatch):
    """The watch only finds a fault sooner. When the chips answer again under the fast cadence,
    the device stays held: only the gate's full health check (fabric included) may lift it."""
    s, clock, _, _ = watch
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "3")
    monkeypatch.setattr(srv, "device_op_active", "")
    s.arm_fast("chip node(s) 0, 13, 31 left sysfs")
    for _ in range(3):
        await s.check_for_dead_chips({})
        clock.t += telemetry.FAST_SAMPLE_SEC
    assert srv.fsm.state is not ServerState.HEALTHY

    healthy = {"0": LIVE, "13": LIVE, "31": LIVE}
    s.arm_fast("still storming")
    for _ in range(10):
        await s.check_for_dead_chips(healthy)
        clock.t += telemetry.FAST_SAMPLE_SEC
    assert srv.fsm.state is not ServerState.HEALTHY, "the sampler must never lift a hold"
