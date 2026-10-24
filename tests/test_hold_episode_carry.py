# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The hold episode carries across a quick release (spec 04 I82).

The 10-min clock of the hold bound (I76) counts from the first moment the device stopped being healthy
in the episode. A release that a new hold follows within _HOLD_CARRY_SEC, or around the first job started
in that window, does not restart it, nor does a latch lift, a failed first job, a class change or a broker
restart. A bound ladder whose check released a device that is held again did not heal it, so the next
bound step is the host rung, not another ladder. No test reaches a real reset or power cycle."""

import dataclasses
import json
import time
from datetime import datetime, timedelta

import pytest

from tests.test_device_safety import (
    _arm_fabric_only_deadline_hold,
    _free_device_lock,
    _no_workspace_activation,
    _quiet_post_job_gate,
    _run_one,
)
from tests.test_hold_bound import _REAL_HOST_RUNG, BOUND, MISMATCH, _drain, _watchdog
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import evidence as health
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED

# Hold classes whose bound ladder can end in a release on a device that still has the fault:
# (fsm why, hold detail, fabric output, known-fault latch).
_CLASSES = {
    "missing port (topology mismatch)": (srv.FABRIC_RELIFT_WHY, "post-job: fabric unverified", MISMATCH, ""),
    "latched I42 topology hardware fault": (
        srv.FABRIC_RELIFT_WHY,
        "job 7: fabric topology hardware fault after a topology-mismatch lift",
        MISMATCH,
        "",
    ),
    "missing cable (known fabric fault)": (
        srv.FABRIC_RELIFT_WHY,
        "fabric HARDWARE FAULT: a cable is missing",
        "",
        "known fabric fault: a cable is missing",
    ),
    "77-only": (srv.FABRIC_RELIFT_WHY, "post-job: fabric check COULD NOT RUN (77)", "", ""),
    "generic": ("eth_frozen", "gate/post-job: eth/fabric fault on a present mesh", "", ""),
}


@pytest.fixture(autouse=True)
def _restore_broker_state(monkeypatch):
    """The broker keeps its hold state in module globals: put every one back after each test."""
    for name in list(vars(srv)):
        if name.startswith(("device_hold_", "_hold_")) and not callable(getattr(srv, name)):
            monkeypatch.setattr(srv, name, getattr(srv, name))


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-a")


def _age() -> float:
    return srv._hold_age_now()


def _open_hold(monkeypatch, hold, age_sec):
    """A hold of class ``hold`` opened through the gate verdict, its episode ``age_sec`` old."""
    why, detail, output, known = _CLASSES[hold]
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output
    )
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: known)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    srv._note_tenant_gate_verdict(detail)
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=age_sec)).isoformat()
    srv._persist_hold_episode(srv.device_hold_episode_since)
    return escalations, events, detail


async def _bound_ladder_releases(monkeypatch, escalations, events):
    """The bound's ladder ran and its check passed (on the discovered links), so the device was released."""
    srv._note_hold_bound_ladder(_age(), OUTCOME_RECOVERED)
    assert "host_rung" not in events
    srv._note_tenant_gate_verdict("")  # its check passed: released
    assert "device_released" in events and not srv.device_hold_episode_since


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", list(_CLASSES))
async def test_replay_a_release_after_the_bound_ladder_then_a_failed_first_job_goes_to_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state, hold
):
    """A Galaxy host, 2026-10-17: a missing-port hold reached the bound; its ladder's check passed on the
    discovered links and released the device; the first job died on the same missing link about 40 s later.
    The new hold started a new clock, so the host rung after the ladder was never due and the loop ran
    every ~13 min for an hour. Fails on base: the re-hold is seconds old and the next bound step is
    another ladder 10 min later."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, hold, BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)

    srv._note_hold_carry_job("start")  # the first job after the release...
    srv._note_hold_carry_job("end")  # ...dies on the fault the ladder did not heal
    srv._note_tenant_gate_verdict(f"job 8: {detail}")

    assert srv.device_hold_episode_since == since, "the re-hold continues the episode: its clock did not restart"
    assert _age() >= BOUND
    assert "hold_bound_carried" in events
    assert srv._hold_bound_cycle_due(_age()), "the ladder already ran this episode: the host rung is due now"

    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [], "no second ladder: the bound goes straight to the host rung"
    assert events.count("host_rung") == 1, "exactly one host-rung request"


@pytest.mark.asyncio
async def test_a_class_change_on_the_rehold_keeps_the_clock(monkeypatch, tmp_path, clear_job_state):
    """The episode is the device's, not the cause's: a re-hold naming another cause continues it."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _esc, _events, _detail = _open_hold(monkeypatch, "missing port (topology mismatch)", 200)
    since = srv.device_hold_episode_since
    srv._note_tenant_gate_verdict("")
    srv._note_tenant_gate_verdict("gate/post-job: eth/fabric fault on a present mesh")
    assert srv.device_hold_episode_since == since
    assert 200 <= _age() < BOUND, "below the bound nothing else changes: the class paths keep their speed"
    assert not srv._hold_bound_cycle_due(_age())


@pytest.mark.asyncio
async def test_a_rehold_after_the_carry_window_with_no_job_is_a_new_episode(monkeypatch, tmp_path, clear_job_state):
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._hold_carry["released"] = time.time() - srv._HOLD_CARRY_SEC - 1
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert _age() < 60, "a hold long after a release with no job in between is a new episode"
    assert not srv._hold_bound_cycle_due(_age())


