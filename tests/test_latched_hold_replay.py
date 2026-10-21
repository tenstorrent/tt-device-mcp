# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Replay of a Galaxy hold sequence on a virtual clock (spec 04 I76, spec 03 I42).

A fabric pass that could not run (rc 77) holds the device; tenant jobs hit the tenant-hold bound
again and again; about 2 h later a last-chance sweep releases the device; the next job fails on
the fabric, which sets the I42 topology latch and opens a new 77 hold. Each hold runs the full
reset ladder at 10 min; a full check that still fails (fabric included) asks for exactly one power
cycle per episode (a request the host never acted on is retried under spec 04 I72, not counted
here); only the bounded post-power-cycle cooldown may hold longer, and the cycle fires as it ends;
nothing is released without a passing full check. The I42-latched hold's bound is one sampler tick
(spec 03 I42): its ladder and power cycle come then, not at 10 min. Also: a hold across a broker restart keeps its
start, lift/fail cycles keep one clock, and a missing-link hold is never lifted on an unrunnable
fabric pass. No device is opened."""

import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty
from tests.test_classac_power_cycle_direct import EXPECTED, rig  # noqa: F401 - the fixture
from tests.test_hold_bound import BOUND, MISMATCH, _drain, _watchdog
from tt_device_mcp import server as srv

BASE = datetime(2026, 1, 5, 12, 0, 0)
HOLD_77 = "post-job: fabric check COULD NOT RUN (77)"


class _Clock(datetime):
    """srv's datetime with now() at BASE + ``t`` seconds."""

    t = 0.0

    @classmethod
    def now(cls, tz=None):
        return BASE + timedelta(seconds=cls.t)


class _Job:
    id, owner = "41", "tenant"


@pytest.fixture
def replay(rig, monkeypatch, clear_job_state):  # noqa: F811
    """``rig`` with a virtual clock, a mesh whose full check reads ``fabric_ok``, and each verdict
    and release recorded in ``order``."""
    order, g, mon = rig["order"], rig["g"], srv.health_monitor
    _Clock.t = 0.0
    monkeypatch.setattr(srv, "datetime", _Clock)
    rig.update(fabric_ok=False, checks=[])
    rig["beats"] = {str(i): 100 for i in range(EXPECTED)}
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in range(EXPECTED)])
    monkeypatch.setattr(mon, "expected", lambda n: EXPECTED)
    monkeypatch.setattr(mon, "known_fabric_fault", lambda: "")
    monkeypatch.setattr(mon, "last_fabric_output", "")
    monkeypatch.setattr(mon, "last_fabric_ok", None)
    monkeypatch.setattr(mon, "topology_hw_fault", "")
    monkeypatch.setattr(mon, "topology_lift_pending", False)
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    monkeypatch.setattr(srv, "device_hold_job_bound_escalated", False)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    _watchdog(monkeypatch)

    async def full_check(expected, log, run_fabric=True, **k):
        assert run_fabric, "every check after a reset includes the fabric (goal 8)"
        rig["checks"].append(rig["fabric_ok"])
        order.append(("check", rig["fabric_ok"]))
        detail = "all links ok" if rig["fabric_ok"] else (MISMATCH if mon.topology_hw_fault else "rc=77")
        return rig["fabric_ok"], {"fabric": {"ok": rig["fabric_ok"], "detail": detail}}

    async def reset_then_full_check(indices, log):
        order.append("glx_reset")
        ok, _ = await g._verify_device(EXPECTED, log, run_fabric=True)
        return ok

    def release(why):
        order.append(("released", rig["checks"][-1] if rig["checks"] else None))

    rig_cycle = srv._auto_power_cycle_host

    async def cycle_and_mark(log, reason):
        # As the real one: a launched request is marked pending, so nothing asks for a second cycle.
        await rig_cycle(log, reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(g, "_verify_device", full_check)
    monkeypatch.setattr(g, "_reset_and_verify_device", reset_then_full_check)
    monkeypatch.setattr(srv, "_clear_device_reported_fault", release)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle_and_mark)
    return rig


def _open_hold(detail: str, at: float) -> None:
    _Clock.t = at
    fsm_dirty(srv, detail, why=srv.FABRIC_RELIFT_WHY)
    srv.device_hold_episode_since = srv.datetime.now().isoformat()
    srv.device_hold_episode_reason = detail


async def _tick(at: float) -> None:
    _Clock.t = at
    srv._check_hold_deadline(now=srv.datetime.now())
    await _drain()


def _cycles(order) -> int:
    return order.count("power_cycle")


def _releases_on(order) -> list:
    return [ok for step in order if isinstance(step, tuple) and step[0] == "released" for ok in [step[1]]]


