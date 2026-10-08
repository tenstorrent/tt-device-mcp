#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Bring every local Blackhole chip's AICLK ceiling to at most --mhz, and prove it.

For each /dev/tenstorrent/N: open the chip with tt-umd, read telemetry AICLK_ARB_MAX; if it reads
above the ceiling, send SET_ASIC_HOST_FMAX (SMC message 0x23, args [mhz, 0]) and read again. A chip
already at or below the ceiling is never sent anything, so another cap holder with the same or a
lower value is left alone.

Prints one JSON line per chip and a final {"summary": true, ...} line. Exit codes:
  0  every chip reads AICLK_ARB_MAX <= mhz
  1  a chip is still above it, or could not be opened, read or sent to
  77 the check cannot run here (tt-umd missing or without AICLK_ARB_MAX, not a Blackhole chip)
``--check`` reads only and never sends. Run by the broker (tt_device_mcp.health.aiclk_ceiling)
with the broker venv's python.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading

SET_ASIC_HOST_FMAX = 0x23
UNSUPPORTED_RC = 77


def _chip_ids() -> list[int]:
    return sorted(int(p.rsplit("/", 1)[1]) for p in glob.glob("/dev/tenstorrent/[0-9]*"))


def _arb_max(umd, dev) -> int | None:
    tel = dev.get_arc_telemetry_reader()
    tag = umd.TelemetryTag.AICLK_ARB_MAX
    if not tel.is_entry_available(tag):
        return None
    return tel.read_entry(tag) & 0xFFFF


def check_chip(umd, chip: int, mhz: int, send: bool) -> dict:
    """One chip, never raising: {chip, before, after, sent, ok} plus an error or unsupported key."""
    rec: dict = {"chip": chip, "before": None, "after": None, "sent": False, "ok": False}
    try:
        dev = umd.TTDevice.create(chip)
        dev.init_tt_device()
        if dev.get_arch() != umd.ARCH.BLACKHOLE:
            rec["unsupported"] = f"arch {dev.get_arch()}"
            return rec
        before = _arb_max(umd, dev)
        rec["before"] = rec["after"] = before
        if before is None:
            rec["unsupported"] = "AICLK_ARB_MAX not available"
            return rec
        if before > mhz and send:
            exit_code = dev.arc_msg(SET_ASIC_HOST_FMAX, True, [mhz, 0], 1000)[0]
            rec["sent"] = True
            if exit_code != 0:
                rec["error"] = f"arc_msg exit {exit_code}"
            rec["after"] = _arb_max(umd, dev)
        rec["ok"] = rec["after"] is not None and rec["after"] <= mhz and "error" not in rec
    except Exception as e:  # noqa: BLE001 - one bad chip must not stop the others
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


def _with_timeout(fn, timeout: float):
    """Run fn in a daemon thread; None if it did not finish. A hung chip open cannot be cancelled,
    so the caller stops at the first one rather than queueing more behind it."""
    box: list = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else None


def run(umd, chips: list[int], mhz: int, send: bool, chip_timeout: float, out=sys.stdout) -> int:
    recs = []
    hung = None
    for chip in chips:
        rec = _with_timeout(lambda c=chip: check_chip(umd, c, mhz, send), chip_timeout)
        if rec is None:
            hung = chip
            rec = {"chip": chip, "ok": False, "error": f"no answer within {chip_timeout:g}s"}
        recs.append(rec)
        print(json.dumps(rec), file=out, flush=True)
        if hung is not None:
            break
    unsupported = [r for r in recs if r.get("unsupported")]
    summary = {
        "summary": True,
        "mhz": mhz,
        "chips": len(chips),
        "ok": sum(1 for r in recs if r["ok"]),
        "sent": sum(1 for r in recs if r.get("sent")),
        "over": [r["chip"] for r in recs if r.get("after") is not None and r["after"] > mhz],
        "unreadable": [r["chip"] for r in recs if r.get("error")],
    }
    print(json.dumps(summary), file=out, flush=True)
    if unsupported and len(unsupported) == len(recs):
        return UNSUPPORTED_RC
    if not chips or hung is not None or summary["ok"] != len(chips):
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mhz", type=int, default=int(os.environ.get("TT_DEVICE_MCP_AICLK_CEILING_MHZ", "0") or 0))
    p.add_argument("--check", action="store_true", help="read only, never send")
    p.add_argument("--chip-timeout", type=float, default=2.0)
    a = p.parse_args(argv)
    if a.mhz <= 0:
        print("aiclk-ceiling: --mhz must be a positive integer", file=sys.stderr)
        return 2
    try:
        import tt_umd as umd
    except ImportError as e:
        print(json.dumps({"summary": True, "mhz": a.mhz, "error": f"tt_umd not importable: {e}"}), flush=True)
        return UNSUPPORTED_RC
    if "AICLK_ARB_MAX" not in getattr(umd.TelemetryTag, "__members__", {}):
        print(json.dumps({"summary": True, "mhz": a.mhz, "error": "this tt-umd has no AICLK_ARB_MAX"}), flush=True)
        return UNSUPPORTED_RC
    rc = run(umd, _chip_ids(), a.mhz, not a.check, a.chip_timeout)
    # A daemon thread still stuck in a chip open must not keep the process alive.
    sys.stdout.flush()
    os._exit(rc)


if __name__ == "__main__":
    sys.exit(main())
