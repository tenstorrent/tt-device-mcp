"""Operator verification jobs (spec 01 I21), queue restore order (01 I6) and the fabric output an
incident bundle keeps (03 I57).

A fabric_unverified hold is lifted only by a fabric pass, and the operator's own run of that pass
used to sit behind the tenant jobs the hold was holding. These pin the class that runs first and
the one hold it may pass and lift.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest

from tests.test_device_safety import _free_device_lock, _no_workspace_activation, _wait_for
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import evidence as health

_HOLD = "device unverified: gate/post-job: enum+ARC healthy, fabric unverified (rc 77)"


@pytest.fixture
def broker_root(monkeypatch, tmp_path):
    """A broker root holding the two shipped checks. The fabric check leaves a marker when it ran."""
    root = tmp_path / "broker"
    root.mkdir()
    marker = tmp_path / "ran"
    for name in srv.VERIFICATION_CHECKS:
        p = root / name
        p.write_text(f"#!/bin/sh\necho {name} >> {marker}\nexit 0\n")
        p.chmod(0o755)
    monkeypatch.setenv("TTDEV_ROOT", str(root))
    return root


@pytest.fixture
def fabric_hold(monkeypatch):
    """A fabric-only hold: RECOVERING on fabric_unverified, nothing dirty, no fault, every chip on."""
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="gate/post-job: fabric unverified", dirty=False)
    assert srv._fabric_hold_only()


@pytest.fixture
def enum_arc(monkeypatch):
    """The broker's own enum+ARC read after a passing check: healthy unless a test says otherwise.
    ``calls`` records each read's run_fabric; ``during`` runs inside the read (a fault landing)."""
    state = {"healthy": True, "calls": [], "during": None}
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: ["0", "1"])
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 2)
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", 100.0)

    async def _observe(expected, log, *, run_fabric=True, **k):
        state["calls"].append(run_fabric)
        if state["during"]:
            state["during"]()
        return state["healthy"], {"snapshot": {"ok": state["healthy"]}}

    monkeypatch.setattr(srv.fsm, "observe", _observe)
    return state


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: seen.append((kind, f)))
    return seen


def _job(jid, command, peer_uid=None, queued_at=None, **k):
    job = srv.Job(
        id=jid,
        owner="op" if peer_uid == 0 else "tenant",
        workspace="/tmp",
        command=command,
        queued_at=queued_at or datetime.now().isoformat(),
        peer_uid=peer_uid,
        **k,
    )
    job.verification = srv.operator_verification_kind(command, peer_uid)
    return job


# --- classification ----------------------------------------------------------------------------


def test_only_an_operator_running_exactly_one_shipped_check_is_a_verification_job(monkeypatch, broker_root):
    fabric_cmd = f"{broker_root}/fabric-check.sh"
    eth_cmd = f"{broker_root}/python3 {broker_root}/eth-heartbeat-probe.py --chips 0"
    assert srv.operator_verification_kind(fabric_cmd, 0) == "fabric"
    assert srv.operator_verification_kind(eth_cmd, 0) == "eth"
    assert srv.operator_verification_kind(f"{broker_root}/eth-heartbeat-probe.py", 0) == "eth"
    # not an operator: a tenant, an unknown uid (no socket identity)
    assert srv.operator_verification_kind(fabric_cmd, 1000) == ""
    assert srv.operator_verification_kind(fabric_cmd, None) == ""
    monkeypatch.setenv(srv.OPERATOR_UIDS_ENV, "1001, 1000")
    assert srv.operator_verification_kind(fabric_cmd, 1000) == "fabric"
    # anything a shell could run more than the check with
    for cmd in (
        f"{fabric_cmd}; pytest",
        f"{fabric_cmd} && pytest",
        f"{fabric_cmd} | tee x",
        f"{fabric_cmd} $(id)",
        f"{fabric_cmd} `id`",
        f"{fabric_cmd} > /tmp/x",
        f"{fabric_cmd}\npytest",
        f"FOO=1 {fabric_cmd}",
        f"/usr/bin/python3 {broker_root}/eth-heartbeat-probe.py",
        f"{broker_root}/../fabric-check.sh",
        "/elsewhere/fabric-check.sh",
        "fabric-check.sh",
        f"{broker_root}/other.sh",
        f"'{fabric_cmd}",
        "",
    ):
        assert srv.operator_verification_kind(cmd, 0) == "", cmd


