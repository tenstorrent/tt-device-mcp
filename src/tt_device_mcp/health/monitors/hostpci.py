# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Host-side PCI usability: is each chip the bus shows actually reachable?

The cheapest probe there is, and the only one that still answers when the chips are wedged.
Everything here is a sysfs read — no device open, no ioctl, no UMD — which is what lets it run
ahead of the tt-smi snapshot rather than after it. A chip whose driver never bound, or whose BAR
the kernel could not place, is unusable no matter what its ARC heartbeat says, and both faults
are invisible to every probe we had: the heartbeat reads a chip's own counter, and the snapshot
asks tt-smi, which needs the very mapping an unplaced BAR denies it.

Two questions get a verdict, because either answer makes the device unusable and that is the
only question a verdict answers (see health.core.Verdict — there is no third state):

  * every Tenstorrent function on the bus is bound to tt-kmd, and
  * no implemented BAR is left unassigned.

Everything else this reads — IOMMU grouping, link speed and width, AER totals on the endpoints
*and* their upstream bridges — is recorded as evidence and never alerts. A correctable-error
count is a trend, not a state: non-zero is ordinary on a healthy link, and a probe that held the
device over one would be wrong far more often than right. The counters are here because the
documented path from a wedged chip to an ungraceful host reboot runs through the root complex
escalating accumulated errors, and the bridge half of that path was not being recorded at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from tt_device_mcp.health.monitors import pci

# Tenstorrent's PCI vendor id, as sysfs renders it.
TT_VENDOR_ID = "0x1e52"
DRIVER_NAME = "tenstorrent"
# Only the six real BARs; sysfs `resource` carries ROM and bridge windows past them.
BAR_COUNT = 6


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _bound_driver(dev: Path) -> Optional[str]:
    """The driver bound to this function, or None when nothing is.

    The link's existence is checked first because ``realpath`` does not raise on a path that is
    not there — it hands back the path unchanged, so an unbound device reported its driver as the
    literal ``"driver"``. Wrong in the one direction that matters: the fault was still caught, but
    named as a mystery driver instead of as nothing bound at all.
    """
    link = dev / "driver"
    if not link.is_symlink() and not link.exists():
        return None
    try:
        return os.path.basename(os.path.realpath(link))
    except OSError:
        return None


def _unassigned_bars(dev: Path) -> list[int]:
    """BARs the device implements that the kernel could not place.

    ``flags`` non-zero is what says the BAR exists — an unimplemented one reads all zeros across
    start, end and flags, and a 64-bit BAR leaves its second slot that way too. So a start of
    zero only means "unplaced" when the flags say there was something to place. Judging on the
    address alone would report every healthy device as broken.
    """
    raw = _read(dev / "resource")
    if raw is None:
        return []
    bad = []
    for index, line in enumerate(raw.splitlines()[:BAR_COUNT]):
        parts = line.split()
        if len(parts) != 3:
            continue
        start, _end, flags = parts
        try:
            if int(flags, 16) != 0 and int(start, 16) == 0:
                bad.append(index)
        except ValueError:
            continue
    return bad


def _aer_totals(dev: Path) -> dict:
    """Accumulated AER counts for one function, or {} where the kernel exposes none."""
    out = {}
    for attr, key in (
        ("aer_dev_correctable", "correctable"),
        ("aer_dev_nonfatal", "nonfatal"),
        ("aer_dev_fatal", "fatal"),
    ):
        total = pci._sum_aer(dev / attr)
        if total is not None:
            out[key] = total
    return out


def tt_functions() -> list[str]:
    """Every Tenstorrent function on the PCI bus, by BDF.

    Read from the bus rather than from /dev or /sys/class/tenstorrent on purpose: a chip whose
    driver never bound has no node under either, so asking those two would make an unbound chip
    look like an absent one — the exact fault this probe exists to name.
    """
    try:
        entries = sorted(pci.PCI_DEVICES_DIR.iterdir())
    except OSError:
        return []
    return [e.name for e in entries if _read(e / "vendor") == TT_VENDOR_ID]


