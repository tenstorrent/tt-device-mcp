# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The reason record every host power cycle carries (spec 04 I113).

A :class:`PowerCycleReason` is a ``str`` (the one-line cause every caller already formats) that also holds the
structured record: the trigger, the fault class, the affected chips with PCI address, tray and upstream root port, the
last failed check and its result, the rungs run in the hold, and the hold's start and age. Every path that requests a
host power cycle must pass one: :meth:`PowerCycleReason.require` raises ``TypeError`` for anything else, so a request
without the record cannot be written. Nothing here reads a chip: the fields come from memory, and the root port from
sysfs.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

from tt_device_mcp.health.evidence import health_dir

_logger = logging.getLogger("tt-device-mcp")

# The stable prefix of the one WARNING line written before the reboot is issued.
LINE_PREFIX = "host power cycle:"
# Kept across the reboot: written and fsynced before it is issued, read back by the boot's confirmation.
POWER_CYCLE_REASON_FILE = "power_cycle_reason.json"
# The rungs listed in a record: the newest few commands of the hold.
MAX_RUNGS = 12


def _epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text, limit: int = 300) -> str:
    return " ".join(str(text).split())[:limit]


def build_chips(bdfs: dict, tray_map: Optional[dict] = None) -> list:
    """``[{chip, bdf, tray, root_port}]`` for ``{chip index: PCI address or None}``.

    ``tray_map`` is ``{tray: [chip, ...]}`` from the galaxy recovery's banked map (None when none is banked). The root
    port is the PCIe bridge above the address, read from sysfs only (never a config-space read: a chip that left the
    bus must not be touched again). A field that cannot be told is None."""
    tray_of: dict = {}
    for tray, members in (tray_map or {}).items():
        for c in members or ():
            tray_of[str(c)] = tray
    rows = []
    for chip, bdf in sorted(bdfs.items(), key=lambda i: (len(str(i[0])), str(i[0]))):
        root = None
        if bdf:
            try:
                from tt_device_mcp.health.bmc_capture import upstream_bridge

                root = upstream_bridge(str(bdf))
            except Exception:  # noqa: BLE001 - a field of a log record; never holds the cycle
                root = None
        rows.append({"chip": str(chip), "bdf": bdf or None, "tray": tray_of.get(str(chip)), "root_port": root or None})
    return rows


class PowerCycleReason(str):
    """Why the broker requests a host power cycle. Its text is the one-line cause; the attributes are the record.

    ``trigger`` (what asked: ``idle_escalation``, ``tray_down``, ``hold_deadline``, ``offbus_gated``,
    ``leftover_holder``, ``operator``, ...) and ``fault_class`` are required; the rest is filled where it is known and
    completed from the broker's memory by :meth:`completed`."""

    trigger: str
    fault_class: str
    chips: list
    failed_check: str
    failed_result: str
    rungs: list
    hold_since: str
    hold_age_sec: Optional[int]
    requested_epoch: float

    def __new__(
        cls,
        text,
        *,
        trigger: str,
        fault_class: str,
        chips: Optional[Iterable] = None,
        failed_check: str = "",
        failed_result: str = "",
        rungs: Optional[Iterable] = None,
        hold_since: str = "",
        hold_age_sec: Optional[int] = None,
        requested_epoch: Optional[float] = None,
    ):
        if not trigger or not fault_class:
            raise ValueError("a power-cycle reason needs its trigger and fault class")
        self = super().__new__(cls, _one_line(text, 600) or trigger)
        self.trigger = str(trigger)
        self.fault_class = str(fault_class)
        self.chips = [dict(c) for c in (chips or ())]
        self.failed_check = str(failed_check or "")
        self.failed_result = str(failed_result or "")
        self.rungs = [dict(r) for r in (rungs or ())]
        self.hold_since = str(hold_since or "")
        self.hold_age_sec = None if hold_age_sec is None else int(hold_age_sec)
        self.requested_epoch = float(requested_epoch if requested_epoch is not None else time.time())
        return self

    @staticmethod
    def require(reason) -> "PowerCycleReason":
        """``reason`` itself when it is a record; ``TypeError`` for a plain string or anything else."""
        if not isinstance(reason, PowerCycleReason):
            raise TypeError(f"a host power cycle needs a PowerCycleReason record, got {type(reason).__name__}")
        return reason

    def completed(
        self,
        *,
        fault_class: str = "",
        bdfs: Optional[dict] = None,
        tray_map: Optional[dict] = None,
        hold_since: str = "",
        rungs: Optional[Iterable] = None,
    ) -> "PowerCycleReason":
        """A copy with what this record lacks filled in from the broker's memory (never a chip read): the chips
        (with tray and root port) when none are named, the hold start and its age, the rungs run in the hold, and a
        fault class the caller left generic."""
        chips = self.chips or build_chips(bdfs or {}, tray_map)
        since = self.hold_since or hold_since or ""
        age = self.hold_age_sec
        if age is None and since:
            try:
                started = datetime.fromisoformat(since)
                if started.tzinfo is None:
                    started = started.astimezone()
                age = max(0, int(self.requested_epoch - started.timestamp()))
            except ValueError:
                age = None
        return PowerCycleReason(
            str(self),
            trigger=self.trigger,
            fault_class=self.fault_class if self.fault_class not in ("", "unknown") else (fault_class or "unknown"),
            chips=chips,
            failed_check=self.failed_check,
            failed_result=self.failed_result,
            rungs=self.rungs or list(rungs or ()),
            hold_since=since,
            hold_age_sec=age,
            requested_epoch=self.requested_epoch,
        )

    def as_dict(self) -> dict:
        return {
            "reason": str(self),
            "trigger": self.trigger,
            "fault_class": self.fault_class,
            "chips": self.chips,
            "failed_check": self.failed_check,
            "failed_result": self.failed_result,
            "rungs": self.rungs[-MAX_RUNGS:],
            "hold_since": self.hold_since,
            "hold_age_sec": self.hold_age_sec,
            "requested_epoch": round(self.requested_epoch, 3),
            "requested_at": _epoch_iso(self.requested_epoch),
        }

    def line(self) -> str:
        """The one WARNING line logged before the reboot is issued."""
        chips = ", ".join(
            f"{c.get('chip')} ({c.get('bdf') or 'address unknown'}, tray {c.get('tray', '?')}, "
            f"root port {c.get('root_port') or 'unknown'})"
            for c in self.chips
        )
        rungs = ", ".join(f"{r.get('rung')}={r.get('status')}" for r in self.rungs[-MAX_RUNGS:])
        age = "unknown" if self.hold_age_sec is None else f"{self.hold_age_sec}s"
        parts = [
            f"{LINE_PREFIX} trigger={self.trigger}",
            f"class={self.fault_class}",
            f"chips=[{chips}]",
            f"last_check={self.failed_check or 'unknown'}",
            f"result={self.failed_result or 'unknown'}",
            f"rungs=[{rungs}]",
            f"hold_since={self.hold_since or 'unknown'}",
            f"hold_age={age}",
            f"reason={self}",
        ]
        return " ".join(" ".join(parts).split())[:1500]


