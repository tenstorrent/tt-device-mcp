# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

"""Spacing of a hold episode's second Galaxy mesh reset (spec 04 I93).

When a first ``tt-smi -glx_reset`` on a mesh with every chip on the bus fails (non-zero exit or a failed
full check), the climb's last-chance sweep runs the episode's second mesh reset before the power cycle.
That second reset heals far more often when it starts some minutes after the first ended than right
after it, so the sweep waits for the gap, but never past the 10-min hold bound (spec 04 I76): at the
bound the ladder runs at once. Code constants only: no env var, config key or CLI option changes the
gap or turns it off, and none adds a third mesh reset.
"""

from __future__ import annotations

from typing import Optional

# The second mesh reset starts no sooner than this after the first ENDS, unless the bound comes first:
# the gap before a second reset that healed was rarely shorter.
MESH_RESET_MIN_GAP_SEC = 200
# The hold bound (spec 04 I76) the wait never runs past.
HOLD_BOUND_SEC = 600


def gap_wait(*, since_first_end_sec: Optional[float], hold_age_sec: Optional[float], bound_sec: float) -> float:
    """Seconds the sweep's mesh reset still waits: the gap left after the first reset ended, cut so the
    wait ends by the bound. 0 when nothing is known about the first reset or the hold clock (no wait),
    or when the bound is reached."""
    if since_first_end_sec is None or hold_age_sec is None:
        return 0.0
    left = min(MESH_RESET_MIN_GAP_SEC - since_first_end_sec, min(bound_sec, HOLD_BOUND_SEC) - hold_age_sec)
    return max(0.0, left)
