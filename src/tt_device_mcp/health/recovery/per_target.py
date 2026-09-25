# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-target recovery: every non-Galaxy board (n150, n300, loudbox, ...) resets per PCIe target
and has no tray or host-level escalation rung — a single/few-chip host has no whole-mesh inversion
failure mode for a reboot/power-cycle to guard against, and no UBB tray to power-cycle."""

from __future__ import annotations

from tt_device_mcp.health.recovery import DEFER, STAGE_SMI_RESET, WAIT, Evidence, Recovery


class PerTargetRecovery(Recovery):
    """Recovery for anything that is not a Galaxy: a per-PCIe-target ``tt-smi -r`` reset, the same
    cooling/in-flight-scope guards as the Galaxy ladder, and nothing above it — no bridge-reset
    surgical rung (today's gate runs that only ahead of the mesh-wide reset) and no host escalation."""

    def next_stage(self, ev: Evidence) -> str:
        if ev.cooling:
            # A reset we already tried and that already failed to revive the device will not
            # revive it now; hammering a dead endpoint is what escalates a PCIe error to fatal.
            return WAIT
        if ev.scope_active:
            # A reset is already cycling in its own PID-1 scope; adopt and verify it rather than
            # starting a second one.
            return DEFER
        return STAGE_SMI_RESET

    def _platform_reset_argv(self, indices: list) -> list:
        return ["tt-smi", "-r", ",".join(indices)]