@pytest.mark.asyncio
async def test_a_failed_first_job_keeps_the_carry_however_long_it_ran(monkeypatch, tmp_path, clear_job_state):
    """A first job that fails shows the release was wrong, however long it ran: a re-hold at its end continues
    the episode, so a device that keeps failing can never loop below the bound."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._note_hold_carry_job("start")
    srv._hold_carry["released"] = time.time() - 3600  # the job ran an hour
    srv._note_hold_carry_job("end", ok=False)
    assert (tmp_path / srv.HOLD_CARRY_FILE).exists(), "a failed first job keeps the carry"
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert srv.device_hold_episode_since == since
    assert srv._hold_bound_cycle_due(_age())


@pytest.mark.asyncio
@pytest.mark.parametrize("job_hours", [0.01, 2])
async def test_a_successful_first_job_ends_the_episode(monkeypatch, tmp_path, clear_job_state, job_hours):
    """Review finding: the bound ladder healed, the first job completed, then a new fault held the device. That
    hold is a fresh episode with its own class path, never a power cycle asked at once with no ladder."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "generic", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._note_hold_carry_job("start")
    srv._hold_carry["job_started"] = time.time() - job_hours * 3600
    srv._note_hold_carry_job("end", ok=True)
    assert not (tmp_path / srv.HOLD_CARRY_FILE).exists(), "a successful first job drops the carry, on disk too"
    srv._note_tenant_gate_verdict("gate/post-job: eth/fabric fault on a present mesh")
    assert _age() < 60, "a fresh episode"
    assert "hold_bound_carried" not in events
    assert not srv._hold_bound_cycle_due(_age())
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and "host_rung" not in events, "below the bound: no ladder and no cycle"


@pytest.mark.asyncio
async def test_a_passing_full_check_with_no_hold_open_drops_the_carry(monkeypatch, tmp_path, clear_job_state):
    """The between-jobs gate's full check (fabric included) passed on a device with no hold open: the device
    proved fit since the release, so the next hold is a new episode."""
    from tests.test_boot_fabric_reverify import _FABRIC_OK, _broker
    from tests.test_device_safety import _ledger_dir
    from tt_device_mcp.fsm import ServerFsm

    _isolate(monkeypatch, tmp_path)
    _ledger_dir(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    assert (tmp_path / srv.HOLD_CARRY_FILE).exists()
    srv._note_hold_carry_job("start")
    srv._note_hold_carry_job("end", ok=False)  # the job failed, yet the full check after it passes

    _broker(monkeypatch, tmp_path, ServerFsm(tmp_path / "fsmdir" / "fsm.json", current_boot_id="boot-a"), _FABRIC_OK)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True, force_fabric=True)

    assert not srv.device_hold_logged
    assert not (tmp_path / srv.HOLD_CARRY_FILE).exists() and srv._hold_carry is None
    srv._note_tenant_gate_verdict(f"job 9: {detail}")
    assert _age() < 60 and not srv._hold_bound_cycle_due(_age())


@pytest.mark.asyncio
async def test_a_pass_whose_release_is_refused_keeps_the_carry(monkeypatch, tmp_path, clear_job_state):
    """A pass that did not release the device (a reset still owes its fabric pass, spec 04 I32) proved
    nothing since the release: the carry stays for the hold that follows."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, _detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    _gate_passes(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "a reset")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True, force_fabric=True)
    assert srv._hold_carry is not None and (tmp_path / srv.HOLD_CARRY_FILE).exists()


def _gate_passes(monkeypatch, tmp_path):
    """The real gate's next pass finds the device healthy, fabric included."""
    from tests.test_boot_fabric_reverify import _FABRIC_OK, _broker
    from tests.test_device_safety import _ledger_dir
    from tt_device_mcp.fsm import ServerFsm

    _ledger_dir(monkeypatch, tmp_path)
    _broker(monkeypatch, tmp_path, ServerFsm(tmp_path / "fsmdir" / "fsm.json", current_boot_id="boot-a"), _FABRIC_OK)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")


@pytest.mark.asyncio
async def test_a_passing_full_check_that_releases_a_hold_keeps_the_new_carry(monkeypatch, tmp_path, clear_job_state):
    """The pass that releases a hold is the release itself, not proof since it: the carry it saves stays,
    and a failed first job continues the episode."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _escalations, _events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", 200)
    since = srv.device_hold_episode_since
    _gate_passes(monkeypatch, tmp_path)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True, force_fabric=True)

    assert not srv.device_hold_logged and not srv.device_hold_episode_since  # released
    assert srv._hold_carry is not None and (tmp_path / srv.HOLD_CARRY_FILE).exists()
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert srv.device_hold_episode_since == since and _age() >= 200


def _open_tenant_hold(monkeypatch, age_sec):
    """A hold opened because a foreign process holds the device, ``age_sec`` old."""
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why="foreign_holder"))
    srv._note_tenant_gate_verdict("device held by a foreign process (pid 4242)")
    assert srv._hold_episode_tenant
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=age_sec)).isoformat()


@pytest.mark.asyncio
async def test_a_tenant_hold_leaves_no_carry(monkeypatch, tmp_path, clear_job_state):
    """Review finding: a 30-min tenant hold was released and a fault hold opened within the window inherited
    age 1800, so the bound forced the full ladder at once and skipped the class's fast path."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "generic", 0)
    srv._note_tenant_gate_verdict("")  # a fault hold before it, released long ago: no carry in force
    srv._hold_carry["released"] = time.time() - srv._HOLD_CARRY_SEC - 1
    srv._note_tenant_gate_verdict(detail)
    srv._note_tenant_gate_verdict("")
    srv._drop_hold_carry()
    events.clear()

    _open_tenant_hold(monkeypatch, 1800)
    srv._note_tenant_gate_verdict("")  # the tenant left
    assert srv._hold_carry is None and not (tmp_path / srv.HOLD_CARRY_FILE).exists(), "nothing on disk"
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why="eth_frozen"))
    srv._note_tenant_gate_verdict(detail)
    assert _age() < 60, "the fault hold's clock starts fresh: the tenant's time is not fault time"
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [], "no forced ladder: the class's own path runs first"


