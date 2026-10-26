# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Who a device holder is, for the journal and the log: logging only, never a decision.

A holder outside the broker blocks gates and resets, and by the time anyone reads the journal it
has usually exited, so a pid and a user name alone cannot say what it was. ``holder_identity``
reads, from /proc only (no subprocess), what identifies the process afterwards: its parent, uid,
command line (secrets redacted, truncated), cgroup and systemd unit, and start time. A process that
exits mid-read is recorded as gone, and an unreadable field is left out: it never raises and never
waits, so the scan it describes keeps its timing and its verdict.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Iterable, Optional

from tt_device_mcp.peercred import username_for_uid

# Where the process tree is read from. A module attribute so tests point it at a staged tree.
PROC_DIR = "/proc"
# The longest command line recorded, redaction applied first, so a cut never exposes a secret.
CMDLINE_MAX_CHARS = 256
REDACTED = "***"

_SECRET_NAME = r"[\w.-]*(?:token|key|secret|passwd|password)[\w.-]*"
# NAME=value / NAME: value, inside one argument (a flag, an env-style arg, a query string, JSON).
_NAMED_VALUE = re.compile(rf"({_SECRET_NAME})([\"']?\s*[=:]\s*[\"']?)([^\s\"',&;]+)", re.IGNORECASE)
# A flag naming a secret whose value is the next argument (``--token abc``).
_SECRET_FLAG = re.compile(rf"^--?{_SECRET_NAME}$", re.IGNORECASE)
# URL userinfo (``scheme://user:pass@host``).
_URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)


def redact_argv(argv: Iterable[str]) -> list[str]:
    """``argv`` with the values of arguments named like a secret, and URL userinfo, replaced."""
    out: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        if _SECRET_FLAG.match(arg):
            out.append(arg)
            redact_next = True
            continue
        arg = _URL_USERINFO.sub(rf"\1{REDACTED}@", arg)
        out.append(_NAMED_VALUE.sub(rf"\1\2{REDACTED}", arg))
    return out


def format_cmdline(raw: bytes, limit: int = CMDLINE_MAX_CHARS) -> str:
    """A NUL-separated /proc cmdline as one printable line: redacted, then cut to ``limit`` chars."""
    argv = [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")] if raw else []
    line = " ".join(redact_argv(argv))
    line = "".join(c if c.isprintable() else "?" for c in line)
    return line if len(line) <= limit else line[: limit - 3] + "..."


def _read(path: str, binary: bool = False):
    with open(path, "rb" if binary else "r") as f:
        return f.read()


def _cgroup(raw: str) -> tuple[str, str]:
    """(cgroup path, systemd unit) from /proc/<pid>/cgroup: the v2 entry, else the systemd one. The
    unit is the deepest ``.service`` or ``.scope`` in the path ('' when none)."""
    path = ""
    for line in raw.splitlines():
        hid, _, rest = line.partition(":")
        controllers, _, p = rest.partition(":")
        if hid == "0" and controllers == "":
            path = p
            break
        if controllers == "name=systemd" or not path:
            path = p
    unit = next((part for part in reversed(path.split("/")) if part.endswith((".service", ".scope"))), "")
    return path, unit


_btime_cache: list = []
_clk_tck_cache: list = []


def _boot_time() -> Optional[float]:
    if not _btime_cache:
        try:
            for line in _read(f"{PROC_DIR}/stat").splitlines():
                if line.startswith("btime "):
                    _btime_cache.append(float(line.split()[1]))
                    break
        except (OSError, ValueError, IndexError):
            return None
    return _btime_cache[0] if _btime_cache else None


def _clk_tck() -> float:
    if not _clk_tck_cache:
        try:
            _clk_tck_cache.append(float(os.sysconf("SC_CLK_TCK")))
        except (ValueError, OSError, AttributeError):
            _clk_tck_cache.append(100.0)
    return _clk_tck_cache[0]


def holder_identity(pid: int) -> dict:
    """What identifies process ``pid`` afterwards (module docstring). Never raises.

    ``status`` is ``live``, or ``gone`` when the process exited before or during the read (then
    only what was read before it went is kept). A field that cannot be read is left out."""
    out: dict = {"pid": int(pid), "status": "live"}
    base = f"{PROC_DIR}/{int(pid)}"
    try:
        stat = _read(f"{base}/stat")
        out["comm"] = stat[stat.index("(") + 1 : stat.rindex(")")]
        fields = stat.rsplit(")", 1)[1].split()
        ppid, start_ticks = int(fields[1]), int(fields[19])
        out["ppid"] = ppid
        boot = _boot_time()
        if boot is not None:
            out["started"] = (
                datetime.fromtimestamp(boot + start_ticks / _clk_tck()).astimezone().isoformat(timespec="seconds")
            )
        for line in _read(f"{base}/status").splitlines():
            if line.startswith("Uid:"):
                out["uid"] = int(line.split()[1])
                out["user"] = username_for_uid(out["uid"])
                break
        out["cmdline"] = format_cmdline(_read(f"{base}/cmdline", binary=True))
        try:
            out["cgroup"], out["unit"] = _cgroup(_read(f"{base}/cgroup"))
        except FileNotFoundError:
            if not os.path.exists(base):
                raise
        try:
            out["parent_comm"] = _read(f"{PROC_DIR}/{ppid}/comm").strip()
        except OSError:
            out["parent_comm"] = "gone"
        # The pid was reused mid-read if its start time changed: what was read is not one process.
        if _read(f"{base}/stat").rsplit(")", 1)[1].split()[19] != str(start_ticks):
            return {"pid": int(pid), "status": "gone"}
    except (FileNotFoundError, ProcessLookupError):
        out["status"] = "gone"
    except Exception as e:  # noqa: BLE001 - logging only: a read error never reaches the caller
        out["error"] = type(e).__name__
    return out


def holder_identities(pids: Iterable[int]) -> list[dict]:
    """``holder_identity`` for each distinct pid, in order."""
    seen: set = set()
    rows = []
    for pid in pids:
        if pid not in seen:
            seen.add(pid)
            rows.append(holder_identity(pid))
    return rows


def describe_identity(row: dict) -> str:
    """One log line for a ``holder_identity`` row."""
    if row.get("status") == "gone" and "cmdline" not in row:
        return f"pid {row['pid']} gone before it could be read"
    keys = ("ppid", "parent_comm", "user", "uid", "unit", "cgroup", "started", "cmdline", "error")
    parts = [f"{k}={row[k]!r}" if k == "cmdline" else f"{k}={row[k]}" for k in keys if row.get(k) not in (None, "")]
    gone = " (gone)" if row.get("status") == "gone" else ""
    return f"pid {row['pid']}{gone} " + " ".join(parts)
