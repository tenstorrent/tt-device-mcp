# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Test _recent_jobs parses persisted job logs (header + footer), newest first."""

import asyncio
from datetime import datetime, timedelta

import pytest

import tt_device_mcp.health.evidence as health
import tt_device_mcp.server as srv
from tests.conftest import fsm_dirty


def _job_log(p, jid, owner, cmd, runtime):
    p.write_text(
        f"JOB ID:      {jid}\nOWNER:       {owner}\nCOMMAND:     {cmd}\n"
        f"QUEUED:      2026-06-11T12:00:00\n[Started...]\n"
        + "noise\n" * 50
        + f"\nFINISHED:    2026-06-11T12:05:00\nSTATUS:      completed\nEXIT CODE:   0\n"
        f"WAIT TIME:   1.0s\nRUNTIME:     {runtime}\n"
    )


def test_recent_jobs_skips_empty_headerless_stubs(monkeypatch, tmp_path):
    """An empty / headerless log file is a stub — a broker killed between creating the log and writing
    its header. It carries no id/owner/command, so it must be SKIPPED, not rendered as a phantom
    `? ? interrupted` row. A real orphaned job (header written at start, no footer) still shows
    interrupted. Fails on base, which surfaces one blank row per empty stub."""
    (tmp_path / "2026-08-01_010000_201.log").write_text("")  # empty stub
    (tmp_path / "2026-08-01_010001_202.log").write_text("")  # empty stub
    _job_log(tmp_path / "2026-08-01_010100_210.log", "210", "jdoe", "echo hi", "5.0s")  # finished
    (tmp_path / "2026-08-01_010200_211.log").write_text(  # orphaned: header, NO footer
        "JOB ID:      211\nOWNER:       bjones\nCOMMAND:     sleep 999\n"
        "QUEUED:      2026-08-01T01:02:00\n[Started at 2026-08-01T01:02:00]\n"
    )
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})

    recs = srv._recent_jobs(50)
    ids = [r["job_id"] for r in recs]
    assert None not in ids, "empty headerless stubs must be skipped, not shown as '? ? interrupted'"
    assert "210" in ids and "211" in ids, "real jobs (finished + orphaned) must still appear"
    assert (
        next(r for r in recs if r["job_id"] == "211")["status"] == "interrupted"
    ), "a real orphaned job (header, no footer) still reads interrupted"


def test_recent_jobs_parses_and_limits(monkeypatch, tmp_path):
    _job_log(tmp_path / "2026-06-11_120000_120000-1.log", "120000-1", "jdoe", "pytest foo", "300.0s")
    _job_log(tmp_path / "2026-06-11_130000_130000-2.log", "130000-2", "bjones", "pytest bar", "12.0s")
    (tmp_path / "server.log").write_text("ignore me")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)

    jobs = srv._recent_jobs(20)
    assert {j["owner"] for j in jobs} == {"jdoe", "bjones"}  # server.log excluded
    j = next(j for j in jobs if j["owner"] == "jdoe")
    assert j["command"] == "pytest foo" and j["runtime"] == "300.0s"
    assert j["status"] == "completed" and j["exit_code"] == "0" and j["job_id"] == "120000-1"
    assert srv._recent_jobs(1) and len(srv._recent_jobs(1)) == 1  # limit honored


def _footerless_log(p, jid, owner, cmd, queued, started=None, envs=40):
    """A job log with header (+ N env vars) and no footer — i.e. not finished."""
    body = (
        "=" * 70 + f"\nJOB ID:      {jid}\nOWNER:       {owner}\nCOMMAND:     {cmd}\n"
        f"QUEUED:      {queued}\n"
        + "-" * 70
        + "\nENVIRONMENT VARIABLES:\n"
        + "".join(f"  VAR{i}=value{i}\n" for i in range(envs))
        + "=" * 70
        + "\n"
        + "\n[Waiting to start...]\n\n"
    )
    if started:
        body += f"[Started at {started}]\n\n" + "device output line\n" * 30
    p.write_text(body)


def test_running_job_uses_live_state_and_counts_up(monkeypatch, tmp_path):
    """A footerless log whose id IS tracked RUNNING => 'running', runtime up to now."""
    import datetime as _dt

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    # started 100s ago, queued 5s before that — env block pushes [Started at] past line 15.
    started = (_dt.datetime.now() - _dt.timedelta(seconds=100)).isoformat()
    queued = (_dt.datetime.now() - _dt.timedelta(seconds=105)).isoformat()
    _footerless_log(
        tmp_path / "2026-06-11_120000_120000-1.log", "120000-1", "jdoe", "pytest foo", queued, started=started, envs=60
    )
    job = srv.Job(
        id="120000-1",
        owner="jdoe",
        workspace="/w",
        command="pytest foo",
        queued_at=queued,
        started_at=started,
        status=srv.JobStatus.RUNNING,
    )
    monkeypatch.setattr(srv, "jobs", {"120000-1": job})

    j = srv._recent_jobs(20)[0]
    assert j["status"] == "running"
    assert j["runtime"] and 95 < float(j["runtime"].rstrip("s")) < 130  # counts up to now
    assert j["wait"] and 4 < float(j["wait"].rstrip("s")) < 7  # ~5s queued before run