@pytest.mark.asyncio
async def test_a_tenant_hold_between_does_not_spend_a_fault_holds_carry(monkeypatch, tmp_path, clear_job_state):
    """A fault hold's carry stays for a later fault hold in its window: a tenant hold in between neither takes
    it nor continues it."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "generic", BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)

    _open_tenant_hold(monkeypatch, 30)
    assert srv.device_hold_episode_since != since, "the tenant hold does not continue the fault episode"
    assert "hold_bound_carried" not in events
    srv._note_tenant_gate_verdict("")
    assert srv._hold_carry is not None and srv._hold_carry["since"] == since, "the fault carry is untouched"

    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why="eth_frozen"))
    srv._note_tenant_gate_verdict(detail)
    assert srv.device_hold_episode_since == since
    assert srv._hold_bound_cycle_due(_age())


async def _tenant_hold_after_a_bound_ladder_release(monkeypatch, tmp_path, released_ago):
    """A fault hold's bound ladder released the device ``released_ago`` seconds ago; a foreign process
    then held it and left with the device still faulted, so the tenant hold is a fault hold from now on."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "generic", BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._hold_carry["released"] = time.time() - released_ago
    _open_tenant_hold(monkeypatch, 20)
    assert srv.device_hold_episode_since != since and srv._hold_carry is not None
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why="eth_frozen", detail=detail))
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    return escalations, events, since


@pytest.mark.asyncio
async def test_a_tenant_hold_that_turns_into_a_fault_hold_takes_the_carry_in_its_window(
    monkeypatch, tmp_path, clear_job_state
):
    """Review finding: the switch from a tenant hold to a fault hold kept the tenant's start and left a fault
    carry still in its window unspent, so the bound ran one more ladder (about 10-13 min) before its host
    rung. Fails on base: the hold is 20 s old and no host rung is asked."""
    escalations, events, since = await _tenant_hold_after_a_bound_ladder_release(monkeypatch, tmp_path, 60)
    assert srv.device_hold_episode_since == since, "the fault hold continues the released episode"
    assert (tmp_path / srv.HOLD_EPISODE_FILE).read_text() == since, "its clock is on disk"
    assert srv._hold_carry is None and not (tmp_path / srv.HOLD_CARRY_FILE).exists(), "the carry is spent"
    assert "hold_turned_fault" in events and "hold_bound_carried" in events
    assert escalations == [], "no second ladder: the bound goes straight to the host rung"
    assert events.count("host_rung") == 1
    assert srv.device_hold_logged, "taking the carry lifts nothing"


@pytest.mark.asyncio
async def test_a_tenant_hold_that_turns_into_a_fault_hold_past_the_window_is_a_new_episode(
    monkeypatch, tmp_path, clear_job_state
):
    """The carry's own window still bounds it: past it, the fault hold keeps its own start and class path."""
    escalations, events, since = await _tenant_hold_after_a_bound_ladder_release(
        monkeypatch, tmp_path, srv._HOLD_CARRY_SEC + 1
    )
    assert srv.device_hold_episode_since != since and _age() < 60
    assert (tmp_path / srv.HOLD_EPISODE_FILE).read_text() == srv.device_hold_episode_since
    assert "hold_turned_fault" not in events and "hold_bound_carried" not in events
    assert escalations == [] and "host_rung" not in events, "below the bound: no ladder and no cycle"


@pytest.mark.asyncio
async def test_the_carried_host_rung_waits_out_the_cooldown_and_asks_once(monkeypatch, tmp_path, clear_job_state):
    """On the carried path the host rung is due at once, yet the I65 cooldown still holds it off: no ladder and
    no cycle inside it, the request fires when it ends, and exactly one per episode however many ticks run."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "generic", BOUND + 5)
    monkeypatch.setattr(srv, "_hold_bound_host_rung", _REAL_HOST_RUNG)  # the governor decides below
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._note_hold_carry_job("start")
    srv._note_hold_carry_job("end", ok=False)
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert srv._hold_bound_cycle_due(_age())

    cycles, until = [], datetime.now().timestamp() + 900
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_holds", lambda: False)
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
        srv.recovery_mechanism.power_cycle_retry = None  # the cycle ran: the governor's retry is spent

    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", allowed)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)

    for _ in range(3):  # inside the cooldown
        srv._check_hold_deadline(now=datetime.now())
        await _drain()
    assert escalations == [] and cycles == [], "inside the cooldown: no ladder, no cycle"
    assert srv.recovery_mechanism.power_cycle_retry is not None, "the denial armed the retry"

    # The cooldown ends: the governor's armed retry is due now. Every tick reads one clock, so the
    # episode age and the window bookkeeping agree.
    state["cooling"] = False
    _until, episode = srv.recovery_mechanism.power_cycle_retry
    srv.recovery_mechanism.power_cycle_retry = (time.time() - 1, episode)
    for _ in range(3):  # the cooldown's end, then later ticks
        srv._check_hold_deadline(now=datetime.now())
        await _drain()
    assert escalations == [], "the carried path never runs a second ladder"
    assert len(cycles) == 1, "exactly one power-cycle request per episode"
    assert "10-min hold bound" in cycles[0]


@pytest.mark.asyncio
async def test_a_job_started_after_the_window_does_not_extend_it(monkeypatch, tmp_path, clear_job_state):
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._hold_carry["released"] = time.time() - srv._HOLD_CARRY_SEC - 1
    srv._note_hold_carry_job("start")
    srv._note_hold_carry_job("end")
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert _age() < 60


# A release's fabric verdict (spec 03 I78): a fresh pass on the full cabling verifies it.
_PASS = {"result": "pass", "missing_vs_cabling": 0, "extra_vs_cabling": 0, "links_short": False, "age_s": 3.0}
_UNVERIFIED = {
    "no verdict": {"result": "none"},
    "failed": {**_PASS, "result": "fail", "missing_vs_cabling": 2, "links_short": True},
    "short pass": {**_PASS, "missing_vs_cabling": 2, "links_short": True},
    "stale pass": {**_PASS, "age_s": 400.0},
}


def _release_on(monkeypatch, verdict):
    """The open hold is released on a check whose fabric verdict is ``verdict``."""
    monkeypatch.setattr(srv.health_monitor, "fabric_verdict", lambda: dict(verdict))
    srv._note_tenant_gate_verdict("")
    assert not srv.device_hold_logged and srv._hold_carry is not None


def _first_job_ran_and_was_killed(seconds):
    """The first job started at the release, ran ``seconds`` and ended killed (exit 143), naming no fault.
    The released episode's start moves back with it. Returns that start."""
    srv._note_hold_carry_job("start")
    srv._hold_carry["released"] = srv._hold_carry["job_started"] = time.time() - seconds
    since = (datetime.fromisoformat(srv._hold_carry["since"]) - timedelta(seconds=seconds)).isoformat()
    srv._hold_carry["since"] = since
    srv._note_hold_carry_job("end")
    assert srv._hold_carry is not None, "a kill is no clean end: the carry stays on record"
    return since


