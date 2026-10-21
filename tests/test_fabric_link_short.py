# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The fabric link-short hold (spec 04 I83).

Every chip is on the bus, but the full fabric check counts fewer links or ports than the cabling
expects (a topology mismatch, or a known fabric fault). The broker runs its full reset ladder once, on
the first watchdog tick of the hold; when the full check after it (fabric included) still fails, it
asks for the power cycle at once. No later tick in the episode runs a second glx_reset or ladder. Only
the bounded boot / post-power-cycle cooldown may delay the cycle. No test reaches a real reset or cycle."""

import time
from datetime import datetime, timedelta

import pytest

from tests.test_device_safety import _arm_fabric_only_deadline_hold, _setup_startup_hold_behind_readopted_job
from tests.test_hold_bound import _KINDS, _REAL_HOST_RUNG, BOUND, _drain, _watchdog
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED, OUTCOME_TERMINAL, OUTCOME_WAITING

LINK_SHORT = ["topology mismatch", "known fabric fault"]
# The measured link-short holds: the spec 03 I42 latch on a short count, and a full check that failed on
# descriptor ports missing (spec 03 I71).
MEASURED = ["latched", "missing ports"]
ALL = LINK_SHORT + MEASURED
_LOST_PORT = "PortA <-> PortB"
LOST_DETAIL = (
    "fabric check FAILED (66s): fabric links missing: 1 port connection(s) of the cabling descriptor "
    f"missing, 0 extra, after traffic passed on the discovered links: {_LOST_PORT}"
)


def _arm(monkeypatch, hold, age_sec=10, latched=False):
    """A ``hold`` opened ``age_sec`` ago, with the watchdog at the start of its episode. The host rung
    is stubbed with its real bookkeeping (a requested power cycle marks the host action pending) and
    lands in events as ``host_rung``."""
    latched = latched or hold == "latched"
    if hold == "missing ports":
        why, detail, output, beats = srv.FABRIC_RELIFT_WHY, f"post-job: {LOST_DETAIL}", LOST_DETAIL, None
    else:
        why, detail, output, beats, _ladder = _KINDS["topology mismatch" if hold == "latched" else hold]
    escalations, alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output, beats=beats, age_min=age_sec / 60
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: hold == "77-only")
    if hold == "known fabric fault":
        monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "known fabric fault: 2 jobs in a row")
        monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "known fabric fault", raising=False)
    if hold == "off-bus":
        monkeypatch.setattr(srv, "dead_chips", lambda b: [])
    if latched:
        monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "hardware fault: needs an operator")
    if hold == "missing ports":
        monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", False)

    async def host_rung(age, log):
        events.append("host_rung")
        srv._hold_bound_rung_pending, srv._hold_bound_cycle_age = False, age
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    return escalations, alerts, events


def _ladder(monkeypatch, escalations, outcome, *, passes=False):
    async def ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        if passes:
            monkeypatch.setattr(srv, "_recovery_degraded", lambda: False)  # the full check passed
        return outcome

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)


def _tick(after_sec):
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=after_sec))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [OUTCOME_RECOVERED, OUTCOME_TERMINAL])
@pytest.mark.parametrize("hold", ALL)
async def test_one_ladder_at_hold_start_then_the_power_cycle_at_once(monkeypatch, clear_job_state, hold, outcome):
    """(a) the first tick of the hold runs the full ladder; (b) a full check after it that still counts
    short (or a ladder that did not bring the mesh back) asks for the host rung in the same run, long
    before the 10-min bound; nothing is released."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    _ladder(monkeypatch, escalations, outcome)
    _tick(0)
    await _drain()
    # Under the I42 latch the hold's bound is one sampler tick: past it, the same one ladder runs as the
    # latch's bound ladder (spec 03 I42), with the same check and host rung after it.
    kinds = ("fabric_link_short", "disproven_lift") if hold == "latched" else ("fabric_link_short",)
    assert srv.device_hold_escalation_kind in kinds
    assert escalations == ["present-forced"], "one full ladder at hold start, not at the 10-min mark"
    assert events.count("host_rung") == 1, "then the power cycle, at once"
    assert events.index("hold_deadline_forced_escalation") < events.index("host_rung")
    assert srv._recovery_degraded() and "device_released" not in events, "no release on a failed check"
    assert srv._hold_age_now() < BOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ALL)
