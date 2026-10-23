# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A chip leaving the bus with reads in flight stops the broker's own readers within seconds (03 I86).

An Advisory Non-Fatal AER error (the ``NonFatalErr`` row of ``aer_dev_correctable``: a completion
timeout the device downgraded) on a chip's own path is the kernel's first sign of it. With such a
rise, one all-ones sample of that chip confirms it at once instead of SAMPLE_INTERVAL_SEC later, and
the valve SIGSTOPs the fabric check and the aiclk ceiling before the PCI remove. The hold stays, and
the stopped fabric run is no verdict, so only a later full pass can lift it. No device is touched.
"""

import json
import signal
import subprocess
import sys

import pytest

import tests.test_deadchip_detach_before_kill as _detach
import tests.test_offbus_early_watch as _early
from tests.conftest import REAL_RUN_SNAPSHOT
from tests.test_deadchip_detach_before_kill import FABRIC, TENANT
from tests.test_offbus_early_watch import DEAD, LIVE
from tests.test_reset import _snapshot
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import FABRIC_DEBT
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import pci

valve = _detach.valve  # shared fixtures
watch = _early.watch

PATHS = {"13": ["0000:02:00.0", "0000:00:01.1"], "14": ["0000:03:00.0", "0000:00:01.2"]}


def _sig(port1: int, port2: int = 0) -> dict:
    return {"chips": ["13", "14"], "aer": {"0000:00:01.1": port1, "0000:00:01.2": port2}, "paths": PATHS}


def test_link_signals_counts_advisory_nonfatal_and_names_each_chip_path(tmp_path):
    root = tmp_path / "pci" / "0000:00:01.1"
    ep = root / "0000:02:00.0"
    ep.mkdir(parents=True)
    (ep / "aer_dev_correctable").write_text("RxErr 40\nNonFatalErr 3\nTOTAL_ERR_COR 43\n")
    c = pci.SYSFS_CLASS_DIR / "tenstorrent!0"
    c.mkdir()
    (c / "device").symlink_to(ep)
    sig = pci.link_signals()
    assert sig["aer"]["0000:02:00.0"] == 3, "Advisory Non-Fatal counts; RxErr does not"
    assert sig["paths"]["0"] == ["0000:02:00.0", "0000:00:01.1"]


@pytest.mark.asyncio
async def test_an_aer_rise_on_the_chips_path_confirms_on_the_first_all_ones_sample(watch, monkeypatch):
    s, clock, isolated, events = watch
    reads = iter([_sig(0), _sig(14)])
    monkeypatch.setattr(srv, "link_signals", lambda: next(reads))
    await s.poll_link()
    await s.poll_link()
    start = clock.t
    await s.check_for_dead_chips({"13": DEAD, "14": LIVE})
    assert isolated == ["13"] and clock.t - start == 0, "time to detach: 0 s after the first all-ones read"
    assert ("offbus_aer_confirmed", {"chips": ["13"]}) in events


@pytest.mark.asyncio
async def test_no_false_trigger_from_another_path_a_stale_rise_aer_alone_or_a_reset(watch, monkeypatch):
    s, clock, isolated, _ = watch
    reads = iter([_sig(0, 0), _sig(0, 9)])  # the rise is on chip 14's port, not 13's
    monkeypatch.setattr(srv, "link_signals", lambda: next(reads))
    await s.poll_link()
    await s.poll_link()
    await s.check_for_dead_chips({"13": DEAD, "14": LIVE})
    assert isolated == [], "a rise on another chip's path corroborates nothing"
    await s.check_for_dead_chips({"13": LIVE, "14": LIVE})
    assert isolated == [], "AER with every chip answering isolates nothing"
    clock.t += telemetry.FAST_WINDOW_SEC + 1
    s.dead_chip_strikes.clear()
    await s.check_for_dead_chips({"13": LIVE, "14": DEAD})
    assert isolated == [], "a rise older than FAST_WINDOW_SEC corroborates nothing"
    s.dead_chip_strikes.clear()
    monkeypatch.setattr(srv, "link_signals", lambda: _sig(50, 50))
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", True)
    await s.poll_link()
    assert s.aer_at == {}, "a rise during a reset records nothing"


@pytest.mark.asyncio
async def test_drop_during_the_fabric_check_stops_it_before_the_remove_and_keeps_the_hold(valve, monkeypatch):
    log, _ = valve
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    await srv.sampler.isolate_dead_chips(["13"])
    assert log[0] == ("stop", FABRIC), f"the fabric check must stop before the remove: {log}"
    assert log.index(("detach", "13")) < log.index(("kill", FABRIC))
    assert ("stop", TENANT) not in log, "a user job is not stopped; its kill policy is unchanged"
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stays"
    assert FABRIC_DEBT.owed, "a stopped run is no verdict: a full pass is owed before any lift"


@pytest.mark.asyncio
async def test_drop_during_a_hold_escalation_check_stops_it_and_the_hold_stays(valve, monkeypatch):
    log, detach_ok = valve
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    srv._mark_device_dirty("an open hold under escalation", why="heartbeat")
    detach_ok["13"] = False  # the remove fails: nobody is killed, the readers stay stopped
    sent = []
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    await srv.sampler.isolate_dead_chips(["13"])
    assert sent[0] == (FABRIC, signal.SIGSTOP) and (FABRIC, signal.SIGKILL) not in sent
    assert srv.fsm.state is not ServerState.HEALTHY
    assert FABRIC_DEBT.owed


class _Ceiling:
    pid = 4242
    returncode = None


@pytest.mark.asyncio
async def test_drop_during_the_aiclk_ceiling_stops_it_before_the_remove(valve, monkeypatch):
    log, _ = valve
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(srv.CEILING, "proc", _Ceiling())
    await srv.sampler.isolate_dead_chips(["13"])
    assert log.index(("stop", _Ceiling.pid)) < log.index(("detach", "13")), log
    assert ("pollers", False) in log, "telemetry and health probes stop reading too"


@pytest.mark.asyncio
async def test_a_finished_reader_is_not_signalled(valve, monkeypatch):
    log, _ = valve
    done = _Ceiling()
    done.returncode = 0
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(srv.CEILING, "proc", done)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    await srv.sampler.isolate_dead_chips(["13"])
    assert ("stop", _Ceiling.pid) not in log
    assert not FABRIC_DEBT.owed, "no fabric run was stopped, so none is marked killed"


@pytest.mark.asyncio
async def test_after_the_abort_only_a_fresh_full_fabric_pass_lets_the_hold_lift(valve, monkeypatch):
    """Goal 11: the stopped run is no verdict; the hold lifts only on a full pass that came after it."""
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    monkeypatch.setattr(FABRIC_DEBT, "runs_here", lambda expected: True)
    FABRIC_DEBT.note_fabric(True)  # a pass from before the drop
    srv._mark_device_dirty("chip(s) 13 fell off the PCIe bus", why="heartbeat")
    await srv.sampler.isolate_dead_chips(["13"])
    assert srv.fsm.state is not ServerState.HEALTHY
    assert srv._hold_lift_unproven(), "the earlier pass no longer vouches for a lift"
    FABRIC_DEBT.note_fabric(True)  # a full pass with every cabled link, after recovery
    assert not FABRIC_DEBT.owed and srv._hold_lift_unproven() == ""


@pytest.mark.asyncio
async def test_an_aer_rise_just_after_a_reset_corroborates_no_first_sample(watch, monkeypatch):
    """The reset's own errors can post a poll after it ends: such a rise leaves the I79 proof in place."""
    s, clock, isolated, _ = watch
    reads = iter([_sig(0), _sig(14), _sig(30)])
    monkeypatch.setattr(srv, "link_signals", lambda: next(reads))
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", True)
    await s.poll_link()
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False)
    clock.t += 2
    await s.poll_link()
    await s.check_for_dead_chips({"13": DEAD, "14": LIVE})
    assert isolated == [] and s.aer_at == {}, "a rise inside POST_STEP_AER_QUIET_SEC isolates nothing at once"
    clock.t += telemetry.POST_STEP_AER_QUIET_SEC
    s.dead_chip_strikes.clear()
    s.bad_since.clear()
    await s.poll_link()
    await s.check_for_dead_chips({"13": DEAD, "14": LIVE})
    assert isolated == ["13"], "past the quiet window a real drop still isolates on the first sample"


