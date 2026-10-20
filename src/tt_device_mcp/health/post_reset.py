# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The post-reset fabric debt: no reset releases the device until a fabric pass has passed since.

Every reset action (a ``tt-smi -r`` or ``glx_reset`` through ``reset_with_quiesce``, a reset this
broker adopted from a foreign scope, a bridge chip reset, a UBB tray reset or re-power, a PCI
rescan) and every broker start marks the debt owed. Only a fabric pass that returns a healthy
verdict settles it; a 77 (the pass could not run) or a failure leaves it owed.

``_clear_device_dirty(verified=True)`` — the one release funnel every path goes through — consults
``blocks_release`` and holds fabric-unverified instead of releasing while the debt is open, so a
path that verified only enum+ARC, read a 77 as healthy, or skipped the fabric pass can no longer
release a reset mesh. There is no switch that turns this off. It does not apply where no fabric
pass can run on this host at all (a single chip, or no fabric check configured): nothing there
could ever settle it, and those hosts have no fabric for a reset to leave unproven.

The between-jobs path is unchanged while nothing is owed: no pass is added to a job boundary that
followed no reset.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors import fabric


class FabricDebt:
    """Process-wide record of the newest reset no healthy fabric pass has followed yet."""

    def __init__(self) -> None:
        self.owed: str = ""
        # Whether any fabric pass has passed since this broker started (spec 04 I29: a boot a 77 host
        # rung brought up has spent that rung until one does).
        self.passed_since_start: bool = False
        # When the newest healthy fabric verdict came in (time.monotonic(), 0.0 for none, or after a
        # failed pass): a hold lifts only on one that is fresh and newer than its latest fault (spec 03
        # I78).
        self.pass_monotonic: float = 0.0
        # Run on every settle (owed -> clear): see add_settle_listener.
        self._settle_listeners: "list[Callable[[], None]]" = []

    def add_settle_listener(self, callback: "Callable[[], None]") -> None:
        """Run ``callback()`` every time a healthy fabric pass settles an owed debt, for the life of
        the process. An operator reset never leaves HEALTHY, so this is the moment its verify passed.
        Must be cheap and thread-safe; a raise is logged and dropped."""
        if callback not in self._settle_listeners:
            self._settle_listeners.append(callback)

    def note_reset(self, what: str) -> None:
        """A reset (or a broker start) happened: the fabric is unproven until a pass clears it.
        Journaled on the transition only; a newer reset while owed just renames it."""
        if not self.owed:
            health_event("post_reset_fabric_owed", reset=what)
        self.owed = what

    def note_fabric(self, ok: Optional[bool]) -> None:
        """Fold one fabric verdict in: only a healthy one settles the debt."""
        if ok is True:
            self.passed_since_start = True
            self.pass_monotonic = time.monotonic()
        elif ok is False:
            self.pass_monotonic = 0.0
        if ok is True and self.owed:
            health_event("post_reset_fabric_settled", reset=self.owed)
            self.owed = ""
            for listener in list(self._settle_listeners):
                try:
                    listener()
                except Exception:  # noqa: BLE001 - a settle hook must never break the verdict
                    logging.getLogger(__name__).exception("fabric settle listener failed")

    def note_evidence(self, evidence: Optional[dict]) -> None:
        """Fold the fabric verdict of a verify pass's evidence in, when that pass ran one."""
        fab = (evidence or {}).get("fabric")
        if isinstance(fab, dict):
            self.note_fabric(fab.get("ok"))

    def blocks_release(self, expected: int) -> str:
        """The reset that still owes a fabric pass, or "" when a release may proceed."""
        if not self.owed or not self.runs_here(expected):
            return ""
        return self.owed

    @staticmethod
    def runs_here(expected: int) -> bool:
        """Whether a fabric pass can run on this host at all: a mesh (more than one chip) with a fabric
        check configured."""
        return expected > 1 and _fabric_check_configured()


def _fabric_check_configured() -> bool:
    """Whether a fabric pass can run here at all: ``fabric.build_command`` is not None, read without
    the cache directories that builds as a side effect."""
    return bool(os.environ.get("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "").strip()) or fabric.validator_missing() is None


FABRIC_DEBT = FabricDebt()
