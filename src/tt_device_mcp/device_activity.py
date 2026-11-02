# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether a held device is doing any work, sampled once a second while a job runs (spec 03 I54).

A job owns the whole device for as long as it runs, so time it spends building, editing or sleeping
is time nobody else can use the device. The status surfaces say so: while the job's processes do no
device work for longer than the threshold, the job's row reads ``DEVICE IDLE for <n>``.

The signals are host-side reads that never touch the device:

* tt-kmd's own list of the pids holding each device (``/proc/driver/tenstorrent/<n>/pids``), or,
  where the driver does not publish it, the open fds of the job's own processes. No job process
  holding a device node means the device is doing no work for this job.
* The CPU time of the job processes that do hold it (``/proc/<pid>/stat``). The runtime drives the
  device from the host and polls for completion, so a holder whose CPU time has not moved is not
  driving it.

AI clock is not used: the runtime raises it when it opens the device and drops it on close, so it
reads "open", not "working". Nothing here opens the device, takes a lock or sends an ioctl.

A read that cannot answer (an unreadable driver record, a holder whose fds cannot be read) is
``unknown``, never idle: an IDLE shown on a device that is working would accuse the wrong person.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Callable, Optional

from tt_device_mcp import device_holders, job_reap

IDLE_LABEL = "DEVICE IDLE"
ACTIVE = "active"
IDLE = "idle"
UNKNOWN = "unknown"

INTERVAL_DEFAULT_SEC = 1.0
IDLE_AFTER_DEFAULT_SEC = 1.0
# A view older than this many intervals means the sampler stopped answering: unknown, not the
# last state it saw, so a stalled read never keeps an IDLE (or an active) on screen.
STALE_INTERVALS = 5


def _env_float(name: str, default: float, *, allow_zero: bool) -> float:
    """``name`` as a float, or ``default`` when unset, malformed or out of range. A typo in a unit
    file must not stop the broker from importing or the job from running."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0 or (value == 0 and not allow_zero):
        return default
    return value


def interval_sec() -> float:
    """TT_DEVICE_MCP_ACTIVITY_INTERVAL_SEC: seconds between samples (default 1)."""
    return _env_float("TT_DEVICE_MCP_ACTIVITY_INTERVAL_SEC", INTERVAL_DEFAULT_SEC, allow_zero=False)


def idle_after_sec() -> float:
    """TT_DEVICE_MCP_DEVICE_IDLE_AFTER_SEC: how long the device must do no work, without a break,
    before it reads IDLE (default 1). Any work seen resets it."""
    return _env_float("TT_DEVICE_MCP_DEVICE_IDLE_AFTER_SEC", IDLE_AFTER_DEFAULT_SEC, allow_zero=True)


def fmt_dur(seconds: float) -> str:
    """4s, 4m12s, 1h03m: the same shape the CLI's runtime column uses."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def label(view: dict) -> str:
    """The one line every surface shows: ``DEVICE IDLE for 4m12s``, ``active`` or
    ``activity unknown``."""
    state = view.get("activity")
    if state == IDLE:
        return f"{IDLE_LABEL} for {fmt_dur(view.get('idle_seconds') or 0)}"
    if state == ACTIVE:
        return ACTIVE
    return "activity unknown"


class ActivityTracker:
    """The idle/active/unknown state machine. No I/O and no clock of its own: each sample is fed
    in with the time it was taken, so the transitions are testable to the millisecond.

    Fed ``True`` (work seen), ``False`` (no work seen) or ``None`` (could not tell). IDLE needs no
    work on every sample for MORE than ``idle_after`` seconds, measured from the first quiet
    sample; one sample with work, or one that could not tell, starts the count over. A gap shorter
    than the threshold therefore reads active, never IDLE.
    """

    def __init__(self, idle_after: float) -> None:
        self.idle_after = idle_after
        self.state = UNKNOWN
        self.why = "not sampled yet"
        self.samples = 0
        self.last_sample_mono: Optional[float] = None
        self._quiet_since_mono: Optional[float] = None
        self._quiet_since_wall: Optional[float] = None
        self._last_quiet_mono: Optional[float] = None
        # Idle time of the episodes already closed; the open one is added on read.
        self._closed_idle_sec = 0.0

    def _close_episode(self) -> None:
        if self.state == IDLE and self._quiet_since_mono is not None and self._last_quiet_mono is not None:
            self._closed_idle_sec += self._last_quiet_mono - self._quiet_since_mono
        self._quiet_since_mono = self._quiet_since_wall = self._last_quiet_mono = None

    def observe(self, working: Optional[bool], now_mono: float, now_wall: float, why: str = "") -> str:
        """Fold in one sample; returns the new state."""
        self.samples += 1
        self.last_sample_mono = now_mono
        self.why = why
        if working is None or working:
            self._close_episode()
            self.state = UNKNOWN if working is None else ACTIVE
            return self.state
        if self._quiet_since_mono is None:
            self._quiet_since_mono, self._quiet_since_wall = now_mono, now_wall
        self._last_quiet_mono = now_mono
        self.state = IDLE if now_mono - self._quiet_since_mono > self.idle_after else ACTIVE
        return self.state

    def idle_seconds(self) -> Optional[float]:
        """How long the current IDLE has lasted, by the last sample (None unless IDLE)."""
        if self.state != IDLE or self._quiet_since_mono is None or self._last_quiet_mono is None:
            return None
        return self._last_quiet_mono - self._quiet_since_mono

    def idle_total_sec(self) -> float:
        """Every IDLE episode so far, the open one included."""
        return self._closed_idle_sec + (self.idle_seconds() or 0.0)

    def view(self, now_mono: float, interval: float) -> dict:
        """The machine-readable status. Stale (no sample for STALE_INTERVALS intervals) reads
        unknown: the sampler is not answering, so the last state is no longer evidence."""
        state, why = self.state, self.why
        if self.last_sample_mono is None or now_mono - self.last_sample_mono > STALE_INTERVALS * interval:
            state = UNKNOWN
            why = why if self.last_sample_mono is None else "sampler has not answered"
        idle_seconds = self.idle_seconds() if state == IDLE else None
        idle_since = None
        if idle_seconds is not None and self._quiet_since_wall is not None:
            idle_since = datetime.fromtimestamp(self._quiet_since_wall).isoformat()
        view = {
            "activity": state,
            "activity_why": why,
            "idle_since": idle_since,
            "idle_seconds": round(idle_seconds, 1) if idle_seconds is not None else None,
            "idle_total_sec": round(self.idle_total_sec(), 1),
            "idle_after_sec": self.idle_after,
        }
        view["activity_label"] = label(view)
        return view


