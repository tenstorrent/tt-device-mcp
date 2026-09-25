# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The mesh-wide tt-smi reset, plus the per-chip ioctl handshake tt-smi resets use."""

from __future__ import annotations


def _tt_smi_reset_device_ioctl():
    """Bind the per-chip reset ioctl ``(interface_id, flag)`` for whichever tt-smi the host ships.

    tt-smi 6.x exposes a module-level ``reset_device_ioctl``; earlier releases exposed it only as
    ``ChipReset().reset_device_ioctl``. The tray rung must fire on either surface — a version pin
    must never make the lightest recovery rung ImportError into a no-op that logs a spurious
    hardware failure and skips to a heavier rung."""
    from tt_smi import reset as tt_smi_reset

    module_level = getattr(tt_smi_reset, "reset_device_ioctl", None)
    if module_level is not None:
        return module_level
    chip_reset = getattr(tt_smi_reset, "ChipReset", None)
    if chip_reset is not None:
        return lambda iid, flag: chip_reset().reset_device_ioctl(iid, flag)
    raise ImportError("tt_smi.reset exposes neither reset_device_ioctl nor ChipReset")


def _reset_ioctl_if_on_bus(reset_device_ioctl, iid: int, flag) -> None:
    """Run the per-chip reset ioctl, skipping a chip that is not on the bus.

    The ioctl opens /dev/tenstorrent/{iid}, which is absent for a chip off the bus — the very chip a
    tray-down reset exists to recover. Pre-pulse there is nothing mapped to quiesce; post-pulse a
    still-absent chip simply did not re-enumerate. Either way the missing node must not abort the walk
    before the IPMI power pulse, nor mask a pulse that did run as a launch failure — the caller's
    re-verify is the authority on whether the tray came back. A non-FileNotFoundError is a real fault
    and still propagates to fall through to the next rung."""
    try:
        reset_device_ioctl(iid, flag)
    except FileNotFoundError:
        pass
