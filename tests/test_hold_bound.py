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
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.core import HealthState
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED, OUTCOME_TERMINAL, galaxy
from tt_device_mcp.job_reap import ReapedLeftover

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

    async def failed_ladder(kind, indices, expected, log, **_):
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
async def test_a_latched_i42_hold_still_escalates_and_its_latch_stands(monkeypatch, clear_job_state):
    """The I42 latch withholds no check: it makes a mismatch FAIL, so the bound keeps it and the check
    after the ladder cannot pass on the links the lift's first job failed on."""
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch,
        why=srv.FABRIC_RELIFT_WHY,
        detail="post-job: fabric unverified",
        fabric_output=MISMATCH,
        age_min=10.1,
    )
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "hardware fault: a mismatch now fails")
    monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "", raising=False)
    await srv._force_escalate_stuck_hold()
    assert escalations == ["present-forced"]
    assert "hold_bound_latch_cleared" not in events
    assert srv.health_monitor.topology_hw_fault, "the latch stands, so a mismatch after the ladder fails"


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

    async def failed_ladder(kind, indices, expected, log, **_):
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

    async def recovered_ladder(kind, indices, expected, log, **_):
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

    async def recovered_ladder(kind, indices, expected, log, **_):
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

    async def recovered_ladder(kind, indices, expected, log, **_):
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

    async def recovered_and_passed(kind, indices, expected, log, **_):
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

    async def recovered_ladder(kind, indices, expected, log, **_):
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
    assert "hold_bound_latch_cleared" not in kinds, "the I42 latch stands: a mismatch after the ladder fails"
    assert srv.health_monitor.topology_hw_fault
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
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    # The mismatch lift arms the one-job watch; that job dies on the fabric and sets the latch.
    monkeypatch.setattr(mon, "topology_lift_pending", True)
    why = mon.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert why and "a reset and then a power cycle run" in why
    srv._hold_device_fabric_unverified(f"job 922: {why}")
    assert srv._fabric_no_reset_hold() == "", "a latched hold is measured (03 I42): never a no-reset hold"
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
        assert mon.topology_hw_fault, "the latch stands through the bound: a mismatch here fails"
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
    assert ("hold_bound_latch_cleared", "topology_hw_fault") not in [(k, e.get("latch")) for k, e in rig["events"]]


@pytest.mark.asyncio
async def test_replay_a_post_job_77_hold_that_only_relifts_gets_the_ladder_at_the_bound_then_the_power_cycle(
    rig, monkeypatch, clear_job_state  # noqa: F811
):
    """A Galaxy host's post-job hold: a job's fabric failure after a topology lift sets the I42 latch, the
    hold is 77-only, and for its whole life no idle relift reset ran (the old latch withheld each one). The
    stuck-hold event at the deadline read escalated=false and no rung had run. Walked tick by tick:
    nothing is forced below the bound; at 600 s the full ladder runs, the full check after it (fabric
    included) fails, the broker asks for the power cycle, and the deadline event reads escalated."""
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
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    monkeypatch.setattr(mon, "topology_lift_pending", True)
    why = mon.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert why, "the job's fabric failure after the lift sets the latch"
    srv._hold_device_fabric_unverified(f"job 930: {why}")
    assert srv._fabric_no_reset_hold() == "", "a latched hold is measured (03 I42): never a no-reset hold"
    monkeypatch.setattr(srv, "device_hold_episode_reason", f"job 930: {why}")
    _watchdog(monkeypatch)
    srv.fsm.set_latch("escalated", False)
    seen = []
    patch_health_event(monkeypatch, lambda kind, **k: seen.append((kind, k)))
    checks = []

    async def reset_then_full_check(indices, log):
        order.append("glx_reset")
        ok, _ = await g._verify_device(EXPECTED, log, run_fabric=True)
        checks.append(ok)
        return False

    async def full_check(expected, log, run_fabric=True, **k):
        assert run_fabric, "the post-reset check includes the fabric (goal 8)"
        assert mon.topology_hw_fault, "the latch stands through the bound: a mismatch here fails"
        return False, {"fabric": {"ok": False, "detail": MISMATCH}}

    monkeypatch.setattr(g, "_reset_and_verify_device", reset_then_full_check)
    monkeypatch.setattr(g, "_verify_device", full_check)
    rig_cycle = srv._auto_power_cycle_host

    async def cycle_and_mark(log, reason):
        await rig_cycle(log, reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle_and_mark)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)

    for age in range(10, BOUND, 10):  # one watchdog tick every 10 s, no idle relift reset in between
        monkeypatch.setattr(srv, "device_hold_episode_since", _since(age))
        srv._check_hold_deadline(now=datetime.now())
        await _drain()
        assert "glx_reset" not in order and "power_cycle" not in order, f"a rung ran at {age}s, below the bound"

    monkeypatch.setattr(srv, "device_hold_episode_since", _since(BOUND))
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert "glx_reset" in order, "the full ladder ran at the bound"
    assert checks == [False], "the full check after the ladder, fabric included, failed"
    assert order.count("power_cycle") == 1, "then the broker's own code asked for the power cycle"
    assert order.index("glx_reset") < order.index("power_cycle")
    assert "released" not in order, "nothing released on a failed check"

    monkeypatch.setattr(srv, "device_hold_episode_since", _since(srv._hold_deadline_sec() + 1))
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    stuck = [k for kind, k in seen if kind == "hold_stuck_past_deadline"]
    assert stuck and all(k["escalated"] for k in stuck), stuck
    assert order.count("power_cycle") == 1, "the requested cycle is not asked for twice"