# --- queue order ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verification_job_queues_at_the_head_and_tenants_stay_fifo(clear_job_state, broker_root):
    cmd = f"{broker_root}/fabric-check.sh"
    order = [
        _job("001", "pytest a", peer_uid=1000),
        _job("002", "pytest b", peer_uid=1000),
        _job("003", cmd, peer_uid=0),
        _job("004", "pytest c", peer_uid=0),  # root, but not a check: a tenant job
        _job("005", cmd, peer_uid=0),
    ]
    for job in order:
        srv.jobs[job.id] = job
        await srv._enqueue_job(job)
    q = srv.get_job_queue()
    assert q.verification_waiting()
    assert [q.get_nowait() for _ in range(5)] == ["003", "005", "001", "002", "004"]
    assert not q.verification_waiting()


@pytest.mark.asyncio
async def test_a_job_handed_back_goes_first_among_the_tenants_behind_the_head(clear_job_state, broker_root):
    q = srv.get_job_queue()
    for job in (_job("010", "pytest b", peer_uid=1000), _job("011", f"{broker_root}/fabric-check.sh", peer_uid=0)):
        srv.jobs[job.id] = job
        await srv._enqueue_job(job)
    q.put_back_nowait("009")  # the held tenant job the runner had dequeued
    assert [q.get_nowait() for _ in range(3)] == ["011", "009", "010"]


