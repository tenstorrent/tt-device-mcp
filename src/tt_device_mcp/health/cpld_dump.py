# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The vendor CPLD DIAG dump, taken once per fault episode before any recovery (spec 04 I109).

A reset or re-power clears the CPLD state that says why a tray or chip dropped, so the DIAG must
run first. It is the vendor's own script, run unchanged (the host's copy at
``/usr/local/bin/cpld-dump.sh``, else a sha256-pinned V1.03.01 the installer placed), so whoever
reads it reads exactly the vendor dump. Its stdout is saved byte for byte. It talks only to the
BMC (``ipmitool raw 0x06 0x52``, I2C to the board CPLDs), never to a chip over PCIe.

Four things make it safe to run from the broker:

- One host-wide lock, ``/run/lock/tt-cpld.lock`` (flock), shared with any other CPLD reader: the DIAG selects
  CPLD debug indexes, and two dumps interleaving their selects read each other's registers.
- The UBB debug index (register 0x50 on each tray CPLD) is saved before and restored after, then
  read back: other readers expect the index they left.
- A hard cap on the whole thing, enforced on the root side: every privileged command runs as
  ``timeout -s KILL <cap> ...`` under sudo, so the kill reaches the root DIAG and its ipmitool
  children, which a per-user broker cannot signal itself. A dump that cannot take the lock, cannot
  find its script, runs past the cap or fails is skipped or killed with a logged reason; the index
  restore and the lock release follow the DIAG's end, and recovery always follows.
- A BMC that stops answering costs one call: the first IPMI call that runs into its bound ends the
  dump's BMC calls. Before the DIAG, the dump is skipped; after it, the remaining index restores
  and the sensor read are skipped. The event names the call (``bmc_silent``).

The dump and the incident bundle of the same fault episode (``evidence.capture_incident``) are
cross-linked: each incident folder of the episode gets a ``cpld_dump.json`` naming the dump, and the
``cpld_dump`` event (or, for an incident captured after it, a ``cpld_dump_linked`` event) names the
incident folders.

