# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The hold reason and /health say where a held device's escalation stands (spec 03 I85).

A below-floor off-bus hold opens with "held for self-heal, not reset", and the hold watchdog then
climbs its rungs underneath it: the 10-min full ladder, then the power cycle and the boot/post-cycle
cooldown it waits on. Without the stage, the next deadline and the power-cycle request, that hold read
the same for its whole life and looked unbounded to anyone watching /health.
"""

import copy
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty
from tests.test_device_safety import _arm_offbus_stuck_hold
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import hold_report
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
        ("device_hold_escalation_stage", ""),
        ("device_hold_escalation_stage_at", ""),
        ("_hold_carry", None),
        ("_hold_carry_loaded", False),
        ("_forced_escalation_task", None),
        ("_hold_bound_tenant_blocker", None),
    ):
        monkeypatch.setattr(srv, name, value)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-a")
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_persist_hold_episode", lambda *a, **k: None)


class _Ledger:
    """The recovery mechanism's durable auto-recovery ledger, and nothing else. Read through
    _hold_escalation_status alone: the rest of /health reads the full mechanism."""

    power_cycle_cooldown_state = None

    def __init__(self, rows=()):
        self.rows = list(rows)

    def read_auto_recovery_ledger(self):
        return list(self.rows)


def _ledger(monkeypatch, rows):
    """Only the ledger read: the next-rung chain reads the full mechanism, so it is left out here."""
    monkeypatch.setattr(srv, "recovery_mechanism", _Ledger(rows))
    monkeypatch.setattr(srv, "_hold_next_rung", lambda now_epoch: None)


@pytest.fixture
def held(monkeypatch, clear_job_state, tmp_path):
    """A below-floor off-bus hold opened at the returned episode start."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _isolate_hold(monkeypatch)
    monkeypatch.setattr(opc, "_pending", {})
    monkeypatch.setattr(srv, "isolated_chips", {"8"})
    fsm_dirty(srv, REASON, why="off_bus")
    since = datetime(2026, 10, 15, 1, 36, 0)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    return since


def test_a_fresh_hold_names_the_watchdogs_own_next_rung(held):
    """The next step and its deadline are the hold watchdog's own next rung, the one /health's
    ``next_rung`` reads: no second clock."""
    now = held + timedelta(seconds=30)
    payload = srv._health_payload(now=now)
    assert payload["held"] is True
    assert payload["held_reason"].startswith(f"device unverified: {REASON} [")
    esc = payload["hold_escalation"]
    assert esc["stage"] == "held, no forced escalation yet" and esc["stage_at"] is None
    rung = srv._hold_next_rung(now.timestamp())
    assert rung is not None
    assert esc["next_step"] == f"{rung.rung} ({rung.source})"
    assert esc["next_deadline"] == srv._hold_report(now)["next_rung_at"]
    assert esc["next_in_sec"] == max(0, int(rung.at - now.timestamp()))
    assert esc["power_cycle_requested"] is False and esc["power_cycle_requested_at"] is None
    assert f"next: {esc['next_step']} at {esc['next_deadline']} (in {esc['next_in_sec']}s)" in payload["held_reason"]
    assert "no power cycle requested" in payload["held_reason"]


def test_the_ten_minute_mark_and_the_cooldown_come_from_the_broker_clocks(held, monkeypatch):
    """The 10-min full ladder, then the power cycle once the boot/post-cycle cooldown ends, as the
    broker's own candidates name them; with no candidate, the bound's own deadline."""
    now = held + timedelta(seconds=30)
    mark = hold_report.RungCandidate(held.timestamp() + 600, "full ladder, then power cycle", "hold_bound")
    monkeypatch.setattr(srv, "_hold_next_rung", lambda now_epoch: mark)
    esc = srv._hold_escalation_status(now)
    assert esc["next_step"] == "full ladder, then power cycle (hold_bound)"
    assert esc["next_deadline"] == (held + timedelta(seconds=600)).isoformat()
    assert esc["next_in_sec"] == 570

    until = held.timestamp() + 900
    cooldown = {"until": until, "last": held.timestamp(), "cause": "boot", "interval_sec": 900, "why": "cooldown"}
    retry = hold_report.cooldown_candidate(cooldown, until)
    monkeypatch.setattr(srv, "_hold_next_rung", lambda now_epoch: retry)
    esc = srv._hold_escalation_status(held + timedelta(seconds=700))
    assert esc["next_step"] == "power cycle (power_cycle_cooldown)"
    assert esc["next_deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")

    monkeypatch.setattr(srv, "_hold_next_rung", lambda now_epoch: None)
    esc = srv._hold_escalation_status(held + timedelta(seconds=700))
    assert esc["next_step"] == "full recovery ladder (10-min hold bound)"
    assert esc["next_deadline"] == srv._hold_bound_deadline_iso(held + timedelta(seconds=700))


def test_each_logged_step_moves_the_stage(held):
    srv._note_escalation_stage("surgical bridge reset did not recover chip 8", now=held + timedelta(seconds=605))
    payload = srv._health_payload(now=held + timedelta(seconds=610))
    esc = payload["hold_escalation"]
    assert esc["stage"] == "surgical bridge reset did not recover chip 8"
    assert esc["stage_at"] == (held + timedelta(seconds=605)).isoformat()
    assert "escalation: surgical bridge reset did not recover chip 8" in payload["held_reason"]


