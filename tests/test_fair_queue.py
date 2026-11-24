# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The fair (round-robin by person) queue policy (spec 01 I72).

Agents submit in batches: one person's sweep of twenty jobs used to sit in front of everyone else's
single run for hours. Under ``TT_DEVICE_MCP_QUEUE_POLICY=fair`` each person gets one job per turn;
with nobody else waiting the order is unchanged.
"""

import asyncio
import json
import os
from datetime import datetime

import pytest

from tt_device_mcp import server as srv


def _job(jid, owner, command="pytest x", peer_uid=1000, status=None):
    job = srv.Job(
        id=jid, owner=owner, workspace="/tmp", command=command, queued_at=datetime.now().isoformat(), peer_uid=peer_uid
    )
    if status is not None:
        job.status = status
    return job


async def _submit(*specs):
    for jid, owner in specs:
        job = _job(jid, owner)
        srv.jobs[jid] = job
        await srv._enqueue_job(job)


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.fixture
def fair(monkeypatch):
    monkeypatch.setenv(srv.QUEUE_POLICY_ENV, "fair")


@pytest.mark.parametrize("value,expected", [(None, "fifo"), ("fair", "fair"), (" FAIR ", "fair"), ("lifo", "fifo")])
def test_the_policy_defaults_to_fifo_and_an_unknown_value_reorders_nothing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(srv.QUEUE_POLICY_ENV, raising=False)
    else:
        monkeypatch.setenv(srv.QUEUE_POLICY_ENV, value)
    assert srv.queue_policy() == expected


@pytest.mark.asyncio
async def test_fifo_keeps_a_batch_in_front_of_a_later_single_job(clear_job_state, monkeypatch):
    monkeypatch.delenv(srv.QUEUE_POLICY_ENV, raising=False)
    await _submit(*[(f"00{i}", "[agent]alice") for i in range(4)], ("100", "bob"))
    assert _drain(srv.get_job_queue()) == ["000", "001", "002", "003", "100"]


@pytest.mark.asyncio
async def test_fair_interleaves_a_batch_with_a_later_single_job(clear_job_state, fair):
    await _submit(*[(f"00{i}", "[agent]alice") for i in range(4)], ("100", "bob"))
    q = srv.get_job_queue()
    assert q.dispatch_order() == ["000", "100", "001", "002", "003"]
    assert _drain(q) == ["000", "100", "001", "002", "003"]


@pytest.mark.asyncio
async def test_fair_puts_a_newcomer_next_when_the_batch_owner_was_just_served(clear_job_state, fair):
    """Alice's first job is already running when Bob arrives: Bob is next, not after Alice's second."""
    await _submit(*[(f"00{i}", "[agent]alice") for i in range(4)])
    q = srv.get_job_queue()
    assert q.get_nowait() == "000"  # dispatched
    srv.jobs["000"].status = srv.JobStatus.RUNNING
    await _submit(("100", "bob"), ("101", "bob"))
    assert _drain(q) == ["100", "001", "101", "002", "003"]


@pytest.mark.asyncio
async def test_fair_is_fifo_with_one_job_per_person(clear_job_state, fair):
    await _submit(("000", "carol"), ("001", "alice"), ("002", "bob"))
    assert _drain(srv.get_job_queue()) == ["000", "001", "002"]


@pytest.mark.asyncio
async def test_fair_is_fifo_with_one_person(clear_job_state, fair):
    await _submit(*[(f"00{i}", "alice") for i in range(5)])
    assert _drain(srv.get_job_queue()) == ["000", "001", "002", "003", "004"]


@pytest.mark.asyncio
async def test_a_persons_agent_and_cli_jobs_share_one_turn(clear_job_state, fair):
    await _submit(("000", "[agent]alice"), ("001", "alice"), ("002", "[agent]alice"), ("003", "bob"))
    assert _drain(srv.get_job_queue()) == ["000", "003", "001", "002"]


@pytest.mark.asyncio
async def test_three_people_rotate_in_the_order_they_joined(clear_job_state, fair):
    await _submit(
        ("000", "alice"), ("001", "alice"), ("002", "alice"),
        ("100", "bob"), ("101", "bob"),
        ("200", "carol"),
    )  # fmt: skip
    assert _drain(srv.get_job_queue()) == ["000", "100", "200", "001", "101", "002"]


async def _verification(jid="900"):
    ver = _job(jid, "op", peer_uid=0)
    ver.verification = "fabric"
    srv.jobs[jid] = ver
    await srv._enqueue_job(ver)


@pytest.mark.asyncio
async def test_fair_keeps_verification_at_the_head_and_a_hand_back_behind_it(clear_job_state, fair):
    """The hand-back is alice's own tracked job, so running it from the front is her turn: bob,
    already waiting, goes before her next one, and the listing predicts exactly that."""
    await _submit(("000", "alice"), ("001", "alice"), ("100", "bob"))
    q = srv.get_job_queue()
    await _verification()
    srv.jobs["050"] = _job("050", "[agent]alice")
    q.put_back_nowait("050")  # a held tenant job the runner had dequeued
    assert q.verification_waiting()
    predicted = q.dispatch_order()
    assert predicted == ["900", "050", "100", "000", "001"]
    assert _drain(q) == predicted


@pytest.mark.asyncio
async def test_fair_predicts_the_turn_a_hand_back_spends(clear_job_state, fair):
    """PR #271 review: alice's A0 is held and handed back behind a verification job, bob arrives while
    the verification job runs. The runner takes A0, then bob, then alice's A1; the listing said A1
    before bob, because it did not count A0's run from the front as alice's turn."""
    await _submit(("000", "alice"), ("001", "alice"))
    q = srv.get_job_queue()
    assert q.get_nowait() == "000"  # dequeued, then held at admission
    await _verification()
    q.put_back_nowait("000")  # yields to the verification job
    assert q.get_nowait() == "900"
    await _submit(("100", "bob"))  # while the verification job runs
    predicted = q.dispatch_order()
    assert predicted == ["000", "100", "001"]
    assert _drain(q) == predicted


@pytest.mark.asyncio
async def test_a_job_killed_while_queued_drains_first_and_costs_its_owner_no_turn(clear_job_state, fair):
    await _submit(("000", "alice"), ("001", "alice"), ("100", "bob"), ("101", "bob"))
    srv.jobs["100"].status = srv.JobStatus.KILLED
    q = srv.get_job_queue()
    assert q.dispatch_order() == ["100", "000", "101", "001"]
    assert q.get_nowait() == "100"  # the runner pops and skips it
    assert _drain(q) == ["000", "101", "001"]


@pytest.mark.asyncio
async def test_status_lists_and_submit_reports_the_fair_position(clear_job_state, fair):
    await _submit(*[(f"00{i}", "[agent]alice") for i in range(3)], ("100", "bob"))
    status = srv._get_queue_status()
    assert [(r["id"], r["position"]) for r in status["queued"]] == [("000", 1), ("100", 2), ("001", 3), ("002", 4)]


@pytest.fixture
def submit(monkeypatch, tmp_path):
    """Submit through the real MCP tool (the `_queue_job` funnel) as this process's own uid, with
    no runner: the queue holds what the test puts there."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "get_activation_script", lambda *a, **k: ("", {}))
    monkeypatch.setattr(srv, "job_runner_task", None)
    monkeypatch.setattr(srv, "job_runner", lambda: asyncio.Event().wait())
    monkeypatch.setattr(srv, "_device_unavailable_for_tenant", lambda: "")
    mcp = srv.create_mcp_server()

    async def _go(command="true"):
        tok, mtok = srv.current_peer_uid.set(os.getuid()), srv.current_via_mcp.set(False)
        try:
            res = await mcp.call_tool("tt_device_job_run_bg", {"params": {"workspace": str(tmp_path), "command": command}})
        finally:
            srv.current_peer_uid.reset(tok)
            srv.current_via_mcp.reset(mtok)
        return json.loads(res.content[0].text)

    return _go


