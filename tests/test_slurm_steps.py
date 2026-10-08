# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Externally-driven health-gate passes: the Slurm pre-step/post-step surface."""

import asyncio
import gc
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.conftest import fsm_dirty, fsm_healthy, patch_recovery
from tests.deploy_helpers import _one
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.socket_transport import PEERCRED_MARKER, current_peer_uid


def _present_chips(monkeypatch, tmp_path, n: int = 1):
    """Fake /dev/tenstorrent entries so device_health_gate's real probe path runs instead of
    taking its "no /dev/tenstorrent devices present -> skip" branch — the same TT_DEV_DIR
    mechanism test_device_safety.py's gate tests already rely on. Without this, every assertion
    here that depends on the gate actually reaching HealthMonitor.update()/_verify_device only
    held on a host that happens to have real Tenstorrent hardware: `conftest.py` sandboxes
    TT_DEV_DIR to an empty dir by default (mirroring PCI_DEVICES_DIR), so on a device-less CI
    runner these tests silently exercised only the empty-/dev skip branch and passed (or failed)
    for a reason unrelated to what they claim to test. This is what makes them independent of
    that default."""
    dev = tmp_path / "tenstorrent"
    dev.mkdir(exist_ok=True)
    for i in range(n):
        (dev / str(i)).mkdir(exist_ok=True)
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))


def _no_holders(monkeypatch):
    """No foreign tenant on the device, so the gate is free to act."""
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    # Module-level ladder state a sibling test file may have left non-empty; every existing
    # gate test resets these itself rather than relying on suite order (test_device_safety.py).
    srv.isolated_chips = set()
    srv.device_fault_reported = ""


def _quiet_gate(monkeypatch):
    """Strip the gate's side channels: no incident bundles, no device-op lock file."""
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)


def _record_escalations(monkeypatch) -> dict:
    """Count every trip into the recovery ladder — the gate's one action seam."""
    calls = {"n": 0}

    async def escalate(phase, indices, expected, log, **_):
        calls["n"] += 1
        return srv.OUTCOME_WAITING

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    return calls


@pytest.mark.asyncio
async def test_a_read_only_pass_never_enters_the_recovery_ladder(monkeypatch, clear_job_state, tmp_path):
    """with_recover=False is the whole contract of the prologue pass: an unhealthy verdict is
    reported, never acted on. A prologue is on the critical path of every job on every node of a
    multi-node allocation, so a pass that can climb the ladder can hold up a whole gang launch
    for minutes."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    calls = _record_escalations(monkeypatch)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 3 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)

    await srv.device_health_gate(None, phase="pre-step", run_fabric=False, with_recover=False)
    assert calls["n"] == 0, "a read-only gate pass entered the recovery ladder"


@pytest.mark.asyncio
async def test_a_read_only_pass_still_holds_an_unhealthy_device(monkeypatch, clear_job_state, tmp_path):
    """Read-only means it does not touch the DEVICE, not that it keeps its findings to itself.
    The FSM is the one truth about the mesh; a pass that saw a fault and recorded nothing would
    let the very next queue job dispatch onto it."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    _record_escalations(monkeypatch)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 3 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)

    await srv.device_health_gate(None, phase="pre-step", run_fabric=False, with_recover=False)
    assert srv.fsm.state is not srv.ServerState.HEALTHY, "an unhealthy read-only pass left the FSM healthy"


@pytest.mark.asyncio
async def test_a_read_only_pass_marks_the_fault_probe_unhealthy_not_job_killed(monkeypatch, clear_job_state, tmp_path):
    """`_mark_device_dirty` defaults to `why="job_killed"` — right for its job-triggered callers,
    wrong here: `device_health_gate` takes no `job`, so a silent default would journal a job crash
    that never happened, next to an empty `job: {}`, and inflate the `job_killed` bucket on
    `recovery_episodes_total`/`fault_state` for every unhealthy Slurm prologue/epilogue pass."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    _record_escalations(monkeypatch)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 3 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)

    await srv.device_health_gate(None, phase="pre-step", run_fabric=False, with_recover=False)
    assert srv.fsm.record.why == "probe_unhealthy", (
        f"expected why='probe_unhealthy', got {srv.fsm.record.why!r} — a read-only pass must "
        f"never be misattributed to a job that never ran"
    )


@pytest.mark.asyncio
async def test_a_recovering_pass_still_enters_the_ladder(monkeypatch, clear_job_state, tmp_path):
    """The default must not change behavior for the broker's own three call sites."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    srv.recovery_mechanism.last_reset_failed = False  # no cooldown standing in for the arm being off
    calls = _record_escalations(monkeypatch)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 3 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)

    await srv.device_health_gate(None, phase="post-step", run_fabric=False, with_recover=True)
    assert calls["n"] == 1, "the recovering pass did not reach the ladder"


@pytest.mark.asyncio
async def test_with_recover_defaults_on_so_existing_callers_are_unchanged(monkeypatch, clear_job_state, tmp_path):
    """The three production call sites pass no with_recover. If the default flipped, the broker
    would silently stop recovering between its own jobs."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    srv.recovery_mechanism.last_reset_failed = False
    calls = _record_escalations(monkeypatch)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 3 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)

    await srv.device_health_gate(None, phase="post-job", run_fabric=False)
    assert calls["n"] == 1, "the default gate pass stopped recovering"


@pytest.mark.asyncio
async def test_a_read_only_pass_never_runs_the_fabric_traffic_pass(monkeypatch, clear_job_state, tmp_path):
    """03 I12. The traffic pass is 45-100s on a healthy mesh and has been measured at 255s on a
    wedged one; the prologue's deadline cannot absorb that."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_dirty(srv, "left dirty by the last run", why="job_killed")
    _record_escalations(monkeypatch)
    seen = {}

    async def verify(expected, log, run_fabric=True, **_):
        seen["run_fabric"] = run_fabric
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv.device_health_gate(None, phase="pre-step", run_fabric=False, with_recover=False)
    assert seen["run_fabric"] is False, "a read-only pass ran the fabric traffic pass on a dirty device"


def test_the_verdict_is_ok_on_a_healthy_free_device(monkeypatch, clear_job_state):
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    v = srv._slurm_step_verdict(require_free=True)
    assert v["ok"] is True, f"a healthy free device was reported unfit: {v['reason']}"
    assert v["reason"] == "", "an ok verdict carried a reason"


def test_the_verdict_reports_the_fsm_hold_as_the_reason(monkeypatch, clear_job_state):
    _no_holders(monkeypatch)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    fsm_dirty(srv, "eth core e6-0 frozen", why="eth_frozen")

    v = srv._slurm_step_verdict(require_free=True)
    assert v["ok"] is False, "a held device was reported fit"
    assert "eth core e6-0 frozen" in v["reason"], f"the hold's detail is missing from {v['reason']!r}"


def test_a_foreign_holder_makes_the_device_not_free(monkeypatch, clear_job_state):
    """The site premise is that at prologue time the device is not busy. A tenant holding it is
    an anomaly there, and dispatching a job onto it would collide with whatever they are running."""
    from tt_device_mcp.device_holders import DeviceHolder

    monkeypatch.setattr("tt_device_mcp.device_holders.username_for_uid", lambda uid: "alovelace")
    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=4242, uid=1001)], complete=True)
    )
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    fsm_healthy(srv)

    v = srv._slurm_step_verdict(require_free=True)
    assert v["ok"] is False, "a device with a foreign holder was reported free"
    assert v["holders"] == [{"pid": 4242, "uid": 1001, "username": "alovelace"}]
    assert "4242" in v["reason"], f"the blocking pid is missing from {v['reason']!r}"


def test_infrastructure_holders_do_not_make_the_device_busy(monkeypatch, clear_job_state):
    """A uid below MIN_TENANT_UID is infrastructure (root, tt_telemetry_server) that survives a
    board reset. Treating it as a tenant would report every telemetry host permanently busy."""
    from tt_device_mcp.device_holders import DeviceHolder

    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=7, uid=113)], complete=True)
    )
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    fsm_healthy(srv)

    v = srv._slurm_step_verdict(require_free=True)
    assert v["ok"] is True, f"an infrastructure holder was read as a tenant: {v['reason']}"


def test_require_free_false_ignores_occupancy(monkeypatch, clear_job_state):
    """post-step asks only whether the mesh is fit; the allocation that just ended is allowed to
    still be shutting down."""
    from tt_device_mcp.device_holders import DeviceHolder

    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=4242, uid=1001)], complete=True)
    )
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    fsm_healthy(srv)

    v = srv._slurm_step_verdict(require_free=False)
    assert v["ok"] is True, f"require_free=False still refused on occupancy: {v['reason']}"


def test_an_incomplete_holder_scan_is_not_free(monkeypatch, clear_job_state):
    """04 I7's fail-closed rule: an unreadable holder may be a tenant."""
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=False))
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    fsm_healthy(srv)

    v = srv._slurm_step_verdict(require_free=True)
    assert v["ok"] is False, "an incomplete holder scan was reported free"
    assert "incomplete" in v["reason"], f"the reason does not name the blind spot: {v['reason']!r}"


