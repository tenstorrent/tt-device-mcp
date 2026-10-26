# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""An unkillable leftover fences the device for at most 600 s, then the host rung (spec 04 I70).

Fault class J-unkillable-leftover: a reaped job's process stuck in the kernel keeps
/dev/tenstorrent open, the fence (01 I16, 04 I21) refuses every reset over it, and no reset would
free it anyway. Past its budget the idle sampler asks for the power cycle, the only host rung,
whether every chip is present or not, under the host rung's own guards. Nothing in the path releases
the device: the boot that follows runs the startup gate. No test reaches a real power cycle."""

import types
from datetime import datetime, timedelta
from functools import partial

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, rig  # noqa: F401 - the fixture
from tests.test_hold_bound import _drain, _present_hold_at_the_bound
from tests.test_reaped_leftover import LEFTOVER_PID, _stage, leftover  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy

BUDGET = srv.LEFTOVER_UNKILLABLE_BUDGET_SEC
RETRY = srv.LEFTOVER_HOST_RUNG_RETRY_SEC
_REAL_POWER_CYCLE = srv._auto_power_cycle_host  # captured at import, before the rig stubs it


@pytest.fixture
def fence(leftover, rig, monkeypatch):  # noqa: F811 - the imported fixtures
    """A staged unkillable leftover on a 32-chip host with every chip present, the rig's rung seams
    ``fence['holders']`` is the live holder scan; ``fence['order']`` what fired."""
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
    # A fired rung leaves its host action pending (I34), as the real request marker does.
    monkeypatch.setattr(
        srv.recovery_mechanism,
        "host_action_pending",
        lambda: ("power-cycle", 1.0) if "power_cycle" in rig["order"] else None,
    )
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: rig["order"].append("cleared_dirty"))

    return state


def _open(fence):
    """The first scan opens the episode; returns its monotonic start, pinned to a whole second.
    A fractional start makes the tick arithmetic inexact: ``(t0 + 600) - t0`` can come out just
    under 600, and the try at the budget would read as one before it."""
    srv._reaped_leftovers()
    assert srv._leftover_unkillable_mono is not None
    srv._leftover_unkillable_mono = float(int(srv._leftover_unkillable_mono))
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


async def test_a_power_cycle_once_past_the_budget_with_every_chip_present(fence):
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == ["power_cycle"]
    r = fence["rig"]
    assert len(_events(r, "leftover_unkillable_deadline")) == 1
    (rung,) = _events(r, "leftover_host_rung")
    assert rung["phase"] == "requested" and rung["detail"].startswith("power-cycle")
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


async def test_a_leftover_that_leaves_while_the_try_waits_on_the_lock_fires_nothing(fence, monkeypatch):
    """The try's own scan saw the leftover; it exits before the rung, under the device lock, scans."""

    calls = []

    def pending_then_gone():
        calls.append(1)
        if len(calls) > 1:  # the first read is the sampler's, before the spawn; this one is the rung's
            fence["holders"] = []  # it closed its fds while the try waited on the lock
        return None

    monkeypatch.setattr(srv.recovery_mechanism, "host_action_pending", pending_then_gone)
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    (rung,) = _events(fence["rig"], "leftover_host_rung")
    assert rung["phase"] == "gone"
    assert srv._leftover_unkillable_mono is None, "the rung's 'gone' closes the episode"
    assert srv._health_payload()["leftover_fence"] is None


