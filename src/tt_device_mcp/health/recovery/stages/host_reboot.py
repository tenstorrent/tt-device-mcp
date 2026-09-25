# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The warm host reboot recovery rung."""

from __future__ import annotations

import subprocess


def _fire_host_reboot() -> None:
    """Shell out to the actual host reboot. Isolated to one line so every test replaces it and
    no test path can ever reboot the box running the suite. Raises on a non-zero exit / missing
    binary so a fire that did NOT take the box down is detectable (see _fire_recovery_escalation)."""
    r = subprocess.run(["systemctl", "reboot"], timeout=30, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl reboot exited {r.returncode}: {r.stderr.strip()[:200]}")
