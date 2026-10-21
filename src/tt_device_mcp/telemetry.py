# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""TelemetrySampler: the always-on chip sampler and dead-chip tripwire.

Constructed by ``ServerFsm.boot`` and owned by the root. Deliberately NOT a consumer of
``HealthMonitor.status()``: that is the last gate pass, minutes stale, and the gate can itself be
stuck inside a fabric check holding the device lock — which is exactly what happened when a host
was lost (the chip died 17 seconds into a traffic pass, and nothing looked at the device again
until the machine was gone). This loop samples raw sysfs on its own clock and is beholden to
nothing, so it acts on its own. It reads the mechanism (a reset in flight reads all-ones by
design) and the FSM (a held box must not re-journal its loss every sample), and writes its
findings INTO the FSM through the injected dirty-mark.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from tt_device_mcp import metrics
from tt_device_mcp.health import ALL_ONES, SAMPLE_INTERVAL_SEC, SAMPLE_RING_SIZE, chip_state

if TYPE_CHECKING:
    from tt_device_mcp.health import HealthMonitor, RecoveryDeps, RecoveryMechanism

# The early off-bus watch (spec 03 I79). Between samples the loop reads the kernel's own PCIe
# record (pci.link_signals: AER counters and the KMD node list, kernel memory only) every
# LINK_WATCH_SEC. A rise in an uncorrectable AER counter on a chip's path, or a chip node leaving
# sysfs, arms a fast cadence: the next sample comes FAST_SAMPLE_SEC after the last one (never
# sooner), and samples stay FAST_SAMPLE_SEC apart for FAST_WINDOW_SEC after the last such signal.
# One storm (signals less than FAST_WINDOW_SEC apart) keeps the fast cadence at most FAST_MAX_SEC
# from its first signal; after that the regular cadence runs until the link has been quiet for
# FAST_WINDOW_SEC. The signal itself decides nothing: the off-bus verdict is still
# check_for_dead_chips on real samples, with the same two-sample proof, and a bad reading that
# began under the fast cadence must also have lasted FAST_CONFIRM_SEC, even past the window's end,
# so a transient that clears in under 4 s never isolates a chip or dirties the device.
LINK_WATCH_SEC = 1.0
FAST_SAMPLE_SEC = 2.0
FAST_WINDOW_SEC = 30.0
FAST_MAX_SEC = 60.0
FAST_CONFIRM_SEC = 4.0

# Spec 03 I84: the dead-chip valve's PCI removes together get at most this long. A remove drains the
# driver's in-flight ioctls, and a reader wedged on the dead chip can hold that drain open; the write
# runs in a worker thread so the sampler never stalls on it, and a remove still running at the bound
# counts as not detached (no kill, the hold stays). Only that case waits this long.
DETACH_TIMEOUT_SEC = 15.0


@dataclass(frozen=True)
class BrokerTelemetry:
    """Everything the broker publishes about itself, in one declaration.

    The point of this type is that it is READABLE: the answer to "what do we track" is this class,
    not four subsystems' internals plus whichever metric objects happen to exist. It is assembled
    from the live subsystems by :meth:`snapshot` and pushed to Prometheus by :meth:`publish`.

    Values only — no metric objects, no registry. ``metrics`` is a leaf module that ``fsm`` and
    ``health`` both import, so it cannot import this one back (see metrics.py's own docstring);
    :meth:`publish` therefore hands it primitives and metrics owns the encoding.
    """

    # ---- orchestration FSM (fsm.ServerFsm): the CURRENT fault, or "" while healthy, plus
    # whether the next gate pass owes this episode a reset attempt. The state itself and the
    # per-state dwell publish from the FSM's own transitions (metrics.state_entered), not here.
    fsm_why: str = ""
    fsm_dirty: bool = False

    # ---- what the last probe pass saw (health.HealthMonitor). chips_expected is the high-water
    # baseline, NOT the survivor count — a mesh that lost a tray must not lower its own bar.
    chips_present: int = 0
    chips_expected: int = 0
    isolated_chips: int = 0
    probe_verdicts: dict = field(default_factory=dict)  # probe -> healthy|unhealthy|skipped
    fabric_ok: Optional[bool] = None  # last REAL verdict; None = never ran

    # ---- the platform the boot flow committed, and the executor's safety state
    platform: str = "unresolved"  # galaxy | per-target | unresolved
    reset_in_flight: bool = False
    reset_cooling: bool = False

    # ---- queue and session accounting (server.Stats). Job counts publish as counters from
    # job_completed() at each job's end; busy/free are the occupancy clock's CUMULATIVE seconds,
    # which metrics turns into counter deltas — utilization is busy/(busy+free), computed by the
    # reader, not shipped as a third number that can disagree with these two.
    device_busy: bool = False
    queue_depth: int = 0
    busy_sec: float = 0.0
    free_sec: float = 0.0
    wait_p50: float = 0.0
    wait_p95: float = 0.0
    wait_max: float = 0.0

    def publish(self) -> None:
        """Push this snapshot into the Prometheus gauges. Never raises: telemetry may not take the
        broker down, and a half-published snapshot is worth more than none."""
        try:
            metrics.publish_snapshot(self)
        except Exception:  # noqa: BLE001 - see docstring
            pass


