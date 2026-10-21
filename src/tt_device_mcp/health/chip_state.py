# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-chip ``lost`` / ``recovering`` state: the gate in front of every broker read of a chip.

The KMD's per-chip sysfs telemetry (``tt_heartbeat``, ``tt_aiclk`` ...) is not a cached value: each
read of one is a PCIe read of the chip's BAR. A read to a chip that has left the bus, or hung, waits
for the root port's completion timeout and comes back all-ones; that wait stalls a CPU core, and
enough of them escalate to a fatal machine check that resets the host. So once the broker has
confirmed a chip lost, nothing of the broker's keeps reading it on a poll clock (spec 03 I66):

* ``lost`` — confirmed off the bus: all-ones on two consecutive sampler reads (marked before the
  sampler isolates the chip from the kernel). Every gated read of a lost chip (:func:`may_read`) is refused and reads
  as all-ones, which is what its last real read said, so every count and rung decides exactly as on
  a real read. A lost chip is read again only when (a) its sysfs node was re-created (a remove and
  rescan, a tray re-power or a hotplug: a new node is a new endpoint, so the read is safe to try),
  (b) a broker reset or recovery step on it has just ended (one read per :data:`GRACE_REPROBE_SEC`
  for :data:`GRACE_SEC`, so the post-reset health check reads the chip for real), or (c) as a
  backstop, once per :data:`LOST_REPROBE_SEC`. A real read that is not all-ones clears ``lost``.
* ``recovering`` — a broker reset or recovery step is running on the chips (resets are mesh- or
  tray-wide, so it covers every chip). The pollers (telemetry sampler, /health) read nothing then;
  only the step's own reads run, ungated, exactly as before.

The state is published to :func:`state_path` (JSON, rewritten atomically on every change) and in
/health, so other readers on the host can stay off a chip the broker knows is lost or under reset.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

STATE_FILE_DEFAULT = "/run/tt-device-broker/chip-state.json"
STATE_VERSION = 1

ALL_ONES = 0xFFFFFFFF