@pytest.mark.parametrize("change", ["gone", "tenant", "scope"])
@pytest.mark.parametrize("during", ["stand-back", "governor"])
async def test_a_change_during_the_last_checks_holds_the_rung(fence, monkeypatch, during, change):
    """The stand-back command can run for its whole timeout: a leftover that exits, a tenant that
    arrives or a reset scope that starts meanwhile is seen on a fresh scan and nothing fires."""
    r = fence["rig"]
    g = r["g"]

    def arrive():
        if change == "gone":
            fence["holders"] = []
        elif change == "tenant":
            fence["holders"] = fence["holders"] + [DeviceHolder(pid=7300, uid=2345, scoped=True)]
        else:
            r["scope"] = "tt-reset-1.scope"

    if during == "stand-back":  # the power-cycle path: a tray off the bus
        r["beats"] = {str(i): 100 for i in range(EXPECTED) if i >= 4}
        monkeypatch.setattr(galaxy.offbus_power_cycle, "standback_reason", lambda **k: arrive() or "")
    else:
        monkeypatch.setattr(g.mechanism, "auto_recovery_allowed", lambda action, **k: arrive() or (True, ""))
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == [], "nothing fires"
    (rung,) = _events(r, "leftover_host_rung")
    if change == "gone":
        assert rung["phase"] == "gone" and srv._leftover_unkillable_mono is None
    else:
        want = "tenant pid 7300" if change == "tenant" else "a reset is cycling in its own scope"
        assert rung["phase"] == "held" and want in rung["detail"]
        assert srv._health_payload()["leftover_fence"]["escalation_stage"] == "held"


async def test_the_operator_stand_back_holds_the_power_cycle_until_it_is_gone(fence, monkeypatch, tmp_path):
    fence["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i >= 4}  # a tray off the bus
    marker = tmp_path / "cycle-in-progress"
    marker.write_text("")
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE", str(marker))
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    fenced = srv._health_payload()["leftover_fence"]
    assert fenced["escalation_stage"] == "held"
    assert "standing back from the power cycle" in fenced["detail"] and str(marker) in fenced["detail"]

    marker.unlink()
    assert await _tick(t0 + BUDGET + RETRY)
    assert fence["order"] == ["power_cycle"]
    assert [e["phase"] for e in _events(fence["rig"], "leftover_host_rung")] == ["held", "requested"]


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
    assert fence["order"] == ["power_cycle"]
    assert [e["phase"] for e in _events(r, "leftover_host_rung")] == ["held", "requested"]
    assert len(_events(r, "leftover_unkillable_deadline")) == 1  # once per episode


async def test_a_held_stage_is_journaled_once_while_it_stands(fence):
    fence["rig"]["allowed"] = (False, "rate limit")
    t0 = _open(fence)

    for k in range(3):
        assert await _tick(t0 + BUDGET + k * RETRY)
    assert [e["phase"] for e in _events(fence["rig"], "leftover_host_rung")] == ["held"]


async def test_a_pending_host_action_asks_for_nothing(fence, monkeypatch):
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_pending", lambda: ("power-cycle", 30.0))
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", ("power-cycle", 1.0))
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
    fence["holders"] = fence["holders"] + [DeviceHolder(pid=7300, uid=2345, scoped=True)]

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    fenced = srv._health_payload()["leftover_fence"]
    assert fenced["escalation_stage"] == "held" and "tenant pid 7300" in fenced["detail"]


async def test_a_running_job_blocks_the_rung(fence, monkeypatch):
    t0 = _open(fence)
    # The rung's guard asks for a running job the broker did not kill for recovery (spec 04 I77).
    monkeypatch.setattr(srv.galaxy_recovery.deps, "live_job_running", lambda: True)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    assert "a broker job is running" in srv._health_payload()["leftover_fence"]["detail"]


async def test_no_host_rung_opted_in_journals_no_host_rung(fence, monkeypatch):
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: False)
    t0 = _open(fence)

    assert await _tick(t0 + BUDGET)
    assert fence["order"] == []
    (rung,) = _events(fence["rig"], "leftover_host_rung")
    assert rung["phase"] == "no_host_rung" and "an operator must power-cycle" in rung["detail"]
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
    assert requested_at == BUDGET  # the first tick at the budget asks, well inside budget + one retry
    assert fence["order"] == ["power_cycle"]  # one request, then the pending host action / stage holds
    assert "released" not in fence["order"] and "cleared_dirty" not in fence["order"]
    assert srv._leftover_unkillable_mono == t0  # the fence episode still stands until the host goes


# --- the 10-min hold bound (I76) and this budget meet: one power-cycle request per episode -------


