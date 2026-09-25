# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Device health: the durable event journal, and forensic incident capture.

The journal is the forensic record of every device health decision. It is
append-only JSONL, fsync'd per record, under /var/lib (not /var/log, which is
rotated and shipped). It has to survive the exact events we want to debug: a
broker restart, a watchdog kill, an ungraceful host reset. If it is not on disk
the instant the decision is made, the reboot takes it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from tt_device_mcp.constants import user_state_dir
from tt_device_mcp.health.monitors import pci


def _default_health_dir() -> Path:
    """Where the journal lives absent ``TT_DEVICE_MCP_HEALTH_DIR``.

    ``/var/lib`` is root-only to create; the system broker is (systemd starts it as
    ``User=root``), but a per-user daemon (deploy/install-user.sh) is not, and would
    otherwise lose this journal's durability silently — every write already never
    raises (see ``_append_durable``), so nothing would look wrong until the restart
    this file exists to survive forgets the episode. A non-root process instead
    resolves under the same machine-local base every other per-user daemon state
    (socket, logs, stats) already uses — see ``constants.user_state_dir``.
    """
    override = os.environ.get("TT_DEVICE_MCP_HEALTH_DIR", "").strip()
    if override:
        return Path(override)
    if os.geteuid() == 0:
        return Path("/var/lib/tt-device-broker/health")
    return user_state_dir() / "health"


# Module-level, not a function, because tests monkeypatch this attribute directly
# (health_dir() below just reads it back) rather than patching a resolver.
HEALTH_DIR = _default_health_dir()
EVENTS_FILE = "health_events.jsonl"


def health_dir() -> Path:
    return HEALTH_DIR


# A journal that fills the disk stops being written to, which is a worse failure than
# the one it was built to survive. Each file is capped and rolled to a single `.1`, so
# the worst case on disk is bounded at 2x these numbers. The chip journal carries 32
# chips of telemetry per record and is allowed to be much larger than the decision log.
MAX_EVENTS_BYTES = 32 * 1024 * 1024
MAX_CHIPS_BYTES = 256 * 1024 * 1024


