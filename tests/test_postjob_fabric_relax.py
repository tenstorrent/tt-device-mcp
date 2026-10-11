"""Spec 03 I37: a clean exit-1 failure may skip the forced post-job fabric pass while a recent OK
pass is fresh. Device-free: every probe is stubbed."""

import asyncio
import logging
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_device_safety import (
    _clean_post_job_gate,
    _free_device_lock,
    _no_workspace_activation,
    _wait_for,
)
from tt_device_mcp import server as srv
from tt_device_mcp.constants import ETH_POST_JOB_TIMEOUT_SEC
from tt_device_mcp.fsm import ServerState

WINDOW = 3600.0


def _exit1_gate(monkeypatch, tmp_path, *, window=WINDOW, ok_age=30.0, eth=(True, "all active eth cores advancing")):
    """A post-job gate after a clean exit-1 failure, with an OK fabric pass `ok_age` seconds ago
    (None: no OK pass on record) and the window set to `window`."""
    calls = _clean_post_job_gate(monkeypatch, tmp_path, eth=eth, chips=4)
    monkeypatch.setattr(srv, "EXIT1_FABRIC_WINDOW_SEC", window)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", True)
    now = srv.time.monotonic()
    monkeypatch.setattr(srv, "last_fabric_ok_monotonic", 0.0 if ok_age is None else now - ok_age)
    srv.last_fabric_check_monotonic = now - (ok_age or 0.0)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    calls["events"] = events
    return calls


async def _post_job(log, exit1=True):
    await srv._device_health_gate(log, phase="post-job", run_fabric=False, force_fabric=True, exit1_failure=exit1)


# --- the gate -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_zero_forces_the_pass(monkeypatch, tmp_path):
    """The default (0) is today's behaviour: a failed job pays the full pass."""
    calls = _exit1_gate(monkeypatch, tmp_path, window=0.0)

    await _post_job(None)

    assert calls["fabric"] == 1


@pytest.mark.asyncio
async def test_fresh_ok_pass_and_clean_reads_skip_the_pass_and_say_so(monkeypatch, tmp_path):
    calls = _exit1_gate(monkeypatch, tmp_path)
    log = tmp_path / "gate.log"

    await _post_job(log)

    assert calls["eth"] == 1, "the cheap eth read did not run"
    assert calls["eth_timeout"] == ETH_POST_JOB_TIMEOUT_SEC
    assert calls["fabric"] == 0, "a clean exit-1 failure inside the window still paid the pass"
    assert calls["resets"] == 0
    assert srv.fsm.state is ServerState.HEALTHY
    text = log.read_text()
    assert "fabric: not run - exit 1 with clean reads, last OK pass 30s ago (window 3600s)" in text
    for verdict in ("fabric: SKIPPED", "fabric: OK", "fabric: UNHEALTHY"):
        assert verdict not in text, f"the skip reads as a fabric verdict: {text}"
    kinds = [f for kind, f in calls["events"] if kind == "exit1_fabric_not_run"]
    assert kinds and kinds[0]["window_sec"] == WINDOW and kinds[0]["phase"] == "post-job"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stale", "never-run", "reset-since", "voided", "last-unhealthy", "not-exit1"])
async def test_without_a_fresh_ok_pass_the_pass_runs(monkeypatch, tmp_path, case):
    calls = _exit1_gate(monkeypatch, tmp_path, ok_age=WINDOW + 1 if case == "stale" else 30.0)
    if case == "never-run":
        srv.last_fabric_ok_monotonic = 0.0
    elif case == "reset-since":
        srv.recovery_mechanism.last_reset_monotonic = srv.time.monotonic()
    elif case == "voided":
        srv._void_fabric_ok("test")
    elif case == "last-unhealthy":
        monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", False)

    await _post_job(None, exit1=case != "not-exit1")

    assert calls["fabric"] == 1, f"{case}: the forced pass was skipped"


@pytest.mark.asyncio
async def test_an_unhealthy_verdict_clears_the_stamp_and_an_ok_one_sets_it(monkeypatch, tmp_path):
    calls = _exit1_gate(monkeypatch, tmp_path, ok_age=None)
    before = srv.time.monotonic()

    await _post_job(None, exit1=False)

    assert calls["fabric"] == 1
    assert srv.last_fabric_ok_monotonic >= before, "an OK pass did not stamp"

    async def bad(*a, **k):
        calls["fabric"] += 1
        return False, "2 links down"

    async def reset(indices, log):
        calls["resets"] += 1
        return False

    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", bad)
    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    await _post_job(None, exit1=False)

    assert srv.last_fabric_ok_monotonic == 0.0, "an UNHEALTHY pass left the OK stamp standing"