@pytest.mark.asyncio
async def test_restore_orders_by_queue_time_then_id_across_the_wrap(monkeypatch, tmp_path, clear_job_state):
    """Filename order ran 000 (the id after 999) first, ahead of the 998 and 999 queued before it."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    t0 = datetime(2026, 10, 14, 12, 0, 0)
    for i, jid in enumerate(("998", "999", "000", "001")):
        srv._persist_queued_job(
            _job(jid, f"pytest {jid}", peer_uid=1000, queued_at=(t0 + timedelta(seconds=i)).isoformat())
        )
    # same queue time: the numeric id decides; an unreadable time sorts last
    srv._persist_queued_job(_job("005", "pytest 005", peer_uid=1000, queued_at=(t0 + timedelta(seconds=9)).isoformat()))
    srv._persist_queued_job(_job("004", "pytest 004", peer_uid=1000, queued_at=(t0 + timedelta(seconds=9)).isoformat()))
    srv._persist_queued_job(_job("002", "pytest 002", peer_uid=1000, queued_at="t"))
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()

    await srv._restore_queued_jobs()

    q = srv.get_job_queue()
    assert [q.get_nowait() for _ in range(7)] == ["998", "999", "000", "001", "004", "005", "002"]


@pytest.mark.asyncio
async def test_restore_puts_a_verification_job_back_at_the_head(monkeypatch, tmp_path, clear_job_state, broker_root):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    t0 = datetime(2026, 10, 14, 12, 0, 0)
    srv._persist_queued_job(_job("101", "pytest a", peer_uid=1000, queued_at=t0.isoformat()))
    v = _job("102", f"{broker_root}/fabric-check.sh", peer_uid=0, queued_at=(t0 + timedelta(seconds=1)).isoformat())
    srv._persist_queued_job(v)
    # a forged spec claiming the class is not believed: the class comes from command + submitter
    forged = tmp_path / srv.QUEUED_SPEC_DIR / "103.json"
    spec = json.loads((tmp_path / srv.QUEUED_SPEC_DIR / "101.json").read_text())
    spec.update(id="103", queued_at=(t0 + timedelta(seconds=2)).isoformat(), verification="fabric")
    forged.write_text(json.dumps(spec))
    srv.jobs.clear()
    srv.job_queue = None
    srv._ensure_async_primitives()

    await srv._restore_queued_jobs()

    q = srv.get_job_queue()
    assert [q.get_nowait() for _ in range(3)] == ["102", "101", "103"]
    assert srv.jobs["102"].verification == "fabric"
    assert srv.jobs["103"].verification == ""


# --- the hold ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoil",
    ["dirty", "other_why", "runtime_fault", "chip_off_bus", "device_op"],
)
def test_only_a_fabric_only_hold_counts(monkeypatch, fabric_hold, spoil):
    if spoil == "dirty":
        srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="job ended failed", dirty=True)
    elif spoil == "other_why":
        srv.fsm.on_fault("heartbeat", detail="chip 3 ARC frozen", dirty=False)
    elif spoil == "runtime_fault":
        monkeypatch.setattr(srv, "device_fault_reported", "job 7 named an eth timeout")
    elif spoil == "chip_off_bus":
        monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "chip 5 off the bus")
    else:
        monkeypatch.setattr(srv, "device_op_active", "fabric check")
    assert not srv._fabric_hold_only()


def _runner_setup(monkeypatch, tmp_path):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv.alert, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 0.02)

    async def _free(job_log_file):
        return ""

    async def _admission(job, job_log_file):
        return "" if srv.fsm.state is ServerState.HEALTHY else _HOLD

    async def _gate(job_log_file, job_failed=False, noop_failure=False, exit1_failure=False):
        return None

    monkeypatch.setattr(srv, "_await_device_free_for_tenant", _free)
    monkeypatch.setattr(srv, "_admission_verdict", _admission)
    monkeypatch.setattr(srv, "_verify_device_after_job", _gate)


def _queue(job, tmp_path):
    log = tmp_path / f"{job.id}.log"
    log.write_text("")
    job.log_file = str(log)
    srv.jobs[job.id] = job
    srv._persist_queued_job(job)


@pytest.mark.asyncio
async def test_a_held_tenant_yields_to_a_verification_job_that_lifts_the_hold(
    monkeypatch, tmp_path, clear_job_state, broker_root, fabric_hold, enum_arc, events
):
    """The whole path: a tenant job held on a fabric-only hold, an operator fabric check queued
    behind it. The tenant hands the runner back, the check runs through the hold, its pass lifts
    the hold, and the tenant then dispatches. Without the class the check waited behind the
    tenant until the hold bound refused it."""
    _runner_setup(monkeypatch, tmp_path)
    tenant = _job("201", "echo tenant-ran", peer_uid=1000)
    check = _job("202", f"{broker_root}/fabric-check.sh", peer_uid=0)
    _queue(tenant, tmp_path)
    await srv._enqueue_job(tenant)
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _wait_for(lambda: "[HELD]" in (tmp_path / "201.log").read_text())
        assert tenant.status is srv.JobStatus.QUEUED
        _queue(check, tmp_path)
        await srv._enqueue_job(check)
        await _wait_for(lambda: tenant.status is srv.JobStatus.COMPLETED, tries=400)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert check.status is srv.JobStatus.COMPLETED and check.exit_code == 0, check.error
    assert tenant.status is srv.JobStatus.COMPLETED
    assert datetime.fromisoformat(check.started_at) < datetime.fromisoformat(tenant.started_at)
    assert srv.fsm.state is ServerState.HEALTHY
    assert srv.health_monitor.last_fabric_ok is True
    assert enum_arc["calls"] == [False], "the broker read enum+ARC after the check, before the tenant"
    kinds = [k for k, _ in events]
    assert "verification_passes_hold" in kinds and "verification_lifted_hold" in kinds
    assert "[VERIFICATION]" in (tmp_path / "202.log").read_text()


@pytest.mark.asyncio
async def test_a_verification_job_passes_no_other_hold(
    monkeypatch, tmp_path, clear_job_state, broker_root, fabric_hold
):
    """A dirty device is a fault a check cannot clear: the operator's check is held like any job
    (here refused at once, with the hold off), and never runs."""
    _runner_setup(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD", "0")
    srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="job ended failed", dirty=True)
    check = _job("301", f"{broker_root}/fabric-check.sh", peer_uid=0)
    _queue(check, tmp_path)
    await srv._enqueue_job(check)
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _wait_for(lambda: check.status is not srv.JobStatus.QUEUED)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert check.status is srv.JobStatus.FAILED
    assert not (tmp_path / "ran").exists(), "the check ran through a dirty hold"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_held_tenant_does_not_yield_without_a_waiting_verification_job(
    monkeypatch, tmp_path, clear_job_state, fabric_hold
):
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 0.01)
    monkeypatch.setenv("TT_DEVICE_MCP_TENANT_HOLD_MAX_SEC", "0.05")
    monkeypatch.setattr(srv, "_escalate_held_job_past_bound", lambda *a, **k: "nothing escalated")

    async def _admission(job, job_log_file):
        return _HOLD

    monkeypatch.setattr(srv, "_admission_verdict", _admission)
    tenant = _job("401", "pytest", peer_uid=1000)
    reason = await srv._hold_job_until_device_fit(tenant, None, _HOLD)
    assert reason != srv.HOLD_YIELDED_TO_VERIFICATION and "hold bound" in reason


# --- the lift ------------------------------------------------------------------------------------


def _finished(job, exit_code=0, status=srv.JobStatus.COMPLETED):
    job.status, job.exit_code = status, exit_code
    job.reset_clock_at_start = srv.recovery_mechanism.last_reset_monotonic  # as the runner sets it
    return job


@pytest.mark.asyncio
async def test_a_passing_fabric_check_lifts_the_hold(broker_root, fabric_hold, enum_arc, events):
    """The check is the fabric half; the broker's own enum+ARC read is the other, and the hold
    lifts only once both passed with no reset since the check started (01 I21)."""
    job = _finished(_job("501", f"{broker_root}/fabric-check.sh", peer_uid=0))
    assert await srv._settle_verification_job(job)
    assert enum_arc["calls"] == [False], "one enum+ARC read, no second fabric pass"
    assert srv.fsm.state is ServerState.HEALTHY
    assert srv.health_monitor.last_fabric_ok is True
    assert [k for k, _ in events if k.startswith("verification_")] == ["verification_lifted_hold"]


@pytest.fixture
def debt_owed(monkeypatch):
    """A 2-chip mesh with a fabric check configured and the debt a broker start leaves (04 I32)."""
    from tt_device_mcp.health import post_reset

    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setattr(post_reset, "health_event", lambda kind, **f: None)
    monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "")
    srv.FABRIC_DEBT.owed = "broker start"
    return srv.FABRIC_DEBT


@pytest.mark.asyncio
async def test_a_passing_check_settles_the_post_reset_debt_and_lifts(
    monkeypatch, broker_root, fabric_hold, enum_arc, events, debt_owed
):
    """After a broker start or a reset the release is refused until a fabric pass settles the debt
    (04 I32). The verification job's pass, with no reset since it started, is that pass: it settles
    the debt and ends a known fabric fault, as the monitor's own pass would, and the hold lifts."""
    monkeypatch.setattr(srv.health_monitor, "fabric_known_fault", "known fabric fault: 2 jobs in a row")
    job = _finished(_job("507", f"{broker_root}/fabric-check.sh", peer_uid=0))
    assert await srv._settle_verification_job(job)
    assert srv.fsm.state is ServerState.HEALTHY
    assert debt_owed.owed == ""
    assert srv.health_monitor.last_fabric_ok is True
    assert srv.health_monitor.fabric_known_fault == ""
    kinds = [k for k, _ in events]
    assert "post_reset_release_refused" not in kinds
    assert "verification_lifted_hold" in kinds and "device_relift" in kinds