def rung_row(epoch: float, command: str, status: str) -> dict:
    """One rung the hold ran, as the record lists it: when, its command and how it ended."""
    return {"at": _epoch_iso(epoch), "epoch": round(epoch, 1), "rung": _one_line(command, 80), "status": status}


def write_state(reason: PowerCycleReason, *, unit: str = "") -> float:
    """Persist the record, fsynced, BEFORE the reboot is issued, so the boot that follows reads it back. Stamps the
    record's request time (now: after the ledger row, before the boot) and returns it. A record that cannot be written
    is logged loudly and the cycle still goes: a log record never weakens the recovery."""
    reason.requested_epoch = time.time()
    path = health_dir() / POWER_CYCLE_REASON_FILE
    row = {**reason.as_dict(), "unit": unit, "outcome": "requested"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        _logger.error("%s reason record NOT written (%s); cycling anyway", LINE_PREFIX, e)
    return reason.requested_epoch


def read_state() -> Optional[dict]:
    """The last record on disk, or None when there is none or it does not parse. Not removed."""
    try:
        row = json.loads((health_dir() / POWER_CYCLE_REASON_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict) or not isinstance(row.get("requested_epoch"), (int, float)):
        return None
    return row


def mark_outcome(outcome: str, requested_epoch: Optional[float] = None, **extra) -> None:
    """Set the record's outcome (``confirmed``, ``unconfirmed``, ``not_launched``), keeping it for the status line.
    A record of another request (its epoch differs) is left alone. Best effort."""
    row = read_state()
    if row is None or (requested_epoch is not None and abs(row["requested_epoch"] - requested_epoch) > 0.5):
        return
    row.update(extra, outcome=outcome)
    path = health_dir() / POWER_CYCLE_REASON_FILE
    try:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        _logger.warning("power-cycle reason record outcome not written: %s", e)


def record_for_boot(low: float, high: float) -> Optional[dict]:
    """The record of the request made between ``low`` and ``high`` (epochs: the ledger row's time, this boot's
    start), or None. The boot's confirmation attaches it to ``power_cycle_confirmed``."""
    row = read_state()
    if row is None or not (low <= row["requested_epoch"] <= high):
        return None
    return row


def last_power_cycle() -> Optional[dict]:
    """``{at, reason, trigger, fault_class, outcome, ...}`` of the last request, for the health payload and status."""
    row = read_state()
    if row is None:
        return None
    keys = ("requested_at", "reason", "trigger", "fault_class", "chips", "failed_check", "failed_result", "hold_since")
    out = {k: row.get(k) for k in keys}
    out.update(hold_age_sec=row.get("hold_age_sec"), outcome=row.get("outcome") or "requested")
    return out
