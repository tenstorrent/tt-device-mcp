# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Event-loop stall watchdog.

Every broker surface (/health, the REST API, the MCP tools, the sampler, the job runner) shares one
asyncio loop. A synchronous call on it that blocks (a sysfs read at a chip that is slow to answer, a
subprocess, a lock) freezes all of them at once, and from outside the broker simply stops answering.

A coroutine ticks a timestamp a few times a second; a daemon thread watches the tick. When the loop
has not ticked for ``LOOP_LAG_WARN_SEC`` the thread logs the loop thread's current stack (in-process,
``sys._current_frames``), which names the call that is blocking it while it still blocks. When the
loop resumes, the coroutine logs how long it was blocked.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import Optional

# Loop lag that is worth a warning: well above scheduling noise, well below a client's timeout.
LOOP_LAG_WARN_SEC = 2.0
# How often the loop ticks and the thread looks.
TICK_SEC = 0.25

_log = logging.getLogger("tt_device_mcp.loop_watch")

_state: dict = {"loop": None, "thread": None, "task": None, "tick": 0.0, "loop_thread_id": None}
_stack_reported: Optional[float] = None  # tick value of the stall whose stack was already logged


def loop_stack(thread_id: int, depth: int = 6) -> str:
    """The innermost ``depth`` frames of a thread, innermost first, as ``file:line func`` items."""
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "unknown"
    frames = traceback.extract_stack(frame)[-depth:]
    return " <- ".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}" for f in reversed(frames))


async def _ticker(warn_sec: float, tick_sec: float, log: logging.Logger) -> None:
    while True:
        before = time.monotonic()
        _state["tick"] = before
        await asyncio.sleep(tick_sec)
        lag = time.monotonic() - before - tick_sec
        if lag >= warn_sec:
            log.warning("event loop was blocked for %.1fs", lag)


def _watcher(warn_sec: float, tick_sec: float, log: logging.Logger, loop: asyncio.AbstractEventLoop) -> None:
    global _stack_reported
    while not loop.is_closed() and _state["loop"] is loop:
        time.sleep(tick_sec)
        tick = _state["tick"]
        stalled = time.monotonic() - tick
        if stalled >= warn_sec and _stack_reported != tick and loop.is_running():
            _stack_reported = tick
            log.warning(
                "event loop stalled %.1fs so far, blocked in: %s", stalled, loop_stack(_state["loop_thread_id"])
            )


def start(
    log: Optional[logging.Logger] = None, warn_sec: float = LOOP_LAG_WARN_SEC, tick_sec: float = TICK_SEC
) -> None:
    """Start the watchdog on the running loop. Idempotent per loop."""
    loop = asyncio.get_running_loop()
    task = _state["task"]
    if _state["loop"] is loop and task is not None and not task.done():
        return
    log = log or _log
    _state.update(loop=loop, tick=time.monotonic(), loop_thread_id=threading.get_ident())
    _state["task"] = loop.create_task(_ticker(warn_sec, tick_sec, log), name="loop-lag-watch")
    thread = threading.Thread(
        target=_watcher, args=(warn_sec, tick_sec, log, loop), name="loop-lag-watch", daemon=True
    )
    _state["thread"] = thread
    thread.start()


def stop() -> None:
    """Stop the watchdog (tests)."""
    task = _state["task"]
    if task is not None and not task.done():
        task.cancel()
    _state.update(loop=None, task=None)
    thread = _state["thread"]
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2)
    _state["thread"] = None
