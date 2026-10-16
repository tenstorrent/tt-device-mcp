# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A dirty device whose post-job gate a holder deferred is gated once the holder is gone (spec 03 I58).

Measured on a Galaxy: a sweep job was reaped hung at 300 s of silence, and its post-job gate found
the job's own exiting process still holding the device. The gate skipped, dropped the dirty mark and
held the device as an ordinary foreign-holder hold. Nothing ran until the idle escalation reset it,
past its 120 s grace and the relift's 120 s pacing: about 4 min to the reset and 5.7 min to the
release, over the 5 min budget for a hung job. Eight episodes in one afternoon. With the mark kept
the relift skips a dirty hold, so the fix is a trigger of its own: the first idle sampler tick after
the holder is gone runs the gate the hung job was owed.

Time to recover, from the holder's exit: one sampler tick (10 s) plus the gate's own full check
(~60 s), plus a reset (~30 s) and its post-reset check when the check fails. About 1.7 min at
worst, against 5.7 min before, and no longer tied to the escalation's grace or ceiling.
"""

import pytest

from tests.conftest import fsm_dirty
from tests.test_device_safety import _clean_post_job_gate
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.server import Job, JobStatus

HUNG = "job 529 ended hung (exit 130)"
EXITING = DeviceHolder(pid=870909, uid=1000)  # the hung job's own process, still closing its fds


@pytest.fixture
def box(monkeypatch, tmp_path, clear_job_state):
    """A 32-chip host, a fabric check configured, every probe stubbed. ``box["holders"]`` is the live
    holder scan; ``box["calls"]`` counts eth reads, fabric passes and resets; ``box["order"]`` lists
    the fabric passes and resets in the order they ran."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    calls = _clean_post_job_gate(monkeypatch, tmp_path, eth=(True, "all active eth cores advancing"), chips=32)
    state = {"holders": [EXITING], "leftover": "", "calls": calls, "order": [], "fabric": [(True, "links healthy")]}
    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=list(state["holders"]), complete=True)
    )
    monkeypatch.setattr(srv, "_reaped_leftover_reason", lambda scan=None: state["leftover"])
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "post_job_gate_pending", False)
    monkeypatch.setattr(srv, "_relift_task", None)
    monkeypatch.setattr(srv, "_holder_deferred_since", "", raising=False)

    async def fabric_pass(*a, **k):
        calls["fabric"] += 1
        state["order"].append("fabric")
        return state["fabric"].pop(0) if len(state["fabric"]) > 1 else state["fabric"][0]

    async def reset_cmd(argv, log):
        calls["resets"] += 1
        state["order"].append("reset")
        return 0, "reset ok"

    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", fabric_pass)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", reset_cmd)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    # The real reset-and-verify, over the stubbed reset command: its post-reset check is under test.
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    monkeypatch.delattr(srv.per_target_recovery, "_reset_and_verify_device")
    FABRIC_DEBT.owed = ""
    fsm_dirty(srv, HUNG, why="job_killed")
    yield state
    FABRIC_DEBT.owed = ""


async def _tick():
    """One idle sampler tick, and the task it spawned run to completion."""
    srv._maybe_spawn_idle_relift()
    if srv._relift_task is not None:
        await srv._relift_task


@pytest.mark.asyncio
async def test_a_hung_job_gate_deferred_by_its_own_exiting_process_runs_once_it_is_gone(box):
    """The measured episode, replayed: the gate skips over the holder, keeps the device dirty, and
    the first idle tick after the holder exits runs the full check with fabric. On base the skip
    dropped the dirty mark, so nothing ran until the escalation reset at its grace or ceiling."""
    await srv._verify_device_after_job(None, job_failed=True)
    assert box["calls"]["fabric"] == 0 and box["calls"]["resets"] == 0, "nothing runs beside the holder"
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.dirty, "the dirty mark must survive the skip"

    await _tick()
    assert box["calls"]["fabric"] == 0, "the holder is still there: no probe"
    assert srv.fsm.record.dirty

    box["holders"] = []
    srv.last_relift_monotonic = srv.time.monotonic()  # the relift's own pacing is not what runs it
    await _tick()
    assert box["calls"]["fabric"] == 1, "the first idle tick without the holder runs the full check, fabric included"
    assert srv.fsm.state is ServerState.HEALTHY, "a passing full check releases the device"


@pytest.mark.asyncio
async def test_a_failed_check_resets_and_releases_only_after_a_passing_fabric_pass(box):
    """Goal: the reset the deferred gate runs is followed by the full check, fabric included, before
    any release (spec 04 I32)."""
    box["fabric"] = [(False, "eth links down"), (True, "links healthy")]
    await srv._verify_device_after_job(None, job_failed=True)
    box["holders"] = []

    await _tick()

    assert box["order"] == ["fabric", "reset", "fabric"], "check, reset, then the post-reset fabric pass"
    assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""


@pytest.mark.asyncio
async def test_a_reset_whose_fabric_pass_fails_never_releases(box):
    box["fabric"] = [(False, "eth links down")]
    await srv._verify_device_after_job(None, job_failed=True)
    box["holders"] = []

    await _tick()

    assert box["calls"]["resets"] >= 1
    assert srv.fsm.state is not ServerState.HEALTHY, "no release without a passing fabric pass after the reset"


@pytest.mark.asyncio
async def test_a_reaped_jobs_leftover_defers_the_same_way(box):
    """The leftover fence (spec 01 I16) already kept the mark, and so stranded the device to the
    hold-deadline watchdog at the 600 s ceiling: the relift skips a dirty hold."""
    box["holders"] = [DeviceHolder(pid=870909, uid=1000, scoped=True)]
    box["leftover"] = "a reaped job's leftover holds the device: job 529 pid 870909"
    await srv._verify_device_after_job(None, job_failed=True)
    assert box["calls"]["fabric"] == 0 and srv.fsm.record.dirty

    await _tick()
    assert box["calls"]["fabric"] == 0, "no probe while the leftover holds it"

    box["holders"], box["leftover"] = [], ""
    await _tick()
    assert box["calls"]["fabric"] == 1 and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_queued_job_keeps_the_gate_to_its_own_pre_job_path(box):
    await srv._verify_device_after_job(None, job_failed=True)
    box["holders"] = []
    srv.jobs["q1"] = Job(
        id="q1", command="true", workspace="/tmp", owner="someone", queued_at="t", status=JobStatus.QUEUED
    )

    await _tick()

    assert box["calls"]["fabric"] == 0, "the queued job's own path waits the holder out and gates first"
    assert srv.fsm.record.dirty


@pytest.mark.asyncio
async def test_a_hold_that_was_never_dirty_keeps_the_fast_verify_shape(box):
    """A device held but not dirty keeps the I43 shape: an undirtied foreign-holder hold, no gate."""
    fsm_dirty(srv, "startup verify pending", why="foreign_holder", dirty=False)
    await srv._verify_device_after_job(None, job_failed=False)
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == "foreign_holder"
    assert not srv.fsm.record.dirty
    box["holders"] = []

    await _tick()

    assert box["calls"]["fabric"] == 0, "no deferred gate is owed to a hold that was never dirty"


@pytest.mark.asyncio
async def test_a_new_episode_never_inherits_an_old_deferral(box):
    await srv._verify_device_after_job(None, job_failed=True)
    box["holders"] = []
    assert srv._deferred_dirty_gate_armed()
    srv._holder_deferred_since = "2000-01-01T00:00:00"  # what an earlier episode left behind

    assert not srv._deferred_dirty_gate_armed(), "a deferral belongs to the episode that owed it"
