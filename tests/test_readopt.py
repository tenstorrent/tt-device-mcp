# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Re-adoption: named job scopes survive a broker restart and are re-tracked on
startup so a restart never orphans a running job (and the queue won't double-book)."""

import asyncio

import pytest

import tt_device_mcp.server as srv
from tests.conftest import fsm_dirty, fsm_healthy, patch_health_event, patch_recovery
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerFsm, ServerState


def test_scope_unit_roundtrip():
    u = srv.job_scope_unit("232239-1")
    assert u == "ttdev-job-232239-1.scope"
    assert srv._scope_unit_job_id(u) == "232239-1"  # job_ids contain a dash
    assert srv._scope_unit_job_id("session-foo.scope") is None
    assert srv._scope_unit_job_id("ttdev-job-1-2.service") is None


def test_list_active_job_scopes_parses(monkeypatch):
    out = (
        "ttdev-job-120000-1.scope loaded active running /bin/bash -c ...\n"
        "user-1000.slice               loaded active active  User Slice\n"
    )

    class R:
        stdout = out

    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: R())
    assert srv.list_active_job_scopes() == {"120000-1": "ttdev-job-120000-1.scope"}


def test_reconcile_readopts_running_scope(monkeypatch):
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "list_active_job_scopes", lambda: {"130000-2": "ttdev-job-130000-2.scope"})
    job = srv.Job(
        id="130000-2",
        owner="jdoe",
        workspace="/w",
        command="pytest x",
        queued_at="2026-06-12T00:00:00",
        status=srv.JobStatus.RUNNING,
        started_at="2026-06-12T00:00:05",
    )
    monkeypatch.setattr(srv, "_job_from_log", lambda jid: job)
    # Don't spawn a real polling monitor in the test.
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: coro.close())

    asyncio.run(srv.reconcile_running_scopes())

    assert srv.jobs["130000-2"].status == srv.JobStatus.RUNNING  # visible as RUNNING
    assert srv.readopted_scopes == {"130000-2": "ttdev-job-130000-2.scope"}  # gates the queue


def test_reconcile_skips_already_tracked(monkeypatch):
    """A job this instance already runs (its own scope) must not be double-added."""
    existing = srv.Job(
        id="140000-3", owner="me", workspace="/w", command="x", queued_at="", status=srv.JobStatus.RUNNING
    )
    monkeypatch.setattr(srv, "jobs", {"140000-3": existing})
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "list_active_job_scopes", lambda: {"140000-3": "ttdev-job-140000-3.scope"})
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: coro.close())

    asyncio.run(srv.reconcile_running_scopes())

    assert srv.readopted_scopes == {}  # not re-adopted; it's ours already
    assert srv.jobs["140000-3"] is existing


# --- exit status across a broker restart -------------------------------------
#
# A job survives a broker restart by design (its own systemd scope), but a scope reports
# its exit status only to the broker that spawned it — and that broker is gone. Re-adoption
# had nothing to go on and recorded every such job "completed", so a job that FAILED across
# an auto-update told its owner it had passed.


@pytest.mark.asyncio
async def test_readopted_job_recovers_its_real_exit_code(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)

    srv.jobs["134"] = srv.Job(
        id="134", owner="[agent]jsmith", workspace="/w", command="pytest", queued_at="", status=srv.JobStatus.RUNNING
    )
    # What the job itself recorded on its way out.
    (tmp_path / "134.exit").write_text("1")

    await srv._monitor_readopted_scope("134", "ttdev-job-134.scope")

    job = srv.jobs["134"]
    assert job.exit_code == 1
    assert job.status == srv.JobStatus.FAILED, "a failed job was reported as completed"
    assert not (tmp_path / "134.exit").exists(), "exit file not cleaned up"


