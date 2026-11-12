# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I69: after a preempted or cancelled job whose whole tree exited without SIGKILL, on a mesh that
reads every chip present after the I89 settle, the full check (fabric included) runs before any reset. A
pass releases with no reset; a fail runs the owed mesh reset, then the full check again, then the bounded
recovery. Every other kill (timeout, hung, a SIGKILL, startup debt) keeps reset-first.

The gate tests run the real probe pass on the kill-reset tests' 32-chip mesh (leaf probes stubbed)."""

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tests.test_eth_timeout_reset import ADVANCING, mesh  # noqa: F401 (fixture)
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState

RESET = [["tt-smi", "-glx_reset"]]


@pytest.fixture
def events(monkeypatch, mesh):  # noqa: F811
    rows = []

    def record(name, *a, **k):
        mesh["events"].append(name)
        rows.append((name, k))

    patch_health_event(monkeypatch, record)
    return rows


def _stopped(mesh, monkeypatch, *, end="preempted", requested=True, clean=True, job_id="9"):  # noqa: F811
    """The mesh after the broker stopped job ``job_id``: it owes the I69 reset, the runner noted the end."""
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 0)
    mesh["eth"] = ADVANCING
    job = srv.Job(id=job_id, owner="alice", workspace="/w", command="pytest", queued_at="")
    job.status = srv.JobStatus(end)
    job.stopped_clean = clean
    srv.jobs[job_id] = job
    fsm_dirty(srv, f"job {job_id} ended {end}")
    srv._owe_kill_reset(f"job {job_id} {end}")
    srv._note_kill_reset_job(
        {"job_id": job_id, "end": end, "stop_requested": requested, "survivors": [], "exited_at": srv.time.monotonic()}
    )
    return mesh


def _named(rows, name) -> list:
    return [k for n, k in rows if n == name]


async def _gate():
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)


# ---------------------------------------------------------------- the new path: check first


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["preempted", "killed"])
async def test_a_clean_stop_runs_the_full_check_and_a_pass_releases_with_no_reset(
    mesh, events, monkeypatch, end,  # noqa: F811
):
    _stopped(mesh, monkeypatch, end=end)
    await _gate()
    assert mesh["resets"] == [], "no reset after a clean stop on a healthy mesh"
    assert mesh["fabric"] == [0], "the full check ran its fabric pass before any reset"
    assert mesh["eth_reads"] >= 1
    deferred = _named(events, "kill_reset_deferred")
    assert len(deferred) == 1 and deferred[0]["job"] == "9" and deferred[0]["end"] == end and deferred[0]["why"]
    assert len(_named(events, "kill_reset_not_needed")) == 1
    assert "kill_mesh_reset" not in mesh["events"]
    assert srv.fsm.state is ServerState.HEALTHY, "the just-passed full check released it"
    assert srv._kill_reset_debt() == ""
    assert srv._dispatch_reset_spent is False, "no reset spent the shared latch"


@pytest.mark.asyncio
async def test_a_failed_fabric_check_after_a_clean_stop_resets_then_checks_again(
    mesh, events, monkeypatch,  # noqa: F811
):
    _stopped(mesh, monkeypatch)
    mesh["fabric_bad"] = True  # the reset stub heals it
    await _gate()
    assert mesh["resets"] == RESET, "one mesh reset after the failed check"
    assert mesh["fabric"][0] == 0 and mesh["fabric"][-1] == 1, "check, reset, check again"
    resets = _named(events, "kill_mesh_reset")
    assert len(resets) == 1 and resets[0]["why"].startswith("full check failed after a clean stop")
    assert not _named(events, "kill_reset_not_needed")
    assert srv.fsm.state is ServerState.HEALTHY, "the full check after the reset released it"
    assert srv._kill_reset_debt() == ""


@pytest.mark.asyncio
async def test_a_check_that_still_fails_after_the_reset_holds_and_climbs(
    mesh, events, monkeypatch, armed_power_cycle,  # noqa: F811
):
    _stopped(mesh, monkeypatch)
    mesh["fabric_bad"] = True
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True  # a prior cycle failed: the host rung is due
    await _gate()
    assert mesh["resets"][0] == RESET[0]
    assert srv.fsm.state is ServerState.RECOVERING, "a failed check never releases"
    assert srv._device_unavailable_for_tenant()
    assert armed_power_cycle == ["power-cycle"], "it goes on to the bounded recovery"


