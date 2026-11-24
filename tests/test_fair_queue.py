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


# --- fair_time: fair by device time (spec 01 I73) ------------------------------------------------


@pytest.fixture
def fair_time(monkeypatch):
    monkeypatch.setenv(srv.QUEUE_POLICY_ENV, "fair_time")


def _run_all(q, cost):
    """Take every queued job as the runner would, billing each its device seconds from ``cost``."""
    out = []
    while not q.empty():
        jid = q.get_nowait()
        out.append(jid)
        q.charge(jid, cost[jid])
        if jid in srv.jobs:
            srv.jobs[jid].status = srv.JobStatus.COMPLETED
    return out


@pytest.mark.asyncio
async def test_fair_time_lets_short_jobs_through_while_a_long_one_is_paid_for(clear_job_state, fair_time):
    """Round-robin by job would run a1 b1 a2 b2 ...: bob's minute-long jobs each waiting out a 25 min one."""
    await _submit(("000", "[agent]alice"), ("001", "[agent]alice"), *[(f"10{i}", "bob") for i in range(5)])
    cost = {"000": 1500, "001": 1500, **{f"10{i}": 60 for i in range(5)}}
    assert _run_all(srv.get_job_queue(), cost) == ["000", "100", "101", "102", "103", "104", "001"]


@pytest.mark.asyncio
async def test_fair_time_with_equal_costs_is_round_robin(clear_job_state, fair_time):
    await _submit(("000", "alice"), ("001", "alice"), ("002", "alice"), ("100", "bob"), ("101", "bob"))
    cost = dict.fromkeys(["000", "001", "002", "100", "101"], 100)
    assert _run_all(srv.get_job_queue(), cost) == ["000", "100", "001", "101", "002"]


@pytest.mark.asyncio
async def test_fair_time_puts_a_newcomer_next_after_a_long_job(clear_job_state, fair_time):
    await _submit(("000", "alice"), ("001", "alice"), ("002", "alice"))
    q = srv.get_job_queue()
    assert q.get_nowait() == "000"
    await _submit(("100", "bob"))  # arrives while alice's 20-minute job runs
    q.charge("000", 1200)
    assert _run_all(q, {"001": 1200, "002": 1200, "100": 60}) == ["100", "001", "002"]


@pytest.mark.asyncio
async def test_fair_time_banks_no_credit_and_no_debt_across_an_idle_queue(clear_job_state, fair_time):
    """Alice ran alone all morning; once the queue drains, she and bob start even."""
    await _submit(("000", "alice"), ("001", "alice"), ("002", "alice"))
    q = srv.get_job_queue()
    assert _run_all(q, dict.fromkeys(["000", "001", "002"], 1000)) == ["000", "001", "002"]
    await _submit(("003", "alice"), ("100", "bob"), ("004", "alice"), ("101", "bob"))
    assert _run_all(q, dict.fromkeys(["003", "004", "100", "101"], 100)) == ["003", "100", "004", "101"]


@pytest.mark.asyncio
async def test_fair_time_bills_nothing_for_a_job_that_never_ran(clear_job_state, fair_time):
    """Dispatched then refused at the door: the runner never charges it, and it costs its person
    nothing, so alice's next job is still level with bob's and goes first on arrival."""
    await _submit(("000", "alice"), ("001", "alice"), ("100", "bob"), ("101", "bob"))
    q = srv.get_job_queue()
    assert q.get_nowait() == "000"
    srv.jobs["000"].status = srv.JobStatus.FAILED  # refused at the door: no charge follows
    assert _run_all(q, {"001": 500, "100": 100, "101": 100}) == ["001", "100", "101"]


@pytest.mark.asyncio
async def test_fair_time_keeps_verification_at_the_head_and_bills_it_to_nobody(clear_job_state, fair_time):
    await _submit(("000", "alice"), ("100", "bob"))
    ver = _job("900", "op", peer_uid=0)
    ver.verification = "fabric"
    srv.jobs["900"] = ver
    await srv._enqueue_job(ver)
    q = srv.get_job_queue()
    assert q.dispatch_order() == ["900", "000", "100"]
    assert q.get_nowait() == "900"
    q.charge("900", 5000)  # not a tenant dispatch: ignored
    assert _run_all(q, {"000": 10, "100": 10}) == ["000", "100"]


@pytest.mark.asyncio
async def test_fair_time_status_predicts_positions_from_each_persons_history(clear_job_state, fair_time):
    q = srv.get_job_queue()
    await _submit(("000", "alice"), ("100", "bob"))
    _run_all(q, {"000": 1200, "100": 60})  # alice's jobs run 20 min, bob's 1 min
    await _submit(("001", "alice"), ("002", "alice"), ("101", "bob"), ("102", "bob"), ("103", "bob"))
    assert [r["id"] for r in srv._get_queue_status()["queued"]] == ["001", "101", "102", "103", "002"]


@pytest.mark.asyncio
async def test_fair_time_estimate_never_exceeds_the_jobs_own_timeout(clear_job_state, fair_time):
    q = srv.get_job_queue()
    await _submit(("000", "alice"))
    _run_all(q, {"000": 1500})
    await _submit(("001", "alice"))
    srv.jobs["001"].timeout_sec = 30
    assert q._estimate("001") == 30


@pytest.mark.asyncio
async def test_the_runner_bills_run_time_plus_the_post_job_gate(monkeypatch, tmp_path, clear_job_state, fair_time):
    from tests.test_device_safety import _free_device_lock, _no_workspace_activation, _wait_for

    _free_device_lock(monkeypatch)
    _no_workspace_activation(monkeypatch)
    import asyncio

    async def _slow_gate(job_log_file, job_failed=False, clean_exit_code=None):
        await asyncio.sleep(0.3)  # the post-job health gate is device time too

    monkeypatch.setattr(srv, "_verify_device_after_job", _slow_gate)
    charges = []
    real_charge = srv.JobQueue.charge
    monkeypatch.setattr(srv.JobQueue, "charge", lambda self, i, s: (charges.append((i, s)), real_charge(self, i, s)))
    job = _job("700", "alice", command="sleep 0.2")
    job.log_file = str(tmp_path / "700.log")
    srv.jobs["700"] = job
    await srv._enqueue_job(job)
    runner = asyncio.create_task(srv.job_runner())
    try:
        await _wait_for(lambda: charges)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert job.status is srv.JobStatus.COMPLETED
    assert charges[0][0] == "700" and charges[0][1] >= 0.5, charges
    assert srv.get_job_queue()._finish["alice"] >= 0.5
