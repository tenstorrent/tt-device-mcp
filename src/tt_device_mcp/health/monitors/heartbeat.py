# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The ARC heartbeat probe.

Reads the KMD's per-chip ARC liveness counter from sysfs. That is the whole point: it
is the only chip-liveness signal that does not touch the device. tt-smi does a full UMD
TopologyDiscovery on every call — a device init — and a device init aimed at a wedged
chip is what walks the root complex toward a fatal PCIe error and an ungraceful host
reset. Reading sysfs cannot: the KMD already holds the value, so the probe is a file
read and nothing reaches the chip. All 32 chips scrape in ~0.03s.

The probe is also total: the driver always has a value to give. There is no
"the check itself broke" outcome to reason about, which is what lets the caller
treat a failed check as a failed device without hedging.

Scope: the heartbeat proves each chip's ARC is alive. It says nothing about the
inter-chip fabric — sysfs exposes no ethernet/link attributes at all — so a
fabric verdict still requires a traffic workload.
"""

from __future__ import annotations

import time
from typing import Optional

from tt_device_mcp import metrics
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.monitors.pci import chip_pci_bdf

# The ARC counter ticks ~10Hz. This is the dwell between the two samples whose
# delta proves liveness: long enough that a live chip is guaranteed to advance,
# short enough to sit in the per-job gate unnoticed.
HEARTBEAT_SETTLE_SEC = 0.5


def read_heartbeats() -> dict[str, int]:
    """Per-chip ARC heartbeat counters, keyed by device index. Zero device touches.

    A chip whose attribute is missing or unreadable is omitted rather than zeroed,
    so the caller sees it as an absent chip (a real fault) and not as a chip whose
    counter happens to sit at zero.
    """
    beats: dict[str, int] = {}
    try:
        entries = sorted(pci.SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return beats
    for ent in entries:
        # KMD names these `tenstorrent!N`; the index is what tt-smi and /dev agree on.
        idx = ent.name.rsplit("!", 1)[-1]
        if not idx.isdigit():
            continue
        try:
            beats[idx] = int((ent / "tt_heartbeat").read_text().strip())
        except (OSError, ValueError):
            continue
    return beats


# A PCIe read to an endpoint that is not answering returns all-ones. It is not a value — no
# counter, clock or trip count is ever 0xFFFFFFFF — it is the bus telling us the chip is
# gone. Every register on the dead chip reads this at once, which is what makes it
# unmistakable.
#
# This is the single most important signal the broker can see. A host was lost because a
# chip started returning all-ones and, for the next two minutes, three different things kept
# issuing MMIO at it: a fabric check that hung on it, tt-telemetry (which segfaulted and was
# restarted straight back into it), and our own sampler. A CPU core eventually stalled on a
# read that never completed, never retired the instruction, and its watchdog took the machine
# down. Reads at a dead endpoint are not harmless — they are what kills the box.
ALL_ONES = 0xFFFFFFFF


def dead_chips(beats: dict[str, int]) -> list[str]:
    """Chips that have fallen off the PCIe bus, by index. See ALL_ONES."""
    return sorted((i for i, v in beats.items() if v == ALL_ONES), key=int)


def heartbeat_absence_reason() -> Optional[str]:
    """Why read_heartbeats() came back empty, or None if a heartbeat is exposed.

    read_heartbeats() collapses two opposite conditions to the same empty dict, and
    the operator's response to each is different:
      "sysfs_absent" — no tenstorrent class nodes at all: the KMD is not loaded, or
                       no chip enumerated. At boot, where the device is meant to be
                       present, this is an alarm, not a tolerable degrade.
      "attr_absent"  — nodes are present but none expose tt_heartbeat: this KMD
                       predates the counter. The detector genuinely does not exist
                       on this host, and falling through to tt-smi is correct.
    Returns None as soon as one chip exposes a readable counter — the probe works.
    """
    try:
        entries = sorted(pci.SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return "sysfs_absent"
    nodes = [e for e in entries if e.name.rsplit("!", 1)[-1].isdigit()]
    if not nodes:
        return "sysfs_absent"
    for ent in nodes:
        try:
            int((ent / "tt_heartbeat").read_text().strip())
            return None
        except (OSError, ValueError):
            continue
    return "attr_absent"


_heartbeat_supported: bool | None = None
_heartbeat_absent_journaled: bool = False


def heartbeat_supported(refresh: bool = False) -> bool:
    """Does this host's driver expose the per-chip ARC heartbeat at all?

    A permanent property of the driver, so the answer is cached once it can be
    decided — but the two empty-sysfs conditions are not the same question and must
    not both settle the cache:
      - ``attr_absent``: nodes are present but none expose the counter — this KMD
        predates it, the detector genuinely does not exist here. Permanent: cache
        False and fall through to tt-smi.
      - ``sysfs_absent``: no nodes at all — the chips are off the bus RIGHT NOW, so
        whether the driver WOULD expose the counter is unknowable. Leave the cache
        undecided (do NOT latch False) and re-probe next call: a box that boots dead
        and is later cold-power-cycled back — with no broker restart — re-arms this
        detector instead of having it disabled for the life of the process. Reports
        unsupported meanwhile (nothing to sample), journaled once per absence so an
        empty class dir where chips are expected reaches a human without burying the
        log on every idle poll.
    """
    global _heartbeat_supported, _heartbeat_absent_journaled
    if _heartbeat_supported is not None and not refresh:
        return _heartbeat_supported
    reason = heartbeat_absence_reason()
    if reason == "sysfs_absent":
        _heartbeat_supported = None  # undecided until chips enumerate — re-probe next call
        if not _heartbeat_absent_journaled:
            health_event("heartbeat_unsupported", reason=reason)
            _heartbeat_absent_journaled = True
        return False
    _heartbeat_absent_journaled = False  # chips answered (or a permanent verdict) — re-arm the alarm
    _heartbeat_supported = reason is None
    if reason is not None:
        # attr_absent is a permanent degrade; journal it once with its cause so the
        # fall-through to tt-smi is never silent.
        health_event("heartbeat_unsupported", reason=reason)
    return _heartbeat_supported


def heartbeat_verdict(expected_count: int, settle_sec: float = HEARTBEAT_SETTLE_SEC) -> tuple[Verdict, str, dict]:
    """Sample the ARC heartbeat twice and classify liveness. Zero device touches.

    Returns (verdict, detail, evidence). Every outcome that is not "all expected
    chips present and ticking" is UNHEALTHY, including an empty sysfs: if the
    devices exist (the caller checked /dev before calling) but the driver exposes
    none of them, the device is not usable, whatever the cause.

    Wrapped by a thin timer that reports the "heartbeat" probe metric — every caller of this
    function (the gate, ``Recovery._verify_device``, ``HealthMonitor.update()``) is covered from
    this one instrumentation point.
    """
    _t0 = time.monotonic()
    verdict, detail, evidence = _heartbeat_verdict_body(expected_count, settle_sec)
    metrics.probe_observed("heartbeat", verdict.value, time.monotonic() - _t0)
    return verdict, detail, evidence


def _heartbeat_verdict_body(expected_count: int, settle_sec: float) -> tuple[Verdict, str, dict]:
    first = read_heartbeats()
    if not first:
        return (
            Verdict.UNHEALTHY,
            "no chips exposed in sysfs (driver wedged or chips off the bus)",
            {"expected_count": expected_count},
        )

    # Checked before anything else, and reported distinctly, because the response is
    # different: a chip returning all-ones has left the bus, and the correct move is to cut
    # it out of the kernel immediately rather than to reset the mesh around it. Every read
    # aimed at it from here on is a transaction that may never complete, and one of those
    # stalling a CPU core is what reboots the host.
    dead = dead_chips(first)
    if dead:
        return (
            Verdict.UNHEALTHY,
            f"chip(s) [{','.join(dead)}] FELL OFF THE PCIe BUS (all reads return 0xFFFFFFFF) "
            f"— isolate before anything touches them again",
            {"dead": dead, "pci": {i: chip_pci_bdf(i) for i in dead}},
        )

    # Only a short count is a drop. An over-count means MORE chips are on the bus than
    # ``expected_count`` — a stale high-water mark baked while degraded, cleared by a reset
    # that recovered the rest — and that is a healthy mesh, not chips that fell off it.
    if len(first) < expected_count:
        return (
            Verdict.UNHEALTHY,
            f"{len(first)} chip(s) in sysfs, expected {expected_count} (chips dropped off the bus)",
            {"present": sorted(first), "expected_count": expected_count},
        )

    time.sleep(settle_sec)
    second = read_heartbeats()

    # A chip that was ticking in `first` can leave the bus during the settle window. It stays a
    # present key reading all-ones, so it is neither "missing" (still in sysfs) nor "stalled"
    # (all-ones != its prior counter) — it would reach HEALTHY. Check it off the bus the same way
    # the first sample is, and with the same urgency: isolate before anything reads it again.
    dead_now = dead_chips(second)
    if dead_now:
        return (
            Verdict.UNHEALTHY,
            f"chip(s) [{','.join(dead_now)}] FELL OFF THE PCIe BUS mid-probe (all reads return "
            f"0xFFFFFFFF) — isolate before anything touches them again",
            {"dead": dead_now, "pci": {i: chip_pci_bdf(i) for i in dead_now}},
        )

    missing = sorted(set(first) - set(second))
    if missing:
        return (
            Verdict.UNHEALTHY,
            f"chip(s) [{','.join(missing)}] disappeared from sysfs mid-probe",
            {"missing": missing},
        )

    stalled = sorted(i for i, v in first.items() if second.get(i) == v)
    if stalled:
        return (
            Verdict.UNHEALTHY,
            f"chip(s) [{','.join(stalled)}] ARC heartbeat frozen over {settle_sec:.1f}s (wedged)",
            {"stalled": stalled, "before": first, "after": second},
        )

    return (
        Verdict.HEALTHY,
        f"all {len(second)} chips' ARC heartbeat advancing",
        {"chips": len(second)},
    )