def snapshot(fsm, monitor, mechanism, stats, *, queue_depth: int = 0, isolated_chips: int = 0) -> BrokerTelemetry:
    """Read the live subsystems into one :class:`BrokerTelemetry`.

    Every read is defensive. This runs on the stats cadence against subsystems that may be
    mid-transition or, before the boot flow has finished, absent entirely — and a snapshot that
    raises would take out the only reporting an operator has at exactly the moment it matters.
    A field that cannot be read keeps its default rather than failing the whole snapshot.
    """
    snap: dict = {}

    record = getattr(fsm, "record", None)
    if record is not None:
        snap.update(fsm_why=record.why or "", fsm_dirty=bool(record.dirty))
    committed = getattr(fsm, "recovery", None)
    if committed is not None:
        # The class name IS the platform: GalaxyRecovery/PerTargetRecovery. Read from the committed
        # instance rather than re-deriving, so this reports what the ladder will actually use.
        snap["platform"] = "galaxy" if "Galaxy" in type(committed).__name__ else "per-target"

    if monitor is not None:
        state = monitor.status()
        if state is not None:
            snap.update(
                chips_expected=int(getattr(state, "expected", 0) or 0),
                probe_verdicts={
                    o.monitor: getattr(o.verdict, "value", str(o.verdict)) for o in getattr(state, "observations", ())
                },
            )
        snap["fabric_ok"] = monitor.last_fabric_ok
        try:
            present = len(monitor._present_chip_indices())
            snap["chips_present"] = present
            # The high-water baseline is knowable before any probe pass has run, and "expected 0"
            # on a host with chips would read as a device-less box rather than an unprobed one.
            snap["chips_expected"] = max(int(snap.get("chips_expected", 0) or 0), int(monitor.expected(present)))
        except Exception:  # noqa: BLE001 - an unreadable /dev is a 0 here, not a dead snapshot
            pass

    if mechanism is not None:
        snap.update(reset_in_flight=bool(mechanism.reset_in_flight), reset_cooling=bool(mechanism.cooling()))

    if stats is not None:
        d = stats.to_dict()
        snap.update(
            device_busy=bool(stats.device_busy),
            busy_sec=d["device"]["busy_sec"],
            free_sec=d["device"]["idle_sec"],
            wait_p50=d["waits"]["p50"],
            wait_p95=d["waits"]["p95"],
            wait_max=d["waits"]["max"],
        )

    snap["queue_depth"] = int(queue_depth)
    snap["isolated_chips"] = int(isolated_chips)
    return BrokerTelemetry(**snap)


