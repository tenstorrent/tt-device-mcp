# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A job's log and final record outlive the broker (spec 01 I71).

A restart (every self-update) and a host reboot empty the in-memory job table while every job
log stays on disk. Status, logs and wait used to answer "Job not found" for any job from before
the restart. They now answer from the job's own log; a job that was running when the broker went
away reads as lost, and the log dir stays bounded by count and size.
"""

import asyncio
import json
import os
import time

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp import job_log_store as store

RULE = "=" * 70 + "\n"


def _header(jid, owner="tester", cmd="bash job.sh"):
    return (
        RULE + f"JOB ID:      {jid}\nOWNER:       {owner}\nCOMMAND:     {cmd}\n"
        "QUEUED:      2026-10-25T22:16:55.000000\n" + RULE + "\n[Started at 2026-10-25T22:17:00.000000]\n"
    )


def _finished_log(path, jid, rc=135, status="failed"):
    job = srv.Job(
        id=jid, owner="tester", workspace="/tmp", command="bash job.sh", queued_at="2026-10-25T22:16:55.000000"
    )
    job.status = srv.JobStatus(status)
    job.exit_code = rc
    job.started_at = "2026-10-25T22:17:00.000000"
    job.finished_at = "2026-10-25T22:23:41.000000"
    path.write_text(_header(jid) + "[22:23:41] [stdout] === end rc=135\n[22:23:41] [stderr] Bus error\n")
    srv.write_job_log_footer(path, job)
    with open(path, "a") as f:  # the post-job gate writes after the footer
        f.write("[22:23:41] [broker] [health-gate/post-job] host-pci: UNHEALTHY\n")


@pytest.fixture
def restarted(monkeypatch, tmp_path):
    """A broker that just started: logs on disk, nothing in memory, the journal silent."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(srv, "job_log_dir", logs)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "_explain_interruption", lambda *a, **k: None)
    return logs


def _rest(path, body):
    from starlette.testclient import TestClient

    app = srv.build_asgi_app(srv.create_mcp_server())
    with TestClient(app) as client:
        return client.post(path, json=body).json()


def test_status_and_logs_of_a_job_from_before_the_restart(restarted, clear_job_state):
    _finished_log(restarted / "2026-10-25_221655_027.log", "027")

    st = _rest("/api/tt_device_job_status", {"job_id": "027"})
    assert st["status"] == "failed" and st["exit_code"] == 135, st
    assert st["signal"].startswith("SIGBUS"), st
    assert st["started_at"] == "2026-10-25T22:17:00.000000"
    assert st["finished_at"] == "2026-10-25T22:23:41.000000"
    assert st["owner"] == "tester" and st["runtime_sec"] > 0

    logs = _rest("/api/tt_device_job_logs", {"job_id": "027", "tail": 50})
    assert "error" not in logs, logs
    assert "Bus error" in logs["content"] and "EXIT CODE:   135" in logs["content"]
    assert logs["content"].rstrip().endswith("host-pci: UNHEALTHY")
    assert logs["log_file"].endswith("_027.log")


def test_an_unknown_id_is_still_not_found(restarted, clear_job_state):
    _finished_log(restarted / "2026-10-25_221655_027.log", "027")
    for jid in ("028", "*", "0*", "../027", "027.log"):
        assert _rest("/api/tt_device_job_status", {"job_id": jid}) == {"error": "Job not found"}, jid
        assert _rest("/api/tt_device_job_logs", {"job_id": jid}) == {"error": "Job not found"}, jid


def test_a_recycled_id_answers_with_its_newest_log(restarted):
    _finished_log(restarted / "2026-10-21_013953_027.log", "027", rc=0, status="completed")
    _finished_log(restarted / "2026-10-26_051655_027.log", "027", rc=135)
    assert store.find_job_log(restarted, "027").name == "2026-10-26_051655_027.log"
    assert srv._job_record("027")["exit_code"] == "135"
    # a longer id ending in the same digits is a different job
    (restarted / "2026-10-27_000000_1027.log").write_text(_header("1027"))
    assert store.find_job_log(restarted, "027").name == "2026-10-26_051655_027.log"


def test_a_job_running_when_the_broker_stopped_reads_lost(restarted, clear_job_state):
    log = restarted / "2026-10-25_221655_031.log"
    log.write_text(_header("031") + "[22:18:00] [stdout] step 1\n")

    st = _rest("/api/tt_device_job_status", {"job_id": "031"})
    assert st["status"] == "interrupted", st
    assert st["cause"] == srv.JOB_LOST_CAUSE and st["cause"].startswith("lost: the broker restarted")
    assert _rest("/api/tt_device_job_logs", {"job_id": "031"})["content"].endswith("step 1\n")


