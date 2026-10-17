# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""What /health says about a hold while the FSM is RECOVERING or DOWN (spec 03 I72, spec 04 I75).

Read-only reporting, nothing else: the hold's fault class, when it began, the next rung the broker
has armed and when it fires, and per UBB tray which chips are on the bus and which answer their ARC
heartbeat. Every input is a read the broker already makes (sysfs enumeration, the heartbeat read,
the governor's cooldown state, the hold watchdog's own clocks); nothing here touches a chip, calls a
chip healthy, admits a job or moves a rung. Pure functions, so each hold kind is testable alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

# The fault classes, as the recovery budgets name them.
CLASS_WHOLE_TRAY = "A"  # one or more whole UBB trays off the bus, the rest still on it
CLASS_PARTIAL = "B1"  # 1-3 chips off the bus, not whole trays
CLASS_TRAY_PARTIAL = "B2"  # 4 or more chips off the bus, not whole trays
CLASS_ALL_OFF = "C"  # every expected chip off the bus (sysfs blackout)
CLASS_PRESENT_MESH = "D"  # every expected chip on the bus: fabric/eth/ARC/dirty-job hold
CLASS_AC_NEEDED = "AC-needed"  # in-band power cycles did not heal it; AC power must be removed
HOLD_CLASSES = (CLASS_WHOLE_TRAY, CLASS_PARTIAL, CLASS_TRAY_PARTIAL, CLASS_ALL_OFF, CLASS_PRESENT_MESH, CLASS_AC_NEEDED)
PARTIAL_MAX_OFF = 3

NOT_VERIFIED = "not verified: sysfs presence and ARC heartbeat only; no health check has passed"

# The universal hold bound's mark: every hold runs the full ladder at this age, then a power cycle
# if the check after it still fails (outside the boot/post-cycle cooldown). The code that enforces
# it registers ``hold_bound_candidate`` with ``register_deadline_feed``; until it does, no mark is
# reported, because /health names only rungs some code will actually fire.
HOLD_BOUND_SEC = 600


@dataclass(frozen=True)
class RungCandidate:
    """One armed next step: ``rung`` fires at epoch ``at``; ``source`` names the clock that arms it."""

    at: float
    rung: str
    source: str


# Extra deadline sources a rung that is not on this branch yet feeds in, keyed by name. Each is
# called as ``feed(started_epoch, now_epoch, cooldown_until)`` and returns a RungCandidate or None.
DeadlineFeed = Callable[[float, float, Optional[float]], Optional[RungCandidate]]
_deadline_feeds: dict[str, DeadlineFeed] = {}


def register_deadline_feed(name: str, feed: DeadlineFeed) -> None:
    """Add (or replace) a deadline source ``next_rung`` takes the earliest of. The hook the
    universal hold bound feeds: ``register_deadline_feed("hold_bound", hold_bound_candidate)``."""
    _deadline_feeds[name] = feed


def unregister_deadline_feed(name: str) -> None:
    _deadline_feeds.pop(name, None)


def fed_candidates(started: float, now: float, cooldown_until: Optional[float]) -> list:
    """Every registered feed's candidate; a feed that raises reports nothing (read-only path)."""
    out = []
    for feed in list(_deadline_feeds.values()):
        try:
            c = feed(started, now, cooldown_until)
        except Exception:  # noqa: BLE001 - /health must answer even if a feed is broken
            c = None
        if c is not None:
            out.append(c)
    return out


def hold_bound_candidate(
    started: float, now: float, cooldown_until: Optional[float], bound_sec: int = HOLD_BOUND_SEC
) -> RungCandidate:
    """The full-ladder mark: hold start + ``bound_sec``. Inside the boot/post-cycle cooldown the
    mark moves to the cooldown's end, the one wait allowed past it."""
    at = started + bound_sec
    if cooldown_until is not None and now < cooldown_until and at < cooldown_until:
        return RungCandidate(cooldown_until, "full ladder, then power cycle", "hold bound (after cooldown)")
    return RungCandidate(at, "full ladder, then power cycle", f"hold bound ({bound_sec}s)")


def classify_hold(
    *,
    expected: int,
    off_bus: int,
    whole_trays: bool,
    ac_power_needed: bool = False,
) -> Optional[str]:
    """The hold's fault class from the live off-bus count. ``whole_trays`` is the I37 classifier's
    verdict that the off-bus set is exactly whole trays. None when no baseline chip count is known."""
    if ac_power_needed:
        return CLASS_AC_NEEDED
    if expected <= 0:
        return None
    if off_bus >= expected:
        return CLASS_ALL_OFF
    if off_bus <= 0:
        return CLASS_PRESENT_MESH
    if whole_trays:
        return CLASS_WHOLE_TRAY
    return CLASS_PARTIAL if off_bus <= PARTIAL_MAX_OFF else CLASS_TRAY_PARTIAL


def cooldown_candidate(cooldown: Optional[Mapping], at: float) -> RungCandidate:
    """The hold watchdog's retry of a power cycle the governor refused (spec 04 I65), due at ``at``:
    named by the refusal's record as the I65 cooldown end, a missed power-cycle request's retry or an
    AC-power recheck. The caller passes it only while that retry is armed for this hold episode."""
    cooldown = cooldown or {}
    if cooldown.get("ac_power_needed"):
        return RungCandidate(at, "AC-power recheck of the bus", "ac_power_needed")
    if cooldown.get("missed"):
        return RungCandidate(at, "power cycle (retry of a missed request)", "power_cycle_missed_retry")
    return RungCandidate(at, "power cycle", "power_cycle_cooldown")


def next_rung(candidates) -> Optional[RungCandidate]:
    """The earliest armed candidate; ties keep the first given."""
    best = None
    for c in candidates:
        if c is not None and (best is None or c.at < best.at):
            best = c
    return best


def trays_present(
    present_buses: Mapping,
    arc_alive: set,
    tray_groups: Mapping,
    read_at: str,
    chips_per_tray: int,
) -> list:
    """Per UBB bus group (spec 04 I16: a chip's tray is its PCI bus masked to ``0xF0``, never its
    index): the chip indices on the bus there and those with a live ARC heartbeat, all from one read
    at ``read_at``. ``present_buses`` is ``{chip index: PCI bus}``; ``tray_groups`` is
    ``{bus group: tray number or None}``. A chip on a bus outside the known groups is listed under
    its own group with ``tray`` None, never dropped."""
    rows: dict = {g: [] for g in tray_groups}
    for idx, bus in present_buses.items():
        if isinstance(bus, int):
            rows.setdefault(bus & 0xF0, []).append(int(idx))
    alive = {int(i) for i in arc_alive}
    out = []
    for group in sorted(rows):
        chips = sorted(rows[group])
        out.append(
            {
                "bus_group": f"0x{group:02x}",
                "tray": tray_groups.get(group),
                "chips_expected": chips_per_tray if group in tray_groups else None,
                "chips_present": chips,
                "arc_alive": [c for c in chips if c in alive],
                "read_at": read_at,
                "verified": False,
            }
        )
    return out
