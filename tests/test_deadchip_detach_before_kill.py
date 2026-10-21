# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The out-of-reset dead-chip valve detaches a chip before it kills anything (03 I84).

tt-kmd's fd-close path sends the chip a power-state message unless the device is marked detached,
and only the PCI remove marks it. Killing a holder of an off-bus chip that is still attached, or
stopping a poller that has it open, makes the kernel itself reach the chip. So every kill and
every poller stop comes after a successful remove; a failed, missing, hung or partial remove kills
nobody and leaves the hold to its bounded recovery. No process is spawned and no device touched:
the remove, the holder scan and every signal are stubbed.
"""

import asyncio
import os
import signal
import threading

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import read_health_events
from tt_device_mcp.health.monitors import heartbeat

ALL_ONES = heartbeat.ALL_ONES
TENANT = 4242
FABRIC = 9999


class _FakeProc:
    pid = FABRIC


@pytest.fixture
def valve(monkeypatch):
    """One ordered log of every remove, kill and poller change the valve makes."""
    srv.device_op_lock = None
    srv.isolated_chips = set()
    srv._pollers_owed = ""
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: f"0000:46:00.{idx}")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=TENANT, uid=1000)], complete=True)
    )
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", _FakeProc())
    monkeypatch.setattr(srv, "current_process", None)
    log = []
    detach_ok = {}
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: log.append(("detach", idx)) or detach_ok.get(idx, True))
    monkeypatch.setattr(srv.os, "kill", lambda pid, sig: log.append(("kill", pid)))
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: log.append(("kill", pid)))

    async def pollers(active, _log):
        log.append(("pollers", active))
        return []

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    return log, detach_ok


def _kills(log):
    return [e for e in log if e[0] in ("kill", "pollers")]


def _refusals():
    return read_health_events(kinds={"chip_dead_kill_refused"})


@pytest.mark.asyncio
async def test_every_kill_and_poller_stop_comes_after_the_detach(valve):
    log, _ = valve
    await srv.sampler.isolate_dead_chips(["13", "14"])

    detaches = [i for i, e in enumerate(log) if e[0] == "detach"]
    kills = [i for i, e in enumerate(log) if e[0] in ("kill", "pollers")]
    assert [log[i][1] for i in detaches] == ["13", "14"]
    assert ("kill", TENANT) in log and ("kill", FABRIC) in log and ("pollers", False) in log
    assert max(detaches) < min(kills), f"a kill came before a detach: {log}"
    assert not _refusals()
    assert "13" in srv._pollers_owed, "the stopped pollers are owed a restart"
    assert srv.fsm.state is not ServerState.HEALTHY, "the valve keeps the hold"


@pytest.mark.asyncio
async def test_a_failed_detach_kills_nobody_and_keeps_the_hold(valve):
    log, detach_ok = valve
    detach_ok["13"] = False
    await srv.sampler.isolate_dead_chips(["13"])

    assert log == [("detach", "13")], f"something was killed or stopped without a detach: {log}"
    refused = _refusals()
    assert refused and refused[-1]["reason"] == "detach_failed"
    assert refused[-1]["chips"] == ["13"] and refused[-1]["host_at_risk"] is True
    assert not srv._pollers_owed
    assert srv.fsm.state is not ServerState.HEALTHY, "a refused kill still holds the device"


@pytest.mark.asyncio
async def test_one_failed_detach_of_two_kills_nobody(valve):
    log, detach_ok = valve
    detach_ok["14"] = False
    await srv.sampler.isolate_dead_chips(["13", "14"])

    assert not _kills(log), log
    refused = _refusals()
    assert refused[-1]["reason"] == "detach_failed"
    assert refused[-1]["chips"] == ["14"] and refused[-1]["detached"] == ["13"]


@pytest.mark.asyncio
async def test_with_no_detach_wired_nobody_is_killed(valve, monkeypatch):
    log, _ = valve
    monkeypatch.setattr(srv.sampler._deps, "isolate_chip", None)
    await srv.sampler.isolate_dead_chips(["13"])

    assert not _kills(log), log
    assert _refusals()[-1]["reason"] == "detach_unavailable"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_hung_detach_is_bounded_and_the_loop_stays_responsive(valve, monkeypatch):
    """The remove runs in a worker thread with one bound for all chips: a remove that hangs is not
    detached, kills nobody, and the next chip is not tried behind it, while the event loop runs on."""
    log, _ = valve
    monkeypatch.setattr(telemetry, "DETACH_TIMEOUT_SEC", 0.3)
    release = threading.Event()

    def hung_remove(idx):
        log.append(("detach", idx))
        release.wait(5)
        return True

    monkeypatch.setattr(srv, "isolate_chip", hung_remove)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.get_running_loop().create_task(ticker())
    try:
        await asyncio.wait_for(srv.sampler.isolate_dead_chips(["13", "14"]), timeout=3.0)
    finally:
        t.cancel()
        release.set()

    assert ticks >= 5, "the event loop stalled behind the remove"
    assert log == [("detach", "13")], f"a kill, a poller stop or a second remove followed a hung one: {log}"
    assert read_health_events(kinds={"chip_detach_timeout"})[-1]["chip"] == "13"
    assert _refusals()[-1]["reason"] == "detach_failed"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_reset_that_started_before_the_kill_keeps_every_holder(valve, monkeypatch):
    log, _ = valve
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "ttdev-reset-1.scope")
    await srv.sampler.isolate_dead_chips(["13"])

    assert ("detach", "13") in log and not _kills(log), log
    assert _refusals()[-1]["reason"] == "reset_started"


@pytest.mark.asyncio
async def test_another_all_ones_chip_not_yet_detached_defers_the_kill_to_its_own_valve(valve):
    """Chip 13 is confirmed while chip 14 is on its first strike: 13 is detached at once, but a kill
    now would close fds on 14, still attached. 14's own confirmation detaches it, then kills."""
    log, _ = valve
    live = lambda b: [b, 800, 800, 0, 0, 0]  # noqa: E731
    await srv.sampler.check_for_dead_chips({"0": live(1), "13": live(ALL_ONES), "14": live(2)})
    await srv.sampler.check_for_dead_chips({"0": live(2), "13": live(ALL_ONES), "14": live(ALL_ONES)})
    assert ("detach", "13") in log
    assert ("kill", TENANT) not in log and ("kill", FABRIC) not in log, log
    assert _refusals()[-1]["reason"] == "chip_not_detached" and _refusals()[-1]["chips"] == ["14"]

    # 13 is gone from sysfs once removed; 14 reads all-ones a second time.
    await srv.sampler.check_for_dead_chips({"0": live(3), "14": live(ALL_ONES)})
    i14 = log.index(("detach", "14"))
    kills = [i for i, e in enumerate(log) if e[0] == "kill"]
    assert kills and min(kills) > i14, log


