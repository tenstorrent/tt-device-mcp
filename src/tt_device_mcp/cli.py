# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""
TT Device MCP CLI

Usage:
    tt-device-mcp run <command>       # Run + stream + wait for exit (blocking)
    tt-device-mcp run-bg <command>    # Queue, return a job_id; track with status/wait/logs
    tt-device-mcp status [N|job_id]   # Queue + recent jobs (all users), or one job
    tt-device-mcp logs [-f] [job_id]  # Show/tail logs
    tt-device-mcp kill [job_id]       # Kill running or cancel queued job
    tt-device-mcp wait <job_id>       # Wait for job completion
    tt-device-mcp watch               # Live TUI

The CLI auto-discovers the server's unix socket (host broker, else this user's
standalone daemon) — socket-only, no TCP port. On a reserved/single-user box
without the broker, `tt-device-mcp daemon start` runs a per-user daemon.
"""

import argparse
import functools
import http.client
import json
import os
import pwd
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from tt_device_mcp import __version__
from tt_device_mcp.constants import (
    DEFAULT_PORT,
    DEFAULT_SOCKET,
    POST_STEP_DEADLINE_DEFAULT_SEC,
    POST_STEP_DEADLINE_ENV,
    PRE_STEP_DEADLINE_DEFAULT_SEC,
    PRE_STEP_DEADLINE_ENV,
    RESET_STREAM_KEEPALIVE_LINE,
    resolve_socket,
    step_client_timeout_sec,
    user_state_dir,
)
from tt_device_mcp.utils import api_call

# Env vars to capture from caller's environment
CAPTURED_ENV_VARS = [
    "TT_METAL_HOME",
    "TT_METAL_CACHE",
    "PYTHONPATH",
    "PYTHON_ENV_DIR",
    "TT_METAL_ENV",
    "VLLM_TARGET_DEVICE",
    "HF_HUB_CACHE",
    "TT_CACHE_PATH",
    "MESH_DEVICE",
    "VLLM_USE_V1",
    "VIRTUAL_ENV",
]


def get_owner() -> str:
    """Get owner from $USER environment variable."""
    return os.environ.get("USER", "unknown")


def is_daemon_running(port: int, host: str = "localhost", *, retries: int = 3, timeout: int = 10) -> bool:
    """Reachable if /health answers ok, over the broker (or per-user) socket.

    Retries with a generous timeout: a busy broker can take seconds to answer, and
    a too-tight probe would falsely report 'not running' while it's just loaded. A
    genuinely-absent server fails fast (no socket / connection refused), so the
    retries only cost time when the server is up-but-slow — exactly when patience
    is warranted.
    """
    for attempt in range(retries):
        result = api_call(port, "/health", method="GET", timeout=timeout)
        # 'degraded' means the device is held, not that the broker is down — it is up and
        # serving, so a held box stays reachable (a tenant job queues and waits behind the hold).
        if isinstance(result, dict) and result.get("status") in ("ok", "degraded"):
            return True
        if attempt < retries - 1:
            time.sleep(0.5)
    return False


def _server_down_message() -> str:
    """Distinguish a wedged/slow server (its socket exists) from no server at all,
    so the user knows whether to wait/escalate or to start a daemon."""
    sock = resolve_socket()
    if sock and os.path.exists(sock):
        return (
            f"Error: the device server at {sock} isn't responding (overloaded "
            f"or wedged). It may self-recover — retry shortly. If it persists, "
            f"an admin can restart it: sudo systemctl restart tt-device-broker"
        )
    return (
        "Error: no device server reachable. On a shared/broker host, ask an "
        "admin to start it (sudo systemctl start tt-device-broker); on a "
        "reserved/single-user box: tt-device-mcp daemon start"
    )


def capture_env() -> dict[str, str]:
    """Capture relevant env vars from caller's environment."""
    env = {}
    for var in CAPTURED_ENV_VARS:
        if var in os.environ:
            env[var] = os.environ[var]
    return env


def mcp_tool_call(port: int, tool: str, arguments: dict, host: str = "localhost", timeout: int = 30) -> dict:
    """Call an MCP tool via the server's REST API. ``timeout`` should exceed the
    tool's expected runtime (e.g. reset's `tt-smi -r` can take ~minutes)."""
    return api_call(port, f"/api/{tool}", method="POST", data=arguments, timeout=timeout)


# ============== Daemon Commands (per-user standalone; socket-only) ==============


def _daemon_state_dir(log_dir=None) -> Path:
    """State dir for the per-user daemon: --log-dir if given, else the machine-local base.

    Refuses to run rather than settle for a directory it cannot secure — the base sits in shared
    /tmp, and the socket, the pid file and the job records in there are what another tenant would be
    reading or replacing. Only the owner can chmod, so the failure below IS that check failing, and
    it names the owner: without that the operator sees a bare EPERM on a path they thought was
    theirs.
    """
    d = Path(log_dir) if log_dir else user_state_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        # 0700: this is one host's device journal and job records, not other tenants' business, and
        # /tmp is shared. Set every time, so a base someone else pre-created is not silently accepted.
        d.chmod(0o700)
    except OSError as e:
        owner = ""
        try:
            owner = f"It belongs to {pwd.getpwuid(d.stat().st_uid).pw_name}. "
        except (OSError, KeyError):
            pass
        sys.exit(
            f"error: cannot use {d} for this daemon's state: {e}\n"
            f"       {owner}Remove it, or put the state elsewhere with "
            f"--log-dir / TT_DEVICE_MCP_STATE_DIR."
        )
    return d