async def test_no_second_glx_reset_or_ladder_in_the_episode(monkeypatch, clear_job_state, hold):
    """(c) after the at-start pass and its power-cycle request, no later tick (the bound, the next
    ceiling window, the hold deadline) runs another glx_reset or ladder, or asks for a second cycle."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    _tick(0)
    await _drain()
    for after in range(10, max(srv._hold_deadline_sec(), 2 * BOUND) + 20, 10):
        _tick(after)
        await _drain()
    assert escalations == ["present-forced"], "the at-start pass is the episode's only ladder"
    assert events.count("host_rung") == 1 and "device_released" not in events


class _Clock(datetime):
    """The broker's ``datetime`` with a settable offset: every age the watchdog and the forced run read
    moves together, and the hold episode (its ``since``) stays the same."""

    offset = 0.0

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(seconds=cls.offset)


def _clock(monkeypatch):
    monkeypatch.setattr(_Clock, "offset", 0.0)
    monkeypatch.setattr(srv, "datetime", _Clock)
    return _Clock


async def _walk(clock, start, stop, step=10):
    """One watchdog tick every ``step`` s of hold age from ``start`` (relative to the hold's opening
    age) up to ``stop``, each run drained before the next tick."""
    for after in range(start, stop, step):
        clock.offset = after
        srv._check_hold_deadline()
        await _drain()


@pytest.mark.asyncio
async def test_a_latched_mismatch_gets_one_ladder_at_hold_start_and_keeps_the_latch(monkeypatch, clear_job_state):
    """Spec 03 I42's latch on a one-port-short mismatch is this class (spec 04 I83): one ladder at hold
    start, with the latch still set; the full check after it counts the same port short and fails, the
    broker asks for the power cycle in the same run, and nothing is released or run again."""
    escalations, _alerts, events = _arm(monkeypatch, "topology mismatch", latched=True)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    clock = _clock(monkeypatch)
    assert srv._fabric_link_short_hold() == "topology latch", "a latched hold is a link-short hold"
    await _walk(clock, 0, 1)
    assert escalations == ["present-forced"], "the one ladder, at hold start"
    # Past one tick (the latch's bound) the same one ladder runs under the bound's kind.
    assert srv.device_hold_escalation_kind in ("fabric_link_short", "disproven_lift")
    assert events.count("host_rung") == 1, "the short count asks for the host rung in the same run"
    await _walk(clock, 1, BOUND + 30)
    assert escalations == ["present-forced"], "no second ladder below or at the bound"
    assert srv.health_monitor.topology_hw_fault, "the I42 latch stands through the bound"
    assert "hold_bound_latch_cleared" not in events
    assert events.count("host_rung") == 1 and "device_released" not in events
    assert srv._recovery_degraded()


def _carry_an_at_start_ladder(monkeypatch, hold, *, restart):
    """A link-short episode whose at-start ladder ran 20 s in and whose check released the device; it is
    held again 30 s in, now as ``hold`` (spec 04 I82 carry, or a broker restart's resume)."""
    escalations, _alerts, events = _arm(monkeypatch, hold, age_sec=30)
    srv._hold_bound_sync()
    carry = {"since": srv.device_hold_episode_since, "ladder_age": 20.0, "ladder_early": True, "cycle_age": None}
    if restart:
        monkeypatch.setattr(srv, "_hold_bound_episode", "")  # a new broker process: no bound state in memory
        srv._hold_bound_sync()
    srv._resume_hold_bound(carry)
    assert srv._hold_bound_early and srv._hold_bound_cycle_due(srv._hold_age_now())
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    return escalations, events


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True], ids=["carry", "broker-restart"])
@pytest.mark.parametrize("hold", ["off-bus", "generic", "77-only"])
async def test_a_rehold_of_another_class_after_a_carried_at_start_ladder_asks_for_the_host_rung_once(
    monkeypatch, clear_job_state, hold, restart
):
    """After a carried at-start ladder, a re-hold of another class below the bound goes to the
    host rung once; it never runs an unrecorded forced ladder on every tick."""
    escalations, events = _carry_an_at_start_ladder(monkeypatch, hold, restart=restart)
    monkeypatch.setattr(srv, "_offbus_one_shot_sec", lambda: 10_000.0)
    clock = _clock(monkeypatch)
    await _walk(clock, 0, 300)
    assert escalations == [], "no ladder below the bound: the carried one already ran"
    assert events.count("host_rung") == 1