class _Reader:
    def __init__(self, pid, returncode=None):
        self.pid, self.returncode = pid, returncode


SNAPSHOT, ETH = 7001, 7002


@pytest.mark.asyncio
async def test_drop_during_the_snapshot_stops_it_before_the_remove_and_kills_it_after(valve, monkeypatch):
    log, _ = valve
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(srv.health_monitor, "snapshot_proc", _Reader(SNAPSHOT), raising=False)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    await srv.sampler.isolate_dead_chips(["13"])
    assert log.index(("stop", SNAPSHOT)) < log.index(("detach", "13")) < log.index(("kill", SNAPSHOT)), log
    assert FABRIC_DEBT.owed, "the full check it belonged to is no verdict: a full pass is owed"


@pytest.mark.asyncio
async def test_drop_during_the_eth_selftest_stops_it_before_the_remove_and_kills_it_after(valve, monkeypatch):
    log, _ = valve
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(srv, "eth_selftest_proc", _Reader(ETH), raising=False)
    await srv.sampler.isolate_dead_chips(["13"])
    assert log.index(("stop", ETH)) < log.index(("detach", "13")) < log.index(("kill", ETH)), log


@pytest.mark.asyncio
async def test_finished_snapshot_and_eth_selftest_are_not_signalled(valve, monkeypatch):
    log, _ = valve
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(srv.health_monitor, "snapshot_proc", _Reader(SNAPSHOT, 0), raising=False)
    monkeypatch.setattr(srv, "eth_selftest_proc", _Reader(ETH, 0), raising=False)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")
    await srv.sampler.isolate_dead_chips(["13"])
    assert not [e for e in log if e[1] in (SNAPSHOT, ETH)], log
    assert not FABRIC_DEBT.owed


