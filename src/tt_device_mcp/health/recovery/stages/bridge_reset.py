# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Reset a chip by writing to its parent PCI bridge, never to the chip itself."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from tt_device_mcp import privileges
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors import pci


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

    Reached the endpoint two ways. A chip that only reads all-ones still HAS its own sysfs
    node, so its bridge is the parent of that node. One that has fully left the bus does not —
    the node and the parent link are gone with it — so, WHEN OPTED IN, fall back to finding the
    bridge by the secondary bus it still points at; that is what lets a link-dropped chip be
    reset too, not only one that stayed enumerated. With the opt-in off the parent walk is the
    only path, so a node-less endpoint yields no bridge and this returns without firing a reset —
    the prior behavior. Rescans the bus afterwards to bring the endpoint back.
    """
    bdf_re = r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]"
    bridge = None
    try:
        bridge = (pci.PCI_DEVICES_DIR / bdf).resolve().parent.name
    except OSError:
        bridge = None
    if not re.fullmatch(bdf_re, bridge or ""):
        bridge = find_bridge_by_secondary_bus(bdf) if gone_chip_bridge_reset_enabled() else None
    if not bridge or not re.fullmatch(bdf_re, bridge):
        # No bridge to write to — a topology fact (a node-less endpoint with the opt-in off is
        # the deliberate no-op), not a broken capability. Still journal it: the gentlest rung
        # not firing is never silent, but reason=no_bridge reads apart from a setpci that was
        # present and failed. None, not False: nothing fired, so a retry re-resolves the same
        # absent bridge and no-ops again — the caller must fall to the next rung, not spin here.
        health_event("bridge_reset_failed", chip=idx, bdf=bdf, reason="no_bridge")
        return None
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
        time.sleep(1.0)
        Path("/sys/bus/pci/rescan").write_text("1")
        # The endpoint has to re-enumerate and the driver rebind before it is usable.
        for _ in range(20):
            time.sleep(0.5)
            if (pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}").exists():
                return True
        health_event("bridge_reset_failed", chip=idx, bridge=bridge, reason="endpoint_not_reenumerated")
        return False
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        health_event("bridge_reset_failed", chip=idx, bridge=bridge, reason="setpci_write_error", error=str(e))
        return False


def find_bridge_by_secondary_bus(bdf: str) -> Optional[str]:
    """The BDF of the PCI bridge whose secondary bus is this chip's bus, or None.

    reset_chip_via_bridge() reaches the bridge by walking the chip's own sysfs node up
    to its parent. A chip that only reads all-ones still HAS that node; one that has
    fully left the bus does not, and the parent link is gone with it. The bridge above
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