def test_orphaned_footerless_is_interrupted_not_running(monkeypatch, tmp_path):
    """A footerless log NOT in the in-memory queue => orphaned by a restart, not running."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _footerless_log(
        tmp_path / "2026-06-11_120000_130000-1.log",
        "130000-1",
        "bjones",
        "pytest bar",
        "2026-06-11T12:00:00",
        started="2026-06-11T12:00:05",
        envs=50,
    )
    monkeypatch.setattr(srv, "jobs", {})  # broker restarted: queue is empty

    j = srv._recent_jobs(20)[0]
    assert j["status"] == "interrupted"  # NOT "running"
    assert j["wait"] == "5.0s"  # 12:00:05 - 12:00:00


def test_interrupted_job_reports_an_estimated_runtime(monkeypatch, tmp_path):
    """An orphaned job never wrote a runtime, but its log's last write approximates when it
    died — so recent shows an estimate, not a blank that reads as "ran for 0s"."""
    import datetime as _dt

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    started = (_dt.datetime.now() - _dt.timedelta(seconds=42)).isoformat()
    queued = (_dt.datetime.now() - _dt.timedelta(seconds=47)).isoformat()
    _footerless_log(
        tmp_path / "2026-06-11_120000_150000-1.log",
        "150000-1",
        "djones",
        "pytest qux",
        queued,
        started=started,
        envs=30,
    )
    monkeypatch.setattr(srv, "jobs", {})  # orphaned by a restart

    j = srv._recent_jobs(20)[0]
    assert j["status"] == "interrupted"
    assert j["runtime"] is not None, "an interrupted job should show an estimated runtime, not blank"
    assert 40 < float(j["runtime"].rstrip("s")) < 90  # ~42s: started -> log mtime (now)


def test_in_flight_broker_op_appears_in_recent_while_running(monkeypatch, tmp_path):
    """A reset/fabric pass must show in history while it runs, not only once it finishes —
    otherwise the 45-60s the broker held the device is a blank in the audit trail."""
    import datetime as _dt

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    started = (_dt.datetime.now() - _dt.timedelta(seconds=20)).isoformat()
    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    monkeypatch.setattr(srv, "device_op_detail", "health check: fabric traffic pass across all links (~45s)")
    monkeypatch.setattr(srv, "device_op_started_at", started)

    live = [r for r in srv._recent_jobs(20) if r["job_id"] == "--"]
    assert len(live) == 1
    assert live[0]["owner"] == "[broker]" and live[0]["status"] == "running"
    assert "fabric traffic pass" in live[0]["command"]
    assert live[0]["runtime"] and 18 < float(live[0]["runtime"].rstrip("s")) < 35


def test_a_reserved_op_row_keeps_its_id_start_and_name_across_completion(monkeypatch, tmp_path):
    """The ledger stability invariant: a broker sub-action reserves its row identity when it
    begins, so the SAME row — same id, same start, same name — represents it while it runs and
    once it finishes. On base the in-flight row is a synthetic ``--`` carrying the op's friendly
    detail while the durable row lands with a fresh id, a reconstructed start, and the argv as its
    name — three fields the row changes as it moves through the ledger. Fails on base: no
    ``_begin_action_row``, so the in-flight row never carries the id the durable row will use."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)

    srv._begin_action_row("[broker]fabric-check", "/opt/tt-device-broker/fabric-check.sh")

    running = [r for r in srv._recent_jobs(20) if r["status"] == "running"]
    assert len(running) == 1
    live = running[0]
    assert live["job_id"] != "--" and live["job_id"].isdigit()
    assert live["owner"] == "[broker]fabric-check"
    assert live["command"] == "/opt/tt-device-broker/fabric-check.sh"
    live_id, live_start = live["job_id"], live["started_at"]

    # The sub-action finishes and writes its durable row.
    srv.write_action_log("[broker]fabric-check", "/opt/tt-device-broker/fabric-check.sh", 30.0, "completed", 0)

    done = srv._recent_jobs(20)
    assert len(done) == 1, "one row for the sub-action, not a live one plus a separate durable one"
    d = done[0]
    assert d["status"] == "completed"
    assert d["job_id"] == live_id, "the durable row keeps the id the in-flight row already showed"
    assert d["started_at"] == live_start, "and the same start — no re-sort when it completes"
    assert d["command"] == "/opt/tt-device-broker/fabric-check.sh", "and the same name"


