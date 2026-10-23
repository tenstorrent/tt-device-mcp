# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The one in-flight blocking PCI sysfs writer, and the PCI-wedged latch (spec 04 I76, spec 03 I83).

A write to /sys/bus/pci/rescan, a chip's ``remove`` or a bridge reset waits on the kernel's PCI
rescan/remove lock and on the device it walks. Behind a hung endpoint it can block in the kernel for
good, and no thread can be cancelled there. So every such write in the broker goes through
:data:`PCI_WRITER`:

- one write in flight per broker, so a write blocked in the kernel leaks at most one worker thread
  of the default executor per host;
- a write that does not return within its timeout latches PCI-wedged and raises :class:`PciWedged`;
  its thread may stay blocked until the host is power-cycled (an accepted gap);
- while that thread is alive, or while the latch is set, a new write is refused at once and starts
  no thread (a refusal also latches);
- the latch tells the broker (``on_latch``), which holds the device and requests the power cycle at
  once, subject only to the bounded post-boot/post-cycle cooldown: no rung can run over a blocked
  PCI lock, so waiting for the 10-min mark gains nothing;
- the latch clears only with no write pending and a full health check, fabric included, just
  passed (:meth:`PciSysfsWriter.clear`, called from the one verified-lift funnel).

Bound: at most one PCI timeout per ladder and one leaked thread per host until the cycle.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from tt_device_mcp.health.evidence import health_event

# Default bound on one rescan or remove; a bridge reset passes its own (it runs setpci and a rescan).
PCI_SYSFS_WRITE_TIMEOUT_SEC = 30


class PciWedged(asyncio.TimeoutError):
    """A PCI sysfs write did not return in time, or was refused because the latch is set or an
    earlier write's thread is still blocked. Every caller treats it as its rung failing."""


class PciSysfsWriter:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Future] = None
        self._deadline = 0.0
        self._context = ""
        self.latched_at: Optional[float] = None  # epoch
        self.latched_mono: Optional[float] = None
        self.reason = ""
        # Set by the broker at boot: holds the device and asks for the power cycle. Called once per latch.
        self.on_latch: Optional[Callable[[str], None]] = None

    @property
    def latched(self) -> bool:
        return self.latched_at is not None

    def pending(self) -> str:
        """The context of a write whose thread is still alive, or ""."""
        t = self._task
        return self._context if t is not None and not t.done() else ""

    def _latch(self, reason: str, **fields: Any) -> None:
        if self.latched:
            return
        self.latched_at, self.latched_mono, self.reason = time.time(), time.monotonic(), reason
        health_event("pci_wedged", reason=reason, host_at_risk=True, **fields)
        if self.on_latch is not None:
            try:
                self.on_latch(reason)
            except Exception:  # noqa: BLE001 - the latch stands; the hold watchdog still asks
                pass

    def _refuse(self, context: str) -> PciWedged:
        pending = self.pending()
        why = (
            f"an earlier PCI write ({pending}) is still blocked in the kernel"
            if pending
            else f"PCI-wedged since an earlier write: {self.reason}"
        )
        self._latch(f"{context}: refused, {why}", context=context, pending=pending)
        health_event("pci_write_refused", context=context, pending=pending, reason=why)
        return PciWedged(f"PCI write refused: {why}")

    def clear(self, why: str) -> bool:
        """Clear the latch once no write is pending. The caller has just seen the full check with fabric
        pass. Returns False (latch kept) while a thread is still blocked."""
        if not self.latched:
            return True
        if self.pending():
            return False
        health_event("pci_wedged_cleared", why=why, reason=self.reason, latched_at=self.latched_at)
        self.latched_at = self.latched_mono = None
        self.reason = ""
        return True

    def reset_for_tests(self) -> None:
        on_latch = self.on_latch
        self.__init__()
        self.on_latch = on_latch

    async def write(self, context: str, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None) -> Any:
        """Run ``fn(*args)`` as the one PCI sysfs write in flight, at most ``timeout`` seconds
        (PCI_SYSFS_WRITE_TIMEOUT_SEC by default). Returns its result or raises its exception; raises
        PciWedged on a timeout or a refusal."""
        if timeout is None:
            timeout = PCI_SYSFS_WRITE_TIMEOUT_SEC
        # Wait out a write still inside its own bound (a concurrent caller, or one whose awaiting task
        # was cancelled); one past its bound is the wedge.
        while True:
            if self.latched:
                raise self._refuse(context)
            if not self.pending():
                break
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise self._refuse(context)
            task = self._task
            try:
                if task is not None and task.get_loop() is asyncio.get_running_loop():
                    await asyncio.wait({task}, timeout=remaining)
                else:
                    raise self._refuse(context)
            except asyncio.TimeoutError:
                raise self._refuse(context) from None
        # One worker thread of the default executor; the one-in-flight rule caps what a blocked write
        # can leak there at one thread per host.
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        self._task, self._context = task, context
        self._deadline = time.monotonic() + timeout
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            if task.done() and not task.cancelled() and isinstance(task.exception(), asyncio.TimeoutError):
                raise  # the write itself raised a timeout; it returned, so nothing is blocked
            self._latch(f"{context}: no answer in {timeout:.0f} s", context=context, timeout_s=timeout)
            raise PciWedged(f"{context}: no answer in {timeout:.0f} s (its thread may stay blocked)") from None


PCI_WRITER = PciSysfsWriter()
