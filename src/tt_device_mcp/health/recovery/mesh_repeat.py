# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

"""The one capped repeat of a failed Galaxy mesh reset in a hold episode (spec 04 I93).

A first ``tt-smi -glx_reset`` that exits non-zero or fails the full check on a mesh with every chip on
the bus may get ONE more mesh reset in the same hold episode, spaced and admitted so it ends before the
10-min hold bound (spec 04 I76). A mesh reset is never killed mid-flight and its full check is not cut
short, so neither can be abandoned at the bound: the repeat starts only when its WORST case (the worst
mesh reset plus one full check pass) still ends by the bound, and its full check takes no 77 retry
that would run past it. Everything here is a code constant: no env var, config key or CLI option
changes the cap or turns the repeat off, and none brings back a removed rung.
"""

from __future__ import annotations

from typing import Optional

# At most one repeat per hold episode: two mesh resets before the 10-min ladder. Most repeats that
# heal a mesh heal on the second reset; later ones add little.
MESH_RESET_REPEAT_CAP = 1
# The repeat starts no sooner than this after the first reset ENDS: the gap before a repeat that
# healed was rarely shorter.
MESH_RESET_REPEAT_MIN_GAP_SEC = 200
# Worst-case wall time of one mesh reset, and of one full check pass after it (fabric included; the
# fabric check's own timeout).
MESH_RESET_WORST_SEC = 263
FULL_CHECK_WORST_SEC = 180
# What a repeat may take, start to verdict: the admission sizes it at this, never at an estimate.
REPEAT_WORST_SEC = MESH_RESET_WORST_SEC + FULL_CHECK_WORST_SEC
# The hold bound the repeat must end before (spec 04 I76). A repeat never pushes it later.
HOLD_BOUND_SEC = 600


def plan(
    *,
    repeats_used: int,
    hold_age_sec: float,
    since_first_end_sec: Optional[float],
) -> tuple[str, str, float]:
    """Decide the repeat from the clock alone: ``("due", "", 0)``, ``("wait", why, seconds_left)``, or
    ``("skip", why, 0)``. A skip leaves today's climb (the last-chance sweep, then the power cycle), and
    the 10-min ladder's mesh reset is the episode's second. A due or waiting repeat ends, at its worst
    case, by the bound."""
    if repeats_used >= MESH_RESET_REPEAT_CAP:
        return "skip", "cap_reached", 0.0
    if since_first_end_sec is None:
        return "skip", "first_reset_unknown", 0.0
    wait = max(0.0, MESH_RESET_REPEAT_MIN_GAP_SEC - since_first_end_sec)
    if hold_age_sec + wait + REPEAT_WORST_SEC > HOLD_BOUND_SEC:
        return "skip", "cannot_end_before_bound", 0.0
    if wait > 0:
        return "wait", "min_gap", wait
    return "due", "", 0.0