def test_a_chip_off_the_bus_is_not_fit_even_with_a_healthy_fsm(monkeypatch, clear_job_state):
    """The live sysfs read catches what no in-memory flag records."""
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "chip(s) [3] fell off the PCIe bus")

    v = srv._slurm_step_verdict(require_free=True)
    assert v["ok"] is False, "a chip off the bus was reported fit"
    assert "off the PCIe bus" in v["reason"]


class _PeerUidShim:
    """TestClient never carries a real SO_PEERCRED, so PeerCredMiddleware reads every request as
    unauthenticated (scope["client"] is a plain testclient tuple, never the peercred marker).
    Stamp whatever this test's own ``current_peer_uid.set()`` established onto scope["client"]
    before the middleware sees it, so a route test can simulate a real socket peer."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            uid = current_peer_uid.get()
            if uid is not None:
                scope = {**scope, "client": (PEERCRED_MARKER, uid)}
        await self._app(scope, receive, send)


@pytest.fixture(autouse=True)
def _startup_already_ran(monkeypatch):
    """The first TestClient lifespan in a process runs run_startup_tasks(), whose startup gate
    pass calls the same patched _verify_device the step tests count. Run alone, a test would then
    see that startup call as its own step's gate pass. Mark startup done, as it already is for
    every test after the first in a full run, so only the step route's own calls are recorded."""
    monkeypatch.setattr(srv, "_startup_tasks_done", True)


def _client():
    return TestClient(_PeerUidShim(srv.build_asgi_app(srv.create_mcp_server())), raise_server_exceptions=False)


def test_both_step_routes_refuse_a_non_root_peer(monkeypatch, clear_job_state):
    """Both routes can reset the device and post-step can SIGKILL another user's processes. Slurm
    runs prologue and epilogue as root; a tenant has `reset` and `exec` for its own needs."""
    _quiet_gate(monkeypatch)
    tok = current_peer_uid.set(1001)
    try:
        with _client() as c:
            for route in ("/api/tt_device_pre_step", "/api/tt_device_post_step"):
                body = c.post(route, json={}).json()
                assert body["status"] == "refused", f"{route} admitted a non-root peer: {body}"
                assert "root" in body["reason"].lower(), f"{route} refusal does not say why: {body['reason']!r}"
    finally:
        current_peer_uid.reset(tok)

    # An unauthenticated caller (no peer credential at all -- current_peer_uid at its unset
    # default of None) must be refused identically to a non-root one. _step_caller_is_root has a
    # distinct `uid is None` branch for exactly this case; a refactor that mishandled it would
    # otherwise go uncaught, since every other test in this file authenticates as someone.
    assert current_peer_uid.get() is None, "a prior test leaked a peer uid into this one"
    with _client() as c:
        for route in ("/api/tt_device_pre_step", "/api/tt_device_post_step"):
            body = c.post(route, json={}).json()
            assert body["status"] == "refused", f"{route} admitted an unauthenticated caller: {body}"
            assert (
                "unauthenticated" in body["reason"].lower()
            ), f"{route} refusal does not name the unauthenticated case: {body['reason']!r}"


def test_pre_step_refuses_while_a_broker_job_is_in_flight(monkeypatch, clear_job_state):
    """The gate runs while the device is IDLE. A queued or running broker job means an external
    pass would probe underneath it."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    # Job's real fields: id is a "000".."999" string and queued_at is required (server.py:655).
    job = srv.Job(id="001", owner="jdoe", workspace="/tmp", command="echo hi", queued_at="2026-09-03T00:00:00")
    job.status = srv.JobStatus.RUNNING
    srv.jobs["001"] = job

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_pre_step", json={}).json()
        assert body["status"] == "refused", f"pre-step ran with a job in flight: {body}"
        assert "job" in body["reason"].lower()
    finally:
        current_peer_uid.reset(tok)


def test_a_step_refuses_while_a_broker_device_op_is_in_flight(monkeypatch, clear_job_state):
    """`device_op_active` covers the broker's own device work (a reset, a fabric pass) that runs
    with no queue job at all — the RUNNING/QUEUED scan alone is blind to it. A step probing the
    device underneath a live reset would race a `-glx_reset` call."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "device_op_active", "reset-tool")

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_pre_step", json={}).json()
        assert body["status"] == "refused", f"pre-step ran while a device op was in flight: {body}"
        assert "device op" in body["reason"].lower(), body["reason"]
    finally:
        current_peer_uid.reset(tok)


def test_the_in_flight_guard_catches_a_hung_jobs_teardown_window(monkeypatch, clear_job_state):
    """A job that just went HUNG stops being RUNNING before the runner finishes reaping it and
    clears `current_job_id` (server.py's runner sets HUNG, then later kills the process group,
    closes the log, and only then clears `current_job_id`). The RUNNING/QUEUED scan alone would
    read the device as idle for that whole window; `current_job_id` is what still catches it."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    job = srv.Job(id="002", owner="jdoe", workspace="/tmp", command="echo hi", queued_at="2026-09-03T00:00:00")
    job.status = srv.JobStatus.HUNG  # no longer RUNNING, but the runner has not reaped it yet
    srv.jobs["002"] = job
    monkeypatch.setattr(srv, "current_job_id", "002")

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_pre_step", json={}).json()
        assert body["status"] == "refused", f"pre-step ran during a hung job's teardown window: {body}"
    finally:
        current_peer_uid.reset(tok)


def test_post_step_re_checks_the_guard_after_the_reclaim_before_the_gate(monkeypatch, clear_job_state, tmp_path):
    """The reclaim's `to_thread` call yields the event loop — the one window in this route where a
    submission can be queued and dispatched. A guard checked only at entry would miss a job that
    landed in exactly that window; the gate must never run underneath it."""
    _present_chips(monkeypatch, tmp_path)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    def reclaim_then_a_job_lands(**_):
        # Simulate a submission dispatching while the reclaim held the event loop.
        srv.current_job_id = "003"
        return srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)

    monkeypatch.setattr(srv, "reclaim_foreign_holders", reclaim_then_a_job_lands)
    _no_holders(monkeypatch)

    gate_ran = []

    async def healthy(expected, log, run_fabric=True, **_):
        gate_ran.append(True)
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={}).json()
        assert gate_ran == [], f"the gate ran over a job that landed during the reclaim: {gate_ran}"
        assert body["status"] == "refused", f"post-step did not refuse the post-reclaim in-flight job: {body}"
    finally:
        current_peer_uid.reset(tok)
        srv.current_job_id = None


def test_pre_step_reports_ok_on_a_healthy_free_device(monkeypatch, clear_job_state, tmp_path):
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    _record_escalations(monkeypatch)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_pre_step", json={}).json()
        assert body["status"] == "ok", f"a healthy free device was not ok: {body}"
        assert body["ok"] is True
    finally:
        current_peer_uid.reset(tok)


def test_pre_step_never_recovers(monkeypatch, clear_job_state, tmp_path):
    """The route's whole contract. Asserted here as well as at the gate so a future refactor of
    the handler cannot quietly pass with_recover=True."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    calls = _record_escalations(monkeypatch)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chip 3 off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_pre_step", json={}).json()
        assert calls["n"] == 0, "pre-step entered the recovery ladder"
        assert body["status"] == "unfit", f"an unhealthy device was not reported unfit: {body}"
    finally:
        current_peer_uid.reset(tok)


def test_post_step_reclaims_then_runs_the_gate(monkeypatch, clear_job_state, tmp_path):
    """Order is the point: the gate refuses over a tenant, so the reclaim has to come first."""
    _present_chips(monkeypatch, tmp_path)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    order = []

    def fake_reclaim(**_):
        order.append("reclaim")
        return srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)

    monkeypatch.setattr(srv, "reclaim_foreign_holders", fake_reclaim)
    _no_holders(monkeypatch)

    async def healthy(expected, log, run_fabric=True, **_):
        order.append("gate")
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={}).json()
        assert order[:2] == ["reclaim", "gate"], f"wrong order: {order}"
        assert body["status"] == "ok", body
    finally:
        current_peer_uid.reset(tok)


def test_post_step_no_reclaim_skips_the_kill(monkeypatch, clear_job_state, tmp_path):
    _present_chips(monkeypatch, tmp_path)
    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    def boom(**_):
        raise AssertionError("reclaim ran with reclaim=false")

    monkeypatch.setattr(srv, "reclaim_foreign_holders", boom)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={"reclaim": False}).json()
        assert body["status"] == "ok", body
    finally:
        current_peer_uid.reset(tok)


def test_post_step_forces_the_fabric_pass_on_a_failed_step(monkeypatch, clear_job_state, tmp_path):
    """A fabric wedge does not show in the enum snapshot, but a job running across it fails — so
    a failure is exactly when the pass is worth its cost. A clean step pays nothing."""
    _present_chips(monkeypatch, tmp_path)
    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            c.post("/api/tt_device_post_step", json={"exit_code": 0})
            clean = list(seen)
            seen.clear()
            c.post("/api/tt_device_post_step", json={"exit_code": 1})
        assert clean == [False], f"a clean step paid for the fabric pass: {clean}"
        assert seen == [True], f"a failed step did not force the fabric pass: {seen}"
    finally:
        current_peer_uid.reset(tok)


