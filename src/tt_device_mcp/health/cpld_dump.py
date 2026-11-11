# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The vendor CPLD DIAG dump, taken once per fault episode before any recovery (spec 04 I109).

A reset or re-power clears the CPLD state that says why a tray or chip dropped, so the DIAG must
run first. It is the vendor's own script, run unchanged (the host's
``/usr/local/bin/cpld-dump.sh``, else a sha256-pinned V1.03.01 the installer placed), so whoever
reads it reads exactly the vendor dump. Its stdout is saved byte for byte.

Three things make it safe to run from the broker:

- One host-wide lock, ``/run/lock/tt-cpld.lock`` (flock), shared with any other CPLD reader: the DIAG selects
  CPLD debug indexes, and two dumps interleaving their selects read each other's registers.
- The UBB debug index (register 0x50 on each tray CPLD) is saved before and restored after, then
  read back: other readers expect the index they left.
- A hard cap on the whole thing. A dump that cannot take the lock, cannot find its script, finds
  the BMC not answering or runs past the cap is skipped or killed with a logged reason; recovery
  always follows.

Never raises.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp.health import evidence

LOCK_PATH = "/run/lock/tt-cpld.lock"
HOST_SCRIPT = "/usr/local/bin/cpld-dump.sh"
PINNED_SCRIPT = "/usr/local/lib/tt-device-mcp/BH_s7t_cpld_dump_V1.03.01.sh"
# The vendor script is not ours to redistribute, so the repo carries its hash, not the file
# (deploy/cpld-dump.pin names its source). A pinned copy whose hash differs never runs.
PINNED_SHA256 = "ad0f966b78ba9f810f8add56e0942bad2b9bd02c434d0d66eb3ba687de89fc14"
IPMITOOL = "ipmitool"
SUDO = ("sudo", "-n")
DUMPS_DIR = "cpld_dumps"
# The four UBB tray CPLDs (I2C bus, 8-bit address) and their debug index register. The vendor's
# UBB swap only relabels these buses, so saving and restoring all four needs no swap logic.
UBB_CPLDS = (("0x0D", "0x80"), ("0x0F", "0x80"), ("0x11", "0x80"), ("0x13", "0x80"))
UBB_INDEX_REG = "0x50"
# About 5 s per tray measured by a select-read-restore reader, 4 trays, doubled for the
# MB/PDB sections and a slow BMC. The lock wait and the index save/restore have their own bounds.
CAP_SEC = 40.0
LOCK_WAIT_SEC = 30.0
IPMI_CALL_SEC = 5.0
# One read-only BMC sensor listing after the DIAG: PSU input/output and the 12 V rails at the fault.
PSU_SDR_SEC = 20.0
_PSU_LINE = re.compile(r"PSU|PWS|P12V|12V|VIN|IIN|PIN|IOUT|POUT", re.I)
PROGRESS_SEC = 5.0
# The DIAG runs under ``timeout -k`` set 1 s past the cap: a root child that outlives our SIGKILL at the cap
# (sudo) is still ended.
KILL_GRACE_SEC = 5.0

_SUMMARY = re.compile(r"(\d+)\s+FAILED signals in\s+(\d+)\s+TEST items")
_VERSION = re.compile(r"^\s*(?:This Script:\s*|VERSION=)(V[\d.]+)", re.M)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_script() -> tuple[Optional[str], str, str]:
    """(path, which copy, sha256) of the DIAG to run, or (None, reason, "")."""
    override = os.environ.get("TT_DEVICE_MCP_CPLD_DUMP_SCRIPT", "").strip()
    for path, which in ((override, "override"), (HOST_SCRIPT, "host"), (PINNED_SCRIPT, "pinned")):
        if not path or not os.path.isfile(path):
            continue
        try:
            digest = _sha256(path)
        except OSError as e:
            return None, f"cannot read {path}: {e}", ""
        if which == "pinned" and digest != PINNED_SHA256:
            return None, f"pinned copy {path} has sha256 {digest}, expected {PINNED_SHA256}", digest
        return path, which, digest
    return None, f"no DIAG script: neither {HOST_SCRIPT} nor {PINNED_SCRIPT} exists", ""


def _open_lock(path: str) -> int:
    """Open the shared lock file so root and a normal user can both take it.

    Opened without O_CREAT first: with fs.protected_regular, an O_CREAT open of a file another user
    owns in a sticky world-writable directory (/run/lock) is refused even when its mode allows it.
    Created 0666 (fchmod past the umask) so whoever comes second can open it read-write."""
    try:
        return os.open(path, os.O_RDWR)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass
        return fd
    except FileExistsError:
        return os.open(path, os.O_RDWR)