def test_a_reserved_action_id_is_not_handed_to_another_job(monkeypatch, tmp_path):
    """A reserved row holds its id in memory with no log file yet, so next_job_id must still treat
    it as taken — else a tenant job allocated during the ~60s op takes the same id and two rows
    collide. Fails on base: the reservation isn't visible to the allocator."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)

    srv._begin_action_row("[broker]fabric-check", "cmd")
    reserved = srv._action_row["id"]
    # Force the allocator back onto the reserved id: the next id it would mint is exactly it.
    monkeypatch.setattr(srv, "job_counter", (int(reserved) - 1) % srv.JOB_ID_MODULUS)
    assert srv.next_job_id() != reserved, "the reserved action id must never be handed to another job"


def test_an_active_action_row_cannot_be_replaced(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)

    srv._begin_action_row("[broker]fabric-check", "fabric-check")
    active = dict(srv._action_row)

    with pytest.raises(RuntimeError, match="action row already active"):
        srv._begin_action_row("[broker]reset-tool", "tt-smi -glx_reset")

    assert srv._action_row == active


@pytest.mark.asyncio
async def test_a_running_fabric_check_shows_the_id_it_finishes_under(monkeypatch, tmp_path, clear_job_state):
    """End-to-end wiring: the real fabric check must reserve its ledger row when it begins, so the
    id an operator sees on the running row is the id the finished row keeps. Fails on base, where
    the running fabric op shows as ``--`` and only picks up an id — a different one — once it lands."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "echo probing; sleep 0.6")

    task = asyncio.create_task(srv.health_monitor.verify_fabric_health(timeout_sec=10))
    live = None
    for _ in range(200):  # poll until the check is mid-flight
        await asyncio.sleep(0.02)
        running = [r for r in srv._recent_jobs(20) if r["status"] == "running"]
        if running:
            live = running[0]
            break
    assert live is not None, "the running fabric check never appeared in the ledger"
    assert live["job_id"].isdigit(), "the in-flight fabric row must carry a real id, not '--'"
    live_id = live["job_id"]

    ok, _ = await task
    assert ok is True
    done = [r for r in srv._recent_jobs(20) if r["status"] != "running"]
    assert any(r["job_id"] == live_id for r in done), "the finished fabric row must keep the id it showed while running"


