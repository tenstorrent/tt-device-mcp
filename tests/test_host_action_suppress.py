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

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.offbus_power_cycle import (
    fire_command as _real_fire_command,  # before the conftest tripwire
)

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


# --- every other reset path (spec 04 I34): relift, gate, operator reset; a timed-out fire ---------


@pytest.fixture
def ladder_spy(monkeypatch):
    """Every rung escalate() can dispatch to, recorded instead of run."""
    ran = []

    async def _rung(name, *a, **k):
        ran.append(name)
        return True

    # On the class: undoing a patch on the instance would leave the bound method behind as an
    # instance attribute, shadowing a later test's class-level spy. Earlier tests that patched
    # the instance left such attributes; hide them for this test.
    gr = galaxy.GalaxyRecovery
    for name in ("_escalate_offbus_stuck_hold", "_escalate_stuck_hold", "_fire_gate_rung", "_tray_down_prelude"):
        if name in vars(srv.galaxy_recovery):
            monkeypatch.delattr(srv.galaxy_recovery, name)
    monkeypatch.setattr(gr, "_escalate_offbus_stuck_hold", lambda *a, **k: _rung("offbus", *a, **k))
    monkeypatch.setattr(gr, "_escalate_stuck_hold", lambda *a, **k: _rung("present", *a, **k))
    monkeypatch.setattr(gr, "_fire_gate_rung", lambda *a, **k: _rung("gate", *a, **k))
    monkeypatch.setattr(gr, "_tray_down_prelude", lambda *a, **k: _rung("prelude", *a, **k))
    return ran


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["offbus", "present", "offbus-forced", "gate/post-job"])
async def test_escalate_starts_no_rung_while_a_host_action_is_pending(held, ladder_spy, phase):
    """The relift, the gate and the forced pass all reach the ladder through escalate(): while a
    requested power cycle is pending none of them runs a rung. One line per request; bounded."""
    srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())
    lines = []
    for _ in range(3):
        out = await srv.galaxy_recovery.escalate(phase, ["0", "1"], 32, lines.append, stage="smi_reset")
        assert out == galaxy.OUTCOME_WAITING
    assert ladder_spy == [], "no rung may start under a pending host action"
    suppressed = [f for k, f in held["events"] if k == "reset_suppressed_host_action"]
    assert len(suppressed) == 1 and suppressed[0]["action"] == "power-cycle" and len(lines) == 1
    assert srv._device_unavailable_for_tenant(), "a held device stays refused to tenants"

    srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic() - 901)
    await srv.galaxy_recovery.escalate(phase, ["0", "1"], 32, lines.append, stage="smi_reset")
    assert ladder_spy, "an expired request hands the hold back to the ladder"
    assert "host_action_request_expired" in _kinds(held["events"])


@pytest.mark.asyncio
async def test_the_gate_runs_no_rung_and_holds_while_a_host_action_is_pending(monkeypatch, tmp_path, clear_job_state):
    """A between-jobs gate on an unhealthy mesh, with a power cycle pending: no reset, no host-rung
    verdict read off a mesh going down, and the device stays held for tenants."""
    from tests.test_device_safety import _reach_reboot_rung

    events, resets = [], {"n": 0}
    reboots = _reach_reboot_rung(monkeypatch, tmp_path, n_present=32)
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 + i for i in range(12)})

    async def counting_reset(indices, log):
        resets["n"] += 1
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", counting_reset)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setattr(recovery_base, "health_event", lambda name, **k: events.append(name))
    srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert resets["n"] == 0 and reboots["n"] == 0
    assert "reset_suppressed_host_action" in events
    assert "reset_unrecoverable_power_cycle_required" not in events, "no verdict off a mesh going down"
    assert srv._device_unavailable_for_tenant(), "the device stays held"


def test_a_job_held_past_its_bound_names_the_pending_host_action(held):
    srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    class _Job:
        id, owner = "042", "tenant"

    why = srv._escalate_held_job_past_bound(_Job(), 1300, 1200, "device held")
    assert "power-cycle" in why and "device op is in flight" not in why
    assert held["spawned"] == []


@pytest.mark.parametrize("route", ["/api/tt_device_reset", "/api/tt_device_reset_stream"])
def test_an_operator_reset_is_refused_while_a_host_action_is_pending(monkeypatch, tmp_path, route):
    """Forced or not by the gate, an operator reset under a pending cycle races the host going down:
    it is refused (``force`` overrides), and nothing reaches the device."""
    from starlette.testclient import TestClient

    from tt_device_mcp.device_holders import HolderScan

    ran = []
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", ("power-cycle", time.monotonic()))
    client = TestClient(srv.build_asgi_app(srv.create_mcp_server()))
    resp = client.post(route, json={"force": False})
    body = resp.text
    if route.endswith("_stream"):
        assert body.rstrip().endswith("::status::refused") and "power-cycle" in body
    else:
        assert resp.json()["status"] == "refused" and "power-cycle" in resp.json()["reason"]
    assert ran == []
    assert srv._host_action_reset_refusal(True) is None, "force overrides"


