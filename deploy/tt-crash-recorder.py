#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Record what the firmware says killed this machine, once per boot.

Standalone and stdlib-only, deliberately: it has to run on hosts that have no broker, no
venv, and nothing else installed — which is exactly where the useful control data is. A
host we never touched crashing the same way as the ones we manage is the single most
informative measurement available, and it is only obtainable by instrumenting a box we do
not otherwise change.

BERT (the Boot Error Record Table) is the firmware's account of the fatal error that ended
the previous boot. It is readable ONLY from the current boot's dmesg, and the next reboot
overwrites it — so a record not taken at startup is a crash nobody will ever explain, which
is why none of these have been.

It also sums the bus locks counted during the boot that just ended. That is the
discriminator between the two live theories, which produce identical error records:

  * AMD erratum 1431 hangs a core only when a BUS LOCK occurs. A crash whose boot recorded
    zero bus locks cannot be erratum 1431.
  * A core stalled on an MMIO access to a hung accelerator needs no bus lock at all: the
    access never completes, the instruction never retires, and the core's watchdog fires.
    It leaves no PCIe error record either, because the platform denies the OS AER control.

Writes JSONL to HEALTH_DIR, in the same schema the broker uses, so hosts with and without a
broker land in one dataset.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HEALTH_DIR = Path(os.environ.get("TT_DEVICE_MCP_HEALTH_DIR", "/var/lib/tt-device-broker/health"))
EVENTS_FILE = "health_events.jsonl"
BUSLOCK_FILE = "buslock.log"
BOOT_MARK = "#BOOT "

HW_ERR = re.compile(r"\[Hardware Error\]:\s*(.*)")
BERT_START = re.compile(r"BERT: Error records from previous boot", re.I)
# The kernel prints the raw MCA status separately from the CPER decode, and it is the only
# line that says which hardware block actually failed. The CPER "Error Structure Type" is
# firmware's label and it is misleading — on this silicon it reports "cache error" for what
# is really an execution-unit watchdog timeout.
MCE_RAW = re.compile(r"CPU (\d+): Machine Check: \d+ Bank (\d+): ([0-9a-f]+)")


def kernel_log() -> str:
    """This boot's kernel log, from journald rather than dmesg.

    dmesg reads a fixed-size ring that WRAPS. On a box with a day of uptime the boot-time
    messages are already gone — including BERT, which is printed in the first seconds and
    never again. Reading dmesg would therefore report "no crash record" on exactly the
    hosts that have been up long enough to be worth asking about, which is the failure mode
    this whole tool exists to avoid. journald keeps the entire boot.
    """
    for argv in (["journalctl", "-k", "-b", "0", "--no-pager", "-o", "short"], ["dmesg"]):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=45)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def previous_boot_error(log: str) -> dict:
    lines = log.splitlines()
    start = next((i for i, ln in enumerate(lines) if BERT_START.search(ln)), None)
    rec: dict = {"present": start is not None}
    if start is None:
        return rec

    raw = []
    for ln in lines[start + 1 :]:
        m = HW_ERR.search(ln)
        if not m:
            if raw:
                break
            continue
        raw.append(m.group(1).strip())

    for ln in raw:
        if ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        key = k.strip().lower().replace(" ", "_")
        if key in (
            "event_severity",
            "fru_text",
            "section_type",
            "error_structure_type",
            "uncorrected",
            "processor_context_corrupt",
            "local_apic_id",
            "msr_address",
            "check_information",
        ):
            rec[key] = v.strip()

    rec["is_pcie"] = any("pcie" in ln.lower() and "section_type" in ln.lower() for ln in raw)
    rec["is_processor"] = any("processor error" in ln.lower() for ln in raw)

    # The raw status is what actually identifies the failing block; keep it verbatim so the
    # decode can be redone later without the machine.
    m = MCE_RAW.search(log)
    if m:
        rec["mce_cpu"] = int(m.group(1))
        rec["mce_bank"] = int(m.group(2))
        rec["mce_status"] = m.group(3)
    return rec


def previous_boot_bus_locks() -> "int | None":
    """Bus locks counted during the boot that just ended, or None if nothing counted them."""
    try:
        lines = (HEALTH_DIR / BUSLOCK_FILE).read_text(errors="replace").splitlines()
    except OSError:
        return None
    last_mark = max((i for i, ln in enumerate(lines) if ln.startswith(BOOT_MARK)), default=-1)
    total, seen = 0, False
    for ln in lines[last_mark + 1 :]:
        parts = ln.split()
        if len(parts) >= 3 and "ls_locks" in parts[-1]:
            try:
                total += int(parts[-2].replace(",", ""))
                seen = True
            except ValueError:
                continue
    return total if seen else None


def append(path: Path, line: str) -> None:
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def main() -> int:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = ""
    marker = HEALTH_DIR / f".boot_recorded_{boot_id or 'unknown'}"
    if boot_id and marker.exists():
        return 0  # already recorded this boot; a restart must not double-count a crash

    log = kernel_log()
    rec = previous_boot_error(log)
    bus_locks = previous_boot_bus_locks()

    event = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "previous_boot_error",
        "host": os.uname().nodename,
        "boot_id": boot_id,
        "previous_boot_bus_locks": bus_locks,
        **rec,
    }
    append(HEALTH_DIR / EVENTS_FILE, json.dumps(event, default=str))
    append(HEALTH_DIR / BUSLOCK_FILE, f"{BOOT_MARK}{boot_id} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")

    if rec.get("present"):
        verdict = (
            "bus locks were counted, so erratum 1431 remains possible"
            if bus_locks
            else (
                "ZERO bus locks this boot -> erratum 1431 is REFUTED for this crash"
                if bus_locks == 0
                else "no bus-lock counter ran, so this crash cannot discriminate"
            )
        )
        print(
            f"tt-crash-recorder: previous boot ended in a {rec.get('event_severity')} "
            f"hardware error (bank {rec.get('mce_bank')}, status {rec.get('mce_status')}); "
            f"bus locks={bus_locks} -- {verdict}",
            file=sys.stderr,
        )
    else:
        print(
            "tt-crash-recorder: previous boot left no firmware error record " "(clean shutdown, or nothing logged)",
            file=sys.stderr,
        )

    try:
        marker.write_text(event["iso"])
        for old in HEALTH_DIR.glob(".boot_recorded_*"):
            if old != marker:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
