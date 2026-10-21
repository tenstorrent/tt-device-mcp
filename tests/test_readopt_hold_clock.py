# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""No startup hold episode over a live re-adopted job (spec 03 I75, spec 04 I76/I82).

A broker restarted over a job it re-adopted opens its startup hold, which names no fault. Counted from the
restart, that hold's 10-min clock ran out while the job still ran, and the bound's ladder deferred on the
running job with no bound of its own (a 742 s hold with no ladder at the mark). While every re-adopted job
still runs and nothing names a fault, no hold episode opens: the device is the job's, as it was before the
restart, and every other job stays refused. The episode, and its 10-min clock, starts when the jobs end and
the startup gate takes the device. Only a full check with fabric, passed after that, gives the device back
to the queue. No test reaches a real reset or power cycle."""

import dataclasses
import json
import time
from datetime import datetime, timedelta

import pytest

from tests.test_boot_fabric_reverify import _FABRIC_OK, _broker
from tests.test_classac_power_cycle_direct import rig  # noqa: F401 - the fixture
from tests.test_device_safety import _ledger_dir
from tests.test_hold_bound import (
    BOUND,
    MISMATCH,
    _bound_hold_with_a_failing_full_check,
    _drain,
    _since,
    _tick,
    _watchdog,
)
from tests.test_hold_episode_carry import (
    _age,
    _isolate,
    _job,
    _open_hold,
    _record_events,
    _restart_broker,
    _restore_broker_state,  # noqa: F401 - the autouse fixture
)
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerFsm, ServerState

STARTUP = "device unverified: gate/startup: awaiting the startup check"
_SHORT = (False, {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": MISMATCH}})
# Escalation and page events no hold over a healthy re-adopted job may raise.
_FALSE_ALARMS = ("device_held", "hold_stuck_past_deadline", "hold_bound_tenant_blocked", "hold_bound_carried")


def _startup_record(monkeypatch):
    """The new broker's startup record as the fsm's boot merge opens it: RECOVERING, nothing names a fault."""
    monkeypatch.setattr(
        srv.fsm,
        "_record",
        dataclasses.replace(srv.fsm.record, state=ServerState.RECOVERING, why="startup_unverified", dirty=False),
    )
    for name, value in (("device_fault_reported", ""), ("_kill_reset_owed", ""), ("isolated_chips", set())):
        monkeypatch.setattr(srv, name, value)


def _readopted_job(monkeypatch, tmp_path, job_id="960"):
    """A job the previous broker started, re-adopted by this one and still running."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(srv, "_startup_verify_ready_at", None)
    monkeypatch.setattr(srv, "_hold_startup_deferred", False)
    job = _job(tmp_path, job_id, "pytest")
    job.status = srv.JobStatus.RUNNING
    job.queued_at = job.started_at = datetime.now().isoformat()
    (tmp_path / f"{job_id}.log").write_text("")
    srv.jobs[job_id] = job
    monkeypatch.setattr(srv, "readopted_scopes", {job_id: f"ttdev-job-{job_id}.scope"})
    return job


async def _job_ends(tmp_path, job, exit_code="0"):
    """The re-adopted job ends through the real re-adopt path; the startup gate then stops waiting on it."""
    (tmp_path / f"{job.id}.exit").write_text(exit_code)
    await srv._monitor_readopted_scope(job.id, f"ttdev-job-{job.id}.scope")
    assert not srv.readopted_scopes
    srv._startup_verify_ready_at = time.monotonic()


