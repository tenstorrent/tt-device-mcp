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
from tt_device_mcp.health import ALL_ONES, SAMPLE_INTERVAL_SEC, SAMPLE_RING_SIZE

if TYPE_CHECKING:
    from tt_device_mcp.health import HealthMonitor, RecoveryDeps, RecoveryMechanism


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
        # Consecutive samples with NO chip nodes in sysfs — every chip off the bus, or the
        # driver wedged. chip_sample() omits an off-bus chip, so an all-gone drop reads as an
        # empty sample the all-ones blackout above never sees. Debounced like that blackout: a
        # lone empty sample from a rescan or a driver re-init settling must not trip it.
        self.all_chips_gone_strikes: int = 0
        # Consecutive samples with FEWER chip nodes than the host's baseline but not none — part of
        # the mesh off the bus. chip_sample() omits a gone node, so 24 of 32 reads as 24 healthy
        # chips; without this count the drop sets no flag until the next gate (spec 03 I30).
        self.short_count_strikes: int = 0
        # Liveness of this loop. Its per-iteration `except Exception` cannot catch a hung
        # `to_thread` device read on a wedged chip, so the loop can stall indefinitely while
        # the event loop keeps serving — see is_stalled() and the watchdog gate in server.py.
        self.last_tick: float = 0.0

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
            try:
                sample = await asyncio.to_thread(self._deps.chip_sample)
                self.ring.append({"ts": time.time(), "chips": sample})
                await asyncio.to_thread(self._deps.append_trace, sample)

                await self.check_for_dead_chips(sample)
                # The one loop that runs with an empty queue, so the one place an idle-degraded
                # window can be caught and put on the durable timeline as it happens.
                self._deps.refresh_idle_hold_ledger()
                # ...and the one place a self-heal hold gets re-verified with no job flowing to
                # do it. Spawned as its own task, never awaited here, so this loop keeps
                # sampling; read-only, rate-limited, inert unless the operator opts in.
                self._deps.spawn_idle_relift()
            except Exception:  # noqa: BLE001 - a sampler must never take the runner down
                pass
            await asyncio.sleep(SAMPLE_INTERVAL_SEC)

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
        if sample and not dead:
            self.dead_chip_strikes = {}
            self.all_chips_gone_strikes = 0
            # expected(0) reads the baseline without ratcheting it down to a short sample.
            # Health checks off (TT_DEVICE_MCP_HEALTH_CHECK=0) means no count check either (I25).
            expected = self._monitor.expected(0) if self._monitor._health_check_enabled() else 0
            if expected <= 0 or len(sample) >= expected:
                # Every chip is present and answering — genuinely healthy. Re-arm every debouncer.
                self.short_count_strikes = 0
                return
            await self._check_short_count(len(sample), expected, log)
            return
        self.short_count_strikes = 0

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
            expected = self._monitor.expected(0)
            if expected <= 0:
                # A host with no TT silicon: an empty class dir is its normal, healthy state,
                # never a drop. (A host that has ever shown chips keeps a nonzero baseline.)
                self.all_chips_gone_strikes = 0
                return
            self.all_chips_gone_strikes += 1
            # Same two-sample proof the all-ones blackout demands, and re-flagged only on the
            # way in (episode already dirty) so a dirty box does not re-journal the loss on
            # every idle sample. The dirty axis, not state-is-HEALTHY: a box under an affirmative
            # hold (eth self-heal, fabric-unverified — placed dirty=False) that then loses every
            # node has ESCALATED, and must still write the host-at-risk event and go dirty so the
            # next gate owes it a real reset.
            if self.all_chips_gone_strikes < 2 or self._deps.episode_dirty():
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

        if len(dead) == len(sample) and len(sample) > 1:
            # Every chip at once is the driver, the bus, or a reset — not 32 simultaneous chip
            # failures — so it never isolates. It must not dirty the device on one sample
            # either: anything that re-inits the mesh (a job's teardown, tt-smi, the validator)
            # makes the heartbeat read all-ones for a moment, and with a BLOCKING tenant gate a
            # single untrusted sample takes a healthy box offline. Hold it to the same
            # two-sample proof a single dead chip must pass; a real blackout stays, a re-init
            # blip does not.
            blackout_strikes = [i for i in dead if self.dead_chip_strikes.get(i, 0) >= 1]
            for i in dead:
                self.dead_chip_strikes[i] = self.dead_chip_strikes.get(i, 0) + 1
            if len(blackout_strikes) != len(dead):
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
        confirmed = [i for i in dead if self.dead_chip_strikes.get(i, 0) >= 1]
        for i in dead:
            self.dead_chip_strikes[i] = self.dead_chip_strikes.get(i, 0) + 1
        for i in list(self.dead_chip_strikes):
            if i not in dead:
                del self.dead_chip_strikes[i]

        if confirmed:
            await self.isolate_dead_chips(confirmed)

    async def _check_short_count(self, present: int, expected: int, log) -> None:
        """Fewer chip nodes than the baseline on two consecutive samples -> dirty (spec 03 I30).

        A reset in flight takes chips off the bus by design and is excused, as for all-ones. Flagged
        only on a HEALTHY box, unlike the all-gone drop: under an open episode (an off-bus hold the
        gate placed dirty=False, an isolated chip) the short count is that same fault, and
        re-dirtying it would turn the gate's hold back into a reset owed on every admission poll."""
        if self._mechanism.reset_in_flight or await asyncio.to_thread(self._mechanism.scope_active):
            return
        self.short_count_strikes += 1
        if self.short_count_strikes < 2 or self._deps.episode_open():
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

    async def isolate_dead_chips(self, dead: list) -> None:
        """Cut chips that have left the PCIe bus out of the kernel, immediately.

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

        # Kill whoever has the device mapped, FIRST, and this is the step that was missing.
        #
        # Removing the endpoint from the kernel does NOT tear down an existing userspace mmap.
        # A process that already mapped the chip's BARs still holds page tables pointing at it,
        # and its reads still go out to an address nothing answers — so removing the device
        # protects the driver and nothing else. A host was lost exactly this way with the fix
        # in place: the chip died 8 seconds into a fabric traffic pass, we cut the endpoint out
        # within 13, and the validator kept touching the dead BAR through its own mapping until
        # a core stalled and the watchdog took the machine 135 seconds later.
        #
        # These processes are already lost — the chip under them is gone — and a dead job is a
        # far better outcome than a reboot that destroys every other tenant's work as well.
        await self._deps.kill_device_holders(f"chip(s) {','.join(fresh)} left the PCIe bus")

        # Then the pollers: tt-telemetry reads every chip in a loop, and on a dead one it
        # segfaults and systemd restarts it straight back into the same read.
        await self._deps.set_device_pollers(False, lambda m: log.info(f"DEAD-CHIP {m}") if log else None)

        removed = []
        for idx in fresh:
            if await asyncio.to_thread(self._deps.isolate_chip, idx):
                removed.append(idx)
        stuck = [i for i in fresh if i not in removed]
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

        self._deps.mark_device_dirty(f"chip(s) {','.join(fresh)} fell off the PCIe bus", why="heartbeat")
        await asyncio.to_thread(
            self._deps.capture_incident,
            "chip_dead",
            job=self._deps.episode_job(),
            trace=list(self.ring),
            evidence={"dead": fresh, "removed": removed},
        )
