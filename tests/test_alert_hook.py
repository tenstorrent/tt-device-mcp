# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The opt-in alert hook for stuck holds (spec 03 I30)."""

import json
import os
import signal
import time
import tracemalloc
from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import alert
from tt_device_mcp import server as srv

SINCE = datetime(2026, 10, 10, 3, 0, 0)


@pytest.fixture
def held(monkeypatch):
    """A fresh stuck-hold episode with the timeline silenced; returns a window-crossing driver."""
    patch_health_event(monkeypatch, lambda kind, **f: None)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_alert_next_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_alert_gap", 0)
    monkeypatch.setattr(srv, "device_hold_episode_reason", "eth/fabric fault on a present mesh")
    monkeypatch.setattr(srv, "device_hold_episode_since", SINCE.isoformat())
    srv.fsm.set_latch("escalated", False)

    def cross(window: int) -> None:
        srv._check_hold_deadline(now=SINCE + timedelta(seconds=window * srv._hold_deadline_sec() + 1))

    return cross


@pytest.fixture
def sent(monkeypatch):
    """Record what the hook would be handed, without running anything."""
    events = []

    def fake_send(event, log=None):
        events.append(event)
        return object()

    monkeypatch.setattr(alert, "send_alert", fake_send)
    return events


def test_unset_hook_runs_nothing(monkeypatch, held):
    monkeypatch.delenv("TT_DEVICE_MCP_ALERT_CMD", raising=False)
    spawned = []
    monkeypatch.setattr(alert.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    for w in range(1, 10):
        held(w)
    assert spawned == []
    assert srv.device_hold_alert_next_bucket == 0


def test_alert_fires_once_on_the_first_stuck_window_then_backs_off(monkeypatch, held, sent):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    d = srv._hold_deadline_sec()
    fired_at = []
    for w in range(1, 32):
        before = len(sent)
        held(w)
        held(w)  # a second sample in the same window never re-fires
        if len(sent) > before:
            fired_at.append(w)
    # first stuck window, then gaps of 2, 4, 8, 16 windows
    assert fired_at == [1, 3, 7, 15, 31]
    first = sent[0]
    assert first["kind"] == "hold_stuck_past_deadline"
    assert first["held_since"] == SINCE.isoformat()
    assert "eth/fabric" in first["reason"]
    assert first["held_age_sec"] >= d
    assert first["next_alert_after_sec"] == 2 * d


def test_alert_backoff_is_capped_at_a_day(monkeypatch, held, sent):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    monkeypatch.setenv("TT_DEVICE_MCP_HOLD_DEADLINE_SEC", "3600")
    for w in range(1, 120):
        held(w)
    gaps = [e["next_alert_after_sec"] for e in sent]
    assert gaps[:4] == [7200, 14400, 28800, 57600]
    assert max(gaps) == 86400
    assert gaps[-1] == 86400


def test_alert_re_arms_when_the_episode_closes(monkeypatch, held, sent):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    held(1)
    held(2)
    assert len(sent) == 1
    monkeypatch.setattr(srv, "device_hold_logged", True)
    srv._note_tenant_gate_verdict("")  # the device comes back fit -> episode closes
    assert srv.device_hold_alert_next_bucket == 0 and srv.device_hold_alert_gap == 0
    monkeypatch.setattr(srv, "device_hold_episode_since", SINCE.isoformat())  # a new stuck episode
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chips off the bus")
    held(1)
    assert len(sent) == 2
    assert sent[1]["reason"] == "chips off the bus"


def _restart_broker(monkeypatch):
    """What a new broker process holds for the same still-held device: nothing in memory, the
    episode start on disk, and the gate's first verdict that it is still degraded."""
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_alert_next_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_alert_gap", 0)
    srv._note_tenant_gate_verdict("eth/fabric fault on a present mesh")
    assert srv.device_hold_episode_since == SINCE.isoformat()


def test_a_restart_mid_episode_keeps_the_backoff(monkeypatch, held, sent):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    srv._persist_hold_episode(SINCE.isoformat())
    held(1)
    assert len(sent) == 1
    _restart_broker(monkeypatch)
    held(2)
    assert len(sent) == 1  # the restart did not page again
    held(3)
    assert len(sent) == 2  # the backoff carried on where it was
    assert sent[1]["next_alert_after_sec"] == 4 * srv._hold_deadline_sec()


def test_a_backoff_from_another_episode_is_ignored(monkeypatch, held, sent):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    srv._persist_hold_alert_backoff((SINCE - timedelta(days=1)).isoformat(), 31, 16)
    srv._persist_hold_episode(SINCE.isoformat())
    _restart_broker(monkeypatch)
    held(1)
    assert len(sent) == 1


def test_the_episode_closing_clears_the_stored_backoff(monkeypatch, held, sent):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    held(1)
    assert (srv.health_dir() / srv.HOLD_ALERT_BACKOFF_FILE).exists()
    monkeypatch.setattr(srv, "device_hold_logged", True)
    srv._note_tenant_gate_verdict("")
    assert not (srv.health_dir() / srv.HOLD_ALERT_BACKOFF_FILE).exists()


def test_a_hook_still_running_does_not_spend_the_backoff(monkeypatch, held):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: None)
    held(1)
    assert srv.device_hold_alert_next_bucket == 0  # next window tries again


