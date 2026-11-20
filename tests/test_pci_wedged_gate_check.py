# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I76: a PCI-wedged latch set while the gate's full check runs ends that check at once, no verdict.

Each check here is a real ``sleep`` in its own session, run through the real ``run_probe``; no device.
"""

import asyncio
import os
import signal
import time
from datetime import datetime

import pytest

from tests.test_pci_wedged_latch import _tick, _wedged_hold, _writer
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import evidence
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import fabric, subproc
from tt_device_mcp.health.post_reset import FABRIC_DEBT

EXIT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)


def _proc_state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


def _stopped(pid: int, within: float = 5.0) -> bool:
    """A stop is delivered asynchronously: on a loaded host /proc shows it a little after killpg returns."""
    deadline = time.monotonic() + within
    while _proc_state(pid) != "T":
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.fixture
def signals(monkeypatch):
    """Every killpg the code under test sends, in order, still delivered."""
    sent = []
    real = os.killpg

    def killpg(pgid, sig):
        sent.append((pgid, sig))
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    return sent


@pytest.fixture
def started():
    """The pids this test started; each is killed by pid at the end (a stopped one included)."""
    pids = []
    yield pids
    for pid in pids:
        for sig in (signal.SIGKILL, signal.SIGCONT):
            try:
                os.kill(pid, sig)
            except OSError:
                pass


@pytest.fixture
def check(monkeypatch, health_deps, started):
    """A fabric check (operator wrapper path, so the exit code is the verdict) that runs ``argv``."""
    m = HealthMonitor(health_deps)
    rows = []
    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "fabric-wrapper")
    monkeypatch.setattr(m, "_begin_action_row", lambda *a, **k: None)
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: rows.append(a))
    monkeypatch.setattr(subproc, "ABORT_POLL_SEC", 0.05)
    argv = {"argv": ["sleep", "60"]}
    monkeypatch.setattr(fabric, "build_command", lambda descriptor=True: (list(argv["argv"]), dict(os.environ)))
    calls = []

    async def plain_terminate(pid, *a, **k):
        calls.append(pid)
        os.killpg(pid, signal.SIGKILL)
        return "SIGKILL"

    monkeypatch.setattr(srv, "_terminate_process_group", plain_terminate)
    return m, argv, rows, calls


async def _reader_pid(m, started) -> int:
    for _ in range(200):
        proc = m.fabric_check_proc
        if proc is not None:
            started.append(proc.pid)
            return proc.pid
        await asyncio.sleep(0.01)
    raise AssertionError("the check never started")


@pytest.mark.asyncio
async def test_a_latch_during_the_check_ends_it_at_once_with_no_verdict(monkeypatch, check, signals, started):
    writer = _writer(monkeypatch)
    m, _, rows, calls = check
    m.last_fabric_ok = True
    FABRIC_DEBT.note_fabric(True)
    task = asyncio.ensure_future(m.verify_fabric_health(180))
    pid = await _reader_pid(m, started)
    writer._latch("test: a remove blocked in the kernel")
    t0 = time.monotonic()
    ok, detail = await asyncio.wait_for(task, 10)
    assert time.monotonic() - t0 < 2, "no wait for the check's own timeout"
    assert ok is None and "PCI-wedged" in detail, detail
    assert _stopped(pid), "the reader is left stopped"
    assert not [s for p, s in signals if p == pid and s in EXIT_SIGNALS], signals
    assert calls == [], "no terminate ladder"
    assert m.last_fabric_ok is None and FABRIC_DEBT.owed, "no verdict, and a full pass is owed"
    assert rows and rows[-1][3] == "stopped"
    evs = [e for e in evidence.read_health_events() if e["kind"] == "fabric_check_no_verdict"]
    assert evs and evs[-1]["cause"] == "pci_wedged"


@pytest.mark.asyncio
async def test_a_latch_already_set_at_the_start_does_not_end_the_check(monkeypatch, check):
    """A pass run under the latch is what may clear it, so it runs to its own end."""
    writer = _writer(monkeypatch)
    m, argv, _, _ = check
    argv["argv"] = ["sh", "-c", "sleep 0.3"]
    writer._latch("test: a remove blocked in the kernel")
    ok, detail = await asyncio.wait_for(m.verify_fabric_health(30), 10)
    assert ok is True, detail


@pytest.mark.asyncio
async def test_a_timed_out_check_under_the_latch_is_stopped_not_killed(monkeypatch, check, signals, started):
    writer = _writer(monkeypatch)
    m, _, _, calls = check
    writer._latch("test: a remove blocked in the kernel")
    task = asyncio.ensure_future(m.verify_fabric_health(0.3))
    pid = await _reader_pid(m, started)
    ok, _ = await asyncio.wait_for(task, 10)
    assert ok is not True
    assert calls == [], "the SIGINT/SIGTERM/SIGKILL ladder never runs under the latch"
    assert (pid, signal.SIGSTOP) in signals
    assert not [s for p, s in signals if p == pid and s in EXIT_SIGNALS], signals
    assert _stopped(pid)


@pytest.mark.asyncio
async def test_a_holder_the_valve_refused_to_kill_gets_no_kill(monkeypatch, check, signals, started):
    """Spec 03 I84: the valve's refusal holds on the check's own terminate path too."""
    _, _, _, calls = check
    proc = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
    started.append(proc.pid)
    os.killpg(proc.pid, signal.SIGSTOP)  # the valve's freeze
    monkeypatch.setattr(srv, "_kill_refused_pids", {proc.pid: srv._read_proc_starttime(proc.pid)})
    how = await srv._terminate_check_reader(proc.pid)
    assert "kill refused" in how and calls == []
    assert not [s for p, s in signals if p == proc.pid and s in EXIT_SIGNALS], signals
    assert _stopped(proc.pid)


@pytest.mark.asyncio
async def test_without_a_refusal_or_the_latch_the_usual_ladder_runs(monkeypatch, check, started):
    _, _, _, calls = check
    proc = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
    started.append(proc.pid)
    monkeypatch.setattr(srv, "_kill_refused_pids", {proc.pid: "a recycled pid's start time"})
    assert await srv._terminate_check_reader(proc.pid) == "SIGKILL"
    assert calls == [proc.pid]
    await asyncio.wait_for(proc.wait(), 5)


@pytest.mark.asyncio
async def test_the_latch_also_ends_the_terminate_wait(monkeypatch, signals, started):
    """A latch set while a timed-out probe's terminate is still in its grace ends that wait too."""
    hold = asyncio.Event()

    async def slow_terminate(pid):
        await hold.wait()

    flag = {"why": ""}
    task = asyncio.ensure_future(
        subproc.run_probe(
            ["sleep", "60"],
            stream=True,
            env=dict(os.environ),
            timeout_sec=0.2,
            track=lambda p: p is not None and started.append(p.pid),
            terminate=slow_terminate,
            abort=lambda: flag["why"],
        )
    )
    await asyncio.sleep(0.6)
    flag["why"] = "PCI-wedged"
    t0 = time.monotonic()
    rc, _ = await asyncio.wait_for(task, 5)
    assert rc is None and time.monotonic() - t0 < 2
    assert started and _stopped(started[0])
    assert not [s for p, s in signals if p == started[0] and s in EXIT_SIGNALS], signals


