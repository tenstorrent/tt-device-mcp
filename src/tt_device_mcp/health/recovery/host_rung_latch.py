# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The host-rung-in-flight latch (spec 04 I34).

A host rung's fire returns as soon as the action is queued: ``systemctl reboot`` hands the job to
PID 1 and exits 0, and a BMC power-cycle command returns once the BMC took it. The box then shuts
down over the next seconds to minutes while this process still runs. Anything that resets the device
in that window (the hold watchdog's next forced pass, the gate, an operator reset) races the shutdown:
a parent-bridge reset and a PCI rescan once ran in the same second the reboot was queued.

So a fire that returned normally latches here, process-wide, and every reset entry point refuses
while it is set. A reboot or power cycle that really happens ends the process, and the latch with it.
One that never happens (cancelled, or a command that returned without acting) must not hold the
device forever, so the latch expires after ``TT_DEVICE_MCP_HOST_RUNG_LATCH_SEC`` (default 1800 s)
and the ladder may act again."""

from __future__ import annotations

import os
import time
from typing import Optional

from tt_device_mcp.health.evidence import health_event

HOST_RUNG_LATCH_SEC_DEFAULT = 1800

# {"action": "reboot" | "power-cycle", "reason": str, "at": epoch seconds}, or None.
_LATCH: Optional[dict] = None
# Entry points already journalled as refused under the current latch, so a sampler driving the
# watchdog every few seconds writes one event per entry point, not one per tick.
_REFUSED_SEEN: set = set()


def latch_sec() -> float:
    """How long a latched host rung refuses resets before it is presumed not to have happened. A
    non-positive or malformed value reads as the default: the latch is never switched off."""
    try:
        sec = float(os.environ.get("TT_DEVICE_MCP_HOST_RUNG_LATCH_SEC", HOST_RUNG_LATCH_SEC_DEFAULT))
    except ValueError:
        return float(HOST_RUNG_LATCH_SEC_DEFAULT)
    return sec if sec > 0 else float(HOST_RUNG_LATCH_SEC_DEFAULT)


def latch(action: str, reason: str, *, now: Optional[float] = None) -> None:
    """Record that a host rung's fire returned normally: the box is going down."""
    global _LATCH
    _LATCH = {"action": action, "reason": reason, "at": time.time() if now is None else now}
    _REFUSED_SEEN.clear()
    health_event("host_rung_in_flight", action=action, reason=reason, latch_sec=int(latch_sec()))


def in_flight(*, now: Optional[float] = None) -> Optional[dict]:
    """The latched host rung, or None. Clears an expired latch, loudly: the box did not go down."""
    global _LATCH
    if _LATCH is None:
        return None
    age = (time.time() if now is None else now) - _LATCH["at"]
    if age < latch_sec():
        return _LATCH
    health_event("host_rung_latch_expired", action=_LATCH["action"], reason=_LATCH["reason"], age_sec=int(age))
    _LATCH = None
    _REFUSED_SEEN.clear()
    return None


def refuse(entry: str, log=None, *, now: Optional[float] = None) -> str:
    """The refusal text when a host rung is in flight, else "". ``entry`` names the reset path;
    the first refusal per path under one latch is journalled and logged."""
    rec = in_flight(now=now)
    if rec is None:
        return ""
    age = int((time.time() if now is None else now) - rec["at"])
    text = f"a host {rec['action']} was requested {age}s ago ({rec['reason']}); no reset while the box goes down"
    if entry not in _REFUSED_SEEN:
        _REFUSED_SEEN.add(entry)
        health_event("reset_refused_host_rung_in_flight", entry=entry, action=rec["action"], age_sec=age)
        if log is not None:
            log(f"{entry} refused: {text}")
    return text


def clear() -> None:
    """Drop the latch. Tests only: in a broker the latch ends with the process or expires."""
    global _LATCH
    _LATCH = None
    _REFUSED_SEEN.clear()