@pytest.mark.asyncio
async def test_a_77_hold_gets_the_ladder_at_10_min_one_cycle_and_releases_only_on_a_pass(replay):
    order = replay["order"]
    _open_hold(HOLD_77, 0)
    assert srv._fabric_77_only_hold(), "a 77-only hold: below the bound nothing is forced"
    for at in (60, 300, BOUND - 1):
        await _tick(at)
    assert "glx_reset" not in order and _cycles(order) == 0

    await _tick(BOUND)
    assert "glx_reset" in order, "the full ladder ran at the 10-min mark"
    assert replay["checks"] and not any(replay["checks"]), "the full check after it, fabric included, failed"
    assert _cycles(order) == 1, "a failed check after the ladder asks for the power cycle"
    assert order.index("glx_reset") < order.index("power_cycle")

    # Tenant jobs hit their hold bound again and again; the watchdog keeps ticking. One cycle per episode.
    for at in range(2 * BOUND, 12 * BOUND, BOUND):
        _Clock.t = at
        said = srv._escalate_held_job_past_bound(_Job(), at, 2 * BOUND, "device held")
        assert said != "forced recovery escalation started", said
        await _tick(at + 1)
    assert _cycles(order) == 1, "never a second power-cycle request in one episode"
    assert _releases_on(order) == [], "nothing released on a failed check"


def _hold_via_gate(detail: str, at: float) -> None:
    """Open (or re-open) the hold the way production does: the tenant gate's verdict starts the
    episode clock, restoring a recorded start after a broker restart."""
    _Clock.t = at
    fsm_dirty(srv, detail, why=srv.FABRIC_RELIFT_WHY)
    srv._note_tenant_gate_verdict(detail)


def _lift_via_gate(at: float) -> None:
    _Clock.t = at
    srv._note_tenant_gate_verdict("")


def _restart_broker(monkeypatch) -> None:
    """A new broker process: in-memory hold state is gone, the recorded episode start is on disk."""
    for name, value in (
        ("device_hold_logged", False),
        ("device_hold_episode_since", ""),
        ("device_hold_escalate_bucket", 0),
        ("device_hold_deadline_bucket", 0),
        ("_hold_episode_restorable", True),
        ("_hold_bound_episode", ""),
        ("_hold_bound_ladder_age", None),
        ("_hold_bound_cycle_age", None),
        ("_hold_bound_check_failed_age", None),
    ):
        monkeypatch.setattr(srv, name, value)


@pytest.mark.asyncio
async def test_the_i42_latched_hold_after_a_lift_gets_the_ladder_one_tick_in_and_one_cycle(replay):
    """The sequence's second episode: a release about 2 h into the first, the first job after it fails
    on the fabric (I42 latch set) and a new hold opens. The latch is no exemption from the bound: its
    bound is one sampler tick, so the full ladder runs then, well inside the 10-min bound."""
    order = replay["order"]
    _Clock.t = 2 * 3600
    srv.health_monitor.topology_lift_pending = True
    detail = srv.health_monitor.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert detail and srv.health_monitor.topology_hw_fault, "the first job after the lift set the I42 latch"
    start = 2 * 3600 + 5
    tick = float(srv.SAMPLE_INTERVAL_SEC)
    _open_hold(detail, start)
    await _tick(start + tick - 1)
    assert "glx_reset" not in order and _cycles(order) == 0

    await _tick(start + tick)
    assert "glx_reset" in order, "the latched hold gets the full ladder one sampler tick in"
    assert replay["checks"] and not any(replay["checks"]), "fabric included, the check after it failed"
    assert _cycles(order) == 1 and order.index("glx_reset") < order.index("power_cycle")
    assert srv.health_monitor.topology_hw_fault, "the latch stands: the post-ladder check must not pass on it"
    for at in range(2 * BOUND, 6 * BOUND, BOUND):
        await _tick(start + at + 1)
    assert _cycles(order) == 1, "one power-cycle request per episode"
    assert _releases_on(order) == [], "nothing released on a failed check"


@pytest.mark.asyncio
async def test_a_hold_that_spans_a_broker_restart_gets_the_ladder_10_min_after_it_began(replay, monkeypatch):
    """A hold opens, the broker restarts 8 min in (new code installed): the restored episode keeps its
    start, so the full ladder runs at 10 min of the hold, not 10 min after the restart. A 77-only hold, so
    its bound is the 10-min ceiling (an unlatched mismatch is spec 04 I83's link-short class: its ladder
    runs at hold start, tests/test_fabric_link_short.py)."""
    order = replay["order"]
    _hold_via_gate(HOLD_77, 0)
    for at in (60, 300):
        await _tick(at)
    _restart_broker(monkeypatch)
    _hold_via_gate(HOLD_77, 480)
    assert srv.device_hold_episode_since == BASE.isoformat(), "the restart restored the hold's start"
    await _tick(BOUND - 1)
    assert "glx_reset" not in order
    await _tick(BOUND)
    assert "glx_reset" in order, "the ladder ran 10 min into the hold, 2 min after the restart"
    assert _cycles(order) == 1 and _releases_on(order) == []


