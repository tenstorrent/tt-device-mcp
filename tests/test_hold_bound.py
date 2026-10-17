# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The 10-min hold bound (spec 04 I76).

No hold goes past 10 min without the broker's own full reset ladder; when the full health check
after that ladder (fabric included) still fails, the broker's own code asks for the power cycle.
Only the bounded boot / post-power-cycle cooldown may hold longer, and the cycle fires when it ends.
No latch, no-reset class or cooldown stops it. No test reaches a real reset or power cycle."""

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tests.test_classac_power_cycle_direct import EXPECTED, rig  # noqa: F401 - the fixture
from tests.test_device_safety import _arm_fabric_only_deadline_hold
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED, OUTCOME_TERMINAL, galaxy

BOUND = 600
_REAL_HOST_RUNG = srv._hold_bound_host_rung  # captured at import, before any test stubs it
MISMATCH = "what(): Connection mismatch detected: 1 missing port"


def _since(age_sec: float) -> str:
    return (datetime.now() - timedelta(seconds=age_sec)).isoformat()


async def _drain():
    task = srv._forced_escalation_task
    if task is not None:
        await task


def _watchdog(monkeypatch):
    """The watchdog's own window bookkeeping at the start of an episode."""
    for name, value in (
        ("device_hold_deadline_bucket", 0),
        ("device_hold_escalate_bucket", 0),
        ("device_hold_offbus_escalated", False),
        ("_forced_escalation_task", None),
    ):
        monkeypatch.setattr(srv, name, value)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry_due", lambda *a, **k: False)
    monkeypatch.setattr(srv.offbus_power_cycle, "due_for_watchdog", lambda *a, **k: False)


# Each hold kind the bound covers: (fsm why, hold detail, fabric output, heartbeats, the forced ladder).
_KINDS = {
    "77-only": (srv.FABRIC_RELIFT_WHY, "post-job: fabric check COULD NOT RUN (77)", "", None, "present-forced"),
    "topology mismatch": (srv.FABRIC_RELIFT_WHY, "post-job: fabric unverified", MISMATCH, None, "present-forced"),
    "known fabric fault": (
        srv.FABRIC_RELIFT_WHY,
        "job 9: known fabric fault: 2 jobs in a row",
        "",
        None,
        "present-forced",
    ),
    "off-bus": ("gate_error", "chip 29 off the bus", "", {str(i): 100 for i in range(31)}, "offbus-forced"),
    "generic": ("eth_frozen", "gate/post-job: eth/fabric fault on a present mesh", "", None, "present-forced"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", list(_KINDS))
async def test_every_hold_kind_runs_the_ladder_at_the_bound_then_the_host_rung(monkeypatch, clear_job_state, hold):
    why, detail, output, beats, ladder = _KINDS[hold]
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output, beats=beats, age_min=BOUND / 60 + 0.05
    )
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: hold == "77-only")
    if hold == "known fabric fault":
        monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "known fabric fault: 2 jobs in a row")
    if hold == "off-bus":
        monkeypatch.setattr(srv, "dead_chips", lambda b: [])

    async def failed_ladder(kind, indices, expected, log):
        escalations.append(kind)
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    await srv._force_escalate_stuck_hold()
    assert escalations == [ladder], "the full ladder runs at the bound whatever the hold kind"
    assert "hold_deadline_forced_escalation" in events
    assert events.count("host_rung") == 1, "a ladder that did not heal goes on to the host rung"
    assert events.index("hold_deadline_forced_escalation") < events.index("host_rung")


@pytest.mark.asyncio
async def test_below_the_bound_a_no_reset_hold_keeps_its_fast_path(monkeypatch, clear_job_state):
    """I30 still holds a topology mismatch without a reset below the bound; I76 changes nothing there."""
    escalations, alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=srv.FABRIC_RELIFT_WHY, detail="post-job: fabric unverified", fabric_output=MISMATCH, age_min=9
    )
    await srv._force_escalate_stuck_hold()
    assert escalations == [] and "host_rung" not in events
    assert [a["cause"] for a in alerts] == ["topology mismatch"]


@pytest.mark.asyncio
async def test_a_latched_i42_hold_still_escalates_and_its_latch_is_lifted(monkeypatch, clear_job_state):
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch,
        why=srv.FABRIC_RELIFT_WHY,
        detail="post-job: fabric unverified",
        fabric_output=MISMATCH,
        age_min=10.1,
    )
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "hardware fault: needs an operator")
    monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "", raising=False)
    await srv._force_escalate_stuck_hold()
    assert escalations == ["present-forced"]
    assert "hold_bound_latch_cleared" in events
    assert srv.health_monitor.topology_hw_fault == "", "the check after the ladder gets a real verdict"