def _wait(t):
    t.join(timeout=10)
    assert not t.is_alive()


def test_hook_gets_the_event_as_json_on_stdin(monkeypatch, tmp_path):
    out = tmp_path / "event.json"
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", f"/bin/sh -c 'cat > {out}'")
    t = alert.send_alert({"kind": "hold_stuck_past_deadline", "held_age_sec": 2401})
    _wait(t)
    assert json.loads(out.read_text()) == {"kind": "hold_stuck_past_deadline", "held_age_sec": 2401}


def test_a_hung_hook_never_blocks_and_is_killed_on_timeout(monkeypatch, tmp_path, caplog):
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", f"/bin/sh -c 'echo $$ > {pidfile}; sleep 60'")
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_TIMEOUT_SEC", "0.5")
    t0 = time.monotonic()
    t = alert.send_alert({"kind": "x"})
    assert time.monotonic() - t0 < 0.5  # returns at once; the run is on its own thread
    # one at a time: a second event while the first runs is dropped, not queued
    assert alert.send_alert({"kind": "y"}) is None
    _wait(t)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert "killed after" in caplog.text
    _wait(alert.send_alert({"kind": "z"}))  # the slot is free again


def test_a_failing_or_missing_hook_only_logs(monkeypatch, caplog):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/sh -c 'echo boom; exit 3'")
    _wait(alert.send_alert({"kind": "x"}))
    assert "exited 3: boom" in caplog.text
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/nonexistent/alert-hook")
    _wait(alert.send_alert({"kind": "x"}))
    assert "could not start" in caplog.text


def test_a_chatty_hook_cannot_grow_the_broker_memory(monkeypatch, caplog):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "yes")
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_TIMEOUT_SEC", "1")
    tracemalloc.start()
    try:
        _wait(alert.send_alert({"kind": "x"}))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024  # buffering everything ran to GBs within the 1 s
    assert "killed after 1s" in caplog.text


def test_a_hook_that_exits_frees_the_slot_even_if_its_child_holds_the_pipe(monkeypatch, tmp_path, caplog):
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", f"/bin/sh -c 'setsid sleep 30 & echo $! > {pidfile}; echo ok'")
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_TIMEOUT_SEC", "10")
    try:
        t0 = time.monotonic()
        _wait(alert.send_alert({"kind": "x"}))
        assert time.monotonic() - t0 < 5  # its own exit ended the run, not the 10 s timeout
        assert "killed" not in caplog.text
        assert "exited 0 but a process it left behind still holds its output" in caplog.text
        _wait(alert.send_alert({"kind": "y"}))  # the slot is free again
    finally:
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_a_hook_killed_on_timeout_is_reaped_even_if_an_escaped_child_holds_the_pipe(monkeypatch, tmp_path):
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", f"/bin/sh -c 'setsid sleep 30 & echo $! > {pidfile}; sleep 30'")
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_TIMEOUT_SEC", "0.3")
    procs = []
    real_popen = alert.subprocess.Popen

    def popen(*a, **k):
        procs.append(real_popen(*a, **k))
        return procs[-1]

    monkeypatch.setattr(alert.subprocess, "Popen", popen)
    try:
        _wait(alert.send_alert({"kind": "x"}))
        assert procs[0].returncode == -signal.SIGKILL  # waited for, so no zombie is left
        assert procs[0].stdout.closed and procs[0].stdin.closed
    finally:
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_an_unparseable_hook_is_logged_once(monkeypatch, caplog):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "'still unbalanced")
    assert alert.alert_argv() is None
    assert alert.alert_argv() is None
    assert caplog.text.count("does not parse") == 1


@pytest.mark.parametrize("raw", ["", "   ", "'unbalanced"])
def test_blank_or_unparseable_hook_is_off(monkeypatch, raw):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", raw)
    assert alert.alert_argv() is None
    assert alert.send_alert({"kind": "x"}) is None


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "x"])
def test_bad_timeout_reads_as_default(monkeypatch, raw):
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_TIMEOUT_SEC", raw)
    assert alert.alert_timeout_sec() == alert.ALERT_TIMEOUT_DEFAULT_SEC