@pytest.mark.asyncio
async def test_readopted_job_that_passed_is_reported_as_passed(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)

    srv.jobs["135"] = srv.Job(
        id="135", owner="[agent]jsmith", workspace="/w", command="pytest", queued_at="", status=srv.JobStatus.RUNNING
    )
    (tmp_path / "135.exit").write_text("0")

    await srv._monitor_readopted_scope("135", "ttdev-job-135.scope")

    assert srv.jobs["135"].exit_code == 0
    assert srv.jobs["135"].status == srv.JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_readopted_job_with_no_exit_status_is_not_called_completed(monkeypatch, tmp_path, clear_job_state):
    """No exit file means the job never ran its EXIT trap — killed by a signal, or the host
    went down under it. It did not finish, and saying "completed" is the original lie."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)

    srv.jobs["136"] = srv.Job(
        id="136", owner="[agent]jsmith", workspace="/w", command="pytest", queued_at="", status=srv.JobStatus.RUNNING
    )

    await srv._monitor_readopted_scope("136", "ttdev-job-136.scope")

    job = srv.jobs["136"]
    assert job.status == srv.JobStatus.FAILED
    assert job.exit_code is None
    assert "left no exit status" in (job.error or "")


@pytest.mark.asyncio
async def test_a_readopted_job_that_wedged_the_mesh_flags_the_device(monkeypatch, tmp_path, clear_job_state):
    """A re-adopted job can leave the mesh wedged just as a normally-run one can, and there is no
    post-job _verify_device on this path. Unless the finalizer flags the device, the tenant queued
    behind it is dispatched onto the wedge. A runtime fault signature is evidence independent of
    exit code (a wedge rides out on exit 0)."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    monkeypatch.setattr(srv, "device_fault_reported", "")

    log = tmp_path / "run_137.log"
    log.write_text("... op start ...\nwaiting for active ethernet core\n... teardown ...\n")
    job = srv.Job(
        id="137", owner="[agent]jsmith", workspace="/w", command="pytest", queued_at="", status=srv.JobStatus.RUNNING
    )
    job.log_file = str(log)
    srv.jobs["137"] = job
    (tmp_path / "137.exit").write_text("0")  # rode out on a clean exit

    await srv._monitor_readopted_scope("137", "ttdev-job-137.scope")

    assert srv._device_unavailable_for_tenant(), (
        "a re-adopted job that reported a device fault must flag the device so the next tenant "
        "is not dispatched onto the wedge"
    )


# --- the deadline across a broker restart ------------------------------------
#
# The timeout is enforced by an asyncio watchdog inside the runner that spawned the job.
# That watchdog dies with its broker. Re-adoption inherited the job but not the deadline,
# so a job that outlived one restart ran forever: observed at 31 minutes against a 25-minute
# hard maximum, holding the device against every tenant queued behind it.


def test_job_from_log_recovers_the_deadline(monkeypatch, tmp_path):
    """The header has always recorded TIMEOUT; nothing read it back."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    (tmp_path / "2026-07-17_000956_010.log").write_text(
        "======\nJOB ID:      010\nOWNER:       ajones\nWORKSPACE:   /w\n"
        "COMMAND:     pytest x\nTIMEOUT:     1500s\nQUEUED:      2026-07-17T00:09:00\n"
        "======\n\n[Started at 2026-07-17T00:09:56]\n"
    )
    job = srv._job_from_log("010")
    assert job is not None
    assert job.timeout_sec == 1500, "a re-adopted job that forgets its deadline is immortal"


def test_job_from_log_without_a_timeout_header_still_gets_a_deadline(monkeypatch, tmp_path):
    """A log predating the field must not yield an unreapable job."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    (tmp_path / "2026-07-17_000956_011.log").write_text(
        "JOB ID:      011\nOWNER:       x\nCOMMAND:     y\n\n[Started at 2026-07-17T00:09:56]\n"
    )
    job = srv._job_from_log("011")
    assert job.timeout_sec == srv.DEFAULT_TIMEOUT_SEC


def test_readopted_deadline_counts_time_already_served(monkeypatch):
    """Re-adoption must not hand a job a fresh timeout: it started under the old broker."""
    started = (srv.datetime.now() - srv.timedelta(seconds=100)).isoformat()
    job = srv.Job(
        id="z", owner="o", workspace="/w", command="c", queued_at="", status=srv.JobStatus.RUNNING, started_at=started
    )
    job.timeout_sec = 300
    remaining = srv._readopted_deadline(job) - srv.time.monotonic()
    assert 190 < remaining < 210, f"expected ~200s left of a 300s limit, got {remaining:.0f}s"