@pytest.mark.asyncio
async def test_a_chip_whose_earlier_detach_failed_blocks_every_later_kill(valve):
    log, detach_ok = valve
    detach_ok["13"] = False
    live = lambda b: [b, 800, 800, 0, 0, 0]  # noqa: E731
    await srv.sampler.check_for_dead_chips({"0": live(1), "13": live(ALL_ONES), "14": live(1)})
    await srv.sampler.check_for_dead_chips({"0": live(2), "13": live(ALL_ONES), "14": live(ALL_ONES)})
    await srv.sampler.check_for_dead_chips({"0": live(3), "13": live(ALL_ONES), "14": live(ALL_ONES)})
    assert ("detach", "14") in log
    assert not [e for e in log if e[0] == "kill"], log
    assert _refusals()[-1]["reason"] == "chip_not_detached" and _refusals()[-1]["chips"] == ["13"]


@pytest.mark.asyncio
async def test_the_broker_never_signals_itself(monkeypatch):
    sent = []
    monkeypatch.setattr(
        srv,
        "enumerate_device_holders",
        lambda: HolderScan(
            holders=[DeviceHolder(pid=os.getpid(), uid=0), DeviceHolder(pid=TENANT, uid=1000)], complete=True
        ),
    )
    monkeypatch.setattr(srv.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", None)
    monkeypatch.setattr(srv, "current_process", None)
    await srv._kill_device_holders("chip(s) 13 left the PCIe bus")
    assert sent == [(TENANT, signal.SIGKILL)], sent
