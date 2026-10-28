# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The always-on dead-chip watch contains a chip that drops off PCIe on its own thread (03 I96).

The watch reads kernel memory only (AER counters, the driver's node list) on every tick, idle or
busy. A chip it finds dead (a node that vanished outside a reset, or an AER burst on a chip the
latest sample read all-ones) is contained before anything reads it again: every user of the device
is SIGSTOPped, the chip is PCI-removed through the one PCI writer, and only then is anything killed.
A remove that times out or fails latches PCI-wedged, which asks for the power cycle, and kills
nobody. A reset's own errors are excused, a burst with no dead chip touches no tenant, and a
blackout (classes A/C) is left to its direct power cycle. The hold this opens lifts only on a
just-passed full check with fabric on every cabled link (03 I78). No device is touched and no
process is spawned: the remove, the holder scan and every signal are stubbed.
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_aer_check_stop import _sig
from tests.test_aer_stop_contain import _held, _rows, _sample, _stop_row, watch  # noqa: F401 - the fixture
from tests.test_deadchip_detach_before_kill import FABRIC, TENANT, valve  # noqa: F401 - the fixture
from tests.test_hold_lift_full_check import FABRIC as VERDICT
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import chip_state
from tt_device_mcp.health.post_reset import FABRIC_DEBT

JOB, JOB_CHILD = 5000, 5001
ROW_FIELDS = (
    "trigger_ts",
    "sigstop_ts",
    "remove_ts",
    "trigger_to_sigstop_s",
    "trigger_to_remove_s",
    "trigger_to_kill_s",
    "removed",
    "chips",
    "stuck",
    "pci_wedged",
)


class _Reader:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None


def _idle(monkeypatch):
    """No broker check runs: the valve fixture's fabric reader is gone."""
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)


def _job(monkeypatch):
    """A tenant job running: its leader's group and one more group its scope holds."""
    monkeypatch.setattr(srv, "current_process", _Reader(JOB))
    monkeypatch.setattr(srv, "current_job_id", None)
    monkeypatch.setattr(srv.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        srv,
        "job_processes",
        lambda *a, **k: {JOB: SimpleNamespace(pgid=JOB), JOB_CHILD: SimpleNamespace(pgid=JOB_CHILD)},
    )


def _feed(monkeypatch, reads):
    monkeypatch.setattr(srv, "link_signals", lambda: reads.pop(0))


def _drop(w, monkeypatch, *gone):
    """Chip node(s) ``gone`` vanish from the driver between two watch reads."""
    left = [c for c in ("13", "14") if c not in gone]
    _feed(monkeypatch, [_sig(0), dict(_sig(0), chips=left)])
    assert w.poll_once() is None
    w._clock.t += 1
    return w.poll_once()


def _order(log, pids):
    """Every stop of ``pids`` before the remove, every kill of them after it."""
    detach = log.index(("detach", "13"))
    for pid in pids:
        assert ("stop", pid) in log and log.index(("stop", pid)) < detach, f"{pid} stopped before the remove: {log}"
        assert ("kill", pid) in log and log.index(("kill", pid)) > detach, f"{pid} killed after the remove: {log}"
    kills = [i for i, e in enumerate(log) if e[0] in ("kill", "pollers")]
    assert kills and detach < min(kills), f"nothing is killed before the remove: {log}"


def _contained():
    rows = _rows("chip_contained")
    assert rows, "the containment record is on disk"
    return rows[-1]


# --- drops, idle and busy ---------------------------------------------------------------------


def test_an_idle_drop_is_contained_by_the_watch_with_no_check_running(watch, monkeypatch):  # noqa: F811
    """A node that vanishes with no job and no check is contained: the holder stopped, then killed;
    the vanished chip needs no remove (it is already gone)."""
    w, log, _detach_ok, _burst = watch
    _idle(monkeypatch)
    assert _drop(w, monkeypatch, "13")
    assert ("detach", "13") not in log, "a vanished chip is already off the driver"
    assert log.index(("stop", TENANT)) < log.index(("kill", TENANT)), log
    row = _contained()
    assert row["chips"] == ["13"] and row["vanished"] == ["13"] and row["removed"] is True
    assert chip_state.is_lost("13") and "13" in srv.isolated_chips
    assert not _rows("check_aer_stop"), "no broker check ran: no check stop record"
    _held()