_GENERIC_REHOLD = "gate/post-job: eth/fabric fault on a present mesh"


@pytest.mark.asyncio
async def test_replay_a_hold_after_a_verified_release_and_a_long_first_job_is_a_new_episode(
    monkeypatch, tmp_path, clear_job_state
):
    """A Galaxy host: a 138 s hold was released on a full check whose fabric pass found every cabled link;
    the next job ran 29 min, was killed for its time limit, and the reset after it held the device again.
    That hold took the released episode's start, was born 32 min old ('held 1929s past 1200s bound') and
    escalated at once. Fails on base: the re-hold continues the episode, far past the bound."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, _detail = _open_hold(monkeypatch, "generic", 138)
    _release_on(monkeypatch, _PASS)
    since = _first_job_ran_and_was_killed(29 * 60)
    srv._note_tenant_gate_verdict(_GENERIC_REHOLD)

    assert srv.device_hold_episode_since != since, "a new episode"
    assert _age() < 60, "its clock starts at the re-hold"
    assert "hold_carry_ended" in events and "hold_bound_carried" not in events
    assert not srv._hold_bound_cycle_due(_age())
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and "host_rung" not in events, "no early ladder and no early power cycle"
    assert "hold_deadline_forced_escalation" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", list(_UNVERIFIED))
async def test_a_long_first_job_after_an_unverified_release_keeps_the_episode(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """A release on no fresh fabric pass of the full cabling proved nothing: the first job that fails keeps
    the episode, however long it ran. Fails on base: the carry does not record whether its release was
    verified."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _esc, events, _detail = _open_hold(monkeypatch, "generic", 138)
    _release_on(monkeypatch, _UNVERIFIED[verdict])
    assert srv._hold_carry["verified"] is False
    since = _first_job_ran_and_was_killed(29 * 60)
    srv._note_tenant_gate_verdict(_GENERIC_REHOLD)
    assert srv.device_hold_episode_since == since and _age() >= BOUND, "the clock keeps the episode start"
    assert "hold_carry_ended" not in events


@pytest.mark.asyncio
async def test_a_quick_rehold_after_a_verified_release_keeps_the_episode(monkeypatch, tmp_path, clear_job_state):
    """Flapping stays one episode: a re-hold seconds after a verified release, no passing full check in
    between, keeps the clock, so the bound still fires no later than the episode start + 600 s."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _esc, events, _detail = _open_hold(monkeypatch, "generic", 400)
    since = srv.device_hold_episode_since
    _release_on(monkeypatch, _PASS)
    srv._hold_carry["released"] = time.time() - 20
    srv._note_tenant_gate_verdict(_GENERIC_REHOLD)
    assert srv.device_hold_episode_since == since and _age() >= 400
    assert "hold_carry_ended" not in events


@pytest.mark.asyncio
async def test_a_verified_release_by_the_bound_ladder_then_a_long_failed_first_job_still_goes_to_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """The bound ladder's check may pass on a device the ladder did not heal: a verified pass does not end
    that carry, so the re-hold after a long failed first job still asks for the host rung at once."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    srv._note_hold_bound_ladder(_age(), OUTCOME_RECOVERED)
    _release_on(monkeypatch, _PASS)
    assert srv._hold_carry["verified"] is True
    since = _first_job_ran_and_was_killed(3600)
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert srv.device_hold_episode_since == since
    assert srv._hold_bound_cycle_due(_age())
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and events.count("host_rung") == 1


@pytest.mark.asyncio
async def test_a_new_episode_after_a_verified_release_runs_its_ladder_at_its_own_10_min_mark_then_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """The fresh clock is a whole clock: nothing before the new hold's own 10-min mark, then the full
    ladder, and a full check that still fails asks for the power cycle in the same run. Fails on base:
    the re-hold is born past the bound and the ladder runs at once."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, _detail = _open_hold(monkeypatch, "generic", 138)
    _release_on(monkeypatch, _PASS)
    _first_job_ran_and_was_killed(29 * 60)
    srv._note_tenant_gate_verdict(_GENERIC_REHOLD)

    async def recovered_ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        return OUTCOME_RECOVERED  # chips back, the full check gave no pass: the hold stands

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", recovered_ladder)
    srv._check_hold_deadline(now=datetime.now())  # the re-hold's first tick, on its own clock
    await _drain()
    assert escalations == [] and "host_rung" not in events, "nothing on the re-hold's first tick"
    for age in (5, BOUND - 30):
        srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=age)).isoformat()
        srv._check_hold_deadline(now=datetime.now())
        await _drain()
        assert escalations == [] and "host_rung" not in events, f"nothing at {age}s, below its own bound"
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=BOUND + 3)).isoformat()
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"], "the full ladder at its own 10-min mark"
    assert srv._recovery_degraded(), "no release without a passing full check"
    assert events.count("host_rung") == 1, "the failed check asks for the power cycle"


@pytest.mark.asyncio
async def test_the_carry_does_not_cross_a_reboot(monkeypatch, tmp_path, clear_job_state):
    """A reboot or power cycle is the biggest reset there is: the next hold is a new episode."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-b")
    monkeypatch.setattr(srv, "_hold_carry", None)
    monkeypatch.setattr(srv, "_hold_carry_loaded", False)  # a new process reads the carry from disk
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert _age() < 60


