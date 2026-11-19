# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The PCIe quiet window: no chip-side config-space read while the device is being started, checked or reset
(spec 03 I101).

Most of what the broker reads in sysfs is kernel memory: ``vendor``, ``device``, the ``aer_dev_*`` counters, a node
existing, the ``driver`` link. A few attributes are not. ``current_link_speed``, ``current_link_width``,
``max_link_speed`` and ``max_link_width`` read the function's PCIe capability on every open, and the ``config`` file
reads config space itself (as ``lspci``/``setpci`` do). Read on a chip, each one is a configuration request that
crosses the link to the chip. On a host whose chips have fallen off the bus seconds after a device start, with a
burst of link errors first, the broker's own pollers were taking those reads on every chip about once a second while
the start ran. So while a window is open, no gated reader sends one:

* a health check pass, from the end of its host-PCI stage to its end (the host-PCI stage runs first, before
  anything in the pass starts the device, and refreshes the cache below);
* the fabric traffic check, wherever it runs (the device start: clocks ramp and traffic starts on every link);
* a reset or recovery step on the chips (:func:`chip_state.recovering`), and :data:`SETTLE_SEC` after one ends;
* the first :data:`JOB_START_SEC` of a job, while its workload starts the device.

Inside the window a gated read serves the last value read outside it, with its age, and a link is judged down
from the chip's upstream port instead: Data Link Layer Link Active in the port's own Link Status. That read is
answered by the port (a root port is part of the host), never by the chip, and it is taken only where the port
says it reports the bit (Link Capabilities bit 20); a port that does not falls back to the cache. So a chip that
drops inside the window still reads as down at once, and every off-bus count and containment step decides as
before.

What the window keeps out is a read beside the work, not the work's own: the step or pass that opened a scoped
window (a reset step, a probe pass, the fabric pass) runs its own reads in its own task, one after another and
never while its device start runs, and those stay live, as a reset step's own reads always were (spec 03 I66). A
presence decision taken there from a stale cache could send a read to a dead chip, or a power cycle to a live one.
A job's start window has no such owner: the workload runs in its own process, so every broker read is gated.
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

from tt_device_mcp.health import chip_state

# The sysfs attributes that are config-space reads of the function they sit on.
CONFIG_SPACE_ATTRS = frozenset({"current_link_speed", "current_link_width", "max_link_speed", "max_link_width"})

# After a reset step ends, the chips are still training their links and booting: no read for this long.
SETTLE_SEC = 15.0
# After a job is handed the device, its workload starts it (clocks ramp, links come up): no read for this long.
JOB_START_SEC = 30.0

_BDF = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")

_lock = threading.Lock()
# owner -> [nesting count, opened at (wall), ends at (monotonic; inf while scoped)]
_owners: dict[str, list] = {}
# path -> (value, read at (wall))
_cache: dict[str, tuple] = {}
# How many scoped windows the current task (and the threads it hands work to) owns: its own reads stay live.
_own: contextvars.ContextVar[int] = contextvars.ContextVar("pcie_quiet_own", default=0)
_served = 0
_oldest_served: Optional[float] = None


def reset() -> None:
    """Forget every window and cached value (tests, and a broker restart)."""
    global _served, _oldest_served
    with _lock:
        _owners.clear()
        _cache.clear()
        _served = 0
        _oldest_served = None


def open_window(owner: str, for_sec: Optional[float] = None, *, own: bool = False) -> None:
    """Open (or nest) ``owner``'s window. ``for_sec`` makes it end by itself after that long; a scoped one ends at
    :func:`close_window`. ``own``: the calling task's own reads stay live until it closes the window."""
    if own:
        own_reads(True)
    ends = time.monotonic() + for_sec if for_sec is not None else math.inf
    with _lock:
        rec = _owners.get(owner)
        if rec is None:
            _owners[owner] = [1, time.time(), ends]
        else:
            rec[0] += 1
            if for_sec is not None:  # a timed window runs to the later end; a scoped one stays scoped
                rec[2] = max(rec[2], ends)


def close_window(owner: str, *, own: bool = False) -> None:
    """End one level of ``owner``'s window; a timed one ends now."""
    with _lock:
        rec = _owners.get(owner)
        if rec is None:
            return
        if own:
            own_reads(False)
        rec[0] -= 1
        if rec[0] <= 0 or not math.isinf(rec[2]):
            del _owners[owner]


@contextlib.contextmanager
def window(owner: str) -> Iterator[None]:
    """The window open for the ``with`` block, on every exit path."""
    open_window(owner, own=True)
    try:
        yield
    finally:
        close_window(owner, own=True)


