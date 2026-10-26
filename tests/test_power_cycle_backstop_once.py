# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I37/I65: a class A/C drop's direct power cycle fired from the gate's reset rung is counted
once in the power-cycle backstop. The gate's host-rung step after that rung must not ask the
governor again in the same pass: the cycle it would count is the one already under way, so the
backstop would deny, page and arm a retry over a cycle that is already happening. A real cycle past
the backstop is still denied."""

import json
import time

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tests.test_device_safety import _ledger_dir, _reach_reboot_rung
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.recovery import base as recovery_base


def _arm_all_off_with_two_cycles(monkeypatch, tmp_path):
    """Class C (every chip off the bus), power cycle opted in, the real governor and ledger, and two
    earlier cycles inside the backstop window that each took the host down."""
    _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)
    _ledger_dir(monkeypatch, tmp_path)
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {})  # no chip answers: all 32 off the bus
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_REBOOT", raising=False)
    monkeypatch.delenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, raising=False)
    # Spaced past the I65 cooldown, so only the backstop can refuse.
    monkeypatch.setattr(recovery_base, "POWER_CYCLE_MIN_INTERVAL_SEC", 60)
    rm = srv.recovery_mechanism
    for name in ("read_auto_recovery_ledger", "auto_recovery_allowed", "last_verified_release"):
        monkeypatch.setattr(rm, name, getattr(type(rm), name).__get__(rm))
    monkeypatch.setattr(rm, "host_action_requested", None)
    now = time.time()
    (tmp_path / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in (
                {"action": "power-cycle", "boot_id": "boot-B", "at_epoch": now - 3000},
                {"action": "power-cycle", "boot_id": "boot-C", "at_epoch": now - 2000},
            )
        )
    )
    boot = {"id": "boot-D"}
    monkeypatch.setattr(srv, "_current_boot_id", lambda: boot["id"])
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(now - 1800)))
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_run_pre_power_cycle_hook", lambda log, reason: {"ran": False})
    cycles = []
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: cycles.append(time.time()))
    alerts = []
    monkeypatch.setattr(recovery_base.alert, "send_alert", lambda event, *a, **k: alerts.append(event) or True)
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    return cycles, alerts, events, boot


def _ledger_cycles(tmp_path):
    rows = (tmp_path / recovery_base.AUTO_RECOVERY_LEDGER).read_text().splitlines()
    return [r for r in map(json.loads, filter(str.strip, rows)) if r.get("action") == "power-cycle"]


@pytest.mark.asyncio
async def test_one_gate_pass_on_all_off_counts_its_cycle_once(monkeypatch, tmp_path, clear_job_state):
    """Two cycles already in the window: the pass requests exactly one cycle (the third), journals no
    backstop denial, pages nobody and arms no retry. Before the fix the host-rung step counted the
    cycle just requested as the third, denied, paged and armed the retry."""
    cycles, alerts, events, _ = _arm_all_off_with_two_cycles(monkeypatch, tmp_path)
    rm = srv.recovery_mechanism

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert len(cycles) == 1, "exactly one power cycle requested"
    assert len(_ledger_cycles(tmp_path)) == 3, "the cycle is recorded once"
    assert "power_cycle_backstop" not in events, events
    assert "auto_recovery_denied" not in events, events
    assert not [a for a in alerts if a.get("kind") == "power_cycle_backstop"], alerts
    assert rm.power_cycle_retry is None, "no retry armed over the cycle under way"
    assert rm.host_action_requested is not None and rm.host_action_requested[0] == "power-cycle"
    assert srv.fsm.state is not ServerState.HEALTHY, "the hold stays until the boot's full check passes"


@pytest.mark.asyncio
async def test_a_second_pass_while_the_cycle_is_pending_asks_nothing(monkeypatch, tmp_path, clear_job_state):
    """Another gate pass before the host goes down: no second cycle, no backstop denial, no page."""
    cycles, alerts, events, _ = _arm_all_off_with_two_cycles(monkeypatch, tmp_path)

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert len(cycles) == 1
    assert "power_cycle_backstop" not in events, events
    assert not alerts, alerts
    assert srv.recovery_mechanism.power_cycle_retry is None


@pytest.mark.asyncio
async def test_the_backstop_still_denies_a_real_fourth_cycle(monkeypatch, tmp_path, clear_job_state):
    """The third cycle took the host down and the drop is back on the new boot: a fourth inside the
    window is refused by the backstop, journaled, paged, and retried when the oldest leaves it."""
    cycles, alerts, events, boot = _arm_all_off_with_two_cycles(monkeypatch, tmp_path)
    rm = srv.recovery_mechanism

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert len(cycles) == 1
    # The host came back from that cycle (a new boot, no request pending in the new process).
    boot["id"] = "boot-E"
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(time.time() - 120)))
    monkeypatch.setattr(rm, "host_action_requested", None)
    rm.last_reset_failed = True

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert len(cycles) == 1, "the backstop refuses a fourth cycle in the window"
    assert len(_ledger_cycles(tmp_path)) == 3
    assert "power_cycle_backstop" in events and "auto_recovery_denied" in events, events
    assert [a for a in alerts if a.get("kind") == "power_cycle_backstop"], "the backstop pages"
    assert rm.power_cycle_retry is not None, "asked again when the oldest cycle leaves the window"
