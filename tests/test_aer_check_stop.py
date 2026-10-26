# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""An AER burst on a chip's path stops the broker's own check readers, from a thread (03 I92).

On a firmware-first host each counted AER record is a system-management interrupt; a storm of them
while something keeps reading the chips can trip the hardware watchdog within seconds. The watch
thread reads link_signals while a broker check reader runs and, on AER_STOP_COUNT counted errors on
one function within AER_STOP_WINDOW_SEC, SIGSTOPs then SIGKILLs the readers' groups before any
journal or log write. The run is no verdict (I82): the hold stays and a full pass is owed. A reset
step's own errors (I66) are excused. No device is touched and no process is spawned.
"""

import signal
import threading
import time

import pytest

import tests.test_fabric_killed_by_dead_chip as _killed
from tests.conftest import fsm_dirty, patch_health_event
from tests.test_offbus_early_watch import _Clock
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerFsm, ServerState
from tt_device_mcp.health import evidence
from tt_device_mcp.health.monitors import fabric
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.telemetry import AER_STOP_COUNT, POST_STEP_AER_QUIET_SEC, CheckAerWatch

fab = _killed.fab  # the shared fabric-runner fixture

PATHS = {"13": ["0000:02:00.0", "0000:00:01.1"], "14": ["0000:03:00.0", "0000:00:01.2"]}
FABRIC, SNAPSHOT, ETH = 9101, 9102, 9103


def _sig(port1: int, port2: int = 0, stray: int = 0) -> dict:
    return {
        "chips": ["13", "14"],
        "aer": {"0000:00:01.1": port1, "0000:00:01.2": port2, "0000:00:1f.0": stray},
        "paths": PATHS,
    }


class _Reader:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None


@pytest.fixture
def rig(monkeypatch):
    """A watch on a fake clock over the real server deps; one ordered log of signals and writes."""
    clock = _Clock()
    w = CheckAerWatch(srv._recovery_deps, srv.recovery_mechanism, srv.sampler)
    w._clock = clock
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setattr(srv.sampler, "scope_seen", False)
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", _Reader(FABRIC))
    monkeypatch.setattr(srv.health_monitor, "snapshot_proc", None, raising=False)
    monkeypatch.setattr(srv, "eth_selftest_proc", None, raising=False)
    monkeypatch.setattr(srv.CEILING, "proc", None)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    log = []
    monkeypatch.setattr(
        srv.os, "killpg", lambda pid, sig: log.append(("stop" if sig == signal.SIGSTOP else "kill", pid))
    )
    monkeypatch.setattr(evidence, "_append_durable", lambda path, rec, mx: log.append(("io", rec["kind"])))
    patch_health_event(monkeypatch, lambda kind, **f: log.append(("event", kind)))
    reads = []

    def feed(*sigs):
        reads.extend(sigs)

    def link_signals():
        return reads.pop(0)

    monkeypatch.setattr(srv, "link_signals", link_signals)
    return w, clock, log, feed


def _burst(w, clock, feed, rise=14):
    feed(_sig(0), _sig(rise))
    assert w.poll_once() is None
    clock.t += 1
    return w.poll_once()


@pytest.mark.parametrize("who, pid", [("fabric_check_proc", FABRIC), ("snapshot_proc", SNAPSHOT), ("eth", ETH)])
def test_a_burst_stops_every_reader_before_any_write_and_owes_a_full_pass(rig, monkeypatch, who, pid):
    w, clock, log, feed = rig
    if who != "fabric_check_proc":
        monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
        if who == "eth":
            monkeypatch.setattr(srv, "eth_selftest_proc", _Reader(ETH), raising=False)
        else:
            monkeypatch.setattr(srv.health_monitor, "snapshot_proc", _Reader(SNAPSHOT), raising=False)
    reason = _burst(w, clock, feed)
    assert reason and "chip(s) 13" in reason and "0000:00:01.1 +14" in reason, reason
    assert log[0] == ("stop", pid), f"the first thing after the burst is the SIGSTOP: {log}"
    assert log.index(("stop", pid)) < log.index(("kill", pid)) < log.index(("event", "check_aer_stop"))
    assert not [e for e in log if e[0] == "io"], f"no synchronous journal append on the watch path: {log}"
    if who != "eth":
        assert FABRIC_DEBT.owed and "AER burst" in FABRIC_DEBT.owed, "no verdict: a full pass is owed"


@pytest.mark.parametrize(
    "detail, why",
    [("startup: the mesh is unverified after a broker start", "job_killed"), ("job 041 ended failed", "job_killed")],
)
def test_the_stop_keeps_the_hold_and_the_earlier_pass_no_longer_vouches(rig, monkeypatch, detail, why):
    """Startup and post-job checks alike: the stop only ends the run; the hold stays (goal 11)."""
    w, clock, _log, feed = rig
    monkeypatch.setattr(FABRIC_DEBT, "runs_here", lambda expected: True)
    FABRIC_DEBT.note_fabric(True)  # a pass from before the fault
    fsm_dirty(srv, detail, why=why)
    assert _burst(w, clock, feed)
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stays"
    assert FABRIC_DEBT.owed and FABRIC_DEBT.pass_monotonic == 0.0
    assert srv._hold_lift_unproven(), "only a later full pass lifts it"


@pytest.fixture
def aer_fab(fab, monkeypatch, rig):
    """The real fabric verdict path, whose run the watch stops mid-way on a burst."""
    m, state = fab
    w, clock, log, feed = rig
    monkeypatch.setattr(srv, "health_monitor", m)
    state["kill"] = False

    async def check(argv, env, **kw):
        m.fabric_check_proc = _Reader(FABRIC)
        stopped = _burst(w, clock, feed)
        m.fabric_check_proc = None
        assert stopped, "the burst stops the run"
        return state["rc"], state["text"]

    monkeypatch.setattr(fabric, "check", check)
    return m, log


@pytest.mark.asyncio
async def test_a_stopped_fabric_run_is_no_verdict(aer_fab):
    m, log = aer_fab
    ok, detail = await m.verify_fabric_health(60)
    assert ok is None and "KILLED" in detail and "AER burst" in detail, detail
    assert m.last_fabric_ok is None and FABRIC_DEBT.owed
    assert ("kill", FABRIC) in log


@pytest.mark.asyncio
async def test_a_burst_during_the_full_check_gate_fails_it_into_the_bounded_recovery(aer_fab, monkeypatch):
    """The I78 gate: a stopped fabric run fails the check, keeps the hold and hands it to the I76
    path (the host rung after the bound's ladder), with the ladder's order unchanged."""
    m, _log = aer_fab
    fsm_dirty(srv, "gate/post-job: eth/fabric fault on a present mesh", why="eth_frozen")
    monkeypatch.setattr(FABRIC_DEBT, "runs_here", lambda expected: True)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)

    async def observe(expected, log, **_):
        ok, detail = await m.verify_fabric_health(60)
        return True, {"fabric": {"ok": ok, "detail": detail}}

    monkeypatch.setattr(srv.fsm, "observe", observe)
    failed = []
    monkeypatch.setattr(srv, "_note_hold_bound_check_failed", lambda log: failed.append(1) or True)
    ok, evidence_ = await srv._full_check_gate(32, lambda msg: None)
    assert ok is False and evidence_["fabric"]["ok"] is None
    assert failed == [1], "a failed full check after the ladder goes on to the power-cycle path"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.parametrize("owner", ["reset_in_flight", "scope_seen"])
def test_a_reset_steps_own_aer_is_excused_then_a_real_burst_still_stops(rig, monkeypatch, owner):
    w, clock, log, feed = rig
    target = srv.recovery_mechanism if owner == "reset_in_flight" else srv.sampler
    feed(_sig(0), _sig(30), _sig(45), _sig(46), _sig(46 + AER_STOP_COUNT))
    assert w.poll_once() is None
    monkeypatch.setattr(target, owner, True)
    clock.t += 1
    assert w.poll_once() is None, "the step's own errors"
    monkeypatch.setattr(target, owner, False)
    clock.t += 2
    assert w.poll_once() is None, "inside POST_STEP_AER_QUIET_SEC after the step"
    assert not log, log
    clock.t += POST_STEP_AER_QUIET_SEC + 1
    assert w.poll_once() is None
    clock.t += 1
    assert w.poll_once(), "past the quiet window a burst stops the check again"
    assert log[0] == ("stop", FABRIC)


def test_a_slow_rise_another_path_or_one_below_the_count_never_stops(rig):
    w, clock, log, feed = rig
    # The calibrated count (telemetry's basis comment): benign ports post at most 2 in 2 s.
    assert (AER_STOP_COUNT, telemetry.AER_STOP_WINDOW_SEC) == (4, 2.0)
    feed(_sig(0), _sig(3))
    assert w.poll_once() is None
    clock.t += 1
    assert w.poll_once() is None, "3 in 2 s is below the count"
    w._reads.clear()
    feed(_sig(0), _sig(2), _sig(4))
    for _ in range(3):
        assert w.poll_once() is None, "4 spread over more than the window"
        clock.t += 1.5
    w._reads.clear()
    feed(_sig(0, stray=0), _sig(0, stray=50))
    assert w.poll_once() is None
    clock.t += 1
    assert w.poll_once() is None, "a function on no chip's path"
    assert not log, log


def test_no_reader_running_means_no_read(rig, monkeypatch):
    w, _clock, log, _feed = rig
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(srv, "link_signals", lambda: pytest.fail("read with no check running"))
    assert w.poll_once() is None and not log
    done = _Reader(FABRIC)
    done.returncode = 0
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", done)
    assert w.poll_once() is None and not log, "a finished reader is not signalled"


def test_a_journal_stalled_in_fsync_delays_neither_signal(monkeypatch):
    """The storm this guards stalled the journal's fsync for tens of seconds: the stop and the kill
    must not wait on it, nor must the watch thread."""
    clock = _Clock()
    w = CheckAerWatch(srv._recovery_deps, srv.recovery_mechanism, srv.sampler)
    w._clock = clock
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    monkeypatch.setattr(srv.sampler, "scope_seen", False)
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", _Reader(FABRIC))
    monkeypatch.setattr(srv.health_monitor, "snapshot_proc", None, raising=False)
    monkeypatch.setattr(srv, "eth_selftest_proc", None, raising=False)
    monkeypatch.setattr(srv.CEILING, "proc", None)
    monkeypatch.setattr(srv, "logger", None)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    stalled, release, signals = threading.Event(), threading.Event(), []

    def fsync_stall(path, rec, mx):
        stalled.set()
        release.wait(30)

    monkeypatch.setattr(evidence, "_append_durable", fsync_stall)
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: signals.append(sig))
    reads = [_sig(0), _sig(14)]
    monkeypatch.setattr(srv, "link_signals", lambda: reads.pop(0))
    try:
        assert w.poll_once() is None
        clock.t += 1
        t = threading.Thread(target=w.poll_once, daemon=True)
        start = time.monotonic()
        t.start()
        t.join(5)
        assert not t.is_alive(), "the watch thread waited on the journal"
        assert signals == [signal.SIGSTOP, signal.SIGKILL] and time.monotonic() - start < 5, signals
        assert stalled.wait(5), "the journal writer took the records after the stop"
        assert FABRIC_DEBT.owed, "the no-verdict mark does not wait on the journal either"
    finally:
        release.set()


def test_boot_builds_the_watch_beside_the_sampler(tmp_path, health_deps):
    f = ServerFsm(tmp_path / "fsm.json")
    f.boot(health_deps)
    assert isinstance(f.aer_watch, CheckAerWatch) and f.aer_watch._sampler is f.sampler
    assert f.aer_watch.thread is None, "started by the broker's main startup, not at build"


def test_the_watch_runs_on_its_own_thread_and_stops(rig, monkeypatch):
    w, _clock, log, feed = rig
    monkeypatch.setattr(telemetry, "LINK_WATCH_SEC", 0.01)
    w._clock = time.monotonic
    feed(*[_sig(0)] * 2 + [_sig(40)] * 200)
    w.start()
    try:
        deadline = time.monotonic() + 5
        while ("kill", FABRIC) not in log and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        w.stop()
        w.thread.join(1)
    assert ("kill", FABRIC) in log and w.thread.name == "check-aer-watch" and not w.thread.is_alive()