def test_an_idle_all_ones_burst_is_contained_by_the_watch(watch, monkeypatch):  # noqa: F811
    _w, log, _detach_ok, burst = watch
    _idle(monkeypatch)
    _sample("13")
    assert burst()
    _order(log, [TENANT])
    assert _contained()["removed_chips"] == ["13"]
    _held()


def test_a_drop_during_a_tenant_job_stops_the_job_before_the_remove_and_kills_it_after(
    watch, monkeypatch  # noqa: F811
):
    _w, log, _detach_ok, burst = watch
    _idle(monkeypatch)
    _job(monkeypatch)
    _sample("13")
    assert burst()
    _order(log, [JOB, JOB_CHILD, TENANT])
    row = _contained()
    assert {"pgid": JOB} in row["users_stopped"] and {"pgid": JOB_CHILD} in row["users_stopped"]
    assert row["kill_refused"] is None and row["killed"] >= 3
    _held()


@pytest.mark.parametrize("check", ["fabric", "snapshot", "eth_selftest", "aiclk_ceiling"])
def test_a_drop_during_each_broker_check_stops_its_reader_before_the_remove(watch, monkeypatch, check):  # noqa: F811
    _w, log, _detach_ok, burst = watch
    reader = 7100
    _idle(monkeypatch)
    if check == "fabric":
        monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", _Reader(reader))
    elif check == "snapshot":
        monkeypatch.setattr(srv.health_monitor, "snapshot_proc", _Reader(reader))
    elif check == "eth_selftest":
        monkeypatch.setattr(srv, "eth_selftest_proc", _Reader(reader))
    else:
        monkeypatch.setattr(srv.CEILING, "proc", _Reader(reader))
    _sample("13")
    assert burst()
    assert log[0] == ("stop", reader), f"the check's reader is the first stopped: {log}"
    _order(log, [reader, TENANT])
    stop = _stop_row()
    assert stop["contained"] == ["13"] and stop["stopped"], stop
    if check in ("fabric", "snapshot"):
        assert FABRIC_DEBT.owed, "the stopped check is no verdict"
    _held()


def test_a_blocked_event_loop_does_not_delay_the_containment(watch):  # noqa: F811
    """The loop blocked for up to 60 s: the watch thread still stops, removes and kills within the
    budget; the loop's half (the hold, the pollers) runs once the loop is back."""
    _w, log, _detach_ok, burst = watch
    loop = asyncio.new_event_loop()
    started, gate = threading.Event(), threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(lambda: (started.set(), gate.wait(60)))
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        assert started.wait(5)
        srv.sampler.loop = loop
        _sample("13")
        t0 = time.monotonic()
        assert burst()
        took = time.monotonic() - t0
        assert not gate.is_set(), "the loop was blocked throughout"
        assert took < 15, f"contained in {took:.1f}s with the loop blocked"
        _order(log, [FABRIC, TENANT])
        row = _contained()
        assert row["trigger_to_remove_s"] < 15 and row["removed"] is True, row
        fut = srv.sampler.aer_containment
        assert fut is not None and not fut.done(), "the loop's half waits for the loop"
    finally:
        gate.set()
    fut.result(10)
    assert srv.fsm.state is not ServerState.HEALTHY
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    loop.close()
    _held()