@pytest.mark.asyncio
async def test_a_submit_reports_the_position_status_lists(clear_job_state, fair, submit):
    """PR #271 review: a job the runner already holds at admission is still QUEUED and next; the
    submit used to count only the deque and report 1 while status listed the new job 2nd."""
    running = _job("800", "carol")
    running.status = srv.JobStatus.RUNNING
    srv.jobs["800"] = running
    await _submit(("000", "alice"))
    assert srv.get_job_queue().get_nowait() == "000"  # dequeued, held at admission, still QUEUED
    res = await submit()
    listed = {r["id"]: r["position"] for r in srv._get_queue_status()["queued"]}
    assert res["status"] == "queued"
    assert res["position"] == listed[res["job_id"]] == 2


@pytest.mark.asyncio
async def test_a_submit_the_runner_took_during_the_prune_reports_it_started(
    clear_job_state, fair, submit, monkeypatch
):
    """PR #271 review: the submit answers after an await (log pruning). A job the runner started in
    that window is not queued behind anyone, itself included."""
    running = _job("800", "carol")
    running.status = srv.JobStatus.RUNNING
    srv.jobs["800"] = running

    def _runner_takes_it(*a, **k):  # the runner dequeues and starts the new job mid-await
        newest = [j for j in srv.jobs.values() if j.id != "800"][-1]
        running.status = srv.JobStatus.COMPLETED
        newest.status = srv.JobStatus.RUNNING

    monkeypatch.setattr(srv, "prune_job_logs", _runner_takes_it)
    res = await submit()
    assert res["status"] == "starting" and res["position"] == 0, res
    assert "waiting for" not in res["message"]