async def _opted_out_or_denied(monkeypatch, escalations, events, how):
    """The real host rung, with both host rungs opted out or the power cycle denied for a reason that is
    not the cooldown (a stand-back check)."""
    monkeypatch.setattr(srv, "_hold_bound_host_rung", _REAL_HOST_RUNG)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
    monkeypatch.setattr(srv, "_bound_tenant_blocker", lambda *a: None)
    monkeypatch.setattr(srv.recovery_mechanism, "_journal_auto_recovery_denied", lambda *a: None)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: how == "denied")
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: False)
    monkeypatch.setattr(
        srv.recovery_mechanism, "auto_recovery_allowed", lambda action, **k: (False, "another host action stands")
    )

    async def no_cycle(log, reason):
        raise AssertionError("a denied power cycle never fires")

    monkeypatch.setattr(srv, "_auto_power_cycle_host", no_cycle)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hold,how",
    [(h, how) for h in ("topology mismatch", "missing ports", "latched") for how in ("opted-out", "denied")],
)
async def test_an_at_start_ladder_whose_host_rung_never_fired_does_not_stand_in_for_the_bounds_ladder(
    monkeypatch, clear_job_state, hold, how
):
    """The at-start ladder is a forced run below the
    bound. When the host rung after it was never granted, the full ladder still runs at the 10-min mark,
    and the later windows follow I76 as for any hold (a ladder each window when both host rungs are opted
    out; a denied rung stays pending and is asked again each tick)."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    await _opted_out_or_denied(monkeypatch, escalations, events, how)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    clock = _clock(monkeypatch)
    await _walk(clock, 0, BOUND - 20)
    assert escalations == ["present-forced"], "the at-start ladder, and none other below the bound"
    await _walk(clock, BOUND - 20, BOUND + 20)
    assert escalations == ["present-forced"] * 2, "the bound's own full ladder at 10 min"
    await _walk(clock, BOUND + 20, 3 * BOUND + 20)
    if how == "opted-out":
        assert escalations.count("present-forced") >= 3, "a later window runs the ladder again (I76)"
    else:
        assert escalations == ["present-forced"] * 2, "a denied rung stays pending, asked each tick (I76)"
        assert srv._hold_bound_rung_pending
    assert "device_released" not in events and srv._recovery_degraded()


@pytest.mark.asyncio
@pytest.mark.parametrize("passes", [False, True], ids=["check fails", "check passes"])
async def test_a_latched_hold_whose_power_cycle_is_denied_reruns_the_full_ladder_at_ten_minutes(
    monkeypatch, clear_job_state, passes
):
    """Under the I42 latch the hold's bound is one sampler tick, so its one at-start ladder counts as the
    bound's. A power cycle the governor then denies for a reason other than the cooldown (another host
    action stands) arms no retry: the rung stays pending and is asked each tick. That never stands in for
    the 10-min mark: the hold's full ladder runs again there, the same gentlest-first ladder (no host step
    of its own: no reboot, no cycle inside it), with the latch kept. Its full check decides: a failing one
    keeps the hold and asks for the power cycle again; only a passing one lets the hold lift."""
    escalations, _alerts, events = _arm(monkeypatch, "latched")
    await _opted_out_or_denied(monkeypatch, escalations, events, "denied")
    clock = _clock(monkeypatch)
    ladders, asks = [], []
    real_allowed = srv.recovery_mechanism.auto_recovery_allowed

    def allowed(action, **k):
        asks.append((action, srv._hold_age_now()))
        return real_allowed(action, **k)

    async def ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        ladders.append((srv._hold_age_now(), bool(srv.health_monitor.topology_hw_fault)))
        if passes and len(ladders) == 2:
            monkeypatch.setattr(srv, "_recovery_degraded", lambda: False)  # the full check passed
        return OUTCOME_RECOVERED

    async def no_reboot(log, reason):
        raise AssertionError("the 10-min ladder never reboots the host")

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)
    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", no_reboot)
    await _walk(clock, 0, BOUND - 20)
    assert escalations == ["present-forced"], "the one at-start ladder below the 10-min mark"
    assert asks and all(a == "power-cycle" for a, _ in asks), "the power cycle is asked after it"
    assert srv._hold_bound_rung_pending and srv.recovery_mechanism.power_cycle_retry is None, "denied, no retry"
    assert srv._recovery_degraded() and "device_released" not in events
    before = len(asks)
    await _walk(clock, BOUND - 20, BOUND + 20)
    assert escalations == ["present-forced"] * 2, "the full ladder again at the 10-min mark"
    assert ladders[1][0] >= BOUND
    assert all(latched for _, latched in ladders), "the I42 latch stands through both ladders"
    assert srv.health_monitor.topology_hw_fault
    if passes:
        assert not any(age >= ladders[1][0] for _, age in asks[before:]), "a passing check asks no cycle"
        return
    assert any(age >= ladders[1][0] for _, age in asks[before:]), "a failing check asks for the cycle again"
    assert srv._hold_bound_rung_pending, "still denied: pending, asked each tick (I76)"
    assert srv._recovery_degraded() and "device_released" not in events, "no lift without a passing check"
    await _walk(clock, BOUND + 20, 2 * BOUND - 20)
    assert escalations == ["present-forced"] * 2, "no further ladder inside the next window"


@pytest.mark.asyncio
async def test_a_latched_hold_cooling_past_the_ten_minute_mark_waits_only_for_the_cooldown(
    monkeypatch, clear_job_state
):
    """The boot / post-power-cycle cooldown is the one exemption from the 10-min ladder: a latched hold
    whose power cycle the cooldown denies runs no second ladder at the 10-min mark (its retry is armed),
    and the cycle fires on the first tick past the cooldown's end."""
    escalations, _alerts, events = _arm(monkeypatch, "latched")
    monkeypatch.setattr(srv, "_hold_bound_host_rung", _REAL_HOST_RUNG)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
    monkeypatch.setattr(srv, "_bound_tenant_blocker", lambda *a: None)
    monkeypatch.setattr(srv.recovery_mechanism, "_journal_auto_recovery_denied", lambda *a: None)
    clock = _clock(monkeypatch)
    cycles, state = [], {"cooling": True}
    until = datetime.now().timestamp() + BOUND + 120  # the cooldown runs past the 10-min mark

    def allowed(action, tenant_active=False, **k):
        if state["cooling"]:
            return srv.recovery_mechanism._deny_power_cycle(
                {"until": until, "last": until - 1800, "cause": "boot", "interval_sec": 1800, "why": "cooldown"}
            )
        return True, ""

    async def power_cycle(log, reason):
        cycles.append(srv._hold_age_now())

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    await _walk(clock, 0, 1)
    assert escalations == ["present-forced"] and cycles == []
    assert srv.recovery_mechanism.power_cycle_retry is not None, "the cooldown denial arms the retry"
    end = int(until - datetime.now().timestamp())
    await _walk(clock, 10, end - 10)
    assert escalations == ["present-forced"], "no ladder at the 10-min mark inside the cooldown"
    assert cycles == [] and srv._recovery_degraded() and "device_released" not in events
    state["cooling"] = False
    clock.offset = end + 1
    srv._check_hold_deadline(now=datetime.fromtimestamp(until + 1))  # the retry reads the tick's own clock
    await _drain()
    assert escalations == ["present-forced"], "the cooldown's end goes straight to the power cycle"
    assert len(cycles) == 1 and cycles[0] >= BOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["fails", "passes", "none"], ids=["check fails", "check passes", "no verdict"])