def per_user_daemon_env(log_dir=None) -> dict:
    """The environment a per-user daemon runs under. Used by every entry point that starts one, so
    `daemon start` and `start-fg` cannot diverge.

    It sets no authority here. Health gating is on, as it is for the system broker, and which
    recovery rungs exist is measured at boot from platform and privilege (spec 04 I17) — an
    unprivileged daemon arrives at the device-scoped resets on its own, without an installer
    having had to declare anything. Root-only state paths become subdirectories of the same
    machine-local base as the socket.
    """
    env = dict(os.environ)
    base = _daemon_state_dir(log_dir)
    env.setdefault("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(base / "device-op.lock"))
    for var, name in (("TT_DEVICE_MCP_HEALTH_DIR", "health"), ("TT_DEVICE_MCP_JOB_EXIT_DIR", "jobexit")):
        if var in env:
            continue
        try:
            (base / name).mkdir(parents=True, exist_ok=True)
            env[var] = str(base / name)
        except OSError as e:
            # Fail-soft, but never silent: unset, the server falls back to /var/lib and /run and
            # drops every record there without a sound, which is what made this hard to see.
            print(
                f"warning: could not create {base / name} ({e}); {var} left unset "
                f"— the server will fall back to a path it may not be able to write",
                flush=True,
            )
    return env


def _daemon_socket(log_dir=None) -> str:
    """Per-user daemon socket. With no --log-dir this equals
    constants.user_socket_path() (what the stdio adapter auto-discovers)."""
    from tt_device_mcp.constants import user_socket_path

    return str(Path(log_dir) / "daemon.sock") if log_dir else user_socket_path()


def cmd_daemon_start(args) -> int:
    """Start a per-user standalone daemon on this user's unix socket — no root,
    no TCP port. For a reserved/single-user box; a shared
    host runs the system broker instead."""
    # On a broker host the system broker serves everyone and the CLI prefers its
    # socket, so a per-user daemon would be shadowed (never used) — refuse to start
    # one. (Skipped under --log-dir, i.e. tests/isolated instances.)
    if not args.log_dir and os.path.exists(DEFAULT_SOCKET):
        if is_daemon_running(args.port, retries=1):
            print(
                f"A system broker is already running at {DEFAULT_SOCKET}; it serves "
                f"all users here and the CLI uses it automatically. No per-user "
                f'daemon needed — just run: tt-device-mcp run "..."'
            )
            return 0
        print(
            f"A system broker socket exists at {DEFAULT_SOCKET} but isn't responding. "
            f"A per-user daemon would be shadowed by it, so it won't help. Ask an "
            f"admin to restart it: sudo systemctl restart tt-device-broker"
        )
        return 1
    sock = _daemon_socket(args.log_dir)
    os.environ["TT_DEVICE_MCP_SOCKET"] = sock  # target this daemon for health
    if is_daemon_running(args.port):
        print(f"Already running (socket {sock})")
        return 0
    state = _daemon_state_dir(args.log_dir)
    Path(sock).parent.mkdir(parents=True, exist_ok=True)
    server_cmd = [sys.executable, "-m", "tt_device_mcp.server", "--socket", sock, "--no-http", "--log-dir", str(state)]
    log_file = state / "daemon.log"
    with open(log_file, "a") as log:
        proc = subprocess.Popen(
            server_cmd, stdout=log, stderr=log, start_new_session=True, env=per_user_daemon_env(args.log_dir)
        )
    (state / "daemon.pid").write_text(str(proc.pid))
    for _ in range(20):  # up to ~10s for the socket + health
        time.sleep(0.5)
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            print(f"Failed to start. Check {log_file}")
            return 1
        if is_daemon_running(args.port):
            print(f"Started (PID {proc.pid}) on {sock}")
            return 0
    print(f"Failed to start (timeout). Check {log_file}")
    return 1


def cmd_daemon_stop(args) -> int:
    """Stop this user's standalone daemon."""
    pidf = _daemon_state_dir(args.log_dir) / "daemon.pid"
    try:
        pid = int(pidf.read_text().strip())
    except (OSError, ValueError):
        print("Not running")
        return 0
    # Confirm the pid is still OUR daemon. A stale pid file — a recycled pid, or one written by a
    # daemon that died hard — otherwise gets an unrelated process of this user SIGTERMed and then
    # SIGKILLed, and the installer now runs this on every upgrade.
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        cmdline = ""
    if "tt_device_mcp.server" not in cmdline:
        print("Not running (stale pid file)")
        pidf.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pidf.unlink(missing_ok=True)
    Path(_daemon_socket(args.log_dir)).unlink(missing_ok=True)
    print("Stopped")
    return 0


def cmd_daemon_status(args) -> int:
    """Health of this user's standalone daemon."""
    sock = _daemon_socket(args.log_dir)
    os.environ["TT_DEVICE_MCP_SOCKET"] = sock
    if is_daemon_running(args.port):
        print(f"Running (socket {sock})")
        return 0
    print("Not running")
    return 1


def cmd_daemon_start_fg(args) -> None:
    """Run the daemon in the foreground (debugging) on this user's socket."""
    state = _daemon_state_dir(args.log_dir)
    sys.argv = [sys.argv[0], "--socket", _daemon_socket(args.log_dir), "--no-http", "--log-dir", str(state)]
    # In-process, so apply the same environment `daemon start` gives its child — debugging a
    # daemon must not run a different configuration from the one being debugged.
    os.environ.update(per_user_daemon_env(args.log_dir))
    from tt_device_mcp.server import main

    main()


def cmd_run(args) -> int:
    """Queue a job and wait for completion (blocking)."""

    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1

    workspace = args.workspace or os.getcwd()
    command = args.cmd
    env_file = args.env
    inherited_env = capture_env() if not env_file else None
    timeout = args.timeout

    print(f"Queueing job in {workspace}...")
    print(f"Command: {command}")

    # Queue the job
    result = mcp_tool_call(
        args.port,
        "tt_device_job_run_bg",
        {
            "workspace": workspace,
            "command": command,
            "env": env_file,
            "inherited_env": inherited_env,
            "timeout_sec": timeout,
        },
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1

    job_id = result["job_id"]
    log_file = result.get("log_file")
    print(f"Job {job_id} queued (position: {result.get('position', '?')})")

    if log_file:
        print(f"Log: {log_file}")

    # Wait and stream logs
    print("\n--- Output ---")
    return _wait_and_stream(args.port, job_id, log_file, args.output_lines)


def cmd_exec(args) -> int:
    """Run a diagnostic directly on the device, bypassing the queue.

    For tt-triage / tt-smi on a HUNG job: it runs alongside the holder rather than
    queueing behind it. A foreign-owned running job is refused unless --force, which
    runs the diagnostic concurrently with theirs (read-only tools only)."""

    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1

    # Mirror the server's hard ceiling so an over-large -t fails here with a clear line
    # rather than as a validation error from the tool call.
    timeout = args.timeout
    if timeout > 600:
        print("exec: timeout capped at 600s (a triage that cannot answer in 600s is itself wedged)")
        timeout = 600

    result = mcp_tool_call(
        args.port,
        "tt_device_exec",
        {
            "command": args.cmd,
            "timeout_sec": timeout,
            "force": args.force,
        },
        timeout=timeout + 30,
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        if result.get("hint"):
            print(f"Hint: {result['hint']}")
        return 1

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if stdout:
        sys.stdout.write(stdout if stdout.endswith("\n") else stdout + "\n")
    if stderr:
        sys.stderr.write(stderr if stderr.endswith("\n") else stderr + "\n")
    return int(result.get("exit_code", 0) or 0)


def cmd_run_bg(args) -> int:
    """Queue a job and return immediately (non-blocking)."""

    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1

    workspace = args.workspace or os.getcwd()
    command = args.cmd
    env_file = args.env
    inherited_env = capture_env() if not env_file else None
    timeout = args.timeout

    result = mcp_tool_call(
        args.port,
        "tt_device_job_run_bg",
        {
            "workspace": workspace,
            "command": command,
            "env": env_file,
            "inherited_env": inherited_env,
            "timeout_sec": timeout,
        },
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1

    job_id = result["job_id"]
    print(f"Job {job_id} queued (position: {result.get('position', '?')})")
    if result.get("log_file"):
        print(f"Log: {result['log_file']}")

    return 0


def _tz_config_path() -> Path:
    """Per-user, so tenants on a shared box each get their own zone."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "tt-device-mcp" / "timezone"


_TZ_DEFAULT = "America/Los_Angeles"
_ZONE_TAB = "/usr/share/zoneinfo/zone1970.tab"

# The zones developers actually sit in — a bare `--list` shows these, because the
# full table is 312 rows. It is a shortlist, not a taxonomy: tzdata has no notion
# of "the" representative zone for a clock (CLDR's own data picks Tijuana for
# Pacific and Chita for UTC+9), so curating beats deriving. `--list <text>`
# searches every canonical zone for anyone not here.
_COMMON_ZONES = (
    "Pacific/Honolulu",
    "America/Anchorage",
    "America/Los_Angeles",
    "America/Vancouver",
    "America/Denver",
    "America/Phoenix",
    "America/Chicago",
    "America/Mexico_City",
    "America/New_York",
    "America/Toronto",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Lisbon",
    "Europe/Dublin",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Brussels",
    "Europe/Zurich",
    "Europe/Prague",
    "Europe/Warsaw",
    "Europe/Belgrade",
    "Europe/Athens",
    "Europe/Helsinki",
    "Europe/Kyiv",
    "Asia/Jerusalem",
    "Europe/Istanbul",
    "Europe/Moscow",
    "Asia/Dubai",
    "Asia/Karachi",
    "Asia/Kolkata",
    "Asia/Dhaka",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Taipei",
    "Asia/Seoul",
    "Asia/Tokyo",
    "Australia/Perth",
    "Australia/Brisbane",
    "Australia/Sydney",
    "Pacific/Auckland",
)


def _fmt_utc_offset(seconds: int) -> str:
    sign = "-" if seconds < 0 else "+"
    h, m = divmod(abs(int(seconds)) // 60, 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def _offset_sort_key(off: str) -> float:
    """Sort 'UTC-8/-7', 'UTC+5:30' west-to-east by their standard offset."""
    base = off[3:].split("/")[0]
    sign = -1 if base[0] == "-" else 1
    h, _, m = base[1:].partition(":")
    return sign * (int(h) + int(m or 0) / 60)


def zone_offsets(key: str) -> str:
    """'UTC-8/-7' for a DST zone, 'UTC+9' for one without — read from tzdata, so it
    stays correct as rules change."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(key)
    offs = {int(datetime(2026, m, 15, 12, tzinfo=tz).utcoffset().total_seconds()) for m in range(1, 13)}
    std, dst = min(offs), max(offs)
    return _fmt_utc_offset(std) + (f"/{_fmt_utc_offset(dst)[3:]}" if dst != std else "")


@functools.lru_cache(maxsize=1)
def canonical_zones() -> tuple:
    """IANA's official one-zone-per-region table (~312 zones). `available_timezones()`
    also enumerates backward-compat links and non-zones — `localtime`, `Factory`,
    `US/Pacific`, `CST6CDT` — which must never be offered or stored: 'localtime' as a
    column header is meaningless, and the aliases are what make bare city names
    collide."""
    zones = set()
    try:
        with open(_ZONE_TAB) as f:
            for ln in f:
                parts = ln.split("\t")
                if not ln.startswith("#") and len(parts) > 2 and "/" in parts[2]:
                    zones.add(parts[2].strip())
    except OSError:
        from zoneinfo import available_timezones

        zones = {z for z in available_timezones() if "/" in z and not z.startswith("Etc/")}
    return tuple(sorted(zones))


def zone_from_name(name: str) -> str:
    """Full IANA key from a full key or a bare city ('Los_Angeles'). Restricted to
    canonical zones, so a bare city is unambiguous for every real zone. Raises
    ValueError with something actionable."""
    zones = canonical_zones()
    name = name.strip().replace(" ", "_")  # zones are displayed with spaces, keyed with underscores
    if name in zones:
        return name
    hits = [z for z in zones if z.rsplit("/", 1)[-1].lower() == name.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"unknown timezone {name!r} — find it with: tt-device-mcp timezone --list {name[:4].lower()}")
    raise ValueError(f"{name!r} is ambiguous ({', '.join(hits)}) — give the full name")


@functools.lru_cache(maxsize=1)
def _resolve_tz():
    """(tzinfo, label) for the caller. TT_DEVICE_MCP_TZ, else the saved preference,
    else Pacific. A zone name carries its own DST rules, so the label is the city
    and stays correct year-round; a bare integer pins a fixed UTC offset instead."""
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo

    raw = os.environ.get("TT_DEVICE_MCP_TZ")
    if raw is None:
        try:
            raw = _tz_config_path().read_text()
        except OSError:
            # $TZ last: on a UTC host it's normally unset, but if the user's session
            # carries one (ssh SendEnv/AcceptEnv TZ), that's their real zone.
            raw = os.environ.get("TZ", "")
    raw = (raw or "").strip()

    if raw and raw.lstrip("+-").isdigit():
        hours = int(raw)
        return timezone(timedelta(hours=hours)), f"UTC{hours:+d}"

    def _city(key):  # the id is keyed with underscores; people read spaces
        return key.rsplit("/", 1)[-1].replace("_", " ")

    try:
        key = zone_from_name(raw) if raw else _TZ_DEFAULT
        return ZoneInfo(key), _city(key)
    except Exception:
        try:
            return ZoneInfo(_TZ_DEFAULT), _city(_TZ_DEFAULT)
        except Exception:
            # A slim image can ship no tz database at all, and then even the default zone
            # raises. UTC needs none, so the CLI still prints times instead of a traceback.
            return timezone.utc, "UTC"


def _fmt_when(iso) -> str:
    """'dd hh:mm:ss' in the caller's zone (`tt-device-mcp timezone`). Server
    timestamps are naive UTC — the host runs UTC."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz, _ = _resolve_tz()
    return f"{dt.astimezone(tz):%d %H:%M:%S}"


# ANSI styling — applied only when the destination is a terminal (see _overview),
# so piping `status` into grep/files stays plain text.
_ANSI = {"hdr": "\033[1;36m", "dim": "\033[2m", "off": "\033[0m"}


def _style(text: str, key: str, color: bool) -> str:
    return f"{_ANSI[key]}{text}{_ANSI['off']}" if color else text


def _display_width(text: str) -> int:
    """Columns ``text`` occupies in a terminal, which is not ``len``.

    The owner column is padded to align, and the icons in it are emoji: one codepoint that draws
    two columns, so padding by length leaves every agent row a column short. A variation selector
    is itself zero-width but promotes the character before it to the two-column emoji form.
    """
    width = 0
    for ch in text:
        if ch == "\ufe0f":
            width += 1
        elif unicodedata.combining(ch):
            continue
        else:
            width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, columns: int) -> str:
    """Left-justify ``text`` to ``columns`` TERMINAL columns (see :func:`_display_width`)."""
    return text + " " * max(0, columns - _display_width(text))


def _clip(text: str, columns: int) -> str:
    """Cut ``text`` to at most ``columns`` terminal columns.

    Slicing would cut by codepoints, and an icon is one codepoint drawing two columns — so a long
    name kept its character budget, overflowed the cell, and shifted every column after it.
    """
    if _display_width(text) <= columns:
        return text
    out = ""
    for ch in text:
        if _display_width(out + ch) > columns:
            break
        out += ch
    return out


# One real space between the icon and the name, always. Every icon here is a single codepoint
# with East_Asian_Width=W, so each is two columns in any terminal — no variation selectors, which
# are what made the previous gear render one width here and another there.
_ICON_GAP = " "


def _fmt_owner(raw: str) -> tuple:
    """(icon, name) — wrench for the broker's own device work, robot for agents, person
    for humans. The broker's health checks and resets hold the device like any job, so
    they appear in RUNNING; the icon is what keeps them from reading as someone's run."""
    if raw.startswith("[agent]"):
        return "🤖", raw[len("[agent]") :]
    if raw.startswith("[broker]"):
        return "🔧", raw[len("[broker]") :] or "broker"
    return "👤", raw


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60)}m"


def recent_lines(jobs, color: bool = False) -> list:
    """The recent-jobs table (all users): time, owner, duration, status, command.

    The list is in EXECUTION order, so the time column is when each entry started running,
    not when it was queued. Those are the same thing only on an idle device: behind a deep
    queue a run starts minutes after it was submitted, and the resets and fabric checks it
    provokes are never queued at all. Stamping queue time printed several rows with the
    same timestamp and put the repairs nowhere near the run that caused them. WAIT already
    says how long the queue held it.
    """
    _, tzlabel = _resolve_tz()
    w = max(11, len(tzlabel))  # 'dd hh:mm:ss' is 11; a long city name widens it
    out = [
        _style(
            f"{tzlabel:<{w}} {'JOB ID':<6} {'OWNER':<17} {'RUNTIME':>8} {'WAIT':>7} {'STATUS':<11} {'EXIT':>4}  COMMAND",
            "dim",
            color,
        )
    ]
    for j in jobs:
        # A job that has not started yet has no run time to show; fall back to when it
        # arrived, which is all that has happened to it so far.
        when = _fmt_when(j.get("started_at") or j.get("queued_at"))
        jid = (j.get("job_id") or "?")[:6]
        # Same icon+name split as RUNNING/QUEUED: the raw tag would otherwise fill the column
        # (`[agent]<user>` is already 16 chars) and truncate the name it is there to show.
        icon, name = _fmt_owner(j.get("owner") or "?")
        owner = _pad(_clip(f"{icon}{_ICON_GAP}{name}", 17), 17)
        rt = j.get("runtime") or "-"
        wait = j.get("wait")
        wait = "-" if wait in (None, "", "0.0s") else wait  # no measurable wait reads as "-"
        st = (j.get("status") or "?")[:11]
        ex = j.get("exit_code")
        ex = "-" if ex in (None, "None") else str(ex)
        cmd = (j.get("command") or "")[:70]
        out.append(f"{when:<{w}} {jid:<6} {owner} {rt:>8} {wait:>7} {st:<11} {ex:>4}  {cmd}")
    return out


def overview_lines(running, queued, rjobs, color: bool = False) -> list:
    """The full overview — RUNNING, QUEUED, RECENT — as text lines. The single
    renderer behind both `status` (one frame) and `watch` (looped). RUNNING/QUEUED
    keep the per-section icon+runtime layout; RECENT is the durable history table."""
    from datetime import datetime

    max_w = max([5] + [len(_fmt_owner(j.get("owner", "?"))[1]) for j in running + queued])

    lines = [_style("RUNNING", "hdr", color)]
    if running:
        lines.append(_style(f"  ID            Owner{' ' * (max_w - 2)}  Runtime  Command", "dim", color))
        for job in running:
            jid = job.get("id", "?")[:10]
            icon, owner = _fmt_owner(job.get("owner", "?"))
            cmd = job.get("command", "?")[:80]
            runtime = "..."
            started = job.get("started_at")
            if started:
                try:
                    runtime = _fmt_dur((datetime.now() - datetime.fromisoformat(started)).total_seconds())
                except Exception:
                    pass
            lines.append(f"  {jid:<10}  {_pad(icon + _ICON_GAP + owner, max_w + 3)}  {runtime:<8} {cmd}")
    else:
        lines.append(_style("  (none)", "dim", color))

    lines.append("")
    lines.append(_style("QUEUED", "hdr", color))
    if queued:
        lines.append(_style(f"  #  ID            Owner{' ' * (max_w - 2)}  Command", "dim", color))
        for job in queued:
            pos = job.get("position", "?")
            jid = job.get("id", "?")[:10]
            icon, owner = _fmt_owner(job.get("owner", "?"))
            cmd = job.get("command", "?")[:80]
            lines.append(f"  {pos:<2} {jid:<10}  {_pad(icon + _ICON_GAP + owner, max_w + 3)}  {cmd}")
    else:
        lines.append(_style("  (none)", "dim", color))

    if rjobs:
        lines.append("")
        lines.append(_style(f"RECENT (last {len(rjobs)})", "hdr", color))
        lines.extend(recent_lines(rjobs, color))
    return lines


def _overview(port, limit=20, color=False):
    """Fetch the queue + recent jobs and render the overview as lines. The one
    code path behind both `status` (printed once) and `watch` (looped).
    Returns (lines, error)."""
    qs = mcp_tool_call(port, "tt_device_queue_status", {})
    if "error" in qs:
        return None, qs["error"]
    recent = mcp_tool_call(port, "tt_device_recent_jobs", {"limit": limit})
    rjobs = recent.get("jobs", []) if isinstance(recent, dict) else []
    return overview_lines(qs.get("running", []), qs.get("queued", []), rjobs, color), None


def cmd_status(args) -> int:
    """Show job status."""

    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1

    # Job ids are bare digits now, so they can't share the positional with the
    # recent-count: `status N` -> N recent, `status -j ID` -> that job's detail.
    target = args.target
    limit = int(target) if (target and target.isdigit()) else 15
    job_id = args.job

    if job_id:
        # Show specific job
        result = mcp_tool_call(args.port, "tt_device_job_status", {"job_id": job_id})
        if "error" in result:
            print(f"Error: {result['error']}")
            return 1

        print(f"Job:     {result['job_id']}")
        print(f"Owner:   {result.get('owner', '?')}")
        print(f"Status:  {result['status']}")
        if result.get("cause"):
            print(f"Cause:   {result['cause']}")
        print(f"Command: {result.get('command', '?')}")
        if result.get("exit_code") is not None:
            print(f"Exit:    {result['exit_code']}")
        if result.get("runtime_sec"):
            print(f"Runtime: {result['runtime_sec']:.1f}s")
        if result.get("log_file"):
            print(f"Log:     {result['log_file']}")
    else:
        # One frame of `watch`: RUNNING + QUEUED + RECENT (all users), durable
        # across broker restarts (includes orphaned jobs, resets, execs).
        lines, err = _overview(args.port, limit, color=sys.stdout.isatty())
        if err:
            print(f"Error: {err}")
            return 1
        print("\n".join(lines))

    return 0


def cmd_logs(args) -> int:
    """Show job logs."""

    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1

    job_id = args.job_id
    if not job_id:
        # Find most recent job for this user
        result = mcp_tool_call(args.port, "tt_device_queue_status", {})
        if "error" in result:
            print(f"Error: {result['error']}")
            return 1

        owner = get_owner()
        # Check running first, then queued
        for job in result.get("running", []) + result.get("queued", []):
            if job.get("owner") == owner or job.get("owner") == f"[agent]{owner}":
                job_id = job["id"]
                break

        if not job_id:
            print("No jobs found for you. Specify a job_id.")
            return 1

    if args.follow:
        # Tail the log file
        result = mcp_tool_call(args.port, "tt_device_job_status", {"job_id": job_id})
        if "error" in result:
            print(f"Error: {result['error']}")
            return 1

        log_file = result.get("log_file")
        if not log_file:
            print("No log file for this job")
            return 1

        print(f"Tailing {log_file}...")
        try:
            subprocess.run(["tail", "-f", log_file])
        except KeyboardInterrupt:
            pass
        return 0
    else:
        # Get logs via API
        result = mcp_tool_call(
            args.port,
            "tt_device_job_logs",
            {
                "job_id": job_id,
                "tail": args.lines,
            },
        )
        if "error" in result:
            print(f"Error: {result['error']}")
            return 1

        print(result.get("content", ""))
        return 0


# Per read, not per reset: the broker sends a keepalive line on every quiet stretch
# (RESET_STREAM_KEEPALIVE_SEC), so only a broker that has gone away trips this.
RESET_TIMEOUT = 300


def _open_reset_stream(
    port: int,
    payload: dict,
    endpoint: str = "/api/tt_device_reset_stream",
    timeout=RESET_TIMEOUT,
    host: str = "localhost",
):
    """Open a streaming POST to a broker endpoint over the unix socket (or TCP).
    Returns (conn, response) so the caller can read chunks as they arrive.
    ``timeout=None`` for an open-ended stream (e.g. interactive smi)."""
    socket_path = resolve_socket()
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if socket_path:
        conn = http.client.HTTPConnection("localhost", timeout=timeout)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.settimeout(timeout)
        conn.sock.connect(socket_path)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.request("POST", endpoint, body=body, headers=headers)
    return conn, conn.getresponse()


def _print_stream_with_dots(resp, interval: float = 2.0):
    """Print each line from ``resp`` as it arrives; while waiting for the next line,
    append a '.' every ``interval`` seconds on the current line. Returns the status
    carried by the trailing '::status::<x>' sentinel."""
    q: "queue.Queue" = queue.Queue()
    DONE = object()

    def _reader():
        try:
            for raw in resp:
                q.put(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
        except Exception as exc:  # noqa: BLE001 - surface stream errors as a line
            q.put(f"[stream error: {exc}]\n")
        finally:
            q.put(DONE)

    threading.Thread(target=_reader, daemon=True).start()
    status = None
    dotted = False
    while True:
        try:
            item = q.get(timeout=interval)
        except queue.Empty:
            print(".", end="", flush=True)
            dotted = True
            continue
        if item is DONE:
            break
        for line in item.splitlines():
            if line == RESET_STREAM_KEEPALIVE_LINE:
                continue  # the broker is alive; the dots already say we are waiting
            if line.startswith("::status::"):
                status = line[len("::status::") :].strip()
                continue
            if dotted:
                print()
                dotted = False
            print(line, flush=True)
    if dotted:
        print()
    return status


def cmd_reset(args) -> int:
    """Reset the device(s) via the broker, streaming progress live. Honors the gate."""

    if not is_daemon_running(args.port):
        print("Error: tt-device-mcp not reachable (is the broker/daemon running?)")
        return 1

    try:
        conn, resp = _open_reset_stream(args.port, {"force": args.force, "keepalive": True})
    except OSError as exc:
        print(f"Error talking to broker: {exc}")
        return 1
    try:
        status = _print_stream_with_dots(resp)
    finally:
        conn.close()

    if status == "reset_complete":
        print("Reset complete.")
        return 0
    if status == "refused":
        print("Reset refused.")
        return 1
    if status == "no_devices":
        return 1
    print(f"Reset failed ({status or 'no status received'}).")
    return 1


def _print_step_verdict(kind: str, body: dict) -> int:
    """One line a Slurm log can be read from, then the exit code the driver acts on."""
    status = body.get("status", "error")
    reason = body.get("reason") or body.get("error") or ""
    if body.get("holders"):
        who = ", ".join(f"pid {h['pid']} ({h['username']})" for h in body["holders"])
        print(f"{kind}: holders on device: {who}")
    if body.get("reclaimed"):
        # The only record of which pids root SIGKILLed on a run where nothing else went wrong —
        # without this line here, a clean post-step leaves an operator whose process vanished
        # with no way to learn why from the Slurm log.
        who = ", ".join(f"pid {h['pid']} ({h['username']})" for h in body["reclaimed"])
        print(f"{kind}: reclaimed: {who}")
    if body.get("survivors"):
        who = ", ".join(f"pid {h['pid']} ({h['username']})" for h in body["survivors"])
        print(f"{kind}: survived reclaim: {who}")
    if status == "ok":
        print(f"{kind}: device fit ({body.get('fsm_state', '?')})")
        return 0
    print(f"{kind}: {status}: {reason}")
    return 1


def cmd_pre_step(args) -> int:
    """The read-only health pass to run before a job step (a Slurm Prolog). Exit 0 iff the device
    is fit and free."""
    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1
    timeout = step_client_timeout_sec(PRE_STEP_DEADLINE_ENV, PRE_STEP_DEADLINE_DEFAULT_SEC)
    body = mcp_tool_call(args.port, "tt_device_pre_step", {}, timeout=timeout)
    return _print_step_verdict("pre-step", body)


def _step_exit_code(raw: str) -> int:
    """A finished step's exit code, in any of the shapes a scheduler actually reports it.

    Slurm's are not the plain small integers their names suggest: `SLURM_JOB_EXIT_CODE` is a
    wait(2) status (an exit of 1 arrives as 256) and the `<exit>:<signal>` spelling turns up too
    — `0:0` for a clean step, `0:9` for one a signal killed. A strict `type=int` rejected `0:0`,
    which made the epilogue exit non-zero without ever reaching the broker and drained the node
    over a job that finished fine.

    Only the zero/nonzero bit is consumed downstream (it decides whether to force the fabric
    pass), so anything unparseable collapses to 1 — the conservative side: a step whose outcome
    cannot be read is not evidence the device is clean. The server's own
    ``_parse_post_step_exit_code`` deliberately falls the other way, to 0, and that is not an
    oversight: here the input is a scheduler's own value in a shape we may not have seen yet,
    while there it is a request body, where forcing on garbage would let any caller buy itself
    a ~45s traffic pass.
    """
    parts = [p for p in str(raw).strip().split(":") if p != ""]
    if not parts:
        return 0
    try:
        return 0 if all(int(p) == 0 for p in parts) else 1
    except ValueError:
        return 1


def cmd_post_step(args) -> int:
    """The recovering health pass to run after a job step (a Slurm Epilog): reclaim stragglers,
    recover if needed, confirm. Exit 0 iff the device ends fit."""
    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1
    payload = {
        "exit_code": int(getattr(args, "exit_code", 0) or 0),
        "reclaim": not getattr(args, "no_reclaim", False),
    }
    # The client timeout must outlast the server's own deadline, or the CLI reports a transport
    # failure for a step the broker is still resolving — so it is derived from the same env var
    # and default the server reads, plus a fixed margin, rather than a number that can fall out
    # of sync the moment a site raises TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC on its own.
    timeout = step_client_timeout_sec(POST_STEP_DEADLINE_ENV, POST_STEP_DEADLINE_DEFAULT_SEC)
    body = mcp_tool_call(args.port, "tt_device_post_step", payload, timeout=timeout)
    return _print_step_verdict("post-step", body)


def cmd_kill(args) -> int:
    """Kill a running job or cancel a queued job."""

    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1

    # The broker authenticates by peer uid; this is only for picking the caller's own job out of
    # the list and for the confirmation text.
    owner = get_owner()
    job_id = args.job_id
    job_owner = owner  # Track actual owner of selected job

    if job_id:
        # Fetch job info to get actual owner
        job_info = mcp_tool_call(args.port, "tt_device_job_status", {"job_id": job_id})
        if "error" not in job_info:
            job_owner = job_info.get("owner", owner)

    if not job_id:
        # Find user's jobs
        result = mcp_tool_call(args.port, "tt_device_queue_status", {})
        if "error" in result:
            print(f"Error: {result['error']}")
            return 1

        my_jobs = []
        for job in result.get("running", []) + result.get("queued", []):
            if job.get("owner") == owner or job.get("owner") == f"[agent]{owner}":
                my_jobs.append(job)

        if not my_jobs:
            print("You have no jobs to cancel.")
            return 0

        if len(my_jobs) == 1:
            job_id = my_jobs[0]["id"]
            job_owner = my_jobs[0].get("owner", owner)
            status = "running" if my_jobs[0] in result.get("running", []) else "queued"
            confirm = input(f"Cancel {job_id} ({status})? (y/n): ")
            if confirm.lower() != "y":
                print("Cancelled.")
                return 0
        else:
            print("Your jobs:")
            for i, job in enumerate(my_jobs, 1):
                status = "running" if job in result.get("running", []) else "queued"
                print(f"  {i}) {job['id']}  {status}")
            choice = input(f"\nCancel which? (1-{len(my_jobs)}, or 'n' to cancel): ")
            if choice.lower() == "n":
                print("Cancelled.")
                return 0
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(my_jobs):
                    job_id = my_jobs[idx]["id"]
                    job_owner = my_jobs[idx].get("owner", owner)
                else:
                    print("Invalid choice.")
                    return 1
            except ValueError:
                print("Invalid choice.")
                return 1

    result = mcp_tool_call(
        args.port,
        "tt_device_job_kill",
        {
            "job_id": job_id,
            "owner": job_owner,
        },
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1

    print(f"Job {job_id} {result.get('status', 'cancelled')}")
    return 0


def cmd_wait(args) -> int:
    """Wait for a job to complete."""

    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1

    job_id = args.job_id

    # Get job info first
    result = mcp_tool_call(args.port, "tt_device_job_status", {"job_id": job_id})
    if "error" in result:
        print(f"Error: {result['error']}")
        return 1

    if result["status"] not in ("queued", "running"):
        print(f"Job {job_id} already finished: {result['status']}")
        return 0 if result["status"] == "completed" else 1

    log_file = result.get("log_file")
    return _wait_and_stream(args.port, job_id, log_file)


def _wait_and_stream(
    port: int, job_id: str, log_file: str | None, output_lines: int = 0, host: str = "localhost"
) -> int:
    """Wait for job completion while streaming logs.

    Args:
        port: Server port
        job_id: Job ID to wait for
        log_file: Path to job log file
        output_lines: If > 0, only show last N lines at the end (default: 0 = show all)
        host: Server hostname
    """
    last_size = 0
    all_output: list[str] = []

    while True:
        # Check job status
        result = mcp_tool_call(port, "tt_device_job_status", {"job_id": job_id})
        if "error" in result:
            print(f"\nError: {result['error']}")
            return 1

        # Stream new log content
        if log_file and os.path.exists(log_file):
            with open(log_file, "r") as f:
                f.seek(last_size)
                new_content = f.read()
                if new_content:
                    print(new_content, end="", flush=True)
                    all_output.extend(new_content.splitlines(keepends=True))
                last_size = f.tell()

        if result["status"] not in ("queued", "running"):
            # Job finished
            print(f"\n--- Job {job_id} finished: {result['status']} ---")
            if result.get("exit_code") is not None:
                print(f"Exit code: {result['exit_code']}")
            if result.get("runtime_sec"):
                print(f"Runtime: {result['runtime_sec']:.1f}s")
            if result["status"] == "timeout" and result.get("hint"):
                print(f"\n!! TIMED OUT after {result.get('timeout_sec')}s !!\n{result['hint']}")

            # Show last N lines summary if requested
            if output_lines > 0 and all_output:
                print(f"\n--- Last {output_lines} lines ---")
                for line in all_output[-output_lines:]:
                    print(line, end="")

            return 0 if result["status"] == "completed" else 1

        time.sleep(1)


def cmd_watch(args) -> int:
    """Live `status`: the same overview, refreshed in place every 2s. Ctrl-C to exit."""
    if not is_daemon_running(args.port):
        print(_server_down_message())
        return 1
    from datetime import datetime

    limit = args.count if getattr(args, "count", None) else 15
    tty = sys.stdout.isatty()
    # Use the alternate screen (like top/vim) so refreshes redraw in place instead
    # of scrolling the scrollback — plain '2J' alone scrolls in some terminals.
    if tty:
        sys.stdout.write("\033[?1049h\033[?25l")  # enter alt screen, hide cursor
    try:
        while True:
            lines, err = _overview(args.port, limit=limit, color=tty)
            tz, tzlabel = _resolve_tz()
            now = f"{tzlabel} {datetime.now(tz):%d %H:%M:%S}"
            body = f"Error: {err}" if err else "\n".join(lines)
            frame = f"tt-device-mcp watch — {now} — Ctrl-C to exit\n\n{body}"
            if tty:
                # Home, redraw, then erase anything left from a taller prior frame.
                sys.stdout.write("\033[H" + frame.replace("\n", "\033[K\n") + "\033[J")
            else:
                sys.stdout.write(frame + "\n")
            sys.stdout.flush()
            time.sleep(2.0)
    except KeyboardInterrupt:
        pass
    finally:
        if tty:
            sys.stdout.write("\033[?25h\033[?1049l")  # show cursor, leave alt screen
            sys.stdout.flush()
    return 0


_SMI_SAFE_FLAGS = {
    "-ls",
    "--list",
    "-s",
    "--snapshot",
    "--snapshot_no_tty",
    "-v",
    "--version",
    "-l",
    "--local",
    "-f",
    "--filename",
    "-h",
    "--help",
}
_SMI_RO_WRAPPER = "/usr/local/bin/tt-device-mcp-smi-ro"


_TT_DEV_GLOB = "/dev/tenstorrent/*"


def device_nodes() -> list:
    """The device nodes, and only those. /dev/tenstorrent also holds a `by-id/`
    directory (0755 root:root) — sampling it for permissions reads as 'unlocked' on
    a locked host, so it must never be mistaken for a device."""
    import glob

    return sorted(p for p in glob.glob(_TT_DEV_GLOB) if os.path.basename(p).isdigit())


def _device_locked() -> bool:
    """True if /dev/tenstorrent is locked (group ttdev, not world-writable)."""
    import grp

    nodes = device_nodes()
    if not nodes:
        return False
    try:
        st = os.stat(nodes[0])
        if st.st_mode & 0o002:  # world-writable => cooperative
            return False
        return grp.getgrgid(st.st_gid).gr_name == _LOCK_GROUP
    except (OSError, KeyError):
        return False


def cmd_smi(args) -> int:
    """Run the REAL tt-smi in your terminal — read-only, fully interactive, and
    parallel-safe (telemetry doesn't block jobs). On a locked host it goes through
    a NOPASSWD read-only sudo wrapper for device access; on a cooperative host it
    runs tt-smi directly. We exec(), so tt-smi owns the terminal and exits cleanly
    (no proxy, no leftover terminal modes)."""
    smi_args = list(args.smi_args)
    bad = [a for a in smi_args if a.startswith("-") and a not in _SMI_SAFE_FLAGS]
    if bad:
        print(f"smi is read-only — these flags are not allowed: {' '.join(bad)}")
        return 2
    if _device_locked():
        if not os.path.exists(_SMI_RO_WRAPPER):
            print(
                f"smi: device is locked but {_SMI_RO_WRAPPER} is missing — run 'sudo tt-device-mcp lock' to (re)install it."
            )
            return 1
        cmd = ["sudo", "-n", _SMI_RO_WRAPPER, *smi_args]
    else:
        import shutil

        ttsmi = shutil.which("tt-smi") or "tt-smi"
        cmd = [ttsmi, *smi_args]
    try:
        os.execvp(cmd[0], cmd)  # replace this process; tt-smi takes over the terminal
    except OSError as exc:
        print(f"smi: failed to launch {cmd[0]}: {exc}")
        return 1


# ============== Lock management (root-only) ==============

_UDEV_RULE_PATH = "/etc/udev/rules.d/99-tenstorrent-ttdev.rules"
_UDEV_RULE = (
    "# Managed by tt-device-mcp. Restrict /dev/tenstorrent to group ttdev (0660) so\n"
    "# only broker-admitted jobs (gid=ttdev) open the device; bare-metal is denied.\n"
    'SUBSYSTEM=="tenstorrent", GROUP="ttdev", MODE="0660"\n'
    'KERNEL=="tenstorrent!*", GROUP="ttdev", MODE="0660"\n'
)
_LOCK_GROUP = "ttdev"
_BROKER_UNIT = "/etc/systemd/system/tt-device-broker.service"
_LOCK_BANNER = "/etc/profile.d/tt-device-mcp-banner.sh"
_LOCK_BANNER_TEXT = (
    'echo "*** Tenstorrent devices are LOCKED for fair sharing! Run device work via '
    "'tt-device-mcp run ...', smi via 'tt-device-mcp smi ...', etc. "
    "See 'tt-device-mcp help'. Bare-metal device access gets Permission denied. ***\" >&2\n"
)


def _require_root_broker() -> bool:
    if os.geteuid() != 0:
        print("Error: run with sudo — lock/unlock changes device permissions.")
        return False
    if not os.path.exists(_BROKER_UNIT):
        print(f"Error: no broker found ({_BROKER_UNIT}). Lock applies only to a broker host.")
        return False
    return True


def _udev_reload() -> bool:
    """Reload the rules and re-trigger the tenstorrent subsystem so a mode change lands
    on the already-enumerated node. Returns False if either udevadm call fails — the
    live node's permissions then can't be trusted to reflect the rule just written."""
    reload_rc = subprocess.run(["udevadm", "control", "--reload-rules"], check=False).returncode
    trigger_rc = subprocess.run(["udevadm", "trigger", "--subsystem-match=tenstorrent"], check=False).returncode
    return reload_rc == 0 and trigger_rc == 0


def _set_service_device_group(enable: bool) -> None:
    """Add/remove ``Environment=TT_DEVICE_MCP_DEVICE_GROUP=ttdev`` in the broker unit
    so privsep jobs run with gid=ttdev (DAC access to the locked node); daemon-reload."""
    text = Path(_BROKER_UNIT).read_text()
    has = "TT_DEVICE_MCP_DEVICE_GROUP" in text
    line = f"Environment=TT_DEVICE_MCP_DEVICE_GROUP={_LOCK_GROUP}\n"
    if enable and not has:
        text = text.replace("Environment=TT_DEVICE_MCP_PRIVSEP=1\n", "Environment=TT_DEVICE_MCP_PRIVSEP=1\n" + line, 1)
    elif not enable and has:
        text = "".join(ln for ln in text.splitlines(keepends=True) if "TT_DEVICE_MCP_DEVICE_GROUP" not in ln)
    else:
        return
    Path(_BROKER_UNIT).write_text(text)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


# Read-only tt-smi wrapper so `tt-device-mcp smi` works on a locked host: any user
# may run it via NOPASSWD sudo, and it enforces a read-only flag whitelist itself.
_SMI_RO_SUDOERS = "/etc/sudoers.d/tt-device-mcp-smi-ro"
_SMI_RO_WRAPPER_BODY = """#!/bin/sh
# Managed by tt-device-mcp. Read-only tt-smi wrapper, invoked via 'sudo -n' so a
# locked /dev/tenstorrent (root:ttdev 0660) stays readable by any user without
# granting write/reset access. Runs as root and any user may exec it, so it
# enforces a read-only flag whitelist itself - never allow reset/config flags.
set -eu
TTSMI=/opt/tt-device-broker/venv/bin/tt-smi
[ -x "$TTSMI" ] || TTSMI=tt-smi
for arg in "$@"; do
    case "$arg" in
    -*)
        case "$arg" in
        -ls|--list|-s|--snapshot|--snapshot_no_tty|-v|--version|-l|--local|-f|--filename|-h|--help) ;;
        *) echo "tt-device-mcp-smi-ro: refusing non-read-only flag: $arg" >&2; exit 2 ;;
        esac ;;
    esac
done
exec "$TTSMI" "$@"
"""
_SMI_RO_SUDOERS_BODY = (
    "# Managed by tt-device-mcp. Let any user run the read-only tt-smi wrapper as\n"
    "# root so 'tt-device-mcp smi' works on a locked host without a password.\n"
    f"ALL ALL=(root) NOPASSWD: {_SMI_RO_WRAPPER}\n"
)


def _install_smi_ro_wrapper() -> None:
    """Install the NOPASSWD read-only tt-smi wrapper + sudoers so `tt-device-mcp smi`
    works on a locked host. Idempotent. The wrapper runs as root and any user may
    exec it, so it enforces a read-only flag whitelist itself."""
    import shutil
    import tempfile

    Path(_SMI_RO_WRAPPER).write_text(_SMI_RO_WRAPPER_BODY)
    # Root-only. The only caller runs it as `sudo -n` (see smi()), so sudo execs it as
    # root and no invoking user ever needs its own read/execute bit — world/group access
    # would only widen a root-owned, sudo-gated script for nothing.
    os.chmod(_SMI_RO_WRAPPER, 0o700)
    os.chown(_SMI_RO_WRAPPER, 0, 0)
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write(_SMI_RO_SUDOERS_BODY)
        tmp = tf.name
    chk = subprocess.run(["visudo", "-cf", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if chk.returncode != 0:
        Path(tmp).unlink(missing_ok=True)
        print(f"Warning: sudoers validation failed; {_SMI_RO_SUDOERS} not installed: {chk.stderr.decode().strip()}")
        return
    # Owner-only. sudo reads sudoers as root and rejects only a file that is group- or
    # world-WRITABLE (or not root-owned); it does not require group-read, so 0o400 is the
    # tightest mode it still accepts.
    os.chmod(tmp, 0o400)
    os.chown(tmp, 0, 0)
    shutil.move(tmp, _SMI_RO_SUDOERS)


def _remove_smi_ro_wrapper() -> None:
    """Remove the read-only tt-smi wrapper + sudoers (used on unlock). Idempotent."""
    for path in (_SMI_RO_WRAPPER, _SMI_RO_SUDOERS):
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass


# Negative self-check payload: exits 13 on EACCES (the node denied a non-ttdev open, so
# the lock holds) and 0 if it opened cleanly (the lock leaked). A distinct 13 separates a
# real denial from a probe that never ran — a systemd-run failure returns its own code.
_DENY_PROBE = (
    "import os, sys\n"
    "try:\n"
    "    os.close(os.open(%r, os.O_RDONLY))\n"
    "    sys.exit(0)\n"
    "except PermissionError:\n"
    "    sys.exit(13)\n"
    "except OSError:\n"
    "    sys.exit(1)\n"
)


def cmd_lock(args) -> int:
    """Lock /dev/tenstorrent to group ttdev so all device work must go through the
    broker (deny bare-metal). For SHARED hosts only. Root-only; idempotent."""
    if not _require_root_broker():
        return 1
    subprocess.run(["groupadd", "--system", _LOCK_GROUP], check=False, stderr=subprocess.DEVNULL)
    # Service env first: privsep jobs must already get gid=ttdev before the node locks.
    _set_service_device_group(True)
    restart_rc = subprocess.run(["systemctl", "restart", "tt-device-broker"], check=False).returncode
    if restart_rc != 0:
        print(
            f"Warning: 'systemctl restart tt-device-broker' exited {restart_rc}; privsep jobs "
            "may not hold gid=ttdev until it restarts — admitted device work can be denied."
        )
    Path(_UDEV_RULE_PATH).write_text(_UDEV_RULE)
    udev_ok = _udev_reload()
    if not udev_ok:
        print(
            "Warning: udevadm reload/trigger exited non-zero; the new rule may not have "
            "reached the live node — the self-checks below decide whether the lock is real."
        )
    time.sleep(1)
    nodes = device_nodes()  # never `by-id/`: opening a directory would pass vacuously
    if nodes:
        # Positive self-check: an admitted (gid=ttdev) job must still open the node, or the
        # lock has bricked the box for legitimate broker work.
        admitted_ok = (
            subprocess.run(
                [
                    "systemd-run",
                    "--scope",
                    "--quiet",
                    "--uid=nobody",
                    f"--gid={_LOCK_GROUP}",
                    sys.executable,
                    "-c",
                    f"import os; os.close(os.open({nodes[0]!r}, os.O_RDONLY))",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if not admitted_ok:
            print("Self-check FAILED: an admitted (gid=ttdev) job could not open the device.")
            print("Reverting to cooperative so the host isn't bricked.")
            return cmd_unlock(args)
        # Negative self-check: a non-ttdev identity must be DENIED, else the "lock" denies
        # nobody and printing LOCKED would be a lie (silent fail-open). Same nobody uid as
        # above, minus gid=ttdev — the only difference is the group the node gates on.
        deny_rc = subprocess.run(
            ["systemd-run", "--scope", "--quiet", "--uid=nobody", sys.executable, "-c", _DENY_PROBE % nodes[0]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        if deny_rc == 0:
            print(
                "Self-check FAILED: /dev/tenstorrent is still openable without gid=ttdev — "
                "the lock did not take effect; bare-metal is NOT denied."
            )
            print("Reverting to cooperative so the state isn't a false lock.")
            cmd_unlock(args)
            return 1
        if deny_rc != 13:
            # The probe reached no verdict (systemd-run failed, node error). The rule is
            # applied, so don't tear down a possibly-good lock — but don't claim a verified
            # one either; a bad node would already have failed the positive check above.
            print(
                f"Self-check INCONCLUSIVE: the deny probe exited {deny_rc} (expected 13=denied). "
                "The rule is applied but denial is UNVERIFIED — confirm before trusting the lock."
            )
            return 1
    _install_smi_ro_wrapper()
    Path(_LOCK_BANNER).write_text(_LOCK_BANNER_TEXT)
    print(f"LOCKED: /dev/tenstorrent is 0660 root:{_LOCK_GROUP}; bare-metal denied.")
    print("Undo: sudo tt-device-mcp unlock")
    return 0


def cmd_unlock(args) -> int:
    """Remove the device lock — restore world-rw (cooperative); bare-metal allowed.
    Root-only; idempotent."""
    if not _require_root_broker():
        return 1
    try:
        Path(_UDEV_RULE_PATH).unlink()
    except FileNotFoundError:
        pass
    _udev_reload()
    _set_service_device_group(False)
    try:
        Path(_LOCK_BANNER).unlink()
    except FileNotFoundError:
        pass
    subprocess.run(["systemctl", "restart", "tt-device-broker"], check=False)
    _remove_smi_ro_wrapper()
    print("UNLOCKED: /dev/tenstorrent restored to world-rw (cooperative); bare-metal allowed.")
    return 0


def cmd_timezone(args) -> int:
    """Show, list, or set this user's timezone for the TIME column. Stored per-user,
    so tenants on a shared box don't affect each other. No root, no daemon."""
    if args.list is not None:
        q = args.list.strip().lower()
        if not q:  # bare --list: the shortlist, sorted west-to-east by offset
            rows = sorted(((zone_offsets(z), z) for z in _COMMON_ZONES), key=lambda r: _offset_sort_key(r[0]))
            for off, z in rows:
                print(f"  {z.rsplit('/', 1)[-1]:<16} {off}")
            print(f"\n{len(canonical_zones())} zones exist — search yours: tt-device-mcp timezone --list <city>")
            return 0

        hits = [z for z in canonical_zones() if q in z.lower()]  # already sorted by zone
        if not hits:
            # The city may exist only as a legacy alias (Europe/Zagreb). Don't leave
            # the user empty-handed — point at the canonical zones on the same clock.
            from zoneinfo import available_timezones

            alias = sorted(z for z in available_timezones() if q in z.lower() and "/" in z)
            if alias:
                clock = zone_offsets(alias[0])
                same = [z for z in _COMMON_ZONES if zone_offsets(z) == clock] or [
                    z for z in canonical_zones() if zone_offsets(z) == clock
                ]
                cities = ", ".join(z.rsplit("/", 1)[-1] for z in same[:6])
                print(f"  {alias[0]} is an alias, not a canonical zone ({clock}).")
                print(f"  Same clock: {cities}")
                return 0
            print(f"No timezone matches {args.list!r}. (Names are IANA zones: Area/City.)")
            return 1
        for z in hits:
            print(f"  {z.rsplit('/', 1)[-1]:<20} {zone_offsets(z):<12} {z}")
        print(f"\nSet with the city: tt-device-mcp timezone {hits[0].rsplit('/', 1)[-1]}")
        return 0

    p = _tz_config_path()
    zone = "_".join(args.zone) if args.zone else None  # rejoin an unquoted city
    if zone is None:
        _, label = _resolve_tz()
        if os.environ.get("TT_DEVICE_MCP_TZ"):
            src = "TT_DEVICE_MCP_TZ"
        elif p.exists():
            src = str(p)
        elif os.environ.get("TZ"):
            src = "$TZ"
        else:
            src = f"default, {_TZ_DEFAULT}"
        print(f"{label}  ({src})")
        print("Change it: tt-device-mcp timezone <City>   |   find one: tt-device-mcp timezone --list <text>")
        return 0

    val = zone.strip()
    if val.lower() in ("pacific", "default"):
        p.unlink(missing_ok=True)
    elif val.lstrip("+-").isdigit():
        if not -12 <= int(val) <= 14:
            print("Error: a fixed UTC offset must be -12..14.")
            return 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{int(val)}\n")
    else:
        # Store the resolved full key: the short name is only an input convenience.
        try:
            key = zone_from_name(val)
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{key}\n")
    _resolve_tz.cache_clear()
    _, label = _resolve_tz()
    print(f"Timezone: {label}")
    return 0


def cmd_refresh_banner(args) -> int:
    """Reconcile the login banner with the lock state — write it when locked,
    remove it when not. Idempotent; called from apply-host-config so a banner-text
    change reaches every host on the next update without re-locking."""
    if os.geteuid() != 0:
        print("Error: run with sudo")
        return 1
    p = Path(_LOCK_BANNER)
    if _device_locked():
        if not p.exists() or p.read_text() != _LOCK_BANNER_TEXT:
            p.write_text(_LOCK_BANNER_TEXT)
    else:
        p.unlink(missing_ok=True)
    return 0


# ============== Main ==============


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TT Device MCP - Tenstorrent device queue server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  tt-device-mcp run "pytest test.py -v"   Run, stream output, wait for exit (blocking)
  tt-device-mcp run-bg "pytest test.py"   Queue, print a job_id, return now (non-blocking)
  tt-device-mcp wait 042                  Attach to a backgrounded job's id; wait + stream
  tt-device-mcp status                    Show queue + recent jobs (all users)
  tt-device-mcp logs -f                   Tail your latest job's logs
""",
    )
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--log-dir", "-l", type=str, default=None, help="Log directory (default: /tmp/tt-device-mcp-<uid>/)"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # Daemon commands
    daemon_parser = subparsers.add_parser(
        "daemon", help="<start|stop|status|start-fg>  — per-user standalone daemon (no root; reserved boxes)"
    )
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command")
    daemon_sub.add_parser("start", help="Start the per-user daemon (socket; detaches)")
    daemon_sub.add_parser("stop", help="Stop the per-user daemon")
    daemon_sub.add_parser("status", help="Check the per-user daemon")
    daemon_sub.add_parser("start-fg", help="Run in foreground (debugging)")

    # Job commands
    run_parser = subparsers.add_parser("run", help="<cmd> [-w dir] [-t sec]  — run, stream, wait for exit (blocking)")
    run_parser.add_argument("cmd", help="Command to run")
    run_parser.add_argument("-w", "--workspace", help="Workspace path (default: cwd)")
    run_parser.add_argument("-e", "--env", help="Env file path")
    run_parser.add_argument("-t", "--timeout", type=int, default=600, help="Timeout in seconds")
    run_parser.add_argument(
        "-o", "--output-lines", type=int, default=0, help="Show last N lines summary at end (default: 0 = no summary)"
    )

    exec_parser = subparsers.add_parser(
        "exec",
        help="<cmd> [-t sec] [-f]  — run a diagnostic (tt-triage/tt-smi) directly, bypassing the queue; for hung jobs",
    )
    exec_parser.add_argument("cmd", help="Diagnostic command to run (e.g. 'tt-triage 0')")
    exec_parser.add_argument("-t", "--timeout", type=int, default=180, help="Timeout in seconds (default 180, max 600)")
    exec_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Run even if another tenant owns the running job (read-only tools only; runs alongside theirs)",
    )

    run_bg_parser = subparsers.add_parser(
        "run-bg", help="<cmd> [-w dir] [-t sec]  — queue, print a job_id, return now; track with status/wait/logs"
    )
    run_bg_parser.add_argument("cmd", help="Command to run")
    run_bg_parser.add_argument("-w", "--workspace", help="Workspace path (default: cwd)")
    run_bg_parser.add_argument("-e", "--env", help="Env file path")
    run_bg_parser.add_argument("-t", "--timeout", type=int, default=600, help="Timeout in seconds")

    status_parser = subparsers.add_parser(
        "status", help="[N] [-j ID]  — queue + last N recent (all users, default 15); -j for one job's detail"
    )
    status_parser.add_argument(
        "target",
        nargs="?",
        help="How many recent jobs to show (e.g. 'status 30', default 15)",
    )
    status_parser.add_argument(
        "-j",
        "--job",
        default=None,
        metavar="ID",
        help="Show this job's detail instead (e.g. -j 042)",
    )

    logs_parser = subparsers.add_parser(
        "logs", help="[job_id] [-f]  — show or tail a job's logs (default: your latest)"
    )
    logs_parser.add_argument("job_id", nargs="?", help="Job ID (default: your latest)")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    logs_parser.add_argument("-n", "--lines", type=int, default=100, help="Number of lines")

    kill_parser = subparsers.add_parser("kill", help="[job_id]  — kill running / cancel queued (default: pick yours)")
    kill_parser.add_argument("job_id", nargs="?", help="Job ID (optional)")

    wait_parser = subparsers.add_parser("wait", help="<job_id>  — wait for a job to finish + stream its logs")
    wait_parser.add_argument("job_id", help="Job ID")

    watch_parser = subparsers.add_parser(
        "watch", help="[N]  — live status, refreshing in place (recent count, default 15)"
    )
    watch_parser.add_argument("count", nargs="?", type=int, help="How many recent jobs to show (default 15)")

    smi_parser = subparsers.add_parser(
        "smi", help="[tt-smi args]  — live tt-smi (read-only) in parallel with jobs; Ctrl-C to exit"
    )
    smi_parser.add_argument("smi_args", nargs=argparse.REMAINDER, help="tt-smi args (read-only; reset/config rejected)")

    reset_parser = subparsers.add_parser(
        "reset", help="[--force]  — reset devices (refused if a foreign tenant holds them)"
    )
    reset_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Override the reset gate even if another user's process holds the device",
    )

    subparsers.add_parser("pre-step", help="health pass before a job step (Slurm Prolog); exit 0 iff fit and free")

    post_step_parser = subparsers.add_parser(
        "post-step", help="recover after a job step (Slurm Epilog); exit 0 iff fit"
    )
    post_step_parser.add_argument(
        "--exit-code",
        type=_step_exit_code,
        default=0,
        help="the finished step's exit code; non-zero forces the fabric check. Accepts Slurm's "
        "<exit>:<signal> spelling and its wait(2) statuses",
    )
    post_step_parser.add_argument(
        "--no-reclaim", action="store_true", help="do not signal processes still holding the device"
    )

    subparsers.add_parser("lock", help="[sudo]  — lock devices to the broker; deny bare-metal (shared hosts)")
    subparsers.add_parser("unlock", help="[sudo]  — remove the lock; restore bare-metal (cooperative)")
    tz_parser = subparsers.add_parser(
        "timezone", help="[City] [--list TEXT]  — show/set YOUR timezone for the TIME column (per-user)"
    )
    # nargs="*" so an unquoted multi-word city (`timezone Los Angeles`) works — the
    # shell splits it, and we rejoin. The underscore form is what --list prints.
    tz_parser.add_argument(
        "zone",
        nargs="*",
        help="City (Los_Angeles, or unquoted: Los Angeles) or full IANA name; "
        "a bare integer pins a fixed UTC offset (-8); 'pacific' restores the default",
    )
    tz_parser.add_argument(
        "--list",
        nargs="?",
        const="",
        default=None,
        metavar="TEXT",
        help="List timezone names matching TEXT (e.g. --list bel)",
    )
    subparsers.add_parser("refresh-banner", help=argparse.SUPPRESS)  # internal: reconcile login banner on update
    subparsers.add_parser("help", help="  — show this help (same as --help)")

    # `smi` forwards everything after it to tt-smi verbatim, including -flags
    # (argparse REMAINDER won't capture a leading dash). Split manually.
    argv = sys.argv[1:]
    # An MCP client (Claude) spawns the bare binary and speaks JSON-RPC over a
    # piped stdin; a human on a tty gets the CLI. Any argv means a CLI verb.
    if not argv and not sys.stdin.isatty():
        from tt_device_mcp.stdio_shim import serve_stdio

        serve_stdio()
        return 0
    if argv and argv[0] == "smi":
        args = parser.parse_args(["smi"])
        args.smi_args = argv[1:]
    else:
        args = parser.parse_args()

    if args.command is None or args.command == "help":
        parser.print_help()
        return 1

    # Route to command handler
    if args.command == "daemon":
        if args.daemon_command is None:
            daemon_parser.print_help()
            return 1
        handlers = {
            "start": cmd_daemon_start,
            "stop": cmd_daemon_stop,
            "status": cmd_daemon_status,
            "start-fg": cmd_daemon_start_fg,
        }
        return handlers[args.daemon_command](args)

    handlers = {
        "run": cmd_run,
        "exec": cmd_exec,
        "run-bg": cmd_run_bg,
        "status": cmd_status,
        "logs": cmd_logs,
        "kill": cmd_kill,
        "wait": cmd_wait,
        "watch": cmd_watch,
        "smi": cmd_smi,
        "reset": cmd_reset,
        "pre-step": cmd_pre_step,
        "post-step": cmd_post_step,
        "lock": cmd_lock,
        "unlock": cmd_unlock,
        "timezone": cmd_timezone,
        "refresh-banner": cmd_refresh_banner,
    }

    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
