# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""An unkillable leftover fences the device for at most 600 s, then the host rung (spec 04 I70).

Fault class J-unkillable-leftover: a reaped job's process stuck in the kernel keeps
/dev/tenstorrent open, the fence (01 I16, 04 I21) refuses every reset over it, and no reset would
free it anyway. Past its budget the idle sampler asks for the host reboot (every chip present) or
the power cycle (a chip off the bus), under the host rung's own guards. Nothing in the path releases
the device: the boot that follows runs the startup gate. No test reaches a real reboot or cycle."""

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, rig  # noqa: F401 - the fixture
from tests.test_reaped_leftover import LEFTOVER_PID, _stage, leftover  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan

BUDGET = srv.LEFTOVER_UNKILLABLE_BUDGET_SEC
RETRY = srv.LEFTOVER_HOST_RUNG_RETRY_SEC


@pytest.fixture
def fence(leftover, rig, monkeypatch):  # noqa: F811 - the imported fixtures
    """A staged unkillable leftover on a 32-chip host with every chip present, the rig's rung seams
    and a reboot recorder. ``fence['holders']`` is the live holder scan; ``fence['order']`` what fired."""
    state = {
        "holders": [DeviceHolder(pid=LEFTOVER_PID, uid=1234)],
        "order": rig["order"],
        "rig": rig,
        "proc": leftover["proc"],
    }
    rig["beats"] = {str(i): 100 for i in range(EXPECTED)}
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in range(EXPECTED)])
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n=0: EXPECTED)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=state["holders"], complete=True))
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: True)
    monkeypatch.setattr(srv.recovery_mechanism, "_reboot_already_attempted", lambda **k: False)
    # A fired rung leaves its host action pending (I34), as the real request marker does.
    fired = []
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_pending", lambda: ("reboot", 1.0) if fired else None)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: rig["order"].append("cleared_dirty"))

    async def reboot(log, reason):
        rig["order"].append("reboot")
        fired.append("reboot")

    monkeypatch.setattr(rig["g"], "_auto_reboot_host", reboot)
    return state


def _open(fence):
    """The first scan opens the episode; returns its monotonic start."""
    srv._reaped_leftovers()
    assert srv._leftover_unkillable_mono is not None
    return srv._leftover_unkillable_mono


async def _tick(now):
    """One sampler tick at monotonic ``now``; waits for the try it spawned, if any."""
    spawned = srv._maybe_spawn_leftover_escalation(now=now)
    if spawned:
        await srv._leftover_rung_task
    return spawned


def _events(r, kind):
    return [f for k, f in r["events"] if k == kind]


async def test_no_rung_before_the_budget(fence):
    t0 = _open(fence)

    assert not await _tick(t0 + BUDGET - 1)
    assert fence["order"] == []
    assert srv._health_payload()["leftover_fence"]["escalation_stage"] == "waiting-budget"


async def test_a_reboot_once_past_the_budget_with_every_chip_present(fence):
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == ["reboot"]
    r = fence["rig"]
    assert len(_events(r, "leftover_unkillable_deadline")) == 1
    (rung,) = _events(r, "leftover_host_rung")
    assert rung["phase"] == "requested" and rung["detail"].startswith("reboot")
    assert srv._health_payload()["leftover_fence"]["escalation_stage"] == "requested"


async def test_a_power_cycle_with_chips_off_the_bus(fence):
    fence["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i >= 4}  # a tray off the bus
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == ["power_cycle"]
    (rung,) = _events(fence["rig"], "leftover_host_rung")
    assert rung["detail"] == f"power-cycle (4/{EXPECTED} chip(s) off the bus)"


async def test_a_leftover_gone_at_the_deadline_fires_nothing_and_closes_the_episode(fence):
    t0 = _open(fence)
    fence["holders"] = []

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    assert srv._leftover_unkillable_mono is None and srv._leftover_unkillable_since is None
    assert _events(fence["rig"], "leftover_unkillable_deadline") == []
    assert srv._health_payload()["leftover_fence"] is None


async def test_a_governor_denial_holds_then_fires_on_a_later_try(fence):
    r = fence["rig"]
    r["allowed"] = (False, "I65 cooldown until 15:26")
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    fenced = srv._health_payload()["leftover_fence"]
    assert fenced["escalation_stage"] == "held" and "I65 cooldown" in fenced["detail"]
    assert not await _tick(t0 + BUDGET + RETRY - 1)  # within the retry window: no try
    assert await _tick(t0 + BUDGET + RETRY)  # the next try: the cooldown still stands
    assert fence["order"] == []
    assert _events(r, "leftover_host_rung")[-1]["phase"] == "held"

    r["allowed"] = (True, "")
    assert await _tick(t0 + BUDGET + 2 * RETRY)
    assert fence["order"] == ["reboot"]
    assert [e["phase"] for e in _events(r, "leftover_host_rung")] == ["held", "requested"]
    assert len(_events(r, "leftover_unkillable_deadline")) == 1  # once per episode


async def test_a_held_stage_is_journaled_once_while_it_stands(fence):
    fence["rig"]["allowed"] = (False, "rate limit")
    t0 = _open(fence)

    for k in range(3):
        assert await _tick(t0 + BUDGET + k * RETRY)
    assert [e["phase"] for e in _events(fence["rig"], "leftover_host_rung")] == ["held"]


async def test_a_pending_host_action_asks_for_nothing(fence, monkeypatch):
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_pending", lambda: ("reboot", 30.0))
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", ("reboot", 1.0))
    t0 = _open(fence)

    assert not await _tick(t0 + BUDGET)
    assert fence["order"] == [] and srv._leftover_rung_task is None


async def test_a_device_op_in_flight_defers_the_try(fence, monkeypatch):
    monkeypatch.setattr(srv, "device_op_active", "gate")
    t0 = _open(fence)

    assert not await _tick(t0 + BUDGET)
    assert fence["order"] == []


async def test_another_tenant_blocks_the_rung_but_the_leftover_itself_does_not(fence):
    t0 = _open(fence)
    fence["holders"] = fence["holders"] + [DeviceHolder(pid=7300, uid=2345)]

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    fenced = srv._health_payload()["leftover_fence"]
    assert fenced["escalation_stage"] == "held" and "tenant pid 7300" in fenced["detail"]


async def test_a_running_job_blocks_the_rung(fence, monkeypatch):
    t0 = _open(fence)
    monkeypatch.setattr(srv.galaxy_recovery.deps, "job_running", lambda: True)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    assert "a broker job is running" in srv._health_payload()["leftover_fence"]["detail"]


async def test_no_host_rung_opted_in_journals_no_host_rung(fence, monkeypatch):
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: False)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: False)
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    (rung,) = _events(fence["rig"], "leftover_host_rung")
    assert rung["phase"] == "no_host_rung" and "an operator must reboot" in rung["detail"]
    assert srv._health_payload()["leftover_fence"]["escalation_stage"] == "no-host-rung"


def test_a_killable_leftover_starts_no_clock(fence):
    _stage(fence["proc"], 6200, state="S", sigpnd="0" * 16)
    fence["holders"] = [DeviceHolder(pid=6200, uid=1234)]

    assert srv._reaped_leftovers()  # fenced (01 I16), but not stuck in the kernel
    assert srv._leftover_unkillable_mono is None
    assert srv._health_payload()["leftover_fence"] is None


def test_the_health_fields(fence):
    assert srv._health_payload()["leftover_fence"] is None
    _open(fence)

    fenced = srv._health_payload()["leftover_fence"]
    assert fenced["fault_class"] == "J-unkillable-leftover"
    assert fenced["budget_sec"] == BUDGET
    assert fenced["escalation_stage"] == "waiting-budget" and fenced["detail"] is None
    assert f"pid {LEFTOVER_PID}" in fenced["leftovers"]
    from datetime import datetime

    since = datetime.fromisoformat(fenced["since"])
    assert (datetime.fromisoformat(fenced["deadline"]) - since).total_seconds() == BUDGET
    assert "only a host reboot clears it" in srv._reaped_leftover_reason()


def test_a_scan_error_leaves_the_clock_as_it_is(fence, monkeypatch):
    t0 = _open(fence)

    def broken():
        raise OSError("proc unreadable")

    monkeypatch.setattr(srv, "enumerate_device_holders", broken)
    srv._reaped_leftovers()
    assert srv._leftover_unkillable_mono == t0


def test_the_sampler_drives_the_escalation(monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "_maybe_spawn_idle_holder_watch", lambda: calls.append("idle"))
    monkeypatch.setattr(srv, "_maybe_spawn_leftover_escalation", lambda now=None: calls.append("leftover"))

    srv._recovery_deps.watch_idle_holders()
    assert calls == ["idle", "leftover"]


async def test_the_idle_holder_scan_keeps_the_clock(fence):
    await srv._idle_holder_watch_pass()
    assert srv._leftover_unkillable_mono is not None


async def test_time_to_request_is_bounded_and_nothing_releases_the_device(fence):
    """Fake-clock TTR: the sampler ticks every second from the first scan; the request comes at or
    before budget + one retry (660 s), and nothing on the path releases the device or clears it."""
    t0 = _open(fence)
    requested_at = None
    for s in range(0, BUDGET + RETRY + 1):
        await _tick(t0 + s)
        if fence["order"] and requested_at is None:
            requested_at = s
    assert requested_at is not None and requested_at <= BUDGET + RETRY
    assert fence["order"] == ["reboot"]  # one request, then the pending host action / stage holds
    assert "released" not in fence["order"] and "cleared_dirty" not in fence["order"]
    assert srv._leftover_unkillable_mono == t0  # the fence episode still stands until the host goes
