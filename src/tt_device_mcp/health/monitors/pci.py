# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Free PCIe/sysfs chip probes: snapshots, samples, isolation, and the chip journal.

Everything here is a sysfs read (or, for isolate_chip, a bus-remove write) — never a UMD
call that could touch a wedged chip. tt-smi does a full UMD TopologyDiscovery on every
call, and a device init aimed at a wedged chip is what walks the root complex toward a
fatal PCIe error and an ungraceful host reset. Reading sysfs cannot: the KMD already
holds the value, so a probe here is a file read and nothing reaches the chip.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from tt_device_mcp.health import chip_state, evidence

SYSFS_CLASS_DIR = Path(os.environ.get("TT_DEVICE_MCP_SYSFS_DIR", "/sys/class/tenstorrent"))

CHIPS_FILE = "chips.jsonl"

# Per-chip attributes worth keeping. All free: the KMD already holds these values, so
# reading them is a file read and nothing reaches the chip.
#   tt_heartbeat        ARC liveness counter — the raw value, so a stall is provable later
#   tt_therm_trip_count thermal trips: the most likely physical cause of a wedge
#   tt_arcclk/tt_aiclk  a chip throttling or stuck off-clock shows up here first
#   tt_fw_bundle_ver    firmware actually running, per chip — drift is real and invisible
#   tt_asic_id          identifies the silicon across reseats and reboots
_CHIP_ATTRS = (
    "tt_heartbeat",
    "tt_therm_trip_count",
    "tt_arcclk",
    "tt_aiclk",
    "tt_axiclk",
    "tt_fw_bundle_ver",
    "tt_asic_id",
    "tt_card_type",
)

PCI_DEVICES_DIR = Path(os.environ.get("TT_DEVICE_MCP_PCI_DIR", "/sys/bus/pci/devices"))


def _sum_aer(path: Path) -> Optional[int]:
    """Total the counters in an aer_dev_* file ('RxErr 0\\nBadTLP 0\\n...').

    None when the file is present but could not be read: a 0 there reads as a clean bus —
    the very signal that precedes a wedge-to-reboot — when in fact nothing was measured.
    None keeps "unknown" apart from "measured zero" so the decision log cannot mask one as
    the other. Matches chip_sample()'s convention of None for an unreadable attribute.

    The kernel's own ``TOTAL_ERR_*`` line wins where it is present, which on real sysfs is
    always: summing every line counts that total a second time and reports exactly double the
    errors that occurred. Healthy hardware hid it — zero doubled is zero — so it would first
    have shown the moment the counters started moving, which is the one moment the number is
    load-bearing. Falling back to a sum keeps files that carry no total (older kernels, and
    every fixture written against this function) reading the same as before."""
    total = 0
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            if parts[0].startswith("TOTAL_ERR"):
                return int(parts[1])
            total += int(parts[1])
    except (OSError, ValueError):
        return None
    return total


def chip_snapshot() -> dict:
    """Everything the host knows about the chips. The telemetry attributes and link config are
    PCIe reads of each chip, so a chip confirmed lost is listed without them (spec 03 I66).

    This is the record that makes a post-mortem possible. When a chip wedges, the
    question is always "what was it doing beforehand" — was it hot, was it throttling,
    was its PCIe link taking errors, was it running different firmware than its
    neighbours. None of that is recoverable after the fact, and all of it is free right
    now, so it gets written down on every health decision.

    The PCIe error counters matter most: the documented path from a wedged chip to an
    ungraceful host reboot runs through the root complex escalating accumulated errors
    to fatal. If that is what is happening here, these counters are where it shows, and
    without them the theory can be neither proved nor dropped.
    """
    chips: dict[str, dict] = {}
    try:
        entries = sorted(SYSFS_CLASS_DIR.iterdir())
    except OSError:
        entries = []
    for ent in entries:
        idx = ent.name.rsplit("!", 1)[-1]
        if not idx.isdigit():
            continue
        rec: dict = {}
        # A chip confirmed lost is not read (spec 03 I66): its attributes and its link config are
        # PCIe reads that would stall on it. The AER counters below are kernel memory and stay.
        lost = chip_state.is_lost(idx) and not chip_state.op_running()
        if lost:
            rec["lost"] = True
        for attr in () if lost else _CHIP_ATTRS:
            try:
                val = (ent / attr).read_text().strip()
            except OSError:
                continue
            rec[attr] = int(val) if val.isdigit() else val
        # The PCI endpoint behind this chip: link health + accumulated bus errors.
        try:
            pci = (ent / "device").resolve()
            rec["pci"] = pci.name
            for f, key in (
                ()
                if lost
                else (
                    ("current_link_width", "link_width"),
                    ("max_link_width", "link_width_max"),
                    ("current_link_speed", "link_speed"),
                )
            ):
                try:
                    rec[key] = (pci / f).read_text().strip()
                except OSError:
                    pass
            for f, key in (
                ("aer_dev_correctable", "aer_correctable"),
                ("aer_dev_nonfatal", "aer_nonfatal"),
                ("aer_dev_fatal", "aer_fatal"),
            ):
                p = pci / f
                if p.exists():
                    rec[key] = _sum_aer(p)
        except OSError:
            pass
        chips[idx] = rec
    return chips