@pytest.mark.asyncio
async def test_replay_post_reset_lift_latch_with_gated_queued_job_runs_the_ladder_at_the_bound(
    rig, monkeypatch, clear_job_state  # noqa: F811
):
    """The same latch reached from an off-bus hold, with a tenant job parked at the door behind it.

    An off-bus hold is healed by a mesh reset; the post-reset check passes with a topology mismatch and
    lifts. The first job fails on the fabric and sets the I42 latch, which opens a new hold episode at
    that job's end. The next tenant job is dequeued and held at the CLEAN-GATE (it opens no device fd).
    At the bound the watchdog runs the ladder with the latch standing, so the full check fails on the
    mismatch and the power cycle is asked for once. The gated job never dispatches, and its own bound
    refuses it."""
    order, g = rig["order"], rig["g"]
    mon = srv.health_monitor
    rig["beats"] = {str(i): 100 for i in range(EXPECTED)}
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in range(EXPECTED)])
    monkeypatch.setattr(mon, "expected", lambda n: EXPECTED)
    monkeypatch.setattr(mon, "known_fabric_fault", lambda: "")
    monkeypatch.setattr(mon, "last_fabric_ok", None)
    monkeypatch.setattr(mon, "topology_hw_fault", "")
    monkeypatch.setattr(mon, "topology_lift_pending", False)
    monkeypatch.setattr(mon, "_topology_alerted", set())
    monkeypatch.setattr(monitor_mod, "_asic_bus_map", lambda: {})  # no chip-to-bus map off the host
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    monkeypatch.setattr(srv, "_hold_episode_restorable", False)
    real_datetime, offset = srv.datetime, [0.0]

    class _Clock(real_datetime):  # the broker's wall clock, moved on by ``offset`` seconds
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=offset[0])

    monkeypatch.setattr(srv, "datetime", _Clock)

    # 1-2. The off-bus hold heals on a mesh reset; the post-reset check passes with a topology mismatch.
    fsm_dirty(srv, "chip 29 off the bus", why="gate_error")
    order.append("glx_reset")
    monkeypatch.setattr(mon, "last_fabric_output", MISMATCH)
    mon._on_topology_mismatch(MISMATCH)
    srv.fsm.on_readings(HealthState(phase="test", at=datetime.now(), expected=0))
    assert srv.fsm.state is ServerState.HEALTHY and mon.topology_lift_pending, "a lift onto the mismatch"

    # 3. The first job fails on the fabric and sets the I42 latch.
    why = mon.note_job_after_topology_lift(True, "... could not find any forwarding direction ...")
    assert why and mon.topology_hw_fault
    srv._hold_device_fabric_unverified(f"job 922: {why}")
    assert not srv._fabric_77_only_hold() and mon.last_fabric_ok is False, "the latch measured the fabric bad"
    assert srv._fabric_no_reset_hold() == "", "a latched hold is measured (03 I42): never a no-reset hold"
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "device_hold_job_bound_escalated", False)

    # 4. The next tenant job is dequeued and parked at the CLEAN-GATE (its refusal opens the new hold
    # episode, at job 922's end); it holds no device fd.
    assert not srv.galaxy_recovery.tenant_holds(), "the gated job opens no /dev/tenstorrent"
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 60.0)
    monkeypatch.setattr(srv, "TENANT_HOLD_WAKE_MIN_GAP_SEC", 0.0)
    gate_passes = []

    async def gate(job, job_log_file):
        gate_passes.append(srv.fsm.state)
        return "" if srv.fsm.state is ServerState.HEALTHY else f"device held: job 922: {why}"

    monkeypatch.setattr(srv, "_admission_verdict", gate)
    job = srv.Job(id="923", owner="tenant", workspace="/tmp", command="true", queued_at="t")
    srv.jobs[job.id] = job
    hold = asyncio.create_task(srv._hold_job_until_device_fit(job, None, f"device held: job 922: {why}"))
    await asyncio.sleep(0.05)
    assert not hold.done(), "inside its own bound the gated job waits at the door"
    assert srv.device_hold_episode_since, "the gated job's refusal opened the hold episode"

    checks = []

    async def reset_then_full_check(indices, log):
        order.append("glx_reset")
        ok, _ = await g._verify_device(EXPECTED, log, run_fabric=True)
        checks.append(ok)
        return False

    async def full_check(expected, log, run_fabric=True, **k):
        checks.append(("run_fabric", run_fabric))
        assert mon.topology_hw_fault, "the latch stands through the bound: a mismatch here fails"
        return False, {"fabric": {"ok": False, "detail": MISMATCH}}

    monkeypatch.setattr(g, "_reset_and_verify_device", reset_then_full_check)
    monkeypatch.setattr(g, "_verify_device", full_check)
    rig_cycle = srv._auto_power_cycle_host

    async def cycle_and_mark(log, reason):
        await rig_cycle(log, reason)
        srv.recovery_mechanism.host_action_requested = ("power-cycle", time.monotonic())

    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle_and_mark)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)

    # BOUND+1: the watchdog runs the ladder with the latch standing; the full check fails; one power cycle.
    before = len(order)
    offset[0] = BOUND + 1
    srv._check_hold_deadline()
    await _drain()
    bound_run = order[before:]
    assert mon.topology_hw_fault, "the I42 latch stands through the bound"
    assert ("hold_bound_latch_cleared", "topology_hw_fault") not in [(k, e.get("latch")) for k, e in rig["events"]]
    assert "glx_reset" in bound_run, "the full ladder ran at the 10-min mark"
    assert ("run_fabric", True) in checks and False in checks, "the full check, fabric included, failed"
    assert bound_run.count("power_cycle") == 1, "the broker's own code asked for the power cycle"
    assert bound_run.index("glx_reset") < bound_run.index("power_cycle")
    assert "released" not in order and srv.fsm.state is not ServerState.HEALTHY
    assert not hold.done() and job.status is srv.JobStatus.QUEUED, "the gated job is never dispatched"

    # 2*BOUND: the gated job's own bound refuses it at the door, and asks for no second cycle.
    offset[0] = 2 * BOUND + 1
    srv._wake_held_jobs()
    refused = await asyncio.wait_for(hold, timeout=5.0)
    await _drain()
    assert refused and f"past the {2 * BOUND}s hold bound" in refused, refused
    assert job.status is srv.JobStatus.QUEUED, "refused at the door: the caller fails it, never dispatches it"
    assert ServerState.HEALTHY not in gate_passes, "no gate pass ever found the device fit"
    held = [e for k, e in rig["events"] if k == "tenant_hold_bound_exceeded"]
    assert len(held) == 1 and held[0]["job_id"] == "923" and held[0]["held_sec"] >= 2 * BOUND
    assert order.count("power_cycle") == 1, "the tenant-hold bound asks for no second power cycle"
    assert "released" not in order


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