@pytest.mark.asyncio
async def test_a_refused_release_is_not_reported_as_a_lift(monkeypatch, broker_root, fabric_hold, enum_arc, events):
    """A release the post-reset gate refuses keeps the hold, and the journal says so: no lift and no
    relift is recorded for a device that is still held."""
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: False)
    job = _finished(_job("508", f"{broker_root}/fabric-check.sh", peer_uid=0))
    assert not await srv._settle_verification_job(job)
    assert srv.fsm.state is ServerState.RECOVERING
    kinds = [k for k, _ in events]
    assert "verification_not_lifted" in kinds
    assert "verification_lifted_hold" not in kinds and "device_relift" not in kinds


@pytest.mark.asyncio
async def test_a_pass_on_the_discovered_links_lifts_nothing(broker_root, fabric_hold, enum_arc, events, debt_owed):
    """A pass that only ran on the discovered links (a topology marker) proves no full mesh: it
    settles no debt and lifts nothing; the monitor's own pass handles the mismatch (03 I45)."""
    job = _finished(_job("509", f"{broker_root}/fabric-check.sh", peer_uid=0))
    job.output = "TOPOLOGY MISMATCH vs galaxy.yaml: 2 missing, 0 extra port connections\nPASSED\n"
    assert not await srv._settle_verification_job(job)
    assert srv.fsm.state is ServerState.RECOVERING
    assert debt_owed.owed == "broker start"
    assert srv.health_monitor.last_fabric_ok is None
    assert [k for k, _ in events if k.startswith("verification_")] == ["verification_not_lifted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["unhealthy", "reset_during_check", "reset_during_read", "fault_during_read", "never_started"]
)
async def test_a_passing_check_without_the_full_check_after_the_last_reset_does_not_lift(
    monkeypatch, broker_root, fabric_hold, enum_arc, events, case
):
    """A passing fabric check lifts nothing unless enum+ARC also read healthy and no reset ran since
    the check started; a failed read keeps the device held for the bounded recovery (01 I21)."""
    job = _finished(_job("506", f"{broker_root}/fabric-check.sh", peer_uid=0))
    if case == "unhealthy":
        enum_arc["healthy"] = False
    elif case == "reset_during_check":
        monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", 200.0)
    elif case == "reset_during_read":
        enum_arc["during"] = lambda: setattr(srv.recovery_mechanism, "last_reset_monotonic", 200.0)
    elif case == "fault_during_read":
        enum_arc["during"] = lambda: srv.fsm.on_fault("job_killed", detail="chip 1 isolated", dirty=True)
    else:
        job.reset_clock_at_start = None
    assert not await srv._settle_verification_job(job)
    assert enum_arc["calls"] == ([] if case in ("reset_during_check", "never_started") else [False])
    assert srv.fsm.state is ServerState.RECOVERING
    assert srv.health_monitor.last_fabric_ok is None
    assert [k for k, _ in events if k.startswith("verification_")] == ["verification_not_lifted"]


