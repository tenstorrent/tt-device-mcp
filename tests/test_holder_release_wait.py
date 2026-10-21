# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A timed-out or killed job must not leave the device held (spec 01 I67, spec 03 I68).

The survivor sweep waits, bounded, for a SIGKILLed process to let go of the device before it names
it a leftover, and a dirty job_killed / foreign_holder hold with nothing queued gets the post-job
gate, fabric forced, from the idle tick instead of waiting for the next job or the hold ceiling.
No device is touched: the holder scan, the gate and the liveness read are stubbed.
"""

import asyncio
import time

import pytest

from tests.conftest import fsm_dirty, fsm_healthy, patch_health_event
from tests.test_job_reap import _escaped_child, _kill_quietly, _unique_tag
from tt_device_mcp import device_holders, job_reap
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState

# --- spec 01 I67: the sweep waits for a SIGKILLed holder to release -----------------------------


def _swallow_signals(monkeypatch):
    """A process that outlives SIGKILL for a while cannot be made in a test: swallow the signals."""
    monkeypatch.setattr(job_reap.os, "kill", lambda *a: None)
    monkeypatch.setattr(job_reap.os, "killpg", lambda *a: None)


@pytest.mark.asyncio
async def test_a_holder_that_releases_within_the_window_is_not_a_leftover(monkeypatch):
    tag = _unique_tag("t-release")
    job_pid, child = _escaped_child(tag)
    _swallow_signals(monkeypatch)
    state = {"holding": True, "scans": 0}

    def scan():
        state["scans"] += 1
        if state["scans"] >= 3:
            state["holding"] = False
        return HolderScan(holders=[DeviceHolder(pid=child, uid=0)] if state["holding"] else [])

    monkeypatch.setattr(device_holders, "enumerate_device_holders", scan)
    monkeypatch.setattr(job_reap, "_holds_device", lambda pid: state["holding"])
    lines = []
    try:
        start = time.monotonic()
        survivors = await job_reap.reap_job_survivors(
            "t1",
            job_pid,
            None,
            tag,
            term_grace_sec=0.2,
            kill_wait_sec=0.2,
            release_wait_sec=10,
            log=lines.append,
        )
        assert time.monotonic() - start < 5, "the wait ends as soon as the holder lets go"
        assert state["scans"] >= 3, "the holder scan was polled until it let go"
        assert [s.holds_device for s in survivors] == [False], "alive but released: no leftover holds the device"
        assert any("waiting up to" in line for line in lines)
    finally:
        monkeypatch.undo()
        _kill_quietly(child)


@pytest.mark.asyncio
async def test_a_holder_still_holding_after_the_window_is_named(monkeypatch):
    tag = _unique_tag("t-hold")
    job_pid, child = _escaped_child(tag)
    _swallow_signals(monkeypatch)
    monkeypatch.setattr(
        device_holders, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=child, uid=0)])
    )
    monkeypatch.setattr(job_reap, "_holds_device", lambda pid: True)
    try:
        start = time.monotonic()
        survivors = await job_reap.reap_job_survivors(
            "t2",
            job_pid,
            None,
            tag,
            term_grace_sec=0.2,
            kill_wait_sec=0.2,
            release_wait_sec=0.8,
            log=lambda _m: None,
        )
        elapsed = time.monotonic() - start
        assert [s.pid for s in survivors] == [child]
        assert survivors[0].holds_device is True, "still a holder when the window ends: the I16 path"
        assert 0.8 <= elapsed < 5, "the wait runs the whole window, and no longer"
    finally:
        monkeypatch.undo()
        _kill_quietly(child)


@pytest.mark.asyncio
async def test_another_runs_child_of_the_same_test_is_not_a_survivor(monkeypatch):
    """Two runs of the suite on one host (two checkouts checked at once) spawn the same test's
    child together. The sweep matches the job tag host-wide, so each run's tag must be its own,
    or one run names the other's child as its survivor."""
    _, other = _escaped_child(_unique_tag("t-hold"))  # the other run's child
    tag = _unique_tag("t-hold")
    job_pid, child = _escaped_child(tag)
    _swallow_signals(monkeypatch)
    monkeypatch.setattr(job_reap, "_holds_device", lambda pid: False)
    try:
        survivors = await job_reap.reap_job_survivors(
            "t3", job_pid, None, tag, term_grace_sec=0.1, kill_wait_sec=0.1, log=lambda _m: None
        )
        assert [s.pid for s in survivors] == [child], "only this run's child is its survivor"
    finally:
        monkeypatch.undo()
        _kill_quietly(child)
        _kill_quietly(other)