@pytest.fixture
def both(fence, monkeypatch, clear_job_state):  # noqa: F811 - the imported fixtures
    """A killed job's unkillable leftover AND its hold, both at 600 s, with only the power cycle opted
    in (so both paths pick it), the REAL governor (ledger, I65 cooldown, per-boot cap), the real
    request marker (I34) and the real power-cycle request with only its ``fire`` stubbed.
    ``both['fires']`` counts the cycler launches; ``both['outcome']`` sets how each launch ends."""
    r = fence["rig"]
    m = srv.recovery_mechanism
    _present_hold_at_the_bound(r, monkeypatch, why="job_killed", detail="job 7 killed: device left dirty")
    monkeypatch.setattr(m, "auto_recovery_allowed", partial(type(m).auto_recovery_allowed, m))
    monkeypatch.setattr(m, "host_action_pending", types.MethodType(type(m).host_action_pending, m))
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-A")
    monkeypatch.delenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", raising=False)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", _REAL_POWER_CYCLE)
    state = {"fires": 0, "outcome": "launched"}

    def fire():
        state["fires"] += 1
        if state["outcome"] == "timed-out":  # the cycler may still act: the request stays pending
            raise recovery_base.HostActionTimedOut("power-cycle command timed out after 30s")

    monkeypatch.setattr(srv, "_fire_power_cycle", fire)
    # A fabric pass that passed before the episode: the request voids it, so the full check is owed.
    monkeypatch.setattr(srv, "last_fabric_ok_monotonic", 1.0)
    return state


async def _bound(both):
    """One hold-deadline watchdog tick past the 10-min bound; waits for the run it spawned, if any."""
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=10))
    await _drain()


@pytest.mark.parametrize("outcome", ["launched", "timed-out"])
@pytest.mark.parametrize("order", ["bound-first", "leftover-first", "leftover-try-spawned-before-the-bound"])
async def test_replay_the_bound_and_the_leftover_budget_request_one_power_cycle(fence, both, order, outcome):
    """Replay: a killed job's process stuck in the kernel keeps the device; the hold it left reaches
    the 10-min bound (I76, ``hold_bound_leftover``) as the leftover reaches its 600 s budget (I70).
    Both paths ask for the host rung, and here both would pick the power cycle. Whichever asks first,
    and however its cycler launch ends, the cycler is launched once: the request marker (I34) and the
    governor's ledger stop the other. The last order is the race: a leftover try the sampler spawned
    before the bound's request, which reaches the device lock after it. A launch that timed out
    retracts its ledger record but stays pending, so only the marker stops that try. Nothing
    releases the device: it stays held with the full check, fabric included, owed (goal 8)."""
    both["outcome"] = outcome
    t0 = _open(fence)
    if order == "bound-first":
        await _bound(both)
        assert both["fires"] == 1
        assert "hold_bound_leftover" in [k for k, _ in fence["rig"]["events"]]
        assert not await _tick(t0 + BUDGET), "a pending request spawns no leftover try"
    elif order == "leftover-first":
        assert await _tick(t0 + BUDGET)
        assert both["fires"] == 1
        await _bound(both)
    else:
        await _bound(both)
        assert both["fires"] == 1
        await srv._escalate_unkillable_leftover()  # the try spawned before, reaching the lock now
        assert srv._health_payload()["leftover_fence"]["escalation_stage"] == "held"
    for s in (RETRY, 2 * RETRY):  # the later leftover tries and bound ticks inside the pending window
        await _tick(t0 + BUDGET + s)
        await _bound(both)

    assert both["fires"] == 1, f"one power-cycle request per episode, got {both['fires']}"
    assert srv.recovery_mechanism.host_action_pending()[0] == "power-cycle"
    assert "released" not in fence["order"] and "cleared_dirty" not in fence["order"]
    assert srv._recovery_degraded(), "the device stays held until the host comes back"
    assert srv._device_unavailable_for_tenant()
    assert srv._fabric_ok_age() is None, "the full health check (fabric included) is still owed"
