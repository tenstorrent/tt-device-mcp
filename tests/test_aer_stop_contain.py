# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""An AER stop on an all-ones burst chip contains it before any reader is killed (03 I94), and the
stop record is on disk before the stop returns (03 I95).

The AER watch (I92) SIGSTOPs the broker's own check readers on a burst. When the sampler's latest
sample read a burst chip all-ones, that chip is off the bus but still attached: a SIGKILL closes the
readers' fds and the driver's close path messages it. So the readers stay stopped and the dead-chip
valve (I84) on the sampler's loop removes the chip first, then kills them. A refused containment
keeps the hold and its bounded recovery; nothing on this path lifts a hold. No device is touched and
no process is spawned: the remove, the holder scan and every signal are stubbed.
"""

import asyncio
import os
import time
import warnings
from collections import deque
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty
from tests.test_aer_check_stop import _sig
from tests.test_deadchip_detach_before_kill import FABRIC, TENANT, valve  # noqa: F401 - the fixture
from tests.test_offbus_early_watch import _Clock
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import ALL_ONES, chip_state, evidence, read_health_events
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import OUTCOME_TERMINAL
from tt_device_mcp.telemetry import CheckAerWatch


@pytest.fixture
def watch(valve, monkeypatch):  # noqa: F811
    """A watch on a fake clock over the real server deps and the valve's stubs; ``burst()`` drives
    the two reads of one AER burst on chip 13's path."""
    log, detach_ok = valve
    clock = _Clock()
    w = CheckAerWatch(srv._recovery_deps, srv.recovery_mechanism, srv.sampler)
    w._clock = clock
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setattr(srv.sampler, "scope_seen", False)
    monkeypatch.setattr(srv.sampler, "ring", deque(maxlen=8))
    monkeypatch.setattr(srv.sampler, "loop", None)
    monkeypatch.setattr(srv.sampler, "aer_containment", None)
    monkeypatch.setattr(srv.CEILING, "proc", None)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    # A host whose lift needs a passing full check with fabric (03 I78).
    monkeypatch.setattr(FABRIC_DEBT, "runs_here", lambda expected: True)
    reads = []
    monkeypatch.setattr(srv, "link_signals", lambda: reads.pop(0))

    def burst():
        reads.extend([_sig(0), _sig(14)])
        assert w.poll_once() is None
        clock.t += 1
        return w.poll_once()

    return w, log, detach_ok, burst


def _sample(*ones, chips=("13", "14"), age=0.0):
    srv.sampler.ring.append({"ts": time.time() - age, "chips": {i: [ALL_ONES if i in ones else 100] for i in chips}})


async def _contain(burst):
    """The burst on the watch's own thread, then the containment it handed the loop, bounded."""
    srv.sampler.loop = asyncio.get_running_loop()
    reason = await asyncio.to_thread(burst)
    assert reason, "the burst stops the readers"
    fut = srv.sampler.aer_containment
    assert fut is not None, "the containment was handed to the loop"
    await asyncio.wait_for(asyncio.wrap_future(fut), 10)
    await asyncio.wait_for(srv.sampler.settle_isolations(), 10)
    return reason


def _rows(kind):
    return read_health_events(kinds={kind})


def _stop_row(timeout=5.0):
    """The check_aer_stop row; the watch thread returned only after its fsync (I95)."""
    rows = _rows("check_aer_stop")
    assert rows, "the stop record is on disk once the stop returned"
    return rows[-1]


def _held():
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stays"
    assert srv._hold_lift_unproven(), "only a later passing full check with fabric lifts it"


