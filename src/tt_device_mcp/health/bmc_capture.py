# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Read-only BMC/CPLD/PCIe evidence at a tray-down onset (spec 04 I18, issue #26).

A tray that leaves the bus is power-cycled within seconds of the first sighting, and the power
cycle erases the one state that explains it: the tray CPLD's power-good and fault latches, the
BMC's SEL, the bridge's link status, the kernel's account of the drop. So the fast path copies
them first, concurrently, under one deadline, and fsyncs them into the incident bundle before
anything is power-cycled.

Read-only by construction: every argv is checked against a fixed shape allow-list before it is
spawned, and a CPLD read is ``ipmitool raw 0x06 0x52 <bus> <addr> 0x01 <reg>`` (master
write-read, one register byte selected, one byte read). Nothing else of ``ipmitool raw`` passes.
The bus, address and register numbers are site data, never shipped in code: they come from
``TT_DEVICE_MCP_TRAY_CPLD_BUSES``/``_ADDR``/``_REGS``, and without all three the CPLD reads are
skipped (journalled) while the SEL, lspci and kernel-log reads still run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp.health.monitors import pci

CAPTURE_DEADLINE_SEC = 10.0
CALL_TIMEOUT_SEC = 2.0
JOURNAL_TIMEOUT_SEC = 3.0

_HEX_BYTE = re.compile(r"^0x[0-9a-fA-F]{1,2}$")
_PCI_ADDR = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def cpld_config() -> Optional[tuple[dict, str, list]]:
    """``({tray: i2c bus}, cpld address, [registers])`` from the broker's environment, or None when
    any of the three is unset or malformed. All-or-nothing: a partial config would read some
    registers on some trays and look like a complete capture."""
    raw_buses = os.environ.get("TT_DEVICE_MCP_TRAY_CPLD_BUSES", "").strip()
    addr = os.environ.get("TT_DEVICE_MCP_TRAY_CPLD_ADDR", "").strip()
    raw_regs = os.environ.get("TT_DEVICE_MCP_TRAY_CPLD_REGS", "").strip()
    if not (raw_buses and addr and raw_regs) or not _HEX_BYTE.match(addr):
        return None
    buses: dict = {}
    try:
        for item in raw_buses.split(","):
            tray, bus = item.split(":")
            if not _HEX_BYTE.match(bus.strip()):
                return None
            buses[int(tray)] = bus.strip()
    except ValueError:
        return None
    regs = [r.strip() for r in raw_regs.split(",")]
    if not buses or not all(_HEX_BYTE.match(r) for r in regs):
        return None
    return buses, addr, regs


def argv_allowed(argv: list) -> bool:
    """Whether ``argv`` is one of the read-only shapes this module may spawn. The only gate between
    a config value and a BMC command, so it is shape-exact rather than a prefix match."""
    if not argv:
        return False
    a = list(argv)
    if a == ["ipmitool", "sel", "elist", "last", "40"]:
        return True
    if len(a) == 8 and a[:2] == ["ipmitool", "raw"]:
        netfn, cmd, bus, addr, count, reg = a[2:]
        return (
            netfn == "0x06" and cmd == "0x52" and count == "0x01" and all(_HEX_BYTE.match(x) for x in (bus, addr, reg))
        )
    if len(a) == 4 and a[0] == "lspci" and a[1] == "-s" and a[3] == "-vv":
        return bool(_PCI_ADDR.match(a[2]))
    if len(a) == 5 and a[:2] == ["journalctl", "-k"] and a[2].startswith("--since=@") and a[3:] == ["--no-pager", "-q"]:
        return a[2][len("--since=@") :].isdigit()
    return False


def upstream_bridge(bdf: str) -> Optional[str]:
    """The bridge or root port above ``bdf``: the parent of its sysfs node while the endpoint is
    still enumerated, else the bridge whose secondary bus is the endpoint's bus. The bridge stays
    on the bus when the endpoint drops, so its LnkSta/LnkCap still say what the link did. A sysfs
    read only; None when neither finds one."""
    from tt_device_mcp.health.recovery.stages.bridge_reset import find_bridge_by_secondary_bus

    if not _PCI_ADDR.match(bdf or ""):
        return None
    node = pci.PCI_DEVICES_DIR / bdf
    if node.exists():
        try:
            parent = node.resolve().parent.name
        except OSError:
            parent = ""
        if _PCI_ADDR.match(parent):
            return parent
    return find_bridge_by_secondary_bus(bdf.lower())