@pytest.mark.asyncio
async def test_a_cooldown_denial_arms_the_retry_and_the_host_rung_fires_at_its_end(monkeypatch, clear_job_state):
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    monkeypatch.setattr(srv, "_hold_bound_host_rung", _REAL_HOST_RUNG)  # the governor decides below
    _watchdog(monkeypatch)
    cycles, until = [], datetime.now().timestamp() + 900
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv.recovery_mechanism, "_journal_auto_recovery_denied", lambda *a: None)
    state = {"cooling": True}

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

    async def failed_ladder(kind, indices, expected, log):
        escalations.append(kind)
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    await srv._force_escalate_stuck_hold()
    assert escalations == ["present-forced"] and cycles == []
    assert srv.recovery_mechanism.power_cycle_retry is not None, "the denial arms the retry"
    bound = srv._hold_bound_health()
    assert bound["next_step"] == "power-cycle" and bound["cooldown_until"]

    state["cooling"] = False
    srv._check_hold_deadline(now=datetime.fromtimestamp(until + 1))
    await _drain()
    assert escalations == ["present-forced"], "the cooldown's end goes straight to the host rung"
    assert len(cycles) == 1 and "10-min hold bound" in cycles[0]


@pytest.mark.asyncio
async def test_a_check_still_waiting_on_hugepages_is_bounded_by_the_check_window(monkeypatch, clear_job_state):
    """A full check after the bound's ladder that has no verdict yet (a 77 while the 1G hugepage pool
    fills, or a check that hangs) gets _HOLD_BOUND_CHECK_SEC, then the host rung."""
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "_last_77_was_hugepages", lambda: True)

    async def recovered_ladder(kind, indices, expected, log):
        escalations.append(kind)
        return OUTCOME_RECOVERED  # chips back, but the fabric pass is still owed: the hold stands

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", recovered_ladder)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"] and "host_rung" not in events
    assert srv._recovery_degraded(), "no release without a passing full check"

    since = datetime.fromisoformat(srv.device_hold_episode_since)
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=srv._HOLD_BOUND_CHECK_SEC - 30))
    await _drain()
    assert "host_rung" not in events, "the full check gets its time"
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=srv._HOLD_BOUND_CHECK_SEC + 5))
    await _drain()
    assert srv.device_hold_escalation_kind == "hold_bound_cycle"
    assert events.count("host_rung") == 1 and escalations == ["present-forced"]
    assert srv.device_hold_episode_since == since.isoformat(), "one episode throughout"


@pytest.mark.asyncio
async def test_another_run_inside_the_check_bound_does_not_cut_the_full_check_short(monkeypatch, clear_job_state):
    """A forced run of another kind (a progress retry, an off-bus one-shot) inside the check bound
    after a bound ladder that brought the mesh back neither re-runs the ladder nor fires the host rung."""
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )

    async def recovered_ladder(kind, indices, expected, log):
        escalations.append(kind)
        return OUTCOME_RECOVERED

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", recovered_ladder)
    monkeypatch.setattr(srv, "_last_77_was_hugepages", lambda: True)  # the check has no verdict yet
    monkeypatch.setattr(srv, "device_hold_escalation_kind", "ceiling_window")
    await srv._force_escalate_stuck_hold()
    monkeypatch.setattr(srv, "device_hold_escalation_kind", "progress_retry")
    await srv._force_escalate_stuck_hold()
    assert escalations == ["present-forced"] and "host_rung" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["77-only", "topology mismatch", "known fabric fault", "generic"])
async def test_a_failed_check_after_the_bound_ladder_asks_for_the_host_rung_at_once(monkeypatch, clear_job_state, hold):
    """The ladder brings the mesh back but its full check (fabric included) ends with no pass: the
    host rung is asked in the same run, not 300 s later, so the hold ends well inside 15 min."""
    why, detail, output, beats, _ladder = _KINDS[hold]
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output, beats=beats, age_min=BOUND / 60 + 0.05
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: hold == "77-only")
    if hold == "known fabric fault":
        monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "known fabric fault: 2 jobs in a row")
        monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "known fabric fault", raising=False)
    if hold == "topology mismatch":
        monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "hardware fault: needs an operator")
    ages = []

    async def host_rung(age, log):
        events.append("host_rung")
        ages.append(srv._hold_age_now())

    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)

    async def recovered_ladder(kind, indices, expected, log):
        escalations.append(kind)
        return OUTCOME_RECOVERED  # chips back, the full check gave no pass: the hold stands

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", recovered_ladder)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"], "the ladder runs at the bound"
    assert srv._recovery_degraded(), "no release without a passing full check"
    assert events.index("hold_bound_check_failed") < events.index("host_rung")
    assert events.count("host_rung") == 1 and ages[0] < BOUND + 60 <= 15 * 60


