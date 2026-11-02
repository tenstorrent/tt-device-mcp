# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Read-only BMC/PCIe evidence at a tray-down onset (spec 04 I19, issue #26).

The reset ladder that follows a tray-down onset (and the power cycle, when the ladder fails)
erases the state that explains the drop: the BMC's SEL and sensors, the bridge's link status,
the kernel's account of the drop. So the prelude copies them first, concurrently, under one
deadline, and fsyncs them to disk before the ladder's first rung.

Read-only by construction: every argv is checked against a fixed shape allow-list before it is
spawned; no ``ipmitool raw`` command passes.

The last tt-smi telemetry is deliberately not read again here: the gate's own ``unhealthy``
incident bundle, written just before, holds the sampler's telemetry ring, and the ``tray_down``
bundle's ``incident.json`` holds the live per-chip PCI snapshot.
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
# A BMC answers IPMI slowly while a tray is browning out; each read gets most of the deadline,
# and all of them run at once, so the capture still ends within CAPTURE_DEADLINE_SEC.
BMC_CALL_TIMEOUT_SEC = 8.0
CALL_TIMEOUT_SEC = 2.0
JOURNAL_TIMEOUT_SEC = 3.0
_IPMI_FIXED = (
    ["ipmitool", "sel", "elist", "last", "40"],
    ["ipmitool", "sdr", "elist"],
    ["ipmitool", "mc", "info"],
)

# The spawner, one name the test suite's isolation can replace (spec 09 I1: no real subprocess).
_RUN: Callable = subprocess.run

_PCI_ADDR = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def fallback_bundle() -> Path:
    """A bundle directory of its own for the capture when no incident bundle was written (incident
    capture switched off, or it failed), so the BMC reads are never dropped for want of one."""
    from tt_device_mcp.health import evidence

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return evidence.HEALTH_DIR / evidence.INCIDENTS_DIR / f"{stamp}_tray_down_bmc"


def argv_allowed(argv: list) -> bool:
    """Whether ``argv`` is one of the read-only shapes this module may spawn. The only gate between
    a config value and a BMC command, so it is shape-exact rather than a prefix match."""
    if not argv:
        return False
    a = list(argv)
    if any(a == fixed for fixed in _IPMI_FIXED):
        return True
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


def capture_argvs(pci_targets: list, onset_epoch: float) -> list:
    """Every argv the capture runs."""
    argvs: list = [list(a) for a in _IPMI_FIXED]
    for target in pci_targets:
        argvs.append(["lspci", "-s", target, "-vv"])
    argvs.append(["journalctl", "-k", f"--since=@{max(0, int(onset_epoch) - 60)}", "--no-pager", "-q"])
    return [a for a in argvs if argv_allowed(a)]


def _run_one(argv: list, run: Callable) -> str:
    timeout = {"journalctl": JOURNAL_TIMEOUT_SEC, "ipmitool": BMC_CALL_TIMEOUT_SEC}.get(argv[0], CALL_TIMEOUT_SEC)
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
    all_trays: Optional[list] = None,
    deadline_sec: float = CAPTURE_DEADLINE_SEC,
    run: Optional[Callable] = None,
) -> dict:
    """Run every allowed read concurrently, stop waiting at ``deadline_sec``, and write ``bmc.txt``
    and ``onset.json`` into ``bundle`` (fsync'd). Never raises; returns a summary for the journal.
    A read still running at the deadline is recorded as such and abandoned (its own per-call timeout
    reaps it shortly after)."""
    run = run or _RUN
    t0 = time.monotonic()
    argvs = capture_argvs(pci_targets, onset.get("epoch", time.time()))
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
        "trays": list(trays),
        "all_trays": list(all_trays or []),
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