def test_post_step_records_a_reclaim_that_signalled_something(monkeypatch, clear_job_state):
    """SIGKILLing another user's process is the one destructive thing this route does; it must
    leave a durable record like every other privileged device action (the reset stream, exec) —
    without it, a user whose process vanished has no way to learn why."""
    from tt_device_mcp.device_holders import DeviceHolder

    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    reclaimed_holder = DeviceHolder(pid=4242, uid=1001)
    monkeypatch.setattr(
        srv,
        "reclaim_foreign_holders",
        lambda **_: srv.ReclaimResult(signalled=[reclaimed_holder], survivors=[], scan_complete=True),
    )

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    events = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append((kind, f)))
    action_rows = []
    monkeypatch.setattr(
        srv, "write_action_log", lambda owner, command, runtime_sec, status, exit_code: action_rows.append(owner)
    )

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={}).json()
        assert body["status"] == "ok", body
        reclaim_events = [f for kind, f in events if kind == "straggler_reclaim"]
        assert len(reclaim_events) == 1, f"no single straggler_reclaim health_event was recorded: {events}"
        assert reclaim_events[0]["signalled"] == [
            {"pid": 4242, "uid": 1001, "username": reclaimed_holder.username}
        ], reclaim_events[0]
        assert "[broker]post-step" in action_rows, f"no action-log row for the reclaim: {action_rows}"
    finally:
        current_peer_uid.reset(tok)


def test_post_step_audits_a_reclaim_with_mixed_outcomes(monkeypatch, clear_job_state):
    """A run where root SIGTERMed one straggler, SIGKILLed a second, and a newcomer opened the
    device in between must not report an empty reclaim just because the SIGKILL round's residue
    is empty — that under-reports exactly the destructive action this audit exists to record.
    Exercises the real `reclaim_foreign_holders`, not a hand-built ReclaimResult, so the route's
    audit is proven against the function's actual round-by-round narrowing."""
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan
    from tt_device_mcp.device_holders import reclaim_foreign_holders as real_reclaim

    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    term_only = DeviceHolder(pid=100, uid=1001)
    escalated = DeviceHolder(pid=200, uid=1001)
    newcomer = DeviceHolder(pid=300, uid=1002)
    scans = [
        HolderScan(holders=[term_only, escalated], complete=True),
        HolderScan(holders=[escalated, newcomer], complete=True),
        HolderScan(holders=[newcomer], complete=True),
        HolderScan(holders=[newcomer], complete=True),  # final rescan
    ]

    def fake_reclaim(**_):
        # A fixed identity for every pid: without this, an unpatched read_starttime falls
        # through to the real /proc/<pid>/stat, and whether 100/200 exist as real processes on
        # the host running the suite (they do, as kernel threads, on a developer box; they do
        # not in a container) decides whether this reclaim signals anyone at all.
        return real_reclaim(
            grace_sec=0,
            kill=lambda pid, sig: None,
            sleep=lambda _: None,
            rescan=lambda: scans.pop(0),
            read_starttime=lambda pid: "fixed",
        )

    monkeypatch.setattr(srv, "reclaim_foreign_holders", fake_reclaim)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    events = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append((kind, f)))
    action_rows = []
    monkeypatch.setattr(
        srv, "write_action_log", lambda owner, command, runtime_sec, status, exit_code: action_rows.append(owner)
    )

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={}).json()
        # The newcomer is still on the device, so the gate correctly refuses this step (it must
        # not reset over it) — that refusal is orthogonal to the audit, which owes a record for
        # every pid this call actually signalled, independent of how the step ultimately verdicts.
        assert body["status"] == "refused", body
        signalled_pids = {h["pid"] for h in body["reclaimed"]}
        assert signalled_pids == {100, 200}, f"a signalled pid went unreported: {body['reclaimed']}"
        reclaim_events = [f for kind, f in events if kind == "straggler_reclaim"]
        assert len(reclaim_events) == 1, f"no straggler_reclaim health_event was recorded: {events}"
        assert "[broker]post-step" in action_rows, f"no action-log row for the reclaim: {action_rows}"
    finally:
        current_peer_uid.reset(tok)


def test_a_no_op_reclaim_writes_nothing(monkeypatch, clear_job_state):
    """A reclaim that signalled nobody has nothing to confess — the ledger must stay quiet, not
    grow a row for every clean post-step on every node of every job."""
    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    events = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append((kind, f)))
    action_rows = []
    monkeypatch.setattr(
        srv, "write_action_log", lambda owner, command, runtime_sec, status, exit_code: action_rows.append(owner)
    )

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={}).json()
        assert body["status"] == "ok", body
        assert not any(kind == "straggler_reclaim" for kind, _ in events), f"a no-op reclaim logged an event: {events}"
        assert "[broker]post-step" not in action_rows, f"a no-op reclaim wrote an action-log row: {action_rows}"
    finally:
        current_peer_uid.reset(tok)


def test_post_step_reports_a_surviving_straggler_as_refused(monkeypatch, clear_job_state):
    """An unkillable holder leaves the gate's fail-closed refusal standing; the epilogue must say
    so rather than report a clean device."""
    from tt_device_mcp.device_holders import DeviceHolder

    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    stuck = DeviceHolder(pid=4242, uid=1001)
    monkeypatch.setattr(
        srv,
        "reclaim_foreign_holders",
        lambda **_: srv.ReclaimResult(signalled=[stuck], survivors=[stuck], scan_complete=True),
    )
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[stuck], complete=True))

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={}).json()
        assert body["status"] == "refused", f"a surviving straggler was not refused: {body}"
        assert "4242" in body["reason"], body["reason"]
    finally:
        current_peer_uid.reset(tok)


def test_a_step_that_exceeds_its_deadline_is_inconclusive(monkeypatch, clear_job_state, tmp_path):
    """A wedged tt-smi -s has been measured timing out at 90s. A step must return a verdict, not
    hang until Slurm's PrologEpilogTimeout kills it — a killed script drains the node with no
    explanation of what was slow."""
    import asyncio

    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC", "0.05")

    async def slow(expected, log, run_fabric=True, **_):
        await asyncio.sleep(5)
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", slow)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_pre_step", json={}).json()
        assert body["status"] == "inconclusive", f"a deadline overrun was not inconclusive: {body}"
        assert body["ok"] is False
    finally:
        current_peer_uid.reset(tok)


def test_the_step_phase_labels_are_the_closed_set(monkeypatch, clear_job_state, tmp_path):
    """03 I5's discipline: phase is a label from a closed set, never free text from a request
    body — a caller-supplied phase would land in the durable journal and the metric labels."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    _record_escalations(monkeypatch)
    seen = []

    async def verify(expected, log, run_fabric=True, phase=None, **_):
        seen.append(phase)
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            c.post("/api/tt_device_pre_step", json={"phase": "../../etc/passwd"})
        assert seen == ["pre-step"], f"the request body steered the phase label: {seen}"
    finally:
        current_peer_uid.reset(tok)


def test_post_step_malformed_exit_code_does_not_500(monkeypatch, clear_job_state, tmp_path):
    """A step must always return a verdict, not a 500 traceback. `int("boom")` raising uncaught
    used to escape the route before the recovering pass ever ran."""
    _present_chips(monkeypatch, tmp_path)
    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            resp = c.post("/api/tt_device_post_step", json={"exit_code": "boom"})
        assert resp.status_code == 200, f"a malformed exit_code produced a {resp.status_code}, not a verdict"
        body = resp.json()
        assert body["status"] in ("ok", "unfit"), f"malformed exit_code did not yield a well-formed verdict: {body}"
    finally:
        current_peer_uid.reset(tok)


@pytest.mark.parametrize(
    "armed, exit_code, want_eth, want_fabric",
    [
        (True, 0, True, False),  # clean step, armed: the passive read, no traffic pass
        (False, 0, False, False),  # disarmed: the old snapshot-only clean exit
        (True, 1, False, True),  # failed step: the forced traffic pass, which reads eth itself
    ],
)
def test_a_clean_post_step_on_an_armed_host_asks_for_the_eth_read(
    monkeypatch, clear_job_state, tmp_path, armed, exit_code, want_eth, want_fabric
):
    """Spec 03 I30. A Slurm step that exits 0 hands the next job a mesh only enum+ARC looked at,
    the same hole the in-broker post-job gate closed. Through the real route, a clean step on an
    armed host asks the probe pass for the eth read and no traffic pass."""
    _present_chips(monkeypatch, tmp_path)
    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    srv.last_fabric_check_monotonic = srv.time.monotonic()  # a pass ran recently: none is owed
    monkeypatch.setattr(srv, "eth_check_armed", armed)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )
    seen = []

    async def verify(expected, log, run_fabric=True, run_eth=False, phase=None, **_):
        seen.append((phase, run_eth, run_fabric))
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            body = c.post("/api/tt_device_post_step", json={"exit_code": exit_code}).json()
        assert seen == [("post-step", want_eth, want_fabric)], f"post-step asked the probe pass for {seen}"
        assert body["status"] == "ok", body
    finally:
        current_peer_uid.reset(tok)


def test_post_step_non_boolean_reclaim_is_refused_not_silently_run(monkeypatch, clear_job_state):
    """Python's `bool("false")` is True: a naive coercion would read the JSON string "false" as
    "run the reclaim" and SIGTERM/SIGKILL another user's processes they explicitly asked to
    spare. An ambiguous `reclaim` must refuse, never silently pick the kill-enabled default."""
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    def boom(**_):
        raise AssertionError("reclaim ran on an unparseable reclaim value")

    monkeypatch.setattr(srv, "reclaim_foreign_holders", boom)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            resp = c.post("/api/tt_device_post_step", json={"reclaim": "false"})
        assert resp.status_code == 200, f"a non-boolean reclaim produced a {resp.status_code}, not a verdict"
        body = resp.json()
        assert body["status"] == "refused", f"a non-boolean reclaim was not refused: {body}"
    finally:
        current_peer_uid.reset(tok)


def test_pre_step_returns_a_verdict_not_a_500_when_the_gate_raises(monkeypatch, clear_job_state):
    """A step must always return a verdict (test_post_step_malformed_exit_code_does_not_500 makes
    the same promise for a malformed body); a gate that raises is the same contract from a
    different direction. Read-only: unlike its in-broker twin (_ensure_device_clean_for_next_job),
    this pass must NOT mark the device dirty on the strength of an exception it never diagnosed —
    that is the epilogue's job."""
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    why_before = srv.fsm.record.why

    async def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(srv, "device_health_gate", boom)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            resp = c.post("/api/tt_device_pre_step", json={})
        assert resp.status_code == 200, f"a raising gate produced a {resp.status_code}, not a verdict"
        body = resp.json()
        # Pinned to the exact verdict the except-block returns, not a tuple that also matches
        # "refused" — a leaked current_job_id/device_op_active from an earlier test (neither is
        # reset by clear_job_state) makes _broker_work_in_flight refuse before the gate is ever
        # called, and "refused" would pass this assertion without the stubbed gate ever raising.
        assert body["status"] == "inconclusive", f"not the gate-exception verdict: {body}"
        assert "gate exploded" in body["reason"], f"the reason does not name the raise: {body}"
        assert srv.fsm.record.why == why_before, "a read-only pass marked the device dirty on a gate exception"
    finally:
        current_peer_uid.reset(tok)


