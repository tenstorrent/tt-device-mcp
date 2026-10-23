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
from tt_device_mcp.health import chip_state
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
    monkeypatch.setattr(s, "_storm_start", None)
    monkeypatch.setattr(s, "_last_signal", None)
    monkeypatch.setattr(s, "_storm_capped", False)
    monkeypatch.setattr(s, "_cycle_at", 0.0)
    monkeypatch.setattr(s, "scope_seen", False)
    monkeypatch.setattr(s, "dead_chip_strikes", {})
    monkeypatch.setattr(s, "aer_at", {})
    monkeypatch.setattr(s, "_aer_rose_chips", [])
    monkeypatch.setattr(s, "all_chips_gone_strikes", 0)
    monkeypatch.setattr(s, "short_count_strikes", 0)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)

    async def no_pollers(*a, **k):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", no_pollers)
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
    assert pci.link_signals() == {"chips": [], "aer": {}, "paths": {}}


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
    start = s._cycle_at = clock.t
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
    sample_gaps = [b - a for a, b in zip(samples, samples[1:])]
    assert all(g >= telemetry.FAST_SAMPLE_SEC for g in sample_gaps), sample_gaps
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


# --- the fast cadence is bounded ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_arming_edge_never_samples_sooner_than_the_fast_interval(watch, monkeypatch):
    """A signal one poll after a sample arms the watch, and the next sample still waits for
    FAST_SAMPLE_SEC after the last one: the edge brings it forward, never closer than that."""
    s, clock, _, _ = watch
    s._link_prev = {"chips": ["0"], "aer": {"0000:00:01.1": 0}}
    monkeypatch.setattr(srv, "link_signals", lambda: {"chips": ["0"], "aer": {"0000:00:01.1": 9}})

    async def fake_sleep(sec):
        clock.t += sec

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    start = s._cycle_at = clock.t
    await s.wait_for_next_sample()
    assert s.fast_active()
    assert clock.t - start == telemetry.FAST_SAMPLE_SEC


def test_a_sustained_aer_storm_keeps_every_gap_and_is_capped(watch, monkeypatch):
    """A counter that rises on every poll: samples stay at least FAST_SAMPLE_SEC apart (later
    signals never cut a wait short), the fast cadence ends FAST_MAX_SEC after the storm's first
    signal, and the regular cadence runs while the storm goes on."""
    s, clock, _, events = watch
    samples: list = []
    monkeypatch.setattr(srv, "chip_sample", lambda: samples.append(clock.t) or {"0": LIVE})
    monkeypatch.setattr(srv, "append_trace", lambda sample: None)
    monkeypatch.setattr(srv, "_refresh_idle_hold_ledger", lambda: None)
    monkeypatch.setattr(srv, "_maybe_spawn_idle_relift", lambda: None)
    monkeypatch.setattr(srv, "_maybe_spawn_idle_holder_watch", lambda: None)
    count = iter(range(10_000))
    monkeypatch.setattr(srv, "link_signals", lambda: {"chips": ["0"], "aer": {"0000:00:01.1": next(count)}})

    class _Stop(Exception):
        pass

    begin = clock.t
    end = begin + 3 * telemetry.FAST_MAX_SEC

    async def fake_sleep(sec):
        clock.t += sec
        if clock.t >= end:
            raise _Stop()

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(s.run())

    gaps = [(a, b - a) for a, b in zip(samples, samples[1:])]
    assert all(g >= telemetry.FAST_SAMPLE_SEC for _, g in gaps), gaps
    first_signal = begin + 2 * telemetry.LINK_WATCH_SEC  # the first poll is only the baseline
    fast = [a for a, g in gaps if g < telemetry.SAMPLE_INTERVAL_SEC]
    assert fast, "the storm must arm the fast cadence"
    assert max(fast) < first_signal + telemetry.FAST_MAX_SEC, "the fast cadence must end at the cap"
    late = [g for a, g in gaps if a >= first_signal + telemetry.FAST_MAX_SEC]
    assert late and all(g == telemetry.SAMPLE_INTERVAL_SEC for g in late), late
    assert (
        len(samples)
        <= telemetry.FAST_MAX_SEC / telemetry.FAST_SAMPLE_SEC
        + (2 * telemetry.FAST_MAX_SEC / telemetry.SAMPLE_INTERVAL_SEC)
        + 3
    )
    assert _kinds(events) == ["offbus_watch_armed"], "one storm, one event"