@pytest.mark.asyncio
async def test_a_remove_that_times_out_latches_pci_wedged_and_asks_for_the_power_cycle(
    watch, monkeypatch  # noqa: F811
):
    _w, log, _detach_ok, burst = watch
    stuck = threading.Event()
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: log.append(("detach", idx)) or stuck.wait(30))
    monkeypatch.setattr(telemetry, "DETACH_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(telemetry, "WATCH_HARD_BOUND_SEC", 0.3)
    monkeypatch.setattr(telemetry, "WATCH_REMOVE_BUDGET_SEC", 0.3)
    monkeypatch.setattr(srv, "_pci_wedged_asked", None, raising=False)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "_recovery_degraded", lambda: True)
    monkeypatch.setattr(srv, "_host_action_blocks_escalation", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)
    rungs = []

    async def host_rung(age, log_):
        rungs.append(age)

    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)
    srv.sampler.loop = asyncio.get_running_loop()
    _sample("13")
    try:
        assert await asyncio.to_thread(burst)
        await asyncio.wait_for(asyncio.wrap_future(srv.sampler.aer_containment), 10)
        assert srv.PCI_WRITER.latched, "a remove that timed out latches PCI-wedged"
        assert not [e for e in log if e[0] == "kill"], f"no kill while the chip may be attached: {log}"
        row = _contained()
        assert row["removed"] is False and row["stuck"] == ["13"] and row["pci_wedged"] is True, row
        assert _rows("chip_dead_kill_refused")[-1]["reason"] == "detach_failed"
        # The hold the containment opened, as the watchdog's next tick dates it: seconds old.
        monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=5)).isoformat())
        srv._check_hold_deadline(datetime.now())
        if srv._forced_escalation_task is not None:
            await asyncio.wait_for(srv._forced_escalation_task, 5)
        assert len(rungs) == 1 and rungs[0] < 60, "the latch asks for the power cycle at once, not at the 10-min mark"
        assert srv._pci_wedged_asked == srv.PCI_WRITER.latched_at
        _held()
    finally:
        stuck.set()


# --- what the watch leaves alone --------------------------------------------------------------


@pytest.mark.parametrize("busy", ["idle", "tenant job"])
def test_a_benign_burst_kills_no_tenant(watch, monkeypatch, busy):  # noqa: F811
    """An AER burst with every chip still answering, no check running: nobody is stopped or killed;
    the sampler's fast cadence (I79) watches the same counters."""
    w, log, _detach_ok, burst = watch
    _idle(monkeypatch)
    if busy == "tenant job":
        _job(monkeypatch)
    _sample()
    assert burst() is None
    assert log == [], log
    assert not _rows("chip_contained") and not _rows("check_aer_stop")
    assert srv.fsm.state is ServerState.HEALTHY or not srv.isolated_chips


@pytest.mark.parametrize("step", ["reset_in_flight", "scope_seen", "recovering"])
@pytest.mark.parametrize("fault", ["burst", "vanish"])
def test_aer_and_node_churn_inside_a_reset_are_excused(watch, monkeypatch, step, fault):  # noqa: F811
    w, log, _detach_ok, burst = watch
    if step == "reset_in_flight":
        monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", True)
    elif step == "scope_seen":
        monkeypatch.setattr(srv.sampler, "scope_seen", True)
    else:
        monkeypatch.setattr(chip_state, "recovering", lambda: True)
    _sample("13")
    got = burst() if fault == "burst" else _drop(w, monkeypatch, "13")
    assert got is None
    assert log == [], f"a reset's own errors stop nobody: {log}"
    assert not _rows("chip_contained") and not _rows("check_aer_stop") and not _rows("chip_watch_trigger")


@pytest.mark.parametrize("fault", ["every node vanished", "every chip all-ones"])
def test_a_blackout_keeps_the_class_a_c_path(watch, monkeypatch, fault):  # noqa: F811
    """A whole-tray or all-chips-off drop (classes A/C) is not contained here: no remove, no kill;
    its direct power cycle (spec 04) stands."""
    w, log, _detach_ok, burst = watch
    _idle(monkeypatch)
    if fault == "every node vanished":
        assert _drop(w, monkeypatch, "13", "14") is None
        assert _rows("chip_watch_trigger")[-1]["blackout"] is True
    else:
        _sample("13", "14")
        assert burst() is None
    assert not [e for e in log if e[0] in ("detach", "kill", "stop")], log
    assert not _rows("chip_contained")
    assert not srv.isolated_chips


# --- foreign holders --------------------------------------------------------------------------