def _take_lock(path: str, wait_sec: float) -> tuple[Optional[int], str, float]:
    """(fd, reason, seconds waited). fd None: not taken, reason says why."""
    t0 = time.monotonic()
    try:
        fd = _open_lock(path)
    except OSError as e:
        return None, f"cannot open lock {path}: {e}", 0.0
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, "", time.monotonic() - t0
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                os.close(fd)
                return None, f"flock {path}: {e}", time.monotonic() - t0
        if time.monotonic() - t0 >= wait_sec:
            os.close(fd)
            return None, f"lock {path} still held after {wait_sec:.0f}s (another CPLD dump)", time.monotonic() - t0
        time.sleep(0.2)


def _privileged(argv: list[str]) -> list[str]:
    # /dev/ipmi0 is root-only: a per-user broker goes through sudo -n, which fails fast, never prompts.
    return argv if os.geteuid() == 0 else [*SUDO, *argv]


class _BmcTimeout(Exception):
    """The BMC did not answer one ipmitool call in IPMI_CALL_SEC: the rest of the dump would only time out too."""


def _ipmi(args: list[str]) -> Optional[str]:
    try:
        r = subprocess.run(
            _privileged([IPMITOOL, "raw", "0x06", "0x52", *args]),
            capture_output=True,
            text=True,
            timeout=IPMI_CALL_SEC,
        )
    except subprocess.TimeoutExpired as e:
        raise _BmcTimeout from e
    except OSError:
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{2}", out) else None


def _read_index(bus: str, addr: str) -> Optional[str]:
    v = _ipmi([bus, addr, "0x01", UBB_INDEX_REG])
    return None if v is None else f"0x{v.lower()}"


def _restore_index(bus: str, addr: str, value: str) -> Optional[str]:
    _ipmi([bus, addr, "0x00", UBB_INDEX_REG, value])
    return _read_index(bus, addr)


