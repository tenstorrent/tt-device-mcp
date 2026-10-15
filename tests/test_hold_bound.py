# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A broker that admits jobs with no rung able to end a hold says so (spec 03 I23, 04 rung inventory).

With TT_DEVICE_MCP_FORCE_ESCALATE=0 nothing ends a hold on a timer, yet the hold messages promised
escalation in ~2 min and a 10-min ceiling, /health showed the hold as recovering, and the startup
inventory never named the missing bound."""

import json
from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import alert
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerFsm

SINCE = datetime(2026, 10, 15, 2, 31, 0)


class _Logger:
    def __init__(self):
        self.said = []

    def info(self, msg):
        self.said.append(("info", msg))

    def warning(self, msg):
        self.said.append(("warning", msg))

    def error(self, msg):
        self.said.append(("error", msg))


@pytest.fixture
def log(monkeypatch):
    lg = _Logger()
    monkeypatch.setattr(srv, "logger", lg)
    return lg


@pytest.fixture
def sent(monkeypatch):
    events = []

    def fake_send(event, log=None):
        events.append(event)
        return object()

    monkeypatch.setattr(alert, "send_alert", fake_send)
    return events


def _escalation_off(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")


def test_inventory_names_no_hold_bound_and_alerts_once(monkeypatch, log, sent):
    _escalation_off(monkeypatch)
    srv.log_rung_inventory()
    line = next(m for lvl, m in log.said if lvl == "info" and m.startswith("RUNG INVENTORY:"))
    assert "hold-bound=NONE" in line
    assert any(lvl == "error" and "hold bound: NONE" in m for lvl, m in log.said)
    assert [e["kind"] for e in sent] == ["hold_unbounded"]
    assert "forced-escalation" in sent[0]["rungs_off"]
    assert srv.fsm.recovery_armed is False and srv.fsm.hold_bound_sec is None


def test_inventory_states_the_armed_bound_and_does_not_alert(monkeypatch, log, sent):
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", "900")
    srv.log_rung_inventory()
    line = next(m for lvl, m in log.said if lvl == "info" and m.startswith("RUNG INVENTORY:"))
    assert "hold-bound=900s" in line
    assert sent == []
    assert srv.fsm.recovery_armed is True and srv.fsm.hold_bound_sec == 900


def test_watchdog_alerts_when_escalation_is_off(monkeypatch, log, sent):
    _escalation_off(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_ALERT_CMD", "/bin/true")
    patch_health_event(monkeypatch, lambda kind, **f: None)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_alert_next_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_alert_gap", 0)
    monkeypatch.setattr(srv, "device_hold_episode_reason", "below-floor off-bus hold")
    monkeypatch.setattr(srv, "device_hold_episode_since", SINCE.isoformat())
    srv.fsm.set_latch("escalated", False)
    srv._check_hold_deadline(now=SINCE + timedelta(seconds=srv._hold_deadline_sec() + 1))
    assert srv._forced_escalation_task is None or srv._forced_escalation_task.done()
    assert [e["kind"] for e in sent] == ["hold_stuck_past_deadline"]
    assert sent[0]["recovery_armed"] is False and sent[0]["hold_bound_sec"] is None
    assert any(lvl == "error" and "no rung will end this hold" in m for lvl, m in log.said)


def test_hold_messages_state_the_real_bound(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", "1200")
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_SEC", "300")
    assert "~5 min" in srv._hold_bound_text(offbus=True) and "20 min ceiling" in srv._hold_bound_text(offbus=True)
    assert "20 min hold ceiling" in srv._hold_bound_text()
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", "90")
    assert "90s hold ceiling" in srv._hold_bound_text()
    _escalation_off(monkeypatch)
    for text in (srv._hold_bound_text(), srv._hold_bound_text(offbus=True)):
        assert "escalates" not in text and "ceiling" not in text
        assert "FORCE_ESCALATE=0" in text and "manual recovery" in text


@pytest.mark.parametrize("off", [False, True])
def test_health_does_not_show_an_unarmed_hold_as_recovering(monkeypatch, off):
    if off:
        _escalation_off(monkeypatch)
    else:
        monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE", raising=False)
    monkeypatch.setattr(srv, "_device_degraded_for_tenant", lambda: "held: off-bus below the floor")
    monkeypatch.setattr(srv, "device_op_active", False)
    monkeypatch.setattr(srv, "device_held_since", "")
    payload = srv._health_payload()
    assert payload["held"] is True
    assert payload["recovery_armed"] is (not off)
    assert payload["hold_recovery"] == ("none" if off else "recovering")
    assert payload["hold_bound_sec"] == (None if off else srv._stuck_hold_ceiling_sec())


def test_fsm_json_records_whether_recovery_is_armed(tmp_path):
    path = tmp_path / "fsm.json"
    f = ServerFsm(path)
    f.set_recovery_armed(False, None)
    assert not path.exists(), "a BOOT record is never written; the first transition carries the fields"
    f.on_fault("off_bus", detail="1 of 32 chips off the bus")
    on_disk = json.loads(path.read_text())
    assert on_disk["recovery_armed"] is False and on_disk["hold_bound_sec"] is None
    f.set_recovery_armed(True, 600)
    on_disk = json.loads(path.read_text())
    assert on_disk["recovery_armed"] is True and on_disk["hold_bound_sec"] == 600