def chip_snapshot_event(event: str, **fields) -> dict:
    """Write a full chip snapshot to its own journal, keyed to what triggered it.

    Kept separate from health_events.jsonl on purpose: 32 chips of telemetry per record
    would bury the decision log that makes the broker's behaviour auditable at a glance.
    The two are joined on the timestamp.
    """
    chips = chip_snapshot()
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "chips": chips,
        **fields,
    }
    evidence._append_durable(evidence.HEALTH_DIR / CHIPS_FILE, rec, evidence.MAX_CHIPS_BYTES)
    return chips


def aer_totals(chips: dict) -> dict:
    """Bus-error totals across every chip — the one number worth putting in the decision
    log itself, so a rising trend is visible without opening the chip journal.

    A counter present but unreadable (_sum_aer returned None) is UNKNOWN, not zero: it is
    tallied under ``unreadable`` rather than summed as a clean 0, so an unreadable bus cannot
    hide as a healthy one. A counter simply absent (old kernel with no AER sysfs) stays silent."""
    out = {"correctable": 0, "nonfatal": 0, "fatal": 0}
    unreadable = 0
    for rec in chips.values():
        for short, key in (("correctable", "aer_correctable"), ("nonfatal", "aer_nonfatal"), ("fatal", "aer_fatal")):
            if key not in rec:
                continue  # AER not exposed for this chip — not the same as unreadable
            v = rec[key]
            if v is None:
                unreadable += 1
            else:
                out[short] += v
    if unreadable:
        out["unreadable"] = unreadable
    return out


# Only the values that MOVE. A trace's job is to show the run-up to a wedge — a chip
# heating, throttling, its ARC stalling, its bus starting to take errors — and carrying
# the static fields (asic id, card type, firmware) on every sample would bloat the ring
# for no information. The static picture is in chips.jsonl already.
_VOLATILE = ("tt_heartbeat", "tt_aiclk", "tt_arcclk", "tt_therm_trip_count")

SAMPLE_INTERVAL_SEC = float(os.environ.get("TT_DEVICE_MCP_SAMPLE_INTERVAL_SEC", "10"))
# 20 minutes of run-up at the default interval. The wedge is what we care about, not the
# hour before it; the ring keeps the recent past and discards the rest.
SAMPLE_RING_SIZE = int(os.environ.get("TT_DEVICE_MCP_SAMPLE_RING", "120"))