@pytest.mark.asyncio
async def test_a_running_reset_shows_the_id_it_finishes_under(monkeypatch, tmp_path, clear_job_state):
    """The recovery-critical path: a reset reserves its ledger row at the top, before the ~60s
    subprocess, so the id on the running row is the id the finished row keeps — across all three
    exit paths. Here the normal path; fails on base, where the running reset carries no reserved id."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: True)
    monkeypatch.setattr(
        srv.recovery_mechanism,
        "_reset_scope_argv",
        lambda argv: ("tt-reset-test.scope", ["/bin/bash", "-c", "echo resetting; sleep 0.6"]),
    )

    task = asyncio.create_task(
        srv.recovery_mechanism.run_scoped(["tt-smi", "-r", "0"], lambda m: None, owner="[broker]reset-tool")
    )
    live = None
    for _ in range(200):
        await asyncio.sleep(0.02)
        running = [r for r in srv._recent_jobs(20) if r["status"] == "running"]
        if running:
            live = running[0]
            break
    assert live is not None, "the running reset never appeared in the ledger"
    assert live["job_id"].isdigit(), "the in-flight reset row must carry a real id, not '--'"
    assert live["owner"] == "[broker]reset-tool" and live["command"] == "tt-smi -r 0"
    live_id = live["job_id"]

    await task
    done = [r for r in srv._recent_jobs(20) if r["status"] != "running"]
    assert any(r["job_id"] == live_id for r in done), "the finished reset row must keep the id it showed while running"


def test_held_degraded_device_appears_as_a_hold_row_in_recent(monkeypatch, tmp_path):
    """A device held degraded with no broker op running must show as a HOLD job in history —
    otherwise the window it sat refused to every tenant is a blank in the audit trail."""
    import datetime as _dt

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "device_op_active", "")
    since = (_dt.datetime.now() - _dt.timedelta(seconds=15)).isoformat()
    monkeypatch.setattr(srv, "device_held_since", since, raising=False)
    fsm_dirty(srv, "job 088 ended timeout")

    hold = [r for r in srv._recent_jobs(20) if r["job_id"] == "--"]
    assert len(hold) == 1
    assert hold[0]["owner"] == "[broker]" and hold[0]["status"] == "running"
    assert "HELD" in hold[0]["command"] and "timeout" in hold[0]["command"]
    assert hold[0]["runtime"] and 13 < float(hold[0]["runtime"].rstrip("s")) < 25


def test_queued_job_waits_up_to_now(monkeypatch, tmp_path):
    import datetime as _dt

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    queued = (_dt.datetime.now() - _dt.timedelta(seconds=30)).isoformat()
    _footerless_log(
        tmp_path / "2026-06-11_120000_140000-1.log", "140000-1", "cy", "pytest baz", queued, started=None, envs=20
    )
    job = srv.Job(
        id="140000-1", owner="cy", workspace="/w", command="pytest baz", queued_at=queued, status=srv.JobStatus.QUEUED
    )
    monkeypatch.setattr(srv, "jobs", {"140000-1": job})

    j = srv._recent_jobs(20)[0]
    assert j["status"] == "queued"
    assert j["wait"] and 28 < float(j["wait"].rstrip("s")) < 40  # waiting, counts up to now


def test_write_action_log_appears_in_recent(monkeypatch, tmp_path):
    import re

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    srv.write_action_log("jdoe", "tt-smi -r 0,1", 42.0, "completed", 0)
    jobs = srv._recent_jobs(20)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["owner"] == "jdoe" and j["command"] == "tt-smi -r 0,1"  # action shown by COMMAND
    assert j["status"] == "completed" and j["runtime"] == "42.0s"
    assert re.fullmatch(r"\d{3}", j["job_id"])  # clean 3-digit id, no action prefix


# --- execution order, which is not queue order --------------------------------


def _finished_log(d, name, *, jid, owner, command, queued, started, finished, status, exit_code="0", runtime="1.0s"):
    (d / name).write_text(
        "=" * 70 + "\n"
        f"JOB ID:      {jid}\nOWNER:       {owner}\nCOMMAND:     {command}\n"
        f"QUEUED:      {queued}\n" + "=" * 70 + "\n"
        f"[Started at {started}]\n"
        f"FINISHED:    {finished}\nSTATUS:      {status}\n"
        f"EXIT CODE:   {exit_code}\nRUNTIME:     {runtime}\n"
    )


def test_recent_is_ordered_by_execution_not_by_queue(monkeypatch, tmp_path):
    """With a deep queue, queue order is not execution order.

    Three jobs are queued within two seconds of each other. Job 100 runs first and fails, so
    the broker repairs the device before 101 can start. Ordering by queue time bunches that
    repair up at the head of the queue, nowhere near the run that caused it. It belongs
    between 100 and 101 — where it ran, and where it explains the gap between them.
    """
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})

    _finished_log(
        tmp_path,
        "2026-07-14_100000_100.log",
        jid="100",
        owner="[agent]a",
        command="pytest A",
        queued="2026-07-14T10:00:00",
        started="2026-07-14T10:00:01",
        finished="2026-07-14T10:05:00",
        status="failed",
        exit_code="1",
        runtime="299.0s",
    )
    _finished_log(
        tmp_path,
        "2026-07-14_100001_101.log",
        jid="101",
        owner="[agent]b",
        command="pytest B",
        queued="2026-07-14T10:00:01",
        started="2026-07-14T10:07:00",
        finished="2026-07-14T10:09:00",
        status="completed",
        runtime="120.0s",
    )
    _finished_log(
        tmp_path,
        "2026-07-14_100002_102.log",
        jid="102",
        owner="[agent]c",
        command="pytest C",
        queued="2026-07-14T10:00:02",
        started="2026-07-14T10:09:30",
        finished="2026-07-14T10:10:00",
        status="completed",
        runtime="30.0s",
    )
    # The repair ran BETWEEN 100 and 101, and carries a later id because ids are handed out
    # when an action finishes.
    _finished_log(
        tmp_path,
        "2026-07-14_100500_103.log",
        jid="103",
        owner="[broker]health-gate",
        command="device reset",
        queued="2026-07-14T10:05:00",
        started="2026-07-14T10:05:00",
        finished="2026-07-14T10:06:02",
        status="completed",
        runtime="62.0s",
    )

    order = [r["job_id"] for r in srv._recent_jobs(limit=10)]
    # Newest-first by when each actually ran: C, B, repair, A.
    assert order == ["102", "101", "103", "100"], order


def test_broker_action_is_stamped_with_its_start_not_its_finish(monkeypatch, tmp_path):
    """A reset is never queued — it runs the instant the device needs it. write_action_log
    is called once it has finished, so the start has to be reconstructed; stamping the
    finish time placed a 60s repair after the job that ran *next*, not the one that
    provoked it."""
    import datetime as _dt

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)

    srv.write_action_log("[broker]health-gate", "device reset", 62.0, "completed", 0)

    log = next(p for p in tmp_path.glob("*.log"))
    body = log.read_text().splitlines()
    queued = next(ln.split(":", 1)[1].strip() for ln in body if ln.startswith("QUEUED:"))
    finished = next(ln.split(":", 1)[1].strip() for ln in body if ln.startswith("FINISHED:"))

    q = _dt.datetime.fromisoformat(queued)
    f = _dt.datetime.fromisoformat(finished)
    assert 61 <= (f - q).total_seconds() <= 63, "QUEUED must be the START, not the finish"
    assert any(ln.startswith(f"[Started at {queued}") for ln in body), "no [Started at] marker"
    # The file name carries the start too, so on-disk order is execution order.
    assert log.name.startswith(q.strftime("%Y-%m-%d_%H%M%S")), log.name

    # And it must surface with a zero wait: an action waits for nothing.
    rec = srv._recent_jobs(10)[0]
    assert rec["started_at"] == queued
    assert rec["runtime"] == "62.0s"


def test_queued_jobs_sort_ahead_of_what_has_already_run(monkeypatch, tmp_path):
    """A job that has not started has no execution time. Its place is where it will run:
    after everything that already has."""
    import datetime as _dt

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)

    _finished_log(
        tmp_path,
        "2026-07-14_100000_100.log",
        jid="100",
        owner="[agent]a",
        command="pytest A",
        queued="2026-07-14T10:00:00",
        started="2026-07-14T10:00:01",
        finished="2026-07-14T10:05:00",
        status="completed",
        runtime="299.0s",
    )
    queued = (_dt.datetime.now() - _dt.timedelta(seconds=30)).isoformat()
    _footerless_log(
        tmp_path / "2026-07-14_100001_101.log", "101", "[agent]b", "pytest B", queued, started=None, envs=10
    )
    job = srv.Job(
        id="101", owner="[agent]b", workspace="/w", command="pytest B", queued_at=queued, status=srv.JobStatus.QUEUED
    )
    monkeypatch.setattr(srv, "jobs", {"101": job})

    order = [r["job_id"] for r in srv._recent_jobs(limit=10)]
    assert order == ["101", "100"], order


# --- the footer is not the end of the file -----------------------------------
#
# The post-job health gate appends its findings AFTER the footer, so reading a fixed few
# lines from the end missed STATUS: and reported finished jobs — successful ones, exit 0 —
# as "interrupted". To their owner that reads as "the broker killed my job".


def _completed_log_with_gate_output(path, job_id, gate_lines):
    """A job log in the exact shape production writes: header, output, footer, then
    whatever the post-job health gate had to say afterwards."""
    body = "".join(f"[00:00:0{i%10}] [stdout] chatty line {i}\n" for i in range(600))
    footer = (
        "\n" + "=" * 70 + "\n"
        "FINISHED:    2026-07-17T01:07:24.174870\n"
        "STATUS:      completed\n"
        "EXIT CODE:   0\n"
        "WAIT TIME:   0.0s\n"
        "RUNTIME:     162.4s\n" + "=" * 70 + "\n"
    )
    gate = "".join(f"[01:08:0{i%10}] [broker] [health-gate/post-job] line {i}\n" for i in range(gate_lines))
    path.write_text(
        "=" * 70 + f"\nJOB ID:      {job_id}\nOWNER:       jsmith\n"
        f"WORKSPACE:   /w\nCOMMAND:     pytest x\nTIMEOUT:     1500s\n"
        f"QUEUED:      2026-07-17T01:04:41\n"
        + "=" * 70
        + "\n\n[Started at 2026-07-17T01:04:41]\n\n"
        + body
        + footer
        + gate
    )


def test_a_completed_job_is_not_called_interrupted_because_the_gate_spoke(monkeypatch, tmp_path, clear_job_state):
    """Job 016's exact shape: completed exit 0, then four post-job health-gate lines."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _completed_log_with_gate_output(tmp_path / "2026-07-17_010441_016.log", "016", gate_lines=4)

    rows = {r["job_id"]: r for r in srv._recent_jobs(10)}

    assert rows["016"]["status"] == "completed", (
        "a job that finished cleanly was reported as interrupted — its owner reads that "
        "as the broker having killed it"
    )
    assert rows["016"]["exit_code"] == "0"
    assert rows["016"]["runtime"] == "162.4s"


