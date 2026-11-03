# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A chip that leaves the bus is contained within 5 s of its first evidence (spec 03 I79, I86, I99).

The sampler's own path is driven end to end on a fake clock: the early watch's polls, the samples, the
two-sample proof and the real isolate_dead_chips, with the reader stop and the PCI remove stubbed. A
synchronous journal write costs FSYNC_SEC of clock (an fsync under an AER storm) and the remove costs
REMOVE_SEC (the kernel's own remove of an off-bus endpoint), so the test sees where every second goes.
No device is touched."""

from __future__ import annotations

import types

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.health import chip_state, evidence
from tt_device_mcp.health.monitors import heartbeat



def _live(t: float) -> list:
    """A live chip's reading: its ARC heartbeat ticks (~10 Hz), so it never reads as hung (I66)."""
    return [100 + int(t * 10), 800, 800, 0, 0, 0]


DEAD = [heartbeat.ALL_ONES, 0, 0, 0, 0, 0]
PORT = "0000:00:01.1"
FABRIC = 9999
WALL0 = 1_800_000_000.0
FSYNC_SEC = 0.5
REMOVE_SEC = 2.4
BUDGET_SEC = 5.0


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rig(monkeypatch):
    clock = _Clock()
    s = srv.sampler
    for name, fresh in (
        ("_clock", clock),
        ("fast_until", 0.0),
        ("_link_prev", None),
        ("bad_since", {}),
        ("_housekeeping_at", 0.0),
        ("_storm_start", None),
        ("_last_signal", None),
        ("_storm_capped", False),
        ("_cycle_at", 0.0),
        ("scope_seen", False),
        ("dead_chip_strikes", {}),
        ("prev_beats", {}),
        ("aer_at", {}),
        ("_aer_rose_chips", []),
        ("_aer_edge", False),
        ("_step_seen_at", None),
        ("all_chips_gone_strikes", 0),
        ("short_count_strikes", 0),
        ("valve_runs", []),
        ("last_sample_all_ones", []),
        ("valve_detaching", set()),
        ("valve_removed", set()),
    ):
        monkeypatch.setattr(s, name, fresh, raising=False)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: f"0000:46:00.{idx}")
    # The sampler's own times run on the fake clock: monotonic for the gaps, wall for the record.
    monkeypatch.setattr(telemetry, "time", types.SimpleNamespace(monotonic=clock, time=lambda: WALL0 + clock.t))

    log: list = []  # (what, clock)

    def sync_event(kind, **f):
        log.append((f"fsync:{kind}", clock.t))
        clock.t += FSYNC_SEC

    def detached_event(kind, **f):
        log.append((f"queued:{kind}", clock.t))

    for mod in (srv, evidence):
        monkeypatch.setattr(mod, "health_event", sync_event)
        monkeypatch.setattr(mod, "health_event_detached", detached_event)
    monkeypatch.setattr(srv, "health_event_flushed", lambda kind, timeout, **f: sync_event(kind, **f) or True)

    def freeze(reason):
        log.append(("sigstop", clock.t))
        return [FABRIC]

    def remove(idx):
        log.append(("remove_start", clock.t))
        clock.t += REMOVE_SEC
        log.append(("remove_end", clock.t))
        return True

    async def no_holders(reason):
        log.append(("kill", clock.t))
        return []

    async def no_pollers(*a, **k):
        return []

    monkeypatch.setattr(srv, "_freeze_device_readers", freeze)
    monkeypatch.setattr(srv, "isolate_chip", remove)
    monkeypatch.setattr(srv, "_kill_device_holders", no_holders)
    monkeypatch.setattr(srv, "_set_device_pollers", no_pollers)

    async def fake_sleep(sec):
        clock.t += sec

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    return s, clock, log


