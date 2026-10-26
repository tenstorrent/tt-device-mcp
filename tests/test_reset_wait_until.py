"""Spec 04 I102: a caller's ``wait_until`` ends the wait for a reset sooner, and nothing else changes.

The late-chip step passes its own bound down through reset_with_quiesce -> run_scoped (or the local
backend). A reset that outlives that bound is waited for only until it, is never killed, comes back
rc None with its scope still registered, and its pollers stay down while the scope cycles. The two
timeouts are scaled down here: DEVICE_RESET_OVERRUN_SEC stands in for the 180 s overrun line.
"""

import asyncio
import sys
import time

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp import pre_reset_scan, privileges
from tt_device_mcp.health.recovery import base as recovery_base

RESET = ["tt-smi", "-r", "0"]
SCOPE = "ttdev-reset-4242-1"


class _NeverEnds:
    """A reset process that outlives every wait, and records any attempt to stop it."""

    returncode = None
    pid = 4242

    def __init__(self, stopped):
        self.stopped = stopped

    async def communicate(self):
        await asyncio.sleep(3600)

    async def wait(self):
        await asyncio.sleep(3600)

    def kill(self):
        self.stopped.append("kill")

    def terminate(self):
        self.stopped.append("terminate")

    def send_signal(self, sig):
        self.stopped.append(f"signal {sig}")


def _scoped(monkeypatch, *, overrun, timeout=600):
    events, stopped = [], []
    monkeypatch.setattr(recovery_base, "health_event", lambda kind, **_kw: events.append(kind))
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_OVERRUN_SEC", overrun)
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_TIMEOUT_SEC", timeout)

    async def fake_exec(*_a, **_k):
        return _NeverEnds(stopped)

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    return events, stopped


def test_no_wait_until_keeps_the_reset_timeout_and_a_bound_only_shortens_it():
    timeout = recovery_base.DEVICE_RESET_TIMEOUT_SEC
    assert recovery_base._reset_wait_sec(None) == timeout
    assert recovery_base._reset_wait_sec(time.monotonic() + 10 * timeout) == timeout
    assert 90 < recovery_base._reset_wait_sec(time.monotonic() + 100) <= 100
    assert recovery_base._reset_wait_sec(time.monotonic() - 5) == 1.0


@pytest.mark.asyncio
async def test_a_bound_past_the_overrun_line_journals_the_overrun_then_times_out_at_the_bound(monkeypatch):
    events, stopped = _scoped(monkeypatch, overrun=0.2)
    t0 = time.monotonic()
    rc, text = await asyncio.wait_for(
        srv.recovery_mechanism.run_scoped(list(RESET), lambda _m: None, wait_until=t0 + 1.5), timeout=10
    )
    took = time.monotonic() - t0
    assert rc is None and "never finished" in text
    assert [e for e in events if e in ("reset_overran", "reset_timeout")] == ["reset_overran", "reset_timeout"]
    assert 1.3 <= took < 3.0, f"the wait ended at {took:.2f}s, not at the caller's 1.5 s bound"
    assert stopped == [], "the reset was stopped at the bound"


@pytest.mark.asyncio
async def test_a_bound_inside_the_overrun_line_times_out_at_the_bound_with_no_overrun(monkeypatch):
    events, stopped = _scoped(monkeypatch, overrun=2.0)
    t0 = time.monotonic()
    rc, _ = await asyncio.wait_for(
        srv.recovery_mechanism.run_scoped(list(RESET), lambda _m: None, wait_until=t0 + 1.2), timeout=10
    )
    took = time.monotonic() - t0
    assert rc is None
    assert "reset_timeout" in events and "reset_overran" not in events
    assert 1.0 <= took < 1.9, f"the wait ended at {took:.2f}s, not at the caller's 1.2 s bound"
    assert stopped == []


@pytest.mark.asyncio
async def test_without_a_bound_the_wait_runs_to_the_reset_timeout(monkeypatch):
    events, stopped = _scoped(monkeypatch, overrun=5.0, timeout=1.0)
    t0 = time.monotonic()
    rc, _ = await asyncio.wait_for(srv.recovery_mechanism.run_scoped(list(RESET), lambda _m: None), timeout=10)
    took = time.monotonic() - t0
    assert rc is None and "reset_timeout" in events
    assert 0.9 <= took < 1.8, f"the unbounded wait ended at {took:.2f}s, not at DEVICE_RESET_TIMEOUT_SEC"
    assert stopped == []