@pytest.mark.asyncio
async def test_a_power_cycle_command_that_timed_out_stays_pending(held, monkeypatch):
    """A cycler that timed out may already have started its detached cycle: the mark stays, with
    its normal bound, so no reset starts under it. A non-zero exit still clears it."""
    from tt_device_mcp.health.recovery import offbus_power_cycle as opc

    monkeypatch.setattr(opc, "_run", lambda *a, **k: (None, "timed out after 30s"))
    with pytest.raises(recovery_base.HostActionTimedOut):
        _real_fire_command(["cycler"], REASON)
    monkeypatch.setattr(opc, "_run", lambda *a, **k: (1, "nope"))
    with pytest.raises(RuntimeError) as err:
        _real_fire_command(["cycler"], REASON)
    assert not isinstance(err.value, recovery_base.HostActionTimedOut)

    def _timed_out():
        raise recovery_base.HostActionTimedOut("power-cycle command timed out after 30s")

    monkeypatch.setattr(srv, "_fire_power_cycle", _timed_out)
    await srv._auto_power_cycle_host(lambda m: None, REASON)
    failed = [f for k, f in held["events"] if k == "auto_power_cycle_failed"]
    assert failed and failed[0]["timed_out"] is True
    assert srv.recovery_mechanism.host_action_pending() is not None
    _offbus_tick()
    assert srv._forced_escalation_task is None and held["spawned"] == []

    srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic() - 901)
    _offbus_tick()
    await srv._forced_escalation_task
    assert held["spawned"] == [True], "the bound still holds after a timeout"


# --- no latch stops resets when the cold rung is refused or not opted in (spec 04 I36 governs) ----


@pytest.mark.asyncio
@pytest.mark.parametrize("cold_rung", ["refused", "not-opted-in"])
async def test_a_shrinking_offbus_set_resets_again_when_the_power_cycle_cannot_fire(
    monkeypatch, clear_job_state, cold_rung
):
    """A reset ran, failed and left chips off the bus; the power cycle is denied by its rate limit or
    not opted in. A chip came back by the next forced window, so the reset runs again: nothing stops
    resets for the rest of the hold while the cold rung cannot fire: no unbounded hold."""
    from tests import test_no_repeat_failed_rung as nr

    counters, events, _ = nr._episode(monkeypatch, power_cycle=cold_rung == "refused")
    if cold_rung == "refused":
        monkeypatch.setattr(
            srv.recovery_mechanism,
            "read_auto_recovery_ledger",
            lambda: [{"action": "power-cycle", "at_epoch": time.time()}],
        )
    assert await nr._window() is True and counters["resets"] == 1
    for still_off in (nr.OFF_BUS - 1, nr.OFF_BUS - 2):
        monkeypatch.setattr(srv, "read_heartbeats", lambda n=still_off: {str(i): 100 for i in range(nr.EXPECTED - n)})
        assert await nr._window() is True
    assert counters["resets"] == 3, "each shrunk set earns its reset"
    assert "device_reset_skipped_power_cycle_required" not in events and nr.SKIP not in events
    assert counters["power_cycles"] == 0 and counters["dirty_cleared"] == 0, "the device stays held"


@pytest.mark.asyncio
async def test_the_gate_keeps_resetting_a_drop_whose_power_cycle_is_not_opted_in(
    monkeypatch, tmp_path, clear_job_state
):
    """The gate's reset ran, hard-failed and left the drop needing the cold rung, with none opted in.
    The next gate pass, once the reset cooldown ran out, still runs the reset: the gate's cooldown
    bounds the retry rate, no latch ends the retries."""
    from tests.test_device_safety import _reach_reboot_rung_with_regressing_reset

    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.now().isoformat(timespec="seconds"))
    srv.device_hold_logged = True
    reboots = _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before=20, off_after=20)
    events, resets = [], {"n": 0}
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")

    async def hard_failing_reset(indices, log):
        resets["n"] += 1
        srv.recovery_mechanism.last_reset_monotonic = time.monotonic()
        srv.recovery_mechanism.last_reset_exit_nonzero = True
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", hard_failing_reset)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert resets["n"] == 1 and "reset_unrecoverable_power_cycle_required" in events
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)  # the reset cooldown ran out
    events.clear()
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert resets["n"] == 2, "the gate retries the reset; no latch stops it"
    assert "device_reset_skipped_power_cycle_required" not in events
    assert reboots["n"] == 0