@pytest.mark.asyncio
async def test_lift_and_fail_cycles_never_restart_the_10_min_clock(replay):
    """A hold lifts, the next job fails the same way, the hold returns; over and over. Each re-hold is
    the same episode: the full ladder still runs 10 min after the first hold opened. A 77-only hold, so
    the bound is the 10-min ceiling (a latched hold's one-tick bound, or an unlatched mismatch's spec 04
    I83 at-start ladder, would run the ladder at once)."""
    order = replay["order"]
    _hold_via_gate(HOLD_77, 0)
    for lift, rehold in ((200, 230), (400, 430)):
        await _tick(lift - 1)
        _lift_via_gate(lift)
        _hold_via_gate(HOLD_77, rehold)
    await _tick(BOUND - 1)
    assert "glx_reset" not in order
    await _tick(BOUND + 1)
    assert "glx_reset" in order, "the ladder ran 10 min after the FIRST hold, not after the last re-hold"


@pytest.mark.asyncio
async def test_an_unlatched_mismatch_hold_gets_its_ladder_at_start_and_none_again_across_a_restart(replay, monkeypatch):
    """Spec 04 I83 on the gate path: an unlatched topology-mismatch hold is the fabric link-short class.
    Its first tick runs the full ladder, the failed full check after it (fabric included) asks for the
    power cycle at once; a broker restart keeps the episode's start, and no tick up to the bound runs a
    second power-cycle request. Nothing is released on a failed check."""
    order = replay["order"]
    _hold_via_gate(MISMATCH, 0)
    await _tick(60)
    assert "glx_reset" in order, "the ladder ran at hold start, not at the 10-min mark"
    assert replay["checks"] and not any(replay["checks"]), "the full check after it, fabric included, failed"
    assert _cycles(order) == 1 and order.index("glx_reset") < order.index("power_cycle")
    _restart_broker(monkeypatch)
    _hold_via_gate(MISMATCH, 480)
    assert srv.device_hold_episode_since == BASE.isoformat(), "the restart restored the hold's start"
    for at in (BOUND - 1, BOUND, BOUND + 60):
        await _tick(at)
    assert _cycles(order) == 1, "one power-cycle request per episode"
    assert _releases_on(order) == [], "nothing released on a failed check"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True, reason="pending fix: the fabric-unrunnable fallback lifts a hold with no passing fabric"
)
async def test_a_latched_topology_hold_is_not_lifted_while_the_link_is_still_missing(replay, monkeypatch):
    """The I40 fallback (fabric pass could not run past its bound, eth heartbeat healthy) must not
    lift a hold whose cause is a missing fabric link: only a passing full check, fabric included,
    lifts a hold, and no job is released onto the broken link."""
    srv.health_monitor.topology_hw_fault = MISMATCH
    fsm_dirty(srv, f"{srv._TOPOLOGY_HOLD}: {MISMATCH}", why=srv.FABRIC_RELIFT_WHY)
    srv._note_tenant_gate_verdict(MISMATCH)
    lifted = []

    async def eth_ok():
        return True, "all active-eth cores advancing"

    async def escalate(rung):
        return True

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_ok)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    monkeypatch.setattr(srv, "_fabric_77_fallback_release_monotonic", 0.0)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: lifted.append(k) or True)
    evidence = {"fabric": {"ok": None, "detail": "rc 77"}}
    await srv._fabric_77_fallback(evidence, lambda m: None, escalate, retried=True)
    assert lifted == [], "a hold on a missing link is not lifted without a passing fabric check"


@pytest.mark.asyncio
async def test_a_latched_hold_inside_the_post_cycle_cooldown_cycles_the_moment_it_ends(replay, monkeypatch):
    """The I42-latched hold opens right after a power cycle: its ladder still runs at 10 min, the
    cooldown refuses the cycle (the only wait allowed past the bound) and the cycle fires on the first
    tick after the cooldown ends, once."""
    order, g = replay["order"], replay["g"]
    srv.health_monitor.topology_hw_fault = MISMATCH
    cooldown_end = 1800
    until = (BASE + timedelta(seconds=cooldown_end)).timestamp()
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)

    def governor(action, tenant_active=False, **k):
        if srv.datetime.now().timestamp() < until:
            return g.mechanism._deny_power_cycle(
                {"until": until, "last": until - 1800, "cause": "power_cycle", "interval_sec": 1800, "why": "cooldown"}
            )
        return True, ""

    monkeypatch.setattr(g.mechanism, "auto_recovery_allowed", governor)
    _open_hold(f"{srv._TOPOLOGY_HOLD}: {MISMATCH}", 0)
    await _tick(BOUND)
    assert "glx_reset" in order, "the cooldown does not delay the ladder"
    assert _cycles(order) == 0 and srv.recovery_mechanism.power_cycle_retry, "the cooldown refused it, retry armed"
    for at in range(BOUND + 60, cooldown_end, 60):
        await _tick(at)
    assert _cycles(order) == 0, "no cycle inside the cooldown"
    await _tick(cooldown_end)
    assert _cycles(order) == 1, "the cycle fires on the first tick after the cooldown"
    for at in range(cooldown_end + 60, cooldown_end + 2 * BOUND, 60):
        await _tick(at)
    assert _cycles(order) == 1 and _releases_on(order) == []