def host_pci_verdict() -> tuple[Optional[bool], str, dict]:
    """``(ok, detail, evidence)`` — ok None when there is nothing on the bus to judge.

    None rather than True for a bus with no Tenstorrent function: this probe has no opinion about
    a host that has no chips, and reporting HEALTHY would let an empty bus vouch for a device.
    """
    bdfs = tt_functions()
    if not bdfs:
        return None, "no Tenstorrent function on the PCI bus", {}

    unbound: list[str] = []
    badbars: dict[str, list[int]] = {}
    devices: dict[str, dict] = {}
    bridges: dict[str, dict] = {}

    for bdf in bdfs:
        dev = pci.PCI_DEVICES_DIR / bdf
        driver = _bound_driver(dev)
        if driver != DRIVER_NAME:
            unbound.append(f"{bdf} ({driver or 'nothing'})")
        bars = _unassigned_bars(dev)
        if bars:
            badbars[bdf] = bars

        rec: dict = {"driver": driver}
        for attr, key in (
            ("current_link_speed", "link_speed"),
            ("current_link_width", "link_width"),
            ("max_link_speed", "link_speed_max"),
            ("max_link_width", "link_width_max"),
        ):
            val = _read(dev / attr)
            if val is not None:
                rec[key] = val
        group = _read(dev / "iommu_group")
        try:
            rec["iommu_group"] = os.path.basename(os.path.realpath(dev / "iommu_group"))
        except OSError:
            if group is not None:
                rec["iommu_group"] = group
        aer = _aer_totals(dev)
        if aer:
            rec["aer"] = aer
        if bars:
            rec["unassigned_bars"] = bars
        devices[bdf] = rec

        # The upstream bridge is the other end of the link, and errors logged there never appear
        # on the endpoint. Recorded per bridge rather than per chip: several chips share one.
        try:
            parent = Path(os.path.realpath(dev / ".."))
        except OSError:
            continue
        if parent.name in bridges or not (parent / "vendor").exists():
            continue
        baer = _aer_totals(parent)
        if baer:
            bridges[parent.name] = {"aer": baer}

    evidence: dict = {"devices": devices, "iommu": _iommu_mode()}
    if bridges:
        evidence["bridges"] = bridges

    problems = []
    if unbound:
        problems.append(f"tt-kmd not bound to {len(unbound)}/{len(bdfs)}: {', '.join(unbound)}")
    if badbars:
        listed = ", ".join(f"{bdf} BAR{'/'.join(str(b) for b in bars)}" for bdf, bars in badbars.items())
        problems.append(f"unassigned BARs on {len(badbars)}/{len(bdfs)}: {listed}")
    if problems:
        return False, "; ".join(problems), evidence

    return True, f"{len(bdfs)}/{len(bdfs)} functions bound to tt-kmd with every BAR assigned", evidence


def _iommu_mode() -> str:
    """How the IOMMU is configured, as a fact for the record rather than a verdict.

    It changes what a DMA fault looks like and whether a passthrough guest can drive the device
    at all, so it belongs in a post-mortem — but a passthrough box and a translated box are both
    legitimate, so it never alerts.
    """
    try:
        types = sorted(p.name for p in Path("/sys/class/iommu").iterdir())
    except OSError:
        types = []
    if not types:
        return "none"
    cmdline = _read(Path("/proc/cmdline")) or ""
    passthrough = "iommu.passthrough=1" in cmdline or "iommu=pt" in cmdline
    return f"{','.join(types)}{' passthrough' if passthrough else ''}"


# The host's 1G hugepage pool. A chip behind an identity-mapped (passthrough) IOMMU, or no IOMMU at
# all, cannot be opened until its 1G hugepage exists: UMD pins the host-side buffers there. Boot
# allocates them in tenstorrent-hugepages.service, late (after multi-user.target), so a broker that
# starts first sees too few, and any fabric pass it runs before then exits 77.
HUGEPAGES_1G_NR = Path("/sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages")


def _needs_hugepages() -> bool:
    """Whether any Tenstorrent function sits behind an identity IOMMU domain or none at all.

    A translated domain (DMA, DMA-FQ) maps the device's buffers through the IOMMU and needs no
    hugepages; a host with no Tenstorrent function on the bus has nothing to wait for."""
    for bdf in tt_functions():
        group = pci.PCI_DEVICES_DIR / bdf / "iommu_group"
        if not group.exists():
            return True  # no IOMMU in front of this function
        if (_read(group / "type") or "") == "identity":
            return True
    return False


