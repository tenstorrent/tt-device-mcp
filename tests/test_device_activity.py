# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""DEVICE IDLE status (spec 03 I54): while a job holds the device, a 1 s host-side sampler says
whether the job is doing device work, and every status surface shows ``DEVICE IDLE for <n>`` once
it has done none for more than the threshold. Unknown is never shown as idle."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests import test_device_safety
from tests.conftest import fsm_healthy
from tests.test_device_safety import _run_one_job
from tt_device_mcp import cli, device_activity, device_holders, job_reap
from tt_device_mcp import server as srv
from tt_device_mcp.device_activity import (
    ACTIVE,
    IDLE,
    UNKNOWN,
    ActivityTracker,
    DeviceActivitySampler,
    JobActivitySource,
)


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _sampler(samples, clock=None, idle_after=1.0, interval=1.0):
    """A sampler fed ``samples`` (working, why) at ``interval`` steps of a fake clock, never started."""
    clock = clock or Clock()
    s = DeviceActivitySampler(lambda: (None, ""), interval=interval, idle_after=idle_after, clock=clock, wall=clock)
    for i, working in enumerate(samples):
        if i:
            clock.t += interval
        s.sample_once(working, "test")
    return s, clock


# --- the state machine ---------------------------------------------------------------------------


def test_a_gap_of_one_second_or_less_never_reads_idle():
    t = ActivityTracker(idle_after=1.0)
    assert t.observe(False, 0.0, 0.0) == ACTIVE
    assert t.observe(False, 0.5, 0.5) == ACTIVE
    assert t.observe(False, 1.0, 1.0) == ACTIVE, "exactly the threshold is not MORE than it"
    assert t.observe(True, 1.4, 1.4) == ACTIVE
    assert t.idle_total_sec() == 0.0


def test_a_gap_over_one_second_reads_idle_and_counts_up():
    t = ActivityTracker(idle_after=1.0)
    for now in (0.0, 1.0):
        t.observe(False, now, now)
    assert t.observe(False, 1.01, 1.01) == IDLE
    t.observe(False, 4.0, 4.0)
    v = t.view(4.0, 1.0)
    assert v["activity"] == IDLE
    assert v["idle_seconds"] == 4.0
    assert v["activity_label"] == "DEVICE IDLE for 4s"
    assert v["idle_since"] is not None


def test_idle_clears_on_the_first_sample_with_work_and_the_total_is_kept():
    t = ActivityTracker(idle_after=1.0)
    for now in (0.0, 1.0, 2.0, 3.0):
        t.observe(False, now, now)
    assert t.state == IDLE
    assert t.observe(True, 4.0, 4.0) == ACTIVE
    v = t.view(4.0, 1.0)
    assert v["activity_label"] == "active"
    assert v["idle_seconds"] is None
    assert v["idle_total_sec"] == 3.0
    # A new quiet stretch starts its own count from zero.
    assert t.observe(False, 5.0, 5.0) == ACTIVE
    assert t.observe(False, 6.5, 6.5) == IDLE
    assert t.idle_total_sec() == 4.5


def test_unknown_is_never_idle_and_restarts_the_count():
    t = ActivityTracker(idle_after=1.0)
    assert t.view(0.0, 1.0)["activity"] == UNKNOWN, "never sampled reads unknown"
    t.observe(False, 0.0, 0.0)
    t.observe(False, 1.0, 1.0)
    assert t.observe(None, 2.0, 2.0) == UNKNOWN
    assert t.view(2.0, 1.0)["activity_label"] == "activity unknown"
    # The quiet count starts over after a read that could not tell.
    assert t.observe(False, 3.0, 3.0) == ACTIVE
    assert t.observe(False, 4.0, 4.0) == ACTIVE
    assert t.observe(False, 4.5, 4.5) == IDLE


def test_a_stale_view_reads_unknown_not_the_last_idle():
    s, clock = _sampler([False, False, False])
    assert s.view()["activity"] == IDLE
    clock.t += device_activity.STALE_INTERVALS * s.interval + 0.1
    v = s.view()
    assert v["activity"] == UNKNOWN
    assert "not answered" in v["activity_why"]
    assert "IDLE" not in v["activity_label"]