def _env_float(name: str, default: float, floor: float) -> float:
    try:
        return max(floor, float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


# Backstop re-read of a lost chip whose node never changed: a chip that came back on its own without
# being re-enumerated. Three sampler periods; it bounds how late such a return is seen.
LOST_REPROBE_SEC = _env_float("TT_DEVICE_MCP_LOST_REPROBE_SEC", 30.0, 5.0)
# After a reset or recovery step ends, the chips it touched are read again at most once per
# GRACE_REPROBE_SEC for GRACE_SEC: covers the post-reset health check and the ladder's verify.
GRACE_SEC = 120.0
GRACE_REPROBE_SEC = 1.0
# A decision read (:func:`may_read` ``probe``, :func:`reprobe_due`) reads a lost chip whose last real
# read is at least this old: a check that makes several decisions reads a dead chip once, not once each.
PROBE_MIN_SEC = 1.0

_lock = threading.RLock()
# idx -> {"since": iso, "reason": str, "node": identity or None, "next": monotonic}
_lost: dict[str, dict] = {}
# owner -> iso since. "op:<id>" is a RecoveryMechanism with a reset in flight; "scope" is a reset
# scope still cycling the chips after the broker stopped waiting for it.
_recovering: dict[str, str] = {}
_grace_until = 0.0
_last_written: Optional[str] = None


def _sysfs_indices() -> list:
    """Chip indices with a KMD sysfs node: a directory listing, no chip read."""
    from tt_device_mcp.health.monitors import pci

    try:
        names = [e.name.rsplit("!", 1)[-1] for e in pci.SYSFS_CLASS_DIR.iterdir()]
    except OSError:
        return []
    return [n for n in names if n.isdigit()]


def chip_nodes(ids: Iterable) -> dict:
    """Index -> the chip's KMD sysfs class node, for :func:`mark_lost`."""
    from tt_device_mcp.health.monitors import pci

    return {str(i): pci.SYSFS_CLASS_DIR / f"tenstorrent!{i}" for i in ids}


_present: Callable[[], list] = _sysfs_indices


def _no_baseline() -> int:
    return 0


_expected: Callable[[], int] = _no_baseline


def reset() -> None:
    """Forget everything (tests; a fresh process starts here)."""
    global _grace_until, _last_written, _present, _expected
    with _lock:
        _lost.clear()
        _recovering.clear()
        _grace_until = 0.0
        _last_written = None
        _present = _sysfs_indices
        _expected = _no_baseline


def configure(*, present: Optional[Callable[[], list]] = None, expected: Optional[Callable[[], int]] = None) -> None:
    """Late-bound sources for the published file: the chip indices with a sysfs node (a directory
    listing, no chip read) and the host's chip baseline."""
    global _present, _expected
    if present is not None:
        _present = present
    if expected is not None:
        _expected = expected


def state_path() -> Path:
    """Where the state is published. Late-bound; defaults to a sibling of the device-op inhibit file,
    so a per-user daemon that redirects that file moves this one too (spec 06)."""
    explicit = os.environ.get("TT_DEVICE_MCP_CHIP_STATE_FILE", "").strip()
    if explicit:
        return Path(explicit)
    lock = os.environ.get("TT_DEVICE_MCP_DEVICE_OP_LOCK", "").strip()
    return Path(lock).with_name("chip-state.json") if lock else Path(STATE_FILE_DEFAULT)


def node_identity(node: Path) -> Optional[tuple]:
    """Identity of a chip's sysfs node without reading the chip: the inode of the device directory the
    class link resolves to, and its PCI address. A remove and re-probe makes a new node, so a new
    identity is a new endpoint. None when the node is gone."""
    try:
        st = os.stat(node)
        return (st.st_dev, st.st_ino, os.path.basename(os.path.realpath(node / "device")))
    except OSError:
        return None


# --- recovering ------------------------------------------------------------------------------


def set_recovering(owner: str, on: bool) -> None:
    """A reset or recovery step started (``on``) or ended on the chips. The end opens the post-reset
    grace window, so the step's verify reads every lost chip for real."""
    global _grace_until
    changed = False
    with _lock:
        if on and owner not in _recovering:
            _recovering[owner] = datetime.now().isoformat(timespec="seconds")
            changed = True
        elif not on and owner in _recovering:
            del _recovering[owner]
            _grace_until = time.monotonic() + GRACE_SEC
            for rec in _lost.values():
                rec["next"] = 0.0
            changed = True
    if changed:
        publish()


def recovering() -> bool:
    """Whether a reset or recovery step is running on the chips. Pollers read nothing while it is."""
    with _lock:
        return bool(_recovering)


def op_running() -> bool:
    """Whether this broker's own reset or recovery step is running: its reads are its own and ungated."""
    with _lock:
        return any(k.startswith("op:") for k in _recovering)


# --- lost ------------------------------------------------------------------------------------


def mark_lost(ids: Iterable, reason: str, nodes: Optional[dict] = None) -> list:
    """Mark chips confirmed off the bus. ``nodes`` maps an index to its sysfs node, for its identity.
    Returns the newly marked ones."""
    fresh = []
    now = time.monotonic()
    with _lock:
        for i in ids:
            idx = str(i)
            if idx in _lost:
                continue
            node = nodes.get(idx) if nodes else None
            _lost[idx] = {
                "since": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "node": node_identity(node) if node is not None else None,
                "next": now + LOST_REPROBE_SEC,
                "read": now,
            }
            fresh.append(idx)
    if fresh:
        _event("chip_marked_lost", chips=fresh, reason=reason)
        publish()
    return fresh


def clear_lost(idx, why: str) -> bool:
    with _lock:
        rec = _lost.pop(str(idx), None)
    if rec is None:
        return False
    _event("chip_lost_cleared", chip=str(idx), why=why, lost_since=rec["since"])
    publish()
    return True


def lost_chips() -> list:
    with _lock:
        return sorted(_lost, key=int)


def is_lost(idx) -> bool:
    with _lock:
        return str(idx) in _lost


def may_read(idx, node: Path, *, probe: bool = False) -> bool:
    """Whether a broker read of chip ``idx`` (sysfs node ``node``) may go to the chip now. True for a
    chip not marked lost, and for every read while this broker's own reset step runs. For a lost chip:
    True once its node was re-created (and the mark is cleared), or when a re-read is due (one per
    :data:`LOST_REPROBE_SEC`, one per :data:`GRACE_REPROBE_SEC` right after a reset step); False
    otherwise, and the caller reports the chip as all-ones without reading it.

    ``probe`` is a decision read (see :func:`reprobe_due`), not a poll: the re-read is due once the
    last real read is :data:`PROBE_MIN_SEC` old, so a chip that answers again on the same node is seen
    by the check that judges it and never judged off the bus on a value nobody read. It restarts the
    reprobe clock."""
    idx = str(idx)
    with _lock:
        rec = _lost.get(idx)
        if rec is None:
            return True
        if op_running():
            return True
        ident = node_identity(node)
        if ident is not None and rec["node"] is not None and ident != rec["node"]:
            renewed = True
        else:
            renewed = False
            now = time.monotonic()
            due = min(rec["next"], rec["read"] + PROBE_MIN_SEC) if probe else rec["next"]
            if now < due:
                return False
            rec["next"] = now + (GRACE_REPROBE_SEC if now < _grace_until else LOST_REPROBE_SEC)
            rec["read"] = now
            if rec["node"] is None:
                rec["node"] = ident
    if renewed:
        clear_lost(idx, "sysfs node re-created")
    return True


def reprobe_due(ids: Optional[Iterable] = None) -> None:
    """Make each lost chip's next read real for a decision read through a plain reader (spec 03 I66):
    the reset gate's and the recovery ladder's off-bus counts, the DOWN off-bus recheck, the
    hold-deadline escalation, and job admission, exec and a Slurm step's verdict. A read already due
    stays due; one whose last real read is younger than :data:`PROBE_MIN_SEC` stays gated, so
    back-to-back decisions read a dead chip once. ``ids`` limits it to those chips (the sampler's
    pending FAST_CONFIRM_SEC proof, I79)."""
    want = None if ids is None else {str(i) for i in ids}
    with _lock:
        for idx, rec in _lost.items():
            if want is None or idx in want:
                rec["next"] = min(rec["next"], rec["read"] + PROBE_MIN_SEC)


def observe(idx, value) -> None:
    """A real heartbeat read of a lost chip: a counter that is not all-ones means it is back."""
    if is_lost(idx) and isinstance(value, int) and value != ALL_ONES:
        clear_lost(idx, "answering again")


# --- publishing ------------------------------------------------------------------------------


def snapshot() -> dict:
    """The published state: per chip ``lost`` and ``recovering``, from the in-memory marks plus a
    directory listing (a chip the baseline expects with no sysfs node is lost too). No chip read."""
    try:
        present = {str(i) for i in _present()}
    except Exception:  # noqa: BLE001 - a listing must never break the publisher
        present = set()
    try:
        expected = int(_expected() or 0)
    except Exception:  # noqa: BLE001
        expected = 0
    with _lock:
        rec_on = bool(_recovering)
        since = min(_recovering.values()) if _recovering else None
        marked = {k: dict(v) for k, v in _lost.items()}
    ids = sorted({str(i) for i in range(expected)} | present | set(marked), key=int)
    chips = {}
    for i in ids:
        if i in marked:
            chips[i] = {
                "lost": True,
                "recovering": rec_on,
                "lost_since": marked[i]["since"],
                "lost_reason": marked[i]["reason"],
            }
        elif i not in present:
            chips[i] = {"lost": True, "recovering": rec_on, "lost_since": None, "lost_reason": "not in sysfs"}
        else:
            chips[i] = {"lost": False, "recovering": rec_on}
    return {
        "version": STATE_VERSION,
        "pid": os.getpid(),
        "recovering": rec_on,
        "recovering_since": since,
        "lost": sorted((i for i, c in chips.items() if c["lost"]), key=int),
        "chips": chips,
    }


def publish() -> None:
    """Rewrite the state file when its content changed (atomic rename). Never raises."""
    global _last_written
    try:
        body = snapshot()
        text = json.dumps(body, sort_keys=True)
        with _lock:
            if text == _last_written:
                return
            _last_written = text
        body["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - publishing is advisory; the gate does not depend on it
        with _lock:
            _last_written = None


def _event(event: str, **fields) -> None:
    try:
        from tt_device_mcp.health.evidence import health_event

        health_event(event, **fields)
    except Exception:  # noqa: BLE001 - a journal failure must never break a read
        pass