@pytest.mark.asyncio
@pytest.mark.parametrize("overrun, bound", [(0.2, 1.5), (2.0, 1.2)], ids=["past-overrun", "inside-overrun"])
async def test_a_local_reset_that_outlives_its_bound_keeps_running_and_registered(monkeypatch, tmp_path, overrun, bound):
    """The no-systemd backend: the child keeps its lock (scope_active) and its pid stays registered."""
    monkeypatch.setitem(privileges._LATCHED, "systemd", False)
    events = []
    monkeypatch.setattr(recovery_base, "health_event", lambda kind, **_kw: events.append(kind))
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_OVERRUN_SEC", overrun)

    async def pollers(_active, _log):
        return []

    mech = recovery_base.RecoveryMechanism(
        current_boot_id=lambda: "boot",
        boot_btime_id=lambda: "0",
        scoped_reset_backend=lambda: False,
        set_device_pollers=pollers,
        set_device_op_detail=lambda _detail: None,
        begin_action_row=lambda *_a: None,
        write_action_log=lambda *_a: None,
        device_hold_episode_since=lambda: "",
        local_reset_dir=lambda: tmp_path,
    )
    release = tmp_path / "release"
    command = [
        sys.executable,
        "-c",
        f"import pathlib,time; p=pathlib.Path({str(release)!r});\nwhile not p.exists(): time.sleep(.01)",
    ]
    before = set(pre_reset_scan._reset_pids)
    t0 = time.monotonic()
    try:
        rc, _ = await asyncio.wait_for(mech.run_scoped(command, lambda _m: None, wait_until=t0 + bound), timeout=10)
        took = time.monotonic() - t0
        assert rc is None
        expected = ["reset_overran", "reset_timeout"] if bound > overrun else ["reset_timeout"]
        assert [e for e in events if e in ("reset_overran", "reset_timeout")] == expected
        assert bound - 0.2 <= took < bound + 1.0, f"the wait ended at {took:.2f}s, not at the {bound} s bound"
        assert mech.scope_active() == recovery_base.LOCAL_RESET_NAME, "the reset was stopped or lost its lock"
        assert set(pre_reset_scan._reset_pids) - before, "the still-running reset's pid was forgotten"
    finally:
        release.write_text("")
        for _ in range(500):
            if mech.scope_active() is None:
                break
            await asyncio.sleep(0.01)
        for pid in set(pre_reset_scan._reset_pids) - before:
            pre_reset_scan.forget_reset_pid(pid)
    assert mech.scope_active() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("overrun, bound", [(0.2, 1.5), (2.0, 1.2)], ids=["past-overrun", "inside-overrun"])
async def test_quiesce_keeps_the_pollers_down_while_the_bounded_reset_still_cycles(monkeypatch, overrun, bound):
    """reset_with_quiesce passes the bound to run_scoped; at the bound the scope still cycles, so no cut,
    no rescan and no poller restart (base.py's rc-None path asks scope_active)."""
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    events, stopped = _scoped(monkeypatch, overrun=overrun)
    mech = srv.recovery_mechanism
    order = []

    async def stop_pollers(active, _log):
        order.append(f"pollers {'on' if active else 'off'}")
        return ["tt-poller.service"]

    async def no_readers(*_a):
        return ""

    async def restore(*_a, **_k):
        order.append("restore")

    async def cut(*_a, **_k):
        order.append("cut")
        return True

    monkeypatch.setattr(mech, "_set_device_pollers", stop_pollers)
    monkeypatch.setattr(mech, "clear_readers_before_reset", no_readers)
    monkeypatch.setattr(mech, "restore_pollers", restore)
    monkeypatch.setattr(mech, "_post_reset_cut", cut)
    monkeypatch.setattr(mech, "scope_active", lambda: SCOPE)
    t0 = time.monotonic()
    rc, _ = await asyncio.wait_for(mech.reset_with_quiesce(list(RESET), lambda _m: None, wait_until=t0 + bound), timeout=10)
    took = time.monotonic() - t0
    assert rc is None
    assert bound - 0.2 <= took < bound + 1.0
    assert ("reset_overran" in events) is (bound > overrun) and "reset_timeout" in events
    assert order == ["pollers off"], f"pollers or chips touched while the reset still cycles: {order}"
    assert stopped == []