def _run_diag(script: str, out: Path, cap_sec: float, log: Callable[[str], None]) -> tuple[Optional[int], float, str]:
    """Run ``<script> DIAG``, streaming raw stdout to ``out``. (rc or None on timeout, seconds, tail)."""
    t0 = time.monotonic()
    tail = b""
    nlines = 0
    try:
        p = subprocess.Popen(
            _privileged(["timeout", "-k", f"{KILL_GRACE_SEC:g}", f"{cap_sec + 1:g}", "bash", script, "DIAG"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        return None, 0.0, f"launch failed: {e}"
    next_progress = t0 + PROGRESS_SEC
    with open(out, "wb") as f:
        fd = p.stdout.fileno()
        while True:
            now = time.monotonic()
            if now - t0 >= cap_sec:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    p.wait(timeout=KILL_GRACE_SEC + 1)
                except subprocess.TimeoutExpired:
                    log(f"cpld dump: pid {p.pid} still alive after SIGKILL; left to timeout -k")
                f.flush()
                os.fsync(f.fileno())
                return None, now - t0, tail.decode(errors="replace")
            ready, _, _ = select.select([fd], [], [], min(0.5, cap_sec - (now - t0)))
            if ready:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                nlines += chunk.count(b"\n")
                tail = (tail + chunk)[-4096:]
            if time.monotonic() >= next_progress:
                last = tail.rstrip().rsplit(b"\n", 1)[-1].decode(errors="replace").strip()[:80]
                log(f"cpld dump: {time.monotonic() - t0:.0f}s, {nlines} lines, at: {last}")
                next_progress += PROGRESS_SEC
        f.flush()
        os.fsync(f.fileno())
    rc = p.wait()
    return rc, time.monotonic() - t0, tail.decode(errors="replace")


def _read_psu(out: Path) -> dict:
    """Save ``ipmitool sdr elist full`` raw to ``out``; return its path and the PSU/12 V lines."""
    try:
        r = subprocess.run(
            _privileged([IPMITOOL, "sdr", "elist", "full"]),
            capture_output=True,
            text=True,
            timeout=PSU_SDR_SEC,
        )
    except subprocess.TimeoutExpired as e:
        text = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        out.write_text(text)
        return {"psu_path": str(out), "psu_error": f"timed out after {PSU_SDR_SEC:.0f}s"}
    except OSError as e:
        return {"psu_error": repr(e)}
    out.write_text(r.stdout + r.stderr)
    lines = [line.strip() for line in r.stdout.splitlines() if _PSU_LINE.search(line.split("|")[0])]
    rec: dict = {"psu_path": str(out), "psu": lines[:64]}
    if r.returncode != 0:
        rec["psu_error"] = f"rc {r.returncode}: {r.stderr.strip()[-120:]}"
    return rec


def take_dump(trigger: str, episode: str, log: Callable[[str], None]) -> dict:
    """Take one DIAG dump and journal it as a ``cpld_dump`` health event. Returns the event fields."""
    t0 = time.monotonic()
    rec: dict = {"trigger": trigger, "episode": episode}
    if os.environ.get("TT_DEVICE_MCP_CPLD_DUMP", "1") == "0":
        rec["skipped"] = "switched off (TT_DEVICE_MCP_CPLD_DUMP=0)"
        return _journal(rec, t0, log)
    cap = float(os.environ.get("TT_DEVICE_MCP_CPLD_DUMP_CAP_SEC") or CAP_SEC)
    wait = float(os.environ.get("TT_DEVICE_MCP_CPLD_DUMP_LOCK_WAIT_SEC") or LOCK_WAIT_SEC)
    script, which, digest = resolve_script()
    if script is None:
        rec.update(skipped=which, sha256=digest)
        return _journal(rec, t0, log)
    rec.update(script=script, which=which, sha256=digest)
    fd, why, waited = _take_lock(os.environ.get("TT_DEVICE_MCP_CPLD_LOCK") or LOCK_PATH, wait)
    rec["lock_wait_sec"] = round(waited, 2)
    if fd is None:
        rec["skipped"] = why
        return _journal(rec, t0, log)
    try:
        log(f"cpld dump: lock held, running {which} DIAG {script} (cap {cap:.0f}s) before recovery")
        try:
            saved = {bus: _read_index(bus, addr) for bus, addr in UBB_CPLDS}
        except _BmcTimeout:
            rec["skipped"] = f"BMC did not answer ipmitool in {IPMI_CALL_SEC:.0f}s; DIAG not run"
            return _journal(rec, t0, log)
        root = evidence.health_dir() / DUMPS_DIR
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out = root / f"{stamp}_{re.sub(r'[^A-Za-z0-9_.-]+', '-', trigger)[:40]}.txt"
        rc, secs, tail = _run_diag(script, out, cap, log)
        index = {}
        bmc_up = True
        for bus, addr in UBB_CPLDS:
            before = saved[bus]
            after = None
            if before is not None and bmc_up:
                try:
                    after = _restore_index(bus, addr, before)
                except _BmcTimeout:
                    bmc_up = False
            index[bus] = {"before": before, "after": after, "restored": before is not None and after == before}
        rec.update(path=str(out), rc=rc, diag_sec=round(secs, 2), index_0x50=index)
        if bmc_up:
            rec.update(_read_psu(out.with_name(out.stem + "_psu.txt")))
        else:
            rec["psu_error"] = "skipped: BMC stopped answering during the index restore"
        m = _VERSION.search(out.read_text(errors="replace")) if out.exists() else None
        rec["version"] = m.group(1) if m else ""
        s = _SUMMARY.search(tail)
        if rc is None:
            rec["summary"] = f"timed out after {cap:.0f}s (partial output kept)"
        elif s:
            rec["summary"] = f"{s.group(1)}/{s.group(2)} failed"
        else:
            rec["summary"] = (
                f"no DIAG summary (rc {rc}): {tail.strip().splitlines()[-1][:120] if tail.strip() else 'no output'}"
            )
        unrestored = [b for b, v in index.items() if v["before"] is not None and not v["restored"]]
        if unrestored:
            rec["summary"] += f"; index 0x50 NOT restored on {','.join(unrestored)}"
        return _journal(rec, t0, log)
    except (OSError, ValueError) as e:
        rec["skipped"] = f"dump failed: {e!r}"
        return _journal(rec, t0, log)
    finally:
        os.close(fd)  # closing the fd releases the flock


def _journal(rec: dict, t0: float, log: Callable[[str], None]) -> dict:
    rec["total_sec"] = round(time.monotonic() - t0, 2)
    if "skipped" in rec:
        log(f"cpld dump skipped ({rec['trigger']}): {rec['skipped']} — recovery proceeds")
        evidence.health_event("cpld_dump_skipped", **rec)
    else:
        log(f"cpld dump ({rec['trigger']}): {rec['summary']} in {rec['total_sec']:.1f}s -> {rec['path']}")
        evidence.health_event("cpld_dump", **rec)
    return rec


def total_bound_sec() -> float:
    """The longest :func:`take_dump` can hold recovery: lock wait + DIAG cap and kill grace + 12 index calls + PSU
    read + slack. A BMC that times out once ends the index calls and the PSU read, so a dead BMC costs one call."""
    cap = float(os.environ.get("TT_DEVICE_MCP_CPLD_DUMP_CAP_SEC") or CAP_SEC)
    wait = float(os.environ.get("TT_DEVICE_MCP_CPLD_DUMP_LOCK_WAIT_SEC") or LOCK_WAIT_SEC)
    return wait + cap + KILL_GRACE_SEC + 12 * IPMI_CALL_SEC + PSU_SDR_SEC + 5.0


def last_episode_file() -> Path:
    return evidence.health_dir() / DUMPS_DIR / "last_episode"


def dumped_episode() -> str:
    try:
        return json.loads(last_episode_file().read_text()).get("episode", "")
    except (OSError, ValueError, AttributeError):
        return ""


def mark_episode(episode: str) -> None:
    """Persist the episode already dumped, so a broker restart mid-episode does not dump it twice."""
    try:
        p = last_episode_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"episode": episode, "ts": time.time()}))
    except OSError:
        pass