# --- the source (read-only /proc and driver reads) -----------------------------------------------


@pytest.fixture
def proc(monkeypatch):
    """Fake driver holder record and per-pid CPU; every pid listed in ``members`` is in the job."""
    ns = SimpleNamespace(driver=(set(), True), cpu={}, members={4001, 4002})
    monkeypatch.setattr(device_holders, "_driver_holder_pids", lambda: ns.driver)
    monkeypatch.setattr(device_holders, "read_proc_cpu", lambda pid: ns.cpu.get(pid))
    monkeypatch.setattr(JobActivitySource, "_is_member", lambda self, p: p in ns.members)
    return ns


def test_no_job_process_holding_the_device_is_no_work(proc):
    proc.driver = ({9999}, True)  # a foreign holder is not the job's work
    working, why = JobActivitySource(4001)()
    assert working is False
    assert "no job process" in why


def test_a_holder_whose_cpu_moves_is_work_and_a_flat_one_is_not(proc):
    proc.driver = ({4001}, True)
    src = JobActivitySource(4001)
    proc.cpu[4001] = (10, "s")
    assert src()[0] is True, "the first sight of a holder counts as work"
    proc.cpu[4001] = (12, "s")
    assert src()[0] is True
    assert src()[0] is False, "flat CPU time on a holder is no device work"
    proc.cpu[4001] = (12, "t")  # a reused pid is a new process, not the same flat one
    assert src()[0] is True


def test_an_unreadable_holder_record_is_unknown(proc, monkeypatch):
    proc.driver = (set(), False)  # a device's record could not be read
    assert JobActivitySource(4001)()[0] is None
    # No driver record at all: fall back to the job's own fds; one unreadable is unknown.
    proc.driver = None
    monkeypatch.setattr(job_reap, "job_processes", lambda pid, scope, tag: {4001: None})
    monkeypatch.setattr(job_reap, "_holds_device", lambda p: None)
    assert JobActivitySource(4001)()[0] is None
    # A holder that exits between the two reads is unknown, not idle.
    proc.driver = ({4001}, True)
    proc.cpu.clear()
    assert JobActivitySource(4001)()[0] is None


# --- cadence and lifetime ------------------------------------------------------------------------


