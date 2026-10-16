# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""No device reset from the hold watchdog once a host reboot or power cycle is requested.

A power cycle handed to an external cycler returns before the host goes down. A forced escalation
the deadline watchdog started in that gap ran a galaxy reset under the cycle. Once a host action is
requested, the watchdog's forced ladder starts no reset: the device stays held and the full health
check stays owed. The suppression is bounded: a request that was refused, failed to launch or was
never acted on does not keep the ladder away for good.
"""

import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy

REASON = "tray-down-no-window: every reset type ran back-to-back and the mesh did not verify"
SINCE = datetime(2026, 10, 15, 11, 50, 0)


@pytest.fixture
def held(monkeypatch, tmp_path):
    """An off-bus hold episode with a stub power-cycle fire, a tmp ledger and captured events. The
    spawned escalation body is a recorder, so no test reaches a real rung."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(recovery_base, "health_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.delenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE", raising=False)
    fired = []
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: fired.append("power-cycle"))
    monkeypatch.setattr(galaxy, "_fire_host_reboot", lambda: fired.append("reboot"))
    spawned = []

    async def _escalation():
        spawned.append(True)

    monkeypatch.setattr(srv, "_force_escalate_stuck_hold", _escalation)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "isolated_chips", {"6", "7"})
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chip(s) 6,7 fell off the PCIe bus")
    monkeypatch.setattr(srv, "device_hold_episode_since", SINCE.isoformat())
    fsm_dirty(srv, "chip(s) 6,7 fell off the PCIe bus", why="heartbeat")
    # A fabric pass that passed before the cycle: the request must void it, so the check stays owed.
    monkeypatch.setattr(srv, "last_fabric_ok_monotonic", time.monotonic())
    return {"events": events, "fired": fired, "spawned": spawned}


def _kinds(events):
    return [k for k, _ in events]


def _offbus_tick():
    srv._check_hold_deadline(now=SINCE + timedelta(seconds=srv._offbus_hold_ceiling_sec() + 1))


def _ceiling_tick():
    srv._check_hold_deadline(now=SINCE + timedelta(seconds=srv._stuck_hold_ceiling_sec() + 1))


def _assert_held_with_check_owed():
    assert srv._recovery_degraded(), "the device must stay held"
    assert srv._device_unavailable_for_tenant(), "a held device stays refused to tenants"
    assert srv._fabric_ok_age() is None, "the full health check (fabric included) is still owed"


@pytest.mark.asyncio
async def test_power_cycle_request_suppresses_every_deadline_tick(held):
    """The 11:54 replay: the tray-down sweep asked for a power cycle, then the off-bus one-shot and
    the ceiling window fired. Neither may start a reset, and neither spends its window latch."""
    await srv._auto_power_cycle_host(lambda m: None, REASON)
    assert held["fired"] == ["power-cycle"]
    assert srv.recovery_mechanism.host_action_requested[0] == "power-cycle"

    _offbus_tick()
    _ceiling_tick()

    assert srv._forced_escalation_task is None, "no forced escalation may start once a power cycle is requested"
    assert held["spawned"] == []
    assert srv.device_hold_offbus_escalated is False, "a suppressed one-shot is not spent"
    assert srv.device_hold_escalate_bucket == 0, "a suppressed ceiling window is not spent"
    _assert_held_with_check_owed()
    suppressed = [f for k, f in held["events"] if k == "hold_escalation_suppressed_host_action"]
    assert len(suppressed) == 1, "one line per request, not one per tick"
    assert suppressed[0]["action"] == "power-cycle"
    assert suppressed[0]["trigger"] == "offbus_one_shot"


@pytest.mark.asyncio
async def test_reboot_request_suppresses_the_deadline_tick(held):
    await srv.galaxy_recovery._auto_reboot_host(lambda m: None, "reset could not recover the device")
    assert held["fired"] == ["reboot"]
    _offbus_tick()
    assert srv._forced_escalation_task is None
    assert "hold_escalation_suppressed_host_action" in _kinds(held["events"])
    assert srv.recovery_mechanism.host_action_pending() is not None


@pytest.mark.asyncio
async def test_escalation_waiting_on_the_lock_runs_no_rung(monkeypatch):
    """A forced escalation spawned before the request, still waiting on the device-op lock, must not
    run the ladder once it gets the lock."""
    climbed = []

    async def _climb(*a, **k):
        climbed.append(a)

    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", _climb)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)
    monkeypatch.setattr(srv, "device_op_lock", None)  # bind a fresh lock to this test's loop
    monkeypatch.setattr(srv, "isolated_chips", {"6", "7"})
    monkeypatch.setattr(srv, "device_hold_episode_since", SINCE.isoformat())
    fsm_dirty(srv, "chip(s) 6,7 fell off the PCIe bus", why="heartbeat")
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", ("power-cycle", time.monotonic()))

    await srv._force_escalate_stuck_hold()

    assert climbed == [], "no rung may run while a requested power cycle is pending"
    assert "hold_deadline_forced_escalation" not in events
    assert "hold_escalation_suppressed_host_action" in events
    assert srv._recovery_degraded()


@pytest.mark.asyncio
async def test_power_cycle_that_fails_to_launch_does_not_suppress(held, monkeypatch):
    """A fire that did not launch took nothing down: the ladder must keep its teeth."""

    def _boom():
        raise RuntimeError("power cycle command exited 1")

    monkeypatch.setattr(srv, "_fire_power_cycle", _boom)
    await srv._auto_power_cycle_host(lambda m: None, REASON)
    assert "auto_power_cycle_failed" in _kinds(held["events"])
    assert srv.recovery_mechanism.host_action_requested is None

    _offbus_tick()
    await srv._forced_escalation_task
    assert held["spawned"] == [True], "a failed power cycle must leave the forced ladder running"
    assert srv.device_hold_offbus_escalated is True


@pytest.mark.asyncio
async def test_power_cycle_refused_by_the_ledger_does_not_suppress(held, monkeypatch):
    """A request the rate-limit ledger could not record is aborted before it fires: nothing is
    pending and the ladder runs."""
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: False)
    await srv._auto_power_cycle_host(lambda m: None, REASON)
    assert held["fired"] == []
    assert "auto_power_cycle_aborted" in _kinds(held["events"])

    _offbus_tick()
    await srv._forced_escalation_task
    assert held["spawned"] == [True]


@pytest.mark.asyncio
async def test_a_request_the_host_never_acted_on_expires_and_the_ladder_resumes(held, monkeypatch):
    """Bounded (goal: no open-ended hold): past the pending window the request reads as failed,
    is logged once, and the next tick runs the ladder."""
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", "60")
    await srv._auto_power_cycle_host(lambda m: None, REASON)
    _offbus_tick()
    assert srv._forced_escalation_task is None

    srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic() - 61)
    _offbus_tick()
    await srv._forced_escalation_task

    assert held["spawned"] == [True], "an expired request must hand the hold back to the ladder"
    assert srv.recovery_mechanism.host_action_requested is None
    expired = [f for k, f in held["events"] if k == "host_action_request_expired"]
    assert len(expired) == 1 and expired[0]["action"] == "power-cycle"


@pytest.mark.parametrize(
    "raw,want",
    [("", 900.0), ("garbage", 900.0), ("0", 900.0), ("-5", 900.0), ("inf", 900.0), ("120", 120.0), ("99999", 3600.0)],
)
def test_pending_window_is_always_finite(monkeypatch, raw, want):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", raw)
    assert recovery_base.host_action_pending_sec() == want