@pytest.mark.asyncio
async def test_a_pass_with_no_verdict_does_not_refresh_the_stamp(monkeypatch, tmp_path):
    calls = _exit1_gate(monkeypatch, tmp_path, ok_age=None)

    async def cannot_check(*a, **k):
        calls["fabric"] += 1
        return None, "skipped (exit 77)"

    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", cannot_check)
    await _post_job(None, exit1=False)

    assert calls["fabric"] == 1
    assert srv.last_fabric_ok_monotonic == 0.0, "a SKIPPED pass stamped OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["dirty", "fault-reported", "disarmed"])
async def test_a_dirty_or_unreadable_device_still_pays_the_pass(monkeypatch, tmp_path, case):
    calls = _exit1_gate(monkeypatch, tmp_path)
    if case == "dirty":
        fsm_dirty(srv, "job 090 crashed", why="job_killed")
    elif case == "fault-reported":
        monkeypatch.setattr(srv, "device_fault_reported", "job 090 eth core never trained")
    else:
        monkeypatch.setattr(srv, "eth_check_armed", False)

    await _post_job(None)

    assert calls["fabric"] >= 1, f"{case}: the forced pass was skipped"


@pytest.mark.asyncio
async def test_a_frozen_eth_read_holds_without_a_pass_or_reset(monkeypatch, tmp_path):
    """The eth read that replaces the pass keeps its verdict (I16): a frozen core holds."""
    calls = _exit1_gate(monkeypatch, tmp_path, eth=(False, "a frozen active-eth core: 0-25"))

    await _post_job(None)

    assert calls["fabric"] == 0
    assert calls["resets"] == 0
    assert srv.fsm.record.why == "eth_frozen"
    assert srv._device_unavailable_for_tenant()
    assert not [e for e in calls["events"] if e[0] == "exit1_fabric_not_run"]


@pytest.mark.asyncio
async def test_an_eth_read_with_no_verdict_runs_the_pass(monkeypatch, tmp_path):
    calls = _exit1_gate(monkeypatch, tmp_path, eth=(None, "eth probe timed out after 9s"))

    await _post_job(None)

    assert calls["eth"] == 1
    assert calls["fabric"] == 1, "an eth read with no verdict bought the skip"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_failed_snapshot_goes_to_recovery(monkeypatch, tmp_path):
    calls = _exit1_gate(monkeypatch, tmp_path)

    async def short(expected):
        return False, f"3 of {expected} chips"

    monkeypatch.setattr(srv.health_monitor, "_verify_device", short)
    await _post_job(None)

    assert calls["resets"] >= 1, "a failed snapshot after an exit-1 job did not take the ladder"
    assert srv.last_fabric_ok_monotonic == 0.0, "the ladder left the OK stamp standing"
    assert not [e for e in calls["events"] if e[0] == "exit1_fabric_not_run"]


@pytest.mark.asyncio
async def test_the_startup_gate_is_unchanged(monkeypatch, tmp_path):
    calls = _exit1_gate(monkeypatch, tmp_path)

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True, exit1_failure=True)

    assert calls["fabric"] == 1


# --- what counts as a clean exit-1 failure ----------------------------------------------------------


def _job(status, exit_code):
    start = datetime(2026, 10, 10, 12, 0, 0)
    job = srv.Job(id="301", owner="tenant", workspace="/tmp", command="pytest t.py", queued_at="t")
    job.status = srv.JobStatus[status]
    job.exit_code = exit_code
    job.started_at = start.isoformat()
    job.finished_at = (start + timedelta(seconds=40)).isoformat()
    return job