@pytest.mark.parametrize("hold", ["latched", "topology mismatch", "missing ports"])
async def test_a_power_cycle_denied_before_the_ten_minute_ladder_waits_for_that_ladders_check(
    monkeypatch, clear_job_state, hold, verdict
):
    """A power cycle denied below the 10-min mark for a reason other than the cooldown arms no retry: the
    host rung stays pending. The 10-min ladder spends it. The full check after that ladder still waits on
    the 1G hugepage pool when the denial clears, and no power cycle fires over it: only a failed verdict,
    or the end of _HOLD_BOUND_CHECK_SEC with no verdict, asks for the cycle; a passing check lifts the
    hold with none."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    await _opted_out_or_denied(monkeypatch, escalations, events, "denied")
    clock = _clock(monkeypatch)
    state = {"denied": True, "hugepages": False}
    ladders, cycles = [], []

    def allowed(action, **k):
        return (False, "another host action stands") if state["denied"] else (True, "")

    async def ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        ladders.append(srv._hold_age_now())
        if len(ladders) == 2:
            state["hugepages"] = True  # the full check after the 10-min ladder waits on the pool
            state["denied"] = False  # and the denial has cleared
        return OUTCOME_RECOVERED

    async def power_cycle(log, reason):
        cycles.append(srv._hold_age_now())

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    monkeypatch.setattr(srv, "_last_77_was_hugepages", lambda: state["hugepages"])
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)
    await _walk(clock, 0, BOUND - 20)
    assert len(ladders) == 1 and srv._hold_bound_rung_pending, "denied below the bound, no retry: pending"
    await _walk(clock, BOUND - 20, BOUND + 20)
    assert len(ladders) == 2 and ladders[1] >= BOUND, "the 10-min ladder"
    check_end = ladders[1] + srv._HOLD_BOUND_CHECK_SEC
    mid = int(ladders[1] - srv._hold_age_now() + clock.offset + srv._HOLD_BOUND_CHECK_SEC / 2)
    await _walk(clock, int(clock.offset) + 10, mid)
    assert cycles == [], "no power cycle while the check after the ladder still waits on the pool"
    assert srv._recovery_degraded() and "device_released" not in events
    if verdict == "fails":
        state["hugepages"] = False
        assert srv._note_hold_bound_check_failed(lambda m: None), "the check ends with no pass"
        await _walk(clock, mid, mid + 20)
        assert len(cycles) == 1 and cycles[0] < check_end, "a failed verdict asks for the cycle at once"
    elif verdict == "passes":
        monkeypatch.setattr(srv, "_recovery_degraded", lambda: False)  # the full check passed: the hold lifts
        await _walk(clock, mid, int(check_end - srv._hold_age_now() + clock.offset) + 60)
        assert cycles == [], "a passing check asks for no cycle"
    else:
        await _walk(clock, mid, int(check_end - srv._hold_age_now() + clock.offset) - 20)
        assert cycles == [], "the check keeps its whole window"
        await _walk(clock, int(clock.offset) + 10, int(check_end - srv._hold_age_now() + clock.offset) + 30)
        assert len(cycles) == 1 and check_end <= cycles[0] <= check_end + 30, "the cycle at the window's end"
    assert len(ladders) == 2, "no other ladder in the window"


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["missed request", "backstop", "per-boot cap"])
@pytest.mark.parametrize("hold", ["latched", "topology mismatch"])
async def test_a_retry_after_no_cycle_that_ran_does_not_defer_the_ten_minute_ladder(
    monkeypatch, clear_job_state, hold, why
):
    """Goal of spec 04 I76: only the boot / post-power-cycle cooldown holds past 10 min with no ladder. A
    power cycle denied for a missed request's backoff, the backstop or the per-boot cap arms a retry at
    that wait's end, but no cycle ran: the full ladder still runs at the 10-min mark, and the hold lifts
    only on a passing check after it."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    await _opted_out_or_denied(monkeypatch, escalations, events, "denied")
    clock = _clock(monkeypatch)
    until = datetime.now().timestamp() + 4 * BOUND  # the wait runs well past the 10-min mark
    ladders = []

    def allowed(action, **k):
        return srv.recovery_mechanism._deny_power_cycle(
            {"until": until, "last": until - 3600, "cause": why, "interval_sec": 3600, "why": why}, cooldown=False
        )

    async def ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        ladders.append(srv._hold_age_now())
        return OUTCOME_RECOVERED

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)
    await _walk(clock, 0, BOUND - 20)
    assert len(ladders) == 1 and srv.recovery_mechanism.power_cycle_retry is not None, "a retry is armed"
    assert not srv.recovery_mechanism.power_cycle_retry_cooldown
    await _walk(clock, BOUND - 20, BOUND + 20)
    assert len(ladders) == 2 and BOUND <= ladders[1] < BOUND + 20, "the full ladder at the 10-min mark"
    assert srv._recovery_degraded() and "device_released" not in events


