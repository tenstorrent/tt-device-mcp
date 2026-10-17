# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A broker that admits jobs with no rung able to end a hold says so (spec 03 I23, 04 rung inventory).

The bound is per hold class: with TT_DEVICE_MCP_FORCE_ESCALATE=0 the idle escalation still ends the
classes it is armed for, and a 77-only fabric hold is ended only by the fabric relift. A hold class
no rung ends must never be shown as recovering or promised an escalation."""

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
    """Every rung that ends a hold on its own clock off: the forced escalation, the idle escalation of
    both classes, and the fabric relift that bounds a 77-only hold."""
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
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
    assert sent[0]["unbounded_classes"] == ["off-bus", "present-mesh", "fabric-77"]
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
    # The call sites' note (spec 03 I55) states the same live bound, never a fixed "~2 min" / "10-min".
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", "1200")
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(srv, "isolated_chips", {7})
    note = srv._hold_escalation_note(off_bus=1)
    assert "~5 min" in note and "each 20 min hold ceiling" in note
    assert "20 min hold ceiling" in srv._hold_escalation_note()
    monkeypatch.setattr(srv, "isolated_chips", set())
    note = srv._hold_escalation_note(1)
    assert "~5 min" not in note and "climbs at the 20 min hold ceiling" in note
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


def _defaults_but_force_off(monkeypatch):
    for name in (
        "TT_DEVICE_MCP_SELFHEAL_RELIFT",
        "TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE",
        "TT_DEVICE_MCP_STUCK_HOLD_RESET",
        "TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE",
        "TT_DEVICE_MCP_FABRIC_RELIFT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")


def _offbus_hold(monkeypatch, off_bus=1):
    srv.fsm.on_fault("off_bus", detail=f"{off_bus} of 32 chips off the bus")
    monkeypatch.setattr(srv, "isolated_chips", {7} if off_bus else set())
    offbus_state = {"isolated_chips": [7] if off_bus else [], "off_bus": off_bus}
    monkeypatch.setattr(srv, "_live_offbus_state", lambda: offbus_state)
    monkeypatch.setattr(srv, "_device_degraded_for_tenant", lambda: "held: off-bus below the floor")
    monkeypatch.setattr(srv, "device_op_active", False)
    monkeypatch.setattr(srv, "device_held_since", "")


def test_the_idle_escalation_alone_bounds_a_hold(monkeypatch, log, sent):
    """Forced escalation off, every other switch at its default: the idle relift still climbs an
    off-bus hold and resets a present-mesh one past the ceiling, so no output may call it unbounded."""
    _defaults_but_force_off(monkeypatch)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", None)
    ceiling = srv._stuck_hold_ceiling_sec()
    srv.log_rung_inventory()
    line = next(m for lvl, m in log.said if lvl == "info" and m.startswith("RUNG INVENTORY:"))
    assert f"hold-bound={ceiling}s" in line and "hold-unbounded" not in line
    assert sent == [] and srv.fsm.recovery_armed is True
    for off_bus in (0, 1):
        text = srv._hold_bound_text(offbus=bool(off_bus))
        assert "manual recovery" not in text and f"{srv._fmt_bound(ceiling)} hold ceiling" in text
        note = srv._hold_escalation_note(off_bus)
        assert "idle escalation climbs the ladder" in note and "only the idle self-heal relift" not in note
    _offbus_hold(monkeypatch)
    payload = srv._health_payload()
    assert payload["hold_recovery"] == "recovering" and payload["hold_bound_sec"] == ceiling


def test_an_off_bus_hold_with_its_idle_escalation_off_is_unbounded(monkeypatch, log, sent):
    """Forced escalation and the self-heal relift off: a present-mesh hold still resets past the
    ceiling, an off-bus one has no rung. The inventory names the unbounded class and pages once."""
    _defaults_but_force_off(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "0")
    ceiling = srv._stuck_hold_ceiling_sec()
    srv.log_rung_inventory()
    line = next(m for lvl, m in log.said if lvl == "info" and m.startswith("RUNG INVENTORY:"))
    assert f"hold-bound={ceiling}s" in line and "hold-unbounded=off-bus" in line
    assert [e["kind"] for e in sent] == ["hold_unbounded"] and sent[0]["unbounded_classes"] == ["off-bus"]
    assert srv._hold_bound_sec(offbus=True) is None and srv._hold_bound_sec(offbus=False) == ceiling
    _offbus_hold(monkeypatch)
    payload = srv._health_payload()
    assert payload["hold_recovery"] == "none" and payload["hold_bound_sec"] is None
    assert "manual recovery" in srv._hold_bound_text(offbus=True)


@pytest.mark.parametrize("relift", [True, False])
def test_a_77_only_hold_is_bounded_only_by_the_fabric_relift(monkeypatch, relift):
    """The forced escalation never fires on a 77-only hold, so its bound is the fabric relift's (I40):
    with TT_DEVICE_MCP_FABRIC_RELIFT=0 /health must not show it recovering on the forced ceiling."""
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE", raising=False)
    if relift:
        monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_RELIFT", raising=False)
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    _offbus_hold(monkeypatch, off_bus=0)
    srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="fabric check exit 77")
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: True)
    monkeypatch.setattr(srv, "_startup_fabric_owed", lambda: False)
    payload = srv._health_payload()
    assert payload["hold_recovery"] == ("recovering" if relift else "none")
    assert payload["hold_bound_sec"] == (600 if relift else None)