def _restart_broker(monkeypatch):
    """A new broker process: nothing in memory, only what the old one left on disk."""
    for name, value in (
        ("device_hold_logged", False),
        ("device_hold_episode_since", ""),
        ("_hold_episode_restorable", True),
        ("_hold_episode_tenant", False),
        ("_hold_episode_startup", False),
        ("_hold_carry", None),
        ("_hold_carry_loaded", False),
        ("_hold_bound_episode", ""),
        ("_hold_bound_ladder_age", None),
        ("_hold_bound_ladder_recovered", False),
        ("_hold_bound_cycle_age", None),
        ("_hold_bound_check_failed_age", None),
    ):
        monkeypatch.setattr(srv, name, value)


@pytest.mark.asyncio
async def test_replay_a_restart_whose_startup_check_releases_then_a_failed_first_job_goes_to_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """The same host, 09:02: an autoupdate restarted the broker mid-episode; its startup check released the
    device on the discovered links and the first job re-held it with a new clock. Fails on base: the
    restart's release wiped the episode start, so the re-hold is seconds old."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    since = srv.device_hold_episode_since
    srv._note_hold_bound_ladder(BOUND + 5, OUTCOME_RECOVERED)  # the bound ladder ran before the restart
    _restart_broker(monkeypatch)

    srv._note_tenant_gate_verdict("")  # the startup check released it
    srv._note_tenant_gate_verdict(f"job 8: {detail}")  # the first job died on the same link

    assert srv.device_hold_episode_since == since, "the restart did not restart the episode clock"
    assert srv._hold_bound_cycle_due(_age()), "the ladder before the restart counts: the host rung is due"
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and events.count("host_rung") == 1


@pytest.mark.asyncio
async def test_replay_a_restart_whose_real_startup_gate_releases_keeps_the_carry(
    monkeypatch, tmp_path, clear_job_state
):
    """The 09:02 replay through the real startup gate: its full check releases the hold the dead broker left
    open and saves the carry; the same pass must not end it. Fails on eb54d67: the gate's own full-check
    drop ended that carry, so the first job's re-hold was seconds old and the host rung never due."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _escalations, _events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    since = srv.device_hold_episode_since
    srv._note_hold_bound_ladder(BOUND + 5, OUTCOME_RECOVERED)  # the bound ladder ran before the restart
    _restart_broker(monkeypatch)
    _gate_passes(monkeypatch, tmp_path)
    assert not srv.device_hold_logged  # no tenant refused yet: the usual startup

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    assert srv._hold_carry is not None and (tmp_path / srv.HOLD_CARRY_FILE).exists()
    srv._note_hold_carry_job("start")
    srv._note_hold_carry_job("end")  # the first job dies on the same link
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert srv.device_hold_episode_since == since, "the restart's startup check did not restart the clock"
    assert srv._hold_bound_cycle_due(_age()), "the ladder before the restart counts: the host rung is due"


@pytest.mark.asyncio
async def test_a_hold_open_across_a_restart_escalates_at_once_when_past_the_bound(
    monkeypatch, tmp_path, clear_job_state
):
    """A hold that began before the restart escalates no later than 10 min after it began: here at once."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 120)
    since = srv.device_hold_episode_since
    _restart_broker(monkeypatch)

    srv._note_tenant_gate_verdict(detail)  # the startup check still fails: held from the start
    assert srv.device_hold_episode_since == since
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"], "the full ladder runs on the first tick, not 10 min after the restart"


@pytest.mark.asyncio
async def test_a_hold_open_across_a_restart_after_its_bound_ladder_goes_to_the_host_rung(
    monkeypatch, tmp_path, clear_job_state
):
    """A restart after the bound's ladder ran keeps that ladder: the host rung follows, not a new ladder."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 120)
    since = srv.device_hold_episode_since
    srv._note_hold_bound_ladder(BOUND + 5, OUTCOME_RECOVERED)
    _restart_broker(monkeypatch)

    srv._note_tenant_gate_verdict(detail)
    assert srv.device_hold_episode_since == since
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and events.count("host_rung") == 1


def _carry_before_a_job(tmp_path):
    """A release moments ago saved a carry; the next job runs inside its window."""
    srv._save_hold_carry((datetime.now() - timedelta(seconds=BOUND + 5)).isoformat(), "post-job: fabric unverified")
    assert (tmp_path / srv.HOLD_CARRY_FILE).exists()


def _job(tmp_path, job_id, command):
    return srv.Job(
        id=job_id,
        owner="tenant",
        workspace="/tmp",
        command=command,
        queued_at="t",
        log_file=str(tmp_path / f"{job_id}.log"),
    )


@pytest.mark.asyncio
async def test_the_first_jobs_end_closes_the_carry_window_even_when_a_job_end_check_raises(
    monkeypatch, tmp_path, clear_job_state
):
    """Review finding: the job-end note ran after the health monitor's job-end checks, so one of them raising
    left job_ended unset and the carry window open for good. Fails on base: no job_ended."""
    _isolate(monkeypatch, tmp_path)
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("job-end check failed")

    monkeypatch.setattr(srv.health_monitor, "note_job_after_topology_lift", boom)
    _carry_before_a_job(tmp_path)
    await _run_one(_job(tmp_path, "951", "true"), limit_sec=3, until=lambda: (srv._hold_carry or {}).get("job_ended"))
    rec = json.loads((tmp_path / srv.HOLD_CARRY_FILE).read_text())
    assert rec.get("job_started") and rec.get("job_ended"), "the job's end is noted, on disk too"
    assert srv._take_hold_carry() is not None, "unknown verdict: the carry stays, bounded by its window"