@pytest.mark.asyncio
async def test_a_granted_host_rung_after_the_at_start_ladder_runs_no_second_ladder_at_the_bound(
    monkeypatch, clear_job_state
):
    """Only a host rung the governor granted (or a cooldown retry it armed) lets the at-start
    ladder stand in for the bound's: then no ladder runs at 10 min."""
    escalations, _alerts, events = _arm(monkeypatch, "topology mismatch")
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)

    async def host_rung(age, log):
        events.append("host_rung")
        srv._hold_bound_rung_pending, srv._hold_bound_cycle_age = False, age
        srv._hold_bound_granted_age = age  # the power cycle was requested and has not healed it yet

    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)
    clock = _clock(monkeypatch)
    await _walk(clock, 0, BOUND + 30)
    assert escalations == ["present-forced"] and events.count("host_rung") >= 1


@pytest.mark.asyncio
async def test_an_at_start_run_that_found_another_class_is_not_spawned_again(monkeypatch, clear_job_state):
    """A chip off the bus by heartbeat (none isolated yet) is not the link-short class. The
    at-start run declines once, and no later tick spawns it again below the bound."""
    escalations, _alerts, events = _arm(monkeypatch, "topology mismatch")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(31)})
    monkeypatch.setattr(srv, "_offbus_one_shot_sec", lambda: 10_000.0)
    monkeypatch.setattr(srv, "dead_chips", lambda b: [])
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    runs = []
    real = srv._maybe_spawn_forced_escalation

    def spawn():
        if srv.device_hold_escalation_kind == "fabric_link_short":
            runs.append(1)
        return real()

    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", spawn)
    clock = _clock(monkeypatch)
    await _walk(clock, 0, 300)
    assert len(runs) == 1, "one declined run, not one per tick"
    assert escalations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ALL)
