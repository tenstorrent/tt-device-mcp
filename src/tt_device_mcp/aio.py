# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""asyncio helpers for tt-device-mcp."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Optional, TypeVar

T = TypeVar("T")


async def wait_for(aw: Awaitable[T], timeout: Optional[float]) -> T:
    """asyncio.wait_for that never drops a cancel.

    Before Python 3.12, asyncio.wait_for returns the inner result when a cancel lands in the same
    loop step as the inner awaitable finishing, and the cancel is lost (bpo-42130). A task that
    must end when cancelled then carries on: job_runner finished its job, went back to
    `queue.get()` and waited forever, so shutdown never completed. asyncio.wait always lets the
    caller's cancel through.

    Otherwise the same contract: on timeout or cancel the inner awaitable is cancelled and waited
    for, then asyncio.TimeoutError or CancelledError is raised. An inner awaitable that finishes
    anyway after the timeout's cancel returns its result, as wait_for does.
    """
    fut = asyncio.ensure_future(aw)
    try:
        done, _ = await asyncio.wait({fut}, timeout=timeout)
    except BaseException:
        await _cancel_and_wait(fut)
        raise
    if not done:
        await _cancel_and_wait(fut)
        if fut.cancelled():
            raise asyncio.TimeoutError
    return fut.result()


async def _cancel_and_wait(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.cancel()
        await asyncio.wait({fut})
    elif not fut.cancelled():
        fut.exception()  # retrieve it, so a result we are dropping never logs "never retrieved"