def test_the_cadence_and_threshold_default_to_one_second(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_ACTIVITY_INTERVAL_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_IDLE_AFTER_SEC", raising=False)
    assert device_activity.interval_sec() == 1.0
    assert device_activity.idle_after_sec() == 1.0
    monkeypatch.setenv("TT_DEVICE_MCP_ACTIVITY_INTERVAL_SEC", "0.25")
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_IDLE_AFTER_SEC", "30")
    assert device_activity.interval_sec() == 0.25
    assert device_activity.idle_after_sec() == 30.0
    for bad in ("x", "-1", "0"):
        monkeypatch.setenv("TT_DEVICE_MCP_ACTIVITY_INTERVAL_SEC", bad)
        assert device_activity.interval_sec() == 1.0, f"interval {bad!r} must fall back to 1 s"


@pytest.mark.asyncio
async def test_the_sampler_reads_once_per_interval_and_a_failed_read_is_unknown():
    clock = Clock()
    sleeps = []
    reads = []

    def source():
        reads.append(clock.t)
        clock.t += 0.2  # each read takes 0.2 s of the interval
        if len(reads) == 2:
            raise OSError("boom")
        return False, "quiet"

    async def sleep(sec):
        sleeps.append(sec)
        clock.t += sec
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    s = DeviceActivitySampler(source, interval=1.0, idle_after=1.0, clock=clock, wall=clock, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert sleeps == pytest.approx([0.8] * 4), "the read time comes out of the 1 s interval"
    assert [round(b - a, 6) for a, b in zip(reads, reads[1:])] == [1.0] * 3
    assert s.tracker.samples == 4
    assert s.tracker.why == "quiet"


@pytest.mark.asyncio
async def test_the_sampler_runs_only_while_the_job_runs(monkeypatch, clear_job_state):
    """Started at spawn with the job's pid, stopped once the process is gone and before the
    post-job gate, and the job keeps its idle total."""
    made = []

    class Recording(DeviceActivitySampler):
        def __init__(self, source, **kw):
            super().__init__(lambda: (False, "quiet"), interval=0.01, idle_after=0.0)
            self.pid = source.pid
            made.append(self)

    monkeypatch.setattr(device_activity, "DeviceActivitySampler", Recording)
    job, *_ = await _run_one_job(monkeypatch, "961", privsep=False, scope_active=True)
    assert len(made) == 1, "one sampler per job"
    assert made[0].pid, "the sampler follows the job's own process"
    assert made[0].task is not None and made[0].task.done(), "the sampler outlived the job"
    assert srv._job_activity == {}
    assert job.idle_total_sec is not None


@pytest.mark.asyncio
async def test_the_sampler_is_stopped_before_the_post_job_gate(monkeypatch, clear_job_state):
    seen_at_gate = []

    async def _gate(job_log_file, job_failed=False, noop_failure=False, exit1_failure=False):
        seen_at_gate.append(dict(srv._job_activity))

    class Quiet(DeviceActivitySampler):
        def __init__(self, source, **kw):
            super().__init__(lambda: (False, "quiet"), interval=0.01, idle_after=0.0)

    monkeypatch.setattr(device_activity, "DeviceActivitySampler", Quiet)
    # _run_one_job quiets the gate with a no-op; watch it instead.
    monkeypatch.setattr(
        test_device_safety, "_quiet_post_job_gate", lambda mp: mp.setattr(srv, "_verify_device_after_job", _gate)
    )
    await _run_one_job(monkeypatch, "962", privsep=False, scope_active=True)
    assert seen_at_gate, "the post-job gate never ran"
    assert seen_at_gate == [{}], "the sampler was still running on the between-jobs path"


def test_a_sampler_that_cannot_start_leaves_the_job_alone(monkeypatch, clear_job_state):
    def boom(*a, **k):
        raise RuntimeError("no loop")

    monkeypatch.setattr(device_activity, "DeviceActivitySampler", boom)
    srv._start_job_activity("963", 1234, None, None)
    assert srv.job_activity_view("963") is None
    srv._stop_job_activity("963", None)  # nothing to stop: no error


# --- the surfaces --------------------------------------------------------------------------------


def _running_idle_job(monkeypatch, job_id="970", samples=(False, False, False, False, False)):
    job = srv.Job(id=job_id, owner="tenant", workspace="/tmp/ws", command="sleep 600", queued_at="2026-10-14T09:59:00")
    job.status = srv.JobStatus.RUNNING
    job.started_at = "2026-10-14T10:00:00"
    srv.jobs[job_id] = job
    sampler, _ = _sampler(list(samples))
    monkeypatch.setitem(srv._job_activity, job_id, sampler)
    return job


def test_queue_status_shows_device_idle_on_the_running_row(monkeypatch, clear_job_state):
    _running_idle_job(monkeypatch)
    row = srv._get_queue_status()["running"][0]
    assert row["activity"] == IDLE
    assert row["activity_label"] == "DEVICE IDLE for 4s"
    assert row["idle_seconds"] == 4.0


def test_queue_status_without_a_sampler_has_no_activity_fields(clear_job_state):
    job = srv.Job(id="971", owner="tenant", workspace="/tmp/ws", command="x", queued_at="t")
    job.status = srv.JobStatus.RUNNING
    srv.jobs["971"] = job
    assert "activity" not in srv._get_queue_status()["running"][0]


def test_health_carries_the_running_jobs_activity(monkeypatch, clear_job_state):
    fsm_healthy(srv)
    assert "device_activity" not in srv._health_payload(), "absent while no job runs"
    _running_idle_job(monkeypatch)
    act = srv._health_payload()["device_activity"]
    assert act["job_id"] == "970"
    assert act["activity"] == IDLE
    assert act["activity_label"].startswith("DEVICE IDLE for ")


@pytest.mark.asyncio
async def test_job_status_tool_shows_idle_while_running_and_the_total_after(monkeypatch, clear_job_state):
    from tt_device_mcp.socket_transport import current_peer_uid, current_via_mcp

    job = _running_idle_job(monkeypatch)
    mcp = srv.create_mcp_server()
    tok, mtok = current_peer_uid.set(4242), current_via_mcp.set(False)
    try:
        out = json.loads((await mcp.call_tool("tt_device_job_status", {"params": {"job_id": "970"}})).content[0].text)
        assert out["activity_label"] == "DEVICE IDLE for 4s"

        srv._stop_job_activity("970", job)
        job.status = srv.JobStatus.COMPLETED
        job.finished_at = "2026-10-14T10:10:00"
        out = json.loads((await mcp.call_tool("tt_device_job_status", {"params": {"job_id": "970"}})).content[0].text)
        assert "activity_label" not in out, "a finished job has no live activity"
        assert out["idle_total_sec"] == 4.0
    finally:
        current_peer_uid.reset(tok)
        current_via_mcp.reset(mtok)


def test_the_footer_records_the_idle_total_and_still_parses(tmp_path, clear_job_state):
    job = srv.Job(id="972", owner="tenant", workspace="/tmp", command="x", queued_at="2026-10-14T10:00:00")
    job.status = srv.JobStatus.COMPLETED
    job.exit_code = 0
    job.started_at = "2026-10-14T10:00:01"
    job.finished_at = "2026-10-14T10:00:11"
    job.idle_total_sec = 6.0
    log = tmp_path / "job.log"
    log.write_text("")
    srv.write_job_log_footer(log, job)
    lines = log.read_text().splitlines()
    assert "DEVICE IDLE: 6.0s" in lines
    parsed = srv._parse_job_log_footer(lines)
    assert parsed.get("status") or parsed, "the footer no longer parses"
    assert "RUNTIME:     10.0s" in lines


def test_the_cli_overview_marks_only_an_idle_job():
    base = {"id": "973", "owner": "tenant", "command": "sleep 600", "started_at": None}
    idle = dict(base, activity="idle", activity_label="DEVICE IDLE for 4m12s")
    lines = cli.overview_lines([idle], [], [])
    assert any(line.strip() == "DEVICE IDLE for 4m12s" for line in lines)
    for state, label in (("active", "active"), ("unknown", "activity unknown"), (None, None)):
        row = dict(base, activity=state, activity_label=label)
        assert not any("IDLE" in line for line in cli.overview_lines([row], [], [])), state


def test_cli_status_of_one_job_prints_its_device_line(monkeypatch, capsys):
    answer = {"job_id": "974", "status": "running", "command": "x", "activity_label": "DEVICE IDLE for 9s"}
    monkeypatch.setattr(cli, "is_daemon_running", lambda port: True)
    monkeypatch.setattr(cli, "mcp_tool_call", lambda port, tool, args: dict(answer))
    args = SimpleNamespace(port=0, target=None, job="974")
    assert cli.cmd_status(args) == 0
    assert "Device:  DEVICE IDLE for 9s" in capsys.readouterr().out

    answer = {"job_id": "974", "status": "completed", "command": "x", "idle_total_sec": 9.0}
    assert cli.cmd_status(args) == 0
    assert "Idle:    9.0s" in capsys.readouterr().out


def test_watch_redraws_every_second(monkeypatch, capsys):
    assert cli.WATCH_INTERVAL_SEC == 1.0
    slept = []

    def _sleep(sec):
        slept.append(sec)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "is_daemon_running", lambda port: True)
    monkeypatch.setattr(cli, "_overview", lambda port, limit, color: (["RUNNING"], None))
    monkeypatch.setattr(cli.time, "sleep", _sleep)
    assert cli.cmd_watch(SimpleNamespace(port=0, count=None)) == 0
    assert slept == [1.0]
