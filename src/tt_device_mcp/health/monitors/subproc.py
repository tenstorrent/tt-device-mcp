# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The subprocess runner shared by every probe that pushes real traffic or reads real firmware
state and so must be killable mid-flight without losing what it already said.

Every probe here gets its own session (``start_new_session=True``, so its pid is its pgid) —
that is what lets a caller signal the whole group on timeout without the group id resolving to
the broker's own. And every probe is tracked for the life of the call: it is mapping chip BARs,
so a chip that leaves the PCIe bus mid-check is the likeliest thing left reading a dead address,
and the caller (see ``server._kill_device_holders``) needs a live handle to kill it.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Awaitable, Callable, Optional

from tt_device_mcp import aio
from tt_device_mcp.device_holders import note_broker_internal
from tt_device_mcp.health import chip_state
from tt_device_mcp.health.shutdown import SIGNAL_RCS, interrupted_by_shutdown

Track = Callable[[Optional["asyncio.subprocess.Process"]], None]
Terminate = Callable[[int], Awaitable[None]]
# Returns why the caller stopped waiting on the probe, or "" to keep waiting.
Abort = Callable[[], str]

# How often the wait reads ``abort``: a flag read, no I/O.
ABORT_POLL_SEC = 0.5


async def _killpg(pid: int) -> None:
    """Default timeout response when the caller supplies no gentler ladder: SIGKILL the whole
    group at once. Own-session spawn makes pid the pgid, so this reaches the probe's own
    children and never the broker's."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass  # already gone


def _stop_group(pid: int) -> None:
    """SIGSTOP the probe's group: it reads nothing more and closes no fd."""
    try:
        os.killpg(pid, signal.SIGSTOP)
    except (ProcessLookupError, OSError):
        pass


async def _wait_or_abort(aw: Awaitable, abort: Optional[Abort], timeout: Optional[float] = None):
    """Await ``aw`` (bounded by ``timeout`` when given) until it ends or ``abort`` gives a reason.
    Returns ``(reason, result)``: ``reason`` "" when ``aw`` ended. Raises what ``aw`` raises."""
    bounded = aio.wait_for(aw, timeout=timeout) if timeout is not None else aw
    if abort is None:
        return "", await bounded
    main = asyncio.ensure_future(bounded)

    async def _watch() -> str:
        while True:
            why = abort()
            if why:
                return why
            await asyncio.sleep(ABORT_POLL_SEC)

    watch = asyncio.ensure_future(_watch())
    try:
        await asyncio.wait({main, watch}, return_when=asyncio.FIRST_COMPLETED)
        if main.done():
            return "", main.result()
        return watch.result(), None
    finally:
        for t in (main, watch):
            if not t.done():
                t.cancel()
        await asyncio.gather(main, watch, return_exceptions=True)


async def run_probe(
    argv: list[str],
    *,
    timeout_sec: float,
    track: Track,
    stream: bool,
    env: dict[str, str],
    cwd: Optional[str] = None,
    terminate: Terminate = _killpg,
    reap_sec: float = 0.0,
    name: str = "",
    preexec_fn: Optional[Callable[[], None]] = None,
    abort: Optional[Abort] = None,
) -> tuple[Optional[int], bytes]:
    """Run ``argv`` to completion or to ``timeout_sec``, whichever comes first.

    Returns ``(rc, output)``. ``rc`` is ``None`` on timeout — never a sentinel exit code, so a
    caller cannot mistake "ran out of time" for anything the probe itself returned — and
    ``terminate`` has already been awaited against the process group by the time this returns.

    ``stream=True`` drains stdout line by line as it arrives instead of waiting for EOF via
    ``communicate()``. The probe most worth reading is the one that times out, and
    ``communicate()`` yields nothing once the task awaiting it is cancelled — a streaming drain
    is what lets a caller still see everything printed up to the moment it gave up.

    ``track`` registers the live process for the caller's dead-chip kill path for exactly the duration of the call, and
    is always cleared again before returning, on every exit path.

    ``reap_sec`` > 0 waits up to that long, after ``terminate``, for the killed process to finish exiting. Until it
    does, the driver can still list its pid while its fd table is already gone, so a holder scan run right after a
    timed-out probe reads as incomplete and defers a reset the broker's own dead probe should never block.

    A probe that ends by SIGTERM/SIGKILL while the host or the broker is shutting down raises
    ``shutdown.CheckInterruptedByShutdown`` (``name`` labels it, else ``argv[0]``): the shutdown killed it, it measured
    nothing, and no caller may count it (spec 03 I63).

    ``preexec_fn`` runs in the child before exec (e.g. to set a resource limit on the probe alone).

    ``abort`` (polled every :data:`ABORT_POLL_SEC`) ends the wait early, also inside the timeout path's wait and
    ``terminate``: the group is SIGSTOPped, never killed, and ``(None, output)`` returns at once. The caller reads
    ``abort`` again to tell it from a timeout (spec 04 I76: PCI-wedged, no fd of a reader may close before the power
    cycle)."""
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=cwd,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )
        # The broker's own device work: a holder scan taken while it exits is not a tenant (I59).
        note_broker_internal(proc.pid)
        track(proc)
        chunks: list[bytes] = []
        if stream:

            async def _drain() -> None:
                assert proc.stdout is not None
                async for line in proc.stdout:
                    chunks.append(line)

            why, _ = await _wait_or_abort(_drain(), abort, timeout_sec)
            if not why:
                why, _ = await _wait_or_abort(proc.wait(), abort)
        else:
            why, res = await _wait_or_abort(proc.communicate(), abort, timeout_sec)
            if res and res[0]:
                chunks.append(res[0])
        if why:
            _stop_group(proc.pid)
            return None, b"".join(chunks)
        if proc.returncode in SIGNAL_RCS:
            interrupted = await asyncio.to_thread(
                interrupted_by_shutdown, name or os.path.basename(argv[0]), proc.returncode
            )
            if interrupted is not None:
                raise interrupted
        return proc.returncode, b"".join(chunks)
    except asyncio.TimeoutError:
        if proc is not None:
            # A reader an AER stop left SIGSTOPped while a chip it may read is attached is not killed
            # on its own timeout until the stop is settled, at most to the hold's bound (spec 03 I92).
            why, _ = await _wait_or_abort(chip_state.await_reader_release(proc.pid), abort)
            if not why:
                try:
                    why, _ = await _wait_or_abort(terminate(proc.pid), abort)
                except (ProcessLookupError, OSError):
                    pass
            if why:
                _stop_group(proc.pid)
                return None, b"".join(chunks)
            if reap_sec > 0:
                try:
                    await aio.wait_for(proc.wait(), timeout=reap_sec)
                except asyncio.TimeoutError:
                    pass  # still exiting (stuck in the driver): a later scan says so
        return None, b"".join(chunks)
    finally:
        if proc is not None:
            track(None)