class TelemetrySampler:
    """Samples every chip's volatile state, forever — and is the tripwire for a dead chip.

    It runs continuously rather than only during jobs because the failure we most need to
    explain — the host rebooting under us — does not wait for a job to be running, and the
    in-memory ring does not survive it. Each sample also goes to disk, so the last lines
    written before the machine died are the run-up to the crash.

    Server-side actions (the holder kill, the dirty mark, the idle-hold ledger, the relift
    spawn) come in through ``deps`` as late-bound callables, the same seam the rest of the
    health subsystem uses, so a test that monkeypatches the server function still governs
    this loop.
    """

    # A stalled sampler means a held device never escalates. The watchdog ping is gated on the
    # tick below so a stall withholds the ping and systemd restarts the broker: the escalation
    # ladder can never sit silently behind a dead sampler.
    STALL_SEC = float(os.environ.get("TT_DEVICE_MCP_SAMPLER_STALL_SEC", "120"))

    def __init__(self, monitor: "HealthMonitor", mechanism: "RecoveryMechanism", deps: "RecoveryDeps") -> None:
        self._monitor = monitor
        self._mechanism = mechanism
        self._deps = deps
        # The telemetry trace: what the chips were doing while the job ran. Snapshots at the
        # gate only ever show the aftermath, so a chip that heated up, throttled, and died
        # slowly is indistinguishable from one that dropped dead instantly. The sample is a
        # sysfs read — ~0.03s for all 32 chips, and it touches nothing — so taking one every
        # few seconds costs effectively nothing and is the only way to see the run-up.
        self.ring: deque = deque(maxlen=SAMPLE_RING_SIZE)
        self.task: Optional[asyncio.Task] = None
        # All-ones on EVERY chip at once is not 32 independent failures. It is the driver, the
        # bus, or a reset — a global event — and removing every device in response leaves a box
        # with nothing to run on and no way back except a rescan. Treat it as a fault to
        # report, not to amputate.
        self.dead_chip_strikes: dict = {}
        # A reset scope seen still cycling the chips after our wait for it ended: no chip read until
        # it is gone (spec 03 I66). Re-checked once per tick only while set.
        self.scope_seen = False
        # The hung chips the pollers were last stopped for, so one episode stops them once.
        self.pollers_stopped_for: frozenset = frozenset()
        # The hung chips a stop was last deferred for (an all-ones chip not yet detached), journalled once.
        self.pollers_deferred_for: frozenset = frozenset()
        self.prev_beats: dict = {}
        # Chips a reset step's detach-before-quiesce could not detach (spec 03 I84), with when: while one
        # still reads all-ones and is attached, every reset is refused at once and the 10-min bound
        # asks for the host rung. Cleared by a passing full check.
        self.quiesce_refused: dict = {}
        # Of those, the chips whose remove failed or timed out (not unwired): the broker asks for the
        # power cycle at once for them, not at the 10-min bound (spec 03 I84). Cleared with the above.
        self.quiesce_detach_failed: set = set()
        # Every chip reading all-ones on the sample the valve runs for (spec 03 I84): one of them it
        # does not remove keeps the holders alive. Set only while check_for_dead_chips calls it.
        self.sample_all_ones: Optional[list] = None
        # Consecutive samples with NO chip nodes in sysfs — every chip off the bus, or the
        # driver wedged. chip_sample() omits an off-bus chip, so an all-gone drop reads as an
        # empty sample the all-ones blackout above never sees. Debounced like that blackout: a
        # lone empty sample from a rescan or a driver re-init settling must not trip it.
        self.all_chips_gone_strikes: int = 0
        # Consecutive samples with FEWER chip nodes than the host's baseline but not none — part of
        # the mesh off the bus. chip_sample() omits a gone node, so 24 of 32 reads as 24 healthy
        # chips; without this count the drop sets no flag until the next gate (spec 03 I31).
        self.short_count_strikes: int = 0
        # Liveness of this loop. Its per-iteration `except Exception` cannot catch a hung
        # `to_thread` device read on a wedged chip, so the loop can stall indefinitely while
        # the event loop keeps serving — see is_stalled() and the watchdog gate in server.py.
        self.last_tick: float = 0.0
        # The early off-bus watch (spec 03 I79): the fast cadence runs until fast_until, the last
        # link_signals read is what the next one is compared with, and bad_since stamps when each
        # off-bus reading began, for the FAST_CONFIRM_SEC proof. _clock is injectable for tests.
        self.fast_until: float = 0.0
        self._link_prev: Optional[dict] = None
        self.bad_since: dict = {}
        self._housekeeping_at: float = 0.0
        # The current storm: its first and its latest signal, for the FAST_MAX_SEC cap, and the
        # start of the last loop cycle, so no sample follows another sooner than FAST_SAMPLE_SEC.
        self._storm_start: Optional[float] = None
        self._last_signal: Optional[float] = None
        self._storm_capped: bool = False
        self._cycle_at: float = 0.0
        self._clock = time.monotonic

    def tick(self) -> None:
        self.last_tick = time.monotonic()

    def is_stalled(self) -> bool:
        """True once the loop has ticked at least once and then stopped for longer than the
        stall window. Zero (never ticked — before the first sample at startup) is NOT stalled:
        the watchdog ping must not be withheld before the loop has had its chance to run."""
        return self.last_tick > 0 and (time.monotonic() - self.last_tick) > self.STALL_SEC

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def mark_job_window(self) -> None:
        """Drop samples from before this job, so an incident's trace is this job's run-up and
        not the idle hour before it."""
        self.ring.clear()

    async def run(self) -> None:
        """The sampler loop — see the class docstring for why it is always-on and independent."""
        while True:
            # Stamp liveness at the top of every cycle: it advances only when the previous
            # iteration completed, so a hang inside the cycle freezes it and the watchdog
            # gate trips.
            self.tick()
            self._cycle_at = self._clock()
            try:
                # A reset or recovery step on the chips owns them while it runs: they are off the bus
                # by design, and only the step's own reads may reach them (spec 03 I66).
                if not await self.recovering_now():
                    sample = await asyncio.to_thread(self._deps.chip_sample)
                    self.ring.append({"ts": time.time(), "chips": sample})
                    await asyncio.to_thread(self._deps.append_trace, sample)

                    await self.check_for_dead_chips(sample)
                await asyncio.to_thread(chip_state.publish)
                # The rest keeps the regular cadence while the fast one runs (I79): only the
                # off-bus check gets faster, never the ledger, the relift or the holder scan.
                now = self._clock()
                if not self.fast_active() or now - self._housekeeping_at >= SAMPLE_INTERVAL_SEC:
                    self._housekeeping_at = now
                    # The one loop that runs with an empty queue, so the one place an idle-degraded
                    # window can be caught and put on the durable timeline as it happens.
                    self._deps.refresh_idle_hold_ledger()
                    # ...and the one place a self-heal hold gets re-verified with no job flowing to
                    # do it. Spawned as its own task, never awaited here, so this loop keeps
                    # sampling; read-only, rate-limited, inert unless the operator opts in.
                    self._deps.spawn_idle_relift()
                    # ...and the idle-holder detector: names a foreign process that holds the device
                    # without working (spec 03 I44). Its own detached, rate-limited scan.
                    watch = getattr(self._deps, "watch_idle_holders", None)
                    if watch is not None:
                        watch()
            except Exception:  # noqa: BLE001 - a sampler must never take the runner down
                pass
            await self.wait_for_next_sample()

    def fast_active(self) -> bool:
        return self._clock() < self.fast_until

    def arm_fast(self, reason: str) -> bool:
        """Run the fast cadence for FAST_WINDOW_SEC from now, but never past FAST_MAX_SEC from the
        storm's first signal (I79). True only on the arming edge. Journalled once per window, on the
        way in, so an AER storm is one event, not one per second."""
        now = self._clock()
        was = self.fast_active()
        if self._last_signal is None or now - self._last_signal >= FAST_WINDOW_SEC:
            self._storm_start, self._storm_capped = now, False
        self._last_signal = now
        self.fast_until = min(now + FAST_WINDOW_SEC, self._storm_start + FAST_MAX_SEC)
        log = self._deps.logger()
        if not self.fast_active():
            if not self._storm_capped:
                self._storm_capped = True
                if log:
                    log.warning(
                        f"early off-bus watch: signals for {FAST_MAX_SEC:g}s without a pause — back to "
                        f"sampling every {SAMPLE_INTERVAL_SEC:g}s until the link is quiet for {FAST_WINDOW_SEC:g}s"
                    )
            return False
        if was:
            return False
        try:
            self._deps.health_event("offbus_watch_armed", reason=reason)
        except Exception:  # noqa: BLE001 - the journal must not stop the watch
            pass
        if log:
            log.warning(f"early off-bus watch: {reason} — sampling every {FAST_SAMPLE_SEC:g}s")
        return True

    def link_signal(self, now: dict) -> Optional[str]:
        """What changed since the last link_signals read, as a reason, or None (I79). An
        uncorrectable AER counter that rose on a chip's path, or a chip node that left sysfs.
        The first read is only a baseline; a counter that could not be read compares as nothing."""
        prev, self._link_prev = self._link_prev, now
        if prev is None:
            return None
        why = []
        # A node the sampler removed itself (isolate_dead_chips) only moves the baseline.
        isolated = getattr(self._deps, "isolated_chips", None)
        ours = set(isolated() if isolated is not None else ())
        gone = sorted(set(prev.get("chips") or ()) - set(now.get("chips") or ()) - ours, key=int)
        if gone:
            why.append(f"chip node(s) {', '.join(gone)} left sysfs")
        before = prev.get("aer") or {}
        rose = sorted(
            bdf
            for bdf, v in (now.get("aer") or {}).items()
            if v is not None and before.get(bdf) is not None and v > before[bdf]
        )
        if rose:
            why.append(f"uncorrectable AER errors on {', '.join(rose)}")
        return "; ".join(why) or None

    async def poll_link(self) -> bool:
        """One early-watch read. True on the arming edge of the fast cadence. While a reset or
        recovery step owns the chips (I66) a signal only moves the baseline: taking the chips off
        the bus is what the step does, so it arms nothing."""
        read = getattr(self._deps, "link_signals", None)
        if read is None:
            return False
        try:
            reason = self.link_signal(await asyncio.to_thread(read))
        except Exception:  # noqa: BLE001 - the watch only speeds the sampler up; never stop it
            return False
        if reason is None:
            return False
        if self._mechanism.reset_in_flight or self.scope_seen or chip_state.recovering():
            return False
        return self.arm_fast(reason)

    async def wait_for_next_sample(self) -> None:
        """Sleep until the next sample: SAMPLE_INTERVAL_SEC, or FAST_SAMPLE_SEC while the fast
        cadence runs. Only the arming edge of the early watch cuts the wait short (I79), and never
        to less than FAST_SAMPLE_SEC after the last cycle began: a storm's later signals do not."""
        interval = FAST_SAMPLE_SEC if self.fast_active() else SAMPLE_INTERVAL_SEC
        if getattr(self._deps, "link_signals", None) is None:
            await asyncio.sleep(interval)
            return
        deadline = self._clock() + interval
        while True:
            left = deadline - self._clock()
            if left <= 0:
                return
            await asyncio.sleep(min(LINK_WATCH_SEC, left))
            if await self.poll_link():
                deadline = min(deadline, self._cycle_at + FAST_SAMPLE_SEC)

    def _lasted(self, key: str) -> bool:
        """The second-sample proof may stand (I79). A bad reading that began on the regular cadence
        is already proved by samples SAMPLE_INTERVAL_SEC apart. One that began under the fast
        cadence must also have lasted FAST_CONFIRM_SEC, even once the window has ended: the sample
        after the last fast one still comes FAST_SAMPLE_SEC later, so the bound follows the
        reading, not the mode, and a faster cadence never shortens the debounce below it."""
        since = self.bad_since.get(key)
        if not self.fast_active() and (since is None or since >= self.fast_until):
            return True
        return since is not None and self._clock() - since >= FAST_CONFIRM_SEC

    def _mark_bad(self, key: str) -> None:
        self.bad_since.setdefault(key, self._clock())

    async def recovering_now(self) -> bool:
        """Whether a reset or recovery step is running on the chips: this broker's own (the flag),
        or a reset scope we stopped waiting for that is still cycling them."""
        if self._mechanism.reset_in_flight:
            return True
        if self.scope_seen:
            if await asyncio.to_thread(self._mechanism.scope_active):
                return True
            self.scope_seen = False
            chip_state.set_recovering("scope", False)
        return False

    async def stop_pollers_for_hung(self, sample: dict, dead: list) -> None:
        """Stop the device pollers at the FIRST sample that shows a present chip hung — reading
        all-ones, or a heartbeat that did not move since the previous sample — not at the second
        (spec 03 I66). Their reads to it are the MMIO a hung endpoint never completes. Isolation
        still waits for its two-sample proof; the pollers are cheap to bring back (the health probe
        re-arms them once the device is idle and healthy). Every chip at once is the re-init blip
        the blackout path already proves on two samples, so it does not count here.

        Only a sample whose hung chips all still answer stops them here (spec 03 I66, I84). A
        stopped poller closes its fds, and tt-kmd's close path sends each chip it had open a
        power-state message (an ARC queue write, then reads polled for up to 1 s) unless the
        device is detached. A frozen heartbeat was just read back, so that chip completes those
        reads; an all-ones chip may be off the bus, and only its PCI remove marks it detached. So
        while any chip on the sample reads all-ones (a first strike, or a lost chip whose remove
        failed), the stop is left to the dead-chip valve, which stops them after a successful
        remove or refuses and keeps the hold."""
        stalled = [
            i
            for i, v in sample.items()
            if v and isinstance(v[0], int) and v[0] != ALL_ONES and self.prev_beats.get(i) == v[0]
        ]
        self.prev_beats = {i: v[0] for i, v in sample.items() if v and isinstance(v[0], int)}
        hung = frozenset(dead) | frozenset(stalled)
        if not hung or len(hung) >= len(sample):
            if not hung:
                self.pollers_stopped_for = frozenset()
                self.pollers_deferred_for = frozenset()
            return
        if hung <= self.pollers_stopped_for:
            return
        log = self._deps.logger()
        if dead:
            if hung > self.pollers_deferred_for:
                self.pollers_deferred_for = hung
                self._deps.health_event(
                    "pollers_quiesce_deferred",
                    reason="chip_not_detached",
                    all_ones=sorted(dead, key=int),
                    stalled=sorted(stalled, key=int),
                )
                if log:
                    log.warning(
                        f"HUNG-CHIP pollers left running: chip(s) {sorted(dead, key=int)} read all-ones and "
                        f"are not detached, so closing the pollers' fds would make the driver message them; "
                        f"the dead-chip valve stops them after the PCI remove"
                    )
            return
        self.pollers_stopped_for = hung
        self._deps.health_event(
            "pollers_quiesced_hung_chip", all_ones=sorted(dead, key=int), stalled=sorted(stalled, key=int)
        )
        await self._deps.set_device_pollers(False, lambda m: log.info(f"HUNG-CHIP {m}") if log else None)

    async def check_for_dead_chips(self, sample: dict) -> None:
        """Decide whether all-ones means a chip died — or something else entirely.

        All-ones is only evidence of a dead chip when nothing else can explain it, and two
        things can:

        A RESET IN FLIGHT. Taking the chips off the bus is what a reset *is*. During one they
        read all-ones exactly like a dead chip, and isolating them then rips the endpoints out
        of the kernel halfway through and destroys the reset. This is not hypothetical — it
        removed all 32 chips from a healthy host and left it with no devices at all. Only a
        reset is excused; during a fabric check or a probe, chips going all-ones IS a failure
        and is acted on, which is the case that actually killed a host.

        EVERY CHIP AT ONCE. Thirty-two chips do not fail independently in the same 10ms. That
        is the driver, the bus, or a reset we did not start — a global event. Amputating every
        device in response leaves nothing to run on and no way back except a rescan, so it is
        reported loudly and left alone.

        Otherwise, a chip must read all-ones on two consecutive samples before it is cut out.
        A genuinely dead chip stays dead; a transient does not. Twenty seconds is a rounding
        error against the ~130s a host survives after a chip drops, and it buys immunity to a
        whole class of false positives — the kind that just took a working box offline.

        FEWER CHIPS THAN THE BASELINE. A chip whose node left sysfs is simply absent from the
        sample, so 24 answering chips on a 32-chip host look healthy chip by chip. The count is
        held to the same baseline heartbeat_verdict uses, on the same two-sample proof; a host
        with no baseline yet is never short.
        """
        log = self._deps.logger()
        dead = sorted((i for i, v in sample.items() if v and v[0] == ALL_ONES), key=int)
        if not self._mechanism.reset_in_flight:
            await self.stop_pollers_for_hung(sample, dead)
        if sample and not dead:
            self.dead_chip_strikes = {}
            self.all_chips_gone_strikes = 0
            self.bad_since = {k: v for k, v in self.bad_since.items() if k == "short"}
            # expected(0) reads the baseline without ratcheting it down to a short sample.
            # Health checks off (TT_DEVICE_MCP_HEALTH_CHECK=0) means no count check either (I25).
            expected = self._monitor.expected(0) if self._monitor._health_check_enabled() else 0
            if expected <= 0 or len(sample) >= expected:
                # Every chip is present and answering — genuinely healthy. Re-arm every debouncer.
                self.short_count_strikes = 0
                self.bad_since = {}
                return
            await self._check_short_count(len(sample), expected, log)
            return
        self.short_count_strikes = 0
        self.bad_since.pop("short", None)

        if self._mechanism.reset_in_flight:
            return  # a reset is supposed to do this

        # reset_in_flight only covers the window we are AWAITING the reset. Our wait times out
        # at DEVICE_RESET_TIMEOUT_SEC while the reset itself keeps running — the scope is
        # deliberately never killed mid-reset — so the flag drops while all 32 chips are still
        # legitimately reading all-ones. Isolating on that tears endpoints out of a LIVE reset,
        # and a chip amputated mid-reset does not come back until the host reboots: it is how a
        # healthy box ends up short a tray. The reset scope outlives our timer, so ask the
        # scope, not the timer.
        if await asyncio.to_thread(self._mechanism.scope_active):
            self.scope_seen = True
            chip_state.set_recovering("scope", True)
            return

        if not sample:
            # No tenstorrent nodes in sysfs at all: every chip off the PCIe bus, or the driver
            # wedged. This reads as an EMPTY sample, not 32 all-ones, because chip_sample()
            # omits a chip whose node is gone — so the all-ones blackout below (which needs the
            # nodes present) never catches it. On a fully idle box no job gate or status query
            # runs the fuller liveness probe, so without this the single worst state (absence
            # is never health) would set no dirty flag, write no held event, and never arm the
            # hold-deadline watchdog until a job or a poll finally poked it. A reset is already
            # excused above.
            self.dead_chip_strikes = {}  # no nodes -> the per-chip counter has nothing to track
            self.bad_since = {k: v for k, v in self.bad_since.items() if k == "gone"}
            expected = self._monitor.expected(0)
            if expected <= 0:
                # A host with no TT silicon: an empty class dir is its normal, healthy state,
                # never a drop. (A host that has ever shown chips keeps a nonzero baseline.)
                self.all_chips_gone_strikes = 0
                self.bad_since = {}
                return
            self.all_chips_gone_strikes += 1
            self._mark_bad("gone")
            # Same two-sample proof the all-ones blackout demands, and re-flagged only on the
            # way in (episode already dirty) so a dirty box does not re-journal the loss on
            # every idle sample. The dirty axis, not state-is-HEALTHY: a box under an affirmative
            # hold (eth self-heal, fabric-unverified — placed dirty=False) that then loses every
            # node has ESCALATED, and must still write the host-at-risk event and go dirty so the
            # next gate owes it a real reset.
            if self.all_chips_gone_strikes < 2 or not self._lasted("gone") or self._deps.episode_dirty():
                return
            self._deps.health_event("all_chips_off_bus", present=0, expected=expected, host_at_risk=True)
            self._deps.mark_device_dirty(
                "no chips exposed in sysfs — every chip off the PCIe bus or the driver wedged", why="heartbeat"
            )
            if log:
                log.error(
                    f"ALL chips are off the PCIe bus (no tenstorrent nodes in sysfs) on "
                    f"two consecutive samples, on a host that expects {expected} — the "
                    f"driver wedged or every chip dropped. Holding the device: a warm "
                    f"reboot cannot re-enumerate dropped Galaxy ASICs, so a cold power "
                    f"cycle is likely required."
                )
            return

        self.bad_since.pop("gone", None)
        if len(dead) == len(sample) and len(sample) > 1:
            # Every chip at once is the driver, the bus, or a reset — not 32 simultaneous chip
            # failures — so it never isolates. It must not dirty the device on one sample
            # either: anything that re-inits the mesh (a job's teardown, tt-smi, the validator)
            # makes the heartbeat read all-ones for a moment, and with a BLOCKING tenant gate a
            # single untrusted sample takes a healthy box offline. Hold it to the same
            # two-sample proof a single dead chip must pass; a real blackout stays, a re-init
            # blip does not.
            seen_twice = [i for i in dead if self.dead_chip_strikes.get(i, 0) >= 1]
            for i in dead:
                self.dead_chip_strikes[i] = self.dead_chip_strikes.get(i, 0) + 1
                self._mark_bad(f"chip:{i}")
            if len(seen_twice) == len(dead):
                # Lost on the second sample, whatever the cadence: a fast cadence adds no BAR read of
                # a dead chip (spec 03 I66). The FAST_CONFIRM_SEC gate holds back only the verdict.
                chip_state.mark_lost(dead, "all chips all-ones on two samples", self._nodes(dead))
                if not all(self._lasted(f"chip:{i}") for i in dead):
                    # The proof still pending: the next sample reads each heartbeat once for real.
                    chip_state.reprobe_due(dead)
                    return
            if len(seen_twice) != len(dead):
                if log:
                    log.info(
                        f"all {len(dead)} chips read 0xFFFFFFFF on one sample — treating "
                        f"as a re-init blip until a second sample confirms it"
                    )
                return
            if log:
                log.error(
                    f"ALL {len(dead)} chips read 0xFFFFFFFF on two consecutive samples — "
                    f"that is the driver, the bus, or a reset, not {len(dead)} separate "
                    f"chip failures. NOT isolating (removing every device would leave "
                    f"nothing to run on)."
                )
            self._deps.health_event("all_chips_blackout", chips=len(dead))
            self._deps.mark_device_dirty(f"all {len(dead)} chips stopped answering on the PCIe bus", why="heartbeat")
            return

        # Two strikes: a dead chip stays dead, a blip does not.
        # Under the fast cadence (I79) the reading must also have lasted FAST_CONFIRM_SEC.
        seen_twice = [i for i in dead if self.dead_chip_strikes.get(i, 0) >= 1]
        confirmed = [i for i in seen_twice if self._lasted(f"chip:{i}")]
        for i in dead:
            self.dead_chip_strikes[i] = self.dead_chip_strikes.get(i, 0) + 1
            self._mark_bad(f"chip:{i}")
        for i in list(self.dead_chip_strikes):
            if i not in dead:
                del self.dead_chip_strikes[i]
                self.bad_since.pop(f"chip:{i}", None)

        if seen_twice:
            # Lost on the second sample, before the isolation runs and apart from the fast cadence's
            # FAST_CONFIRM_SEC gate: a chip the kernel will not let go of, or one still inside that
            # gate, is never read on the poll clock again (spec 03 I66). One still inside the gate
            # gets its heartbeat read once for real at the next sample, so the proof rests on reads.
            chip_state.mark_lost(seen_twice, "all-ones on two samples", self._nodes(seen_twice))
            pending = [i for i in seen_twice if i not in confirmed]
            if pending:
                chip_state.reprobe_due(pending)
        if confirmed:
            self.sample_all_ones = dead
            try:
                await self.isolate_dead_chips(confirmed)
            finally:
                self.sample_all_ones = None

    @staticmethod
    def _nodes(ids) -> dict:
        return chip_state.chip_nodes(ids)

    async def _check_short_count(self, present: int, expected: int, log) -> None:
        """Fewer chip nodes than the baseline on two consecutive samples -> dirty (spec 03 I31).

        A reset in flight takes chips off the bus by design and is excused, as for all-ones. Flagged
        only on a HEALTHY box, unlike the all-gone drop: under an open episode (an off-bus hold the
        gate placed dirty=False, an isolated chip) the short count is that same fault, and
        re-dirtying it would turn the gate's hold back into a reset owed on every admission poll."""
        if self._mechanism.reset_in_flight or await asyncio.to_thread(self._mechanism.scope_active):
            return
        self.short_count_strikes += 1
        self._mark_bad("short")
        if self.short_count_strikes < 2 or not self._lasted("short") or self._deps.episode_open():
            return
        self._deps.health_event("chips_missing", present=present, expected=expected)
        self._deps.mark_device_dirty(
            f"{present} of {expected} chips in sysfs — chips dropped off the PCIe bus", why="heartbeat"
        )
        if log:
            log.error(
                f"{present} of {expected} chips present in sysfs on two consecutive samples — "
                f"{expected - present} chip(s) dropped off the PCIe bus. Holding the device."
            )

    async def _detach_chips(self, chips: list) -> tuple:
        """PCI-remove each chip, every write in a worker thread and all of them inside one
        DETACH_TIMEOUT_SEC budget (spec 03 I84). Returns ``(removed, stuck)``. A remove still running
        at the bound is stuck, and no further chip is tried: a hung remove keeps the kernel's
        remove path busy, so the next write would hang behind it."""
        removed: list = []
        if self._deps.isolate_chip is None:
            return removed, list(chips)
        deadline = time.monotonic() + DETACH_TIMEOUT_SEC
        for idx in chips:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                ok = await asyncio.wait_for(asyncio.to_thread(self._deps.isolate_chip, idx), timeout=left)
            except asyncio.TimeoutError:
                self._deps.health_event(
                    "chip_detach_timeout", chip=idx, timeout_sec=DETACH_TIMEOUT_SEC, host_at_risk=True
                )
                break
            if ok:
                removed.append(idx)
        return removed, [i for i in chips if i not in removed]

    def attached_all_ones(self) -> list:
        """Chips the sampler last saw reading all-ones (lost on two samples, or on a first strike)
        whose KMD class node is still there: maybe off the bus and not detached. No chip is read."""
        ids = set(chip_state.lost_chips()) | set(self.dead_chip_strikes)
        out = []
        for idx, node in self._nodes(ids).items():
            try:
                if node.exists():
                    out.append(idx)
            except OSError:
                out.append(idx)  # unreadable: count it as attached
        return sorted(out, key=int)

    def quiesce_refused_live(self) -> list:
        """The chips a reset's detach-before-quiesce could not detach that still read all-ones and
        are still attached (spec 03 I84)."""
        live = set(self.attached_all_ones())
        return sorted((i for i in self.quiesce_refused if i in live), key=int)

    def quiesce_failed_live(self) -> list:
        """The live refused chips whose remove failed or timed out (spec 03 I84): no reset can run over
        them, so the broker asks for the power cycle at once rather than at the 10-min bound."""
        if not self.quiesce_detach_failed:
            return []
        return [i for i in self.quiesce_refused_live() if i in self.quiesce_detach_failed]

    async def detach_before_quiesce(self, what: str) -> Optional[str]:
        """Detach every attached all-ones chip before a reset step stops the pollers (spec 03 I84,
        I66). A stopped poller closes its fds, and tt-kmd's close path reaches every chip that is
        not detached; only the PCI remove marks it. Returns why the step must not stop the pollers
        or reset, or None. A chip that only stalled (it still answers) needs nothing here.

        Refused, with nothing detached or stopped: every present chip reads all-ones (spec 03 I19
        never isolates the whole mesh, and that class goes straight to the power cycle), a chip an
        earlier step could not detach (refused at once, no new remove), or a remove that fails or
        runs past DETACH_TIMEOUT_SEC. A refusal records the holders it leaves alive, so the 10-min
        bound sends the hold to the host rung; a remove that failed or timed out also marks its chips
        in ``quiesce_detach_failed``, and the broker asks for the power cycle at once."""
        chips = self.attached_all_ones()
        if not chips:
            return None
        present = chip_state._sysfs_indices()
        reason = ""
        removed: list = []
        stuck: list = []
        if len(chips) >= len(present):
            reason, stuck = "all_chips_all_ones", chips
        elif any(i in self.quiesce_refused for i in chips):
            reason, stuck = "detach_failed_earlier", [i for i in chips if i in self.quiesce_refused]
        else:
            removed, stuck = await self._detach_chips(chips)
            # A remove the valve finished meanwhile fails here, yet the chip is detached.
            stuck = [i for i in stuck if i in self.attached_all_ones()]
            if removed:
                self._deps.isolated_chips().update(removed)  # the reset's rescan brings them back
                self._deps.health_event("chip_isolated", chips=removed)
            if stuck:
                reason = "detach_failed" if self._deps.isolate_chip is not None else "detach_unavailable"
        self._deps.health_event(
            "reset_quiesce_detach",
            what=what,
            chips=chips,
            detached=removed,
            refused=reason or None,
            stuck=stuck,
            host_at_risk=bool(reason),
        )
        if not reason:
            return None
        now = time.time()
        for i in stuck:
            self.quiesce_refused.setdefault(i, now)
        if reason == "detach_failed":
            self.quiesce_detach_failed.update(stuck)
        log = self._deps.logger()
        if log:
            log.error(
                f"RESET refused before the poller stop ({reason}): chip(s) {stuck} read all-ones and are "
                f"not detached, so stopping the pollers or resetting would make the driver reach them. "
                f"The hold stays; the power cycle is asked "
                f"{'now' if reason == 'detach_failed' or self.quiesce_failed_live() else 'at the 10-min bound'} — {what}"
            )
        if self._deps.note_kill_refused is not None:
            self._deps.note_kill_refused(reason, stuck)
        return reason

    async def _kill_refusal(self, stuck: list, others: list) -> Optional[str]:
        """Why the valve must not kill or stop anything yet, or None (spec 03 I84). Each case leaves
        a chip that is off the bus and still attached, and a holder's fd close would reach it."""
        if stuck:
            return "detach_failed" if self._deps.isolate_chip is not None else "detach_unavailable"
        if others:
            # On its first strike, inside the fast cadence's confirm gate, or an earlier failed
            # remove: its own valve run detaches it first and then kills.
            return "chip_not_detached"
        if self._mechanism.reset_in_flight or await asyncio.to_thread(self._mechanism.scope_active):
            return "reset_started"  # the reset owns the chips and their holders now
        return None

    async def isolate_dead_chips(self, dead: list) -> None:
        """Cut chips that have left the PCIe bus out of the kernel, immediately.

        Every other chip that read all-ones on the same sample (``sample_all_ones``) and is not
        removed here keeps every holder alive (spec 03 I84).

        This does NOT take the device lock, and that is deliberate. Every other device
        operation waits its turn because two of them at once is dangerous; this one cannot
        wait, because the danger is the waiting. From the moment a chip starts returning
        all-ones the host has perhaps a couple of minutes before some core stalls on a read
        that never completes and the machine is gone — and the thing holding the lock may be
        the very fabric check that is hung on the dead chip. Removing the endpoint is what
        makes further MMIO to it impossible; nothing else can, and nothing else matters until
        it is done.

        A job holding the device will fault when its mapping disappears. It was already lost —
        its chip is gone — and a failed job is a far better outcome than a reboot that
        destroys every other tenant's work as well.
        """
        log = self._deps.logger()
        isolated_chips = self._deps.isolated_chips()
        fresh = [i for i in dead if i not in isolated_chips]
        if not fresh:
            return
        isolated_chips.update(fresh)

        if log:
            log.error(
                f"DEAD CHIP(S) {fresh}: all reads return 0xFFFFFFFF — the chip has left "
                f"the PCIe bus. Isolating NOW; every further access risks a core stall "
                f"that reboots the host."
            )
        self._deps.health_event("chip_dead", chips=fresh, pci={i: self._deps.chip_pci_bdf(i) for i in fresh})

        # Detach first, then kill (spec 03 I84). The PCI remove is the only step that marks the
        # driver's device detached, and tt-kmd's fd-close path sends the chip a power-state message
        # unless it is: killing a holder of a chip still attached makes the kernel itself reach the
        # off-bus chip. A stopped poller closes its fd the same way, so it waits for the detach too.
        removed, stuck = await self._detach_chips(fresh)
        if stuck:
            # A dead chip that could not be removed is still on the bus and still
            # MMIO-reachable — the host is NOT out of danger, whatever the removed ones did.
            # isolate_chip journals each failure; say so at the decision layer too instead of
            # the blanket "out of danger" below, which claimed a safety the valve did not
            # deliver.
            if log:
                log.error(
                    f"DEAD CHIP(S) {stuck} could NOT be removed from the kernel — they "
                    f"remain on the bus and MMIO-reachable; the host is STILL at risk of "
                    f"a core stall that reboots it. Removed only {removed}."
                )
            self._deps.health_event("chip_isolation_incomplete", stuck=stuck, removed=removed, host_at_risk=True)
        elif log:
            log.error(
                f"DEAD CHIP(S) {removed} removed from the kernel — the host is out of "
                f"danger; the mesh is short {len(removed)} chip(s) until recovery"
            )
        self._deps.health_event("chip_isolated", chips=removed)

        # Then whoever still has the device open, and the pollers: tt-telemetry reads every chip in
        # a loop, and on a dead one it segfaults and systemd restarts it straight back into the
        # same read. An older driver leaves a userspace mapping of a removed chip in place, so a
        # holder could still read the dead BAR through it; these processes are already lost — the
        # chip under them is gone — and a dead job is a far better outcome than a lost host.
        reason = f"chip(s) {','.join(fresh)} left the PCIe bus"
        others = [i for i in (self.sample_all_ones or dead) if i not in removed]
        refused = await self._kill_refusal(stuck, others)
        if refused:
            if log:
                log.error(
                    f"DEAD-CHIP holders and pollers NOT killed ({refused}): closing their fds would "
                    f"make the driver touch a chip that is not detached. The hold and its bounded "
                    f"recovery decide — {reason}"
                )
            self._deps.health_event(
                "chip_dead_kill_refused",
                reason=refused,
                chips=stuck or others,
                detached=removed,
                host_at_risk=bool(stuck),
            )
        else:
            await self._deps.kill_device_holders(reason)
            await self._deps.set_device_pollers(False, lambda m: log.info(f"DEAD-CHIP {m}") if log else None)
            # No reset pairs this stop with a restore, and an idle relift may close the episode
            # without one: owe the restart, so the next close to HEALTHY (a passing full check)
            # starts them.
            if self._deps.owe_device_pollers is not None:
                self._deps.owe_device_pollers(reason)

        self._deps.mark_device_dirty(f"chip(s) {','.join(fresh)} fell off the PCIe bus", why="heartbeat")
        if refused and self._deps.note_kill_refused is not None:
            # After the hold opened: the holders left alive are recorded against it, so the 10-min bound
            # sends them to the host rung rather than reading them as tenants (spec 04 I76).
            self._deps.note_kill_refused(refused, stuck or others)
        await asyncio.to_thread(
            self._deps.capture_incident,
            "chip_dead",
            job=self._deps.episode_job(),
            trace=list(self.ring),
            evidence={"dead": fresh, "removed": removed},
        )
