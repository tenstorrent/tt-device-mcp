# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A running tenant job's own off-bus reading is reported apart from a hold (spec 03 I88).

A job that resets the whole mesh itself takes every chip off the bus for a few seconds. With no hold
episode open, /health read that live probe as `held: true` (the latch took it the moment no broker op
was active), so pollers saw a hold the broker never opened. It now reads `held: false` with the reading
in `tenant_job_degraded`. A chip still off when the job ends opens the hold exactly as before, and only
a just-passed full check, fabric included, lifts it.
"""

from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tests.test_health_held_during_op import _isolate_hold
from tests.test_post_reset_gate import mesh  # noqa: F401  (fixture)
from tt_device_mcp import server as srv
from tt_device_mcp.health.post_reset import FABRIC_DEBT

OFF_BUS = "chip(s) [3] fell off the PCIe bus (reads return 0xFFFFFFFF)"
T0 = datetime(2026, 10, 23, 18, 24, 0)


@pytest.fixture
def running(mesh, monkeypatch):  # noqa: F811
    """A 32-chip host, the FSM healthy, no hold open, one tenant job RUNNING; the live probe is
    driven through the returned dict."""
    _isolate_hold(monkeypatch)
    patch_health_event(monkeypatch, lambda kind, **f: mesh.append((kind, f)))  # _isolate_hold drops events
    monkeypatch.setattr(srv, "tenant_job_degraded_since", "")
    live = {"reason": ""}
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: live["reason"])
    job = srv.Job(id="071", owner="tenant", workspace="/w", command="pytest", queued_at=T0.isoformat())
    job.status = srv.JobStatus.RUNNING
    srv.jobs["071"] = job
    return live, job, mesh


def _hold_rows():
    return [r for r in srv._get_queue_status()["running"] if r.get("op") == "hold"]


def test_a_running_jobs_own_off_bus_reading_is_not_a_hold(running):
    live, _job, events = running
    live["reason"] = OFF_BUS
    first = srv._health_payload(now=T0)
    later = srv._health_payload(now=T0 + timedelta(seconds=12))
    for payload in (first, later):
        assert payload["held"] is False and payload["status"] == "ok"
        assert "held_since" not in payload and "held_reason" not in payload
        assert payload["device_degraded"] == OFF_BUS, "the live reading stays visible"
    assert later["tenant_job_degraded"] == {"job_id": "071", "reason": OFF_BUS, "since": T0.isoformat()}
    assert srv.device_held_since == "", "no hold latch"
    assert srv.device_hold_logged is False and srv.device_hold_episode_since == "", "no hold episode"
    assert "device_held" not in [k for k, _ in events]
    assert srv._device_hold_state() is None and _hold_rows() == [], "no HOLD row in the jobs list"

    live["reason"] = ""  # the job's own reset brought the mesh back
    back = srv._health_payload(now=T0 + timedelta(seconds=14))
    assert back["held"] is False and back["tenant_job_degraded"] is None
    assert srv.tenant_job_degraded_since == ""


def test_a_chip_still_off_when_the_job_ends_opens_the_hold_and_only_a_full_check_lifts_it(running):
    live, job, events = running
    live["reason"] = OFF_BUS
    assert srv._health_payload(now=T0)["held"] is False

    job.status = srv.JobStatus.COMPLETED  # exit 0, chip 3 still off
    ended = srv._health_payload(now=T0 + timedelta(seconds=20))
    assert ended["held"] is True and ended["status"] == "degraded", "a hold, as before"
    assert ended["tenant_job_degraded"] is None
    assert ended["held_since"] == (T0 + timedelta(seconds=20)).isoformat(), "its clock starts at the job's end"

    # The post-job gate finds it off the bus: the hold episode opens.
    reason = "gate/post-job: 1/32 chip(s) off the bus — held"
    fsm_dirty(srv, reason, why="off_bus")
    srv._note_tenant_gate_verdict(f"device unverified: {reason}")
    assert srv.device_hold_logged is True and "device_held" in [k for k, _ in events]

    live["reason"] = ""  # the chip is back on the bus, nothing verified yet
    assert srv._health_payload()["held"] is True

    FABRIC_DEBT.note_evidence({"fabric": {"ok": False, "detail": "fabric check FAILED (exit 1)"}})
    assert srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy") is False
    assert srv._health_payload()["held"] is True, "a failed full check keeps the hold"

    FABRIC_DEBT.note_evidence({"fabric": {"ok": True, "detail": "fabric links healthy (53s)"}})
    assert srv._clear_device_dirty(verified=True, why="gate/post-job: verified healthy") is True
    srv._note_tenant_gate_verdict("")
    assert srv._health_payload()["held"] is False


def test_an_open_hold_episode_still_reads_held_while_a_job_runs(running):
    """An episode already open (a tenant hold, a startup hold behind a re-adopted job) is a hold."""
    live, _job, _events = running
    reason = "gate/post-job: 1/32 chip(s) off the bus — held"
    srv._note_tenant_gate_verdict(f"device unverified: {reason}")
    live["reason"] = OFF_BUS
    payload = srv._health_payload(now=T0)
    assert payload["held"] is True and payload["tenant_job_degraded"] is None


@pytest.mark.parametrize("record", ["fsm", "fault"])
def test_an_in_memory_verdict_under_a_running_job_still_reads_held(running, monkeypatch, record):
    """Only the live probe alone is the job's own reading; the FSM off HEALTHY or a reported runtime
    fault reads as held exactly as before."""
    live, _job, _events = running
    live["reason"] = OFF_BUS
    if record == "fsm":
        fsm_dirty(srv, "job 070 ended killed")
    else:
        monkeypatch.setattr(srv, "device_fault_reported", "dispatch timeout")
    payload = srv._health_payload(now=T0)
    assert payload["held"] is True and payload["tenant_job_degraded"] is None