@pytest.mark.asyncio
async def test_an_all_ones_burst_chip_is_removed_before_any_reader_is_killed(watch):
    _w, log, _detach_ok, burst = watch
    _sample("13")
    await _contain(burst)
    assert log[0] == ("stop", FABRIC), f"the first act after the burst is the SIGSTOP: {log}"
    detach = log.index(("detach", "13"))
    kills = [i for i, e in enumerate(log) if e[0] in ("kill", "pollers")]
    assert kills and detach < min(kills), f"a kill came before the remove: {log}"
    assert ("kill", FABRIC) in log and ("kill", TENANT) in log, "after the remove the valve kills as usual"
    assert ("detach", "14") not in log, "only the all-ones burst chip is removed"
    row = _stop_row()
    assert row["offbus"] == ["13"] and row["contained"] == ["13"] and row["stopped"], row
    assert _rows("chip_contained")[-1]["chips"] == ["13"]
    assert [r["chips"] for r in _rows("chip_isolated")] == [["13"]]
    assert chip_state.is_lost("13"), "no poll reads the chip again"
    assert "13" in srv.isolated_chips
    assert FABRIC_DEBT.owed, "the stopped run is no verdict: a full pass is owed"
    _held()


@pytest.mark.parametrize(
    "case",
    ["no sample", "every chip answers", "another chip all-ones", "blackout", "stale sample"],
)
def test_with_no_all_ones_burst_chip_the_stop_kills_at_once_as_before(watch, case):
    """No contained chip: today's I92 stop, SIGSTOP then SIGKILL, no remove. A blackout (every chip
    all-ones, the class A/C fault) is never contained here: its direct power cycle path stands."""
    _w, log, _detach_ok, burst = watch
    if case == "every chip answers":
        _sample()
    elif case == "another chip all-ones":
        _sample("14")
    elif case == "blackout":
        _sample("13", "14")
    elif case == "stale sample":
        _sample("13", age=telemetry.FAST_WINDOW_SEC + 5)
    assert burst()
    assert log == [("stop", FABRIC), ("kill", FABRIC)], log
    assert srv.sampler.aer_containment is None and not _rows("check_aer_contain")
    assert not _rows("chip_contained")
    row = _stop_row()
    assert row["offbus"] == [] and row["contained"] == []
    assert FABRIC_DEBT.owed


def test_with_no_loop_the_watch_thread_still_removes_before_any_kill(watch):
    """No running event loop (stalled, or gone): the watch thread contains the chip itself (03 I96),
    every stop before the remove and every kill after it."""
    _w, log, _detach_ok, burst = watch
    _sample("13")
    assert burst()
    stops = [i for i, e in enumerate(log) if e[0] == "stop"]
    detach = log.index(("detach", "13"))
    kills = [i for i, e in enumerate(log) if e[0] == "kill"]
    assert stops and kills and max(stops) < detach < min(kills), f"stop, then remove, then kill: {log}"
    assert ("kill", FABRIC) in log and ("kill", TENANT) in log
    row = _stop_row()
    assert row["offbus"] == ["13"] and row["contained"] == ["13"]
    assert _rows("chip_contained")[-1]["removed"] is True
    assert FABRIC_DEBT.owed
    _held()


@pytest.mark.asyncio
async def test_a_failed_remove_kills_nobody_and_keeps_the_hold(watch):
    _w, log, detach_ok, burst = watch
    detach_ok["13"] = False
    _sample("13")
    await _contain(burst)
    assert ("detach", "13") in log
    assert not [e for e in log if e[0] == "kill"], f"no kill while the chip stays attached: {log}"
    assert _rows("chip_dead_kill_refused")[-1]["reason"] == "detach_failed"
    _held()


@pytest.mark.asyncio
async def test_a_pci_wedged_latch_skips_the_remove_and_keeps_the_hold(watch, monkeypatch):
    _w, log, _detach_ok, burst = watch
    monkeypatch.setattr(srv.PCI_WRITER, "latched_at", time.time())
    monkeypatch.setattr(srv.PCI_WRITER, "reason", "a remove blocked in the kernel")
    _sample("13")
    await _contain(burst)
    assert not [e for e in log if e[0] in ("detach", "kill")], log
    assert _rows("chip_isolation_skipped")[-1]["chips"] == ["13"]
    _held()


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["reset_in_flight", "scope"])
async def test_a_reset_that_took_the_chips_since_the_stop_refuses_and_keeps_the_hold(watch, monkeypatch, step):
    _w, log, _detach_ok, _burst = watch
    if step == "reset_in_flight":
        monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", True)
    else:
        monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "ttdev-reset.scope")
    await srv.sampler.contain_after_aer_stop(["13"], ["13"], "AER burst on chip(s) 13")
    assert not [e for e in log if e[0] in ("detach", "kill")], log
    assert _rows("check_aer_contain_refused")[-1]["reason"] == "reset_in_flight"
    assert srv.fsm.state is not ServerState.HEALTHY and srv._hold_lift_unproven()