def lspci_targets(addrs: list) -> list:
    """The lspci targets for the off chips' recorded PCI addresses: each one's upstream bridge, and
    the endpoint too while it is still enumerated. Deduplicated, in order."""
    out: list = []
    for addr in addrs:
        if not _PCI_ADDR.match(addr or ""):
            continue
        endpoint = addr if (pci.PCI_DEVICES_DIR / addr).exists() else None
        for target in (upstream_bridge(addr), endpoint):
            if target and target not in out:
                out.append(target)
    return out


def capture_argvs(trays: list, pci_targets: list, onset_epoch: float) -> tuple[list, bool]:
    """Every argv the capture runs, and whether the CPLD reads were included (config present)."""
    argvs: list = [["ipmitool", "sel", "elist", "last", "40"]]
    cfg = cpld_config()
    if cfg is not None:
        buses, addr, regs = cfg
        for tray in trays:
            bus = buses.get(tray)
            if bus is None:
                continue
            for reg in regs:
                argvs.append(["ipmitool", "raw", "0x06", "0x52", bus, addr, "0x01", reg])
    for target in pci_targets:
        argvs.append(["lspci", "-s", target, "-vv"])
    argvs.append(["journalctl", "-k", f"--since=@{max(0, int(onset_epoch) - 60)}", "--no-pager", "-q"])
    return [a for a in argvs if argv_allowed(a)], cfg is not None


def _run_one(argv: list, run: Callable) -> str:
    timeout = JOURNAL_TIMEOUT_SEC if argv[0] == "journalctl" else CALL_TIMEOUT_SEC
    try:
        p = run(argv, capture_output=True, text=True, timeout=timeout)
        return f"exit {p.returncode}\n{p.stdout or ''}{p.stderr or ''}"
    except FileNotFoundError:
        return "not installed"
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001 - one failed read must not lose the others
        return f"error: {e!r}"


def _fsync_tree(root: Path) -> None:
    for f in [*root.iterdir(), root]:
        try:
            fd = os.open(f, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def capture_tray_down(
    bundle: Optional[Path],
    onset: dict,
    *,
    trays: list,
    pci_targets: list,
    deadline_sec: float = CAPTURE_DEADLINE_SEC,
    run: Callable = subprocess.run,
) -> dict:
    """Run every allowed read concurrently, stop waiting at ``deadline_sec``, and write ``bmc.txt``
    and ``onset.json`` into ``bundle`` (fsync'd). Never raises; returns a summary for the journal.
    A read still running at the deadline is recorded as such and abandoned (its own per-call timeout
    reaps it shortly after)."""
    t0 = time.monotonic()
    argvs, cpld = capture_argvs(trays, pci_targets, onset.get("epoch", time.time()))
    results: dict = {}
    pool = ThreadPoolExecutor(max_workers=max(1, len(argvs)))
    try:
        futs = {pool.submit(_run_one, a, run): " ".join(a) for a in argvs}
        done, _ = wait(futs, timeout=deadline_sec)
        for fut, key in futs.items():
            results[key] = fut.result() if fut in done else f"not finished within the {deadline_sec}s deadline"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    summary = {
        "reads": len(argvs),
        "finished": sum(1 for v in results.values() if "deadline" not in v),
        "cpld": "read" if cpld else "skipped (no TT_DEVICE_MCP_TRAY_CPLD_* config)",
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "bundle": str(bundle) if bundle else None,
    }
    if bundle is not None:
        try:
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "bmc.txt").write_text("".join(f"$ {k}\n{v}\n\n" for k, v in results.items()))
            (bundle / "onset.json").write_text(json.dumps({**onset, "capture": summary}, indent=2, default=str))
            _fsync_tree(bundle)
        except OSError as e:
            summary["write_error"] = repr(e)
    return summary
