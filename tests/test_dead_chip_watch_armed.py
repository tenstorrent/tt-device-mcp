# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The broker says at startup whether the dead-chip watch is armed (03 I96).

A watch that logs nothing cannot be confirmed from the logs after an install. Right after the
watch's thread starts, the broker logs one INFO ``I96 dead-chip watch ARMED`` line and journals one
``dead_chip_watch_armed`` row; a watch that cannot run (no link_signals read, no thread, a failed
read, no chip listed) gets one WARNING line and a ``dead_chip_watch_not_armed`` row instead. This
is reporting only: the report never starts, stops or changes the watch, and never raises.
"""

import inspect
from types import SimpleNamespace

from tt_device_mcp import server as srv
from tt_device_mcp.health.evidence import health_event_flushed, read_health_events
from tt_device_mcp.telemetry import LINK_WATCH_SEC, CheckAerWatch


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, msg))


def _watch(read=None, running=True, **deps):
    if read is not None:
        deps["link_signals"] = read
    w = CheckAerWatch(SimpleNamespace(**deps), None, None)
    if running:
        w.thread = SimpleNamespace(is_alive=lambda: True)
    return w


def _report(monkeypatch, w):
    log = _Log()
    monkeypatch.setattr(srv, "logger", log)
    ok = srv._report_dead_chip_watch(w)
    # Rows queued before this one are on disk once it returns True.
    assert health_event_flushed("test_flush", 5.0)
    return ok, log.lines


def _rows(kind):
    return read_health_events(kinds={kind})


def test_an_armed_watch_logs_one_info_line_and_one_row(monkeypatch):
    w = _watch(lambda: {"chips": ["0", "1", "2"], "aer": {}, "paths": {}})
    ok, lines = _report(monkeypatch, w)
    assert ok is True
    assert lines == [
        ("info", f"I96 dead-chip watch ARMED: 3 chips, interval {LINK_WATCH_SEC:g}s (config confirm off, DLLLA off)")
    ]
    rows = _rows("dead_chip_watch_armed")
    assert len(rows) == 1
    row = rows[0]
    assert {"ts", "iso", "kind"} <= set(row)
    assert (row["chips"], row["interval_sec"], row["config_confirm"], row["dllla"]) == (3, LINK_WATCH_SEC, False, False)
    assert _rows("dead_chip_watch_not_armed") == []


def _assert_not_armed(monkeypatch, w, why):
    ok, lines = _report(monkeypatch, w)
    assert ok is False
    assert len(lines) == 1
    level, msg = lines[0]
    assert level == "warning"
    assert msg.startswith("I96 dead-chip watch NOT ARMED: ") and why in msg
    assert _rows("dead_chip_watch_armed") == []
    rows = _rows("dead_chip_watch_not_armed")
    assert len(rows) == 1 and why in rows[0]["reason"]


def test_no_link_signals_read_warns_not_armed(monkeypatch):
    _assert_not_armed(monkeypatch, _watch(), "unsupported")


def test_a_thread_that_is_not_running_warns_not_armed(monkeypatch):
    _assert_not_armed(monkeypatch, _watch(lambda: {"chips": ["0"]}, running=False), "not running")
    dead = _watch(lambda: {"chips": ["0"]})
    dead.thread = SimpleNamespace(is_alive=lambda: False)
    ok, _ = _report(monkeypatch, dead)
    assert ok is False


def test_a_read_listing_no_chip_warns_not_armed(monkeypatch):
    _assert_not_armed(monkeypatch, _watch(lambda: {"chips": []}), "no chip")
    assert srv._report_dead_chip_watch(_watch(lambda: None)) is False


def test_a_failing_read_warns_and_never_raises(monkeypatch):
    def boom():
        raise OSError("sysfs gone")

    _assert_not_armed(monkeypatch, _watch(boom), "sysfs gone")


def test_a_broken_watch_or_logger_never_raises(monkeypatch):
    assert srv._report_dead_chip_watch(SimpleNamespace()) is False

    class _BadLog:
        def __getattr__(self, level):
            raise RuntimeError("log handler broke")

    monkeypatch.setattr(srv, "logger", _BadLog())
    assert srv._report_dead_chip_watch(_watch(lambda: {"chips": ["0"]})) is False
    monkeypatch.setattr(srv, "logger", None)
    assert srv._report_dead_chip_watch(_watch(lambda: {"chips": ["0"]})) is True


def test_the_report_only_reads_and_leaves_the_watch_as_it_was(monkeypatch):
    w = _watch(lambda: {"chips": ["0", "1"]}, running=False)
    _report(monkeypatch, w)
    assert w.thread is None, "the report never starts the watch"
    assert w._nodes is None and not w._reads, "the report's read is not the watch's baseline"
    assert not w._halt.is_set(), "the report never stops the watch"


def test_startup_reports_the_watch_right_after_starting_it():
    src = inspect.getsource(srv.run_transports)
    start = src.index("fsm.aer_watch.start()")
    report = src.index("_report_dead_chip_watch, fsm.aer_watch")
    assert start < report < src.index("serve_unix_socket(")