@pytest.mark.parametrize(
    "status, exit_code, tail, expected",
    [
        ("FAILED", 1, "E   AssertionError: 3 != 4\n1 failed in 40s\n", True),
        ("FAILED", 1, "+++ Timeout (>300.0s) from pytest-timeout +++\n", False),
        ("FAILED", 1, "TT_THROW: TIMEOUT: device timeout, potential hang detected\n", False),
        ("FAILED", 1, "RuntimeError: Timed out waiting for completion\n", False),
        ("FAILED", 1, "Waiting for active ethernet core 0-25\n", False),
        ("FAILED", 1, "fabric router sync: timeout\n", False),
        ("FAILED", 1, None, False),  # no log: no evidence
        ("FAILED", 124, "AssertionError\n", False),
        ("FAILED", 134, "AssertionError\n", False),
        ("FAILED", -6, "AssertionError\n", False),
        ("FAILED", 2, "AssertionError\n", False),
        ("FAILED", True, "AssertionError\n", False),
        ("FAILED", None, "AssertionError\n", False),
        ("KILLED", 1, "AssertionError\n", False),
        ("TIMEOUT", 1, "AssertionError\n", False),
        ("HUNG", 1, "AssertionError\n", False),
        ("COMPLETED", 0, "1 passed\n", False),
    ],
)
def test_job_failed_exit1_clean(tmp_path, status, exit_code, tail, expected):
    log = None
    if tail is not None:
        log = tmp_path / "301.log"
        log.write_text("x" * 300000 + tail)
    assert srv._job_failed_exit1_clean(_job(status, exit_code), log) is expected


def test_only_the_log_tail_is_read(tmp_path):
    log = tmp_path / "301.log"
    log.write_text("Timed out once, early\n" + "x" * 300000 + "AssertionError\n")
    assert srv._job_failed_exit1_clean(_job("FAILED", 1), log) is True


# --- what clears the OK stamp -----------------------------------------------------------------------


def test_every_device_action_but_the_pass_clears_the_stamp(monkeypatch):
    monkeypatch.setattr(srv, "job_log_dir", None)
    for owner, cleared in [("[broker]fabric-check", False), ("[broker]reset", True), ("[broker]bridge-reset", True)]:
        monkeypatch.setattr(srv, "last_fabric_ok_monotonic", srv.time.monotonic())
        srv._begin_action_row(owner, "x")
        assert (srv.last_fabric_ok_monotonic == 0.0) is cleared, owner


def test_a_reset_after_the_ok_pass_makes_it_stale(monkeypatch):
    now = srv.time.monotonic()
    monkeypatch.setattr(srv, "last_fabric_ok_monotonic", now - 10)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", now - 20)
    assert srv._fabric_ok_age() is not None
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", now - 5)
    assert srv._fabric_ok_age() is None


def test_rung_inventory_reports_the_window(monkeypatch):
    monkeypatch.setattr(srv, "EXIT1_FABRIC_WINDOW_SEC", 900.0)
    lines = []
    monkeypatch.setattr(srv, "logger", logging.getLogger("t302"))
    monkeypatch.setattr(srv.logger, "info", lambda m, *a, **k: lines.append(m))
    srv.log_rung_inventory()
    inv = [m for m in lines if m.startswith("RUNG INVENTORY:")]
    assert inv and inv[0].endswith("exit1-fabric-window=900s")


# --- the runner -------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, expected, fault",
    [
        ("echo 'E   AssertionError'; exit 1", True, False),
        ("echo '+++ Timeout (>3.0s) from pytest-timeout +++'; exit 1", False, False),
        ("echo 'Waiting for active ethernet core'; exit 1", False, True),
        ("echo 'E   AssertionError'; exit 124", False, False),
        ("kill -6 $$", False, False),
    ],
)
async def test_the_runner_passes_a_clean_exit1_to_the_gate(
    monkeypatch, clear_job_state, tmp_path, command, expected, fault
):
    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    gates = []

    async def _gate(job_log_file, job_failed=False, noop_failure=False, exit1_failure=False):
        gates.append((job_failed, exit1_failure))

    monkeypatch.setattr(srv, "_verify_device_after_job", _gate)
    log = tmp_path / "302.log"
    log.write_text("")
    job = srv.Job(id="302", owner="tenant", workspace="/tmp", command=command, queued_at=datetime.now().isoformat())
    job.log_file = str(log)
    srv.jobs["302"] = job
    await srv.get_job_queue().put("302")

    runner = asyncio.create_task(srv.job_runner())
    try:
        await _wait_for(lambda: bool(gates), tries=400)
        assert gates == [(True, expected)], f"{command}: {gates} ({job.status.value}, exit {job.exit_code})"
        assert "EXCEPTION" not in (job.error or ""), job.error
        assert bool(srv.device_fault_reported) is fault
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