def test_a_capped_storm_rearms_only_after_a_quiet_window(watch):
    s, clock, _, events = watch
    assert s.arm_fast("storm")
    t0 = clock.t
    while clock.t < t0 + telemetry.FAST_MAX_SEC:
        clock.t += telemetry.LINK_WATCH_SEC
        s.arm_fast("storm")
    assert not s.fast_active(), "the cap ends the fast cadence while the storm goes on"
    clock.t += telemetry.FAST_WINDOW_SEC - telemetry.LINK_WATCH_SEC
    assert not s.arm_fast("storm"), "a pause shorter than FAST_WINDOW_SEC is the same storm"
    clock.t += telemetry.FAST_WINDOW_SEC
    assert s.arm_fast("new storm"), "a quiet FAST_WINDOW_SEC ends the storm; the next one arms"
    assert s.fast_until == clock.t + telemetry.FAST_WINDOW_SEC
    assert _kinds(events) == ["offbus_watch_armed", "offbus_watch_armed"]


@pytest.mark.asyncio
async def test_a_reset_moves_the_baseline_without_arming(watch, monkeypatch):
    """A reset takes the chips off the bus by design: the AER counters and node list it moves become
    the new baseline, with no offbus_watch_armed event and no fast cadence."""
    s, _, _, events = watch
    reads = iter(
        [
            {"chips": ["0", "1"], "aer": {"0000:00:01.1": 0}},
            {"chips": ["0"], "aer": {"0000:00:01.1": 7}},  # during the reset
            {"chips": ["0", "1"], "aer": {"0000:00:01.1": 7}},  # after it: nothing new
        ]
    )
    monkeypatch.setattr(srv, "link_signals", lambda: next(reads))
    assert not await s.poll_link()
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", True)
    assert not await s.poll_link()
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    assert not await s.poll_link()
    assert not s.fast_active() and events == []


# --- a dead chip is marked lost on the second sample -------------------------------------------


@pytest.mark.asyncio
async def test_two_fast_all_ones_samples_mark_a_chip_lost_before_the_confirm_bound(watch):
    """Under the fast cadence the second all-ones sample marks the chip lost (spec 03 I66), so the
    poll clock does no full read of it again; only the isolation waits for FAST_CONFIRM_SEC. The
    next sample reads its heartbeat once for real, so the proof never rests on a value nobody read."""
    s, clock, isolated, _ = watch
    s.arm_fast("uncorrectable AER errors on 0000:00:01.1")
    bad = {"0": LIVE, "13": DEAD, "31": LIVE}

    await s.check_for_dead_chips(bad)
    assert not chip_state.is_lost("13"), "one sample is not the proof"
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(bad)
    assert chip_state.is_lost("13"), "the second all-ones sample marks it lost"
    assert isolated == [], "the isolation still waits for FAST_CONFIRM_SEC"
    rec = chip_state._lost["13"]
    assert rec["next"] <= rec["read"] + chip_state.PROBE_MIN_SEC, "the next sample reads it for real"
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(bad)
    assert isolated == ["13"]


@pytest.mark.asyncio
async def test_two_fast_blackout_samples_mark_every_chip_lost_before_the_confirm_bound(watch):
    s, clock, isolated, events = watch
    s.arm_fast("uncorrectable AER errors on 0000:00:01.1")
    black = {"0": DEAD, "13": DEAD, "31": DEAD}

    await s.check_for_dead_chips(black)
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(black)
    assert sorted(chip_state.lost_chips(), key=int) == ["0", "13", "31"]
    assert "all_chips_blackout" not in _kinds(events)
    assert srv.fsm.state is ServerState.HEALTHY
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(black)
    assert "all_chips_blackout" in _kinds(events)
    assert isolated == [], "a blackout never isolates"


# --- the confirm bound holds across the end of a fast window --------------------------------------