def test_a_snapshot_the_valve_stopped_mid_run_gives_no_pass(valve, monkeypatch):
    """Its later exit 0 with every chip listed measured nothing: no pass, the hold's debt stays."""
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "")

    def run(self, argv, timeout_sec):
        self.snapshot_proc = _Reader(SNAPSHOT)
        srv._freeze_device_readers("chip(s) 13 fell off the PCIe bus")
        self.snapshot_proc = None
        return subprocess.CompletedProcess(argv, 0, json.dumps(_snapshot(2)), "")

    monkeypatch.setattr(HealthMonitor, "_run_snapshot", run, raising=False)
    ok, detail = srv.health_monitor._verify_device_health_body(2)
    assert ok is False and "no verdict" in detail, detail
    assert FABRIC_DEBT.owed


def test_the_snapshot_runs_tracked_in_its_own_session_and_a_timeout_ends_it(monkeypatch):
    m = srv.health_monitor
    seen = {}

    real_popen = subprocess.Popen

    def popen(argv, **k):
        p = real_popen(argv, **k)
        seen["session"] = k.get("start_new_session")
        return p

    monkeypatch.setattr(monitor_mod.subprocess, "Popen", popen)
    done = REAL_RUN_SNAPSHOT(m, [sys.executable, "-c", "print('ok')"], 30)
    assert done.returncode == 0 and done.stdout.strip() == "ok"
    assert seen["session"] is True and m.snapshot_proc is None
    with pytest.raises(subprocess.TimeoutExpired):
        REAL_RUN_SNAPSHOT(m, [sys.executable, "-c", "import time; time.sleep(30)"], 0.2)
    assert m.snapshot_proc is None


@pytest.mark.asyncio
async def test_an_eth_selftest_the_valve_stopped_mid_run_arms_nothing(valve, monkeypatch):
    """Its exit 0 measured nothing: the rung stays off and retryable, and no frozen core is claimed."""
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "")
    monkeypatch.delenv("TTDEV_ETH_CHECK_ARMED")
    monkeypatch.setattr(srv, "eth_check_armed", False)
    monkeypatch.setattr(srv.eth, "build", lambda: (["eth-read"], {}))

    class _Proc(_Reader):
        async def communicate(self):
            srv._freeze_device_readers("chip(s) 13 fell off the PCIe bus")
            self.returncode = 0
            return b"all eth cores advancing\n", None

    async def spawn(*argv, **k):
        return _Proc(ETH)

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", spawn)
    assert await srv.selftest_eth_heartbeat() == ""
    assert srv.eth_check_armed is False and "dead-chip isolation" in srv.eth_check_disarm_reason
    assert srv.eth_rearm_retryable and srv.eth_selftest_proc is None