def test_a_foreign_holder_is_stopped_before_the_remove_and_killed_only_after_it(watch, monkeypatch):  # noqa: F811
    _w, log, _detach_ok, burst = watch
    _idle(monkeypatch)
    _sample("13")
    assert burst()
    _order(log, [TENANT])
    assert [u for u in _contained()["users_stopped"] if u.get("pid") == TENANT], "the holder is named as stopped"


def test_a_failed_remove_leaves_the_foreign_holder_stopped(watch, monkeypatch):  # noqa: F811
    """The remove failed: the chip stays attached, so nothing is killed and nothing resumed; the
    holder stays stopped for the power cycle the latch asks for."""
    _w, log, detach_ok, burst = watch
    _idle(monkeypatch)
    monkeypatch.setattr(srv.PCI_WRITER, "on_latch", lambda reason: None)
    detach_ok["13"] = False
    _sample("13")
    assert burst()
    assert ("stop", TENANT) in log and log.index(("stop", TENANT)) < log.index(("detach", "13"))
    assert not [e for e in log if e[0] == "kill"], f"no kill and no resume of a stopped holder: {log}"
    row = _contained()
    assert row["removed"] is False and row["stuck"] == ["13"] and row["pci_wedged"] is True
    assert srv.PCI_WRITER.latched
    _held()


# --- the record -------------------------------------------------------------------------------


def test_the_chip_contained_row_carries_its_fields_and_latencies_within_budget(watch, monkeypatch):  # noqa: F811
    _w, _log, _detach_ok, burst = watch
    _sample("13")
    assert burst()
    row = _contained()
    missing = [f for f in ROW_FIELDS if f not in row]
    assert not missing, f"chip_contained lacks {missing}: {row}"
    assert row["chips"] == ["13"] and row["removed"] is True and row["stuck"] == [] and row["pci_wedged"] is False
    assert row["trigger_ts"] <= row["sigstop_ts"] <= row["remove_ts"]
    assert 0 <= row["trigger_to_sigstop_s"] <= telemetry.WATCH_STOP_BUDGET_SEC, row
    assert row["trigger_to_sigstop_s"] <= row["trigger_to_remove_s"] <= telemetry.WATCH_REMOVE_BUDGET_SEC, row
    assert row["trigger_to_remove_s"] <= row["trigger_to_kill_s"] < telemetry.DETACH_TIMEOUT_SEC, row
    assert abs(row["trigger_ts"] - time.time()) < 60, "wall clock, not the watch's window clock"
    assert row["pci"]["13"], "the removed chip's PCI address is named"
    assert row["kill_refused"] is None and row["killed"] >= 2


# --- the hold lifts only on a just-passed full check with fabric ------------------------------

LIFTS = {
    "idle relift": "idle relift: verified healthy",
    "post-reset check": "reset_verified: tt-smi -glx_reset",
    "startup": "gate/startup: verified healthy",
    "post-cycle": "idle relift: every chip back on the bus, full gate passed",
}


@pytest.mark.parametrize("verdict", ["pass", "fail", "missing", "pass before the drop"])
@pytest.mark.parametrize("path", sorted(LIFTS))
def test_the_contained_hold_lifts_only_on_a_just_passed_full_check_with_fabric(
    watch, monkeypatch, path, verdict  # noqa: F811
):
    """03 I78 on every lift path out of a containment's hold: a fabric check that failed, found fewer
    links than the cabling expects, or passed before the drop keeps the hold."""
    _w, _log, _detach_ok, burst = watch
    if verdict == "pass before the drop":
        FABRIC_DEBT.note_evidence({"fabric": dict(VERDICT["pass"])})
    _sample("13")
    assert burst()
    srv.sampler.aer_containment.result(10)
    _held()
    if verdict != "pass before the drop":
        FABRIC_DEBT.note_evidence({"fabric": dict(VERDICT[verdict])})
    released = srv._clear_device_dirty(verified=True, why=LIFTS[path])
    assert released is (verdict == "pass"), f"{path}: {verdict}"
    if verdict != "pass":
        _held()
