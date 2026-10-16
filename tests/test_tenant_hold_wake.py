"""A release wakes a held tenant job at once; the wake never dispatches by itself (spec 03 I65).

A job held at the door re-ran its gate only every TENANT_HOLD_POLL_SEC (60 s). A job restored
across a broker restart parked under the startup hold sat up to a minute after the startup gate
had released the device. These pin that the release wakes the hold and that only the gate's own
verdict lets the job go."""

import asyncio
import time
from datetime import datetime

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.core import HealthState


def _held(monkeypatch, reasons: list[str]) -> list[float]:
    """Park the hold on a scripted gate: each pass pops the next verdict (the last one repeats).
    Returns the monotonic time of each gate pass."""
    monkeypatch.setattr(srv, "TENANT_HOLD_POLL_SEC", 60.0)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    passes: list[float] = []

    async def verdict(job, job_log_file):
        passes.append(time.monotonic())
        return reasons[0] if len(reasons) == 1 else reasons.pop(0)

    monkeypatch.setattr(srv, "_admission_verdict", verdict)
    return passes


def _job(job_id: str) -> "srv.Job":
    job = srv.Job(id=job_id, owner="tenant", workspace="/tmp", command="true", queued_at="t")
    srv.jobs[job_id] = job
    return job


def _gate_lifts_device() -> None:
    """The startup gate's verified pass: the episode closes to HEALTHY."""
    srv.fsm.on_readings(HealthState(phase="test", at=datetime.now(), expected=0))
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_startup_release_dispatches_a_held_job_within_a_second(monkeypatch, clear_job_state):
    """Job 668's case: held under startup_unverified, the startup gate lifts the device, and the
    job dispatches about a second later, not on the next 60 s poll."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    reasons = ["startup fabric verify has not passed yet"]
    passes = _held(monkeypatch, reasons)
    srv.fsm.on_fault("startup_unverified", detail="startup verify pending", dirty=False)

    job = _job("668")
    hold = asyncio.create_task(srv._hold_job_until_device_fit(job, None, reasons[0]))
    await asyncio.sleep(0.05)
    assert not hold.done()
    assert len(srv._hold_waiters) == 1

    reasons[0] = ""  # the gate now finds the device fit
    released = time.monotonic()
    _gate_lifts_device()
    result = await asyncio.wait_for(hold, timeout=5.0)

    assert result == "", result
    assert job.status is srv.JobStatus.QUEUED, "a woken hold must hand the job on, never cancel it"
    assert time.monotonic() - released < 2.0, "the held job waited for the poll, not the release"
    assert len(passes) == 1, "the wake must re-run the gate once, and only the gate releases the job"
    assert "device_released" in events
    assert not srv._hold_waiters, "a finished hold must drop its waiter"


@pytest.mark.asyncio
async def test_a_wake_while_still_held_does_not_dispatch(monkeypatch, clear_job_state):
    """A close to HEALTHY while the gate still holds for another reason (a fabric pass still owed)
    re-runs the gate, and the job stays held: a wake is never a verdict."""
    reasons = ["fabric check owed after reset"]
    passes = _held(monkeypatch, reasons)
    srv.fsm.on_fault("startup_unverified", detail="startup verify pending", dirty=False)
    job = _job("669")

    hold = asyncio.create_task(srv._hold_job_until_device_fit(job, None, reasons[0]))
    try:
        await asyncio.sleep(0.05)
        _gate_lifts_device()
        await asyncio.sleep(1.5)
        assert len(passes) == 1, "the wake did not re-run the gate"
        assert not hold.done(), "a wake while still held let the job go"
        assert job.status is srv.JobStatus.QUEUED

        # A burst of wakes re-runs the gate at most once a second, never a spin.
        for _ in range(50):
            srv._wake_held_jobs()
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        assert len(passes) <= 3, passes
        gaps = [b - a for a, b in zip(passes, passes[1:])]
        assert all(g >= srv.TENANT_HOLD_WAKE_MIN_GAP_SEC - 0.05 for g in gaps), gaps
        assert not hold.done()
    finally:
        hold.cancel()
        await asyncio.gather(hold, return_exceptions=True)
    assert not srv._hold_waiters, "a cancelled hold must drop its waiter"


@pytest.mark.asyncio
async def test_a_release_from_another_thread_wakes_the_hold(monkeypatch, clear_job_state):
    """Release paths can run in a worker thread (a gate step under to_thread): the wake must reach
    the loop the job waits on."""
    reasons = ["device held"]
    _held(monkeypatch, reasons)

    hold = asyncio.create_task(srv._hold_job_until_device_fit(_job("670"), None, reasons[0]))
    await asyncio.sleep(0.05)
    reasons[0] = ""
    await asyncio.to_thread(srv._wake_held_jobs)
    assert await asyncio.wait_for(hold, timeout=5.0) == ""