def _restarted_over_a_live_job(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _restart_broker(monkeypatch)
    _startup_record(monkeypatch)
    return _readopted_job(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_a_live_readopted_job_opens_no_hold_and_nothing_escalates_past_10_min(
    monkeypatch, tmp_path, clear_job_state
):
    """The startup hold over a healthy re-adopted job that runs 25 min opens no hold episode: no clock, no
    deadline, no forced escalation, no tenant-blocked page. Fails on base: the hold opened at the restart
    and its clock passed 10 min while the bound's ladder deferred on the running job. The device stays
    refused to every other job meanwhile (goal 11): the startup record and the re-adopted scope hold it."""
    _restarted_over_a_live_job(monkeypatch, tmp_path)
    seen = _record_events(monkeypatch)

    for minutes in (0, 11, 25):
        srv._note_tenant_gate_verdict(STARTUP)
        srv._check_hold_deadline(now=datetime.now() + timedelta(minutes=minutes))
        await _drain()

    kinds = [kind for kind, _ in seen]
    assert not srv.device_hold_logged and not srv.device_hold_episode_since, "no hold episode opened"
    assert kinds.count("startup_hold_behind_readopted_job") == 1, "named once for the log reader"
    for false_alarm in _FALSE_ALARMS:
        assert false_alarm not in kinds
    assert srv._forced_escalation_task is None, "nothing escalated over the healthy job"
    assert srv._device_unavailable_for_tenant(), "every other job stays refused"
    assert not srv._queue_idle(), "the re-adopted scope keeps the queue shut"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["dirty", "reported fault", "owed kill reset", "isolated chip", "liveness", "job not running"]
)
async def test_anything_that_names_a_fault_opens_the_hold_at_once(monkeypatch, tmp_path, clear_job_state, fault):
    """The deferral covers only a startup hold whose sole cause is the live re-adopted job: any fault sign
    opens the hold, and its clock, at once, the job still running."""
    job = _restarted_over_a_live_job(monkeypatch, tmp_path)
    if fault == "dirty":
        monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, dirty=True))
    elif fault == "reported fault":
        monkeypatch.setattr(srv, "device_fault_reported", "waiting for active ethernet core")
    elif fault == "owed kill reset":
        monkeypatch.setattr(srv, "_kill_reset_owed", "re-adopted job 960 killed")
    elif fault == "isolated chip":
        monkeypatch.setattr(srv, "isolated_chips", {"29"})
    elif fault == "liveness":
        monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "chip 29 off the bus")
    else:
        job.status = srv.JobStatus.FAILED

    srv._note_tenant_gate_verdict(STARTUP)

    assert srv.device_hold_logged and _age() < 60


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [True, False], ids=["startup episode dropped", "fault episode kept"])
async def test_a_restored_startup_episode_is_dropped_and_a_restored_fault_episode_keeps_its_clock(
    monkeypatch, tmp_path, clear_job_state, startup
):
    """A startup hold the previous broker left open over the same job restarts its clock: it is dropped from
    disk, so a later restart restores nothing from it either. A fault hold open at the restart keeps its
    own clock (spec 04 I82) and opens at once."""
    _isolate(monkeypatch, tmp_path)
    old = _since(BOUND + 900)
    srv._persist_hold_episode(old, startup=startup)
    _restarted_over_a_live_job(monkeypatch, tmp_path)

    srv._note_tenant_gate_verdict(STARTUP)

    if startup:
        assert not srv.device_hold_logged and not srv.device_hold_episode_since
        assert not (tmp_path / srv.HOLD_EPISODE_FILE).exists(), "the dropped clock is gone from disk too"
        _restart_broker(monkeypatch)
        assert srv._restore_hold_episode() == ""
    else:
        assert srv.device_hold_logged and srv.device_hold_episode_since == old
        assert _age() > BOUND, "the fault episode's clock runs on"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "fewer links than cabled"])