def test_the_footer_survives_a_reset_worth_of_gate_output(monkeypatch, tmp_path, clear_job_state):
    """A gate that resets the device writes dozens of lines after the footer."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _completed_log_with_gate_output(tmp_path / "2026-07-17_010441_020.log", "020", gate_lines=60)

    rows = {r["job_id"]: r for r in srv._recent_jobs(10)}
    assert rows["020"]["status"] == "completed"


def test_a_job_printing_status_of_its_own_is_still_unfinished(monkeypatch, tmp_path, clear_job_state):
    """No footer means no footer: a job's own output must not be mistaken for one."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    (tmp_path / "2026-07-17_010441_021.log").write_text(
        "=" * 70 + "\nJOB ID:      021\nOWNER:       jsmith\nCOMMAND:     pytest x\n"
        "QUEUED:      2026-07-17T01:04:41\n"
        + "=" * 70
        + "\n\n[Started at 2026-07-17T01:04:41]\n\n"
        + "[00:00:01] [stdout] STATUS:      completed\n"  # the job said it, not the broker
        + "[00:00:02] [stdout] EXIT CODE:   0\n"
    )

    rows = {r["job_id"]: r for r in srv._recent_jobs(10)}
    assert rows["021"]["status"] == "interrupted", "a job's own output was read as a footer"


# --- "interrupted" must say HOW ------------------------------------------------
#
# "Interrupted" is a description, not a diagnosis: it means the log had no footer and no
# broker tracked it. The broker knew why — it recorded the chip going dead and the
# holder-kill that followed, fsync'd, moments before the host went down — and then answered
# "what happened to my job?" with a shrug.


def _interrupted_log(path, job_id, started_at):
    path.write_text(
        "=" * 70 + f"\nJOB ID:      {job_id}\nOWNER:       jsmith\nWORKSPACE:   /w\n"
        f"COMMAND:     pytest x\nQUEUED:      {started_at}\n" + "=" * 70 + "\n\n"
        f"[Started at {started_at}]\n\n[22:57:00] [stdout] working\n"
    )


def _journal(tmp_path, records):
    import json as _json

    (tmp_path / "health_events.jsonl").write_text("".join(_json.dumps(r) + "\n" for r in records))