def chip_sample() -> dict:
    """One compact sample of every chip's volatile state.

    Compact on purpose: this runs every few seconds for the life of a job, so it carries
    the moving values only, as a flat list per chip — [heartbeat, aiclk, arcclk,
    therm_trips, aer_correctable, aer_fatal].

    The four telemetry values are PCIe reads of the chip. A chip confirmed lost reads as
    [ALL_ONES, None, None, None, ...] without touching it; when its re-read is due
    (:func:`chip_state.may_read`) only its heartbeat is read, once (spec 03 I66).
    """
    out: dict = {}
    try:
        entries = sorted(SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return out
    for ent in entries:
        idx = ent.name.rsplit("!", 1)[-1]
        if not idx.isdigit():
            continue
        row = []
        lost = chip_state.is_lost(idx)
        attrs = _VOLATILE
        if lost and not chip_state.may_read(idx, ent):
            attrs = ()
            row = [chip_state.ALL_ONES, None, None, None]
        elif lost and chip_state.is_lost(idx):
            attrs = _VOLATILE[:1]
        for attr in attrs:
            try:
                v = (ent / attr).read_text().strip()
                row.append(int(v) if v.isdigit() else v)
            except OSError:
                row.append(None)
        if attrs == _VOLATILE[:1]:
            chip_state.observe(idx, row[0])
            row.extend([None, None, None])
        try:
            pci = (ent / "device").resolve()
            row.append(_sum_aer(pci / "aer_dev_correctable"))
            row.append(_sum_aer(pci / "aer_dev_fatal"))
        except OSError:
            row.extend([None, None])
        out[idx] = row
    return out


SAMPLE_FIELDS = list(_VOLATILE) + ["aer_correctable", "aer_fatal"]

_BDF = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def _uncorrectable_aer(dev: Path) -> Optional[int]:
    """Uncorrectable (non-fatal + fatal) AER errors the kernel has counted on one PCI function: its
    own ``aer_dev_nonfatal``/``aer_dev_fatal`` and, on a root port, the ``aer_rootport_total_err_*``
    messages it received from below, plus the one correctable row that is not link noise
    (``NonFatalErr`` in ``aer_dev_correctable``: an Advisory Non-Fatal error, i.e. an uncorrectable
    error the device downgraded, such as a completion timeout or an unsupported request). None when
    no counter could be read. The other correctable rows (RxErr, BadTLP, Timeout, ...) are left
    out on purpose: they are routine on a healthy link and would arm the watch all day. A chip that
    stops answering with its reads in flight shows first as an Advisory Non-Fatal storm on the port
    above it, often reported firmware-first, so this row is the earliest kernel signal of it."""
    total, seen = 0, False
    try:
        for line in (dev / "aer_dev_correctable").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "NonFatalErr" and parts[1].isdigit():
                total, seen = total + int(parts[1]), True
    except OSError:
        pass
    for name in ("aer_dev_nonfatal", "aer_dev_fatal"):
        p = dev / name
        if p.exists():
            v = _sum_aer(p)
            if v is not None:
                total, seen = total + v, True
    for name in ("aer_rootport_total_err_nonfatal", "aer_rootport_total_err_fatal"):
        try:
            total += int((dev / name).read_text().strip())
            seen = True
        except (OSError, ValueError):
            continue
    return total if seen else None


def link_signals() -> dict:
    """The kernel's own record of PCIe trouble on every chip's path, for the sampler's early
    off-bus watch (spec 03 I79): ``{"chips": [index, ...], "aer": {bdf: uncorrectable total}}``.

    ``chips`` is the KMD node list; ``aer`` covers each chip's endpoint and every port above it up
    to the root port, keyed by PCI address, so a link drop the port above a chip counted shows
    even when the chip's own counters did not move (a vanished node is its own signal). ``paths``
    maps each chip index to the addresses on its own path, endpoint first, so a rise can be pinned
    to the chips below the port that counted it. Every value here is a counter the kernel keeps in memory:
    no read reaches a chip, a switch or a config space, which is what makes it safe to take every
    second while a chip may be dying (an access at a dead endpoint is what stalls a CPU core). The
    link-state attributes (``current_link_speed``/``width``) are config reads and stay out."""
    chips: list = []
    aer: dict = {}
    paths: dict = {}
    try:
        entries = sorted(SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return {"chips": chips, "aer": aer, "paths": paths}
    for ent in entries:
        idx = ent.name.rsplit("!", 1)[-1]
        if not idx.isdigit():
            continue
        chips.append(idx)
        try:
            dev = (ent / "device").resolve()
        except OSError:
            continue
        path = paths.setdefault(idx, [])
        while _BDF.match(dev.name):
            path.append(dev.name)
            if dev.name not in aer:
                aer[dev.name] = _uncorrectable_aer(dev)
            dev = dev.parent
    return {"chips": chips, "aer": aer, "paths": paths}


def chip_pci_bdf(idx: str) -> Optional[str]:
    """The PCI address behind a chip index, e.g. '0000:46:00.0'."""
    try:
        return (SYSFS_CLASS_DIR / f"tenstorrent!{idx}" / "device").resolve().name
    except OSError:
        return None


def sysfs_chip_ids() -> list[str]:
    """Every chip index the KMD exposes a class node for, sorted, from the directory listing alone.

    No attribute is read, so a chip whose heartbeat (or any other attribute) cannot be read is
    still listed: this is enumeration, not liveness. Use it wherever "which chips does the kernel
    have" is the question, e.g. the startup chip-index-to-PCI-address map the bridge rung needs."""
    try:
        entries = list(SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return []
    ids = (ent.name.rsplit("!", 1)[-1] for ent in entries)
    return sorted((i for i in ids if i.isdigit()), key=int)


def chip_pci_bdfs() -> dict:
    """Every chip index the KMD currently exposes, mapped to its PCI address:
    ``{0: '0000:01:00.0', ...}``. A chip whose address cannot be read is left out.

    The kernel's chip index is the one /dev, sysfs and the heartbeat read use, and it need not
    follow PCI order — so this, not a list position in a tt-smi snapshot, is what ties a chip
    index to its bus."""
    out: dict = {}
    try:
        entries = sorted(SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return out
    for ent in entries:
        idx = ent.name.rsplit("!", 1)[-1]
        if not idx.isdigit():
            continue
        bdf = chip_pci_bdf(idx)
        if bdf and pci_bus_number(bdf) is not None:
            out[int(idx)] = bdf
    return out


def pci_bus_number(address) -> Optional[int]:
    """The bus number of a PCI address ('0000:81:00.0' -> 0x81), or None when unreadable."""
    try:
        return int(str(address).split(":")[1], 16)
    except (IndexError, ValueError):
        return None


# Chips this broker cut out of the kernel itself (isolate_chip), chip index -> {"bdf", "bridge"}.
# Once the endpoint's node is removed its parent link is gone with it, so the bridge a reset must
# go through is recorded HERE, before the remove. A chip in this map is hung AND un-enumerated by
# us: re-enumerating it without a reset first lets the driver probe a hung chip (spec 04 I69).
# Kept on disk too (REMOVED_CHIPS_FILE), so a broker restart does not forget a chip it removed.
REMOVED_CHIPS: dict = {}
# The rung, the sampler and the gate read and write the records from worker threads.
_REMOVED_LOCK = threading.RLock()
REMOVED_CHIPS_FILE = "removed_chips.json"
# Spec 04 I98: how many times each chip was seen back on the bus (forget_removed_chip), so the hold's
# SBR record can tell a chip that came back and dropped again from one that never returned.
_BACK_SEQ: dict = {}
BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")

_BDF_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]")


def parent_bridge(bdf: Optional[str]) -> Optional[str]:
    """The BDF of the bridge directly above an enumerated endpoint, or None."""
    if not bdf:
        return None
    try:
        bridge = (PCI_DEVICES_DIR / bdf).resolve().parent.name
    except OSError:
        return None
    return bridge if _BDF_RE.fullmatch(bridge) else None


def _boot_id() -> str:
    try:
        return BOOT_ID_FILE.read_text().strip()
    except OSError:
        return ""


def _save_removed_chips() -> None:
    """Write the records with this boot's id, atomically. State that cannot be written must not
    break recovery: the in-memory records still guard this process."""
    try:
        with _REMOVED_LOCK:
            body = json.dumps({"boot_id": _boot_id(), "chips": dict(REMOVED_CHIPS)})
        p = evidence.health_dir() / REMOVED_CHIPS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(body)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001 - persistence is best effort; recovery must go on
        pass


def load_removed_chips() -> list:
    """Reload the records a previous broker process of THIS boot left, at broker start (spec 04 I69).
    A record from another boot is dropped: the boot reset every endpoint, so the chip is no longer
    un-reset, and its bus numbers may have moved. Records of chips back on the bus are dropped too.
    Returns the chip ids still recorded; the caller isolates them, so the bridge rung resets each
    through its recorded bridge before anything re-enumerates it. The file is rewritten only when a
    record was dropped."""
    try:
        data = json.loads((evidence.health_dir() / REMOVED_CHIPS_FILE).read_text())
    except (OSError, ValueError):
        data = None
    chips = data.get("chips") if isinstance(data, dict) else None
    if isinstance(chips, dict) and chips:
        if data.get("boot_id") and data.get("boot_id") == _boot_id():
            with _REMOVED_LOCK:
                for idx, rec in chips.items():
                    if str(idx).isdigit() and isinstance(rec, dict):
                        REMOVED_CHIPS.setdefault(str(idx), {"bdf": rec.get("bdf"), "bridge": rec.get("bridge")})
                loaded = sorted(REMOVED_CHIPS, key=int)
            evidence.health_event("removed_chip_records_loaded", chips=loaded)
        else:
            _save_removed_chips()
            evidence.health_event("removed_chip_records_cleared", chips=sorted(chips), reason="new_boot")
    forget_returned_removed_chips("broker start")
    return removed_chip_ids()


def _back_on_bus(idx: str, rec: dict) -> bool:
    """Whether anything is enumerated again at the removed chip's recorded address. Keyed on the
    address, not on ``tenstorrent!<idx>``: the driver may hand that index to another chip."""
    bdf = rec.get("bdf") if isinstance(rec, dict) else None
    try:
        if bdf:
            return (PCI_DEVICES_DIR / bdf).exists()
        return (SYSFS_CLASS_DIR / f"tenstorrent!{idx}").exists()
    except OSError:
        return False


def forget_returned_removed_chips(context: str) -> list:
    """Drop every record whose chip is enumerated again, whatever brought it back (a bridge reset, a
    mesh reset, an external tray re-power, a power cycle, an outside rescan): a record only describes a chip
    that is still off the bus. Left standing, it would withhold every later rescan and aim a bridge
    reset at a bus that now holds live silicon. Never releases anything (spec 04 I69)."""
    with _REMOVED_LOCK:
        back = [i for i, rec in sorted(REMOVED_CHIPS.items(), key=lambda kv: int(kv[0])) if _back_on_bus(i, rec)]
        for i in back:
            REMOVED_CHIPS.pop(i, None)
    if back:
        _save_removed_chips()
        evidence.health_event("removed_chip_records_cleared", chips=back, reason="back_on_bus", context=context)
    return back


def removed_chip(idx) -> Optional[dict]:
    """The record isolate_chip left for a chip it removed, or None when the broker did not remove it
    or the chip is back on the bus."""
    with _REMOVED_LOCK:
        rec = REMOVED_CHIPS.get(str(idx))
    if rec is not None and _back_on_bus(str(idx), rec):
        forget_returned_removed_chips("record read")
        return None
    return rec


def removed_chip_ids() -> list:
    """Every chip that still has a removal record and is still off the bus, sorted by index."""
    forget_returned_removed_chips("record read")
    with _REMOVED_LOCK:
        return sorted(REMOVED_CHIPS, key=int)


def forget_removed_chip(idx) -> None:
    """Drop the removal record of a chip that is back on the bus."""
    with _REMOVED_LOCK:
        gone = REMOVED_CHIPS.pop(str(idx), None)
        _BACK_SEQ[str(idx)] = _BACK_SEQ.get(str(idx), 0) + 1
    if gone is not None:
        _save_removed_chips()


def chip_back_seq(idx) -> int:
    """How many times chip ``idx`` was seen back on the bus in this process (spec 04 I98)."""
    with _REMOVED_LOCK:
        return _BACK_SEQ.get(str(idx), 0)


def isolate_chip(idx: str) -> bool:
    """Remove a dead chip from the kernel, so nothing on the box can reach it any more.

    THIS IS THE SAFETY VALVE, and it is worth doing even if the chip never comes back. Once
    the endpoint is unbound and its mappings are torn down, no driver, no poller and no job
    can issue MMIO at it — which converts "this host dies in about two minutes" into "one
    chip is offline". Recovering the chip is the next problem; not losing the machine is
    this one.

    A job holding the device will fault when its mapping vanishes. It was already doomed —
    its chip is returning all-ones — and a dead job is a far better outcome than a dead host
    that takes every other tenant's work with it.
    """
    bdf = chip_pci_bdf(idx)
    if not bdf:
        # The valve did not fire: with no BDF the endpoint cannot be unbound, so the dead
        # chip stays on the bus reachable by MMIO — the condition that takes the host down.
        # A silent False here reads as "isolated" to the caller; journal it as the hazard.
        evidence.health_event("chip_isolation_failed", chip=idx, reason="no_pci_bdf", host_at_risk=True)
        return False
    # Record the bridge BEFORE the remove: afterwards the node and its parent link are gone.
    rec = {"bdf": bdf, "bridge": parent_bridge(bdf)}
    with _REMOVED_LOCK:
        REMOVED_CHIPS[str(idx)] = rec
    _save_removed_chips()
    try:
        (PCI_DEVICES_DIR / bdf / "remove").write_text("1")
        return True
    except OSError as e:
        forget_removed_chip(idx)
        evidence.health_event(
            "chip_isolation_failed", chip=idx, bdf=bdf, reason="remove_write_failed", error=str(e), host_at_risk=True
        )
        return False


def chip_node_present(bdf: Optional[str]) -> bool:
    """Whether this chip's own PCIe endpoint node is still enumerated in sysfs.

    read_heartbeats() omits a chip two ways that look identical to it: one that LEFT THE BUS
    (its node is gone with it) and one still enumerated whose ARC-heartbeat read merely failed
    (a mailbox stall). Only the first is off the bus and safe to route to the gone-chip bridge
    reset; the second still shares its parent bridge with live silicon a Secondary Bus Reset
    would knock off. This is what tells them apart. Fail-safe toward "present" — a node that
    still resolves keeps the chip out of that reset. Honors TT_DEVICE_MCP_PCI_DIR.

    An unresolved address (chip_pci_bdf returned None) is treated as present: we cannot name the
    bridge, so we cannot reset it, and a chip we cannot confirm off the bus is held, not SBR'd.
    """
    if not bdf:
        return True
    try:
        return (PCI_DEVICES_DIR / bdf).exists()
    except OSError:
        return True
