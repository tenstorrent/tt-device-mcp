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
  rescan, an external tray re-power or a hotplug: a new node is a new endpoint, so the read is safe to try),
  (b) a broker reset or recovery step on it has just ended (one read per :data:`GRACE_REPROBE_SEC`
  for :data:`GRACE_SEC`, so the post-reset health check reads the chip for real), or (c) by a decision
  read (:func:`reprobe_due`), only while its PCI remove is still to come. No poll clock re-reads a
  lost chip that is still attached, and once its remove failed or was skipped (:func:`seal`) only
  (a) and (b) read it: that chip goes to the hold's bounded recovery, never to a re-read loop. A real
  read that is not all-ones clears ``lost``.
* ``fenced`` — read as dead, not yet confirmed off the bus: the containment (spec 03 I92) fences a chip
  before its first step, and a sampler read that comes back all-ones fences that chip. Every
  in-process reader checks :func:`read_blocked` before each MMIO-backed attribute and reports a
  fenced chip all-ones without reading it, so the broker's own reads never hold up the chip's PCI
  remove (the kernel's remove waits for every in-flight sysfs read). A fenced chip is read again
  only under the probe rules of :func:`may_read` (a decision read, the sampler's confirm read), and
  the fence clears when a real read answers, its node is re-created or a broker reset step ends.
* ``held readers`` — the broker's own check readers an AER stop left SIGSTOPped while a chip it
  may still read is attached (spec 03 I92/I96). Their own timeout does not SIGKILL them (a kill
  closes their fds, and the driver's close path reaches the chip, I84) until the watch releases
  them or the hold's deadline passes (:func:`wait_reader_release`).
* ``recovering`` — a broker reset or recovery step is running on the chips (resets are mesh- or
  tray-wide, so it covers every chip). The pollers (telemetry sampler, /health) read nothing then;
  only the step's own reads run, ungated, exactly as before.

The state is published to :func:`state_path` (JSON, rewritten atomically on every change) and in
/health, so other readers on the host can stay off a chip the broker knows is lost or under reset.
"""

from __future__ import annotations

import asyncio
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


# No poll clock re-reads a lost chip whose node never changed (spec 03 I66): it is still attached, and
# each read is a BAR read of a chip that left the bus. Only a decision read (reprobe_due), a reset
# step's grace or a re-created node reads it.
NEVER = float("inf")
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
# idx -> {"since": iso, "reason": str, "node": identity or None, "next": monotonic, "read": monotonic}
_fenced: dict[str, dict] = {}
# idx -> [first wall ts, last wall ts, kind]: the earliest sign a chip was dying (an AER burst on its
# path, an all-ones read), for the containment record's first_evidence_ts (spec 03 I96).
_evidence: dict[str, list] = {}
# pid -> monotonic deadline: readers an AER stop left SIGSTOPped (spec 03 I92), see hold_readers.
_held: dict[int, float] = {}
# How often a reader's timeout path looks again whether its hold ended.
HOLD_POLL_SEC = 0.1
# Evidence older than this before the next sign starts a new episode.
EVIDENCE_GAP_SEC = 60.0
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
        _fenced.clear()
        _evidence.clear()
        _held.clear()
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
            # A reset step ends every fence: the post-reset check reads the chips for real.
            _fenced.clear()
            _evidence.clear()
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


def mark_lost(ids: Iterable, reason: str, nodes: Optional[dict] = None, deferred: bool = False) -> list:
    """Mark chips confirmed off the bus. ``nodes`` maps an index to its sysfs node, for its identity.
    With ``deferred`` (the dead-chip containment, before its PCI remove, spec 03 I99) the mark does no
    blocking I/O: the journal line goes through the detached writer and the state file is left to the
    caller's :func:`publish` after the remove. Returns the newly marked ones."""
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
                "next": NEVER,
                "read": now,
                "sealed": False,
            }
            fresh.append(idx)
    if fresh and deferred:
        _event_detached("chip_marked_lost", chips=fresh, reason=reason)
    elif fresh:
        _event("chip_marked_lost", chips=fresh, reason=reason)
        publish()
    return fresh


def clear_lost(idx, why: str) -> bool:
    with _lock:
        _fenced.pop(str(idx), None)
        rec = _lost.pop(str(idx), None)
    if rec is None:
        return False
    _event("chip_lost_cleared", chip=str(idx), why=why, lost_since=rec["since"])
    publish()
    return True


def seal(ids: Iterable, why: str) -> list:
    """Lost chips whose PCI remove failed, timed out or was skipped (PCI-wedged): still attached and
    MMIO-reachable. No decision read reads them again; only a re-created node or a broker reset step
    and its grace does (spec 03 I66). Returns the newly sealed ones."""
    fresh = []
    with _lock:
        for i in ids:
            rec = _lost.get(str(i))
            if rec is not None and not rec.get("sealed"):
                rec["sealed"] = True
                fresh.append(str(i))
    if fresh:
        _event("chip_lost_sealed", chips=fresh, why=why)
    return fresh


def unreadable_lost() -> list:
    """Lost chips a reader that opens the whole device (the aiclk-ceiling helper, the eth-heartbeat
    probe) must not reach now: still attached (the node is the one marked), outside this broker's
    own reset step and its grace. Node identity is a stat, no chip read."""
    with _lock:
        if op_running() or time.monotonic() < _grace_until:
            return []
        marked = {k: v.get("node") for k, v in _lost.items()}
    from tt_device_mcp.health.monitors import pci

    out = []
    for idx, ident in marked.items():
        now_ident = node_identity(pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}")
        if now_ident is not None and (ident is None or now_ident == ident):
            out.append(idx)
    return sorted(out, key=int)


def lost_chips() -> list:
    with _lock:
        return sorted(_lost, key=int)


def is_lost(idx) -> bool:
    with _lock:
        return str(idx) in _lost


# --- fenced ----------------------------------------------------------------------------------


def fence(ids: Iterable, reason: str, nodes: Optional[dict] = None) -> list:
    """Stop the broker's own MMIO reads of chips read as dead (spec 03 I92/I97). Fast on purpose: the
    containment calls it before its first step, so it writes no journal line and reads no chip (the
    node identity is a stat). Returns the newly fenced ones."""
    want = [str(i) for i in ids]
    with _lock:
        want = [i for i in want if i not in _fenced]
    if not want:
        return []
    paths = chip_nodes(want)
    if nodes:
        paths.update({str(k): v for k, v in nodes.items() if str(k) in paths})
    idents = {i: node_identity(paths[i]) for i in want}
    now = time.monotonic()
    fresh = []
    with _lock:
        for idx in want:
            if idx in _fenced:
                continue
            _fenced[idx] = {
                "since": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "node": idents[idx],
                "next": NEVER,
                "read": now,
            }
            fresh.append(idx)
    return fresh


def unfence(idx, why: str = "") -> bool:
    with _lock:
        return _fenced.pop(str(idx), None) is not None


def is_fenced(idx) -> bool:
    with _lock:
        return str(idx) in _fenced


def fenced_chips() -> list:
    with _lock:
        return sorted(_fenced, key=int)


def read_blocked(idx) -> bool:
    """Whether an in-process reader must skip chip ``idx``'s MMIO-backed attributes now: lost or
    fenced, outside this broker's own reset step. Checked before EACH attribute read, so a fence
    set while a reader is mid-chip stops its next read (spec 03 I97). No chip read."""
    idx = str(idx)
    with _lock:
        return (idx in _lost or idx in _fenced) and not op_running()


# --- first evidence ---------------------------------------------------------------------------


def note_evidence(ids: Iterable, kind: str, ts: Optional[float] = None) -> None:
    """A sign that chips are dying (``kind`` "aer_burst" or "all_ones") at wall time ``ts``. Keeps
    the earliest sign of the episode; a sign more than :data:`EVIDENCE_GAP_SEC` after the last one
    starts a new episode."""
    ts = time.time() if ts is None else float(ts)
    with _lock:
        for i in ids:
            idx = str(i)
            ev = _evidence.get(idx)
            if ev is None or ts - ev[1] > EVIDENCE_GAP_SEC:
                _evidence[idx] = [ts, ts, kind]
            else:
                if ts < ev[0]:
                    ev[0], ev[2] = ts, kind
                ev[1] = max(ev[1], ts)


def first_evidence(ids: Iterable) -> tuple:
    """(wall ts, kind) of the earliest sign of the current episode across ``ids``; (None, None) when
    none was seen."""
    best = (None, None)
    with _lock:
        for i in ids:
            ev = _evidence.get(str(i))
            if ev is not None and (best[0] is None or ev[0] < best[0]):
                best = (ev[0], ev[2])
    return best


# --- held readers ----------------------------------------------------------------------------


def hold_readers(pids: Iterable, until: float) -> None:
    """Spec 03 I92: the AER watch left these broker check readers (process group ids) SIGSTOPped
    while a chip they may read is still attached. Until :func:`release_readers` or the monotonic
    ``until`` (the watch's stated bound), their own timeout path waits instead of SIGKILLing them."""
    with _lock:
        for pid in pids:
            if isinstance(pid, int):
                _held[pid] = max(_held.get(pid, 0.0), float(until))


def release_readers(pids: Optional[Iterable] = None) -> None:
    """The watch settled the stop (detached, contained, answering, the readers gone, or a reset step
    owns the chips): ``pids``, or every held reader, may be killed again."""
    with _lock:
        if pids is None:
            _held.clear()
        for pid in pids or ():
            _held.pop(pid, None)


def reader_held(pid) -> bool:
    """Whether ``pid``'s timeout kill must wait now: held, before its deadline, and no broker reset
    step running (the step owns the chips and ends their readers itself, so it never waits on this)."""
    with _lock:
        until = _held.get(pid)
        return until is not None and time.monotonic() < until and not op_running()


def _hold_over(pid, waited: float) -> bool:
    if reader_held(pid):
        return False
    with _lock:
        expired = _held.pop(pid, None) is not None and not op_running()
    if expired:
        _event("check_reader_hold_expired", pid=pid, waited_s=round(waited, 3))
    return True


def wait_reader_release(pid) -> None:
    """Block until ``pid`` is no longer held (:func:`reader_held`), at most to its hold's deadline.
    For a reader's own timeout path, right before its SIGKILL; a hold that expires is journaled."""
    t0 = time.monotonic()
    while not _hold_over(pid, time.monotonic() - t0):
        time.sleep(HOLD_POLL_SEC)


async def await_reader_release(pid) -> None:
    """:func:`wait_reader_release` for the event loop."""
    t0 = time.monotonic()
    while not _hold_over(pid, time.monotonic() - t0):
        await asyncio.sleep(HOLD_POLL_SEC)


def may_read(idx, node: Path, *, probe: bool = False) -> bool:
    """Whether a broker read of chip ``idx`` (sysfs node ``node``) may go to the chip now. True for a
    chip not marked lost, and for every read while this broker's own reset step runs. For a lost chip:
    True once its node was re-created (and the mark is cleared), or when a re-read is due (one per
    :data:`GRACE_REPROBE_SEC` right after a reset step, or one a decision read asked for while the
    chip's remove is still to come); False otherwise, and the caller reports the chip as all-ones
    without reading it. No poll clock makes a re-read due, and a sealed chip (:func:`seal`) is read
    only in the grace.

    ``probe`` is a decision read (see :func:`reprobe_due`), not a poll: the re-read is due once the
    last real read is :data:`PROBE_MIN_SEC` old, so a chip that answers again on the same node is seen
    by the check that judges it and never judged off the bus on a value nobody read. It restarts the
    reprobe clock."""
    idx = str(idx)
    with _lock:
        rec = _lost.get(idx)
        if rec is None:
            frec = _fenced.get(idx)
            if frec is None or op_running():
                return True
            # Fenced, not yet lost: the same probe rules, without a grace (a reset step ended the fence).
            ident = node_identity(node)
            if ident is not None and ident != frec["node"]:
                del _fenced[idx]
                return True
            now = time.monotonic()
            due = min(frec["next"], frec["read"] + PROBE_MIN_SEC) if probe else frec["next"]
            if now < due:
                return False
            frec["next"] = NEVER
            frec["read"] = now
            return True
        if op_running():
            return True
        ident = node_identity(node)
        # A node where the mark found none is a re-created one too (removed, then rescanned).
        if ident is not None and ident != rec["node"]:
            renewed = True
        else:
            renewed = False
            now = time.monotonic()
            grace = now < _grace_until
            if rec.get("sealed") and not grace:
                return False
            due = min(rec["next"], rec["read"] + PROBE_MIN_SEC) if probe else rec["next"]
            if now < due:
                return False
            rec["next"] = now + GRACE_REPROBE_SEC if grace else NEVER
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
        for idx, rec in list(_lost.items()) + [(k, v) for k, v in _fenced.items() if k not in _lost]:
            if want is None or idx in want:
                rec["next"] = min(rec["next"], rec["read"] + PROBE_MIN_SEC)


def observe(idx, value) -> None:
    """A real heartbeat read of a lost or fenced chip: a counter that is not all-ones means it is back."""
    if isinstance(value, int) and value != ALL_ONES:
        if is_lost(idx):
            clear_lost(idx, "answering again")
        else:
            unfence(idx, "answering again")


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


def _event_detached(event: str, **fields) -> None:
    try:
        from tt_device_mcp.health.evidence import health_event_detached

        health_event_detached(event, **fields)
    except Exception:  # noqa: BLE001 - a journal failure must never break a containment
        pass


def _event(event: str, **fields) -> None:
    try:
        from tt_device_mcp.health.evidence import health_event

        health_event(event, **fields)
    except Exception:  # noqa: BLE001 - a journal failure must never break a read
        pass