@pytest.mark.asyncio
async def test_a_passing_check_after_the_bound_ladder_asks_for_no_host_rung(monkeypatch, clear_job_state):
    """The ladder brings the mesh back and its full check passes: no hold_bound_check_failed, no host
    rung, then or on a later tick."""
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)

    async def recovered_and_passed(kind, indices, expected, log):
        escalations.append(kind)
        monkeypatch.setattr(srv, "_recovery_degraded", lambda: False)  # the full check passed
        return OUTCOME_RECOVERED

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", recovered_and_passed)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"]
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=10))  # the next sampler tick
    await _drain()
    assert "hold_bound_check_failed" not in events and "host_rung" not in events


@pytest.mark.asyncio
async def test_a_relift_that_fails_after_the_bound_ladder_cycles_on_the_next_tick(monkeypatch, clear_job_state):
    """A check that was still waiting on hugepages after the ladder, then ends with no pass, asks for
    the host rung on the next watchdog tick, not at the end of the check window."""
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)
    pending = {"hugepages": True}
    monkeypatch.setattr(srv, "_last_77_was_hugepages", lambda: pending["hugepages"])

    async def recovered_ladder(kind, indices, expected, log):
        escalations.append(kind)
        return OUTCOME_RECOVERED

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", recovered_ladder)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert "host_rung" not in events and not srv._note_hold_bound_check_failed(lambda m: None)
    pending["hugepages"] = False  # the relift's fabric pass ran and failed
    assert srv._note_hold_bound_check_failed(lambda m: None)
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=10))  # one sampler tick
    await _drain()
    assert srv.device_hold_escalation_kind == "hold_bound_cycle"
    assert events.count("host_rung") == 1 and escalations == ["present-forced"]


def test_health_shows_the_bound(monkeypatch, clear_job_state):
    srv._mark_device_dirty("gate/post-job: eth/fabric fault on a present mesh")
    monkeypatch.setattr(srv, "device_hold_episode_since", _since(120))
    until = datetime.now().timestamp() + 1500
    monkeypatch.setattr(
        srv.recovery_mechanism,
        "power_cycle_cooldown_state",
        {"until": until, "last": until - 1800, "cause": "boot", "interval_sec": 1800, "why": "cooldown"},
    )
    bound = srv._health_payload()["hold_bound"]
    assert bound["bound_sec"] == BOUND and bound["next_step"] == "ladder"
    # The cooldown ends after start + bound, so the escalation waits on it.
    assert bound["deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")
    assert 119 <= bound["held_age_sec"] <= 125
    assert bound["cooldown_until"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")


@pytest.mark.parametrize("hold", list(_KINDS))
def test_every_hold_kind_reports_one_escalation_deadline(monkeypatch, clear_job_state, hold):
    """hold_bound_deadline: start + bound, or the end of a running power-cycle cooldown when later; /health
    and the stuck-hold event and alert all read it, whatever the hold's kind."""
    why, detail, output, beats, _ladder = _KINDS[hold]
    _escalations, alerts, _events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output, beats=beats, age_min=20.1
    )
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: hold == "77-only")
    _watchdog(monkeypatch)
    start = datetime.fromisoformat(srv.device_hold_episode_since)
    assert srv.hold_bound_deadline() == start + timedelta(seconds=BOUND)
    # A cooldown that already ended, or ends before start + bound, does not move it.
    for until in (time.time() - 5, start.timestamp() + 60):
        monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", {"until": until})
        assert srv.hold_bound_deadline() == start + timedelta(seconds=BOUND)
    until = time.time() + 900
    monkeypatch.setattr(
        srv.recovery_mechanism,
        "power_cycle_cooldown_state",
        {"until": until, "last": until - 1800, "cause": "boot", "interval_sec": 1800, "why": "cooldown"},
    )
    assert srv.hold_bound_deadline() == datetime.fromtimestamp(until)
    want = datetime.fromtimestamp(until).isoformat(timespec="seconds")
    assert srv._hold_bound_health()["deadline"] == want
    seen = []
    patch_health_event(monkeypatch, lambda kind, **k: seen.append((kind, k)))
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: False)
    monkeypatch.setattr(srv.alert, "alert_argv", lambda: ["true"])
    monkeypatch.setattr(srv, "_persist_hold_alert_backoff", lambda *a: None)
    monkeypatch.setattr(srv, "device_hold_alert_next_bucket", 0)
    srv._check_hold_deadline()
    stuck = [k for kind, k in seen if kind == "hold_stuck_past_deadline"]
    assert stuck and all(k["escalation_deadline"] == want for k in stuck)
    assert alerts and alerts[0]["escalation_deadline"] == want
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    assert srv.hold_bound_deadline() is None and srv._hold_bound_health() is None