@pytest.mark.asyncio
async def test_readopted_job_past_its_deadline_is_terminated(monkeypatch, tmp_path, clear_job_state):
    """The job-010 regression: re-adopted, overdue, and nothing reaped it."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    killed = []

    active = {"v": True}
    monkeypatch.setattr(srv, "_scope_active", lambda scope: active["v"])

    async def fake_terminate(scope, grace_sec=0):
        killed.append(scope)
        active["v"] = False

    monkeypatch.setattr(srv, "_terminate_scope", fake_terminate)

    # Started 40 minutes ago against a 25-minute limit.
    started = (srv.datetime.now() - srv.timedelta(seconds=2400)).isoformat()
    job = srv.Job(
        id="010",
        owner="ajones",
        workspace="/w",
        command="pytest",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        started_at=started,
    )
    job.timeout_sec = 1500
    srv.jobs["010"] = job

    await asyncio.wait_for(srv._monitor_readopted_scope("010", "ttdev-job-010.scope"), timeout=5)

    assert killed == ["ttdev-job-010.scope"], "an overdue re-adopted job ran on untouched"
    assert srv.jobs["010"].status == srv.JobStatus.TIMEOUT


@pytest.mark.asyncio
async def test_readopted_job_inside_its_deadline_is_left_alone(monkeypatch, tmp_path, clear_job_state):
    """The other half: re-adoption must not kill a job that is simply still working."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    killed = []

    calls = {"n": 0}

    def scope_active(scope):
        calls["n"] += 1
        return calls["n"] < 20  # ends on its own, well inside its limit

    monkeypatch.setattr(srv, "_scope_active", scope_active)

    async def fake_terminate(scope, grace_sec=0):
        killed.append(scope)

    monkeypatch.setattr(srv, "_terminate_scope", fake_terminate)

    job = srv.Job(
        id="011",
        owner="jdoe",
        workspace="/w",
        command="pytest",
        queued_at="",
        status=srv.JobStatus.RUNNING,
        started_at=srv.datetime.now().isoformat(),
    )
    job.timeout_sec = 1500
    srv.jobs["011"] = job
    (tmp_path / "011.exit").write_text("0")

    await asyncio.wait_for(srv._monitor_readopted_scope("011", "ttdev-job-011.scope"), timeout=5)

    assert killed == [], "killed a job that was inside its timeout"
    assert srv.jobs["011"].status == srv.JobStatus.COMPLETED


# --- a killed job must not certify its own success ---------------------------


def test_a_signalled_job_records_a_real_exit_status(tmp_path):
    """`$?` in an EXIT trap is the status of the last command to COMPLETE, and a shell
    terminated by a signal ran no failing command — so a killed job recorded 0 and the
    broker reported it "completed". Observed on a job killed 31 minutes in."""
    import os as _os
    import signal as _signal
    import subprocess as _sp
    import time as _time

    exit_file = tmp_path / "sig.exit"
    script = tmp_path / "job.sh"
    script.write_text(srv._exit_trap_preamble(str(exit_file)) + "set -e\nsleep 60\n")

    proc = _sp.Popen(["bash", str(script)], start_new_session=True)
    try:
        _time.sleep(0.5)
        # systemd signals every process in the scope's cgroup, not just the shell.
        _os.killpg(proc.pid, _signal.SIGTERM)
        proc.wait(timeout=10)
        for _ in range(50):
            if exit_file.exists():
                break
            _time.sleep(0.1)
        assert exit_file.exists(), "the job left no exit status at all"
        rc = exit_file.read_text().strip()
        assert rc != "0", "a SIGTERMed job reported itself as a clean success"
        assert rc == "143", f"expected 128+SIGTERM, got {rc}"
    finally:
        try:
            _os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


# --- startup tasks must actually run ------------------------------------------
#
# main() starts the job runner before any client can connect, so the lifespan's copy of the
# startup sequence sat behind an `if` that never fired. A queue restore and a startup health
# report were added only there: both were dead code in production while looking live in the
# diff, and neither ever ran once.