async def test_a_full_check_that_passes_after_the_ladder_asks_for_no_power_cycle(monkeypatch, clear_job_state, hold):
    """(d) only a full check that passed after the ladder ends the hold's escalation: no host rung, then
    or on a later tick, and no second ladder."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED, passes=True)
    _tick(0)
    await _drain()
    _tick(10)
    await _drain()
    assert escalations == ["present-forced"]
    assert "hold_bound_check_failed" not in events and "host_rung" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["topology mismatch"] + MEASURED)
async def test_a_check_still_waiting_gets_the_power_cycle_by_the_bound(monkeypatch, clear_job_state, hold):
    """(d) a full check with no verdict yet (a 77 while the 1G hugepage pool fills) keeps the hold and
    asks for no cycle while it runs; it has until min(ladder end + _HOLD_BOUND_CHECK_SEC, the 10-min
    bound), then the host rung. The same window under the I42 latch, whose own bound is one sampler
    tick: the hold is 1 s old, so the latched one takes the at-start pass, and its ladder takes real hold
    time, so the latch's bound is long past when it ends."""
    escalations, _alerts, events = _arm(monkeypatch, hold, age_sec=1)
    monkeypatch.setattr(srv, "_last_77_was_hugepages", lambda: True)
    clock = _clock(monkeypatch)
    ladder_sec = 120

    async def ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        clock.offset += ladder_sec
        return OUTCOME_RECOVERED

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)
    await _walk(clock, 0, 1)
    assert escalations == ["present-forced"] and "host_rung" not in events, "the check waits on the pool"
    assert srv._recovery_degraded()
    assert srv.device_hold_escalation_kind == "fabric_link_short" and srv._hold_bound_early, "the at-start pass"
    lad = srv._hold_bound_ladder_age
    assert lad >= ladder_sec
    check_end = min(lad + srv._HOLD_BOUND_CHECK_SEC, BOUND)
    start = int(clock.offset) + 10
    await _walk(clock, start, start + int(check_end - srv._hold_age_now()) - 20)
    assert "host_rung" not in events, "the full check gets its time"
    await _walk(clock, int(clock.offset) + 10, BOUND + 30)
    assert escalations == ["present-forced"] and events.count("host_rung") == 1
    assert srv._hold_bound_cycle_age <= check_end + 20, "the host rung by the window's end, at the bound at the latest"


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["topology mismatch"] + MEASURED)
async def test_the_cooldown_delays_the_cycle_only_until_its_end(monkeypatch, clear_job_state, hold):
    """(e) a running boot / post-power-cycle cooldown denies the host rung and arms the retry; the cycle
    fires on the first tick past the cooldown's end, with no second ladder."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    monkeypatch.setattr(srv, "_hold_bound_host_rung", _REAL_HOST_RUNG)  # the governor decides below
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "_journal_auto_recovery_denied", lambda *a: None)
    cycles, until, state = [], datetime.now().timestamp() + 240, {"cooling": True}

    def allowed(action, tenant_active=False, **k):
        if state["cooling"]:
            return srv.recovery_mechanism._deny_power_cycle(
                {"until": until, "last": until - 1800, "cause": "boot", "interval_sec": 1800, "why": "cooldown"}
            )
        return True, ""

    async def power_cycle(log, reason):
        cycles.append(reason)

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    _ladder(monkeypatch, escalations, OUTCOME_TERMINAL)
    _tick(0)
    await _drain()
    assert escalations == ["present-forced"] and cycles == []
    assert srv.recovery_mechanism.power_cycle_retry is not None, "the denial arms the retry"

    srv._check_hold_deadline(now=datetime.fromtimestamp(until - 20))
    await _drain()
    assert cycles == [], "no cycle inside the cooldown"
    state["cooling"] = False
    srv._check_hold_deadline(now=datetime.fromtimestamp(until + 1))
    await _drain()
    assert escalations == ["present-forced"], "the cooldown's end goes straight to the power cycle"
    assert len(cycles) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["77-only", "off-bus", "generic"])
async def test_other_classes_get_no_ladder_at_hold_start(monkeypatch, clear_job_state, hold):
    """(f) the at-start pass is this class's only: a 77-only, off-bus or generic hold on its first tick
    runs no ladder from I83 (each keeps its own path: the idle relift, the off-bus one-shot, the bound)."""
    escalations, _alerts, events = _arm(monkeypatch, hold, age_sec=1)
    monkeypatch.setattr(srv, "_offbus_one_shot_sec", lambda: 60.0)
    _ladder(monkeypatch, escalations, OUTCOME_TERMINAL)
    _tick(0)
    await _drain()
    assert srv.device_hold_escalation_kind != "fabric_link_short"
    assert escalations == [] and "host_rung" not in events


def test_an_offbus_chip_is_not_the_link_short_class(monkeypatch, clear_job_state):
    """(f) with a chip isolated off the bus the hold is the partial off-bus class, which keeps its ladder."""
    _arm(monkeypatch, "topology mismatch")
    assert srv._fabric_link_short_hold() == "topology mismatch"
    monkeypatch.setattr(srv, "isolated_chips", {"29"})
    assert srv._fabric_link_short_hold() == ""
    assert not srv._fabric_link_short_ladder_due(10)


def _startup_link_short_hold(monkeypatch, tmp_path):
    """Spec 03 I75 with spec 04 I83: a broker restart re-adopted a running job and opened the startup
    hold 30 s ago, and the episode it continues names a topology mismatch, so the hold reads as the
    link-short class below the bound. The host rung's governor grants a power cycle, stubbed."""
    since, job, climbs, gate_calls, gate_heals, release_job, _tick_fixed, events = (
        _setup_startup_hold_behind_readopted_job(monkeypatch, tmp_path)
    )
    clock = _clock(monkeypatch)
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=30)).isoformat())
    monkeypatch.setattr(srv, "device_hold_episode_reason", "topology mismatch: 1 port connection differs")
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "")
    monkeypatch.setattr(srv, "_fabric_link_short_declined", None)
    monkeypatch.setattr(srv, "_hold_bound_episode", "")
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())  # as the real rung

    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda action, tenant_active: (True, ""))
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)

    async def tick(after_sec):
        clock.offset = after_sec
        srv._check_hold_deadline()
        if srv._forced_escalation_task is not None:
            await srv._forced_escalation_task

    assert srv._fabric_link_short_hold() == "topology mismatch"
    return job, climbs, gate_calls, gate_heals, release_job, tick, events, cycles


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_passes", [True, False], ids=["gate passes", "gate fails"])
async def test_a_startup_hold_link_short_leaves_the_at_start_ladder_to_the_startup_gate(
    monkeypatch, tmp_path, gate_passes
):
    """Spec 03 I75 with spec 04 I83: while the re-adopted job runs, the at-start ladder meets it on
    job_running and no host rung is asked for. Once the job ends and before the startup gate's verdict,
    the at-start ladder stands down to the gate (blocker startup_verify) and runs no rung. Only the gate's
    full check, fabric included, lifts the hold. A gate that fails gets the at-start ladder on the next
    tick and, when the full check after it still fails, one power cycle; no second ladder by the bound.
    Fails before fabric_link_short joined the I75 deferrable kinds: the ladder reset over the pending gate."""
    job, climbs, gate_calls, gate_heals, release_job, tick, events, cycles = _startup_link_short_hold(
        monkeypatch, tmp_path
    )
    gate_heals["on"] = gate_passes
    ladder = srv.galaxy_recovery.escalate

    async def unhealed_ladder(phase, indices, expected, log, **kw):
        outcome = await ladder(phase, indices, expected, log, **kw)
        return OUTCOME_WAITING if outcome == OUTCOME_RECOVERED else outcome  # its full check failed

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", unhealed_ladder)
    try:
        await tick(0)
        assert srv.device_hold_escalation_kind == "fabric_link_short"
        assert climbs == [] and cycles == [], "a rung over the running re-adopted job"
        assert ("stuck_hold_deferred", "job_running") in events

        job.status = srv.JobStatus.COMPLETED
        await tick(10)
        await tick(20)
        assert climbs == [] and cycles == [], "the at-start ladder reset before the startup gate's verdict"
        assert ("stuck_hold_deferred", "startup_verify") in events
        assert gate_calls == []

        await release_job()
        assert len(gate_calls) == 1 and gate_calls[0][1], "the startup gate runs the full check, fabric included"
        assert gate_calls[0][3] == [], "a reset ran before the gate"
        if gate_passes:
            assert srv.fsm.state is not ServerState.RECOVERING, "only the gate's passing full check lifts it"
            await tick(30)
            assert climbs == [] and cycles == []
            return
        assert srv.fsm.state is ServerState.RECOVERING, "a failed gate never lifts the hold"
        await tick(30)
        assert climbs == ["present-forced"], "the at-start ladder runs on the first tick after a failed gate"
        assert len(cycles) == 1, "the full check after it still fails: the power cycle at once"
        clock_bound = int(BOUND) + 30
        for after in range(40, clock_bound, 10):
            await tick(after)
        assert climbs == ["present-forced"] and len(cycles) == 1, "the hold was driven twice"
        assert srv.fsm.state is ServerState.RECOVERING
    finally:
        srv.readopted_scopes.clear()
        srv._startup_verify_task.cancel()


