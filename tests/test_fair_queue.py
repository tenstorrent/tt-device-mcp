# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The fair (round-robin by person) queue policy (spec 01 I72).

Agents submit in batches: one person's sweep of twenty jobs used to sit in front of everyone else's
single run for hours. Under ``TT_DEVICE_MCP_QUEUE_POLICY=fair`` each person gets one job per turn;
with nobody else waiting the order is unchanged.
"""

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


@pytest.mark.asyncio
async def test_fair_keeps_verification_at_the_head_and_a_hand_back_behind_it(clear_job_state, fair, monkeypatch):
    await _submit(("000", "alice"), ("001", "alice"), ("100", "bob"))
    q = srv.get_job_queue()
    ver = _job("900", "op", peer_uid=0)
    ver.verification = "fabric"
    srv.jobs["900"] = ver
    await srv._enqueue_job(ver)
    q.put_back_nowait("050")  # a held tenant job the runner had dequeued
    assert q.verification_waiting()
    assert _drain(q) == ["900", "050", "000", "100", "001"]


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
    assert srv.get_job_queue().position("100") == 2
