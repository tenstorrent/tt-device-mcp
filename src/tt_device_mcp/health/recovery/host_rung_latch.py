# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The host-rung-in-flight latch (spec 04 I34, I35).

A host rung's fire returns as soon as the action is queued: ``systemctl reboot`` hands the job to
PID 1 and exits 0, and a BMC power-cycle command returns once the BMC took it. The box then shuts
down over the next seconds to minutes while this process still runs. Anything that resets the device
in that window (the hold watchdog's next forced pass, the gate, an operator reset) races the shutdown:
a parent-bridge reset and a PCI rescan once ran in the same second the reboot was queued.

So a fire that returned normally latches here, process-wide, and every reset entry point refuses
while it is set. The latch is kept on disk with this boot's id, so a broker restart in the same boot
keeps refusing, and the first look after a reboot sees a new boot id and ends it (the request was
carried out). One that never happens (the hand-off was lost, the cycle was declined, the host stayed
up) must not hold the device forever: the latch has a deadline, ``TT_DEVICE_MCP_HOST_RUNG_LATCH_SEC``
(default 1200 s). At the deadline, still on the same boot, the broker that fired it re-requests the
same action once; past the deadline of that second request the latch ends and the bounded escalation
goes on. Neither the latch nor its end releases the device: only a passing health check does."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from tt_device_mcp.health.evidence import health_dir, health_event

HOST_RUNG_LATCH_SEC_DEFAULT = 1200
STATE_FILE = "host_rung_latch.json"
# A latch whose re-request is owned by a live deadline task in this process waits for that task, so no
# reset slips in between the deadline and the re-request. Should the task never run, the latch still
# ends at this many deadlines.
PENDING_REREQUEST_CAP = 2

# {"action": "reboot" | "power-cycle", "reason": str, "at": epoch seconds, "boot_id": str,
#  "attempt": 1 | 2}, or None. In memory only: "rerequest" (a deadline task owns the re-request).
_LATCH: Optional[dict] = None
_LOADED = False
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


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _state_path() -> Path:
    return health_dir() / STATE_FILE


def _save() -> None:
    """Atomic. A latch that cannot be written still holds in memory for this process."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in (_LATCH or {}).items() if k != "rerequest"}))
        os.replace(tmp, path)
    except OSError:
        pass


def _drop() -> None:
    global _LATCH
    _LATCH = None
    _REFUSED_SEEN.clear()
    try:
        _state_path().unlink()
    except OSError:
        pass


def _load_once() -> None:
    """A broker restarted while a request was in flight picks the latch up again."""
    global _LATCH, _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        rec = json.loads(_state_path().read_text())
        if isinstance(rec, dict) and {"action", "at"} <= rec.keys():
            rec["at"] = float(rec["at"])
            _LATCH = rec
    except (OSError, ValueError, TypeError):
        pass


def latch(action: str, reason: str, *, now: Optional[float] = None, attempt: int = 1, rerequest: bool = False) -> None:
    """Record that a host rung's fire returned normally: the box is going down. ``rerequest`` says a
    deadline task in this process will re-request the action once if the box is still up."""
    global _LATCH, _LOADED
    _LOADED = True
    _LATCH = {
        "action": action,
        "reason": reason,
        "at": time.time() if now is None else now,
        "boot_id": _boot_id(),
        "attempt": attempt,
        "rerequest": rerequest,
    }
    _REFUSED_SEEN.clear()
    _save()
    health_event("host_rung_in_flight", action=action, reason=reason, attempt=attempt, latch_sec=int(latch_sec()))


def _rebooted(rec: dict) -> bool:
    then, now = rec.get("boot_id") or "", _boot_id()
    return bool(then and now and then != now)


def in_flight(*, now: Optional[float] = None) -> Optional[dict]:
    """The latched host rung, or None. Ends a latch whose request was carried out (a new boot), and
    one past its deadline, loudly: the box did not go down."""
    _load_once()
    if _LATCH is None:
        return None
    if _rebooted(_LATCH):
        health_event("host_rung_completed", action=_LATCH["action"], attempt=_LATCH.get("attempt", 1))
        _drop()
        return None
    age = (time.time() if now is None else now) - _LATCH["at"]
    limit = latch_sec() * (PENDING_REREQUEST_CAP if _LATCH.get("rerequest") else 1)
    if age < limit:
        return _LATCH
    health_event(
        "host_rung_latch_expired",
        action=_LATCH["action"],
        reason=_LATCH["reason"],
        attempt=_LATCH.get("attempt", 1),
        age_sec=int(age),
        next="bounded escalation",
    )
    _drop()
    return None


def expire(*, now: Optional[float] = None) -> Optional[dict]:
    """Called by the deadline task: the latch it owns, now ended, once its deadline has passed with the
    box still on the same boot; None when there is nothing to re-request (no latch, one the task does not
    own, a reboot, or a deadline not reached yet)."""
    rec = in_flight(now=now)
    if rec is None or not rec.get("rerequest"):
        return None
    age = (time.time() if now is None else now) - rec["at"]
    if age < latch_sec():
        return None
    health_event(
        "host_rung_latch_expired",
        action=rec["action"],
        reason=rec["reason"],
        attempt=rec.get("attempt", 1),
        age_sec=int(age),
        next="re-request",
    )
    _drop()
    return rec


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
    """Forget the latch in memory, file untouched. Tests only: in a broker it ends with a reboot or expires."""
    global _LATCH, _LOADED
    _LATCH = None
    _LOADED = True
    _REFUSED_SEEN.clear()