def _append_durable(path: Path, rec: dict, max_bytes: int) -> None:
    """Append one JSON record and fsync it.

    fsync per record is the entire contract: the events worth having are the ones
    written moments before the machine dies. Never raises — a journal that cannot be
    written must not take the broker down with it.
    """
    try:
        HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        # Roll before writing, so the cap holds even if the process dies mid-append.
        try:
            if path.stat().st_size >= max_bytes:
                path.replace(path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except (OSError, ValueError, TypeError):
        pass


def health_event(kind: str, **fields) -> None:
    """Append one decision to the durable journal."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind}
    rec.update(fields)
    _append_durable(HEALTH_DIR / EVENTS_FILE, rec, MAX_EVENTS_BYTES)


def read_health_events(since_ts: float | None = None, kinds: "set[str] | None" = None) -> list[dict]:
    """Records from the durable journal, oldest first.

    The journal is written on every device decision and fsync'd per record, precisely so it
    survives the reboot that follows. Reading it is what turns "your job was interrupted"
    into a reason, because the broker knows why — it recorded the chip going dead and the
    holder-kill that followed, and then answered the question with a shrug.

    Never raises: a report that cannot be produced must not take down the caller that only
    wanted a job list.
    """
    out: list[dict] = []
    # The current file, plus the one roll behind it: a busy hour can push a reboot's own
    # events into the roll, and those are the events most worth reading.
    for name in (EVENTS_FILE, EVENTS_FILE + ".1"):
        path = HEALTH_DIR / name
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a torn tail line is expected; skip it, keep the rest
                    if not isinstance(rec, dict):
                        continue
                    if kinds is not None and rec.get("kind") not in kinds:
                        continue
                    if since_ts is not None and float(rec.get("ts") or 0) < since_ts:
                        continue
                    out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: float(r.get("ts") or 0))
    return out


TRACE_FILE = "telemetry.jsonl"
MAX_TRACE_BYTES = 512 * 1024 * 1024


def append_trace(sample: dict) -> None:
    """Persist one telemetry sample as it is taken.

    The in-memory ring is enough to explain a failed JOB, because the broker survives to
    read it. It is worthless for explaining a REBOOT: the host goes down and takes the ring
    with it, which is why nobody has ever seen the seconds leading into one of these
    crashes. On disk, the last lines before the machine died are exactly that.

    This is what makes the next reboot decisive. If a chip's heartbeat freezes and the box
    dies moments later, a core stalled on a hung ASIC is the story. If every chip is ticking
    normally right up to the last sample, it is not — and the CPU erratum is.
    """
    _append_durable(HEALTH_DIR / TRACE_FILE, {"ts": time.time(), "chips": sample}, MAX_TRACE_BYTES)


INCIDENTS_DIR = "incidents"

# A bounded number of incident bundles, newest kept. An unbounded evidence directory is
# just a slower way of filling the disk.
MAX_INCIDENTS = 50


def _tail_text(path: Path, max_bytes: int) -> str:
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-max_bytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def kernel_log() -> str:
    """This boot's kernel log, from journald rather than dmesg.

    dmesg reads a fixed-size ring that WRAPS. On a box with a day of uptime the boot-time
    messages are long gone — including BERT, the firmware's record of what killed the
    previous boot, which is printed in the first seconds and never again. Reading dmesg
    therefore reports "no crash record" on exactly the hosts that have been up long enough
    to be interesting. journald keeps the whole boot.

    Falls back to dmesg where journald is unavailable, which is better than nothing.
    """
    for argv in (["journalctl", "-k", "-b", "0", "--no-pager", "-o", "short"], ["dmesg"]):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=45)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def _kernel_evidence(max_lines: int = 400) -> str:
    """The kernel's account of the same event, filtered to what matters here.

    This is the evidence that has been missing. The documented path from a wedged chip to
    an ungraceful host reboot is: the host keeps issuing MMIO at a dead endpoint, those
    transactions never complete, the root complex escalates to a fatal error, and
    firmware-first RAS resets the machine. Every step of that leaves a line in the kernel
    log — and the reboot then takes the kernel log with it. Copying it out at the moment
    of failure is the only way anyone gets to read it afterwards.
    """
    log = kernel_log()
    if not log:
        return "(kernel log unavailable)"
    pat = re.compile(
        r"pcieport|\bAER\b|aer_|GHES|APEI|Machine Check|\bMCE\b|mce:|tenstorrent|"
        r"Uncorrected|uncorrectable|correctable error|Hardware Error|NMI|PCIe Bus Error",
        re.I,
    )
    hits = [ln for ln in log.splitlines() if pat.search(ln)]
    return "\n".join(hits[-max_lines:])


_BERT_START = re.compile(r"BERT: Error records from previous boot", re.I)
_HW_ERR = re.compile(r"\[Hardware Error\]:\s*(.*)")
# The kernel prints the RAW machine-check status on its own line, separately from the CPER
# decode. That raw word is the only thing that says which hardware block actually failed —
# the CPER "Error Structure Type" is firmware's label, and on this silicon it reports
# "cache error" for what the raw status proves is an execution-unit watchdog timeout. A
# record without it preserves the misleading label and discards the truth.
_MCE_RAW = re.compile(r"CPU (\d+): Machine Check: \d+ Bank (\d+): ([0-9a-f]+)")


def previous_boot_error() -> dict:
    """What the firmware says killed the machine last time.

    BERT — the Boot Error Record Table — is how firmware hands the OS the fatal error
    that ended the previous boot. It is the only account of an ungraceful reboot that
    exists, since the reboot destroys the running kernel's log, and it is readable ONLY
    from this boot's dmesg: the next reboot replaces it. Nobody has been reading it, so
    every one of these crashes has gone unexplained.

    Capturing it at every broker start turns a box that reboots daily into a dataset:
    after a week there is a distribution of what actually kills these machines, instead
    of a theory. Returns {"present": False} on a clean boot — no BERT record means the
    firmware logged no fatal error, which is itself a fact worth keeping.
    """
    log = kernel_log()
    if not log:
        return {"present": False, "error": "kernel log unavailable"}

    lines = log.splitlines()
    start = next((i for i, ln in enumerate(lines) if _BERT_START.search(ln)), None)
    if start is None:
        return {"present": False}

    raw: list[str] = []
    for ln in lines[start + 1 :]:
        m = _HW_ERR.search(ln)
        if not m:
            # The record is a contiguous block of [Hardware Error] lines; the first line
            # that is not one ends it.
            if raw:
                break
            continue
        raw.append(m.group(1).strip())

    rec: dict = {"present": True, "raw": raw}
    # Pull out the fields that decide the question: is this a PCIe fault (the wedged-chip
    # theory) or a processor fault (something else entirely)?
    for ln in raw:
        if ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        key = k.strip().lower().replace(" ", "_")
        val = v.strip()
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
            rec[key] = val
    rec["is_pcie"] = any("pcie" in ln.lower() and "section_type" in ln.lower() for ln in raw)
    rec["is_processor"] = any("processor error" in ln.lower() for ln in raw)

    # Keep the raw status verbatim so the decode can be redone later, on a machine that no
    # longer exists, without trusting the label firmware chose.
    m = _MCE_RAW.search(log)
    if m:
        rec["mce_cpu"] = int(m.group(1))
        rec["mce_bank"] = int(m.group(2))
        rec["mce_status"] = m.group(3)
    rec["host"] = platform.node()
    return rec


BUSLOCK_FILE = "buslock.log"
_BOOT_MARK = "#BOOT "


def previous_boot_bus_locks() -> Optional[int]:
    """How many CPU bus locks happened during the boot that just ended.

    This is the discriminator between the two theories for why these hosts reboot. AMD
    erratum 1431 hangs a core only when a bus lock occurs; a core stalled on an MMIO access
    to a hung accelerator needs no bus lock at all. Both produce an identical EX-unit
    watchdog record, so the firmware's error record alone cannot tell them apart — but a
    crash with a lifetime bus-lock count of zero cannot be erratum 1431.

    The count is written to disk every minute by tt-device-buslock.service, because a perf
    session dies with the machine. Returns None if the counter was not running.
    """
    path = HEALTH_DIR / BUSLOCK_FILE
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None

    # Everything after the last boot marker belongs to the boot that just ended; the marker
    # is written by the first broker start of each boot.
    last_mark = max((i for i, ln in enumerate(lines) if ln.startswith(_BOOT_MARK)), default=-1)
    total = 0
    seen = False
    for ln in lines[last_mark + 1 :]:
        parts = ln.split()
        # perf -I format: "     1.000123456        12,345      ls_locks.bus_lock"
        if len(parts) >= 3 and "ls_locks" in parts[-1]:
            try:
                total += int(parts[-2].replace(",", ""))
                seen = True
            except ValueError:
                continue
    return total if seen else None


def mark_boot(boot_id: str) -> None:
    """Separate this boot's bus-lock counts from the previous boot's."""
    try:
        HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        with open(HEALTH_DIR / BUSLOCK_FILE, "a", encoding="utf-8") as f:
            f.write(f"{_BOOT_MARK}{boot_id} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _prune_incidents() -> None:
    root = HEALTH_DIR / INCIDENTS_DIR
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    except OSError:
        return
    for old in dirs[:-MAX_INCIDENTS] if len(dirs) > MAX_INCIDENTS else []:
        try:
            for f in old.iterdir():
                f.unlink()
            old.rmdir()
        except OSError:
            pass


def capture_incident(
    label: str,
    *,
    job: dict | None = None,
    job_log: Path | None = None,
    trace: list | None = None,
    reset_output: str = "",
    fabric_output: str = "",
    evidence: dict | None = None,
    job_log_bytes: int = 256 * 1024,
) -> Optional[Path]:
    """Freeze everything a root-cause investigation will want, at the moment it exists.

    The broker's decision was already being recorded; the evidence behind it was not. The
    job's own output, the kernel's account of the bus, the reset's full transcript, the
    fabric validator's full transcript, and the telemetry trace leading into the failure
    all lived somewhere volatile — a rotating log, a kernel ring buffer an ungraceful
    reboot erases, a truncated string in a JSON field. Each one is copied here, once,
    while it still exists.

    Never raises. Returns the bundle directory.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    jid = (job or {}).get("id", "none")
    root = HEALTH_DIR / INCIDENTS_DIR / f"{stamp}_{label}_{jid}"
    try:
        root.mkdir(parents=True, exist_ok=True)

        # The evidence lanes are the caller's verdicts, sampled BEFORE the fault was discovered —
        # they can lag the live bus (a heartbeat lane that read 8/8 seconds before a chip left).
        # The chips snapshot is taken NOW; recording its count next to the lanes keeps the bundle
        # internally honest instead of asserting two chip counts with no arbiter.
        chips = pci.chip_snapshot()
        meta = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "label": label,
            "job": job or {},
            "evidence": evidence or {},
            "evidence_note": "lanes sampled before capture; 'chips' is the live bus at capture time",
            "chips": chips,
            "chips_present_at_capture": len(chips),
        }
        (root / "incident.json").write_text(json.dumps(meta, indent=2, default=str))

        # The kernel's view — the piece that a reboot destroys and that nobody has had.
        (root / "kernel.log").write_text(_kernel_evidence())

        if trace:
            (root / "telemetry_trace.json").write_text(
                json.dumps(
                    {"fields": pci.SAMPLE_FIELDS, "interval_sec": pci.SAMPLE_INTERVAL_SEC, "samples": trace},
                    default=str,
                )
            )
        if job_log and job_log.exists():
            (root / "job.log").write_text(_tail_text(job_log, job_log_bytes))
        if reset_output:
            (root / "reset.log").write_text(reset_output)
        if fabric_output:
            (root / "fabric.log").write_text(fabric_output)

        for f in root.iterdir():
            try:
                fd = os.open(f, os.O_RDONLY)
                os.fsync(fd)
                os.close(fd)
            except OSError:
                pass
        _prune_incidents()
        return root
    except (OSError, ValueError, TypeError) as e:
        # The forensic bundle for a host-killing incident could not be written — the one record a
        # post-mortem needs is the one we failed to keep. Do not vanish: drop a distinct sentinel in
        # the decision log so the GAP itself is on the record, greppable, instead of an unexplained
        # absence of a bundle. Still never raises.
        health_event("incident_capture_failed", label=label, job=jid, error=repr(e), host_at_risk=True)
        return None