@pytest.mark.parametrize("env, ceiling", [("1200", BOUND), ("86400", BOUND), ("300", 300), ("0", BOUND), ("x", BOUND)])
def test_env_shortens_the_bound_never_lengthens_it(monkeypatch, env, ceiling):
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", env)
    assert galaxy._stuck_hold_ceiling_sec() == ceiling
    assert srv._stuck_hold_ceiling_sec() == ceiling


# ---- broker-level replay: a latched topology-mismatch hold on a Galaxy host -------------------
#
# A Galaxy host, 2026-10-16: a chip dropped during the startup fabric check; two mesh resets brought
# it back, but the fabric then showed a topology mismatch (one missing tray-to-tray port). The first
# job failed on the fabric and latched the I42 topology hardware fault, which made the hold 77-only:
# at 10 min nothing ran, and the stuck-hold event logged escalated=false at 20 and 40 min. The hold
# stood about 70 min until an operator power cycle healed it in about 5 min.


@pytest.mark.asyncio
async def test_replay_a_latched_topology_mismatch_hold_gets_the_ladder_then_the_power_cycle(
    rig, monkeypatch, clear_job_state  # noqa: F811
):
    order, g = rig["order"], rig["g"]
    rig["beats"] = {str(i): 100 for i in range(EXPECTED)}  # every chip present with a live ARC
    fsm_dirty(srv, "post-job: fabric check COULD NOT RUN (77)", why=srv.FABRIC_RELIFT_WHY)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in range(EXPECTED)])
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: EXPECTED)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", MISMATCH)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "")
    monkeypatch.setattr(
        srv.health_monitor,
        "topology_hw_fault",
        "hardware fault: the first job after a topology-mismatch lift failed on the fabric; needs an operator",
    )
    monkeypatch.setattr(srv, "device_hold_episode_since", _since(BOUND + 1))
    monkeypatch.setattr(srv, "device_hold_episode_reason", "post-job: fabric check COULD NOT RUN (77)")
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    _watchdog(monkeypatch)
    assert srv._fabric_77_only_hold(), "the latch made the hold 77-only: on the old code nothing runs"
    checks = []

    async def reset_then_full_check(indices, log):
        # The mesh reset exits 0; the full health check after it, fabric included, finds the same mismatch.
        order.append("glx_reset")
        ok, _ = await g._verify_device(EXPECTED, log, run_fabric=True)
        checks.append(("full", ok))
        return False

    async def full_check(expected, log, run_fabric=True, **k):
        assert run_fabric, "the post-reset check includes the fabric (goal 8)"
        return False, {"fabric": {"ok": False, "detail": MISMATCH}}

    monkeypatch.setattr(g, "_reset_and_verify_device", reset_then_full_check)
    monkeypatch.setattr(g, "_verify_device", full_check)
    rig_cycle = srv._auto_power_cycle_host

    async def cycle_and_mark(log, reason):
        # As the real one: a launched request is marked pending, so nothing asks for a second cycle.
        await rig_cycle(log, reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle_and_mark)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    assert "glx_reset" in order, "the full ladder ran at the 10-min mark"
    assert checks and all(not ok for _, ok in checks), "the full check after the ladder still failed"
    assert order.count("power_cycle") == 1, "the broker's own code asked for the power cycle"
    assert order.index("glx_reset") < order.index("power_cycle")
    assert "released" not in order, "nothing released on a failed check"
    kinds = [k for k, _ in rig["events"]]
    assert "hold_bound_latch_cleared" in kinds
    assert srv._hold_escalated_report(), "a stuck-hold event past the bound reports the escalation"