def test_post_step_returns_a_verdict_and_marks_dirty_when_the_gate_raises(monkeypatch, clear_job_state):
    """Same contract as the pre-step twin, but post-step's in-broker equivalent
    (_verify_device_after_job) marks the device dirty with why="gate_error" on a gate exception —
    the gate threw before it could clear the device, so the next tenant must not inherit a device
    nothing verified. post-step must match that, not the read-only pre-step's silence."""
    _quiet_gate(monkeypatch)
    _no_holders(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )

    async def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(srv, "device_health_gate", boom)

    tok = current_peer_uid.set(0)
    try:
        with _client() as c:
            resp = c.post("/api/tt_device_post_step", json={})
        assert resp.status_code == 200, f"a raising gate produced a {resp.status_code}, not a verdict"
        body = resp.json()
        assert body["status"] in ("refused", "unfit", "inconclusive"), f"not a well-formed verdict: {body}"
        assert srv.fsm.record.why == "gate_error", f"post-step did not mark the device dirty: {srv.fsm.record.why!r}"
    finally:
        current_peer_uid.reset(tok)


# --- the external-step reservation: findings A/B/C on the pre-step/post-step surface --------
#
# The mechanism under test: _reserve_external_step / _release_external_step / _run_step_gate
# (server.py) plus job_runner's own await on get_external_step_free_event(). These tests need a
# real job_runner() task and a step's own route handler making progress on the SAME event loop —
# TestClient cannot give us that (it drives the ASGI app from a second thread with its own loop,
# so a synchronous client.post() call blocks the test's thread, and a job_runner task on the
# test's own loop never gets to run while it does). _step_endpoints() below calls the route
# functions directly instead.


class _FakeStepRequest:
    """Enough of a Request for api_post_step: it only ever awaits request.json()."""

    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def _step_endpoints():
    mcp = srv.create_mcp_server()
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=False, host="0.0.0.0")
    routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "path")}
    return routes["/api/tt_device_pre_step"], routes["/api/tt_device_post_step"]


async def _call_pre_step(endpoint) -> dict:
    response = await endpoint(None)
    return json.loads(response.body)


async def _call_post_step(endpoint, body: dict) -> dict:
    response = await endpoint(_FakeStepRequest(body))
    return json.loads(response.body)


def _noop_post_job_gate(monkeypatch):
    """These tests are about the STEP's reservation, not the runner's own post-job gate; keep the
    runner off the device once its dispatched job finishes so nothing here depends on the real
    post-job gate's own timing."""

    async def _noop(job_log_file, job_failed=False, noop_failure=False):
        return None

    monkeypatch.setattr(srv, "_verify_device_after_job", _noop)


