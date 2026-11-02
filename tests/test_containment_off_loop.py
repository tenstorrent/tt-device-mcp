# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The dead-chip containment waits on no event loop and no slow I/O (03 I99).

Between the stop and the PCI remove the watch thread does no blocking file, journal or log write:
the chip's lost mark journals through the detached writer, the state file and the log line come
after the remove, and the removed-chip record is written beside the remove, not ahead of it. The
remove has one hard bound counted from the trigger; a remove still running then latches PCI-wedged,
which asks for the power cycle, kills nobody and keeps the hold. The loop's own periodic stats write
runs on a worker thread. No device is touched: the remove, the holder scan and every signal are
stubbed.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_aer_stop_contain import _held, _rows, _sample, watch  # noqa: F401 - the fixture
from tests.test_deadchip_detach_before_kill import FABRIC, TENANT, valve  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.health import chip_state

SLOW = 1.2  # each blocking write, longer than the stop budget


def _contained():
    rows = _rows("chip_contained")
    assert rows, "the containment record is on disk"
    return rows[-1]


@pytest.mark.parametrize("late", [False, True], ids=["inside the bound", "started past the bound"])
def test_a_remove_still_running_at_the_hard_bound_latches_pci_wedged(watch, monkeypatch, late):  # noqa: F811
    """The bound counts from the trigger, not from the stop: a remove blocked in the kernel latches
    PCI-wedged when it passes. A host stall that started the remove past the bound still gives it the
    remove budget. Either way nobody is killed and the hold stays."""
    _w, log, _detach_ok, _burst = watch
    stuck = threading.Event()
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: log.append(("detach", idx)) or stuck.wait(30))
    monkeypatch.setattr(telemetry, "WATCH_HARD_BOUND_SEC", 1.0)
    monkeypatch.setattr(telemetry, "WATCH_REMOVE_BUDGET_SEC", 0.3)
    _sample("13")
    trigger = time.monotonic() - (5.0 if late else 0.4)
    try:
        row = srv.sampler.contain_on_thread(["13"], ["13"], "test burst", trigger_at=trigger)
        took = time.monotonic() - trigger
    finally:
        stuck.set()
    assert srv.PCI_WRITER.latched, "a remove past the bound latches PCI-wedged"
    assert ("detach", "13") in log, f"the remove was tried: {log}"
    if late:
        assert 5.25 <= took < 6.5, f"a late start still gets the remove budget: {took:.2f}s"
    else:
        assert 0.95 <= took < 2.0, f"latched at the bound from the trigger: {took:.2f}s"
    assert row["stuck"] == ["13"] and row["removed"] is False and row["pci_wedged"] is True, row
    assert row == _contained() or _contained()["stuck"] == ["13"]
    assert not [e for e in log if e[0] == "kill"], f"no kill while the chip may be attached: {log}"
    _held()


def test_slow_journal_state_and_log_writes_never_delay_the_stop_or_the_remove(watch, monkeypatch):  # noqa: F811
    """Every synchronous write the containment could make blocks for longer than the stop budget:
    the stop and the remove still land within budget, and the state file is written after the remove."""
    _w, log, _detach_ok, burst = watch

    def slow(tag):
        def write(*a, **k):
            log.append((tag,))
            time.sleep(SLOW)

        return write

    monkeypatch.setattr(chip_state, "publish", slow("publish"))
    monkeypatch.setattr(chip_state, "_event", slow("journal"))
    monkeypatch.setattr(
        srv, "logger", SimpleNamespace(**{k: slow("log") for k in ("debug", "info", "warning", "error")})
    )
    _sample("13")
    assert burst()
    row = _contained()
    assert row["removed"] is True, row
    assert row["trigger_to_sigstop_s"] <= telemetry.WATCH_STOP_BUDGET_SEC, row
    assert row["trigger_to_remove_s"] - row["trigger_to_sigstop_s"] < SLOW / 2, row
    assert row["trigger_to_remove_s"] <= telemetry.WATCH_REMOVE_BUDGET_SEC, row
    detach = log.index(("detach", "13"))
    for tag in ("publish", "journal", "log"):
        early = [i for i, e in enumerate(log) if e == (tag,) and i < detach]
        assert not early, f"no blocking {tag} write before the remove: {log}"
    assert ("publish",) in log[detach:], f"the state file is written after the remove: {log}"
    assert chip_state.is_lost("13"), "the chip is marked lost before the remove, in memory"
    _held()


@pytest.mark.asyncio
async def test_the_periodic_stats_write_runs_off_the_event_loop(monkeypatch, tmp_path):
    loop_thread = threading.get_ident()
    seen: list = []
    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def write(body, path, folder):
        seen.append((threading.get_ident(), body))
        loop.call_soon_threadsafe(done.set)

    monkeypatch.setattr(srv, "_write_stats", write)
    monkeypatch.setattr(srv, "STATS_UPDATE_SEC", 0)
    monkeypatch.setattr(srv, "stats", SimpleNamespace(to_dict=lambda: {"jobs": 1}))
    monkeypatch.setattr(srv, "stats_file", tmp_path / "stats.json")
    monkeypatch.setattr(srv, "stats_dir", tmp_path)
    monkeypatch.setattr(srv, "_update_queue_depth_metric", lambda: None)
    monkeypatch.setattr(srv, "_publish_telemetry_snapshot", lambda: None)
    monkeypatch.setattr(srv.metrics, "write_textfile", lambda: None)
    task = asyncio.create_task(srv.stats_persistence_loop())
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        task.cancel()
    assert seen and seen[0][0] != loop_thread, "the write ran on a worker thread"
    assert seen[0][1] == {"jobs": 1}, "a snapshot taken on the loop"


def test_the_stats_write_keeps_its_file_and_current_link(tmp_path):
    path = tmp_path / "stats-1.json"
    (tmp_path / "current").symlink_to("old.json")
    srv._write_stats({"jobs": 2}, path, tmp_path)
    assert (tmp_path / "current").resolve() == path.resolve()
    assert '"jobs": 2' in path.read_text()