@pytest.mark.asyncio
async def test_a_below_bound_host_rung_never_fires_over_a_running_readopted_job(monkeypatch, tmp_path):
    """Spec 04 I83/I82 with spec 03 I75: a host rung due below the bound (an at-start ladder carried
    across the restart that did not heal) meets the running re-adopted job as a tenant (job_running): no
    power cycle is asked for while the job runs. Once it has ended, the rung is asked for."""
    job, climbs, _gate_calls, _gate_heals, _release_job, _tick, events, cycles = _startup_link_short_hold(
        monkeypatch, tmp_path
    )
    try:
        srv._hold_bound_sync()
        monkeypatch.setattr(srv, "_hold_bound_ladder_age", 20.0)
        monkeypatch.setattr(srv, "_hold_bound_early", True)
        monkeypatch.setattr(srv, "_hold_bound_check_failed_age", 25.0)
        assert srv._hold_bound_cycle_due(30.0), "control: the carried ladder's host rung is due"
        monkeypatch.setattr(srv, "device_hold_escalation_kind", "hold_bound_cycle")
        await srv._force_escalate_stuck_hold()
        assert cycles == [] and climbs == [], "a power cycle over the running re-adopted job"
        assert srv._hold_bound_tenant_blocker == {"blocker": "job_running"}

        job.status = srv.JobStatus.COMPLETED
        monkeypatch.setattr(srv, "device_hold_tenant_deferred", False)
        await srv._force_escalate_stuck_hold()
        assert len(cycles) == 1 and climbs == [], "the host rung after the job, no second ladder"
    finally:
        srv.readopted_scopes.clear()
        srv._startup_verify_task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ALL)
