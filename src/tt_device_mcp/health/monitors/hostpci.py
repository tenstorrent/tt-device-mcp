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

Four questions get a verdict, because any answer makes the device unusable and that is the
only question a verdict answers (see health.core.Verdict — there is no third state):

  * every Tenstorrent function on the bus is bound to tt-kmd,
  * no implemented BAR is left unassigned,
  * no function's link reads as down (speed ``Unknown`` or width 63: :func:`link_down`), and
  * no live link trained below the link it should have: narrower or slower than the lower of the
    two ends' caps, or than it showed on an earlier passing check (:func:`_judge_link`).

Everything else this reads — IOMMU grouping, the speed of a device whose speed is not judged,
AER totals on the endpoints *and* their upstream bridges — is recorded as evidence and never
alerts. A correctable-error count is a trend, not a state: non-zero is ordinary on a healthy
link, and a probe that held the device over one would be wrong far more often than right. The
counters are here because the documented path from a wedged chip to an ungraceful host reboot
runs through the root complex escalating accumulated errors, and the bridge half of that path
was not being recorded at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from tt_device_mcp.health import evidence
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


# A link that is down reads all-ones from config space: the kernel shows its speed as "Unknown" and
# its width as 63 (the six-bit field all set). The node is still listed, so it looks present.
LINK_DOWN_WIDTH = "63"


def link_down(bdf: str, devices_dir: Optional[Path] = None) -> bool:
    """Whether this function's PCIe link reads as down (speed ``Unknown`` or width 63), so a listed
    node is not counted as a chip on the bus. An attribute that cannot be read is no evidence: False.
    ``devices_dir`` defaults to :data:`pci.PCI_DEVICES_DIR`."""
    dev = (devices_dir or pci.PCI_DEVICES_DIR) / bdf
    speed = _read(dev / "current_link_speed")
    return (speed is not None and speed.startswith("Unknown")) or _read(dev / "current_link_width") == LINK_DOWN_WIDTH


# A live link that trained narrower or slower than it can (spec 03 I9). The link it should have is
# the lower of the two ends' capabilities: on a Galaxy most chips are x8 endpoints behind x1 root
# ports, so they train x1 by design and the endpoint's own cap alone would hold every one of them.
# Each link is also judged against the best it has shown on a passing check, banked here.
PCIE_LINK_BASELINE_FILE = "pcie_link_baseline.json"
# Device ids whose trained speed is judged as well as their width. ASPM (L0s/L1) changes a link's
# power state, never the speed it trained at, but a device may still retrain slower on its own while
# idle (autonomous speed change), and that is a power state, not a fault. Blackhole (0xb140) is
# listed on evidence: every link on a 32-chip Galaxy read its capped 32 GT/s on every recorded
# pass, idle or not. Any other device is judged on width only, with its speed kept as evidence,
# until its links show the same.
SPEED_JUDGED_DEVICE_IDS = frozenset({"0xb140"})


def _link_width(raw: Optional[str]) -> Optional[int]:
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _link_speed(raw: Optional[str]) -> Optional[float]:
    """GT/s from a sysfs speed like ``32.0 GT/s PCIe``; None for ``Unknown`` or anything unparsable."""
    try:
        return float(raw.split()[0]) if raw else None
    except (ValueError, IndexError):
        return None


def _cap(*values: Optional[float]) -> Optional[float]:
    known = [v for v in values if v is not None]
    return min(known) if known else None


def _load_link_baseline() -> dict:
    """The banked links, ``{bdf: {...}}``. Missing or unreadable is an empty bank: the caps still
    judge every link, and the next passing check rewrites the file."""
    try:
        data = json.loads((evidence.health_dir() / PCIE_LINK_BASELINE_FILE).read_text())
        links = data.get("links")
        return links if isinstance(links, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _bank_link_baseline(banked: dict, current: dict) -> None:
    """Ratchet each link's mark up to what this passing check read. A mark belongs to the link it
    was taken on (the upstream port and both caps): when those differ, the old mark is replaced,
    since it no longer describes this link. Written only when something changed."""
    out = dict(banked)
    for bdf, cur in current.items():
        old = banked.get(bdf)
        if isinstance(old, dict) and all(old.get(k) == cur[k] for k in ("port", "width_cap", "speed_cap")):
            cur = dict(cur)
            for k in ("width", "speed"):
                if old.get(k) is not None and (cur[k] is None or old[k] > cur[k]):
                    cur[k] = old[k]
        out[bdf] = cur
    if out == banked:
        return
    try:
        p = evidence.health_dir() / PCIE_LINK_BASELINE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"links": out}))
        os.replace(tmp, p)
    except OSError:
        pass  # a mark we cannot persist must not break the gate; the caps still judge every link


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
    downlinks: list[str] = []
    degraded: list[str] = []
    badbars: dict[str, list[int]] = {}
    devices: dict[str, dict] = {}
    bridges: dict[str, dict] = {}
    banked = _load_link_baseline()
    links: dict[str, dict] = {}

    for bdf in bdfs:
        dev = pci.PCI_DEVICES_DIR / bdf
        driver = _bound_driver(dev)
        if driver != DRIVER_NAME:
            unbound.append(f"{bdf} ({driver or 'nothing'})")
        bars = _unassigned_bars(dev)
        if bars:
            badbars[bdf] = bars
        down = link_down(bdf)
        if down:
            downlinks.append(bdf)

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

        # The upstream bridge is the other end of the link: its cap bounds what the link can train
        # to, and errors logged there never appear on the endpoint.
        try:
            parent: Optional[Path] = Path(os.path.realpath(dev / ".."))
        except OSError:
            parent = None
        port = parent if parent is not None and (parent / "vendor").exists() else None
        reason = _judge_link(dev, port, rec, banked, links)
        if reason and not down:
            degraded.append(f"{bdf} {reason}")

        # Recorded per bridge rather than per chip: several chips share one.
        if port is None or port.name in bridges:
            continue
        baer = _aer_totals(port)
        if baer:
            bridges[port.name] = {"aer": baer}

    evidence: dict = {"devices": devices, "iommu": _iommu_mode()}
    if bridges:
        evidence["bridges"] = bridges

    problems = []
    if unbound:
        problems.append(f"tt-kmd not bound to {len(unbound)}/{len(bdfs)}: {', '.join(unbound)}")
    if downlinks:
        problems.append(f"PCIe link down on {len(downlinks)}/{len(bdfs)}: {', '.join(downlinks)}")
    if degraded:
        problems.append(f"PCIe link degraded on {len(degraded)}/{len(bdfs)}: {', '.join(degraded)}")
    if badbars:
        listed = ", ".join(f"{bdf} BAR{'/'.join(str(b) for b in bars)}" for bdf, bars in badbars.items())
        problems.append(f"unassigned BARs on {len(badbars)}/{len(bdfs)}: {listed}")
    if problems:
        return False, "; ".join(problems), evidence

    _bank_link_baseline(banked, links)
    return (
        True,
        f"{len(bdfs)}/{len(bdfs)} functions bound to tt-kmd with every BAR assigned and no link below its expected width or speed",
        evidence,
    )