@pytest.mark.asyncio
async def test_a_contained_chip_is_contained_once(watch):
    _w, log, _detach_ok, burst = watch
    _sample("13")
    await _contain(burst)
    n = len(log)
    await srv.sampler.contain_after_aer_stop(["13"], ["13"], "again")
    assert len(log) == n and len(_rows("chip_contained")) == 1


@pytest.mark.asyncio
async def test_the_contained_hold_runs_the_full_ladder_at_the_bound_then_the_host_rung(watch, monkeypatch):
    """Goals of spec 04 I76 on this path: the hold the containment opens is a hold like any other.
    At 10 min the broker's own full ladder runs, and a ladder that does not heal goes on to the
    host rung; nothing before it lifts the hold."""
    _w, _log, _detach_ok, burst = watch
    _sample("13")
    await _contain(burst)
    _held()
    escalations, events = [], []

    async def ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        return OUTCOME_TERMINAL

    async def host_rung(age, log):
        events.append("host_rung")

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)
    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)
    monkeypatch.setattr(srv.fsm, "on_outcome", lambda outcome: None)
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    # The host as the containment left it: chip 13 removed from the bus, chip 14 present and beating.
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: ["14"])
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: 2)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"14": 100})
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=601)).isoformat())
    await srv._force_escalate_stuck_hold()
    assert escalations == ["offbus-forced"], "the full ladder runs at the bound"
    assert events == ["host_rung"], "a ladder that did not heal goes on to the host rung"
    _held()


@pytest.mark.asyncio
async def test_the_contained_hold_fails_the_full_check_gate_into_the_bounded_recovery(watch):
    """A pass from before the fault no longer vouches (03 I82, I78): the hold needs a new full
    check with fabric."""
    _w, _log, _detach_ok, burst = watch
    FABRIC_DEBT.note_fabric(True)
    fsm_dirty(srv, "startup: the mesh is unverified after a broker start", why="job_killed")
    _sample("13")
    await _contain(burst)
    assert FABRIC_DEBT.owed and FABRIC_DEBT.pass_monotonic == 0.0
    _held()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["containment", "sampler"])
async def test_overlapping_valve_runs_kill_nobody_before_every_all_ones_chip_is_removed(watch, monkeypatch, first):
    """The containment of chip 13 and the sampler's own valve run for chip 14 overlap on one loop,
    either one first, each remove held until the test lets it end: the first one's until the second
    run's remove is queued, the second one's until the first run has decided. No run clears or replaces the other's all-ones
    chips: while either chip is attached nobody is killed and no poller stops; once both are removed
    the valve kills as usual, and the hold stays."""
    import threading

    _w, log, _detach_ok, _burst = watch
    blocked = "13" if first == "containment" else "14"
    later = "14" if blocked == "13" else "13"
    entered, gate, gate2 = threading.Event(), threading.Event(), threading.Event()

    def isolate(idx):
        log.append(("detach", idx))
        if idx == blocked:
            entered.set()
            gate.wait(15)
        else:
            gate2.wait(15)
        log.append(("detached", idx))
        return True

    monkeypatch.setattr(srv, "isolate_chip", isolate)
    monkeypatch.setattr(srv.sampler, "dead_chip_strikes", {"14": 1})
    live = lambda b: [b, 800, 800, 0, 0, 0]  # noqa: E731
    # Chip 14 confirmed all-ones on its second sample; chip 13 reads all-ones once it is off the bus.
    ones13 = live(ALL_ONES) if first == "containment" else live(1)

    def contain():
        return asyncio.ensure_future(srv.sampler.contain_after_aer_stop(["13"], ["13"], "AER burst on chip(s) 13"))

    def sample():
        return asyncio.ensure_future(
            srv.sampler.check_for_dead_chips({"0": live(1), "13": ones13, "14": live(ALL_ONES)})
        )

    runs = []
    try:
        runs.append(contain() if first == "containment" else sample())
        assert await asyncio.to_thread(entered.wait, 15), f"chip {blocked}'s remove started"
        runs.append(sample() if first == "containment" else contain())
        # Until the second run's remove is queued behind the first (it marks the chip isolated, then
        # removes, no await between).
        deadline = time.monotonic() + 15
        while later not in srv.isolated_chips and not runs[1].done():
            assert time.monotonic() < deadline, f"the second run reached chip {later}'s remove"
            await asyncio.sleep(0.01)
        gate.set()
        await asyncio.wait_for(asyncio.shield(runs[0]), 15)
    finally:
        gate.set()
        gate2.set()
    await asyncio.wait_for(asyncio.gather(*runs), 15)
    await asyncio.wait_for(srv.sampler.settle_isolations(), 15)
    last = max(log.index(("detached", "13")), log.index(("detached", "14")))
    early = [e for e in log[:last] if e[0] in ("kill", "pollers")]
    assert not early, f"a kill or poller stop came while an all-ones chip was attached: {log}"
    assert ("kill", TENANT) in log[last:], f"after both removes the valve kills as usual: {log}"
    assert srv.sampler.valve_runs == [] and not srv.sampler.valve_detaching
    _held()


