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
async def test_result_and_exception_pass_through():
    async def ok():
        return 7

    async def boom():
        raise ValueError("x")

    assert await aio.wait_for(ok(), timeout=1) == 7
    assert await aio.wait_for(ok(), timeout=None) == 7
    with pytest.raises(ValueError):
        await aio.wait_for(boom(), timeout=1)
