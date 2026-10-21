# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

"""The one capped repeat of a failed Galaxy mesh reset in a hold episode (spec 04 I93).

A first ``tt-smi -glx_reset`` that exits non-zero or fails the full check on a mesh with every chip on
the bus may get ONE more mesh reset in the same hold episode, spaced and sized so it ends before the
10-min hold bound (spec 04 I76). Everything here is a code constant: no env var, config key or CLI
option changes the cap or turns the repeat off, and none brings back a removed rung.
"""

from __future__ import annotations

from typing import Optional

# At most one repeat per hold episode: two mesh resets before the 10-min ladder. Most repeats that
# heal a mesh heal on the second reset; later ones add little.
MESH_RESET_REPEAT_CAP = 1
# The repeat starts no sooner than this after the first reset ENDS: the gap before a repeat that
# healed was rarely shorter.
MESH_RESET_REPEAT_MIN_GAP_SEC = 200
# Worst-case wall time of one mesh reset, and of the full check after it (fabric included).
MESH_RESET_WORST_SEC = 263
FULL_CHECK_WORST_SEC = 180
# Floor for the reset leg when sizing the repeat from the first reset's measured time.
MESH_RESET_FLOOR_SEC = 60
# The hold bound the repeat must end before (spec 04 I76). A repeat never pushes it later.
HOLD_BOUND_SEC = 600


def repeat_cost_sec(first_reset_sec: Optional[float]) -> float:
    """Estimated wall time of the repeat: the reset leg plus the full check's worst case. The reset leg
    is the first reset's own measured time on this mesh (same command, same box), floored at
    MESH_RESET_FLOOR_SEC and capped at MESH_RESET_WORST_SEC; the worst case when it is unknown."""
    if first_reset_sec is None or first_reset_sec <= 0:
        leg = MESH_RESET_WORST_SEC
    else:
        leg = min(MESH_RESET_WORST_SEC, max(MESH_RESET_FLOOR_SEC, first_reset_sec))
    return leg + FULL_CHECK_WORST_SEC


def plan(
    *,
    repeats_used: int,
    hold_age_sec: float,
    since_first_end_sec: Optional[float],
    first_reset_sec: Optional[float],
) -> tuple[str, str, float]:
    """Decide the repeat from the clock alone: ``("due", "", 0)``, ``("wait", why, seconds_left)``, or
    ``("skip", why, 0)``. A skip leaves today's climb (the last-chance sweep, then the power cycle), and
    the 10-min ladder's mesh reset is the episode's second."""
    if repeats_used >= MESH_RESET_REPEAT_CAP:
        return "skip", "cap_reached", 0.0
    if since_first_end_sec is None:
        return "skip", "first_reset_unknown", 0.0
    wait = max(0.0, MESH_RESET_REPEAT_MIN_GAP_SEC - since_first_end_sec)
    if hold_age_sec + wait + repeat_cost_sec(first_reset_sec) > HOLD_BOUND_SEC:
        return "skip", "cannot_end_before_bound", 0.0
    if wait > 0:
        return "wait", "min_gap", wait
    return "due", "", 0.0
