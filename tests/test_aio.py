# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""aio.wait_for: asyncio.wait_for's contract, minus the dropped cancel (bpo-42130)."""

import asyncio

import pytest

from tt_device_mcp import aio


@pytest.mark.asyncio
async def test_a_cancel_that_lands_as_the_inner_work_ends_is_not_dropped():
    """The CI hang: on Python 3.10, asyncio.wait_for returns the result here and the cancel is
    lost, so job_runner went back to its queue and shutdown waited on it forever."""
    inner = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(aio.wait_for(inner, timeout=10))
    await asyncio.sleep(0)  # parked in the wait
    inner.set_result("done")
    task.cancel()  # same loop step as the completion
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_also_cancels_the_inner_work():
    inner = asyncio.ensure_future(asyncio.sleep(10))
    task = asyncio.ensure_future(aio.wait_for(inner, timeout=10))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.cancelled()


@pytest.mark.asyncio
async def test_timeout_cancels_the_inner_work_and_raises_timeout():
    inner = asyncio.ensure_future(asyncio.sleep(10))
    with pytest.raises(asyncio.TimeoutError):
        await aio.wait_for(inner, timeout=0.01)
    assert inner.cancelled()


@pytest.mark.asyncio
async def test_a_timed_out_gather_raises_timeout_not_cancel():
    """A cancelled gather ends with CancelledError as its exception rather than in the cancelled
    state. The job runner waits on a gather of its stream readers, and a cancel there reads as a
    broker shutdown: the job is left running and the runner exits."""
    a = asyncio.ensure_future(asyncio.sleep(10))
    b = asyncio.ensure_future(asyncio.sleep(10))
    with pytest.raises(asyncio.TimeoutError):
        await aio.wait_for(asyncio.gather(a, b), timeout=0.01)
    assert a.cancelled() and b.cancelled()


@pytest.mark.asyncio
async def test_result_and_exception_pass_through():
    async def ok():
        return 7

    async def boom():
        raise ValueError("x")

    assert await aio.wait_for(ok(), timeout=1) == 7
    assert await aio.wait_for(ok(), timeout=None) == 7
    with pytest.raises(ValueError):
        await aio.wait_for(boom(), timeout=1)


def test_the_broker_tasks_that_shutdown_cancels_use_aio_wait_for():
    """The job runner and the startup tasks must end on the shutdown cancel. Every plain
    asyncio.wait_for in server.py is one more place a cancel can be dropped (the output-dir
    preflight was one: the runner then ran the job and waited on its queue forever), so only the
    request handlers and the route-side wait below may keep it."""
    import ast
    from pathlib import Path

    from tt_device_mcp import server

    allowed = {"create_mcp_server", "_await_external_steps_clear"}
    tree = ast.parse(Path(server.__file__).read_text())
    offenders = []
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) or top.name in allowed:
            continue
        for node in ast.walk(top):
            f = getattr(node, "func", None)
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "wait_for"
                and isinstance(f.value, ast.Name)
                and f.value.id == "asyncio"
            ):
                offenders.append(f"{top.name}:{node.lineno}")
    assert not offenders, f"use aio.wait_for (bpo-42130): {offenders}"
