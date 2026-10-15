"""The hold reason and /health say where a held device's escalation stands (spec 03 I55).

A below-floor off-bus hold opens with "held for self-heal, not reset", and the hold watchdog then
climbs the off-bus one-shot, the power-cycle grace and the ceiling windows underneath it. Without
the stage, the next deadline and the power-cycle request, that hold read the same for its whole
life and looked unbounded to anyone watching /health.
"""

from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty
from tests.test_device_safety import _arm_offbus_stuck_hold
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.recovery import offbus_power_cycle as opc

REASON = "gate/startup: 1/32 chip(s) off the bus, below the reset floor — held for self-heal, not reset"


def _isolate_hold(monkeypatch):
    """Every hold global these tests touch is restored afterwards, so no hold leaks into later tests."""
    for name, value in (
        ("device_hold_logged", False),
        ("device_hold_episode_since", ""),
        ("device_hold_episode_reason", ""),
        ("device_held_since", ""),
        ("_hold_row", None),
        ("device_hold_escalation_trigger", ""),
    ):
        monkeypatch.setattr(srv, name, value)
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_persist_hold_episode", lambda *a, **k: None)


@pytest.fixture
def held(monkeypatch, clear_job_state, tmp_path):
    """A below-floor off-bus hold opened at the returned episode start."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _isolate_hold(monkeypatch)
    monkeypatch.setattr(opc, "_pending", {})
    monkeypatch.setattr(srv, "isolated_chips", {"8"})
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_offbus_hold_ceiling_sec", lambda: 120)
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 600)
    for name in (
        "TT_DEVICE_MCP_POWER_CYCLE_CMD",
        "TT_DEVICE_MCP_FORCE_ESCALATE",
        "TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC",
    ):
        monkeypatch.delenv(name, raising=False)
    fsm_dirty(srv, REASON, why="off_bus")
    since = datetime(2026, 10, 15, 1, 36, 0)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    return since


def test_a_fresh_offbus_hold_names_the_one_shot_deadline(held):
    payload = srv._health_payload(now=held + timedelta(seconds=30))
    assert payload["held"] is True
    assert payload["held_reason"].startswith(f"device unverified: {REASON} [")
    esc = payload["hold_escalation"]
    assert esc["stage"] == "held, no forced escalation yet"
    assert esc["next_step"] == "off-bus forced escalation"
    assert esc["next_deadline"] == (held + timedelta(seconds=120)).isoformat()
    assert esc["next_in_sec"] == 90
    assert esc["power_cycle_requested"] is False
    assert "next: off-bus forced escalation at 2026-10-15T01:38:00 (in 90s)" in payload["held_reason"]
    assert "no power cycle requested" in payload["held_reason"]


def test_each_step_moves_the_stage_and_the_deadline(held, monkeypatch):
    """One-shot spent -> the power-cycle grace is next; cycle requested -> the ceiling window is next,
    and the reason says the cycle was requested and when."""
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/usr/local/sbin/host-power-cycle --now")
    episode = held.isoformat()
    srv._note_escalation_stage("surgical bridge reset did not recover chip 8", now=held + timedelta(seconds=125))
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    opc.arm(episode)
    opc.note_ladder_ran(episode)
    payload = srv._health_payload(now=held + timedelta(seconds=130))
    esc = payload["hold_escalation"]
    assert esc["stage"] == "surgical bridge reset did not recover chip 8"
    assert esc["stage_at"] == (held + timedelta(seconds=125)).isoformat()
    assert esc["next_step"] == "power cycle (off-bus grace)"
    assert esc["next_deadline"] == (held + timedelta(seconds=300)).isoformat()
    assert "escalation: surgical bridge reset did not recover chip 8" in payload["held_reason"]

    opc.mark_spawned(episode)
    at = (held + timedelta(seconds=301)).timestamp()
    opc.record_attempt({}, at, episode, "1/32 chip(s) off the bus")
    srv._note_escalation_stage("forced recovery ladder (the 300s gated off-bus grace) ended: power_cycle")
    payload = srv._health_payload(now=held + timedelta(seconds=310))
    esc = payload["hold_escalation"]
    assert esc["power_cycle_requested"] is True
    assert esc["power_cycle_requested_at"] == (held + timedelta(seconds=301)).isoformat()
    assert esc["next_step"] == "forced recovery ladder (ceiling window 1)"
    assert esc["next_deadline"] == (held + timedelta(seconds=600)).isoformat()
    assert "power cycle requested at 2026-10-15T01:41:01" in payload["held_reason"]
    assert payload["held"] is True, "a requested power cycle releases nothing: the hold stands until a gate passes"


def test_another_episodes_power_cycle_is_not_this_ones(held):
    opc.record_attempt({}, held.timestamp() - 3600, "2026-10-14T20:00:00", "an older hold")
    assert srv._hold_escalation_status(held + timedelta(seconds=10))["power_cycle_requested"] is False


def test_a_running_ladder_and_the_kill_switch_and_a_77_hold_say_so(held, monkeypatch):
    now = held + timedelta(seconds=30)

    class Running:
        def done(self):
            return False

    monkeypatch.setattr(srv, "_forced_escalation_task", Running())
    assert srv._hold_escalation_status(now)["next_step"] == "the forced recovery ladder is running"
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    esc = srv._hold_escalation_status(now)
    assert esc["next_deadline"] is None and "TT_DEVICE_MCP_FORCE_ESCALATE=0" in esc["next_step"]
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE")
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: True)
    esc = srv._hold_escalation_status(now)
    assert esc["next_deadline"] is None and "(77)" in esc["next_step"]
    assert "next: none forced" in srv._hold_escalation_suffix(esc)


def test_the_hold_row_keeps_its_name(held, monkeypatch, tmp_path):
    """The jobs-list hold row keeps the name it opened with (its ledger row lands with that name on
    release); the escalation is reported in /health, not by renaming the row."""
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    srv._note_escalation_stage("per-tray UBB re-power of tray 1 failed")
    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"]
    assert rows and rows[0]["command"] == f"device HELD, refused to tenants: device unverified: {REASON}"
    assert "escalation: per-tray UBB re-power of tray 1 failed" in srv._health_payload()["held_reason"]


def test_release_clears_the_stage(held):
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    srv._note_escalation_stage("forced recovery ladder ended: waiting")
    srv._note_tenant_gate_verdict("")
    assert srv.device_hold_escalation_stage == "" and srv.device_hold_escalation_stage_at == ""
    assert srv._hold_escalation_status() is None


def test_not_held_reports_no_escalation(monkeypatch, clear_job_state):
    _isolate_hold(monkeypatch)
    assert srv._hold_escalation_status() is None
    assert srv._hold_escalation_suffix(None) == ""
    assert "hold_escalation" not in srv._health_payload()


@pytest.mark.asyncio
async def test_a_forced_pass_updates_the_reason_and_keeps_the_hold(monkeypatch, clear_job_state, tmp_path):
    """The forced escalation writes each rung and its end into the stage /health shows. A ladder whose
    reset did not recover releases nothing: the device stays held (goal: no reset path releases the
    device before the full health check, fabric included, passes)."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path / "jobs")
    _isolate_hold(monkeypatch)
    _arm_offbus_stuck_hold(monkeypatch, off_bus=0, isolated=set(), reset_result=False, sbr_verify_healthy=False)
    dev = tmp_path / "tenstorrent"
    dev.mkdir()
    for i in range(32):
        (dev / str(i)).mkdir()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: present)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_REBOOT", raising=False)
    fsm_dirty(srv, "gate/post-job: eth/fabric fault on a present mesh — held", why="eth_frozen")
    srv._note_tenant_gate_verdict("device unverified: eth/fabric fault on a present mesh — held")
    before = srv._health_payload()["held_reason"]
    assert "escalation: held, no forced escalation yet" in before
    monkeypatch.setattr(srv, "device_hold_escalation_trigger", "the 600s ceiling (window 1)")
    await srv._force_escalate_stuck_hold()
    payload = srv._health_payload()
    assert payload["held"] is True and srv.fsm.state is not ServerState.HEALTHY
    stage = payload["hold_escalation"]["stage"]
    assert stage.startswith("forced recovery ladder (the 600s ceiling (window 1)) ended:"), stage
    assert payload["held_reason"] != before