@pytest.mark.asyncio
async def test_a_job_queued_during_a_steps_gate_defers_dispatch_until_the_reservation_clears(
    monkeypatch, clear_job_state
):
    """Finding A: the in-flight check both step impls run is only a snapshot taken before the
    gate call. Without a reservation held for the gate's whole lifetime, a job queued right after
    that check could reach job_runner's dispatch while device_health_gate is still on its own
    initial holder scan — before it ever takes `_device_op`. This would fail on the unfixed code:
    the runner had nothing to wait on, so the job would dispatch during the sleep below instead
    of staying QUEUED."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    _noop_post_job_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    gate_started = asyncio.Event()
    gate_may_finish = asyncio.Event()

    async def slow_gate(job_log_file, *, phase, run_fabric, with_recover=True, force_fabric=False):
        gate_started.set()
        await gate_may_finish.wait()

    monkeypatch.setattr(srv, "device_health_gate", slow_gate)

    pre_step, _ = _step_endpoints()
    tok = current_peer_uid.set(0)
    runner = None
    try:
        step_task = asyncio.create_task(_call_pre_step(pre_step))
        await asyncio.wait_for(gate_started.wait(), timeout=2)

        job = srv.Job(id="920", owner="tenant", workspace="/tmp", command="echo raced", queued_at="t")
        srv.jobs["920"] = job
        await srv.get_job_queue().put("920")
        runner = asyncio.create_task(srv.job_runner())

        await asyncio.sleep(0.2)
        assert job.status is srv.JobStatus.QUEUED, (
            "a job queued while a step's gate was still running dispatched anyway — the "
            "reservation did not hold the runner off"
        )

        gate_may_finish.set()
        body = await asyncio.wait_for(step_task, timeout=2)
        assert body["status"] == "ok", body

        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert (
            job.status is not srv.JobStatus.QUEUED
        ), "the deferred job never dispatched once the step released its reservation"
    finally:
        current_peer_uid.reset(tok)
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_deferred_dispatch_is_visible_in_the_health_log_and_the_queue_status(monkeypatch, clear_job_state):
    """A step's reservation can defer dispatch for however long its own gate takes — for
    `post-step` that is unbounded by the step's own deadline once a ladder climb starts (03 I29).
    During an incident, an operator watching an empty-looking queue needs a line saying why
    nothing is dispatching, and `tt-device-mcp status` needs to name the holder — neither existed
    before this test's fix: job_runner deferred silently, and external_step_active was read by
    nothing outside the test suite."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    _noop_post_job_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    events = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append((kind, f)))

    gate_started = asyncio.Event()
    gate_may_finish = asyncio.Event()

    async def slow_gate(job_log_file, *, phase, run_fabric, with_recover=True, force_fabric=False):
        gate_started.set()
        await gate_may_finish.wait()

    monkeypatch.setattr(srv, "device_health_gate", slow_gate)

    pre_step, _ = _step_endpoints()
    tok = current_peer_uid.set(0)
    runner = None
    try:
        step_task = asyncio.create_task(_call_pre_step(pre_step))
        await asyncio.wait_for(gate_started.wait(), timeout=2)

        job = srv.Job(id="931", owner="tenant", workspace="/tmp", command="echo raced", queued_at="t")
        srv.jobs["931"] = job
        await srv.get_job_queue().put("931")
        runner = asyncio.create_task(srv.job_runner())

        # Give the runner a real chance to dequeue "931" and reach the reservation wait.
        for _ in range(200):
            deferred = [f for kind, f in events if kind == "job_deferred_for_external_step"]
            if deferred:
                break
            await asyncio.sleep(0.02)
        assert deferred, "job_runner deferred a job on the reservation but logged no health_event"
        assert deferred[0]["job"] == "931", deferred[0]
        assert deferred[0]["holder"] == "pre-step", deferred[0]

        status = srv._get_queue_status()
        assert status["external_step_active"] == "pre-step", status
        assert any("pre-step" in row.get("command", "") for row in status["running"]), status["running"]

        gate_may_finish.set()
        await asyncio.wait_for(step_task, timeout=2)
    finally:
        current_peer_uid.reset(tok)
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_post_step_reclaim_never_races_a_job_dispatched_in_its_scan_window(monkeypatch, clear_job_state):
    """Finding B: _broker_work_in_flight is only a snapshot taken before `await
    asyncio.to_thread(reclaim_foreign_holders)` — the `to_thread` hand-off itself yields the
    event loop before the worker thread's first scan runs, and a job queued in exactly that gap
    could dispatch, open the device, and be signalled by reclaim's very first round as if it were
    a stale straggler root just SIGTERM/SIGKILLed. The reservation must be held before the
    reclaim even starts, not just before the gate. This would fail on the unfixed code: the
    queued job would leave QUEUED (and get recorded as "killed") during the fake scan below."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    _noop_post_job_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    # Not queued yet — _broker_work_in_flight would otherwise refuse the step outright at entry
    # (03 I29: the gate never runs while a job is QUEUED or RUNNING), which is a DIFFERENT,
    # already-covered guarantee than this test's. This job is queued only after the step is
    # already past that check and into the reclaim's scan window — the race Finding B is about.
    job = srv.Job(id="921", owner="jdoe", workspace="/tmp", command="echo raced", queued_at="t")

    status_when_scanned = []
    kill_calls = []

    def fake_reclaim(**_):
        # Stands in for the worker thread's own first `rescan()` — runs in a real thread pool
        # thread (asyncio.to_thread), so the sleep here genuinely lets job_runner make progress
        # concurrently on the event loop while this "scan" is in flight.
        time.sleep(0.2)
        status_when_scanned.append(job.status)
        if job.status is not srv.JobStatus.QUEUED:
            kill_calls.append(job.id)  # what an unfixed reclaim would have signalled as a straggler
        return srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)

    monkeypatch.setattr(srv, "reclaim_foreign_holders", fake_reclaim)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)

    _, post_step = _step_endpoints()
    tok = current_peer_uid.set(0)
    runner = asyncio.create_task(srv.job_runner())
    try:
        step_task = asyncio.create_task(_call_post_step(post_step, {}))
        # Let the step run past its (clean-device) entry checks and reach the reclaim's
        # `to_thread` call — only THEN does the race window this test is about open.
        await asyncio.sleep(0)
        srv.jobs["921"] = job
        await srv.get_job_queue().put("921")
        # Job.status defaults to QUEUED (server.py), so the central assertion below would also
        # hold if this job were never really enqueued at all. Prove it was: nothing has run since
        # `put` returned to take it back off, so the queue must show it.
        assert srv.get_job_queue().qsize() >= 1, "job 921 was not actually enqueued"

        body = await asyncio.wait_for(step_task, timeout=2)
        assert status_when_scanned == [
            srv.JobStatus.QUEUED
        ], f"job 921 had already left QUEUED by the time reclaim's scan ran: {status_when_scanned}"
        assert (
            kill_calls == []
        ), f"reclaim would have signalled a job dispatched during its own scan window: {kill_calls}"
        # The post-reclaim re-check (kept for a concurrent reset-tool invocation — see
        # _post_step_impl) correctly sees the now-queued job and refuses the GATE over it; that is
        # a separate, already-covered guarantee (test_post_step_re_checks_the_guard_after_the_
        # reclaim_before_the_gate). What this test is about is that reclaim itself never touched
        # job 921 — asserted above — and that refusing here does not strand the job.
        assert body["status"] == "refused", body
        assert "921" in body["reason"], body["reason"]

        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert (
            job.status is not srv.JobStatus.QUEUED
        ), "the deferred job never dispatched once post-step released its reservation"
    finally:
        current_peer_uid.reset(tok)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_steps_deadline_leaves_the_gate_task_running_and_the_reservation_held(
    monkeypatch, clear_job_state, tmp_path
):
    """Finding C: the old `asyncio.wait_for` cancelled the gate coroutine at the deadline, which
    unwound its `async with _device_op(...)` and released the device lock while a
    `to_thread`-backed worker thread kept running underneath it. This would fail on the unfixed
    code two ways: the reservation/`external_step_active` would already be cleared by the time
    the deadline reply comes back, and the gate task would either not exist or be cancelled
    rather than still running."""
    _present_chips(monkeypatch, tmp_path)
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC", "0.05")

    gate_may_finish = asyncio.Event()

    async def slow(expected, log, run_fabric=True, **_):
        await gate_may_finish.wait()
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", slow)

    pre_step, _ = _step_endpoints()
    tok = current_peer_uid.set(0)
    try:
        body = await _call_pre_step(pre_step)
        assert body["status"] == "inconclusive", f"a deadline overrun was not inconclusive: {body}"

        assert srv.external_step_active == "pre-step", (
            "the reservation was released at the deadline instead of staying held for the " "still-running gate task"
        )
        assert (
            not srv.get_external_step_free_event().is_set()
        ), "the free event was set despite the gate task still running"
        assert (
            len(srv._step_background_tasks) == 1
        ), f"expected exactly one live gate task, found {srv._step_background_tasks}"
        task = next(iter(srv._step_background_tasks))
        assert not task.done(), "the gate task was not kept running past the deadline"

        # Let the still-running task actually finish — its own done-callback is what releases
        # the reservation, not this test, and not the reply that already went out above.
        gate_may_finish.set()
        await asyncio.wait_for(task, timeout=2)
        assert srv.external_step_active == "", "the reservation never cleared once the gate task finished"
        assert srv.get_external_step_free_event().is_set(), "the free event was not set once the gate task finished"
    finally:
        current_peer_uid.reset(tok)


@pytest.mark.asyncio
async def test_the_post_step_deadline_bounds_reclaim_too_not_just_the_gate(monkeypatch, clear_job_state):
    """Copilot finding: the deadline used to start only after `await
    asyncio.to_thread(reclaim_foreign_holders)` returned, so it bounded the gate but not the
    route — a reclaim with two SIGTERM/SIGKILL rounds and real grace sleeps in between could run
    well past what an 'inconclusive' reply promises to bound. The deadline is now one absolute
    reply deadline for the whole route (reclaim plus gate); reclaim is never cancelled mid-signal
    any more than the gate is. This would fail on the unfixed code: the route would wait for the
    fake reclaim below to actually finish (5s, or forever without the test's own safety timeout)
    instead of replying inconclusive at the 0.05s deadline."""
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setenv("TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC", "0.05")

    reclaim_started = threading.Event()
    reclaim_may_finish = threading.Event()

    def slow_reclaim(**_):
        # Runs in a real OS thread (asyncio.to_thread), standing in for the real grace sleeps
        # between SIGTERM and SIGKILL rounds -- a threading.Event, not asyncio's, because this
        # body has no event loop of its own to await one on.
        reclaim_started.set()
        reclaim_may_finish.wait(timeout=5)
        return srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)

    monkeypatch.setattr(srv, "reclaim_foreign_holders", slow_reclaim)

    _, post_step = _step_endpoints()
    tok = current_peer_uid.set(0)
    try:
        body = await _call_post_step(post_step, {})
        assert body["status"] == "inconclusive", f"a reclaim that outlasts the deadline was not inconclusive: {body}"
        assert reclaim_started.is_set(), "the reclaim never actually started"

        assert (
            srv.external_step_active == "post-step"
        ), "the reservation was released at the deadline instead of staying held for the still-running reclaim"
        assert (
            not srv.get_external_step_free_event().is_set()
        ), "the free event was set despite the reclaim still running"
        assert len(srv._step_background_tasks) == 1, srv._step_background_tasks
        task = next(iter(srv._step_background_tasks))
        assert not task.done(), "the reclaim task was not kept running past the deadline"

        # Let the still-running reclaim actually finish -- its own done-callback is what releases
        # the reservation, not this test, and not the reply that already went out above.
        reclaim_may_finish.set()
        await asyncio.wait_for(task, timeout=2)
        assert srv.external_step_active == "", "the reservation never cleared once the reclaim task finished"
        assert srv.get_external_step_free_event().is_set(), "the free event was not set once reclaim finished"
    finally:
        current_peer_uid.reset(tok)


@pytest.mark.asyncio
async def test_the_gate_tasks_done_callback_retrieves_the_exception_with_no_warning(monkeypatch, clear_job_state):
    """The done-callback added in _run_step_gate MUST call task.exception() (or .result()) even
    when it otherwise ignores it, or a garbage-collected task whose exception was never fetched
    logs "Task exception was never retrieved" — the exact traceback this project's own history
    already recorded once for the old wait_for-based version of this code (this would fail on a
    version of _run_step_gate that skips that call: the exception handler below would fire)."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )
    why_before = srv.fsm.record.why

    async def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(srv, "device_health_gate", boom)

    unretrieved = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: unretrieved.append(context))

    try:
        pre_step, post_step = _step_endpoints()
        tok = current_peer_uid.set(0)
        try:
            pre_body = await _call_pre_step(pre_step)
            assert pre_body["status"] == "inconclusive", pre_body
            assert srv.fsm.record.why == why_before, "pre-step marked the device dirty on a gate exception"

            post_body = await _call_post_step(post_step, {})
            assert post_body["status"] == "inconclusive", post_body
            assert (
                srv.fsm.record.why == "gate_error"
            ), f"post-step did not mark the device dirty on a gate exception: {srv.fsm.record.why!r}"
        finally:
            current_peer_uid.reset(tok)

        # Both tasks are already done and discarded from _step_background_tasks by now; force GC so a
        # task whose exception was never retrieved would fire the handler right here.
        gc.collect()
        await asyncio.sleep(0)
        assert not unretrieved, f"a gate task's exception was never retrieved: {unretrieved}"
    finally:
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_a_gate_that_outlives_its_deadline_and_then_raises_is_still_retrieved(monkeypatch, clear_job_state):
    """The scenario finding C is actually about: a wedged read hangs PAST the deadline, the route
    already replied 'inconclusive' without ever calling task.exception() itself (see
    _post_step_impl's timed_out branch), and the gate keeps running in the background — then
    fails. Only the done-callback's own retrieval stands between that and a "Task exception was
    never retrieved" traceback at GC; this test never awaits or reads the task's result/exception itself, so a callback
    that skipped the retrieval would leave it unfetched at GC and fail this test."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )
    monkeypatch.setenv("TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC", "0.05")
    why_before = srv.fsm.record.why

    async def hangs_then_raises(*a, **k):
        await asyncio.sleep(0.2)
        raise RuntimeError("gate exploded after the deadline")

    monkeypatch.setattr(srv, "device_health_gate", hangs_then_raises)

    unretrieved = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: unretrieved.append(context))

    try:
        _, post_step = _step_endpoints()
        tok = current_peer_uid.set(0)
        try:
            body = await _call_post_step(post_step, {})
            assert body["status"] == "inconclusive", body
            assert (
                srv.fsm.record.why == why_before
            ), "the reply-time path marked the device dirty before the background gate ever raised"
            assert len(srv._step_background_tasks) == 1, srv._step_background_tasks
            task = next(iter(srv._step_background_tasks))
        finally:
            current_peer_uid.reset(tok)

        # Poll .done() only — never .exception()/.result()/await on the task itself, so the only
        # thing that can retrieve its exception here is the done-callback.
        for _ in range(200):
            if task.done():
                break
            await asyncio.sleep(0.02)
        assert task.done(), "the background gate task never finished"
        del task

        assert srv.fsm.record.why == "gate_error", (
            f"the done-callback did not mark the device dirty once the background gate raised: "
            f"{srv.fsm.record.why!r}"
        )
        gc.collect()
        await asyncio.sleep(0)
        assert not unretrieved, f"a gate task's exception was never retrieved: {unretrieved}"
    finally:
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_the_done_callbacks_own_failure_does_not_leak_the_reservation(monkeypatch, clear_job_state):
    """The done-callback's cleanup (logging, _mark_device_dirty) does real work and can itself
    raise; asyncio swallows a done-callback's own exception into the loop's exception handler, so
    it never propagates to anyone who could notice. A release that only runs AFTER that work,
    not in a `finally`, would leak the reservation permanently on exactly this kind of error —
    wedging every future dispatch until the broker restarts. This would fail on the unfixed code:
    `external_step_active` would stay 'post-step' forever and the job queued below would never
    dispatch."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    _noop_post_job_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(
        srv, "reclaim_foreign_holders", lambda **_: srv.ReclaimResult(signalled=[], survivors=[], scan_complete=True)
    )

    async def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(srv, "device_health_gate", boom)

    def mark_dirty_boom(*a, **k):
        raise RuntimeError("_mark_device_dirty exploded too")

    monkeypatch.setattr(srv, "_mark_device_dirty", mark_dirty_boom)

    unhandled = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))

    _, post_step = _step_endpoints()
    tok = current_peer_uid.set(0)
    job = srv.Job(id="930", owner="jdoe", workspace="/tmp", command="echo raced", queued_at="t")
    runner = asyncio.create_task(srv.job_runner())
    try:
        body = await _call_post_step(post_step, {})
        assert body["status"] == "inconclusive", body
        # The callback's own failure must have actually happened (surfaced via the loop's
        # exception handler) or this test is not exercising the failure mode it claims to.
        assert unhandled, "the done-callback's own raise never reached the loop's exception handler"

        assert (
            srv.external_step_active == ""
        ), f"the reservation leaked after the done-callback's own body raised: {srv.external_step_active!r}"
        assert srv.get_external_step_free_event().is_set(), "the free event stayed cleared after the callback raised"
        assert srv._external_step_holders == {}, srv._external_step_holders

        # Prove dispatch is not wedged, not just that the flags look clear.
        srv.jobs["930"] = job
        await srv.get_job_queue().put("930")
        for _ in range(200):
            if job.status is not srv.JobStatus.QUEUED:
                break
            await asyncio.sleep(0.02)
        assert job.status is not srv.JobStatus.QUEUED, "dispatch stayed wedged after the callback's own failure"
    finally:
        current_peer_uid.reset(tok)
        loop.set_exception_handler(old_handler)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_second_step_waits_for_the_first_instead_of_overlapping(monkeypatch, clear_job_state):
    """Step routes serialize. An Epilog and the next Prolog used to both pass the in-flight check
    during the OTHER's pre-`_device_op` window (for post-step that window is the reclaim's grace
    sleeps) and both reserve. Overlapping that way, a prologue reads the epilogue's stragglers as
    foreign holders and reports the mesh unfit — draining the node and requeueing the job over a
    device seconds from being handed over clean."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")

    first_started = asyncio.Event()
    first_may_finish = asyncio.Event()
    second_started = asyncio.Event()

    async def gate_one(job_log_file, *, phase, run_fabric, with_recover=True, force_fabric=False):
        first_started.set()
        await first_may_finish.wait()

    async def gate_two(job_log_file, *, phase, run_fabric, with_recover=True, force_fabric=False):
        second_started.set()

    pre_step, _ = _step_endpoints()
    tok = current_peer_uid.set(0)
    try:
        monkeypatch.setattr(srv, "device_health_gate", gate_one)
        task_one = asyncio.create_task(_call_pre_step(pre_step))
        await asyncio.wait_for(first_started.wait(), timeout=2)

        monkeypatch.setattr(srv, "device_health_gate", gate_two)
        task_two = asyncio.create_task(_call_pre_step(pre_step))
        # Long enough that an overlapping implementation would certainly have started its gate.
        await asyncio.sleep(0.2)
        assert not second_started.is_set(), "the second step ran its gate while the first still held the device"
        assert len(srv._external_step_holders) == 1, srv._external_step_holders

        first_may_finish.set()
        await asyncio.wait_for(task_one, timeout=2)
        body_two = await asyncio.wait_for(task_two, timeout=5)
        assert second_started.is_set(), "the second step never ran once the first released"
        assert body_two["status"] == "ok", f"the second step did not get a clean verdict: {body_two}"
        assert srv.get_external_step_free_event().is_set(), "the device stayed reserved after both steps released"
        assert srv._external_step_holders == {}, srv._external_step_holders
    finally:
        current_peer_uid.reset(tok)


async def test_a_step_that_waits_past_its_deadline_reports_inconclusive(monkeypatch, clear_job_state):
    """Waiting is bounded by the route's own deadline. A step still holding the device when it
    expires means a recovery really is in progress, so 'not ready' is the true answer — but it
    must be reported as inconclusive, never as a gate verdict the prologue never actually ran."""
    _no_holders(monkeypatch)
    _quiet_gate(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "_device_liveness_reason", lambda: "")
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC", "0.1")

    first_started = asyncio.Event()
    first_may_finish = asyncio.Event()
    second_gate_ran = {"n": 0}

    async def gate_one(job_log_file, *, phase, run_fabric, with_recover=True, force_fabric=False):
        first_started.set()
        await first_may_finish.wait()

    async def gate_two(job_log_file, *, phase, run_fabric, with_recover=True, force_fabric=False):
        second_gate_ran["n"] += 1

    pre_step, _ = _step_endpoints()
    tok = current_peer_uid.set(0)
    try:
        monkeypatch.setattr(srv, "device_health_gate", gate_one)
        task_one = asyncio.create_task(_call_pre_step(pre_step))
        await asyncio.wait_for(first_started.wait(), timeout=2)

        monkeypatch.setattr(srv, "device_health_gate", gate_two)
        body = await asyncio.wait_for(_call_pre_step(pre_step), timeout=5)

        assert body["status"] == "inconclusive", f"a wait timeout was not inconclusive: {body}"
        assert body["ok"] is False
        assert "still held the device" in body["reason"], body["reason"]
        assert second_gate_ran["n"] == 0, "the second step probed anyway after giving up on the wait"
    finally:
        first_may_finish.set()
        await asyncio.wait_for(task_one, timeout=2)
        current_peer_uid.reset(tok)


def test_the_reservation_refcounts_so_one_release_cannot_free_another_holder():
    """Kept as a unit property even though the step routes now serialize and cannot produce two
    simultaneous holders: the release path is token-keyed so a second holder (a future caller, or
    a handed-off background task) is never freed by someone else's release, and so a DOUBLE
    release is a no-op rather than an under-count that frees the device out from under a live
    holder."""
    assert srv._external_step_holders == {}, "a prior test leaked a reservation"
    first = srv._reserve_external_step("pre-step")
    second = srv._reserve_external_step("post-step")
    assert len(srv._external_step_holders) == 2
    assert not srv.get_external_step_free_event().is_set()

    srv._release_external_step(first)
    assert not srv.get_external_step_free_event().is_set(), "releasing one holder freed the device for both"
    srv._release_external_step(first)  # a double release must change nothing
    assert not srv.get_external_step_free_event().is_set(), "a double release under-counted and freed the device"
    assert len(srv._external_step_holders) == 1

    srv._release_external_step(second)
    assert srv.get_external_step_free_event().is_set(), "the device stayed reserved after every holder released"
    assert srv._external_step_holders == {}


DEPLOY_SLURM = Path(__file__).resolve().parents[1] / "deploy" / "slurm"


def _stub_cli_env(tmp_path, body):
    """A stand-in tt-device-mcp reachable only via TTDEV_VENV, plus a TTDEV_ETC_DEFAULT pointed
    at a config that does not exist. The hooks resolve the pinned absolute install FIRST (finding
    1 of the fix rounds: PATH is never trusted first for a root-run Slurm hook), so a stub placed
    only on PATH would be silently shadowed by a real /opt/tt-device-broker install if this
    machine happens to have one — which it does. TTDEV_ETC_DEFAULT keeps the real
    /etc/default/tt-device-broker from ever being sourced, so this env's own TTDEV_VENV is what
    resolution actually uses."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "tt-device-mcp"
    stub.write_text(body)
    stub.chmod(0o755)
    return {
        "PATH": "/usr/bin:/bin",
        "TTDEV_ETC_DEFAULT": str(tmp_path / "no-such-etc-default"),
        "TTDEV_VENV": str(tmp_path / "venv"),
    }