def hugepages_shortfall(need: int) -> Optional[tuple[int, int]]:
    """``(have, need)`` when the chips need 1G hugepages and fewer than ``need`` are allocated.

    None when there is nothing to wait for: no count given, every chip behind a translating IOMMU,
    or a kernel with no 1G hugepage pool to read (an unreadable pool is not evidence of a short
    one, and waiting on it would hold a box that may not use hugepages at all). Sysfs reads only."""
    if need <= 0 or not _needs_hugepages():
        return None
    raw = _read(HUGEPAGES_1G_NR)
    if raw is None or not raw.isdigit():
        return None
    have = int(raw)
    return (have, need) if have < need else None


# Minimum host software the broker's own reset and probe semantics were matched against. Read
# from sysfs only, so the check costs nothing and cannot touch a device. Overridable per site:
# a floor that cannot be lowered is a floor that gets deleted the first time it is inconvenient.
KMD_VERSION_FLOOR = os.environ.get("TT_DEVICE_MCP_KMD_MIN_VERSION", "2.9.0").strip()
FW_BUNDLE_VERSION_FLOOR = os.environ.get("TT_DEVICE_MCP_FW_MIN_VERSION", "19.11").strip()
KMD_VERSION_PATH = Path("/sys/module/tenstorrent/version")
# tt-smi's version is deliberately NOT checked here. Reading it means spawning tt-smi, and this
# module's whole contract is that it opens nothing and spawns nothing — a boot-time probe that
# shells the device tool is the one thing a wedged host cannot afford. Declare tt-smi's floor in
# deployment instead, where the install already pins the binary.


def _version_tuple(raw: str) -> tuple:
    """A dotted version as comparable ints, stopping at the first non-numeric component.

    Element-wise on unequal lengths is what makes `19.7.1.0` correctly read as below `19.11`:
    string comparison would put "19.7" above "19.11", which is the wrong answer in the one
    direction that matters.
    """
    parts = []
    for chunk in raw.strip().split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def _below_floor(actual: str, floor: str) -> bool:
    a, f = _version_tuple(actual), _version_tuple(floor)
    if not a or not f:
        return False
    return a < f


def kmd_version() -> Optional[str]:
    return _read(KMD_VERSION_PATH)


def fw_bundle_versions() -> dict:
    """Each chip's firmware bundle version, by chip index, from the driver's own sysfs class."""
    out: dict[str, str] = {}
    try:
        entries = sorted(pci.SYSFS_CLASS_DIR.iterdir())
    except OSError:
        return out
    for ent in entries:
        idx = ent.name.rsplit("!", 1)[-1]
        if not idx.isdigit():
            continue
        val = _read(ent / "tt_fw_bundle_ver")
        if val:
            out[idx] = val
    return out


def version_floor_warnings() -> list[str]:
    """Host software below the versions this broker's behaviour was matched against.

    Warnings, never a refusal to serve. An old bundle is a configuration gap, not a missing
    recovery capability: the device may be perfectly usable, and a broker that would not start
    over it denies service for something nobody asked it to enforce. What it buys is an
    explanation — a firmware below 19.9 does not populate the eth link-status telemetry at all,
    so the eth probe SKIPs, and without this line that skip looks like a broken probe rather
    than a host that cannot answer the question.
    """
    warns: list[str] = []

    kmd = kmd_version()
    if kmd and _below_floor(kmd, KMD_VERSION_FLOOR):
        warns.append(f"tt-kmd {kmd} is below the {KMD_VERSION_FLOOR} floor ({KMD_VERSION_PATH})")

    below = {idx: ver for idx, ver in fw_bundle_versions().items() if _below_floor(ver, FW_BUNDLE_VERSION_FLOOR)}
    if below:
        seen = sorted(set(below.values()))
        warns.append(
            f"{len(below)} chip(s) on firmware bundle {', '.join(seen)}, below the "
            f"{FW_BUNDLE_VERSION_FLOOR} floor — eth link-status telemetry is unpopulated below "
            f"19.9, so the eth probe cannot answer on this host"
        )
    return warns
