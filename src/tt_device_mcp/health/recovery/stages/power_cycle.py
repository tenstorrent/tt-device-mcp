# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cold BMC chassis power-cycle recovery rung — the final one on the ladder."""

from __future__ import annotations

import subprocess


def _fire_power_cycle() -> None:
    """Shell out to the BMC chassis power cycle — the one action that recovers a whole-bus wedge
    a warm reboot leaves wedged (measured on blx04). Isolated to one line so every test replaces
    it and no test path can ever power-cycle the box running the suite. Raises on a non-zero exit /
    missing binary so a fire that did NOT take the box down is detectable."""
    r = subprocess.run(["ipmitool", "chassis", "power", "cycle"], timeout=30, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ipmitool power cycle exited {r.returncode}: {r.stderr.strip()[:200]}")