@pytest.mark.asyncio
async def test_startup_tasks_run_and_run_once(monkeypatch):
    ran = []
    monkeypatch.setattr(srv, "_startup_tasks_done", False)
    monkeypatch.setattr(srv, "should_privsep", lambda: True)

    async def fake_reconcile():
        ran.append("reconcile")

    async def fake_restore():
        ran.append("restore")

    async def fake_health():
        ran.append("health")

    monkeypatch.setattr(srv, "reconcile_running_scopes", fake_reconcile)
    monkeypatch.setattr(srv, "_restore_queued_jobs", fake_restore)
    monkeypatch.setattr(srv, "_record_startup_health", fake_health)

    await srv.run_startup_tasks()
    await asyncio.sleep(0.05)  # the health report is fire-and-forget
    assert ran == ["reconcile", "restore", "health"], (
        "the startup sequence did not run: a restored queue and the 'did it come back "
        "healthy?' report both depend on it"
    )

    # A second caller (main() and the lifespan both reach startup) must not redo it:
    # two startup health rows for one boot is a lie about how many times it started.
    await srv.run_startup_tasks()
    await asyncio.sleep(0.05)
    assert ran == ["reconcile", "restore", "health"], "startup sequence ran twice"


@pytest.mark.asyncio
async def test_broker_start_holds_the_door_until_the_fabric_verify(monkeypatch, clear_job_state):
    """A restart loses the in-memory verdict, and enum+ARC read healthy across a wedged eth link, so a
    fresh broker must HOLD the tenant door until the startup fabric pass proves the mesh moves data —
    never admit a job onto fabric nothing re-verified. run_startup_tasks sets that hold synchronously
    (before the runner is created, so no queued job races in), and a healthy fabric verify lifts it.
    Fails on base, which admits jobs on the ARC verdict alone."""
    monkeypatch.setattr(srv, "_startup_tasks_done", False)
    monkeypatch.setattr(srv, "should_privsep", lambda: True)
    fsm_healthy(srv)
    srv.device_op_active = ""
    srv.device_fault_reported = ""

    async def noop():
        pass

    monkeypatch.setattr(srv, "reconcile_running_scopes", noop)
    monkeypatch.setattr(srv, "_restore_queued_jobs", noop)
    monkeypatch.setattr(srv, "_record_startup_health", noop)
    # Hold the fabric pass off so we can assert the hold IS placed; its own tests cover the lift.
    monkeypatch.setattr(srv, "_verify_fabric_on_start", noop)

    await srv.run_startup_tasks()
    assert srv.fsm.state is not ServerState.HEALTHY, "broker start must hold the door pending the fabric verify"
    assert (
        srv._device_unavailable_for_tenant()
    ), "a fresh broker must refuse tenants until the fabric pass verifies the mesh"

    # A healthy startup fabric verify lifts the hold — the gate's verified clear (see _device_health_gate).
    srv._clear_device_dirty(verified=True, why="startup fabric verified healthy")
    assert srv._device_unavailable_for_tenant() == "", "a healthy fabric verify must reopen the door"


@pytest.mark.asyncio
async def test_startup_hold_names_the_foreign_holder_blocking_the_verify(monkeypatch, clear_job_state):
    """The startup verify cannot run beside a foreign tenant, so the gate skips and the door stays
    held. The tenant queued behind it must see WHO holds the device, not a bare "device unverified"
    that reads as a device fault — the misread that provoked a manual force-reset over a live foreign
    job. Fails on base: the gate names the holder in its log but the hold reason keeps the generic
    startup string, because the not-dirty startup hold never routes the holder into the reason."""
    # The startup hold state, as _hold_pending_startup_fabric_verify leaves it: unverified, NOT dirty.
    fsm_dirty(srv, "broker start: awaiting the startup fabric verify", why="startup_unverified")
    srv.device_op_active = ""

    holder = DeviceHolder(pid=2355525, uid=2000)  # a real logged-in tenant (uid >= MIN_TENANT_UID)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[holder], complete=True))

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    assert (
        srv.fsm.state is not ServerState.HEALTHY
    ), "the door must stay held while the foreign holder blocks the verify"
    assert (
        str(holder.pid) in srv.fsm.record.detail
    ), "the hold reason must name the foreign holder, not read as a bare device fault"
    assert (
        "awaiting the startup fabric verify" not in srv.fsm.record.detail
    ), "the generic startup reason must be replaced once the real blocker is a foreign holder"