async def test_the_jobs_end_opens_a_fresh_hold_that_only_a_full_check_with_fabric_lifts(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """Goal 11: after the re-adopted job's 30 min, the hold opens with a fresh clock when it ends and the
    queue stays shut until the startup gate's full check, fabric included, passes. A fabric check that
    finds fewer links than the cabling expects is a fail: the hold and its clock stay."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _restart_broker(monkeypatch)
    _ledger_dir(monkeypatch, tmp_path)
    fresh = ServerFsm(tmp_path / "fsmdir" / "fsm.json", current_boot_id="boot-a")
    fresh.boot_merge(None)
    events, seen = _broker(monkeypatch, tmp_path, fresh, _FABRIC_OK if verdict == "pass" else _SHORT)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    monkeypatch.setattr(srv, "_kill_reset_owed", "")
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == "startup_unverified"
    job = _readopted_job(monkeypatch, tmp_path)

    srv._note_tenant_gate_verdict(srv._device_unavailable_for_tenant())
    assert not srv.device_hold_logged
    assert srv._device_unavailable_for_tenant() and not srv._queue_idle()

    await _job_ends(tmp_path, job)
    assert srv._device_unavailable_for_tenant(), "the job's end alone opens nothing"
    srv._note_tenant_gate_verdict(srv._device_unavailable_for_tenant())  # the next sampler tick
    assert srv.device_hold_logged and _age() < 60, "the clock starts at the job's end"
    since = srv.device_hold_episode_since

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    kinds = [kind for kind, _ in events]
    assert seen["fabric"] and all(seen["fabric"]), "the startup gate ran the full check with fabric"
    if verdict == "pass":
        assert not srv.device_hold_logged and srv.fsm.state is ServerState.HEALTHY
        assert "device_released" in kinds
        assert srv._device_unavailable_for_tenant() == "", "the queue opens only now"
    else:
        assert srv.device_hold_logged and srv.device_hold_episode_since == since
        assert srv.fsm.state is not ServerState.HEALTHY and "device_released" not in kinds
        assert srv._device_unavailable_for_tenant(), "a short link count keeps every job off the device"


@pytest.mark.asyncio
async def test_a_failed_startup_gate_after_the_job_gets_the_bound_ladder_by_600_s_after_its_end(
    rig, monkeypatch, tmp_path, clear_job_state  # noqa: F811
):
    """The re-adopted job ran 25 min, then the startup gate's full check failed. The hold's clock counts from
    the job's end: no ladder before the 10-min mark, the broker's own full ladder at it, and when the full
    check with fabric after it still fails, one power-cycle request. Nothing releases the hold."""
    order = rig["order"]
    job = _restarted_over_a_live_job(monkeypatch, tmp_path)
    for minutes in (0, 25):
        srv._note_tenant_gate_verdict(STARTUP)
        srv._check_hold_deadline(now=datetime.now() + timedelta(minutes=minutes))
    assert not srv.device_hold_logged

    await _job_ends(tmp_path, job)
    srv._note_tenant_gate_verdict(STARTUP)
    since = srv.device_hold_episode_since
    assert srv.device_hold_logged and _age() < 60
    checks = _bound_hold_with_a_failing_full_check(rig, monkeypatch)  # the startup gate's check failed
    monkeypatch.setattr(srv, "device_hold_episode_since", since)

    _tick(BOUND - 60)
    await _drain()
    assert "glx_reset" not in order and "power_cycle" not in order, "the 10-min mark is not reached yet"

    _tick(BOUND + 1)
    await _drain()
    assert "glx_reset" in order, "the full ladder ran at the 10-min mark after the job's end"
    assert checks and all(checks), "the check after the ladder is the full one, fabric included"
    assert order.count("power_cycle") == 1 and order.index("glx_reset") < order.index("power_cycle")
    assert "released" not in order and srv.device_hold_logged


@pytest.mark.asyncio
async def test_a_fault_hold_over_a_readopted_job_pages_once_and_runs_the_ladder_when_the_job_ends(
    rig, monkeypatch, tmp_path, clear_job_state  # noqa: F811
):
    """A fault hold open at the restart keeps its clock over the re-adopted job (spec 04 I82). Past the bound
    the running job keeps every reset off the device: paged once, naming it. On the first tick after the job
    ends the ladder runs, its full check fails, and the power cycle is asked for once."""
    order = rig["order"]
    _isolate(monkeypatch, tmp_path)
    checks = _bound_hold_with_a_failing_full_check(rig, monkeypatch)
    old = srv.device_hold_episode_since
    srv._persist_hold_episode(old)
    _restart_broker(monkeypatch)
    job = _readopted_job(monkeypatch, tmp_path)
    alerts = []
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: alerts.append(event))
    monkeypatch.setattr(srv, "device_hold_tenant_deferred", False)

    srv._note_tenant_gate_verdict(srv._device_unavailable_for_tenant())
    assert srv.device_hold_logged and srv.device_hold_episode_since == old
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    _tick()
    await _drain()

    kinds = [k for k, _ in rig["events"]]
    assert [a["kind"] for a in alerts].count("hold_bound_tenant_blocked") == 1
    assert kinds.count("hold_bound_tenant_blocked") == 1
    assert "glx_reset" not in order and "power_cycle" not in order, "no reset over the running job"

    job.status = srv.JobStatus.COMPLETED
    srv.readopted_scopes.clear()
    _tick(20)
    await _drain()

    assert "glx_reset" in order, "the ladder ran on the first tick after the job ended"
    assert checks and all(checks)
    assert order.count("power_cycle") == 1 and order.index("glx_reset") < order.index("power_cycle")
    assert "released" not in order


@pytest.mark.asyncio
async def test_the_deferred_startup_hold_does_not_take_a_closed_episodes_carry(monkeypatch, tmp_path, clear_job_state):
    """Spec 04 I82: a release saved a carry and the next job started inside its window; a restart 26 min
    later re-adopted that live job. The deferred startup hold takes no carry and escalates nothing. When the
    job then fails, the carry is pinned to the startup hold that opens after it, as to one already open:
    that hold starts its own clock and nothing escalates at once."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, _events, _detail = _open_hold(monkeypatch, "generic", 130)
    old_since = srv.device_hold_episode_since
    srv._note_tenant_gate_verdict("")  # reset + full check passed: released, carry saved
    srv._note_hold_carry_job("start")  # the next job starts inside the window
    shift = 1580.0
    srv._hold_carry.update(
        since=(datetime.fromisoformat(old_since) - timedelta(seconds=shift)).isoformat(),
        released=srv._hold_carry["released"] - shift,
        job_started=srv._hold_carry["job_started"] - shift,
    )
    srv._write_hold_carry()
    _restart_broker(monkeypatch)
    _startup_record(monkeypatch)
    job = _readopted_job(monkeypatch, tmp_path)
    seen = _record_events(monkeypatch)

    srv._note_tenant_gate_verdict(STARTUP)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert not srv.device_hold_logged and (tmp_path / srv.HOLD_CARRY_FILE).exists(), "no carry taken"

    await _job_ends(tmp_path, job, exit_code="1")
    assert json.loads((tmp_path / srv.HOLD_CARRY_FILE).read_text()).get("startup_pinned")
    srv._note_tenant_gate_verdict(STARTUP)
    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    kinds = [kind for kind, _ in seen]
    held = [k for kind, k in seen if kind == "device_held"]
    assert len(held) == 1 and "continues_episode" not in held[0] and held[0].get("carry_deferred")
    assert _age() < 60, "the startup hold starts its own clock"
    assert escalations == [], "no forced escalation before the hold's own 10-min mark"
    for false_alarm in _FALSE_ALARMS[1:]:
        assert false_alarm not in kinds
    assert not srv._hold_startup_deferred, "the deferral ended with the hold that opened"