@pytest.mark.asyncio
@pytest.mark.parametrize("command, kept", [("true", False), ("exit 3", True)])
async def test_the_first_jobs_verdict_still_decides_the_carry(monkeypatch, tmp_path, clear_job_state, command, kept):
    """A clean first job drops the carry; a failed one keeps it with its end noted."""
    _isolate(monkeypatch, tmp_path)
    _free_device_lock(monkeypatch)
    _quiet_post_job_gate(monkeypatch)
    _no_workspace_activation(monkeypatch)
    _carry_before_a_job(tmp_path)
    job = _job(tmp_path, "952", command)
    await _run_one(job, limit_sec=5)
    assert job.finished_at is not None
    assert (tmp_path / srv.HOLD_CARRY_FILE).exists() is kept
    if kept:
        assert srv._hold_carry.get("job_ended")


def _startup_hold_after_restart(monkeypatch):
    """The new broker's fresh startup episode (spec 03 I4): nothing in it names a fault."""
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why="startup_unverified"))


def _record_events(monkeypatch):
    """Every health event with its fields, from here on."""
    from tests.conftest import patch_health_event

    seen = []
    patch_health_event(monkeypatch, lambda kind, **k: seen.append((kind, k)))
    return seen


@pytest.mark.asyncio
async def test_a_startup_hold_after_a_released_episode_starts_its_own_clock(monkeypatch, tmp_path, clear_job_state):
    """A job timed out, its reset and full check (fabric included) released the hold about 130 s later, and
    the next job started inside the carry window. A broker restart about 26 min later re-adopted that live
    job and opened the startup hold, which took the released episode's start: it was born about 1700 s old,
    so the deadline flag, a forced escalation and a tenant-blocked page fired on a device whose job was
    healthy. Fails on base: the startup hold continues the released episode. The startup hold neither takes
    nor spends the carry: the fault it may turn into still takes it."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, _events, detail = _open_hold(monkeypatch, "generic", 130)
    old_since = srv.device_hold_episode_since
    srv._note_tenant_gate_verdict("")  # reset + full check passed: released, carry saved
    srv._note_hold_carry_job("start")  # the next job starts inside the window and is still running
    shift = 1580.0  # the restart comes ~26 min later, the job still running
    srv._hold_carry.update(
        since=(datetime.fromisoformat(old_since) - timedelta(seconds=shift)).isoformat(),
        released=srv._hold_carry["released"] - shift,
        job_started=srv._hold_carry["job_started"] - shift,
    )
    srv._write_hold_carry()
    _restart_broker(monkeypatch)
    _startup_hold_after_restart(monkeypatch)
    seen = _record_events(monkeypatch)

    srv._note_tenant_gate_verdict("device unverified: gate/startup: awaiting the startup check")

    assert _age() < 60, "the released episode is over: the startup hold starts its own clock"
    held = [k for kind, k in seen if kind == "device_held"]
    assert len(held) == 1 and "continues_episode" not in held[0]
    assert held[0].get("carry_deferred"), "the deferred carry is named for the log reader"
    assert (tmp_path / srv.HOLD_CARRY_FILE).exists(), "the carry is kept for a fault the hold may turn into"
    assert not srv._hold_bound_cycle_due(_age())
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    kinds = [kind for kind, _ in seen]
    assert escalations == [], "no forced escalation before the hold's own 10-min mark"
    for false_alarm in ("hold_stuck_past_deadline", "hold_bound_tenant_blocked", "hold_bound_carried"):
        assert false_alarm not in kinds


@pytest.mark.asyncio
async def test_a_hold_open_across_a_restart_keeps_its_clock_even_under_a_startup_why(
    monkeypatch, tmp_path, clear_job_state
):
    """A hold the last broker left open keeps its start, whatever why the new fsm opens with: a restart never
    resets a real hold's clock, so the bound ladder still runs at the episode's 10-min mark (here at once)."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, _events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 120)
    since = srv.device_hold_episode_since
    _restart_broker(monkeypatch)
    _startup_hold_after_restart(monkeypatch)
    seen = _record_events(monkeypatch)

    srv._note_tenant_gate_verdict(detail)

    assert srv.device_hold_episode_since == since, "the open hold's clock survives the restart"
    assert not any(k.get("carry_deferred") for kind, k in seen if kind == "device_held")
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why=srv.FABRIC_RELIFT_WHY))
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"], "the full ladder runs on the first tick, not 10 min after the restart"


