# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The post-reset eth link-count check's durable state (spec 03 I64, spec 04 I57).

A reset's verify proves enumeration, ARC and a fabric traffic pass; the traffic pass can still
pass on a mesh that came back with fewer eth links than it had (the validator tests the links it
finds). The built-in eth probe's count is the only place such a loss shows, so the post-reset
verify compares it with the host's link high-water mark. Two files hold what that comparison
needs across restarts and reboots:

* ``eth_link_deficit.json`` — the ACCEPTED deficit: a link count below the high-water mark that
  is known and tolerated (a cable fault awaiting repair). Only two writers: the operator
  (``tt-device-mcp eth-links accept``) and the broker itself once a power-cycle rung it fired
  came back with the same deficit. A verify at or above its ``links`` passes, loudly; anything
  below fails.
* ``eth_link_deficit_pending.json`` — a deficit seen but not yet accepted: the boot it was first
  seen in and whether its one reset rung has been spent. It bounds a new deficit to one reset rung
  plus one power cycle before the broker accepts it, so a known cable fault never loops resets.

Neither file ever lowers the high-water mark: clearing the accepted record (operator, or a verify
that measures the full mark again) restores the full bar.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from tt_device_mcp.health.evidence import health_dir

DEFICIT_FILE = "eth_link_deficit.json"
PENDING_FILE = "eth_link_deficit_pending.json"
# The broker's own auto-recovery ledger (recovery.base.AUTO_RECOVERY_LEDGER); read-only here.
_LEDGER_FILE = "auto_recovery.jsonl"


def _read(name: str) -> Optional[dict]:
    try:
        data = json.loads((health_dir() / name).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        data["links"] = int(data["links"])
    except (KeyError, TypeError, ValueError):
        return None
    return data


def _write(name: str, data: dict) -> None:
    path = health_dir() / name
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _remove(name: str) -> bool:
    try:
        (health_dir() / name).unlink()
        return True
    except FileNotFoundError:
        return False


def read_accepted() -> Optional[dict]:
    """The accepted deficit record, or None. A torn or unreadable record reads as none: the full
    high-water bar applies, which fails closed (held) rather than tolerating an unknown loss."""
    return _read(DEFICIT_FILE)


def write_accepted(links: int, high_water: int, source: str, ports: Optional[list] = None) -> dict:
    rec = {"links": int(links), "high_water": int(high_water), "source": source, "at": time.time()}
    if ports:
        rec["ports"] = list(ports)
    _write(DEFICIT_FILE, rec)
    return rec


def clear_accepted() -> bool:
    return _remove(DEFICIT_FILE)


def read_pending() -> Optional[dict]:
    return _read(PENDING_FILE)


def write_pending(links: int, high_water: int, boot_id: str, *, reset_spent: bool = False) -> dict:
    rec = {
        "links": int(links),
        "high_water": int(high_water),
        "boot_id": boot_id,
        "at": time.time(),
        "reset_spent": bool(reset_spent),
    }
    _write(PENDING_FILE, rec)
    return rec


def clear_pending() -> bool:
    return _remove(PENDING_FILE)


def reset_spent() -> bool:
    """Whether a pending (unaccepted) deficit has already had its one reset rung: the ladder then
    goes straight to its host rungs instead of resetting again."""
    rec = read_pending()
    return bool(rec and rec.get("reset_spent"))


def mark_reset_spent() -> None:
    rec = read_pending()
    if rec and not rec.get("reset_spent"):
        rec["reset_spent"] = True
        _write(PENDING_FILE, rec)


def power_cycled_since(pending: dict, current_boot_id: str) -> bool:
    """Whether a broker-fired power cycle separates the boot the pending deficit was first seen in
    from this one: the ledger's last escalation is a ``power-cycle`` stamped with the pending
    deficit's boot (the broker stamps the boot that is ending), and this is a different boot. A
    warm reboot, a manual reboot or an unreadable ledger is not that proof."""
    seen = str(pending.get("boot_id") or "")
    if not seen or not current_boot_id or seen == current_boot_id:
        return False
    try:
        lines = Path(health_dir() / _LEDGER_FILE).read_text().splitlines()
    except OSError:
        return False
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            last = json.loads(ln)
        except ValueError:
            continue
        return isinstance(last, dict) and last.get("action") == "power-cycle" and last.get("boot_id") == seen
    return False