@pytest.mark.parametrize(
    "case",
    ["exit_1", "failed", "args", "eth", "tenant", "hold_changed"],
)
@pytest.mark.asyncio
async def test_nothing_else_lifts_the_hold(monkeypatch, broker_root, fabric_hold, enum_arc, case):
    cmd, uid, exit_code, status = f"{broker_root}/fabric-check.sh", 0, 0, srv.JobStatus.COMPLETED
    if case == "exit_1":
        exit_code = 1
    elif case == "failed":
        status = srv.JobStatus.FAILED
    elif case == "args":
        cmd += " --skip-traffic"
    elif case == "eth":
        cmd = f"{broker_root}/eth-heartbeat-probe.py"
    elif case == "tenant":
        uid = 1000
    job = _finished(_job("502", cmd, peer_uid=uid), exit_code, status)
    if case == "hold_changed":
        srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="job ended failed", dirty=True)
    assert not await srv._settle_verification_job(job)
    assert srv.fsm.state is not ServerState.HEALTHY
    assert srv.health_monitor.last_fabric_ok is None
    assert enum_arc["calls"] == []


@pytest.mark.asyncio
async def test_a_check_run_with_another_environment_does_not_lift(
    monkeypatch, broker_root, fabric_hold, enum_arc, events
):
    monkeypatch.setenv("TTDEV_FABRIC_DESCRIPTOR", "/opt/descriptors/galaxy.yaml")
    job = _finished(
        _job(
            "503", f"{broker_root}/fabric-check.sh", peer_uid=0, env_vars={"TTDEV_FABRIC_DESCRIPTOR": "/tmp/mine.yaml"}
        )
    )
    assert not await srv._settle_verification_job(job)
    assert srv.fsm.state is not ServerState.HEALTHY
    assert [k for k, _ in events] == ["verification_not_lifted"]
    assert events[0][1]["keys"] == ["TTDEV_FABRIC_DESCRIPTOR"]