@pytest.mark.parametrize("fault", ["fsync", "open"])
def test_a_failed_journal_write_is_not_reported_on_disk(watch, monkeypatch, fault):
    """A write or fsync that failed is not a flush: the watch logs the stop record as not on disk.
    The signals, the owed full pass and the hold are unchanged."""
    _w, log, _detach_ok, burst = watch
    if fault == "fsync":

        def bad_fsync(fd):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(evidence.os, "fsync", bad_fsync)
    else:
        (evidence.HEALTH_DIR / evidence.EVENTS_FILE).mkdir(parents=True)  # open() raises IsADirectoryError
    lines = []

    class _Log:
        def error(self, msg, *a, **k):
            lines.append(msg)

        info = warning = debug = error

    monkeypatch.setattr(srv, "logger", _Log())
    assert evidence.health_event_flushed("probe", 5) is False
    assert burst()
    assert log == [("stop", FABRIC), ("kill", FABRIC)], log
    assert any("stop record not on disk" in m for m in lines), lines
    assert FABRIC_DEBT.owed, "the stopped run is still no verdict"


def test_the_stop_record_is_on_disk_when_the_process_dies_right_after_the_stop(watch):
    """The broker is gone the moment the stop returns (a host lost to the storm): the record that
    shows the stop is already fsynced. The child exits with no flush, no atexit, no thread join."""
    _w, _log, _detach_ok, burst = watch
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads alive: the child only writes
        pid = os.fork()
    if pid == 0:
        code = 3
        try:
            code = 0 if burst() else 4
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status
    row = _stop_row()
    assert row["stopped"] and "AER burst on chip(s) 13" in row["reason"]


def test_a_journal_stalled_in_fsync_bounds_the_wait_and_delays_neither_signal(watch, monkeypatch):
    """The storm stalled one fsync for 48 s: both signals come before the wait, and the watch thread
    returns at AER_STOP_JOURNAL_WAIT_SEC whatever the disk does."""
    import threading

    _w, log, _detach_ok, burst = watch
    monkeypatch.setattr(telemetry, "AER_STOP_JOURNAL_WAIT_SEC", 0.2)
    stalled, release = threading.Event(), threading.Event()

    def fsync_stall(path, rec, mx):
        stalled.set()
        release.wait(30)

    monkeypatch.setattr(evidence, "_append_durable", fsync_stall)
    monkeypatch.setattr(srv, "logger", None)
    try:
        t = threading.Thread(target=burst, daemon=True)
        start = time.monotonic()
        t.start()
        t.join(10)
        assert not t.is_alive(), "the watch thread's wait is bounded"
        assert log == [("stop", FABRIC), ("kill", FABRIC)], log
        assert time.monotonic() - start < 10
        assert stalled.wait(5), "the journal writer took the records after the stop"
        assert FABRIC_DEBT.owed, "the no-verdict mark does not wait on the journal"
    finally:
        release.set()