def test_the_journals_cause_beats_the_generic_lost(restarted, monkeypatch, clear_job_state):
    (restarted / "2026-10-25_221655_032.log").write_text(_header("032"))
    monkeypatch.setattr(srv, "_explain_interruption", lambda *a, **k: "host reboot by auto-recovery")
    st = _rest("/api/tt_device_job_status", {"job_id": "032"})
    assert st["status"] == "interrupted" and st["cause"] == "host reboot by auto-recovery", st


def test_a_queued_job_never_started_reads_abandoned_not_lost(restarted, clear_job_state):
    (restarted / "2026-10-25_221655_033.log").write_text(
        _header("033").replace("\n[Started at 2026-10-25T22:17:00.000000]\n", "\n[Waiting to start...]\n")
    )
    st = _rest("/api/tt_device_job_status", {"job_id": "033"})
    assert st["status"] == "abandoned", st


def test_a_truncated_or_unflushed_log_still_reads(restarted, clear_job_state):
    """A log cut mid-write: a footer stopped after FINISHED:, a last line with no newline that ends
    inside a multibyte character, and binary output. Read back with bad bytes replaced, never an
    exception, and the half footer is not a final status."""
    log = restarted / "2026-10-25_221655_034.log"
    body = _header("034").encode() + b"bin \xff\xfe\x00 out\n" + RULE.encode() + b"FINISHED:    2026-10-25T22:20:00\n"
    log.write_bytes(body + "[22:20:00] [stdout] café".encode()[:-1])

    logs = _rest("/api/tt_device_job_logs", {"job_id": "034", "tail": 5})
    assert "bin ��" in logs["content"], logs
    assert logs["content"].endswith("caf�"), logs["content"][-20:]
    st = _rest("/api/tt_device_job_status", {"job_id": "034"})
    assert st["status"] == "interrupted" and st["cause"] == srv.JOB_LOST_CAUSE, st


def test_an_in_memory_job_with_binary_output_reads_too(monkeypatch, tmp_path, clear_job_state):
    log = tmp_path / "2026-10-25_221655_035.log"
    log.write_bytes(b"ok\n\xff\xfe\n")
    monkeypatch.setattr(srv, "jobs", {})
    srv.jobs["035"] = srv.Job(id="035", owner="t", workspace="/tmp", command="c", queued_at="q", log_file=str(log))
    assert _rest("/api/tt_device_job_logs", {"job_id": "035"})["content"] == "ok\n��\n"


@pytest.mark.asyncio
async def test_job_wait_returns_the_final_record_of_a_pre_restart_job(restarted, clear_job_state):
    from tt_device_mcp.socket_transport import current_peer_uid, current_via_mcp

    _finished_log(restarted / "2026-10-25_221655_027.log", "027")
    mcp = srv.create_mcp_server()
    tok, mtok = current_peer_uid.set(4242), current_via_mcp.set(False)
    try:
        res = await mcp.call_tool("tt_device_job_wait", {"params": {"job_id": "027", "output_lines": 3}})
        out = json.loads(res.content[0].text)
        status = json.loads(
            (await mcp.call_tool("tt_device_job_status", {"params": {"job_id": "027"}})).content[0].text
        )
    finally:
        current_peer_uid.reset(tok)
        current_via_mcp.reset(mtok)
    assert out["status"] == "failed" and out["exit_code"] == 135, out
    assert out["output_tail"][-1].endswith("host-pci: UNHEALTHY"), out
    assert status["status"] == "failed" and status["signal"].startswith("SIGBUS"), status


def test_the_footer_names_the_signal_and_is_parsed_back(tmp_path):
    log = tmp_path / "2026-10-25_221655_040.log"
    _finished_log(log, "040", rc=-9, status="killed")
    footer = srv._parse_job_log_footer(log.read_text().splitlines(keepends=True))
    assert footer["signal"] == "SIGKILL" and footer["status"] == "killed"
    _finished_log(log, "040", rc=0, status="completed")
    assert "signal" not in srv._parse_job_log_footer(log.read_text().splitlines(keepends=True))