def _present_hold_at_the_bound(r, monkeypatch, *, why, detail):
    """A present-mesh hold of ``why``/``detail`` one second past the bound, on the rig's real ladder."""
    r["beats"] = {str(i): 100 for i in range(EXPECTED)}  # every chip present with a live ARC
    fsm_dirty(srv, detail, why=why)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in range(EXPECTED)])
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: EXPECTED)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", "")
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "")
    since = _since(BOUND + 1)
    monkeypatch.setattr(srv, "device_hold_episode_since", since)
    monkeypatch.setattr(srv, "device_hold_episode_reason", detail)
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    _watchdog(monkeypatch)
    return since


@pytest.mark.asyncio
async def test_replay_a_77_held_with_no_host_rung_gets_the_power_cycle_at_the_bound(
    rig, monkeypatch, clear_job_state  # noqa: F811
):
    """galaxy's _hold_77_without_host_rung (spec 04 I29): the episode's reset came back with the fabric
    unverified (77) and its last-chance sweep, the full check after it, read a 77 again. Below the
    bound that holds with no host rung; at the bound the watchdog's forced ladder reaches the same exit
    and the broker's own code asks for the power cycle in that run."""
    order, g = rig["order"], rig["g"]
    detail = "post-job: fabric check COULD NOT RUN (77)"
    since = _present_hold_at_the_bound(rig, monkeypatch, why=srv.FABRIC_RELIFT_WHY, detail=detail)
    # The ladder this episode already climbed: a reset that left the fabric unverified, then the sweep.
    monkeypatch.setattr(g.mechanism, "unverified_reset_since", since)
    monkeypatch.setattr(g.mechanism, "unverified_sweep_since", since)
    monkeypatch.setattr(g.mechanism, "unverified_sweep_77_since", since)
    monkeypatch.setattr(g, "_host_rung_77_paged_since", None)
    assert srv._fabric_77_only_hold() and srv._fabric_no_reset_hold() == "fabric check could not run (77)"

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    kinds = [k for k, _ in rig["events"]]
    assert "stuck_hold_escalation" in kinds, "the forced ladder ran at the bound"
    assert "fabric_unverified_no_host_rung" in kinds, "it reached the 77 no-host-rung exit"
    assert order.count("power_cycle") == 1, "the bound asked for the power cycle past that exit"
    assert kinds.index("fabric_unverified_no_host_rung") < kinds.index("hold_bound_host_rung")
    assert "released" not in order


