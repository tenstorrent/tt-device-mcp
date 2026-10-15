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

import os
import time
from pathlib import Path
from typing import Optional

from tt_device_mcp.health import evidence

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
    """Everything the host knows about the chips without touching them.

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
        for attr in _CHIP_ATTRS:
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
                ("current_link_width", "link_width"),
                ("max_link_width", "link_width_max"),
                ("current_link_speed", "link_speed"),
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
    """One compact sample of every chip's volatile state. Zero device touches.

    Compact on purpose: this runs every few seconds for the life of a job, so it carries
    the moving values only, as a flat list per chip — [heartbeat, aiclk, arcclk,
    therm_trips, aer_correctable, aer_fatal].
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
        for attr in _VOLATILE:
            try:
                v = (ent / attr).read_text().strip()
                row.append(int(v) if v.isdigit() else v)
            except OSError:
                row.append(None)
        try:
            pci = (ent / "device").resolve()
            row.append(_sum_aer(pci / "aer_dev_correctable"))
            row.append(_sum_aer(pci / "aer_dev_fatal"))
        except OSError:
            row.extend([None, None])
        out[idx] = row
    return out


SAMPLE_FIELDS = list(_VOLATILE) + ["aer_correctable", "aer_fatal"]


def chip_pci_bdf(idx: str) -> Optional[str]:
    """The PCI address behind a chip index, e.g. '0000:46:00.0'."""
    try:
        return (SYSFS_CLASS_DIR / f"tenstorrent!{idx}" / "device").resolve().name
    except OSError:
        return None


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
    try:
        (PCI_DEVICES_DIR / bdf / "remove").write_text("1")
        return True
    except OSError as e:
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