async def test_the_idle_relift_runs_no_glx_reset_once_the_at_start_pass_ran(monkeypatch, clear_job_state, hold):
    """(c) single flight: before the at-start pass the idle relift's present-mesh reset is its own; once
    that pass ran (and asked for the host rung) the watchdog's one ladder owns the episode's reset, and a
    measured-fault relift retry runs no other glx_reset."""
    escalations, _alerts, events = _arm(monkeypatch, hold)
    _ladder(monkeypatch, escalations, OUTCOME_RECOVERED)
    assert srv._fabric_link_short_hold(), "every chip on the bus, the fabric short: this class"
    assert not srv._link_short_relift_reset_owned(), "no pass yet: nothing owns the reset"
    _tick(0)
    await _drain()
    assert escalations == ["present-forced"] and events.count("host_rung") == 1
    assert srv._link_short_relift_reset_owned(), "the at-start pass owns the episode's reset"


@pytest.mark.asyncio
async def test_an_offbus_hold_under_the_latch_is_not_this_class(monkeypatch, clear_job_state):
    """(f) the I42 latch does not make an off-bus hold a link-short one: a chip off the bus by heartbeat
    runs no ladder at hold start (the at-start run reads the heartbeats, declines once and is not spawned
    again), the off-bus one-shot keeps its time, and an isolated chip (partial off-bus, or a whole tray)
    is never this class."""
    escalations, _alerts, events = _arm(monkeypatch, "off-bus", age_sec=1, latched=True)
    monkeypatch.setattr(srv, "_offbus_one_shot_sec", lambda: 60.0)
    _ladder(monkeypatch, escalations, OUTCOME_TERMINAL)
    _tick(0)
    await _drain()
    assert escalations == [] and "host_rung" not in events, "no ladder at hold start"
    assert srv._fabric_link_short_declined == srv.device_hold_episode_since
    assert not srv._fabric_link_short_ladder_due(10)
    _tick(60)
    await _drain()
    assert escalations == ["offbus-forced"], "the off-bus one-shot, at its own time"
    monkeypatch.setattr(srv, "isolated_chips", {str(i) for i in range(8)})
    assert srv._fabric_link_short_hold() == "", "a whole tray off the bus goes the off-bus way"


def test_a_missing_port_hold_needs_a_failed_full_check(monkeypatch, clear_job_state):
    """(f) the missing-port class is a FAILED full check (spec 03 I71). A check that passed (ports the
    accept list excuses never fail it) or one with no verdict is not this class."""
    _arm(monkeypatch, "missing ports")
    assert srv._fabric_link_short_hold() == "fabric links missing"
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", True)
    assert srv._fabric_link_short_hold() == ""
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    assert srv._fabric_link_short_hold() == ""