A broker that is not root needs these sudoers lines (the caps are the defaults; a changed
``TT_DEVICE_MCP_CPLD_DUMP_CAP_SEC`` changes the DIAG line's number)::

    <user> ALL=(root) NOPASSWD: /usr/bin/timeout -s KILL 40 bash /usr/local/bin/cpld-dump.sh DIAG
    <user> ALL=(root) NOPASSWD: /usr/bin/timeout -s KILL 40 bash /usr/local/lib/tt-device-mcp/BH_s7t_cpld_dump_V1.03.01.sh DIAG
    <user> ALL=(root) NOPASSWD: /usr/bin/timeout -s KILL 5 ipmitool raw 0x06 0x52 *
    <user> ALL=(root) NOPASSWD: /usr/bin/timeout -s KILL 20 ipmitool sdr elist full

Never raises.
"""

from __future__ import annotations

import calendar
import errno
import fcntl
import hashlib
import json
import math
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
# coreutils timeout, run as root: it kills the command's whole process group at the bound.
TIMEOUT = ("timeout", "-s", "KILL")
DUMPS_DIR = "cpld_dumps"
# The four UBB tray CPLDs (I2C bus, 8-bit address) and their debug index register. The vendor's
# UBB swap only relabels these buses, so saving and restoring all four needs no swap logic.
UBB_CPLDS = (("0x0D", "0x80"), ("0x0F", "0x80"), ("0x11", "0x80"), ("0x13", "0x80"))
UBB_INDEX_REG = "0x50"
# About 5 s per tray measured with a select-read-restore reader, 4 trays, doubled for the
# MB/PDB sections and a slow BMC. The lock wait and the index save/restore have their own bounds.
CAP_SEC = 40.0
LOCK_WAIT_SEC = 30.0
IPMI_CALL_SEC = 5.0
# One read-only BMC sensor listing after the DIAG: PSU input/output and the 12 V rails at the fault.
PSU_SDR_SEC = 20.0
_PSU_LINE = re.compile(r"PSU|PWS|P12V|12V|VIN|IIN|PIN|IOUT|POUT", re.I)
PROGRESS_SEC = 5.0
# How long past a root-side bound the broker waits for the command to end before its own backup
# kill (which reaches only sudo when the broker is not root).
KILL_GRACE_SEC = 3.0
# An incident captured this long before its episode opened is still that episode's (the gate can
# bundle an unhealthy verdict just before the FSM opens the episode).
LINK_EARLY_SEC = 120.0
LINK_FILE = "cpld_dump.json"

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

    Read-only: flock needs no write access, so the file is 0644, never world-writable.
    Opened without O_CREAT first: with fs.protected_regular, an O_CREAT open of a file another user
    owns in a sticky world-writable directory (/run/lock) is refused even when its mode allows it.
    Created 0644 (fchmod past the umask) so whoever comes second can still open it to read."""
    try:
        return os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.fchmod(fd, 0o644)
        except OSError:
            pass
        return fd
    except FileExistsError:
        return os.open(path, os.O_RDONLY)


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


def _privileged(argv: list[str], bound_sec: float) -> list[str]:
    """``argv`` bounded by a root-side ``timeout -s KILL``. /dev/ipmi0 is root-only: a per-user
    broker goes through sudo -n, which fails fast, never prompts. The timeout runs inside sudo, so
    its kill reaches the root command and its children; the broker's own kill reaches only sudo."""
    bounded = [*TIMEOUT, str(int(math.ceil(bound_sec))), *argv]
    return bounded if os.geteuid() == 0 else [*SUDO, *bounded]


class BmcSilent(Exception):
    """An IPMI call ran into its bound: the BMC is not answering, so the dump makes no more BMC calls."""


def _killed_at(rc: Optional[int], secs: float, bound: float) -> bool:
    """The root-side timeout's KILL takes timeout itself down too: 137 from a shell, -9 from sudo
    re-raising it, 124 from an older coreutils."""
    return rc in (124, 137, -signal.SIGKILL) and secs >= bound - 0.5


def _ipmi(args: list[str]) -> Optional[str]:
    """One ``ipmitool raw 0x06 0x52`` call: its hex byte, or None on an error or a bad answer.
    Raises :class:`BmcSilent` when the call ran into its bound."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            _privileged([IPMITOOL, "raw", "0x06", "0x52", *args], IPMI_CALL_SEC),
            capture_output=True,
            text=True,
            timeout=IPMI_CALL_SEC + KILL_GRACE_SEC,
        )
    except subprocess.TimeoutExpired:
        raise BmcSilent(f"ipmitool raw {' '.join(args)} got no answer in {IPMI_CALL_SEC:.0f}s") from None
    except OSError:
        return None
    if _killed_at(r.returncode, time.monotonic() - t0, IPMI_CALL_SEC):
        raise BmcSilent(f"ipmitool raw {' '.join(args)} got no answer in {IPMI_CALL_SEC:.0f}s")
    out = r.stdout.strip()
    return out if r.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{2}", out) else None


def _read_index(bus: str, addr: str) -> Optional[str]:
    v = _ipmi([bus, addr, "0x01", UBB_INDEX_REG])
    return None if v is None else f"0x{v.lower()}"


def _restore_index(bus: str, addr: str, value: str) -> Optional[str]:
    _ipmi([bus, addr, "0x00", UBB_INDEX_REG, value])
    return _read_index(bus, addr)


def _run_diag(
    script: str, out: Path, cap_sec: float, log: Callable[[str], None]
) -> tuple[Optional[int], float, str, str]:
    """Run ``<script> DIAG`` under the root-side cap, streaming raw stdout to ``out``.

    (rc or None on timeout or error, seconds, tail, error). Whatever ends the loop (EOF, the cap, a
    failed write or read), the DIAG has ended before this returns (:func:`_end_diag`)."""
    t0 = time.monotonic()
    tail = b""
    nlines = 0
    try:
        p = subprocess.Popen(
            _privileged(["bash", script, "DIAG"], cap_sec),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        return None, 0.0, f"launch failed: {e}", f"launch failed: {e!r}"
    next_progress = t0 + PROGRESS_SEC
    timed_out = eof = False
    error = ""
    try:
        with open(out, "wb") as f:
            fd = p.stdout.fileno()
            while True:
                now = time.monotonic()
                # The root-side timeout kills the DIAG at the cap, which ends the pipe; past the
                # grace the broker stops reading and kills what it can.
                if now - t0 >= cap_sec + KILL_GRACE_SEC:
                    timed_out = True
                    break
                ready, _, _ = select.select([fd], [], [], min(0.5, cap_sec + KILL_GRACE_SEC - (now - t0)))
                if ready:
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        eof = True
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
    except Exception as e:  # noqa: BLE001 - a full disk or a failed read still ends the DIAG below
        error = f"DIAG output: {e!r}"
    finally:
        rc = _end_diag(p, t0 + cap_sec + KILL_GRACE_SEC)
    secs = time.monotonic() - t0
    killed_at_cap = _killed_at(rc, secs, cap_sec)
    if error or timed_out or not eof or killed_at_cap:
        rc = None
    return rc, secs, tail.decode(errors="replace"), error


def _end_diag(p: subprocess.Popen, deadline: float) -> Optional[int]:
    """End the DIAG and reap it; rc, or None if it is still up. After an error the pipe is drained
    (output dropped) until the DIAG ends or the root-side timeout kills its whole group at the cap,
    so no straggler of it outlives this; a pipe that cannot be read is closed (the writer dies of
    SIGPIPE). Then the broker's own group kill, a backup that reaches only sudo when not root."""
    try:
        fd = p.stdout.fileno()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], min(0.5, max(0.0, deadline - time.monotonic())))
            if ready and not os.read(fd, 1 << 16):
                break
    except Exception:  # noqa: BLE001 - an unreadable pipe is closed below
        pass
    try:
        p.stdout.close()
    except OSError:
        pass
    try:
        return p.wait(timeout=max(KILL_GRACE_SEC, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        return p.wait(timeout=KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        return None


def _read_psu(out: Path) -> dict:
    """Save ``ipmitool sdr elist full`` raw to ``out``; return its path and the PSU/12 V lines."""
    try:
        r = subprocess.run(
            _privileged([IPMITOOL, "sdr", "elist", "full"], PSU_SDR_SEC),
            capture_output=True,
            text=True,
            timeout=PSU_SDR_SEC + KILL_GRACE_SEC,
        )
    except subprocess.TimeoutExpired as e:
        text = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        out.write_text(text)
        return {"psu_path": str(out), "psu_error": f"timed out after {PSU_SDR_SEC:.0f}s"}
    except OSError as e:
        return {"psu_error": repr(e)}
    out.write_text(r.stdout + r.stderr)
    lines = [ln.strip() for ln in r.stdout.splitlines() if _PSU_LINE.search(ln.split("|")[0])]
    rec: dict = {"psu_path": str(out), "psu": lines[:64]}
    if r.returncode != 0:
        rec["psu_error"] = f"rc {r.returncode}: {r.stderr.strip()[-120:]}"
    return rec


def _restore_indexes(saved: dict, silent: str = "") -> tuple[dict, str]:
    """Restore and read back each UBB index that was read before the DIAG; (per-CPLD record, why
    the BMC is silent or ""). Once a call runs into its bound (or ``silent`` says one did), the
    rest are skipped, not tried. Never raises."""
    index = {}
    for bus, addr in UBB_CPLDS:
        before = saved.get(bus)
        after = None
        if before is not None and not silent:
            try:
                after = _restore_index(bus, addr, before)
            except BmcSilent as e:
                silent = f"index restore on {bus}: {e}"
            except Exception:  # noqa: BLE001 - one CPLD's failure must not skip the others
                pass
        index[bus] = {"before": before, "after": after, "restored": before is not None and after == before}
    return index, silent


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
    saved: dict = {}
    out: Optional[Path] = None
    rc, tail = None, ""
    silent = ""
    try:
        log(f"cpld dump: lock held, running {which} DIAG {script} (cap {cap:.0f}s) before recovery")
        for bus, addr in UBB_CPLDS:
            try:
                saved[bus] = _read_index(bus, addr)
            except BmcSilent as e:
                silent = f"index save on {bus}: {e}"
                break
        if silent:
            raise BmcSilent(silent)
        root = evidence.health_dir() / DUMPS_DIR
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out = root / f"{stamp}_{re.sub(r'[^A-Za-z0-9_.-]+', '-', trigger)[:40]}.txt"
        _note_dump(episode, trigger, out)
        rc, secs, tail, err = _run_diag(script, out, cap, log)
        rec.update(path=str(out), rc=rc, diag_sec=round(secs, 2))
        if err:
            rec["error"] = err
    except BmcSilent:
        # Nothing was written before the DIAG, so nothing needs restoring.
        rec["bmc_silent"] = silent
        rec["skipped"] = f"BMC not answering ({silent}); DIAG not run"
    except Exception as e:  # noqa: BLE001 - never raises; the event names what broke
        rec["error"] = repr(e)
    finally:
        # Only after the DIAG has ended (_run_diag waits for it), and before the lock goes.
        try:
            rec["index_0x50"], silent = _restore_indexes(saved, silent)
        finally:
            os.close(fd)  # closing the fd releases the flock
    if "skipped" in rec:
        return _journal(rec, t0, log)
    if out is None or "path" not in rec:
        rec["skipped"] = f"dump failed: {rec.get('error', 'unknown')}"
        return _journal(rec, t0, log)
    if silent:
        rec["bmc_silent"] = silent
    try:
        if silent:
            rec["psu_error"] = "skipped: the BMC stopped answering"
        else:
            rec.update(_read_psu(out.with_name(out.stem + "_psu.txt")))
        m = _VERSION.search(out.read_text(errors="replace")) if out.exists() else None
        rec["version"] = m.group(1) if m else ""
    except Exception as e:  # noqa: BLE001
        rec.setdefault("error", repr(e))
    s = _SUMMARY.search(tail)
    if rec.get("error"):
        rec["summary"] = f"DIAG aborted: {rec['error'][:120]} (partial output kept)"
    elif rc is None:
        rec["summary"] = f"timed out after {cap:.0f}s (partial output kept)"
    elif s:
        rec["summary"] = f"{s.group(1)}/{s.group(2)} failed"
    else:
        rec["summary"] = (
            f"no DIAG summary (rc {rc}): {tail.strip().splitlines()[-1][:120] if tail.strip() else 'no output'}"
        )
    unrestored = [b for b, v in rec["index_0x50"].items() if v["before"] is not None and not v["restored"]]
    if unrestored:
        rec["summary"] += f"; index 0x50 NOT restored on {','.join(unrestored)}"
    if silent:
        rec["summary"] += f"; BMC stopped answering ({silent}), later BMC calls skipped"
    rec["incidents"] = _link_incidents(episode, _link_body(episode, trigger, out))
    return _journal(rec, t0, log)


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
    """The longest :func:`take_dump` can hold recovery: lock wait + DIAG cap and its kill grace +
    the index calls + PSU read + slack."""
    cap = float(os.environ.get("TT_DEVICE_MCP_CPLD_DUMP_CAP_SEC") or CAP_SEC)
    wait = float(os.environ.get("TT_DEVICE_MCP_CPLD_DUMP_LOCK_WAIT_SEC") or LOCK_WAIT_SEC)
    return wait + cap + 3 * KILL_GRACE_SEC + 12 * (IPMI_CALL_SEC + KILL_GRACE_SEC) + PSU_SDR_SEC + KILL_GRACE_SEC + 5.0


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


# The fault episode open now ("" when none); fsm.boot binds it to the FSM's record, as it does
# ``RecoveryMechanism.cpld_episode``. capture_incident reads it to find the episode's dump.
def current_episode() -> str:
    return ""


def _note_dump(episode: str, trigger: str, out: Path) -> None:
    """Record the episode's dump path as soon as it is chosen, so an incident captured while the
    DIAG runs (or after) can link to it."""
    try:
        p = last_episode_file()
        p.write_text(json.dumps({"episode": episode, "ts": time.time(), **_link_body(episode, trigger, out)}))
    except OSError:
        pass


def _link_body(episode: str, trigger: str, out: Path) -> dict:
    return {
        "episode": episode,
        "trigger": trigger,
        "path": str(out),
        "psu_path": str(out.with_name(out.stem + "_psu.txt")),
    }


def _episode_epoch(episode: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(episode.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return None


def _write_link(incident: Path, body: dict) -> bool:
    """Write ``cpld_dump.json`` into an incident folder, unless it already names another episode."""
    link = incident / LINK_FILE
    try:
        if link.exists() and json.loads(link.read_text()).get("episode") != body["episode"]:
            return False
        link.write_text(json.dumps(body, indent=2))
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _link_incidents(episode: str, body: dict) -> list[str]:
    """Link the dump into every incident folder of its episode: captured since the episode opened
    (or up to LINK_EARLY_SEC before). Returns those folders. Never raises."""
    opened = _episode_epoch(episode)
    if opened is None:
        return []
    linked = []
    try:
        dirs = sorted(d for d in (evidence.health_dir() / evidence.INCIDENTS_DIR).iterdir() if d.is_dir())
    except OSError:
        return []
    for d in dirs:
        try:
            at = calendar.timegm(time.strptime(d.name.split("_", 1)[0], "%Y%m%dT%H%M%SZ"))
        except ValueError:
            continue
        if at >= opened - LINK_EARLY_SEC and _write_link(d, body):
            linked.append(str(d))
    return linked


def link_incident(incident: Path) -> Optional[dict]:
    """capture_incident's side: if an episode is open and its dump has started, write the link file
    into ``incident`` and journal ``cpld_dump_linked``. The dump links incidents that exist when it
    ends; this covers the ones captured after. Never raises."""
    try:
        episode = current_episode()
        if not episode:
            return None
        rec = json.loads(last_episode_file().read_text())
        if rec.get("episode") != episode or not rec.get("path"):
            return None
        body = {k: rec[k] for k in ("episode", "trigger", "path", "psu_path") if k in rec}
        if not _write_link(incident, body):
            return None
        evidence.health_event("cpld_dump_linked", incident=str(incident), **body)
        return body
    except Exception:  # noqa: BLE001 - never raises
        return None