@pytest.mark.asyncio
async def test_a_startup_hold_below_its_own_bound_runs_the_ladder_at_its_own_10_min_mark(
    monkeypatch, tmp_path, clear_job_state
):
    """The fresh clock is still bounded: a startup hold that deferred a carry and is still held at its own
    10-min mark gets the bound's full ladder there (spec 04 I76)."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, _events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", 130)
    srv._note_tenant_gate_verdict("")  # released: carry saved
    _restart_broker(monkeypatch)
    _startup_hold_after_restart(monkeypatch)
    srv._note_tenant_gate_verdict("device unverified: gate/startup: awaiting the startup check")
    assert _age() < 60
    # The startup gate's full check fails on the short link count: the hold is now that class...
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why=srv.FABRIC_RELIFT_WHY))
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=BOUND + 5)).isoformat()
    srv._check_hold_deadline(now=datetime.now())  # ...and its own 10-min mark has come
    await _drain()
    assert escalations == ["present-forced"]


@pytest.mark.asyncio
async def test_a_startup_hold_that_deferred_a_carry_lifts_only_on_a_full_check_pass(
    monkeypatch, tmp_path, clear_job_state
):
    """The fresh clock adds no lift path (spec 03 I4, goal: the full health check before any hold lifts): the
    real startup gate keeps the hold on a fabric check that counts fewer links than the cabling expects, and
    only a full pass, fabric included, releases it."""
    from tests.test_boot_fabric_reverify import _FABRIC_OK, _broker
    from tests.test_device_safety import _ledger_dir
    from tt_device_mcp.fsm import ServerFsm

    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, _detail = _open_hold(monkeypatch, "generic", 130)
    srv._note_tenant_gate_verdict("")  # released: carry saved
    srv._note_hold_carry_job("start")
    _restart_broker(monkeypatch)
    _ledger_dir(monkeypatch, tmp_path)
    short = (False, {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": f"what(): {MISMATCH}"}})
    fresh = ServerFsm(tmp_path / "fsmdir" / "fsm.json", current_boot_id="boot-a")
    fresh.boot_merge(None)
    _broker(monkeypatch, tmp_path, fresh, short)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    assert srv.fsm.record.why == "startup_unverified"
    srv._note_tenant_gate_verdict("device unverified: gate/startup: awaiting the startup check")
    assert _age() < 60, "the released episode is not continued"

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert srv.device_hold_logged and srv.fsm.state is not ServerState.HEALTHY, "a short link count is a failure"

    _broker(monkeypatch, tmp_path, srv.fsm, _FABRIC_OK)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert not srv.device_hold_logged and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", list(_CLASSES))
async def test_replay_a_restart_during_the_first_job_after_the_bound_ladder_then_a_failed_check_goes_to_the_host_rung(
    monkeypatch, tmp_path, clear_job_state, hold
):
    """The bound's ladder released the device; the first job was still running when the broker restarted and
    re-adopted it; it then failed on the same fault (the startup hold turns into that fault's hold). The
    episode continues: the host rung is due at once, with no second ladder and no fresh 10-min clock. Fails
    when the startup hold spends the carry: the clock restarts, and the cycle waits another ladder."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, _detail = _open_hold(monkeypatch, hold, BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._note_hold_carry_job("start")  # the first job after the release, still running
    _restart_broker(monkeypatch)  # the new broker re-adopts it
    _startup_hold_after_restart(monkeypatch)
    srv._note_tenant_gate_verdict("device unverified: gate/startup: awaiting the startup check")
    assert _age() < 60, "the startup hold starts its own clock while it names no fault"
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why=_CLASSES[hold][0]))

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    assert srv.device_hold_episode_since == since, "the failed first job continues the episode"
    assert srv._hold_bound_cycle_due(_age()), "the ladder already ran this episode: the host rung is due now"
    assert escalations == [], "no second ladder"
    assert events.count("host_rung") == 1
    assert not (tmp_path / srv.HOLD_CARRY_FILE).exists(), "taken once: no later hold continues it again"


@pytest.mark.asyncio
async def test_a_startup_hold_whose_real_startup_check_fails_takes_the_carry(monkeypatch, tmp_path, clear_job_state):
    """Through the real startup gate: a full check that counts fewer links than the cabling expects keeps the
    hold, the hold now names that fault, and the watchdog's next tick continues the released episode."""
    from tests.test_boot_fabric_reverify import _broker
    from tests.test_device_safety import _ledger_dir
    from tt_device_mcp.fsm import ServerFsm

    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, _detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._note_hold_carry_job("start")
    _restart_broker(monkeypatch)
    _ledger_dir(monkeypatch, tmp_path)
    short = (False, {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": f"what(): {MISMATCH}"}})
    fresh = ServerFsm(tmp_path / "fsmdir" / "fsm.json", current_boot_id="boot-a")
    fresh.boot_merge(None)
    _broker(monkeypatch, tmp_path, fresh, short)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    srv._note_tenant_gate_verdict("device unverified: gate/startup: awaiting the startup check")
    assert _age() < 60

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert srv.device_hold_logged and srv.fsm.record.why != "startup_unverified"
    srv._check_hold_deadline(now=datetime.now())

    assert srv.device_hold_episode_since == since
    assert srv._hold_bound_cycle_due(_age())
    assert not (tmp_path / srv.HOLD_CARRY_FILE).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exit_code, log, kept", [("0", "", False), ("1", "", True), ("0", "waiting for active ethernet core\n", True)]
)
async def test_a_readopted_first_job_closes_the_carry_window(
    monkeypatch, tmp_path, clear_job_state, exit_code, log, kept
):
    """The first job after a release, re-adopted across a restart, ends the carry window as it would have
    under the broker that started it: a clean exit 0 proves the release (carry dropped), a failure or a
    reported fault keeps it with its end noted, so the window closes _HOLD_CARRY_SEC later."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    _carry_before_a_job(tmp_path)
    srv._note_hold_carry_job("start")
    _restart_broker(monkeypatch)
    job = _job(tmp_path, "953", "pytest")
    job.status = srv.JobStatus.RUNNING
    job.queued_at = job.started_at = datetime.now().isoformat()
    (tmp_path / "953.log").write_text(log)
    srv.jobs["953"] = job
    (tmp_path / "953.exit").write_text(exit_code)

    await srv._monitor_readopted_scope("953", "ttdev-job-953.scope")

    assert (tmp_path / srv.HOLD_CARRY_FILE).exists() is kept
    if kept:
        assert json.loads((tmp_path / srv.HOLD_CARRY_FILE).read_text()).get("job_ended")


@pytest.mark.asyncio
async def test_a_startup_hold_restored_across_a_second_restart_still_defers_the_carry(
    monkeypatch, tmp_path, clear_job_state
):
    """A second restart during the startup hold restores that hold's own start; it still names no fault, so
    it still neither takes nor spends the released episode's carry."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _escalations, _events, _detail = _open_hold(monkeypatch, "generic", 130)
    srv._note_tenant_gate_verdict("")  # released: carry saved
    srv._note_hold_carry_job("start")
    for _ in range(2):
        _restart_broker(monkeypatch)
        _startup_hold_after_restart(monkeypatch)
        srv._note_tenant_gate_verdict("device unverified: gate/startup: awaiting the startup check")
        assert _age() < 60
        assert (tmp_path / srv.HOLD_CARRY_FILE).exists()


# How a re-adopted first job can end without proving the release: (exit file, log).
_UNPROVEN_ENDS = {
    "fault named": ("1", "waiting for active ethernet core\n"),
    "exit 1, no fault": ("1", ""),
    "no exit status": (None, ""),
}


async def _readopted_first_job_ends(monkeypatch, tmp_path, hold, end):
    """A release after the bound ladder; its first job was still running when the broker restarted, the new
    broker re-adopted it under its startup hold, and the job then ended ``end`` through the real re-adopt path."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    escalations, events, _detail = _open_hold(monkeypatch, hold, BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._note_hold_carry_job("start")
    _restart_broker(monkeypatch)
    # The startup record as the fsm's boot merge opens it: RECOVERING, startup_unverified, not dirty.
    monkeypatch.setattr(
        srv.fsm,
        "_record",
        dataclasses.replace(srv.fsm.record, state=ServerState.RECOVERING, why="startup_unverified", dirty=False),
    )
    monkeypatch.setattr(srv, "device_fault_reported", "")
    srv._note_tenant_gate_verdict("device unverified: gate/startup: awaiting the startup check")
    assert _age() < 60
    exit_code, log = _UNPROVEN_ENDS[end]
    job = _job(tmp_path, "960", "pytest")
    job.status = srv.JobStatus.RUNNING
    job.queued_at = job.started_at = datetime.now().isoformat()
    (tmp_path / "960.log").write_text(log)
    if exit_code is not None:
        (tmp_path / "960.exit").write_text(exit_code)
    srv.jobs["960"] = job
    mark = len(events)
    await srv._monitor_readopted_scope("960", "ttdev-job-960.scope")
    return escalations, events, since, mark


def _gate_ran_for(seconds):
    """The startup gate's reset and full check took ``seconds`` after the job's end."""
    for k in ("released", "job_started", "job_ended"):
        if srv._hold_carry.get(k):
            srv._hold_carry[k] -= seconds
    srv._write_hold_carry()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_sec", [srv._HOLD_CARRY_SEC + 10, srv.STARTUP_VERIFY_DEFER_MAX_SEC])
@pytest.mark.parametrize("end", list(_UNPROVEN_ENDS))
@pytest.mark.parametrize("hold", list(_CLASSES))
async def test_a_readopted_first_job_that_fails_keeps_the_carry_through_a_slow_startup_gate(
    monkeypatch, tmp_path, clear_job_state, hold, end, gate_sec
):
    """The re-adopted first job's failure does not change the startup hold's `why`, so the hold names its
    fault only when the startup gate's reset and full check fail, which can come more than _HOLD_CARRY_SEC
    after the job's end (a failing reset plus the fabric check and its retry). The episode still continues:
    the host rung is due at once, no second ladder, no fresh 10-min clock. Fails when the carry window keeps
    counting from the job's end under the startup hold: the clock restarts and the cycle waits a ladder."""
    escalations, events, since, mark = await _readopted_first_job_ends(monkeypatch, tmp_path, hold, end)
    assert srv.fsm.record.why == "startup_unverified"
    _gate_ran_for(gate_sec)
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, why=_CLASSES[hold][0]))

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    assert srv.device_hold_episode_since == since, "the failed first job continues the episode"
    assert srv._hold_bound_cycle_due(_age()), "the ladder already ran this episode: the host rung is due now"
    assert escalations == [], "no second ladder"
    assert events[mark:].count("host_rung") == 1
    assert not (tmp_path / srv.HOLD_CARRY_FILE).exists(), "taken once"
    assert srv.device_hold_logged and "device_released" not in events[mark:], "nothing lifted the hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("end", list(_UNPROVEN_ENDS))