# ---- broker-level replay: the latch set by the first job after a topology-mismatch lift ----------
#
# A Galaxy host, 2026-10-16: after an off-bus recovery the startup fabric check passed with a topology
# mismatch (1 port missing) and the broker released the device. The next job failed on the fabric,
# which set the I42 latch ("needs an operator (cabling) and a broker restart"); every later fabric
# check was withheld. The tenant-hold bound logged "forced recovery escalation started" at 20 and 40
# min, but the forced run returned at its 77-only early exit: no rung and no power cycle ran until an
# operator reset at about 50 min.


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["watchdog", "tenant_hold_bound"])
async def test_replay_the_lift_then_job_fabric_failure_latch_gets_the_ladder_check_and_power_cycle(
    rig, monkeypatch, clear_job_state, driver  # noqa: F811
):
    order, g = rig["order"], rig["g"]
    mon = srv.health_monitor
    rig["beats"] = {str(i): 100 for i in range(EXPECTED)}
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in range(EXPECTED)])
    monkeypatch.setattr(mon, "expected", lambda n: EXPECTED)
    monkeypatch.setattr(mon, "known_fabric_fault", lambda: "")
    monkeypatch.setattr(mon, "last_fabric_output", MISMATCH)
    monkeypatch.setattr(mon, "last_fabric_ok", None)
    monkeypatch.setattr(mon, "topology_hw_fault", "")
    monkeypatch.setattr(mon, "topology_hw_fault_paged", 0.0)
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    # The mismatch lift arms the one-job watch; that job dies on the fabric and sets the latch.
    monkeypatch.setattr(mon, "topology_lift_pending", True)
    why = mon.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert why and "needs an operator (cabling)" in why
    srv._hold_device_fabric_unverified(f"job 922: {why}")
    withheld, detail = await mon._verify_fabric_health_body(1)
    assert withheld is None and "withheld" in detail, "the latch withholds every later fabric check"
    assert srv._fabric_77_only_hold(), "a 77-only hold: the old forced run returned here, running no rung"
    age = BOUND + 1 if driver == "watchdog" else 2 * BOUND + 1
    monkeypatch.setattr(srv, "device_hold_episode_since", _since(age))
    monkeypatch.setattr(srv, "device_hold_episode_reason", f"job 922: {why}")
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "device_hold_job_bound_escalated", False)
    checks = []

    async def reset_then_full_check(indices, log):
        order.append("glx_reset")
        ok, _ = await g._verify_device(EXPECTED, log, run_fabric=True)
        checks.append(ok)
        return False

    async def full_check(expected, log, run_fabric=True, **k):
        assert run_fabric, "the post-reset check includes the fabric (goal 8)"
        assert not mon.topology_hw_fault, "the latch is lifted, so the fabric pass really runs"
        return False, {"fabric": {"ok": False, "detail": MISMATCH}}

    monkeypatch.setattr(g, "_reset_and_verify_device", reset_then_full_check)
    monkeypatch.setattr(g, "_verify_device", full_check)
    rig_cycle = srv._auto_power_cycle_host

    async def cycle_and_mark(log, reason):
        await rig_cycle(log, reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle_and_mark)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)

    if driver == "watchdog":
        srv._check_hold_deadline(now=datetime.now())
    else:

        class _Job:
            id, owner = "923", "tenant"

        said = srv._escalate_held_job_past_bound(_Job(), age, 2 * BOUND, "device held")
        assert said == "forced recovery escalation started"
    await _drain()

    assert "glx_reset" in order, "the full ladder ran"
    assert checks == [False], "the full check after the ladder, fabric included, still failed"
    assert order.count("power_cycle") == 1, "the broker's own code asked for the power cycle"
    assert order.index("glx_reset") < order.index("power_cycle")
    assert "released" not in order, "nothing released on a failed check"
    assert ("hold_bound_latch_cleared", "topology_hw_fault") in [(k, e.get("latch")) for k, e in rig["events"]]


@pytest.mark.asyncio
async def test_a_stuck_hold_event_past_the_bound_never_reads_escalated_false(monkeypatch, clear_job_state):
    """The deadline event reports the bound's ladder: on the old code it said escalated=false while the
    I42 latch kept every rung off."""
    escalations, _alerts, _events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    seen = []
    patch_health_event(monkeypatch, lambda kind, **k: seen.append((kind, k)))
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: True)
    srv.fsm.set_latch("escalated", False)
    await srv._force_escalate_stuck_hold()
    assert escalations == ["present-forced"]
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=srv._hold_deadline_sec()))
    stuck = [k for kind, k in seen if kind == "hold_stuck_past_deadline"]
    assert stuck and all(k["escalated"] for k in stuck)
    await asyncio.sleep(0)
