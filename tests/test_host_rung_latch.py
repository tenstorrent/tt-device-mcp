# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""No reset once a host reboot or power cycle has been requested (spec 04 I34).

``systemctl reboot`` returns as soon as PID 1 has queued it, so the host rung's fire returns normally
while the box shuts down. Without a latch the hold watchdog's next tick forced the ladder again in the
same second and ran a parent-bridge reset plus a PCI rescan during the shutdown.
"""

import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.recovery import galaxy, host_rung_latch, pcie_guard
from tt_device_mcp.health.recovery.stages import bridge_reset, ubb_tray


@pytest.fixture(autouse=True)
def _no_latch():
    host_rung_latch.clear()
    yield
    host_rung_latch.clear()


def _fire_ok(monkeypatch, action="reboot"):
    """The real _fire_recovery_escalation with a fire that returns normally, as `systemctl reboot` does."""
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: True)
    monkeypatch.setattr(srv.recovery_mechanism, "_write_action_log", lambda *a, **k: None)
    fired = []

    async def go():
        await srv.recovery_mechanism._fire_recovery_escalation(
            action,
            row_owner="[broker]test",
            label=f"auto {action}",
            detail="test",
            fire=lambda: fired.append(action),
            log=lambda m: None,
            reason="mass drop",
        )

    return go, fired


def _held_past_ceiling(monkeypatch, tmp_path):
    """A present-mesh hold past the escalation ceiling, its ladder rungs replaced by counters."""
    fsm_dirty(srv, "chips stopped heartbeating", why="heartbeat")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "1")
    dev = tmp_path / "tenstorrent"
    dev.mkdir()
    for i in range(32):
        (dev / str(i)).mkdir()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: present)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32)})
    monkeypatch.setattr(srv, "dead_chips", lambda beats: [])
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "device_op_active", False)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_fabric_no_reset_hold", lambda: "")
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    since = datetime.now() - timedelta(seconds=srv._stuck_hold_ceiling_sec() + 5)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())

    issued = []

    async def rung(indices, expected, log, *, force=False, measured_fault=None):
        issued.append("ladder")  # the bridge reset / SBR / tray re-power / mesh reset it would run
        return True

    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_stuck_hold", rung)
    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_offbus_stuck_hold", rung)

    async def mesh_reset(*a, **k):
        issued.append("mesh-reset")
        return 0, ""

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", mesh_reset)
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda *a: issued.append("bridge") or True)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: issued.append("tray"))
    return issued


@pytest.mark.asyncio
async def test_no_reset_on_the_hold_tick_after_a_reboot_was_requested(monkeypatch, tmp_path, clear_job_state):
    """The reported sequence: the reboot fire returns normally, the next hold-deadline tick must not
    force the ladder (no bridge reset, SBR, tray re-power or mesh reset)."""
    issued = _held_past_ceiling(monkeypatch, tmp_path)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    go, fired = _fire_ok(monkeypatch)
    await go()
    assert fired == ["reboot"]

    srv._check_hold_deadline()
    if srv._forced_escalation_task is not None:
        await srv._forced_escalation_task

    assert issued == [], f"reset the device while the box was going down: {issued}"
    assert srv._forced_escalation_task is None
    # The watchdog's own pending-request guard answers first; the latch backs it on every other path.
    assert {"hold_escalation_suppressed_host_action", "reset_refused_host_rung_in_flight"} & set(events)
    assert srv.device_hold_escalate_bucket == 0, "a refused window is not spent"


@pytest.mark.asyncio
async def test_the_latch_alone_keeps_the_hold_tick_from_resetting(monkeypatch, tmp_path, clear_job_state):
    """The latch backs the watchdog's pending-request guard: with that request gone, the tick still
    forces nothing and journals the latch refusal."""
    issued = _held_past_ceiling(monkeypatch, tmp_path)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    go, fired = _fire_ok(monkeypatch)
    await go()
    srv.recovery_mechanism.host_action_requested = None

    srv._check_hold_deadline()
    if srv._forced_escalation_task is not None:
        await srv._forced_escalation_task

    assert issued == [] and srv._forced_escalation_task is None
    assert "reset_refused_host_rung_in_flight" in events
    assert srv.device_hold_escalate_bucket == 0, "a refused window is not spent"


@pytest.mark.asyncio
async def test_without_a_host_rung_the_same_tick_forces_the_ladder(monkeypatch, tmp_path, clear_job_state):
    """Control: the setup above does reach the ladder when no host rung is in flight."""
    issued = _held_past_ceiling(monkeypatch, tmp_path)
    srv._check_hold_deadline()
    assert srv._forced_escalation_task is not None
    await srv._forced_escalation_task
    assert issued == ["ladder"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reboot", "power-cycle"])
async def test_every_reset_entry_point_refuses_while_a_host_rung_is_in_flight(monkeypatch, tmp_path, action):
    go, _ = _fire_ok(monkeypatch, action)
    await go()
    assert host_rung_latch.in_flight()["action"] == action

    # Mesh reset (operator reset, gate, ladder): refused before any quiesce or scope.
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", lambda *a, **k: pytest.fail("mesh reset ran"))
    monkeypatch.setattr(srv.recovery_mechanism, "_set_device_pollers", lambda *a, **k: pytest.fail("quiesced"))
    rc, text = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)
    assert rc is None and "no reset while the box goes down" in text

    # Bridge reset / SBR + rescan: not issued at all.
    monkeypatch.setattr(bridge_reset.subprocess, "run", lambda *a, **k: pytest.fail("setpci ran"))
    assert bridge_reset.reset_chip_via_bridge("27", "0000:c1:00.0") is None

    # Tray re-power: refused before the ioctl handshake or ipmitool.
    with pytest.raises(pcie_guard.TrayRepowerRefused):
        ubb_tray._fire_ubb_reset(0x02, [8, 9])

    # The ladder itself: nothing runs.
    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_stuck_hold", lambda *a, **k: pytest.fail("ladder ran"))
    out = await srv.galaxy_recovery.escalate("present-forced", ["0"], 32, lambda m: None)
    assert out == galaxy.OUTCOME_WAITING

    # A second host rung: no record, no fire.
    recorded = []
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: recorded.append(a) or True)
    again = []
    await srv.recovery_mechanism._fire_recovery_escalation(
        "power-cycle",
        row_owner="[broker]test",
        label="auto power-cycle",
        detail="test",
        fire=lambda: again.append(True),
        log=lambda m: None,
        reason="again",
    )
    assert recorded == [] and again == []


@pytest.mark.asyncio
async def test_a_fire_that_did_not_launch_latches_nothing(monkeypatch):
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: True)
    monkeypatch.setattr(srv.recovery_mechanism, "retract_last_auto_recovery", lambda *a: None)
    monkeypatch.setattr(srv.recovery_mechanism, "_write_action_log", lambda *a, **k: None)

    def boom():
        raise RuntimeError("systemctl reboot exited 1")

    await srv.recovery_mechanism._fire_recovery_escalation(
        "reboot", row_owner="[broker]test", label="auto reboot", detail="t", fire=boom, log=lambda m: None, reason="r"
    )
    assert host_rung_latch.in_flight() is None


def test_the_latch_expires_so_a_reboot_that_never_happened_does_not_hold_forever(monkeypatch):
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    monkeypatch.delenv("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", raising=False)
    host_rung_latch.latch("reboot", "r", now=1000.0)
    assert host_rung_latch.refuse("x", now=1000.0 + 899)
    assert host_rung_latch.refuse("x", now=1000.0 + 900) == ""
    assert "host_rung_latch_expired" in events
    assert host_rung_latch.in_flight() is None


def test_an_expired_watchdog_request_ends_the_latch_too(monkeypatch):
    """One bound for every reset path: once the hold watchdog reads the request as failed, the bridge,
    tray and operator paths resume with it, not on a later clock."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    host_rung_latch.latch("power-cycle", "r")
    srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic() - 901)
    assert srv.recovery_mechanism.host_action_pending() is None
    assert host_rung_latch.in_flight() is None
    assert host_rung_latch.refuse("bridge reset") == ""
    assert "host_rung_latch_expired" in events


@pytest.mark.parametrize("value,expected", [(None, 900.0), ("60", 60.0), ("0", 900.0), ("x", 900.0), ("99999", 3600.0)])
def test_the_latch_follows_the_host_action_pending_bound(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", raising=False)
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", value)
    assert host_rung_latch.latch_sec() == expected