async def test_the_pinned_carry_leaves_the_startup_hold_to_its_gate(monkeypatch, tmp_path, clear_job_state, end):
    """Until the startup gate's verdict the hold is still the gate's (spec 03 I75): the pinned carry resumes
    no bound and asks no host rung, however long the gate runs; it waits on disk for the verdict."""
    escalations, events, since, mark = await _readopted_first_job_ends(monkeypatch, tmp_path, "generic", end)
    _gate_ran_for(srv._HOLD_CARRY_SEC + 10)

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    assert srv.device_hold_episode_since != since and _age() < 60
    assert not srv._hold_bound_cycle_due(_age())
    assert escalations == [] and "host_rung" not in events[mark:]
    assert json.loads((tmp_path / srv.HOLD_CARRY_FILE).read_text()).get("startup_pinned")
    assert srv.device_hold_logged and "device_released" not in events[mark:]


@pytest.mark.asyncio
async def test_a_slow_real_startup_gate_after_a_failed_readopted_job_goes_to_the_host_rung_and_lifts_only_on_a_pass(
    monkeypatch, tmp_path, clear_job_state
):
    """Through the real startup gate, more than _HOLD_CARRY_SEC after the re-adopted job ended on a fault: a
    full check that counts fewer links than the cabling expects keeps the hold and continues the episode at
    the host rung; only a later full pass, fabric included, releases it."""
    from tests.test_boot_fabric_reverify import _FABRIC_OK, _broker
    from tests.test_device_safety import _ledger_dir
    from tt_device_mcp.fsm import ServerFsm

    escalations, events, since, mark = await _readopted_first_job_ends(
        monkeypatch, tmp_path, "missing port (topology mismatch)", "fault named"
    )
    _gate_ran_for(srv._HOLD_CARRY_SEC + 10)
    _ledger_dir(monkeypatch, tmp_path)
    short = (False, {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": f"what(): {MISMATCH}"}})
    fresh = ServerFsm(tmp_path / "fsmdir" / "fsm.json", current_boot_id="boot-a")
    fresh.boot_merge(None)
    _broker(monkeypatch, tmp_path, fresh, short)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert srv.device_hold_logged and srv.fsm.record.why != "startup_unverified", "a short link count fails"
    srv._check_hold_deadline(now=datetime.now())

    assert srv.device_hold_episode_since == since
    assert srv._hold_bound_cycle_due(_age())
    assert "device_released" not in events[mark:]

    _broker(monkeypatch, tmp_path, srv.fsm, _FABRIC_OK)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert not srv.device_hold_logged and srv.fsm.state is ServerState.HEALTHY