def test_an_interrupted_job_says_it_was_the_holder_kill(monkeypatch, tmp_path, clear_job_state):
    """The user's case: SIGKILLed by the dead-chip holder-kill, broker restarted 8s later."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)

    started = datetime.now() - timedelta(seconds=120)
    _interrupted_log(logs / "2026-07-16_155818_969.log", "969", started.isoformat())
    kill_ts = (started + timedelta(seconds=113)).timestamp()
    _journal(
        tmp_path,
        [
            {
                "ts": kill_ts,
                "iso": "2026-07-16T22:59:11Z",
                "kind": "device_holders_killed",
                "reason": "chip 26 reading all-ones",
                "killed": [{"pid": 1234, "user": "[broker]job"}],
                "job_id": "969",
            },
            {"ts": kill_ts + 8, "iso": "2026-07-16T22:59:19Z", "kind": "broker_start", "pid": 99},
        ],
    )

    row = {r["job_id"]: r for r in srv._recent_jobs(10)}["969"]
    assert row["status"] == "interrupted"
    assert "cause" in row, "the broker recorded why and still said nothing"
    assert "holder-kill" in row["cause"]
    assert "chip 26 reading all-ones" in row["cause"]
    assert "22:59:11" in row["cause"]


def test_an_interrupted_job_names_the_user_who_killed_it(monkeypatch, tmp_path, clear_job_state):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)

    started = datetime.now() - timedelta(seconds=60)
    _interrupted_log(logs / "2026-07-16_155818_970.log", "970", started.isoformat())
    _journal(
        tmp_path,
        [
            {
                "ts": (started + timedelta(seconds=30)).timestamp(),
                "iso": "2026-07-16T23:10:00Z",
                "kind": "job_killed",
                "job_id": "970",
                "requested_by": "ajones",
            },
        ],
    )

    row = {r["job_id"]: r for r in srv._recent_jobs(10)}["970"]
    assert "ajones" in row["cause"]


def test_an_unexplained_interruption_says_nothing_rather_than_guessing(monkeypatch, tmp_path, clear_job_state):
    """A wrong cause is worse than none: an event naming ANOTHER job is not ours to claim."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)

    started = datetime.now() - timedelta(seconds=60)
    _interrupted_log(logs / "2026-07-16_155818_971.log", "971", started.isoformat())
    _journal(
        tmp_path,
        [
            {
                "ts": (started + timedelta(seconds=10)).timestamp(),
                "iso": "2026-07-16T23:10:00Z",
                "kind": "device_holders_killed",
                "reason": "x",
                "killed": [],
                "job_id": "888",
            },
        ],
    )

    row = {r["job_id"]: r for r in srv._recent_jobs(10)}["971"]
    assert row["status"] == "interrupted"
    assert row.get("cause") is None, "claimed another job's kill as this job's cause"


def test_a_missing_journal_never_breaks_the_job_list(monkeypatch, tmp_path, clear_job_state):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path / "nonexistent")

    started = datetime.now() - timedelta(seconds=60)
    _interrupted_log(logs / "2026-07-16_155818_972.log", "972", started.isoformat())

    row = {r["job_id"]: r for r in srv._recent_jobs(10)}["972"]
    assert row["status"] == "interrupted"
    assert row.get("cause") is None


# --- a hold must survive its own ending ---------------------------------------
#
# The live HOLD row is synthetic: it exists only while the device is held, so the moment the
# device came back the window it sat refused vanished from the one place anyone looks. Seen
# on blx02: chip 2 fell off the bus, the reset "timed out", the device was held 4m39s, and
# the list jumped from the failed reset straight to the next broker start.