def _leftover_scan(monkeypatch, r, *, tenant: bool):
    """A reaped job's process stuck in the kernel still holding /dev/tenstorrent (spec 01 I16), and,
    with ``tenant``, a live user process beside it."""
    stuck = DeviceHolder(pid=4242, uid=1001, scoped=True)
    holders = [stuck] + ([DeviceHolder(pid=5151, uid=1002)] if tenant else [])
    r["scan"] = HolderScan(holders=holders, complete=True)
    leftover = ReapedLeftover("7", 4242, 1001, "D", "tt_ioctl", True)
    monkeypatch.setattr(
        srv, "find_reaped_leftovers", lambda hs, live, survivors=None: [leftover] if stuck in list(hs) else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", [False, True])
async def test_a_killed_jobs_leftover_holder_does_not_stop_the_bound(
    rig, monkeypatch, clear_job_state, tenant  # noqa: F811
):
    """A killed job whose process is stuck in the kernel keeps its device fds (spec 01 I16, after the
    I67 release wait). Every reset is fenced while it holds, and it read as a tenant, so the bound's
    ladder stood down and re-armed forever. At the bound it is no tenant: the ladder's resets cannot
    run, so the broker asks for the host rung, which is what clears it. A live tenant still refuses."""
    order = rig["order"]
    _present_hold_at_the_bound(rig, monkeypatch, why="job_killed", detail="job 7 killed: device left dirty")
    _leftover_scan(monkeypatch, rig, tenant=tenant)
    assert srv.galaxy_recovery.tenant_holds(), "the leftover reads as a tenant to the ladder"
    monkeypatch.setattr(  # the real governor's first rule: a tenant refuses a host rung
        rig["g"].mechanism,
        "auto_recovery_allowed",
        lambda action, tenant_active=False, **k: (False, "a tenant holds the device") if tenant_active else (True, ""),
    )

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    kinds = [k for k, _ in rig["events"]]
    assert "glx_reset" not in order, "no reset runs over a leftover (01 I16)"
    if tenant:
        assert "power_cycle" not in order, "a live tenant still refuses the host rung"
        assert kinds.count("hold_bound_tenant_blocked") == 1, "the tenant-blocked hold is paged, naming it"
        assert dict(rig["events"])["hold_bound_tenant_blocked"]["pid"] == 5151
        assert srv._hold_bound_health()["next_step"] == "tenant_blocked"
        _leftover_scan(monkeypatch, rig, tenant=False)  # the tenant leaves; the leftover stays
        _tick()
        await _drain()
        assert order.count("power_cycle") == 1, "the first tick after the tenant leaves asks for the host rung"
        assert [k for k, _ in rig["events"]].count("hold_bound_tenant_blocked") == 1
        return
    assert "hold_bound_leftover" in kinds
    assert order.count("power_cycle") == 1, "the bound asked for the host rung that clears the leftover"
    assert "released" not in order


def _real_host_rung(monkeypatch, cycles, allowed=lambda action: (True, "")):
    """The real I76 host rung over a stub governor (``allowed``) and a recorded power cycle."""
    monkeypatch.setattr(srv, "_hold_bound_host_rung", _REAL_HOST_RUNG)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv.recovery_mechanism, "_journal_auto_recovery_denied", lambda *a: cycles.append("denied"))
    monkeypatch.setattr(
        srv.recovery_mechanism, "auto_recovery_allowed", lambda action, tenant_active=False, **k: allowed(action)
    )

    async def power_cycle(log, reason):
        cycles.append("power_cycle")

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)