def _judge_link(dev: Path, port: Optional[Path], rec: dict, banked: dict, links: dict) -> str:
    """Why this function's live link is below the link it should have, or "" when it is not.

    Expected is the lower of the endpoint's and the upstream port's cap, and also no lower than the
    best this same link showed on a passing check. Records the expected link in ``rec`` and this
    link's reading in ``links`` (banked by the caller only when the whole check passes). An
    unreadable attribute is no evidence: a link with no readable width is not judged on width."""
    width, speed = _link_width(rec.get("link_width")), _link_speed(rec.get("link_speed"))
    port_width = _read(port / "max_link_width") if port is not None else None
    port_speed = _read(port / "max_link_speed") if port is not None else None
    if port_width is not None:
        rec["link_width_port_max"] = port_width
    if port_speed is not None:
        rec["link_speed_port_max"] = port_speed
    width_cap = _cap(_link_width(rec.get("link_width_max")), _link_width(port_width))
    speed_cap = _cap(_link_speed(rec.get("link_speed_max")), _link_speed(port_speed))
    judge_speed = _read(dev / "device") in SPEED_JUDGED_DEVICE_IDS
    if width_cap is not None:
        rec["link_width_expected"] = width_cap
    if speed_cap is not None and judge_speed:
        rec["link_speed_expected"] = speed_cap

    here = {"port": port.name if port is not None else None, "width_cap": width_cap, "speed_cap": speed_cap}
    links[dev.name] = {**here, "width": width, "speed": speed}
    mark = banked.get(dev.name)
    if not isinstance(mark, dict) or any(mark.get(k) != v for k, v in here.items()):
        mark = {}

    reasons = []
    if width is not None:
        if width_cap is not None and width < width_cap:
            reasons.append(f"x{width} (expected x{width_cap})")
        elif isinstance(mark.get("width"), int) and width < mark["width"]:
            reasons.append(f"x{width} (x{mark['width']} on an earlier passing check)")
    if speed is not None and judge_speed:
        if speed_cap is not None and speed < speed_cap:
            reasons.append(f"{speed:g} GT/s (expected {speed_cap:g} GT/s)")
        elif isinstance(mark.get("speed"), (int, float)) and speed < mark["speed"]:
            reasons.append(f"{speed:g} GT/s ({mark['speed']:g} GT/s on an earlier passing check)")
    return ", ".join(reasons)


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


# The host's 1G hugepage pool. A PCI function behind an identity-mapped (passthrough) IOMMU, or no
# IOMMU at all, cannot be opened until its 1G hugepage exists: UMD pins the host-side buffers there,
# one page per function (not per chip: an n300 or T3K card carries two chips behind one function).
# Boot allocates them in tenstorrent-hugepages.service, late (after multi-user.target), so a broker
# that starts first sees too few, and any fabric pass it runs before then exits 77.
HUGEPAGES_1G_NR = Path("/sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages")


def _functions_needing_hugepages() -> int:
    """How many Tenstorrent functions sit behind an identity IOMMU domain or none at all.

    A translated domain (DMA, DMA-FQ) maps the device's buffers through the IOMMU and needs no
    hugepage; a host with no Tenstorrent function on the bus has nothing to wait for."""
    n = 0
    for bdf in tt_functions():
        group = pci.PCI_DEVICES_DIR / bdf / "iommu_group"
        if not group.exists() or (_read(group / "type") or "") == "identity":
            n += 1
    return n


def hugepages_shortfall() -> Optional[tuple[int, int]]:
    """``(have, need)`` when fewer 1G hugepages are allocated than the functions that need one.

    None when there is nothing to wait for: every function behind a translating IOMMU, none on the
    bus, or a kernel with no 1G hugepage pool to read (an unreadable pool is not evidence of a short
    one, and waiting on it would hold a box that may not use hugepages at all). Sysfs reads only."""
    need = _functions_needing_hugepages()
    if need <= 0:
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
