# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Reset a chip by writing to its parent PCI bridge, never to the chip itself."""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Optional

from tt_device_mcp import privileges
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors import pci

_BDF_RE = r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]"
# After a Secondary Bus Reset release the link must retrain before a rescan reads config space
# (PCIe: at least 100 ms); every SBR here waits this long before its rescan.
SBR_RETRAIN_SETTLE_SEC = 1.0


def bridge_reset_enabled() -> bool:
    """Whether this process can issue a Secondary Bus Reset (spec 04 I17).

    A permission failure from setpci is indistinguishable at the call site from a bad bridge, and
    that reads as retryable — so the capability is decided here rather than discovered by running
    the write.
    """
    return privileges.can_setpci()


def bridge_reset_unavailable_reason() -> str:
    """Which half of root+setpci is missing. One source for the boot inventory and the runtime
    log, because they are read by the same operator and must not name different causes."""
    if not privileges.is_root():
        return "not root — setpci cannot write the parent bridge's config space"
    return "setpci is not on PATH — nothing to issue the Secondary Bus Reset with"


def gone_chip_bridge_reset_enabled() -> bool:
    """Whether reset_chip_via_bridge may reach an endpoint whose OWN sysfs node is gone by
    resolving its bridge from the secondary bus (see reset_chip_via_bridge). OFF by default: with
    it off the parent walk is the only path, so a node-less endpoint yields no bridge and no reset
    fires — exactly the prior behavior. It fires a Secondary Bus Reset on a class of drop the
    broker did not reset before, so it stays opt-in until validated on a reserved box.
    TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET=1 opts in."""
    return os.environ.get("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "0").strip() == "1"


def reset_chip_via_bridge(idx: str, bdf: str) -> Optional[bool]:
    """Reset a chip by writing to its PARENT BRIDGE, never to the chip itself.

    Tri-state so the caller can retry only when a retry can help: ``True`` the chip is back;
    ``False`` a Secondary Bus Reset was issued (or a setpci call failed mid-attempt) but the
    endpoint has not re-bound yet, so a settle-and-retry may still land it; ``None`` the rung
    could not be issued AT ALL — no bridge to write to (the endpoint left the bus) or setpci is
    gone — so it structurally cannot work here and a retry only re-runs the identical no-op.

    A dead endpoint cannot answer, so it cannot be reset through its own config space — but
    it does not have to be. A Secondary Bus Reset is a write to the bridge above it, and the
    bridge is always alive. That is what lets a chip that has stopped responding be reset at
    all. On this hardware each Blackhole sits alone behind its own bridge, so the reset is
    surgical: the other 31 chips never see it, and the inter-chip fabric survives (measured
    — a traffic pass still passes afterwards).

    Reaches the bridge three ways. A chip still enumerated has its own sysfs node, so its bridge
    is the parent of that node. A chip the broker removed itself (isolate_chip — the all-ones
    chips, which is how this rung normally finds them) has no node any more: its bridge is the one
    isolate_chip recorded before the remove, used only while that record still holds (spec 04
    I69, recorded_bridge). One that left the bus on its own has no node and no record — so, WHEN
    OPTED IN, fall back to finding the bridge by the secondary bus it still points at. With the
    opt-in off such an endpoint yields no bridge and this returns without firing a reset. Rescans
    the bus afterwards to bring the endpoint back.
    """
    bridge = pci.parent_bridge(bdf)
    if not bridge and pci.removed_chip(idx):
        bridge = recorded_bridge(idx)
    if not bridge and gone_chip_bridge_reset_enabled():
        bridge = find_bridge_by_secondary_bus(bdf)
    if not bridge or not re.fullmatch(_BDF_RE, bridge):
        # No bridge to write to — a topology fact (a node-less endpoint with the opt-in off is
        # the deliberate no-op), not a broken capability. Still journal it: the gentlest rung
        # not firing is never silent, but reason=no_bridge reads apart from a setpci that was
        # present and failed. None, not False: nothing fired, so a retry re-resolves the same
        # absent bridge and no-ops again — the caller must fall to the next rung, not spin here.
        health_event("bridge_reset_failed", chip=idx, bdf=bdf, reason="no_bridge")
        return None
    fired = secondary_bus_reset(idx, bridge)
    if not fired:
        return fired
    try:
        time.sleep(SBR_RETRAIN_SETTLE_SEC)
        from tt_device_mcp.health.recovery import pcie_guard  # the package imports this module

        pcie_guard.rescan_tt_root_ports()
        # The endpoint has to re-enumerate and the driver rebind before it is usable.
        for _ in range(20):
            time.sleep(0.5)
            if (pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}").exists():
                pci.forget_removed_chip(idx)
                return True
        health_event("bridge_reset_failed", chip=idx, bridge=bridge, reason="endpoint_not_reenumerated")
        return False
    except OSError as e:
        health_event("bridge_reset_failed", chip=idx, bridge=bridge, reason="setpci_write_error", error=str(e))
        return False


def reset_removed_chip(idx: str) -> Optional[bool]:
    """Fire the Secondary Bus Reset of a chip the broker removed, through its recorded bridge, and
    do NOT rescan: the caller resets every removed chip of the pass first, waits
    SBR_RETRAIN_SETTLE_SEC once and rescans once, so no rescan re-enumerates a removed chip that is
    still hung (spec 04 I69). ``True`` the reset fired,
    ``False`` a setpci write failed, ``None`` no usable bridge or no setpci."""
    bridge = recorded_bridge(idx)
    if not bridge:
        rec = pci.removed_chip(idx) or {}
        health_event("bridge_reset_failed", chip=idx, bdf=rec.get("bdf"), reason="no_bridge")
        return None
    return secondary_bus_reset(idx, bridge)


def recorded_bridge(idx: str) -> Optional[str]:
    """The bridge isolate_chip recorded for chip ``idx`` before removing it, or None.

    Used only while the record still describes the bus: the bridge is still enumerated, its
    secondary bus is the removed chip's bus and nothing is enumerated on that bus. A galaxy reset or
    rescan since the remove can renumber buses, and a Secondary Bus Reset through a bridge that now
    feeds live silicon would knock out a chip that never dropped — the same refusal as
    find_bridge_by_secondary_bus. A refused record is journaled; the chip then holds and escalates.
    """
    rec = pci.removed_chip(idx)
    if not rec:
        return None
    bridge, bdf = rec.get("bridge"), rec.get("bdf") or ""
    m = re.fullmatch(r"([0-9a-f]{4}):([0-9a-f]{2}):[0-9a-f]{2}\.[0-9a-f]", bdf)
    why = None
    if not bridge or not re.fullmatch(_BDF_RE, bridge) or not m:
        why = "no_recorded_bridge"
    elif not (pci.PCI_DEVICES_DIR / bridge).exists():
        why = "bridge_gone"
    else:
        try:
            sec = int((pci.PCI_DEVICES_DIR / bridge / "secondary_bus_number").read_text().strip(), 10)
        except (OSError, ValueError):
            sec = None
        if bridge[:4] != m.group(1) or sec != int(m.group(2), 16):
            why = "bridge_renumbered"
        else:
            try:
                live = [
                    d.name for d in pci.PCI_DEVICES_DIR.iterdir() if d.name.startswith(f"{m.group(1)}:{m.group(2)}:")
                ]
            except OSError:
                live = ["unreadable"]
            if live:
                why = "bus_occupied"
    if why:
        health_event("recorded_bridge_refused", chip=idx, bdf=bdf, bridge=bridge, reason=why)
        return None
    return bridge


def secondary_bus_reset(idx: str, bridge: str) -> Optional[bool]:
    """Assert, hold and release Secondary Bus Reset on ``bridge``. ``True`` it fired; ``False`` a
    setpci call failed (journaled); ``None`` setpci is not on PATH."""
    try:
        cur = subprocess.run(["setpci", "-s", bridge, "BRIDGE_CONTROL"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        # setpci is gone: the gentlest rung is dead for EVERY chip. The caller normally catches
        # this ahead of the loop (bridge_reset_enabled), so reaching here means it went missing
        # after that check — mid-run, which is why this stays loud. None, not False: the binary
        # will not reappear between retries, so the rung cannot fire — fall through, don't spin.
        health_event("bridge_reset_failed", chip=idx, bridge=bridge, reason="setpci_missing", host_at_risk=True)
        return None
    except (OSError, subprocess.SubprocessError) as e:
        health_event("bridge_reset_failed", chip=idx, bridge=bridge, reason="setpci_read_error", error=str(e))
        return False
    if cur.returncode != 0:
        # setpci ran but could not read this bridge's control word — a permission or bad-bridge
        # failure specific to this attempt, distinct from the binary being absent. Journal the rc
        # and bridge so it reads as a real setpci failure, not the same quiet give-up as no_bridge.
        health_event(
            "bridge_reset_failed",
            chip=idx,
            bridge=bridge,
            reason="setpci_read_rc",
            rc=cur.returncode,
            stderr=(cur.stderr or "").strip()[:200],
        )
        return False
    try:
        val = int(cur.stdout.strip(), 16)
        # Bit 6 of BRIDGE_CONTROL is Secondary Bus Reset: assert, hold, release.
        asserted = subprocess.run(
            ["setpci", "-s", bridge, f"BRIDGE_CONTROL={val | 0x40:04x}"], capture_output=True, text=True, timeout=15
        )
        if asserted.returncode != 0:
            # Nothing was reset. Without this the miss surfaces later as
            # endpoint_not_reenumerated, which names the wrong cause and sends the reader
            # looking at the endpoint instead of the write that never landed.
            health_event(
                "bridge_reset_failed",
                chip=idx,
                bridge=bridge,
                reason="setpci_assert_rc",
                rc=asserted.returncode,
                stderr=(asserted.stderr or "").strip()[:200],
            )
            return False
        time.sleep(0.1)
        released = subprocess.run(
            ["setpci", "-s", bridge, f"BRIDGE_CONTROL={val & ~0x40:04x}"], capture_output=True, text=True, timeout=15
        )
        if released.returncode != 0:
            # Secondary Bus Reset is still asserted, so EVERY device behind this bridge stays
            # held in reset — a worse outage than the one dead chip this rung came to fix, and
            # setpci is the only lever we have. host_at_risk so it cannot read as a routine miss.
            health_event(
                "bridge_reset_failed",
                chip=idx,
                bridge=bridge,
                reason="setpci_release_rc",
                rc=released.returncode,
                stderr=(released.stderr or "").strip()[:200],
                host_at_risk=True,
            )
            return False
        return True
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        health_event("bridge_reset_failed", chip=idx, bridge=bridge, reason="setpci_write_error", error=str(e))
        return False


def find_bridge_by_secondary_bus(bdf: str) -> Optional[str]:
    """The BDF of the PCI bridge whose secondary bus is this chip's bus, or None.

    reset_chip_via_bridge() reaches the bridge by walking the chip's own sysfs node up
    to its parent. A chip still enumerated HAS that node; one that has fully left the
    bus (or that the broker removed) does not, and the parent link is gone with it. The bridge above
    it never moves — it stays enumerated with secondary_bus_number still pointing at the
    now-empty bus — so matching that attribute finds the bridge to reset without ever
    touching the vanished endpoint. Bus numbers repeat across PCI domains, so the domain
    must match too. secondary_bus_number is decimal in sysfs; a BDF's bus field is hex.

    The address passed here is cached at broker start and never refreshed, so a galaxy
    reset or rescan that renumbered buses since can leave it pointing at a bus that OTHER
    live silicon now occupies. A chip that truly left the bus leaves it EMPTY; a device
    still enumerated ON the target bus means the address is stale and the bridge above it
    now feeds a different, live chip. Refuse in that case — a Secondary Bus Reset there
    would knock out silicon that never dropped. The chip then holds and escalates to a
    heavier reset, which is recoverable; an errant SBR of a live neighbor is not.
    """
    m = re.fullmatch(r"([0-9a-f]{4}):([0-9a-f]{2}):[0-9a-f]{2}\.[0-9a-f]", bdf or "")
    if not m:
        return None
    domain, target_bus = m.group(1), int(m.group(2), 16)
    try:
        entries = sorted(pci.PCI_DEVICES_DIR.iterdir())
    except OSError:
        return None
    bridge = None
    for dev in entries:
        dm = re.fullmatch(r"([0-9a-f]{4}):([0-9a-f]{2}):[0-9a-f]{2}\.[0-9a-f]", dev.name)
        if not dm or dm.group(1) != domain:
            continue
        if int(dm.group(2), 16) == target_bus:
            return None  # live silicon on the bus this chip supposedly left — the address is stale
        try:
            sec = (dev / "secondary_bus_number").read_text().strip()
        except OSError:
            continue  # an endpoint, not a bridge — it has no secondary bus
        try:
            if int(sec, 10) == target_bus:
                bridge = dev.name
        except ValueError:
            continue
    return bridge
