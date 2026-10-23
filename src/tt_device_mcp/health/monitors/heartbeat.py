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

import errno
import time
from typing import Optional

from tt_device_mcp import metrics
from tt_device_mcp.health import chip_state
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors import hostpci, pci
from tt_device_mcp.health.monitors.pci import chip_pci_bdf

# The ARC counter ticks ~10Hz. This is the dwell between the two samples whose
# delta proves liveness: long enough that a live chip is guaranteed to advance,
# short enough to sit in the per-job gate unnoticed.
HEARTBEAT_SETTLE_SEC = 0.5


def read_heartbeats(*, probe_lost: bool = False, only: Optional[frozenset] = None) -> dict[str, int]:
    """Per-chip ARC heartbeat counters, keyed by device index.

    Each attribute read is a PCIe read of the chip (the KMD reads the counter from the chip's
    BAR), so a chip the broker has confirmed lost is not read again until :func:`chip_state.may_read`
    allows it (spec 03 I66); it reports ALL_ONES, what its last real read said. ``probe_lost`` is for
    a health check's decision read: each lost chip is read for real once, so a chip that answers
    again is judged on its answer, not on the mark. ``only``, when given, limits the read to those
    chips (spec 03 I83: the chips that just failed POST_RESET, and no other).

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
        if not idx.isdigit() or (only is not None and idx not in only):
            continue
        if not chip_state.may_read(idx, ent, probe=probe_lost):
            beats[idx] = ALL_ONES
            continue
        try:
            beats[idx] = int(_read_beat(ent))
        except (OSError, ValueError):
            continue
        chip_state.observe(idx, beats[idx])
    return beats


def _read_beat(node) -> str:
    """One chip's raw ``tt_heartbeat`` text. Its own function so the suite can make the read fail
    with a chosen errno (ENODATA cannot be produced by a plain file)."""
    return (node / "tt_heartbeat").read_text().strip()


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


def enodata_presence(beats: dict[str, int]) -> dict[str, str]:
    """The chips :func:`read_heartbeats` left out of ``beats`` whose heartbeat read fails with ENODATA
    on an endpoint bound to the driver, each with where it stands by a live read (spec 04 I24):
    :data:`hostpci.CONFIG_PRESENT` (its link does not read down and its config-space vendor id reads
    0x1e52 now: on the bus, ARC not ready), :data:`hostpci.CONFIG_MISSING` (link down, or the vendor
    id reads 0xffff: off the bus) or :data:`hostpci.CONFIG_UNREADABLE` (the config read failed: no
    evidence either way). tt-kmd answers ENODATA while a chip needs hardware init, after a reset
    whether it still has power or not, so ENODATA alone proves nothing. Only chips missing from
    ``beats`` are read: a full read makes no read at all."""
    try:
        entries = sorted(pci.SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return {}
    out: dict[str, str] = {}
    for ent in entries:
        idx = ent.name.rsplit("!", 1)[-1]
        if not idx.isdigit() or idx in beats:
            continue
        try:
            _read_beat(ent)
            continue  # it reads now: whatever made the first read miss, it is not ENODATA
        except OSError as e:
            if e.errno != errno.ENODATA:
                continue
        except ValueError:
            continue
        state = _enodata_state(idx)
        if state is not None:
            out[idx] = state
    return out


def _enodata_state(idx: str) -> Optional[str]:
    """Where a chip whose heartbeat read gives ENODATA stands (see :func:`enodata_presence`); None when
    its endpoint is gone or unbound (off the bus, as before)."""
    bdf = chip_pci_bdf(idx)
    try:
        bound = bool(bdf) and (pci.PCI_DEVICES_DIR / bdf / "driver").exists()
    except OSError:
        bound = False
    if not bound:
        return None
    if hostpci.link_down(bdf):
        return hostpci.CONFIG_MISSING
    return hostpci.config_vendor_state(bdf)


def arc_not_ready_chips(beats: dict[str, int]) -> set[str]:
    """Chips :func:`read_heartbeats` left out of ``beats`` that are still ON the bus: the KMD node is
    there, its PCI endpoint is bound to the driver, its link does not read down, a live read of its
    config-space vendor id gives 0x1e52, and the heartbeat read fails with ENODATA (the KMD has no ARC
    telemetry for it yet: ARC not ready, e.g. mid-boot after a reset or re-power). Such a chip is
    present but not usable. It is unhealthy for the gate, but it is not off the bus, so it never arms
    an off-bus rung (bridge, tray, gone-chip) nor counts toward a floor (spec 04 I24). Only the chips
    missing from ``beats`` are re-read; any other error, an endpoint that is gone, unbound or whose
    link is down, a vendor id of 0xffff, or a config read that fails leaves the chip counted off the
    bus (the last one is named by :func:`config_unreadable_chips`)."""
    return {i for i, s in enodata_presence(beats).items() if s == hostpci.CONFIG_PRESENT}


def config_unreadable_chips(beats: dict[str, int]) -> set[str]:
    """Chips whose heartbeat read gives ENODATA on a bound endpoint with its link up but whose live
    config read fails (:data:`hostpci.CONFIG_UNREADABLE`): neither ARC-not-ready nor proven gone. The
    rungs count them off the bus, so the hold stays; only the class A read takes them as on the bus
    (:func:`unreadable_on_bus_chips`, spec 04 I37)."""
    return {i for i, s in enodata_presence(beats).items() if s == hostpci.CONFIG_UNREADABLE}


def unreadable_on_bus_chips(ids) -> set[str]:
    """Of ``ids``, the chips whose KMD node is there with its PCI endpoint bound to the driver but
    whose heartbeat read fails with an error other than ENODATA (EIO, or the attribute missing on
    that node), or with ENODATA while its live config read fails (:func:`config_unreadable_chips`),
    and whose link does not read as down. :func:`offbus_chip_ids` counts such a chip off
    the bus; it is enumerated and bound, so it is on the bus but unhealthy. Only the class A read uses
    this (spec 04 I37): survivors of a whole-tray drop that read like this must not hide the drop's
    shape. A chip that reads now (a counter or all-ones), or reads ENODATA with its vendor id 0x1e52
    or 0xffff, is not in it; those have their own handling. Nor is a lingering node whose link is down (speed ``Unknown`` or width 63,
    :func:`hostpci.link_down`): spec 03 I9 never counts it back on the bus."""
    out: set[str] = set()
    for idx in sorted({str(i) for i in ids}, key=int):
        node = pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}"
        try:
            _read_beat(node)
            continue
        except OSError as e:
            if e.errno == errno.ENODATA:
                if node.is_dir() and _enodata_state(idx) == hostpci.CONFIG_UNREADABLE:
                    out.add(idx)
                continue
        except ValueError:
            continue
        bdf = chip_pci_bdf(idx)
        try:
            bound = bool(bdf) and node.is_dir() and (pci.PCI_DEVICES_DIR / bdf / "driver").exists()
        except OSError:
            bound = False
        if bound and not hostpci.link_down(bdf):
            out.add(idx)
    return out


def offbus_chip_ids(beats: dict[str, int], expected: int, not_ready: Optional[set] = None) -> set[str]:
    """The chips off the PCIe bus by this heartbeat read: every expected index missing from
    ``beats``, other than an ARC-not-ready chip (:func:`arc_not_ready_chips`), plus every chip
    reading all-ones. ``not_ready`` defaults to a fresh look at the missing chips."""
    nr = arc_not_ready_chips(beats) if not_ready is None else set(not_ready)
    missing = {str(i) for i in range(expected)} - set(beats) - nr
    return set(dead_chips(beats)) | missing


def offbus_count(
    beats: dict[str, int], expected: int, not_ready: Optional[set] = None, dead: Optional[list] = None
) -> int:
    """How many chips are off the PCIe bus by this heartbeat read: all-ones chips plus the shortfall
    below ``expected`` that ARC-not-ready chips do not explain. The one count every off-bus rung,
    floor and the /health ``off_bus`` field use."""
    nr = arc_not_ready_chips(beats) if not_ready is None else set(not_ready)
    dead = dead_chips(beats) if dead is None else dead
    return len(dead) + max(0, expected - len(beats) - len(nr - set(beats)))


def returned_chips(ids) -> list[str]:
    """Of ``ids``, the chips back on the bus by a fresh sysfs read: the KMD node is there and its
    heartbeat is a counter that is not all-ones, or it is ARC-not-ready on a bound endpoint. Read
    after every mesh reset and PCI rescan so a chip that came back leaves
    ``isolated_chips`` at once, whatever the verify that follows says."""
    want = {str(i) for i in ids}
    if not want:
        return []
    beats = read_heartbeats()
    back = {i for i in want if i in beats and beats[i] != ALL_ONES}
    if want - back:
        back |= want & arc_not_ready_chips(beats)
    return sorted(back, key=int)


def arc_live_chips(ids) -> list[str]:
    """Of ``ids``, the chips whose ARC heartbeat reads as a counter now: not all-ones, not an
    ENODATA (ARC not ready) or other failed read. Stricter than :func:`returned_chips`: a chip a
    bridge reset re-enumerated with its ARC dead is back on the bus but not recovered (spec 04 I94)."""
    want = {str(i) for i in ids}
    if not want:
        return []
    # Only these chips, and a lost-marked one too: a decision read, so it is judged on its answer.
    beats = read_heartbeats(probe_lost=True, only=frozenset(want))
    return sorted((i for i in want if i in beats and beats[i] != ALL_ONES), key=int)


def heartbeat_attr_present(ids) -> bool:
    """Does any of ``ids``' sysfs nodes expose ``tt_heartbeat``? A stat, no chip read. False on a
    host whose KMD predates the counter."""
    return any((pci.SYSFS_CLASS_DIR / f"tenstorrent!{i}" / "tt_heartbeat").exists() for i in ids)


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
      "attr_unreadable" — nodes are present and expose tt_heartbeat, but no read of it
                       succeeds right now (e.g. every chip left in a bad state by a failed
                       reset). The driver HAS the counter; the chips are not answering.
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
            int(_read_beat(ent))
            return None
        except (OSError, ValueError):
            continue
    if any((ent / "tt_heartbeat").exists() for ent in nodes):
        return "attr_unreadable"
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
        detector instead of having it disabled for the life of the process.
      - ``attr_unreadable``: the counter exists but no chip answers it right now. Also
        undecided, for the same reason: the driver supports it, the chips are bad. Reports
        unsupported meanwhile (nothing to sample), journaled once per absence so an
        empty class dir where chips are expected reaches a human without burying the
        log on every idle poll.
    """
    global _heartbeat_supported, _heartbeat_absent_journaled
    if _heartbeat_supported is not None and not refresh:
        return _heartbeat_supported
    reason = heartbeat_absence_reason()
    if reason in ("sysfs_absent", "attr_unreadable"):
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
    # A check, not a poll: a chip marked lost is read for real here (once; the second sample is
    # gated again), so a chip that came back passes and one that did not is judged on a real read.
    first = read_heartbeats(probe_lost=True)
    # read_heartbeats() leaves out a chip whose read fails, so a short (or empty) read is not a
    # short sysfs: count the class nodes too, so chips that are listed but unreadable are named as
    # such and never reported as "no chips exposed".
    in_sysfs = pci.sysfs_chip_ids() if len(first) < expected_count or not first else sorted(first, key=int)
    if not first and not in_sysfs:
        return (
            Verdict.UNHEALTHY,
            "no chips exposed in sysfs (driver wedged or chips off the bus)",
            {"expected_count": expected_count},
        )
    unreadable = sorted(set(in_sysfs) - set(first), key=int)

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
    if len(first) < expected_count or not first:
        # A chip whose heartbeat read gives ENODATA on a bound endpoint is on the bus with its ARC not
        # ready: still unhealthy, but named as such, so nobody reads it as a drop (spec 04 I24).
        # The config-space read decides ENODATA chips only: a chip whose config read fails is named as
        # such, counted off the bus, and never taken as present.
        presence = enodata_presence(first)
        not_ready = sorted((i for i, s in presence.items() if s == hostpci.CONFIG_PRESENT), key=int)
        cfg_unreadable = sorted((i for i, s in presence.items() if s == hostpci.CONFIG_UNREADABLE), key=int)
        cfg_note = f"; config space unreadable on [{','.join(cfg_unreadable)}]" if cfg_unreadable else ""
        off = offbus_count(first, expected_count, set(not_ready))
        if not_ready:
            ev = {
                "present": sorted(first),
                "in_sysfs": len(in_sysfs),
                "arc_not_ready": not_ready,
                "off_bus": off,
                "expected_count": expected_count,
            }
            if cfg_unreadable:
                ev["config_unreadable"] = cfg_unreadable
            return (
                Verdict.UNHEALTHY,
                f"chip(s) [{','.join(not_ready)}] on the bus but ARC not ready (heartbeat read ENODATA), "
                f"{off} off the bus{cfg_note}",
                ev,
            )
        if unreadable:
            ev = {
                "present": sorted(first),
                "in_sysfs": len(in_sysfs),
                "heartbeat_unreadable": unreadable,
                "expected_count": expected_count,
            }
            if cfg_unreadable:
                ev["config_unreadable"] = cfg_unreadable
            return (
                Verdict.UNHEALTHY,
                f"{len(in_sysfs)} chip(s) in sysfs, heartbeat unreadable on {len(unreadable)} "
                f"[{','.join(unreadable)}], expected {expected_count}{cfg_note}",
                ev,
            )
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