@pytest.mark.asyncio
async def test_under_privsep_the_job_env_is_only_its_env_vars(monkeypatch, broker_root, fabric_hold, enum_arc):
    """A non-root job under privsep does not inherit the broker's environment, so a job whose
    env_vars leave out the broker's TTDEV_* settings ran another check; one that carries them lifts."""
    monkeypatch.setattr(srv, "should_privsep", lambda env=None: True)
    monkeypatch.setenv(srv.OPERATOR_UIDS_ENV, "1000")
    cmd = f"{broker_root}/fabric-check.sh"
    assert not await srv._settle_verification_job(_finished(_job("504", cmd, peer_uid=1000)))
    broker = {k: v for k, v in os.environ.items() if k.startswith("TTDEV_")}
    assert broker, "the suite sets TTDEV_* for the broker"
    assert await srv._settle_verification_job(_finished(_job("505", cmd, peer_uid=1000, env_vars=dict(broker))))
    assert srv.fsm.state is ServerState.HEALTHY


# --- incident fabric stamp -----------------------------------------------------------------------


def test_stale_fabric_output_is_left_out_of_the_bundle():
    since = datetime.now()
    bundle = health.capture_incident(
        "dirty", fabric_output="old pass", fabric_output_at=since - timedelta(minutes=5), fabric_since=since
    )
    assert not (bundle / "fabric.log").exists()
    meta = json.loads((bundle / "incident.json").read_text())
    assert meta["fabric_output"]["stale"] is True and meta["fabric_output"]["omitted"] is True
    assert meta["fabric_output"]["at"].endswith("Z")
    # no stamp at all is no proof the output is this pass's
    bundle = health.capture_incident("unhealthy", fabric_output="unknown pass", fabric_since=since)
    assert not (bundle / "fabric.log").exists()
    assert json.loads((bundle / "incident.json").read_text())["fabric_output"] == {
        "at": None,
        "stale": True,
        "omitted": True,
    }


def test_fresh_fabric_output_is_stamped_with_its_run_time():
    since = datetime.now()
    at = since + timedelta(seconds=3)
    bundle = health.capture_incident("dirty", fabric_output="link 7 down", fabric_output_at=at, fabric_since=since)
    text = (bundle / "fabric.log").read_text()
    stamp = health._utc_iso(at)
    assert text.splitlines()[0] == f"# fabric check run started {stamp}" and "link 7 down" in text
    assert json.loads((bundle / "incident.json").read_text())["fabric_output"] == {"at": stamp, "stale": False}


def test_a_caller_without_a_pass_start_keeps_the_old_bundle():
    bundle = health.capture_incident("dirty", fabric_output="as given")
    assert (bundle / "fabric.log").read_text() == "as given"
    assert "fabric_output" not in json.loads((bundle / "incident.json").read_text())


@pytest.mark.asyncio
async def test_the_monitor_stamps_its_fabric_output_with_the_run_start(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "echo links-ok; exit 0")
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output_at", None)
    before = datetime.now()
    await srv.health_monitor.verify_fabric_health(timeout_sec=10)
    at = srv.health_monitor.last_fabric_output_at
    assert at is not None and before <= at <= datetime.now()
    assert "links-ok" in srv.health_monitor.last_fabric_output