def _end_window(s, clock, capped: bool) -> None:
    """Arm the watch and put the clock 1 s before the fast window ends: by its natural end
    (FAST_WINDOW_SEC after the last signal) or by the storm cap (FAST_MAX_SEC after the first)."""
    s.arm_fast("uncorrectable AER errors on 0000:00:01.1")
    if capped:
        t0 = clock.t
        while clock.t < t0 + telemetry.FAST_MAX_SEC - telemetry.LINK_WATCH_SEC:
            clock.t += telemetry.LINK_WATCH_SEC
            s.arm_fast("storm")
        assert s.fast_until == t0 + telemetry.FAST_MAX_SEC
    clock.t = s.fast_until - 1.0


_PATHS = {
    # path: (expected chips, the bad sample, the event its verdict journals)
    "per-chip": (3, {"0": LIVE, "13": DEAD, "31": LIVE}, "chip_dead"),
    "blackout": (3, {"0": DEAD, "13": DEAD, "31": DEAD}, "all_chips_blackout"),
    "all-gone": (3, {}, "all_chips_off_bus"),
    "short-count": (3, {"0": LIVE, "31": LIVE}, "chips_missing"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("capped", [False, True], ids=["window-end", "storm-cap"])
@pytest.mark.parametrize("path", list(_PATHS))
async def test_the_confirm_bound_holds_across_the_end_of_a_fast_window(watch, monkeypatch, path, capped):
    """A bad reading that began on a fast sample keeps the FAST_CONFIRM_SEC bound after the window
    ends: the sample after the last fast one still comes FAST_SAMPLE_SEC later, and two samples 2 s
    apart are never the debounce, whatever ended the window (I79: it never goes lower than 4 s)."""
    s, clock, isolated, events = watch
    expected, bad, verdict = _PATHS[path]
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", str(expected))
    monkeypatch.setattr(srv, "device_op_active", "")
    _end_window(s, clock, capped)

    await s.check_for_dead_chips(bad)  # the last fast sample
    clock.t += telemetry.FAST_SAMPLE_SEC
    assert not s.fast_active()
    await s.check_for_dead_chips(bad)  # 2 s later, the window over
    assert isolated == [], "two samples 2 s apart isolated a chip"
    assert verdict not in _kinds(events), "two samples 2 s apart reached a verdict"
    assert srv.fsm.state is ServerState.HEALTHY
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(bad)  # lasted FAST_CONFIRM_SEC
    if path == "per-chip":
        assert isolated == ["13"]
    else:
        assert verdict in _kinds(events)
        assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_pending_chip_past_the_window_still_gets_its_real_read(watch):
    """A chip still inside the bound when the window ends is lost and owes one real heartbeat read,
    as it would inside the window (I66)."""
    s, clock, isolated, _ = watch
    _end_window(s, clock, capped=False)
    bad = {"0": LIVE, "13": DEAD, "31": LIVE}
    await s.check_for_dead_chips(bad)
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips(bad)
    assert chip_state.is_lost("13") and isolated == []
    rec = chip_state._lost["13"]
    assert rec["next"] <= rec["read"] + chip_state.PROBE_MIN_SEC


@pytest.mark.asyncio
async def test_a_reading_that_began_after_the_window_keeps_the_regular_proof(watch):
    """Past the window the regular cadence is the debounce: two samples SAMPLE_INTERVAL_SEC apart
    confirm, as they did before the watch existed."""
    s, clock, isolated, _ = watch
    s.arm_fast("uncorrectable AER errors on 0000:00:01.1")
    clock.t = s.fast_until
    bad = {"0": LIVE, "13": DEAD, "31": LIVE}
    await s.check_for_dead_chips(bad)
    clock.t += telemetry.SAMPLE_INTERVAL_SEC
    await s.check_for_dead_chips(bad)
    assert isolated == ["13"]


def test_a_chip_the_sampler_isolated_arms_nothing_when_its_node_leaves(watch, monkeypatch):
    """Isolating a dead chip removes its node: that removal is the broker's own act, so it moves the
    baseline and is not a new signal. Another chip's node leaving still is."""
    s, _, _, _ = watch
    monkeypatch.setattr(srv, "isolated_chips", {"13"})
    assert s.link_signal({"chips": ["0", "13", "31"], "aer": {}}) is None
    assert s.link_signal({"chips": ["0", "31"], "aer": {}}) is None
    assert s.link_signal({"chips": ["0"], "aer": {}}) == "chip node(s) 31 left sysfs"