@pytest.mark.asyncio
async def test_the_gate_check_frees_the_device_op_and_the_power_cycle_is_asked_within_10s(
    monkeypatch, check, signals, started
):
    """End to end: the latch sets mid-check while the gate holds the device op. The watchdog cannot escalate
    while the op is held; the check ends at once, and the next tick asks for the power cycle seconds after the
    latch. The device stays held throughout: no hold lifts without a just-passed full check with fabric."""
    writer = _writer(monkeypatch)
    m, _, _, calls = check
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda action, **k: (True, ""))
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_bound_tenant_blocker", lambda *a: None)
    asked = []

    async def cycle(log, cause):
        asked.append((time.monotonic(), cause))
        return True

    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle)

    async def gate():
        srv.device_op_active = "post-reset full check"
        try:
            return await m.verify_fabric_health(180)
        finally:
            srv.device_op_active = ""

    monkeypatch.setattr(srv, "device_op_active", "")
    task = asyncio.ensure_future(gate())
    pid = await _reader_pid(m, started)
    now = datetime.now()
    host_rung = srv._hold_bound_host_rung
    _wedged_hold(monkeypatch, writer, now)  # dirty, then the latch, mid-check
    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)  # the real rung, down to the request
    t0 = time.monotonic()
    await _tick(now)
    assert asked == [], "nothing escalates while the gate holds the device op"
    ok, _ = await asyncio.wait_for(task, 10)
    assert ok is None
    await _tick(datetime.now())
    assert asked and asked[0][0] - t0 < 10, asked
    assert "PCI-wedged" in asked[0][1]
    assert srv.fsm.state is not ServerState.HEALTHY, "the device stays held"
    assert writer.latched, "the latch clears only on a verified lift"
    assert not [s for p, s in signals if p == pid and s in EXIT_SIGNALS] and calls == []