@pytest.mark.asyncio
async def test_wait_device_released_with_nothing_to_wait_for_scans_nothing(monkeypatch):
    def boom():
        pytest.fail("an empty set needs no scan")

    monkeypatch.setattr(device_holders, "enumerate_device_holders", boom)
    assert await device_holders.wait_device_released([], 30) == set()


@pytest.mark.asyncio
async def test_wait_device_released_returns_once_the_pids_let_go(monkeypatch):
    scans = {"n": 0}

    def scan():
        scans["n"] += 1
        return HolderScan(holders=[DeviceHolder(pid=7, uid=0), DeviceHolder(pid=99, uid=0)] if scans["n"] < 2 else [])

    monkeypatch.setattr(device_holders, "enumerate_device_holders", scan)
    start = time.monotonic()
    assert await device_holders.wait_device_released({7}, 30, poll_sec=0.01) == set()
    assert scans["n"] == 2 and time.monotonic() - start < 1


@pytest.mark.asyncio
async def test_wait_device_released_gives_up_at_the_timeout(monkeypatch):
    calls = {"n": 0}

    def scan():
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("unreadable")  # a failed scan keeps the set and never raises
        return HolderScan(holders=[DeviceHolder(pid=7, uid=0)])

    monkeypatch.setattr(device_holders, "enumerate_device_holders", scan)
    start = time.monotonic()
    assert await device_holders.wait_device_released({7, 8}, 0.2, poll_sec=0.02) == {7}
    assert 0.2 <= time.monotonic() - start < 1
    assert calls["n"] > 2


# --- spec 03 I68: the idle tick gives a dirty hold its post-job gate ----------------------------


@pytest.fixture
def dirty_idle(monkeypatch, clear_job_state):
    """A dirty job_killed hold on an idle device, with the gate recorded instead of run."""
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan())
    monkeypatch.setattr(srv.galaxy_recovery, "_tenant_active", lambda scan, **k: False)
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: "")
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(srv, "_down_offbus_recheck_armed", lambda: False)
    monkeypatch.setattr(srv, "_maybe_spawn_eth_rearm", lambda: None)
    monkeypatch.setattr(srv, "_relift_task", None)
    monkeypatch.setattr(srv, "_dirty_idle_gate_monotonic", 0.0)
    monkeypatch.setattr(srv, "_dirty_idle_gate_spent_since", None)
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "post_job_gate_pending", False)
    monkeypatch.setattr(srv, "device_op_active", "")
    srv.readopted_scopes.clear()

    async def no_relift():
        pytest.fail("a dirty job_killed hold belongs to the idle gate, not the read-only relift")

    monkeypatch.setattr(srv, "_attempt_idle_relift", no_relift)
    calls = []
    outcome = {"lift": False, "raise": None}

    async def gate(job_log_file, *, phase, run_fabric, force_fabric=False, **k):
        calls.append({"phase": phase, "run_fabric": run_fabric, "force_fabric": force_fabric})
        if outcome["raise"]:
            raise outcome["raise"]
        if outcome["lift"]:
            srv._clear_device_dirty(verified=True, why="test: gate passed")

    monkeypatch.setattr(srv, "_device_health_gate", gate)
    fsm_healthy(srv)
    fsm_dirty(srv, "job 7 timed out", why="job_killed")
    yield calls, outcome
    fsm_healthy(srv)


async def _tick():
    srv._maybe_spawn_idle_relift()
    task = srv._relift_task
    if task is not None:
        await task
    return task