class JobActivitySource:
    """One read per call: is the job rooted at ``pid`` / ``scope`` / ``tag`` driving the device?

    Returns ``(working, why)`` with working True, False or None (could not tell). Read-only: it
    reads /proc and the driver's pid record, and never opens the device. Membership is the same
    rule the end-of-job sweep uses (``job_reap.job_processes``): the job's process group, session,
    scope or tag, and their descendants.
    """

    def __init__(self, pid: Optional[int], scope: Optional[str] = None, tag: Optional[str] = None) -> None:
        self.pid = pid
        self.scope = scope
        self.tag = tag
        self._prev_cpu: Optional[dict] = None

    def _is_member(self, p: int) -> bool:
        if p in (os.getpid(), 0, 1):
            return False
        info = job_reap._read_stat(p)
        if info is None:
            return False
        if self.pid and (p == self.pid or info.pgid == self.pid or info.sid == self.pid):
            return True
        if self.pid and device_holders.descends_from(p, self.pid):
            return True
        if self.scope and job_reap._in_scope(p, self.scope):
            return True
        return bool(self.tag and job_reap._has_tag(p, self.tag))

    def job_holders(self) -> Optional[list]:
        """The job's pids that hold a device node; None when that cannot be known."""
        driver = device_holders._driver_holder_pids()
        if driver is not None:
            pids, complete = driver
            members = sorted(p for p in pids if self._is_member(p))
            if members or complete:
                return members
            return None  # a device's record was unreadable: a job holder may hide there
        held, blind = [], False
        for p in job_reap.job_processes(self.pid, self.scope, self.tag):
            holds = job_reap._holds_device(p)
            if holds is None:
                blind = True
            elif holds:
                held.append(p)
        if held:
            return sorted(held)
        return None if blind else []

    def __call__(self) -> tuple:
        holders = self.job_holders()
        if holders is None:
            self._prev_cpu = None
            return None, "device holders unreadable"
        if not holders:
            self._prev_cpu = None
            return False, "no job process holds the device"
        cpu = {}
        for p in holders:
            read = device_holders.read_proc_cpu(p)
            if read is not None:
                ticks, start = read
                cpu[(p, start)] = ticks
        prev, self._prev_cpu = self._prev_cpu, cpu
        if not cpu:
            return None, "device holders exited mid-read"
        n = len(cpu)
        # The first sight of a holder has nothing to compare against: it counts as work.
        if prev is None or any(prev.get(k) != v for k, v in cpu.items()):
            return True, f"{n} job process(es) hold the device and are running"
        return False, f"{n} job process(es) hold the device with no CPU time"


class DeviceActivitySampler:
    """Runs ``source`` every ``interval`` seconds into an :class:`ActivityTracker` until stopped.

    One per running job: started when the job's process is live, stopped (cancelled, never
    awaited) when it exits, so it neither outlives the job nor adds a second to the between-jobs
    path. Each read runs off the event loop; a read that raises counts as unknown.
    """

    def __init__(
        self,
        source: Callable[[], tuple],
        *,
        interval: Optional[float] = None,
        idle_after: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable = asyncio.sleep,
    ) -> None:
        self.source = source
        self.interval = interval_sec() if interval is None else interval
        self.tracker = ActivityTracker(idle_after_sec() if idle_after is None else idle_after)
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self.task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def sample_once(self, working: Optional[bool], why: str) -> str:
        return self.tracker.observe(working, self._clock(), self._wall(), why)

    async def run(self) -> None:
        while True:
            began = self._clock()
            try:
                working, why = await asyncio.to_thread(self.source)
            except Exception as e:  # noqa: BLE001 - a status read must never end the job's sampler
                working, why = None, f"sample failed: {type(e).__name__}"
            self.sample_once(working, why)
            await self._sleep(max(0.0, self.interval - (self._clock() - began)))

    def view(self) -> dict:
        return self.tracker.view(self._clock(), self.interval)

    def stop(self) -> dict:
        """Cancel the loop (it is not awaited) and return the final view."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
        return self.view()