def test_a_power_cycle_request_is_read_from_disk_and_survives_a_restart(held, monkeypatch):
    """The off-bus path's attempt record for this episode, or a power-cycle ledger row since the episode
    began. With no stage logged in this process (the cycle's reboot), the stage says how far it got."""
    at = (held + timedelta(seconds=650)).timestamp()
    opc.record_attempt({}, at, held.isoformat(), "1/32 chip(s) off the bus")
    payload = srv._health_payload(now=held + timedelta(seconds=700))
    esc = payload["hold_escalation"]
    assert esc["power_cycle_requested"] is True
    assert esc["power_cycle_requested_at"] == (held + timedelta(seconds=650)).isoformat()
    assert esc["stage"] == "power cycle requested" and esc["stage_at"] == esc["power_cycle_requested_at"]
    assert "power cycle requested at 2026-10-15T01:46:50" in payload["held_reason"]
    assert payload["held"] is True, "a requested power cycle releases nothing: the hold stands until a gate passes"

    later = (held + timedelta(seconds=660)).timestamp()
    _ledger(monkeypatch, [{"action": "power-cycle", "at_epoch": later}])
    assert (
        srv._hold_escalation_status(held + timedelta(seconds=700))["power_cycle_requested_at"]
        == (held + timedelta(seconds=660)).isoformat()
    )


def test_another_episodes_power_cycle_is_not_this_ones(held, monkeypatch):
    opc.record_attempt({}, held.timestamp() - 3600, "2026-10-14T20:00:00", "an older hold")
    _ledger(
        monkeypatch,
        [
            {"action": "power-cycle", "at_epoch": held.timestamp() - 60},
            {"action": "reboot", "at_epoch": held.timestamp() + 60},
            {"action": "power-cycle", "at_epoch": "not a time"},
        ],
    )
    assert srv._hold_escalation_status(held + timedelta(seconds=120))["power_cycle_requested"] is False


def test_the_bound_ladder_ran_before_a_restart(held, monkeypatch):
    monkeypatch.setattr(srv, "_hold_bound_episode", held.isoformat())
    monkeypatch.setattr(srv, "_hold_bound_ladder_age", 612.0)
    assert srv._hold_escalation_status(held + timedelta(seconds=700))["stage"] == (
        "full recovery ladder ran at hold age 612s"
    )


def test_a_running_ladder_and_a_tenant_say_so(held, monkeypatch):
    now = held + timedelta(seconds=630)

    class Running:
        def done(self):
            return False

    monkeypatch.setattr(srv, "_forced_escalation_task", Running())
    esc = srv._hold_escalation_status(now)
    assert esc["next_step"] == "the forced recovery ladder is running" and esc["next_deadline"] is None
    assert "next: the forced recovery ladder is running;" in srv._hold_escalation_suffix(esc)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "_hold_bound_tenant_blocker", {"pid": 4242})
    monkeypatch.setattr(srv, "_describe_bound_blocker", lambda b: "pid 4242")
    esc = srv._hold_escalation_status(now)
    assert esc["next_step"] == "waits for the tenant to leave (pid 4242)" and esc["next_deadline"] is None


def test_reading_it_moves_no_clock(held, monkeypatch):
    """Reporting only: /health claims, moves or expires no escalation clock and lifts nothing."""
    names = (
        "device_hold_episode_since",
        "device_hold_escalate_bucket",
        "device_hold_offbus_escalated",
        "_hold_bound_ladder_age",
        "_hold_bound_cycle_age",
        "_hold_bound_rung_pending",
        "_hold_bound_check_failed_age",
        "device_hold_escalation_stage",
    )
    before = {n: getattr(srv, n) for n in names}
    pending = copy.deepcopy(opc._pending)
    state = srv.fsm.state
    for s in (30, 610, 1300):
        srv._health_payload(now=held + timedelta(seconds=s))
    assert {n: getattr(srv, n) for n in names} == before
    assert opc._pending == pending and srv.fsm.state is state


def test_the_hold_row_keeps_its_name(held):
    """The jobs-list hold row keeps the name it opened with; the escalation is reported in /health."""
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    srv._note_escalation_stage("per-tray UBB re-power of tray 1 failed")
    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"]
    assert rows and rows[0]["command"] == f"device HELD, refused to tenants: device unverified: {REASON}"
    assert "escalation: per-tray UBB re-power of tray 1 failed" in srv._health_payload()["held_reason"]


def test_a_new_episode_and_the_release_clear_the_stage(held):
    srv._note_escalation_stage("a stage left from somewhere")
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    assert srv.device_hold_escalation_stage == ""
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
async def test_a_forced_pass_updates_the_stage_and_keeps_the_hold(monkeypatch, clear_job_state, tmp_path):
    """The forced escalation writes each rung and its end into the stage /health shows. A ladder whose
    reset did not recover releases nothing: the device stays held until the full check, fabric
    included, passes."""
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
    assert payload["hold_escalation"]["stage"] != "held, no forced escalation yet"
    assert payload["held_reason"] != before