@pytest.mark.asyncio
async def test_a_fabric_check_that_cannot_run_counts_as_a_fail(mesh, events, monkeypatch):  # noqa: F811
    _stopped(mesh, monkeypatch)
    mesh["fabric_77"] = True
    await _gate()
    assert mesh["resets"] == RESET, "no verdict is no pass: the reset runs"
    assert not _named(events, "kill_reset_not_needed")
    assert srv.fsm.state is not ServerState.HEALTHY, "never released without a passed fabric check"


# ---------------------------------------------------------------- unchanged: reset first


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["timeout", "hung"])
async def test_timeout_and_hung_ends_reset_first(mesh, events, monkeypatch, end):  # noqa: F811
    _stopped(mesh, monkeypatch, end="killed", requested=False)
    srv._note_kill_reset_job({"job_id": "9", "end": end, "stop_requested": False, "survivors": [], "exited_at": 0.0})
    await _gate()
    assert mesh["resets"] == RESET
    assert mesh["fabric"] == [1], "no fabric pass before the reset"
    assert not _named(events, "kill_reset_deferred")
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_sigkill_at_the_bound_resets_first(mesh, events, monkeypatch):  # noqa: F811
    _stopped(mesh, monkeypatch, clean=False)
    await _gate()
    assert mesh["resets"] == RESET and mesh["fabric"] == [1]
    assert not _named(events, "kill_reset_deferred")


@pytest.mark.asyncio
async def test_a_survivor_the_quiesce_killed_resets_first(mesh, events, monkeypatch):  # noqa: F811
    _stopped(mesh, monkeypatch)
    real = srv._post_kill_quiesce

    async def quiesce(expected, log):
        q = await real(expected, log)
        return {**q, "killed": [4_000_000]}

    monkeypatch.setattr(srv, "_post_kill_quiesce", quiesce)
    await _gate()
    assert mesh["resets"] == RESET and mesh["fabric"] == [1]
    assert not _named(events, "kill_reset_deferred")


@pytest.mark.asyncio
async def test_startup_kill_debt_resets_first(mesh, events, monkeypatch):  # noqa: F811
    _stopped(mesh, monkeypatch)
    await srv._device_health_gate(None, phase="startup", run_fabric=False)
    assert mesh["resets"] == RESET
    assert not _named(events, "kill_reset_deferred")


# ---------------------------------------------------------------- the stop ladder's record


@pytest.mark.asyncio
@pytest.mark.parametrize("step,clean", [("SIGTERM", True), ("SIGINT (gone before SIGKILL)", True), ("SIGKILL", False)])
async def test_the_pgid_ladder_records_whether_it_sent_sigkill(monkeypatch, clear_job_state, step, clean):
    srv.jobs["5"] = srv.Job(id="5", owner="alice", workspace="/w", command="x", queued_at="")
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    monkeypatch.setattr(srv, "_pgid_pids", lambda pid: [pid])

    async def ladder(pid, grace_sec):
        return step

    monkeypatch.setattr(srv, "_terminate_process_group", ladder)
    await srv._terminate_job("5", 4_000_000)
    assert srv.jobs["5"].stopped_clean is clean


@pytest.mark.asyncio
@pytest.mark.parametrize("reaped,clean", [(False, True), (True, False)])
async def test_a_systemctl_stop_reap_counts_as_a_sigkill(monkeypatch, clear_job_state, reaped, clean):
    srv.jobs["5"] = srv.Job(id="5", owner="alice", workspace="/w", command="x", queued_at="")
    monkeypatch.setattr(srv, "_scope_active", lambda scope: True)

    async def scope_stop(scope, grace_sec):
        return reaped

    monkeypatch.setattr(srv, "_terminate_scope", scope_stop)
    await srv._terminate_job("5", None)
    assert srv.jobs["5"].stopped_clean is clean