def test_a_finished_hold_is_written_to_the_ledger(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    monkeypatch.setattr(srv, "device_hold_episode_reason", "")
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    # The device goes degraded ...
    srv._note_tenant_gate_verdict("device is dirty and unverified: chip(s) 2 fell off the bus")
    assert srv.device_hold_episode_since, "the episode was not opened"
    # ... and comes back.
    srv._note_tenant_gate_verdict("")

    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"]
    assert len(rows) == 2, "the device sat refused and the ledger has start and end rows"
    assert "chip(s) 2 fell off the bus" in rows[1]["command"]
    assert rows[1]["status"] == "started"
    assert rows[0]["status"] == "ended"


def test_a_hold_keeps_its_id_start_and_name_from_the_ledger_to_its_release(monkeypatch, tmp_path, clear_job_state):
    """A hold is a latched state, but the ledger stability invariant is the op's: the row an operator
    watches while the device sits refused must be the row that lands when it releases — same id, same
    start, same name. On base the live hold is a synthetic ``--`` reading "device HELD (degraded)"
    that the release supersedes with a fresh id, a reconstructed start, and "refused to tenants" — all
    three fields change under the reader. Fails on base: no ``_hold_row``, so nothing is reserved."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    monkeypatch.setattr(srv, "device_hold_episode_reason", "")
    monkeypatch.setattr(srv, "_hold_row", None, raising=False)
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    # Degraded with no op running, so the live synthetic hold row shows while held.
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "chip(s) 2 fell off the bus")
    monkeypatch.setattr(srv, "device_held_since", "")

    srv._note_tenant_gate_verdict("device is dirty and unverified: chip(s) 2 fell off the bus")

    live = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"]
    assert len(live) == 1, "the held device must show one hold row"
    hrow = live[0]
    assert hrow["job_id"].isdigit(), "the live hold row must carry a real id, not '--'"
    hid, hstart, hcmd = hrow["job_id"], hrow["started_at"], hrow["command"]
    assert hrow["status"] == "started"

    srv._note_tenant_gate_verdict("")  # the device comes back

    done = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"]
    assert len(done) == 2, "two hold rows: start and end"
    assert done[0]["status"] == "ended"
    assert done[1]["status"] == "started"
    assert done[1]["job_id"] == hid, "the released hold keeps the id it showed while held"
    assert done[1]["started_at"] == hstart, "and the same start — anchored where it began"
    assert done[1]["command"] == hcmd, "and the same name"


def test_one_hold_episode_writes_start_and_end_rows(monkeypatch, tmp_path, clear_job_state):
    """A queue's worth of refusals onto one degraded device is one episode, writing start and end rows."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    monkeypatch.setattr(srv, "device_hold_episode_reason", "")
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    for _ in range(5):
        srv._note_tenant_gate_verdict("device is dirty and unverified: chip(s) 2 fell off the bus")
    srv._note_tenant_gate_verdict("")

    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"]
    assert len(rows) == 2, f"one hold wrote {len(rows)} rows (expected start and end)"
    assert rows[0]["status"] == "ended"
    assert rows[1]["status"] == "started"


def test_a_device_that_was_never_held_writes_no_row(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    srv._note_tenant_gate_verdict("")

    assert [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"] == []


def test_an_operator_reset_names_the_operator_while_it_runs(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "device_op_active", "reset")
    monkeypatch.setattr(srv, "device_op_owner", "jsmith")
    monkeypatch.setattr(srv, "device_op_detail", "device reset (~60s)")
    monkeypatch.setattr(srv, "device_op_started_at", datetime.now().isoformat())

    row = [r for r in srv._recent_jobs(20) if r["job_id"] == "--"][0]
    assert row["owner"] == "jsmith"


def test_the_brokers_own_op_still_says_broker(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    monkeypatch.setattr(srv, "device_op_owner", "[broker]")
    monkeypatch.setattr(srv, "device_op_detail", "fabric traffic pass")
    monkeypatch.setattr(srv, "device_op_started_at", datetime.now().isoformat())

    row = [r for r in srv._recent_jobs(20) if r["job_id"] == "--"][0]
    assert row["owner"] == "[broker]"


# --- a hold that outlived its broker still gets a row -------------------------
#
# The episode is in-memory. A restart while the device was held lost it, so the window it
# sat refused vanished from the ledger — seen live: held at 02:18:37, broker restarted at
# 02:24:05, and the list ran from the operator's reset straight to the next startup.


def test_a_hold_the_last_broker_died_holding_gets_a_row(monkeypatch, tmp_path, clear_job_state):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    written = []
    monkeypatch.setattr(srv, "health_event", lambda k, **kw: written.append(k))

    import json as _json
    import time as _time

    (tmp_path / "health_events.jsonl").write_text(
        _json.dumps(
            {
                "ts": _time.time() - 300,
                "iso": "2026-07-17T02:18:37Z",
                "kind": "device_held",
                "reason": "device is dirty and unverified: all 32 chips stopped answering",
            }
        )
        + "\n"
    )

    srv._close_orphaned_hold()

    rows = [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"]
    assert len(rows) == 1, "the device sat held across a restart and the ledger says nothing"
    assert "all 32 chips stopped answering" in rows[0]["command"]
    assert "restart" in rows[0]["command"]
    assert rows[0]["status"] == "ended"
    assert 280 < float(rows[0]["runtime"].rstrip("s")) < 320, "the hold's duration was lost"
    assert "device_released" in written, "the episode was left open to be re-reported forever"
    # Stamped at its END, like the normal release row: filed at the hold's start it sorts above
    # the whole recovery it closes, and every rung below reads as "after the hold ended".
    from datetime import datetime as _dt

    age = (_dt.now() - _dt.fromisoformat(rows[0]["started_at"])).total_seconds()
    assert age < 60, f"the ended row was filed at the hold's start ({age:.0f}s ago), not its end"


def test_a_closed_hold_is_not_re_reported(monkeypatch, tmp_path, clear_job_state):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    import json as _json
    import time as _time

    now = _time.time()
    (tmp_path / "health_events.jsonl").write_text(
        _json.dumps({"ts": now - 300, "kind": "device_held", "reason": "x"})
        + "\n"
        + _json.dumps({"ts": now - 200, "kind": "device_released"})
        + "\n"
    )

    srv._close_orphaned_hold()

    assert [
        r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"
    ] == [], "re-reported a hold that had already been closed"


def test_no_journal_means_no_orphaned_hold(monkeypatch, tmp_path, clear_job_state):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    srv._close_orphaned_hold()  # must not raise

    assert [r for r in srv._recent_jobs(20) if r["owner"] == "[broker]hold"] == []


# --- the row the watcher actually reads ---------------------------------------
#
# There are two in-flight rows built from the same state: _recent_jobs' and queue_status'.
# Only the first was reachable from a test, so when the row learned to name whoever asked
# for the op, this copy kept saying "[broker]" — and the test that should have caught it
# passed against the other one. An operator watching their own reset saw the broker.


def test_queue_status_names_the_operator_running_a_reset(monkeypatch, clear_job_state):
    monkeypatch.setattr(srv, "device_op_active", "reset")
    monkeypatch.setattr(srv, "device_op_owner", "jsmith")
    monkeypatch.setattr(srv, "device_op_detail", "device reset (~60s)")
    monkeypatch.setattr(srv, "device_op_started_at", datetime.now().isoformat())
    monkeypatch.setattr(srv, "device_op_stage_started_at", datetime.now().isoformat())

    row = [r for r in srv._get_queue_status()["running"] if r["id"] == "--"][0]
    assert row["owner"] == "jsmith"


def test_queue_status_still_says_broker_for_the_brokers_own_op(monkeypatch, clear_job_state):
    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    monkeypatch.setattr(srv, "device_op_owner", "[broker]")
    monkeypatch.setattr(srv, "device_op_detail", "fabric traffic pass")
    monkeypatch.setattr(srv, "device_op_started_at", datetime.now().isoformat())

    row = [r for r in srv._get_queue_status()["running"] if r["id"] == "--"][0]
    assert row["owner"] == "[broker]"


def test_both_in_flight_rows_agree_on_the_owner(monkeypatch, tmp_path, clear_job_state):
    """The two rows are built from the same state and must not drift again."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "device_op_active", "reset")
    monkeypatch.setattr(srv, "device_op_owner", "ajones")
    monkeypatch.setattr(srv, "device_op_detail", "device reset (~60s)")
    monkeypatch.setattr(srv, "device_op_started_at", datetime.now().isoformat())
    monkeypatch.setattr(srv, "device_op_stage_started_at", datetime.now().isoformat())

    live = [r for r in srv._get_queue_status()["running"] if r["id"] == "--"][0]
    recent = [r for r in srv._recent_jobs(20) if r["job_id"] == "--"][0]
    assert live["owner"] == recent["owner"] == "ajones"


def test_recent_jobs_endpoint_tolerates_an_empty_body(monkeypatch, tmp_path):
    """A bodyless (or non-JSON) POST — a bare health probe — must return the ledger with the
    default limit, not a 500. The handler only reads `.get("limit")`, so an empty body means
    defaults. Fails on base: request.json() raises JSONDecodeError -> 500 (seen in production)."""
    from starlette.testclient import TestClient

    _job_log(tmp_path / "2026-06-11_120000_120000-1.log", "120000-1", "alice", "pytest foo", "300.0s")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    client = TestClient(srv.build_asgi_app(srv.create_mcp_server()), raise_server_exceptions=False)

    r = client.post("/api/tt_device_recent_jobs")  # no body at all
    assert r.status_code == 200, f"empty body must not 500, got {r.status_code}"
    assert r.json()["jobs"], "default-limit ledger should still come back"

    r2 = client.post("/api/tt_device_recent_jobs", content=b"not json")  # garbage body
    assert r2.status_code == 200, f"non-JSON body must not 500, got {r2.status_code}"


def test_job_killed_by_recovery_gets_broker_kill_status_and_cause(monkeypatch, tmp_path):
    log = tmp_path / "2026-09-04_134342_782.log"
    log.write_text(
        "JOB ID:      782\n"
        "OWNER:       pshah\n"
        "COMMAND:     python run.py\n"
        "QUEUED:      2026-09-04T13:43:40\n"
        "[Started at 2026-09-04T13:43:42]\n"
        "running model\n"
        "[KILLED by device recovery] Device reset triggered by monitor after repeated failures\n"
        "FINISHED:    2026-09-04T13:45:04\n"
        "STATUS:      failed\n"
        "EXIT CODE:   -9\n"
        "RUNTIME:     82.5s\n"
    )
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})

    rows = srv._recent_jobs(10)
    assert len(rows) == 1
    r = rows[0]
    assert r["job_id"] == "782"
    assert r["owner"] == "pshah"
    assert r["status"] == "broker-kill"
    assert str(r["exit_code"]) == "-9"
    assert "Device reset triggered by monitor" in r.get("cause", "")
    assert r["command"].startswith("[MCP killed:")


def test_begin_action_row_creates_durable_header_file(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "_action_row", None)

    srv._begin_action_row("smarton", "tt-smi -r 0")
    try:
        row = srv._action_row
        assert row is not None
        assert row["owner"] == "smarton"
        assert row["id"].isdigit()
        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text()
        assert f"JOB ID:      {row['id']}" in content
        assert "OWNER:       smarton" in content
        assert "COMMAND:     tt-smi -r 0" in content
    finally:
        srv._action_row = None


def test_in_flight_hold_row_appears_in_recent_jobs(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    hold = {
        "id": "789",
        "owner": "[broker]hold",
        "command": "device HELD: gate/post-job failed",
        "started_at": datetime.now().isoformat(),
        "log_path": None,
    }
    monkeypatch.setattr(srv, "_hold_row", hold)

    rows = srv._recent_jobs(10)
    assert any(r.get("job_id") == "789" and r.get("owner") == "[broker]hold" for r in rows)


def test_a_queued_job_that_never_started_is_abandoned_not_interrupted(monkeypatch, tmp_path, clear_job_state):
    """Job 901 (cs04, 2026-09-16): queued into a hold, its owner gave up before it ever ran. It
    showed as "interrupted" — the word for a job the broker killed mid-run. Nothing ran."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    (logs / "2026-09-16_050100_901.log").write_text(
        "=" * 70 + "\n"
        "JOB ID:      901\nOWNER:       sjett\nCOMMAND:     /home/sjett/job-body.sh\n"
        "QUEUED:      2026-09-16T05:01:00.349696\n" + "-" * 70 + "\n"
        "[Waiting to start...]\n\n[HELD] device degraded — held for self-heal, not reset\n"
    )

    j = next(r for r in srv._recent_jobs(20) if r["job_id"] == "901")
    assert j["status"] == "abandoned"
    assert "before it started" in j["cause"]