@pytest.mark.asyncio
async def test_a_restored_queue_survives_when_main_already_started_the_runner(monkeypatch, clear_job_state):
    """The production shape: main() starts the runner, THEN the lifespan runs. The lifespan
    must not be the only thing that restores the queue, or a restart drops every queued job."""
    restored = []
    monkeypatch.setattr(srv, "_startup_tasks_done", False)
    monkeypatch.setattr(srv, "should_privsep", lambda: True)

    async def fake_reconcile():
        pass

    async def fake_restore():
        restored.append(True)

    async def fake_health():
        pass

    monkeypatch.setattr(srv, "reconcile_running_scopes", fake_reconcile)
    monkeypatch.setattr(srv, "_restore_queued_jobs", fake_restore)
    monkeypatch.setattr(srv, "_record_startup_health", fake_health)

    # Exactly what main() does before serving: the runner is already live.
    sentinel = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setattr(srv, "job_runner_task", sentinel)
    try:
        await srv.run_startup_tasks()
        assert restored == [True], "queued jobs were dropped because startup never restored them"
    finally:
        sentinel.cancel()
        await asyncio.gather(sentinel, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_restart_does_not_hand_a_degraded_device_a_clean_slate(monkeypatch, clear_job_state):
    """device_dirty lives in memory. A broker that dies holding a device degraded came back
    with the flag clear and the hold gone: the degradation survived the restart, the
    knowledge of it did not."""
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(24)})
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setattr(
        srv, "heartbeat_verdict", lambda expected: (srv.Verdict.UNHEALTHY, "only 24 of 32 chips present", {})
    )
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)

    await srv._record_startup_health()

    assert (
        srv.fsm.state is not ServerState.HEALTHY
    ), "a broker that restarted onto a 24-of-32 box called it clean; the next tenant gets it"
    assert "24 of 32" in srv.fsm.record.detail


@pytest.mark.asyncio
async def test_a_healthy_start_does_not_flag_the_device(monkeypatch, clear_job_state):
    """The other half: a clean start must not park the box behind a hold nobody asked for."""
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32)})
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setattr(srv, "heartbeat_verdict", lambda expected: (srv.Verdict.HEALTHY, "all 32 chips advancing", {}))
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)

    await srv._record_startup_health()

    assert srv.fsm.state is ServerState.HEALTHY


# --- fabric check on start ----------------------------------------------------


@pytest.mark.asyncio
async def test_startup_waits_for_a_readopted_job_before_touching_the_fabric(monkeypatch, clear_job_state):
    """A fabric pass drives traffic over every inter-chip link. Run beside a re-adopted
    tenant job it corrupts their measurements and ours — and the device is theirs."""
    monkeypatch.setattr(srv, "_SCOPE_POLL_SEC", 0.01)
    gate_calls = []

    async def fake_gate(job_log_file, *, phase, run_fabric, force_fabric=False):
        gate_calls.append((phase, run_fabric, force_fabric))

    monkeypatch.setattr(srv, "_device_health_gate", fake_gate)
    monkeypatch.setattr(srv, "readopted_scopes", {"010": "ttdev-job-010.scope"})

    task = asyncio.create_task(srv._verify_fabric_on_start())
    await asyncio.sleep(0.1)
    assert gate_calls == [], "drove fabric traffic across a link a tenant's job was using"

    srv.readopted_scopes.clear()  # the re-adopted job finishes
    await asyncio.wait_for(task, timeout=5)

    assert gate_calls == [("startup", True, True)], (
        "no fabric pass on start: a wedged eth core leaves sysfs perfectly healthy, so the "
        "chip probe alone says nothing about whether the mesh moves data"
    )


@pytest.mark.asyncio
async def test_a_fabric_check_that_cannot_run_does_not_break_startup(monkeypatch, clear_job_state):
    monkeypatch.setattr(srv, "readopted_scopes", {})

    async def boom(job_log_file, *, phase, run_fabric, force_fabric=False):
        raise RuntimeError("fabric check exploded")

    monkeypatch.setattr(srv, "_device_health_gate", boom)
    await srv._verify_fabric_on_start()  # must not raise


# --- per-user daemon probe on start (spec 03 I4) --------------------------------


def _per_user_start(monkeypatch, tmp_path, *, holders=()):
    """A per-user daemon at BOOT with one present chip, the given device holders, and the gate's side
    channels stripped. Returns the list each journaled health event lands in."""
    monkeypatch.setattr(srv, "_startup_tasks_done", False)
    monkeypatch.setattr(srv, "should_privsep", lambda: False)
    monkeypatch.setattr(srv, "fsm", ServerFsm(tmp_path / "fsm.json"))
    assert srv.fsm.state is ServerState.BOOT
    dev = tmp_path / "tenstorrent"
    (dev / "0").mkdir(parents=True)
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=list(holders), complete=True))
    srv.isolated_chips = set()
    srv.device_fault_reported = ""
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    return events