def _tick(seconds=10):
    srv._check_hold_deadline(now=datetime.now() + timedelta(seconds=seconds))


@pytest.mark.asyncio
async def test_a_ladder_below_the_bound_does_not_stand_in_for_the_bounds_ladder(monkeypatch, clear_job_state):
    """An off-bus one-shot (or any forced run) at 2 min is not the bound's ladder: at the 10-min mark the
    bound runs the full ladder again, and only then asks for the host rung."""
    why, detail, _output, beats, ladder = _KINDS["off-bus"]
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, beats=beats, age_min=BOUND / 60 + 0.05
    )
    monkeypatch.setattr(srv, "dead_chips", lambda b: [])

    async def failed_ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    real_age = srv._hold_age_now
    monkeypatch.setattr(srv, "_hold_age_now", lambda now=None: 120.0)  # the one-shot, 2 min into the hold
    monkeypatch.setattr(srv, "device_hold_escalation_kind", "offbus_one_shot")
    await srv._force_escalate_stuck_hold()
    assert escalations == [ladder] and "host_rung" not in events
    monkeypatch.setattr(srv, "_hold_age_now", real_age)
    assert srv._hold_bound_health()["next_step"] == "ladder"
    srv.device_hold_escalation_kind = "ceiling_window"  # the window the watchdog claims at the bound
    await srv._force_escalate_stuck_hold()
    assert escalations == [ladder, ladder], "the bound runs its own ladder at the 10-min mark"
    assert events.count("host_rung") == 1
    assert events.index("hold_deadline_forced_escalation", 1) < events.index("host_rung")


@pytest.mark.asyncio
async def test_a_host_rung_deferred_by_a_reset_scope_fires_on_the_first_tick_after_it(monkeypatch, clear_job_state):
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)
    cycles, scope = [], {"active": None}
    _real_host_rung(monkeypatch, cycles)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: scope["active"])

    async def failed_ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        scope["active"] = "a reset still cycling"
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"] and cycles == []
    assert srv._hold_bound_cycle_age is None, "a deferred host rung is not one that was asked"
    _tick()
    await _drain()
    assert cycles == [], "still in its scope"
    scope["active"] = None
    _tick(20)
    await _drain()
    assert cycles == ["power_cycle"], "the first tick after the scope ends asks for the host rung"
    assert escalations == ["present-forced"]


@pytest.mark.asyncio
async def test_a_bound_ladder_deferred_by_a_reset_scope_runs_on_the_first_tick_after_it(monkeypatch, clear_job_state):
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)
    scope = {"active": "a reset still cycling"}
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: scope["active"])
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and srv.device_hold_escalate_bucket == 0, "the window is given back"
    scope["active"] = None
    _tick()
    await _drain()
    assert escalations == ["present-forced"], "the bound's ladder runs on the first tick after the scope"


@pytest.mark.asyncio
@pytest.mark.parametrize("chip_back", [False, True])
async def test_an_offbus_one_shot_past_the_bound_deferred_by_a_reset_scope_runs_on_the_first_tick_after_it(
    monkeypatch, clear_job_state, chip_back
):
    """A one-shot that fires past the bound (given back earlier, e.g. to a tenant) and meets a reset
    still cycling ran nothing: it gives back its claim and the window it took with it, so the bound's
    ladder runs on the first tick after the scope, whether the chip is still off or came back."""
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "isolated_chips", {"3"})
    monkeypatch.setattr(srv, "device_hold_offbus_tenant_wait", True)  # a tenant stood the early one-shot down
    monkeypatch.setattr(srv, "device_hold_offbus_prior_bucket", None)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
    scope = {"active": "a reset still cycling"}
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: scope["active"])
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [], "the scope ran nothing"
    assert not srv.device_hold_offbus_escalated and srv.device_hold_escalate_bucket == 0, "the claim is given back"
    scope["active"] = None
    if chip_back:
        monkeypatch.setattr(srv, "isolated_chips", set())
    _tick()
    await _drain()
    assert escalations == ["present-forced"], "the bound's ladder runs on the first tick after the scope"