async def _run(s, clock, log, sample_at, link_at, limit=60.0) -> None:
    """The sampler loop's own order (sample, check, wait), until the remove ends or ``limit`` passes."""
    srv_link = srv.link_signals
    srv.link_signals = lambda: link_at(clock.t)
    s._link_prev = link_at(clock.t)  # the watch has run before: its baseline is the healthy link
    try:
        start = clock.t
        while clock.t - start < limit and not any(w == "remove_end" for w, _ in log):
            s._cycle_at = clock.t
            log.append(("sample", clock.t))
            await s.check_for_dead_chips(sample_at(clock.t), telemetry.time.time())
            if any(w == "remove_end" for w, _ in log):
                break
            await s.wait_for_next_sample()
    finally:
        srv.link_signals = srv_link


def _at(log, what) -> float:
    return next(t for w, t in log if w == what)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["all-ones", "aer-burst"])
async def test_a_chip_off_the_bus_is_removed_within_5_s_of_its_first_evidence(rig, first):
    """All-ones first (no uncorrectable AER, nothing arms the watch): the first all-ones sample arms the fast
    cadence and the second one, FAST_SAMPLE_SEC later, is the proof. AER first: the rise on the chip's own
    path samples at once, and that sample's all-ones read is the proof (I86). Either way the readers stop
    at the trigger and the remove follows with no fsync in between (I99)."""
    s, clock, log = rig
    t0 = clock.t
    # Just after a sample began: the AER edge's poll comes before FAST_SAMPLE_SEC since that cycle, so a floor
    # there would hold the proving sample back; the all-ones drop waits for the next regular sample.
    drop = t0 + 0.3
    aer = first == "aer-burst"

    def sample_at(t):
        return {"0": _live(t), "13": DEAD if t >= drop else _live(t), "31": _live(t)}

    def link_at(t):
        return {
            "chips": ["0", "13", "31"],
            "aer": {PORT: 4 if aer and t >= drop else 0},
            "paths": {"13": [PORT]},
        }

    await _run(s, clock, log, sample_at, link_at)

    samples = [t for w, t in log if w == "sample"]
    first_all_ones = next(t for t in samples if t >= drop)
    evidence_at = drop if aer else first_all_ones  # the counter rose at the drop; the read saw it at a poll
    # The trigger is the confirming sample; the stop is the first thing the containment does after it.
    trigger, stop = samples[-1], _at(log, "sigstop")
    removed = _at(log, "remove_end")

    if aer:
        assert first_all_ones - drop <= telemetry.LINK_WATCH_SEC, "the AER rise samples on its own poll"
        assert trigger == first_all_ones, "one all-ones read after the chip's own AER rise is the proof"
    else:
        assert trigger - evidence_at == telemetry.FAST_SAMPLE_SEC, "the proof stands at the second fast sample"
        assert len([t for t in samples if t >= drop]) == 2, "two reads, never one"
    assert stop - trigger == 0, f"the readers stop at the trigger, with no fsync before them: {log}"
    assert _at(log, "remove_start") == stop, f"no blocking I/O between the stop and the remove: {log}"
    assert removed - evidence_at <= BUDGET_SEC, f"first evidence to remove {removed - evidence_at:.1f}s: {log}"
    assert not [w for w, t in log if w.startswith("fsync:") and evidence_at <= t < removed], log
    assert chip_state.is_lost("13")

    assert any(w == "fsync:chip_contained" for w, _ in log), "the containment record follows the remove"
    assert _at(log, "fsync:chip_contained") >= removed


@pytest.mark.asyncio
async def test_a_lone_all_ones_read_that_clears_on_the_next_sample_is_never_contained(rig):
    """The shorter bound keeps the two-sample proof: a single all-ones read is a blip."""
    s, clock, log = rig
    blip = clock.t + 3.3

    def sample_at(t):
        return {"0": _live(t), "13": DEAD if blip <= t < blip + 7.0 else _live(t), "31": _live(t)}

    def link_at(t):
        return {"chips": ["0", "13", "31"], "aer": {PORT: 0}, "paths": {"13": [PORT]}}

    # One regular sample sees it, the next fast one does not.
    await _run(s, clock, log, sample_at, link_at, limit=30.0)
    dead_reads = [t for w, t in log if w == "sample" and blip <= t < blip + 7.0]
    assert len(dead_reads) == 1, log
    assert not any(w == "sigstop" for w, _ in log)
    assert not chip_state.is_lost("13")