def test_the_slurm_hooks_exist_and_are_executable():
    for name in ("prolog.sh", "epilog.sh"):
        p = DEPLOY_SLURM / name
        assert p.is_file(), f"{name} is missing"
        assert p.stat().st_mode & 0o111, f"{name} is not executable; slurmd will not run it"


def test_the_slurm_hooks_are_shell_clean():
    """A syntax error in a Prolog drains every node it runs on."""
    for name in ("prolog.sh", "epilog.sh"):
        r = subprocess.run(["bash", "-n", str(DEPLOY_SLURM / name)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


def test_the_hooks_propagate_the_cli_exit_code(tmp_path):
    """The whole contract with Slurm is the exit code. A hook that swallows it turns a wedged
    device into a job that runs anyway."""
    env = {**_stub_cli_env(tmp_path, "#!/bin/sh\nexit 3\n"), "SLURM_JOB_EXIT_CODE": "0"}
    for name in ("prolog.sh", "epilog.sh"):
        r = subprocess.run(["bash", str(DEPLOY_SLURM / name)], env=env, capture_output=True, text=True)
        assert r.returncode == 3, f"{name} returned {r.returncode}, not the CLI's 3"


def test_the_hooks_export_their_deadline_var_from_etc_default(tmp_path):
    """slurmd builds Prolog/Epilog a fresh environment rather than propagating its own service
    environment, and /etc/default/tt-device-broker uses plain KEY=value with no `export` -- so a
    site raising the deadline only in that file must still see it reach the CLI child each hook
    execs. This would fail without the hook's own `export` line: the stub below would see an
    empty variable, not the value set in the sourced config."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "tt-device-mcp"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "PRE=$TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC POST=$TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    etc_default = tmp_path / "tt-device-broker-defaults"
    etc_default.write_text(
        f"TTDEV_VENV={tmp_path / 'venv'}\n"
        "TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC=222\n"
        "TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC=999\n"
    )
    env = {"PATH": "/usr/bin:/bin", "TTDEV_ETC_DEFAULT": str(etc_default), "SLURM_JOB_EXIT_CODE": "0"}

    r = subprocess.run(["bash", str(DEPLOY_SLURM / "prolog.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "PRE=222" in r.stdout, f"prolog.sh did not export its deadline var to the CLI: {r.stdout!r}"

    r = subprocess.run(["bash", str(DEPLOY_SLURM / "epilog.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "POST=999" in r.stdout, f"epilog.sh did not export its deadline var to the CLI: {r.stdout!r}"


def test_the_hooks_do_not_require_the_deadline_var_to_be_set(tmp_path):
    """The `export` must be harmless when the site never set the var at all -- a bare `export
    NAME` with no assignment must not itself fail under `set -u`, and must not manufacture an
    empty-string override that shadows the CLI's own default."""
    env = {**_stub_cli_env(tmp_path, '#!/bin/sh\necho "PRE=[$TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC]"\nexit 0\n')}
    r = subprocess.run(["bash", str(DEPLOY_SLURM / "prolog.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "PRE=[]" in r.stdout, f"an unset deadline var should reach the CLI as unset, not fail the hook: {r!r}"


def test_the_epilog_passes_slurms_job_exit_code_through():
    """Slurm knows how the step ended; the broker does not. That bit is what forces the fabric
    traffic pass on a failure and keeps a clean step free of it."""
    text = (DEPLOY_SLURM / "epilog.sh").read_text()
    assert "SLURM_JOB_EXIT_CODE" in text, "the epilog does not read Slurm's job exit code"
    assert "--exit-code" in text, "the epilog does not pass --exit-code to post-step"


def test_the_hooks_fail_loudly_when_the_cli_cannot_be_resolved(tmp_path):
    """Slurm's own docs: Prolog/Epilog run with no search path. A hook that falls back to a bare
    command name exits 127 with bash's generic 'exec: tt-device-mcp: not found' — which already
    happens to name the binary, so a plain substring check on 'tt-device-mcp' would not catch a
    regression back to the old bare `exec`. Assert on the hook's OWN distinguishing phrase
    instead, which only the explicit resolution-failure branch emits. TTDEV_ETC_DEFAULT/TTDEV_VENV
    are pointed at paths that cannot exist so this is deterministic even on a host with a real
    broker already installed."""
    env = {
        "PATH": "/usr/bin:/bin",
        "TTDEV_ETC_DEFAULT": str(tmp_path / "no-such-etc-default"),
        "TTDEV_VENV": str(tmp_path / "no-such-venv"),
    }
    for name in ("prolog.sh", "epilog.sh"):
        r = subprocess.run(["bash", str(DEPLOY_SLURM / name)], env=env, capture_output=True, text=True)
        assert r.returncode != 0, f"{name} exited 0 when tt-device-mcp could not be resolved"
        assert "not found on PATH or at" in r.stderr, (
            f"{name}'s failure is not the hook's own distinguishing message (bash's generic "
            f"'exec: ... not found' would wrongly satisfy a bare 'tt-device-mcp' substring check "
            f"here): {r.stderr!r}"
        )


def test_the_hooks_resolve_the_cli_via_the_installed_venv_with_no_usable_path(tmp_path):
    """The actual defect: Slurm gives Prolog/Epilog no search path, so a hook that only ever
    tries a bare command name can never reach the CLI in that environment, even on a correctly
    installed broker host. Simulate a system install (TTDEV_ETC_DEFAULT naming a TTDEV_VENV) with
    PATH holding nothing usable, and confirm the hook still finds and runs the CLI."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    cli = venv_bin / "tt-device-mcp"
    cli.write_text("#!/bin/sh\nexit 3\n")
    cli.chmod(0o755)
    etc_default = tmp_path / "tt-device-broker-defaults"
    etc_default.write_text(f"TTDEV_VENV={tmp_path / 'venv'}\n")
    # PATH points at a directory that exists but is empty — no bash, no tt-device-mcp — so bash
    # itself must be invoked by absolute path; only the SCRIPT's internal `command -v` (using
    # this PATH) is what must fail to find tt-device-mcp, forcing the TTDEV_ETC_DEFAULT fallback.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {"PATH": str(empty_bin), "TTDEV_ETC_DEFAULT": str(etc_default), "SLURM_JOB_EXIT_CODE": "0"}
    bash = shutil.which("bash")
    for name in ("prolog.sh", "epilog.sh"):
        r = subprocess.run([bash, str(DEPLOY_SLURM / name)], env=env, capture_output=True, text=True)
        assert r.returncode == 3, (
            f"{name} did not resolve the installed CLI with no usable PATH: exit {r.returncode}, "
            f"stderr {r.stderr!r}"
        )


def test_the_epilog_prefers_slurm_job_derived_ec_when_nonzero(tmp_path):
    """SLURM_JOB_EXIT_CODE is only the wrapping batch script's own status and can read 0 even
    when a step genuinely failed (multiple srun steps, `|| true`, a trap). SLURM_JOB_DERIVED_EC
    is the highest exit code across every step in the job — the value that must decide whether
    the fabric traffic pass runs, or a wedge hides behind a batch script that swallowed it."""
    env = {
        **_stub_cli_env(tmp_path, '#!/bin/sh\necho "$@"\nexit 0\n'),
        "SLURM_JOB_EXIT_CODE": "0",
        "SLURM_JOB_DERIVED_EC": "1",
    }
    r = subprocess.run(["bash", str(DEPLOY_SLURM / "epilog.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (
        "--exit-code 1" in r.stdout
    ), f"the epilog did not prefer a nonzero SLURM_JOB_DERIVED_EC over a zero SLURM_JOB_EXIT_CODE: {r.stdout!r}"


def test_the_epilog_falls_back_to_slurm_job_exit_code_when_derived_ec_is_zero(tmp_path):
    """A single-step job's SLURM_JOB_DERIVED_EC is legitimately 0 while SLURM_JOB_EXIT_CODE
    carries the real failure; the fallback must still reach post-step, not drop the signal.

    Asserted as the nonzero bit rather than the literal 5: post-step consumes only `!= 0` (it
    forces the fabric pass), and the epilog normalizes because Slurm's own value is a wait(2)
    status — a real exit of 1 arrives as 256, so passing it through was never faithful anyway.
    """
    env = {
        **_stub_cli_env(tmp_path, '#!/bin/sh\necho "$@"\nexit 0\n'),
        "SLURM_JOB_EXIT_CODE": "5",
        "SLURM_JOB_DERIVED_EC": "0",
    }
    r = subprocess.run(["bash", str(DEPLOY_SLURM / "epilog.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (
        "--exit-code 1" in r.stdout
    ), f"the epilog dropped SLURM_JOB_EXIT_CODE when SLURM_JOB_DERIVED_EC was zero: {r.stdout!r}"


@pytest.mark.parametrize(
    "exit_code, derived, expect",
    [
        ("0:0", "0:0", "0"),  # a clean step in the <exit>:<signal> spelling
        ("0:9", "0:0", "1"),  # exit 0, killed by SIGKILL — a failure the exit half cannot show
        ("0:0", "1:0", "1"),  # the step failed, the batch script did not
        ("256", "0", "1"),  # the wait(2) status for a plain exit of 1
        ("", "", "0"),  # neither set: no evidence of failure
    ],
)
def test_the_epilog_normalizes_every_shape_slurm_reports_an_exit_code_in(tmp_path, exit_code, derived, expect):
    """A clean `0:0` must not drain the node.

    Slurm reports these in more than one shape, and `--exit-code` used to be a strict `type=int`:
    argparse rejected `0:0`, the epilog exited non-zero without ever contacting the broker, and
    Slurm drained the node over a job that finished fine.
    """
    env = {
        **_stub_cli_env(tmp_path, '#!/bin/sh\necho "$@"\nexit 0\n'),
        "SLURM_JOB_EXIT_CODE": exit_code,
        "SLURM_JOB_DERIVED_EC": derived,
    }
    r = subprocess.run(["bash", str(DEPLOY_SLURM / "epilog.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert f"--exit-code {expect}" in r.stdout, f"{exit_code!r}/{derived!r} normalized wrong: {r.stdout!r}"


def test_the_installer_stages_the_slurm_hooks_at_the_documented_path():
    """deploy/README.md points slurm.conf's Prolog=/Epilog= at
    /opt/tt-device-broker/slurm/{prolog,epilog}.sh; nothing was true there until the installer
    actually staged the hooks. Required, like the fabric-check stage: install-tt-device-broker.sh
    runs under `set -euo pipefail`, so a failed `install` here aborts the install rather than
    leaving a slurm.conf pointed at a path that does not exist on the host."""
    installer = Path(__file__).resolve().parents[1] / "deploy" / "install-tt-device-broker.sh"
    for name in ("prolog.sh", "epilog.sh"):
        stmt = _one(installer, "install -m 0755", f"deploy/slurm/{name}", f'"$R/slurm/{name}"')
        assert "|| true" not in stmt, f"staging {name} must not silently swallow a failure"


def test_apply_host_config_also_stages_the_slurm_hooks():
    """apply-host-config.sh's own header: auto-update never re-runs the installer, only this
    script (also true of tt-smi-ro.sh, fabric-check.sh, install-fabric-validator.sh — all staged
    here too). Staging the hooks ONLY in install-tt-device-broker.sh means an autoupdating host
    (TTDEV_AUTOUPDATE=1, the normal path on these boxes) never receives deploy/slurm/*.sh at all —
    not merely late, but never — so the README's documented path stays permanently empty on
    exactly the hosts that update themselves."""
    apply_cfg = Path(__file__).resolve().parents[1] / "deploy" / "apply-host-config.sh"
    for name in ("prolog.sh", "epilog.sh"):
        stmt = _one(apply_cfg, "install -m 0755", f'"$DEPLOY/slurm/{name}"', f'"$ROOT/slurm/{name}"')
        assert "|| true" not in stmt, f"staging {name} must not silently swallow a failure"


async def test_a_cancelled_reclaim_hands_the_reservation_off_instead_of_freeing_it(monkeypatch, clear_job_state):
    """A client that disconnects mid-reclaim must not free the device.

    `reclaim_foreign_holders` runs in `asyncio.to_thread`, which cannot be cancelled: the SIGTERM
    and SIGKILL rounds keep going after the route unwinds. The old code attached its done-callback
    and stamped `handoff` only after `asyncio.wait` returned, so a CancelledError raised *in* that
    await skipped both — `_post_step_impl`'s `finally` then saw an unstamped handoff, released the
    reservation, and `job_runner` was free to dispatch a tenant onto a device whose holders were
    still being killed.
    """
    from tt_device_mcp.device_holders import DeviceHolder, ReclaimResult

    may_finish = threading.Event()

    def slow_reclaim(**_):
        may_finish.wait(10)
        return ReclaimResult(signalled=[DeviceHolder(pid=4242, uid=60001)], survivors=[], scan_complete=True)

    monkeypatch.setattr(srv, "reclaim_foreign_holders", slow_reclaim)
    events: list = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)

    token = srv._reserve_external_step("post-step")
    handoff = {"reserved_by_task": False}
    task = asyncio.ensure_future(srv._run_step_reclaim(time.monotonic() + 30, handoff, token))
    await asyncio.sleep(0.05)  # let it reach the await it will be cancelled inside

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert handoff["reserved_by_task"] is True, "a cancelled reclaim did not stamp the handoff"
    assert srv.external_step_active == "post-step", "the reservation was freed while the reclaim still ran"
    assert not srv.get_external_step_free_event().is_set(), "job dispatch was unblocked mid-reclaim"

    may_finish.set()
    for _ in range(200):
        if srv.external_step_active == "":
            break
        await asyncio.sleep(0.02)
    assert srv.external_step_active == "", "the reclaim's own callback never released the reservation"
    kinds = [k for k, _ in events]
    assert "straggler_reclaim" in kinds, f"the cancelled reclaim's kills were never audited: {events}"


async def test_a_reclaim_that_overran_its_deadline_is_still_audited(monkeypatch, clear_job_state):
    """'Inconclusive' went out to the scheduler, but root still SIGKILLed another user's pids.

    The audit contract has no deadline: the timed-out path used to discard the eventual
    ReclaimResult, so those kills appeared in neither the health journal nor the action log.
    """
    from tt_device_mcp.device_holders import DeviceHolder, ReclaimResult

    may_finish = threading.Event()

    def slow_reclaim(**_):
        may_finish.wait(10)
        return ReclaimResult(signalled=[DeviceHolder(pid=777, uid=60002)], survivors=[], scan_complete=True)

    monkeypatch.setattr(srv, "reclaim_foreign_holders", slow_reclaim)
    events: list = []
    actions: list = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: actions.append(a))

    token = srv._reserve_external_step("post-step")
    handoff = {"reserved_by_task": False}
    timed_out, res = await srv._run_step_reclaim(time.monotonic() + 0.05, handoff, token)

    assert timed_out is True and res is None, "the reclaim did not report a deadline overrun"
    assert not events, f"nothing should be audited before the reclaim finishes: {events}"

    may_finish.set()
    for _ in range(200):
        if srv.external_step_active == "":
            break
        await asyncio.sleep(0.02)

    reclaim_events = [f for k, f in events if k == "straggler_reclaim"]
    assert len(reclaim_events) == 1, f"the late reclaim was not audited exactly once: {events}"
    assert reclaim_events[0]["late"] is True, "a late reclaim was not marked late in the journal"
    assert [h["pid"] for h in reclaim_events[0]["signalled"]] == [777]
    assert actions, "the late reclaim never reached the action log"


@pytest.mark.parametrize(
    "raw, expect",
    [
        ("0", 0),
        ("0:0", 0),  # a clean step in Slurm's <exit>:<signal> spelling
        ("0:9", 1),  # exit 0 but signal-killed: a failure the exit half alone cannot show
        ("1:0", 1),
        ("5", 1),
        ("256", 1),  # the wait(2) status for a plain exit of 1
        ("", 0),  # unset reaches argparse as an empty string
        ("garbage", 1),  # unreadable: not evidence the device is clean
        ("0:notanint", 1),
    ],
)
def test_the_step_exit_code_parser_accepts_every_shape_a_scheduler_reports(raw, expect):
    """`--exit-code` used to be a strict `type=int`, so `0:0` made argparse exit 2 before the
    epilogue ever reached the broker — and a non-zero epilogue is a drained node."""
    from tt_device_mcp.cli import _step_exit_code

    assert _step_exit_code(raw) == expect


def test_the_post_step_verb_accepts_a_slurm_shaped_exit_code_without_erroring(tmp_path):
    """The parser is wired into the argparse action, not merely defined beside it.

    Run as a real process against a socket nothing serves: the verb must get as far as failing
    to reach a broker (exit 1), never argparse's own usage error (exit 2) on `0:0`.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TT_DEVICE_MCP_SOCKET": str(tmp_path / "nothing.sock"),
    }
    r = subprocess.run(
        [str(Path(sys.executable).parent / "tt-device-mcp"), "post-step", "--exit-code", "0:0"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode != 2, f"argparse rejected a clean Slurm 0:0: {r.stderr}"
    assert "invalid" not in r.stderr.lower(), r.stderr