def _probe(monkeypatch, healthy: bool) -> list:
    """Replace the probe pass with a verdict; returns the run_fabric value of each pass it ran."""
    passes = []

    async def verify(expected, log, run_fabric=True, **_):
        passes.append(run_fabric)
        if healthy:
            return True, {"snapshot": {"ok": True}}
        return False, {"snapshot": {"ok": False, "detail": "ARC heartbeat stalled on chip 0"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    return passes


def _no_ladder(monkeypatch) -> list:
    calls = []

    async def escalate(*a, **k):
        calls.append(a)
        return srv.OUTCOME_WAITING

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    return calls


@pytest.mark.asyncio
async def test_a_per_user_daemon_start_runs_a_light_probe_and_a_pass_reads_healthy(
    monkeypatch, clear_job_state, tmp_path
):
    """A per-user daemon used to go BOOT -> HEALTHY on trust. It now probes first — the light pass
    only, no fabric — and a healthy pass leaves it HEALTHY. Fails on base: no probe ran."""
    _per_user_start(monkeypatch, tmp_path)
    passes = _probe(monkeypatch, healthy=True)

    await srv.run_startup_tasks()

    assert passes == [False], "the per-user start must run exactly one light pass, with no fabric"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_per_user_daemon_start_on_a_bad_device_stays_recovering(monkeypatch, clear_job_state, tmp_path):
    """The hole: a per-user daemon restarted onto a device whose ARC stopped ticking read HEALTHY and
    admitted its first job onto it. A failing start pass must hold the door — a self-heal hold, not a
    dirty mark, see the next test — journal the verdict, and never climb the ladder itself.
    Fails on base: BOOT went straight to HEALTHY."""
    events = _per_user_start(monkeypatch, tmp_path)
    _probe(monkeypatch, healthy=False)
    ladder = _no_ladder(monkeypatch)

    await srv.run_startup_tasks()

    assert srv.fsm.state is ServerState.RECOVERING, "a failing start probe left the per-user daemon open"
    assert srv.fsm.record.why in srv.SELFHEAL_WHYS, "the start hold must be one the idle relift can lift"
    assert not srv.fsm.record.dirty, "dirty here routes a later healthy read to a hold nothing lifts"
    assert srv._device_unavailable_for_tenant(), "a tenant must not be admitted onto the failed device"
    assert any(
        kind == "gate" and f.get("phase") == "startup" and f.get("healthy") is False for kind, f in events
    ), f"the failing start pass was not journaled: {[k for k, _ in events]}"
    assert ladder == [], "a start probe reports; it must never enter the recovery ladder"


@pytest.mark.asyncio
async def test_a_failed_per_user_start_reopens_once_the_device_reads_healthy(monkeypatch, clear_job_state, tmp_path):
    """A dirty mark would strand this: a dirty per-user device that later reads healthy routes to
    HOLD_FABRIC_UNVERIFIED (no validator to give a verdict), which nothing lifts by default. The start
    hold must instead be lifted by the idle relift's read-only re-read."""
    _per_user_start(monkeypatch, tmp_path)
    _probe(monkeypatch, healthy=False)
    _no_ladder(monkeypatch)
    await srv.run_startup_tasks()
    assert srv.fsm.state is ServerState.RECOVERING

    passes = _probe(monkeypatch, healthy=True)

    async def eth_unconfigured(timeout_sec=60.0):
        return None, "skipped (no eth reader)"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unconfigured)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setenv("TT_DEVICE_MCP_SELFHEAL_RELIFT", "1")
    srv.device_op_active = ""
    srv.last_relift_monotonic = 0.0

    await srv._attempt_idle_relift()

    assert srv.fsm.state is ServerState.HEALTHY, "a device that recovered after a failed start stayed held"
    assert passes == [False], "the relift must re-read without the fabric pass"


@pytest.mark.asyncio
async def test_a_per_user_restart_keeps_a_loaded_open_episode(monkeypatch, clear_job_state, tmp_path):
    """A same-boot restart loads the last episode from fsm.json. The start must not wipe it to
    HEALTHY (or probe over it): its own gate or relift settles it."""
    _per_user_start(monkeypatch, tmp_path)
    srv.fsm.on_fault("job_killed", detail="killed mid-run")
    passes = _probe(monkeypatch, healthy=True)

    await srv.run_startup_tasks()

    assert passes == [], "probed over a loaded open episode"
    assert srv.fsm.state is ServerState.RECOVERING
    assert srv.fsm.record.why == "job_killed" and srv.fsm.record.dirty


@pytest.mark.asyncio
async def test_a_per_user_daemon_start_beside_a_holder_stays_healthy(monkeypatch, clear_job_state, tmp_path):
    """The gate never probes beside a foreign holder. At a per-user start that is often the user's own
    process; holding on it would leave a foreign_holder hold nothing lifts short of the stuck-hold
    escalation. Skip and stay as before."""
    _per_user_start(monkeypatch, tmp_path, holders=[DeviceHolder(pid=4242, uid=2000)])
    passes = _probe(monkeypatch, healthy=False)

    await srv.run_startup_tasks()

    assert passes == [], "probed the device beside a foreign holder"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_per_user_start_probe_that_raises_holds_the_device(monkeypatch, clear_job_state, tmp_path):
    """A start probe that could not run verified nothing: the device is held, not trusted, and the
    daemon still comes up."""
    _per_user_start(monkeypatch, tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(srv, "_device_health_gate", boom)

    await srv.run_startup_tasks()

    assert srv.fsm.state is ServerState.RECOVERING
    assert srv.fsm.record.why in srv.SELFHEAL_WHYS and not srv.fsm.record.dirty


@pytest.mark.asyncio
async def test_the_privsep_broker_start_does_not_run_the_per_user_probe(monkeypatch, clear_job_state):
    """The system broker keeps its own start: boot_merge's startup_unverified hold and the forced
    fabric verify. The per-user light pass must not run there, or lift that hold."""
    monkeypatch.setattr(srv, "_startup_tasks_done", False)
    monkeypatch.setattr(srv, "should_privsep", lambda: True)
    ran = []

    async def noop():
        pass

    async def per_user_probe():
        ran.append(True)

    for name in (
        "reconcile_running_scopes",
        "_restore_queued_jobs",
        "_record_startup_health",
        "_verify_fabric_on_start",
    ):
        monkeypatch.setattr(srv, name, noop)
    monkeypatch.setattr(srv, "_probe_on_per_user_start", per_user_probe)

    await srv.run_startup_tasks()

    assert ran == []
    assert srv.fsm.record.why == "startup_unverified"


def test_the_job_exit_dir_is_redirectable(monkeypatch, tmp_path):
    """Only root can create a directory under /run, so a per-user daemon must be able to move this
    or it loses every job's self-reported exit status without a sound. Bound at import, the variable
    the launcher sets could never reach it — renaming it left the whole suite green."""
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path / "jx"))
    assert srv.job_exit_dir() == tmp_path / "jx"
    assert srv.job_exit_file("007") == tmp_path / "jx" / "007.exit"

    monkeypatch.delenv("TT_DEVICE_MCP_JOB_EXIT_DIR", raising=False)
    assert srv.job_exit_dir() == srv.Path(srv.JOB_EXIT_DIR_DEFAULT)


def test_a_redirected_job_exit_dir_is_not_made_world_writable(monkeypatch, tmp_path):
    """The shared default is sticky+1777 because privsep jobs run as different users and each must
    write its own file. A redirected dir sits inside one user's 0700 state dir; widening it to 1777
    would publish their job records to every tenant on the box."""
    d = tmp_path / "jx"
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(d))
    srv.ensure_job_exit_dir()
    assert d.is_dir()
    assert d.stat().st_mode & 0o777 != 0o777, oct(d.stat().st_mode)


def test_the_device_op_lock_is_redirectable(monkeypatch, tmp_path):
    """Only root can create a directory under /run, so a per-user daemon cannot write the default
    and the restart-inhibit it is meant to provide is silently absent (tt-device-mcp#21)."""
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(tmp_path / "device-op.lock"))
    assert srv.device_op_inhibit() == tmp_path / "device-op.lock"

    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", raising=False)
    assert srv.device_op_inhibit() == srv.Path(srv.DEVICE_OP_INHIBIT_DEFAULT)