@pytest.mark.parametrize(
    "rc,want",
    [(0, None), (1, None), (128, None), (None, None), ("x", None), (-11, "SIGSEGV"), (137, "SIGKILL (from exit")],
)
def test_exit_signal(rc, want):
    got = store.exit_signal(rc)
    assert (got is None) if want is None else got.startswith(want), (rc, got)


def _old_log(d, name, size=10, age=7200):
    p = d / name
    p.write_bytes(b"x" * size)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_retention_keeps_the_newest_n(tmp_path):
    for i in range(10):
        _old_log(tmp_path, f"2026-10-2{i}_000000_{i:03d}.log")
    (tmp_path / "server.log").write_text("keep")
    (tmp_path / "server.log.1").write_text("keep")
    gone = store.prune_job_logs(tmp_path, max_count=4, max_bytes=1 << 30)
    assert sorted(p.name for p in gone) == [f"2026-10-2{i}_000000_{i:03d}.log" for i in range(6)]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == [f"2026-10-2{i}_000000_{i:03d}.log" for i in range(6, 10)] + ["server.log", "server.log.1"]


def test_retention_bounds_bytes_and_keeps_only_the_newest(tmp_path):
    _old_log(tmp_path, "2026-10-20_000000_001.log", size=10)
    _old_log(tmp_path, "2026-10-21_000000_002.log", size=10)
    _old_log(tmp_path, "2026-10-22_000000_003.log", size=500)
    _old_log(tmp_path, "2026-10-23_000000_004.log", size=100)
    gone = store.prune_job_logs(tmp_path, max_count=100, max_bytes=300)
    # 004 fits; 003 would pass the bound, so it and everything older goes, small ones too
    assert sorted(p.name[-7:] for p in gone) == ["001.log", "002.log", "003.log"]


def test_retention_never_prunes_a_tracked_or_fresh_log(tmp_path):
    _old_log(tmp_path, "2026-10-20_000000_001.log")  # tracked (queued or re-adopted)
    _old_log(tmp_path, "2026-10-21_000000_002.log", age=60)  # written a minute ago
    _old_log(tmp_path, "2026-10-22_000000_003.log")
    _old_log(tmp_path, "2026-10-23_000000_004.log")
    gone = store.prune_job_logs(tmp_path, keep_ids={"001"}, max_count=1, max_bytes=1 << 30)
    assert [p.name[-7:] for p in gone] == ["003.log"]


def test_retention_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_JOB_LOG_KEEP", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_JOB_LOG_MAX_MB", raising=False)
    assert store.job_log_limits() == (store.JOB_LOG_KEEP_DEFAULT, store.JOB_LOG_MAX_MB_DEFAULT << 20)
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_LOG_KEEP", "50")
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_LOG_MAX_MB", "junk")
    assert store.job_log_limits() == (50, store.JOB_LOG_MAX_MB_DEFAULT << 20)
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_LOG_KEEP", "0")
    assert store.job_log_limits()[0] == store.JOB_LOG_KEEP_DEFAULT


def test_read_log_tail_edge_cases(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"")
    assert store.read_log_tail(p, 5) == ""
    assert store.read_log_tail(tmp_path / "missing.log", 5) == ""
    p.write_bytes(b"".join(b"line %d\n" % i for i in range(50000)))
    assert store.read_log_tail(p, 2, block=7) == "line 49998\nline 49999\n"
    assert store.read_log_tail(p, 0) == ""


def test_a_job_purged_after_retention_still_answers(restarted, clear_job_state):
    """No restart: the same broker drops a finished job from memory after its retention window,
    and its status, logs and wait answer from the log just the same."""
    log = restarted / "2026-10-25_221655_041.log"
    _finished_log(log, "041", rc=0, status="completed")
    job = srv.Job(id="041", owner="tester", workspace="/tmp", command="c", queued_at="2026-10-25T22:16:55")
    job.status, job.exit_code, job.log_file = srv.JobStatus.COMPLETED, 0, str(log)
    job.finished_at = (srv.datetime.now() - srv.timedelta(seconds=srv.JOB_RETENTION_SEC + 60)).isoformat()
    srv.jobs["041"] = job
    asyncio.run(srv.cleanup_finished_jobs())
    assert "041" not in srv.jobs
    st = _rest("/api/tt_device_job_status", {"job_id": "041"})
    assert st["status"] == "completed" and st["exit_code"] == 0 and "signal" not in st, st
    assert "EXIT CODE:   0" in _rest("/api/tt_device_job_logs", {"job_id": "041"})["content"]
