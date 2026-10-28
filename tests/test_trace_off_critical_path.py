# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I98: the sampler's dead-chip check never waits on the durable trace write, and a chip's
all-ones evidence is stamped at the sample's read time. Under an AER storm a trace fsync can stall
for seconds; the poller stop, the fast cadence and the containment record's first evidence must not
move with it. Fake sysfs, fake fsync; no device is touched."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tests.test_offbus_early_watch import DEAD, LIVE, watch  # noqa: F401
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.health import chip_state, evidence

STALL_SEC = 3.0


class _Stop(Exception):
    pass


@pytest.fixture
def stalled_trace(monkeypatch):
    """The real trace path (server wiring -> evidence.append_trace -> its writer thread) with the
    trace file's fsync stalled for up to STALL_SEC; other journal writes are untouched."""
    release = threading.Event()
    rows: list = []
    real = evidence._append_durable

    def durable(path, rec, max_bytes):
        if path.name == evidence.TRACE_FILE:
            release.wait(STALL_SEC)
            rows.append(rec)
            return True
        return real(path, rec, max_bytes)

    monkeypatch.setattr(evidence, "_append_durable", durable)
    yield release, rows
    release.set()


def _cycles(monkeypatch, s, samples):
    """Run the sampler loop over ``samples``, one per cycle, then stop it."""
    left = list(samples)
    monkeypatch.setattr(srv, "chip_sample", lambda: left.pop(0))
    monkeypatch.setattr(srv, "link_signals", lambda: {"chips": ["0", "13"], "aer": {}})
    monkeypatch.setattr(srv, "_refresh_idle_hold_ledger", lambda: None)
    monkeypatch.setattr(srv, "_maybe_spawn_idle_relift", lambda: None)
    monkeypatch.setattr(srv, "_maybe_spawn_idle_holder_watch", lambda: None)

    async def fake_sleep(sec):
        s._clock.t += sec
        if not left:
            raise _Stop()

    monkeypatch.setattr(telemetry.asyncio, "sleep", fake_sleep)
    t0 = time.monotonic()
    with pytest.raises(_Stop):
        asyncio.run(s.run())
    return time.monotonic() - t0


def test_a_stalled_trace_fsync_does_not_delay_the_fast_watch(watch, stalled_trace, monkeypatch):  # noqa: F811
    """A chip's first all-ones read arms the fast cadence (and journals offbus_watch_armed) and runs
    the poller-stop step while that sample's trace row still waits on its fsync."""
    s, _clock, _, events = watch
    release, rows = stalled_trace
    at_arm, at_stop = [], []
    arm, stop = s.arm_fast, s.stop_pollers_for_hung

    def spy_arm(reason):
        at_arm.append(len(rows))
        return arm(reason)

    async def spy_stop(sample, dead):
        at_stop.append(len(rows))
        return await stop(sample, dead)

    monkeypatch.setattr(s, "arm_fast", spy_arm)
    monkeypatch.setattr(s, "stop_pollers_for_hung", spy_stop)
    took = _cycles(monkeypatch, s, [{"0": LIVE, "13": DEAD}])
    assert took < STALL_SEC / 2, f"the sampler cycle waited {took:.1f}s on the trace fsync"
    assert at_stop == [0], "the poller-stop step ran only after the trace row was on disk"
    assert at_arm == [0], "the fast cadence armed only after the trace row was on disk"
    assert s.fast_active()
    assert "offbus_watch_armed" in [k for k, _ in events]
    assert "pollers_quiesce_deferred" in [k for k, _ in events]
    release.set()
    deadline = time.monotonic() + 5
    while not rows and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rows and rows[0]["chips"] == {"0": LIVE, "13": DEAD}, "the trace row is still written"
    assert rows[0]["ts"] == s.ring[-1]["ts"], "the trace row's ts is the read time, not the write's"


def test_a_stalled_trace_fsync_does_not_delay_the_hung_chip_poller_stop(
    watch, stalled_trace, monkeypatch  # noqa: F811
):
    """A heartbeat frozen across two samples stops the pollers on the second sample, while both
    samples' trace rows still wait on their fsync."""
    s, _clock, _, events = watch
    _release, rows = stalled_trace
    monkeypatch.setattr(s, "prev_beats", {})
    monkeypatch.setattr(s, "pollers_stopped_for", frozenset())
    stopped = []

    async def pollers(on, *a, **k):
        stopped.append((on, len(rows)))

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    frozen = [555, 800, 800, 0, 0, 0]
    took = _cycles(monkeypatch, s, [{"0": LIVE, "13": frozen}, {"0": [101, *LIVE[1:]], "13": frozen}])
    assert took < STALL_SEC / 2, f"the sampler cycles waited {took:.1f}s on the trace fsync"
    assert stopped == [(False, 0)], stopped
    assert "pollers_quiesced_hung_chip" in [k for k, _ in events]


@pytest.mark.asyncio
async def test_first_evidence_is_the_earliest_all_ones_read_not_the_check_time(watch):  # noqa: F811
    """The containment record's first evidence is the first sample that read the chip all-ones, at
    its read time, even when the check ran seconds later; a later read never moves it."""
    s, clock, _, _ = watch
    read_ts = time.time() - 11.3
    await s.check_for_dead_chips({"0": LIVE, "13": DEAD}, read_ts)
    clock.t += telemetry.FAST_SAMPLE_SEC
    await s.check_for_dead_chips({"0": [101, *LIVE[1:]], "13": DEAD}, read_ts + 2.0)
    now = time.time()
    row = telemetry._first_evidence(["13"], now, now + 0.5)
    assert row["first_evidence"] == "all_ones", row
    assert row["first_evidence_ts"] == round(read_ts, 3), row
    assert row["first_evidence_to_remove_s"] >= 11.3, row


def test_the_sampler_loop_stamps_all_ones_evidence_at_the_read(watch, stalled_trace, monkeypatch):  # noqa: F811
    """Through the loop: the evidence time is the ring's read time of the all-ones sample."""
    s, _clock, _, _ = watch
    _cycles(monkeypatch, s, [{"0": LIVE, "13": DEAD}])
    ts, kind = chip_state.first_evidence(["13"])
    assert kind == "all_ones" and ts == s.ring[-1]["ts"], (ts, kind, s.ring[-1]["ts"])


def test_a_full_trace_backlog_drops_the_row_and_never_blocks(monkeypatch, stalled_trace):
    """A disk stalled past TRACE_BACKLOG rows costs trace rows, never the sampler's time."""
    monkeypatch.setattr(evidence, "_trace_rows", evidence.queue.Queue(maxsize=1))
    monkeypatch.setattr(evidence, "_trace_writer", None)
    t0 = time.monotonic()
    queued = [evidence.append_trace({"0": LIVE}, 1.0) for _ in range(5)]
    assert time.monotonic() - t0 < 0.5
    assert queued[0] is True and queued[-1] is False, queued
