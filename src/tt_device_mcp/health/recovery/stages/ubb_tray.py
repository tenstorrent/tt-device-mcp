# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The per-tray BMC power-cycle rung for a 6U Galaxy's UBBs."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Optional

from tt_device_mcp.health.recovery.stages.smi_reset import (
    _reset_ioctl_if_on_bus,
    _tt_smi_reset_device_ioctl,
)

_LOG = logging.getLogger(__name__)

# The tray's chips leave and re-join the bus across the BMC power pulse; wait this long between the
# pulse and the POST_RESET ioctl that re-inits them, so they have re-enumerated first. Measured at
# ~25-30s on a 6U Galaxy.
UBB_RESET_SETTLE_SEC = float(os.environ.get("TT_DEVICE_MCP_UBB_RESET_SETTLE_SEC", "28"))


class BmcIpmiError(RuntimeError):
    """The BMC did not take the tray re-power over IPMI: ``ipmitool`` exited non-zero (e.g. "No data
    available" from a BMC whose IPMI is wedged while Redfish still answers), timed out or could not
    start. Nothing was re-powered, and the BMC's other IPMI rungs will not work either, so the off-bus
    hold hands off to the host's power-cycle command on its own clock (spec 04 I27). ``returncode`` is
    ipmitool's exit code, for the journal; None when it never exited (missing, timed out)."""

    def __init__(self, message: str, returncode: Optional[int] = None):
        super().__init__(message)
        self.returncode = returncode


def _ubb_reset_argv(bitmap: int) -> list:
    """The BMC command that re-powers the trays in ``bitmap``, as an argv list. Bit i is tray i+1:
    tt-smi's tray tables are 1-based and the BMC's bits are not, so the caller shifts (spec 04 I16). The
    single source of truth for the command named in the loud actionable event so the string an
    operator is told to run is exactly the one the opted-in fire (F13b) would issue."""
    return ["ipmitool", "raw", "0x30", "0x8b", f"0x{bitmap:02x}", "0xff", "0x00", "0x0f"]


def _fire_ubb_reset(bitmap: int, tray_chip_ids: list, log=None):
    """Re-power exactly the trays in ``bitmap`` over the BMC, wrapped in the tt-smi ioctl handshake
    that quiesces the tray's chips before the power pulse and re-inits them after. Isolated so every
    test replaces it and no test path can reset real trays. Raises on a non-zero BMC exit so a fire
    that did not re-power is detectable.

    The handshake mirrors tt-smi's own full-tray reset, scoped to the downed tray's chips: a USER_RESET
    ioctl per chip quiesces the kernel's access, the partial ``ipmitool raw`` bitmap pulses only those
    trays' power, and after the chips re-enumerate a POST_RESET ioctl re-inits them. Skipping either
    ioctl half is what strands a chip in the no_bridge state the per-chip rung cannot then clear.

    tt_smi is imported lazily: it is absent on non-Galaxy hosts, and this fires only where an operator
    opted in on a real 6U Galaxy. ``tray_chip_ids`` are the PCIe interface ids of the downed trays'
    chips — the ipmitool bitmap alone decides which silicon is re-powered, so a mis-mapped id can only
    fail to quiesce/re-init, never re-power a healthy tray. A chip already off the bus (the very chip a
    tray-down reset exists to recover) has no /dev node to ioctl, so its quiesce/re-init is skipped
    rather than aborting the walk before the power pulse that would re-enumerate it.

    Raises ``pcie_guard.TrayRepowerRefused`` when the envelope refused to cut power (nothing fired), and
    :class:`BmcIpmiError` when the BMC did not take the pulse.
    Returns the envelope's ``TrayPlan``."""
    from tt_device_mcp.health.recovery import host_rung_latch, pcie_guard

    going_down = host_rung_latch.refuse("tray re-power", log)
    if going_down:
        raise pcie_guard.TrayRepowerRefused(going_down)  # spec 04 I34: nothing quiesced or re-powered

    from tt_smi.reset import IoctlResetFlags

    reset_device_ioctl = _tt_smi_reset_device_ioctl()

    def quiesce(_on_tray: list) -> None:
        for iid in tray_chip_ids:
            _reset_ioctl_if_on_bus(reset_device_ioctl, iid, IoctlResetFlags.USER_RESET)

    def pulse() -> None:
        try:
            r = subprocess.run(_ubb_reset_argv(bitmap), timeout=60, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BmcIpmiError(f"ubb tray reset did not run: {exc!r}"[:300]) from exc
        if r.returncode != 0:
            raise BmcIpmiError(f"ubb tray reset exited {r.returncode}: {r.stderr.strip()[:200]}", r.returncode)
        time.sleep(UBB_RESET_SETTLE_SEC)

    def reinit(_on_tray: list) -> None:
        for iid in tray_chip_ids:
            _reset_ioctl_if_on_bus(reset_device_ioctl, iid, IoctlResetFlags.POST_RESET)

    # The envelope (spec 04 I23) refuses a mis-mapped tray or a held chip, and masks AER on every
    # Tenstorrent root port while the tray is unpowered, so the fallout cannot flood the host.
    return pcie_guard.safe_tray_repower(bitmap, tray_chip_ids, pulse, log or _LOG.info, quiesce=quiesce, reinit=reinit)