@pytest.mark.asyncio
async def test_a_dirty_killed_hold_with_an_empty_queue_runs_the_full_post_job_gate_once(dirty_idle, monkeypatch):
    calls, outcome = dirty_idle
    outcome["lift"] = True
    assert await _tick() is not None, "the idle tick spawns the gate for a dirty job_killed hold"
    assert calls == [{"phase": "post-job", "run_fabric": True, "force_fabric": True}], "the full check, fabric forced"
    assert srv.fsm.state is ServerState.HEALTHY, "a passing gate lifts the hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("why", sorted(srv.DIRTY_IDLE_GATE_WHYS))
async def test_a_failed_idle_gate_keeps_the_hold_and_runs_once_per_episode(dirty_idle, monkeypatch, why):
    calls, _ = dirty_idle
    fsm_healthy(srv)
    fsm_dirty(srv, "job 7 left a holder", why=why, dirty=True)
    await _tick()
    assert len(calls) == 1
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.dirty, "a failed gate never releases"
    monkeypatch.setattr(srv, "_dirty_idle_gate_monotonic", 0.0)  # past the recheck period
    monkeypatch.setattr(srv, "_attempt_idle_relift", _noop)  # a spent foreign_holder hold is the escalation's
    srv._relift_task = None
    await _tick()
    assert len(calls) == 1, "one gate per hold episode; the bounded escalation owns the rest"


@pytest.mark.asyncio
async def test_an_idle_gate_that_raises_keeps_the_device_held(dirty_idle):
    calls, outcome = dirty_idle
    outcome["raise"] = RuntimeError("boom")
    await _tick()
    assert len(calls) == 1
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.dirty
    assert srv.fsm.record.why == "gate_error"


@pytest.mark.asyncio
async def test_the_idle_gate_is_rate_limited(dirty_idle, monkeypatch):
    calls, _ = dirty_idle
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: "job 7 pid 42 holds the device")
    await _tick()
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: "")
    srv._relift_task = None
    await _tick()
    assert calls == [], "within DIRTY_IDLE_GATE_RECHECK_SEC of the last check nothing runs"
    monkeypatch.setattr(srv, "_dirty_idle_gate_monotonic", time.monotonic() - srv.DIRTY_IDLE_GATE_RECHECK_SEC - 1)
    srv._relift_task = None
    await _tick()
    assert len(calls) == 1, "the next check after the leftover let go runs the gate"


def _queued_job():
    return srv.Job(id="q1", owner="tenant", workspace="/w", command="echo hi", queued_at="t")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    ["queued", "running", "pending_gate", "device_op", "offbus", "leftover", "tenant", "scope", "other_why", "clean"],
)
async def test_no_idle_gate_while_anything_else_owns_the_device(dirty_idle, monkeypatch, blocker):
    calls, _ = dirty_idle
    if blocker == "queued":
        srv.jobs["q1"] = _queued_job()
    elif blocker == "running":
        job = _queued_job()
        job.status = srv.JobStatus.RUNNING
        srv.jobs["q1"] = job
    elif blocker == "pending_gate":
        monkeypatch.setattr(srv, "post_job_gate_pending", True)
    elif blocker == "device_op":
        monkeypatch.setattr(srv, "device_op_active", "reset")
    elif blocker == "offbus":
        monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "chip 3 off the bus")
    elif blocker == "leftover":
        monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: "job 7 pid 42 holds the device")
    elif blocker == "tenant":
        monkeypatch.setattr(srv.galaxy_recovery, "_tenant_active", lambda scan, **k: True)
    elif blocker == "scope":
        monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: True)
    elif blocker == "other_why":
        fsm_healthy(srv)
        fsm_dirty(srv, "gate error", why="gate_error", dirty=True)
    elif blocker == "clean":
        fsm_healthy(srv)
        fsm_dirty(srv, "foreign holder present", why="foreign_holder", dirty=False)
    monkeypatch.setattr(srv, "_attempt_idle_relift", _noop)
    await _tick()
    assert calls == [], f"{blocker}: the idle gate must not run"
    assert not srv._dirty_idle_gate_armed() or blocker in ("leftover", "tenant", "scope")
    assert srv._dirty_idle_gate_spent_since is None, "a skipped pass does not spend the episode"


async def _noop():
    await asyncio.sleep(0)
