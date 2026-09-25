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

Track = Callable[[Optional["asyncio.subprocess.Process"]], None]
Terminate = Callable[[int], Awaitable[None]]


async def _killpg(pid: int) -> None:
    """Default timeout response when the caller supplies no gentler ladder: SIGKILL the whole
    group at once. Own-session spawn makes pid the pgid, so this reaches the probe's own
    children and never the broker's."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass  # already gone


async def run_probe(
    argv: list[str],
    *,
    timeout_sec: float,
    track: Track,
    stream: bool,
    env: dict[str, str],
    cwd: Optional[str] = None,
    terminate: Terminate = _killpg,
) -> tuple[Optional[int], bytes]:
    """Run ``argv`` to completion or to ``timeout_sec``, whichever comes first.

    Returns ``(rc, output)``. ``rc`` is ``None`` on timeout — never a sentinel exit code, so a
    caller cannot mistake "ran out of time" for anything the probe itself returned — and
    ``terminate`` has already been awaited against the process group by the time this returns.

    ``stream=True`` drains stdout line by line as it arrives instead of waiting for EOF via
    ``communicate()``. The probe most worth reading is the one that times out, and
    ``communicate()`` yields nothing once the task awaiting it is cancelled — a streaming drain
    is what lets a caller still see everything printed up to the moment it gave up.

    ``track`` registers the live process for the caller's dead-chip kill path for exactly the
    duration of the call, and is always cleared again before returning, on every exit path.
    """
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        track(proc)
        chunks: list[bytes] = []
        if stream:

            async def _drain() -> None:
                assert proc.stdout is not None
                async for line in proc.stdout:
                    chunks.append(line)

            await asyncio.wait_for(_drain(), timeout=timeout_sec)
            await proc.wait()
        else:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            if out:
                chunks.append(out)
        return proc.returncode, b"".join(chunks)
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                await terminate(proc.pid)
            except (ProcessLookupError, OSError):
                pass
        return None, b"".join(chunks)
    finally:
        if proc is not None:
            track(None)
