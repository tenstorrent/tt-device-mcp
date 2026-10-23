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

import signal

import pytest

from tests.test_deadchip_detach_before_kill import FABRIC, TENANT, valve  # noqa: F401 - fixture
from tests.test_offbus_early_watch import DEAD, LIVE, watch  # noqa: F401 - fixture
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import FABRIC_DEBT
from tt_device_mcp.health.monitors import pci

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
    srv.mark_device_dirty("an open hold under escalation", why="heartbeat")
    detach_ok["13"] = False  # the remove fails: nobody is killed, the readers stay stopped
    sent = []
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    await srv.sampler.isolate_dead_chips(["13"])
    assert sent[0] == (FABRIC, signal.SIGSTOP) and (FABRIC, signal.SIGKILL) not in sent
    assert srv.fsm.state is not ServerState.HEALTHY
    assert FABRIC_DEBT.owed
