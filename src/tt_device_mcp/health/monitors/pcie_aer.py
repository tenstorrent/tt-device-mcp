# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Per-chip PCIe error and link telemetry over a job and the post-job check (spec 03 I91).

A chip that drops during the fabric pass after a failed job leaves one question: did its link take
errors before the pass, during the job, or only once the pass started? The chip journal answers it
for the whole mesh at a decision; this answers it per chip, at four points: job start, job end, just
before the post-job check's probe pass (fabric included) and just after it. Two snapshots give one
delta per chip: the AER counts its endpoint and its root port added, and any change in either end's
link speed or width.

Read-only and sysfs only. The ``aer_dev_*`` counters are the kernel's own tallies, held in host
memory: reading them reaches no chip. The link status (``current_link_speed`` and
``current_link_width``) is a config-space read of the function, so a chip's own link is read only
while the chip is on the bus and not confirmed lost (spec 03 I66), and never while a reset step owns
the chips. A chip whose link read came back all-ones (speed ``Unknown`` or width 63) is not read
again in the same job cycle. The root port is the host's side of the link and is always read. No
``setpci``, no config-space write, no ``/dev/tenstorrent`` open.

Telemetry only: nothing here decides a verdict, a hold, a rung or a release.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from tt_device_mcp.health import chip_state
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.monitors.hostpci import LINK_DOWN_WIDTH
from tt_device_mcp.health.recovery import pcie_guard

AER_CLASSES = (("aer_dev_correctable", "cor"), ("aer_dev_nonfatal", "nonfatal"), ("aer_dev_fatal", "fatal"))

# Endpoints whose own link read all-ones in this job cycle: never read again until the next job
# start clears the set. Keyed by BDF.
_dead_links: set = set()
_lock = threading.Lock()


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _aer(dev: Path) -> dict:
    out = {}
    for attr, key in AER_CLASSES:
        p = dev / attr
        if p.exists():
            out[key] = pci._sum_aer(p)
    return out


def _link(dev: Path) -> dict:
    """``{"speed", "width"}`` from sysfs; ``down`` when the read came back all-ones. Width is not
    read after an ``Unknown`` speed: the link is down and a second read tells nothing new."""
    speed = _read(dev / "current_link_speed")
    if speed is not None and speed.startswith("Unknown"):
        return {"speed": speed, "down": True}
    width = _read(dev / "current_link_width")
    out = {"speed": speed, "width": width}
    if width == LINK_DOWN_WIDTH:
        out["down"] = True
    return out


def _tray_of(bus: int, groups: Optional[dict]) -> Optional[int]:
    if not groups:
        return None
    for tray, group in groups.items():
        if bus & 0xF0 == group:
            return tray
    return None


def snapshot(suspect=()) -> dict:
    """``{chip id: record}`` for every Tenstorrent function the kernel lists now. A chip off the bus
    has no node and is absent (the delta names it ``gone``). ``suspect`` are chip ids whose last
    sampler read was all-ones: their own link is not read, as for a lost chip. Never raises: {} when
    sysfs cannot be read."""
    try:
        eps = pcie_guard.tt_endpoints()
    except Exception:  # noqa: BLE001 - telemetry never fails the caller
        eps = None
    if not eps:
        return {}
    try:
        groups = pcie_guard.tray_bus_groups(eps)
    except Exception:  # noqa: BLE001
        groups = None
    root = pcie_guard._pci_dir()
    resetting = chip_state.op_running()
    suspect = {str(c) for c in suspect or ()}
    out: dict = {}
    for bdf, ep in sorted(eps.items()):
        cid = ep.chip if ep.chip is not None else bdf
        dev = root / bdf
        rec: dict = {"bdf": bdf, "root_port": ep.root_port, "tray": _tray_of(ep.bus, groups)}
        rec["aer"] = _aer(dev)
        with _lock:
            dead = bdf in _dead_links
        if ep.chip is not None and (chip_state.is_lost(ep.chip) or chip_state.is_fenced(ep.chip)):
            rec["link_skipped"] = "lost"
        elif str(cid) in suspect:
            rec["link_skipped"] = "all_ones"
        elif resetting:
            rec["link_skipped"] = "reset"
        elif dead:
            rec["link_skipped"] = "all_ones"
        else:
            link = _link(dev)
            rec["link"] = link
            if link.get("down"):
                with _lock:
                    _dead_links.add(bdf)
        if ep.root_port:
            port = root / ep.root_port
            rec["rp_aer"] = _aer(port)
            rec["rp_link"] = _link(port)
        out[str(cid)] = rec
    return out


def new_cycle() -> None:
    """A job is starting: endpoints that read all-ones in the last cycle may be read again (a reset
    or re-power has run since, or the chip is lost and gated by chip_state anyway)."""
    with _lock:
        _dead_links.clear()


def _aer_delta(before: dict, after: dict) -> dict:
    """Per class, what the counter added. A counter that went down was reset (the function was
    re-enumerated): its new value is what it added. A counter unreadable at either end is left out."""
    out = {}
    for _attr, key in AER_CLASSES:
        a, b = before.get(key), after.get(key)
        if not isinstance(b, int):
            continue
        if not isinstance(a, int):
            a = 0 if key not in before else None
        if a is None:
            continue
        d = b - a if b >= a else b
        if d:
            out[key] = d
    return out


def _link_key(link: Optional[dict]) -> Optional[tuple]:
    if not link:
        return None
    return (link.get("speed"), link.get("width"), bool(link.get("down")))


def delta(before: dict, after: dict) -> list:
    """The chips with something to report between two snapshots, sorted by chip id: a non-zero AER
    delta at the endpoint or the root port, a link that changed at either end (only where both
    snapshots read it), or a chip that left or joined the bus. Each entry names the chip, its BDF,
    root port and tray."""
    rows = []
    ids = set(before) | set(after)

    def _order(c):
        return (0, int(c)) if str(c).isdigit() else (1, str(c))

    for cid in sorted(ids, key=_order):
        a, b = before.get(cid), after.get(cid)
        ref = b or a
        row: dict = {"chip": int(cid) if str(cid).isdigit() else cid}
        for k in ("bdf", "root_port", "tray"):
            row[k] = ref.get(k)
        if a is None:
            row["joined"] = True
        elif b is None:
            row["gone"] = True
        else:
            ep = _aer_delta(a.get("aer") or {}, b.get("aer") or {})
            rp = _aer_delta(a.get("rp_aer") or {}, b.get("rp_aer") or {})
            if ep:
                row["aer"] = ep
            if rp:
                row["rp_aer"] = rp
            for key in ("link", "rp_link"):
                la, lb = _link_key(a.get(key)), _link_key(b.get(key))
                if la is not None and lb is not None and la != lb:
                    row[key] = {"before": a[key], "after": b[key]}
            skipped = b.get("link_skipped")
            if skipped and len(row) > 4:
                row["link_skipped"] = skipped
        if len(row) > 4:
            rows.append(row)
    return rows


def totals(rows: list) -> dict:
    """Summed AER deltas over ``rows``, endpoint and root port apart, for a one-line summary."""
    out = {"chips": len(rows)}
    for side in ("aer", "rp_aer"):
        acc: dict = {}
        for r in rows:
            for k, v in (r.get(side) or {}).items():
                acc[k] = acc.get(k, 0) + v
        if acc:
            out[side] = acc
    return out