def own_reads(on: bool) -> None:
    """The calling task starts (``on``) or ends one level of work that owns the window: its reads stay live."""
    _own.set(_own.get() + 1 if on else max(0, _own.get() - 1))


def owners() -> list[str]:
    """Why the window is open now, empty when it is not."""
    now = time.monotonic()
    with _lock:
        for owner in [o for o, rec in _owners.items() if rec[2] <= now]:
            del _owners[owner]
        out = sorted({o.split(":", 1)[0] for o in _owners})
    if chip_state.recovering():
        out.append("reset")
    ended = chip_state.step_ended_at()
    if ended is not None and now - ended < SETTLE_SEC:
        out.append("settle")
    return out


def active() -> bool:
    """Whether a gated read from here must be served from the cache: a window is open and this task does not own it."""
    return _own.get() == 0 and bool(owners())


def _remember(key: str, value) -> None:
    with _lock:
        _cache[key] = (value, time.time())


def _serve(key: str):
    global _served, _oldest_served
    with _lock:
        hit = _cache.get(key)
        _served += 1
        if hit is not None:
            _oldest_served = hit[1] if _oldest_served is None else min(_oldest_served, hit[1])
    return hit[0] if hit is not None else None


def read_attr(path: Path, reader: Optional[Callable[[Path], str]] = None) -> Optional[str]:
    """One config-space sysfs attribute (:data:`CONFIG_SPACE_ATTRS`), stripped; None when unreadable. Outside the
    window it is read and remembered; inside, the last value read is served (None if there is none)."""
    key = str(path)
    if path.name in CONFIG_SPACE_ATTRS and active():
        return _serve(key)
    try:
        value = (reader or (lambda p: p.read_text()))(path).strip()
    except OSError:
        value = None
    _remember(key, value)
    return value


def read_config(path: Path, size: int) -> Optional[bytes]:
    """The first ``size`` bytes of a function's ``config`` file; None when unreadable. Outside the window it is read
    and remembered; inside, the last bytes read are served (None if there are none)."""
    key = f"{path}#{size}"
    if active():
        return _serve(key)
    try:
        with open(path, "rb", buffering=0) as f:
            raw = f.read(size)
    except OSError:
        raw = None
    _remember(key, raw)
    return raw


def upstream_port(dev: Path) -> Optional[Path]:
    """The port directly above the function at ``dev`` (its sysfs parent), or None when that is not a PCI function."""
    try:
        parent = dev.resolve().parent
    except OSError:
        return None
    return parent if _BDF.match(parent.name) else None


def port_link_active(port: Path) -> Optional[bool]:
    """Data Link Layer Link Active of ``port``'s link (Link Status bit 13), read from the port's own config space,
    or None when the port does not report it (Link Capabilities bit 20 clear: the bit then always reads 0) or it
    cannot be read. The port answers this itself; nothing reaches the chip below it."""
    try:
        with open(port / "config", "rb", buffering=0) as f:
            cfg = f.read(256)
    except OSError:
        return None
    if len(cfg) < 64 or not cfg[0x06] & 0x10:  # no capability list
        return None
    pos, seen = cfg[0x34] & 0xFC, set()
    while pos and pos + 0x14 <= len(cfg) and pos not in seen:
        seen.add(pos)
        if cfg[pos] == 0x10:  # PCI Express capability
            if not int.from_bytes(cfg[pos + 0x0C : pos + 0x10], "little") & (1 << 20):
                return None
            return bool(int.from_bytes(cfg[pos + 0x12 : pos + 0x14], "little") & 0x2000)
        pos = cfg[pos + 1] & 0xFC
    return None


def link_down_quiet(dev: Path) -> Optional[bool]:
    """Inside the window: whether the link to the function at ``dev`` is down, from its upstream port alone. None
    when the port cannot tell (the caller then serves its cache)."""
    port = upstream_port(dev)
    if port is None:
        return None
    up = port_link_active(port)
    return None if up is None else not up


def status() -> dict:
    """For /health: whether gated link reads are being served from the cache now, why, and the age of the oldest
    value served since the window opened."""
    why = owners()
    now = time.time()
    global _served, _oldest_served
    with _lock:
        since = min((rec[1] for rec in _owners.values()), default=None)
        out = {
            "quiet": bool(why),
            "why": why,
            "since": None if since is None else datetime.fromtimestamp(since).isoformat(timespec="seconds"),
            "served_from_cache": _served,
            "oldest_cached_age_sec": None if _oldest_served is None else round(now - _oldest_served, 1),
        }
        if not why:
            _served = 0
            _oldest_served = None
    return out