@pytest.mark.asyncio
async def test_a_host_rung_denied_with_no_retry_is_asked_again_each_tick_and_journaled_once(
    monkeypatch, clear_job_state
):
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)
    cycles, gov = [], {"ok": False}
    _real_host_rung(monkeypatch, cycles, lambda action: (True, "") if gov["ok"] else (False, "ledger unreadable"))
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)

    async def failed_ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    _tick()
    await _drain()
    assert cycles == ["denied"], "the same denial is journaled once"
    gov["ok"] = True
    _tick(20)
    await _drain()
    assert cycles == ["denied", "power_cycle"] and escalations == ["present-forced"]


@pytest.mark.asyncio
async def test_a_live_tenant_at_the_bound_is_paged_once_and_the_ladder_runs_when_it_leaves(
    monkeypatch, clear_job_state
):
    """Past the bound a live tenant still keeps every reset and the host rung off the device (no reset over
    a tenant). That hold is the one past the bound with no deadline of its own, so it is not silent: one
    page per episode naming the blocker, and /health shows it. The first tick after it leaves runs the ladder."""
    escalations, alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "device_hold_tenant_deferred", False)
    tenant = {"on": True}
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: tenant["on"])
    monkeypatch.setattr(
        srv,
        "enumerate_device_holders",
        lambda: HolderScan(holders=[DeviceHolder(pid=5151, uid=1002)] if tenant["on"] else [], complete=True),
    )
    monkeypatch.setattr(galaxy, "is_tenant", lambda h: True)
    monkeypatch.setattr(galaxy, "discount_broker_internal", lambda scan: scan)
    monkeypatch.setattr(galaxy, "holder_comm", lambda pid: "python3")

    async def ladder(kind, indices, expected, log, **_):
        if tenant["on"]:
            srv.galaxy_recovery.tenant_deferred = True
            return galaxy.OUTCOME_WAITING
        srv.galaxy_recovery.tenant_deferred = False
        escalations.append(kind)
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    _tick()
    await _drain()
    pages = [a for a in alerts if a["kind"] == "hold_bound_tenant_blocked"]
    assert len(pages) == 1 and pages[0]["pid"] == 5151 and "5151" in pages[0]["blocked_by"]
    assert events.count("hold_bound_tenant_blocked") == 1
    health = srv._hold_bound_health()
    assert health["next_step"] == "tenant_blocked" and "5151" in health["blocked_by"]
    assert escalations == [] and "host_rung" not in events
    tenant["on"] = False
    _tick(20)
    await _drain()
    assert escalations == ["present-forced"] and events.count("host_rung") == 1
    assert srv._hold_bound_health()["next_step"] != "tenant_blocked"


@pytest.mark.asyncio
async def test_the_operator_kill_switch_does_not_stop_the_bound(monkeypatch, clear_job_state):
    """TT_DEVICE_MCP_FORCE_ESCALATE=0 governs only forced runs below the bound."""
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=9
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "isolated_chips", {"3"})  # the off-bus one-shot is due below the bound
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [], "below the bound the kill-switch still holds"
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "device_hold_episode_since", _since(BOUND + 5))
    srv._check_hold_deadline()
    await _drain()
    assert escalations == ["present-forced"], "at the bound the ladder runs whatever the kill-switch says"


@pytest.mark.parametrize("past_bound", [False, True])
def test_a_held_job_past_its_bound_under_the_kill_switch_says_the_bound_still_runs(
    monkeypatch, clear_job_state, past_bound
):
    """TT_DEVICE_MCP_FORCE_ESCALATE=0 governs only runs below the bound: a held job's note never says the
    device stays held with no exit, and past the bound its escalation starts as the watchdog's would."""
    _arm_fabric_only_deadline_hold(
        monkeypatch,
        why="eth_frozen",
        detail="gate/post-job: eth/fabric fault on a present mesh",
        age_min=BOUND / 60 + (0.1 if past_bound else -1),
    )
    _watchdog(monkeypatch)
    monkeypatch.setattr(srv, "device_hold_job_bound_escalated", False)
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    spawned = []
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: spawned.append(1) or True)

    class _Job:
        id, owner = "7", "tenant"

    said = srv._escalate_held_job_past_bound(_Job(), 2 * BOUND, 2 * BOUND, "device held")
    assert "stays held" not in said
    if past_bound:
        assert said == "forced recovery escalation started" and spawned == [1]
    else:
        assert spawned == [] and "10-min hold bound" in said
