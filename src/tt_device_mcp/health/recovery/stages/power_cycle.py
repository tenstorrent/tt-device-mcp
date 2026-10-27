# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cold BMC chassis power-cycle recovery rung — the final one on the ladder."""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Callable, Optional

from tt_device_mcp.health.evidence import health_dir, health_event

_logger = logging.getLogger("tt-device-mcp")

# Spec 04 I26. The pre-power-cycle hook gets this long by default to drain whatever else on the host
# would be cut by the cycle; past it the hook's process group is killed and the cycle goes ahead.
PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC = 300.0
# Ceiling on a configured timeout: the hook delays recovery of a box that is already down, so a
# typo (3000000) must never read as "wait forever".
PRE_POWER_CYCLE_HOOK_TIMEOUT_MAX_SEC = 3600.0
# Only the tail of the hook's output reaches the log line; the rest stays in the temp file.
HOOK_OUTPUT_TAIL_BYTES = 2048


DEFAULT_POWER_CYCLE_ARGV = ("ipmitool", "chassis", "power", "cycle")
# The auto rung's ipmitool answers in seconds; the configured command (I27) brings its own timeout.
DEFAULT_POWER_CYCLE_TIMEOUT_SEC = 30


class PowerCycleNotLaunched(RuntimeError):
    """The power cycle could not start with the OS brought down first, and nothing fired (spec 04
    I88): the caller retracts the ledger entry, keeps the hold, pages, and retries under I76. Never a
    timeout: a cycle that did not launch must not read as one that may still act."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def _fire_power_cycle() -> None:
    """The BMC chassis power cycle — the one action that recovers a whole-bus wedge a warm reboot
    leaves wedged (measured on box D) — with the host OS brought down cleanly first (spec 04 I88).
    Every test replaces it. Raises PowerCycleNotLaunched when the clean path cannot start."""
    fire_os_first(list(DEFAULT_POWER_CYCLE_ARGV), "auto BMC power cycle", DEFAULT_POWER_CYCLE_TIMEOUT_SEC)


# Spec 04 I88. The power cycle brings the OS down first: the broker leaves the cycle's argv in
# REQUEST_FILE, starts a fallback unit that fires it once SHUTDOWN_BOUND passes, then asks systemd to
# reboot. The installed systemd-shutdown hook (deploy/tt-device-mcp-power-cycle.shutdown) fires it
# after the filesystems are read-only; the fallback fires it if the shutdown hangs before that.
SHUTDOWN_HOOK_NAME = "tt-device-mcp-power-cycle.shutdown"
SHUTDOWN_HOOK_DIRS = ("/usr/lib/systemd/system-shutdown", "/lib/systemd/system-shutdown")
# /run survives into systemd-shutdown (an API mount it never unmounts) and is empty on the next boot,
# so a request can never outlive the boot it was written in.
REQUEST_FILE = "/run/tt-device-mcp-power-cycle.request"
FALLBACK_UNIT_PREFIX = "tt-device-mcp-power-cycle-fallback"
# systemd-shutdown's killall spares a root process whose argv[0] starts with '@', so the fallback
# lives through it and still bounds a hang after the hand-over (umount, the hooks themselves). The
# hook finds it by the pid it writes and stops it before firing the cycle itself.
FALLBACK_ARGV0 = "@" + FALLBACK_UNIT_PREFIX
FALLBACK_FIRE_ARGV0 = "@tt-device-mcp-power-cycle-fire"
# Spec 04 I99. Past the bound the fallback cycles a shutdown that hangs, so the bound must cover a
# slow shutdown that still ends with the filesystems read-only: a clean reboot of a Galaxy host
# reached systemd-shutdown in 86 s (measured), and one unit that hangs (an unmount, a service stop)
# adds up to systemd's own timeout for it before systemd gives up on it and goes on (a mount unit
# timed out at 90 s and the power went off 116 s after the request, 4 s inside a fixed 120 s bound).
# So the bound is the clean shutdown, plus the host's longer default unit timeout (start covers
# mount units, stop covers services), plus a margin: 210 s with systemd's 90 s defaults. Read from
# the host at each request; a value it cannot read counts as systemd's default. Never off, never
# past MAX, so the cycle stays bounded.
SHUTDOWN_CLEAN_SEC = 90
SHUTDOWN_MARGIN_SEC = 30
SYSTEMD_DEFAULT_UNIT_TIMEOUT_SEC = 90
SHUTDOWN_BOUND_MAX_SEC = 600
SYSTEMD_TIMEOUT_PROPERTIES = ("DefaultTimeoutStartUSec", "DefaultTimeoutStopUSec")
STEP_TIMEOUT_SEC = 30
# The read-only BMC check before anything is written: a BMC that does not answer it cannot cycle, so
# the host would only reboot warm (F2 of the I88 review).
PREFLIGHT_TIMEOUT_SEC = 10
# systemd-shutdown kills a hook still running after 90 s and reboots warm. The hook's worst case —
# the RAID wait and the IPMI driver load side by side, the SEL record, every try at its cap, the
# pauses between them and the wait for power to drop after a try that worked — stays inside it
# (hook_worst_case_sec).
SYSTEMD_SHUTDOWN_HOOK_TIMEOUT_SEC = 90
HOOK_MDADM_WAIT_SEC = 10
HOOK_IPMI_PREPARE_SEC = 10
HOOK_SEL_TIMEOUT_SEC = 2
HOOK_TRIES = 3
HOOK_RETRY_PAUSE_SEC = 2
HOOK_POWER_DROP_WAIT_SEC = 25
HOOK_TRY_TIMEOUT_MIN_SEC = 5
HOOK_TRY_TIMEOUT_MAX_SEC = 15
# Runs in the fallback unit under bash, as FALLBACK_ARGV0: record its pid for the hook, wait out the
# bound inside bash (a sleep child would die in the killall and fire the cycle early), flush what it
# can without blocking on it, have the installed hook load the IPMI drivers again if a service stopped
# them (a hung shutdown may hang past that stop), then cycle under the command's timeout. Args: pid
# file, bound, timeout, argv.
FALLBACK_SH = (
    'printf "%s\\n" "$$" >"$1" 2>/dev/null; b=$2; t=$3; shift 3\n'
    'read -rt "$b" <> <(:)\n'
    "sync & read -rt 2 <> <(:)\n"
    f"for h in {' '.join(d + '/' + SHUTDOWN_HOOK_NAME for d in SHUTDOWN_HOOK_DIRS)}; do\n"
    f'    [ -x "$h" ] && {{ timeout {HOOK_IPMI_PREPARE_SEC} "$h" --prepare-ipmi >/dev/null 2>&1; break; }}\n'
    "done\n"
    f'exec -a {FALLBACK_FIRE_ARGV0} timeout "$t" "$@"\n'
)
# Re-execs bash with argv[0] set to its own $0 (FALLBACK_ARGV0), then runs FALLBACK_SH.
FALLBACK_LAUNCH_SH = 'exec -a "$0" /bin/bash -c "$1" "$0" "${@:2}"'
REBOOT_ARGV = ("systemctl", "--no-block", "-i", "reboot")


def hook_try_timeout_sec(timeout: float) -> int:
    """The per-try timeout the shutdown hook gives the cycle command: the command's own timeout,
    clamped so the hook's worst case fits systemd-shutdown's 90 s."""
    try:
        t = int(timeout)
    except (TypeError, ValueError, OverflowError):
        t = DEFAULT_POWER_CYCLE_TIMEOUT_SEC
    return max(HOOK_TRY_TIMEOUT_MIN_SEC, min(t, HOOK_TRY_TIMEOUT_MAX_SEC))


def hook_worst_case_sec() -> int:
    """The shutdown hook's longest run: under SYSTEMD_SHUTDOWN_HOOK_TIMEOUT_SEC by construction."""
    return (
        max(HOOK_MDADM_WAIT_SEC, HOOK_IPMI_PREPARE_SEC)
        + HOOK_SEL_TIMEOUT_SEC
        + HOOK_TRIES * HOOK_TRY_TIMEOUT_MAX_SEC
        + (HOOK_TRIES - 1) * HOOK_RETRY_PAUSE_SEC
        + HOOK_POWER_DROP_WAIT_SEC
    )


_TIMESPAN_UNITS = {
    "us": 1e-6,
    "usec": 1e-6,
    "ms": 1e-3,
    "msec": 1e-3,
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}
_TIMESPAN_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


def parse_systemd_timespan(text: str) -> Optional[float]:
    """Seconds in a timespan as ``systemctl show`` prints it (``1min 30s``, ``90s``, ``500ms``),
    ``inf`` for ``infinity``, None when it does not parse."""
    t = (text or "").strip().lower()
    if t == "infinity":
        return float("inf")
    if not t:
        return None
    total, pos = 0.0, 0
    for m in _TIMESPAN_PART.finditer(t):
        if t[pos : m.start()].strip() or m.group(2) not in _TIMESPAN_UNITS:
            return None
        total += float(m.group(1)) * _TIMESPAN_UNITS[m.group(2)]
        pos = m.end()
    if pos == 0 or t[pos:].strip():
        return None
    return total


def host_unit_timeout_sec() -> float:
    """The longer of the host's systemd DefaultTimeoutStartSec and DefaultTimeoutStopSec, read-only
    and bounded; systemd's 90 s default when the manager does not answer or a value does not parse.
    ``inf`` when either is infinity."""
    argv = ["systemctl", "show", "--property=" + ",".join(SYSTEMD_TIMEOUT_PROPERTIES)]
    rc, out = _run_query(argv, PREFLIGHT_TIMEOUT_SEC)
    seen = {}
    for ln in (out or "").splitlines() if rc == 0 else ():
        key, _, val = ln.partition("=")
        if key.strip() in SYSTEMD_TIMEOUT_PROPERTIES:
            v = parse_systemd_timespan(val)
            if v is not None and v >= 0:
                seen[key.strip()] = v
    if not seen:
        return float(SYSTEMD_DEFAULT_UNIT_TIMEOUT_SEC)
    return max(seen.values())


def shutdown_bound_sec(unit_timeout: Optional[float] = None) -> int:
    """How long the clean shutdown gets before the fallback cycles the host anyway (spec 04 I99):
    SHUTDOWN_CLEAN_SEC + the host's unit timeout (:func:`host_unit_timeout_sec` unless given) +
    SHUTDOWN_MARGIN_SEC. ``TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC`` may only raise it; a lower or
    malformed value is ignored. Always within SHUTDOWN_BOUND_MAX_SEC (``inf`` reads as that cap), so
    no host and no setting turns the bound off."""
    if unit_timeout is None:
        unit_timeout = host_unit_timeout_sec()
    if not unit_timeout >= 0:  # NaN
        unit_timeout = float(SYSTEMD_DEFAULT_UNIT_TIMEOUT_SEC)
    bound = min(SHUTDOWN_CLEAN_SEC + unit_timeout + SHUTDOWN_MARGIN_SEC, float(SHUTDOWN_BOUND_MAX_SEC))
    try:
        v = float(os.environ.get("TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC", "").strip())
    except ValueError:
        v = 0.0
    if v == v and v > bound:
        bound = min(v, float(SHUTDOWN_BOUND_MAX_SEC))
    return int(bound)


def installed_shutdown_hook() -> str:
    """The installed, executable shutdown hook's path, or "" when there is none."""
    for d in SHUTDOWN_HOOK_DIRS:
        p = os.path.join(d, SHUTDOWN_HOOK_NAME)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def os_first_unavailable() -> str:
    """Why the clean path cannot start here, or "" when it can."""
    if not os.path.isdir("/run/systemd/system"):
        return "systemd is not running as init"
    if not installed_shutdown_hook():
        return f"no executable {SHUTDOWN_HOOK_NAME} in {' or '.join(SHUTDOWN_HOOK_DIRS)}"
    for tool in ("systemctl", "systemd-run", "timeout"):
        if shutil.which(tool) is None:
            return f"{tool} not found"
    if not os.access("/bin/bash", os.X_OK):
        return "/bin/bash not found (the fallback needs it)"
    return ""


def _one_line(reason: str) -> str:
    return " ".join(str(reason).split())[:300]


# Spec 04 I88. What asked for the cycle, journaled just before the fallback unit starts: the broker's
# escalation notes it (note_power_cycle_trigger) and fire_os_first takes it once. A note older than any
# pre-cycle hook could take is from an escalation that never fired and is not used.
_trigger: dict = {}
TRIGGER_MAX_AGE_SEC = PRE_POWER_CYCLE_HOOK_TIMEOUT_MAX_SEC + 600.0


def note_power_cycle_trigger(reason: str, fault_class: str = "", chips: Optional[dict] = None, hold: str = "") -> None:
    """Record why the next cycle fires: the escalation's reason (the auto_recovery row's text), the
    fault class, ``{chip index: PCI address or None}`` and the hold episode."""
    _trigger.clear()
    _trigger.update(reason=reason, fault_class=fault_class, chips=dict(chips or {}), hold=hold, at=time.monotonic())


def power_cycle_trigger_text(reason: str) -> str:
    """One line naming the cycle's trigger; the passed reason alone when no fresh note is there."""
    t = dict(_trigger)
    _trigger.clear()
    if not t or time.monotonic() - t.get("at", 0.0) > TRIGGER_MAX_AGE_SEC:
        t = {}
    chips = sorted((t.get("chips") or {}).items(), key=lambda i: (len(str(i[0])), str(i[0])))
    named = ", ".join(f"{c} ({bdf or 'address unknown'})" for c, bdf in chips)
    parts = [
        f"class={t.get('fault_class') or 'unknown'}",
        f"chips=[{named}]",
        f"reason={_one_line(t.get('reason') or reason)}",
    ]
    if t.get("hold"):
        parts.append(f"hold={t['hold']}")
    return " ".join(" ".join(parts).split())[:900]


def _journal_trigger(reason: str, unit: str) -> str:
    """Journal the trigger under the broker before the fallback unit starts; the returned text goes
    in the unit's description. Best effort: never raises, never delays the cycle."""
    try:
        text = power_cycle_trigger_text(reason)
        _logger.warning("POWER CYCLE TRIGGER (unit %s): %s", unit, text)
        return text
    except Exception as exc:  # noqa: BLE001 - logging only; never holds the cycle
        try:
            _logger.error("POWER CYCLE TRIGGER: could not journal it (%r); cycling anyway", exc)
        except Exception:  # noqa: BLE001
            pass
        return ""


def fallback_pid_file() -> str:
    """Where the fallback records its pid for the hook: beside the request, so both share /run."""
    base = REQUEST_FILE[: -len(".request")] if REQUEST_FILE.endswith(".request") else REQUEST_FILE
    return base + ".fallback.pid"


def request_text(argv: list, reason: str, now: float, timeout: float = DEFAULT_POWER_CYCLE_TIMEOUT_SEC) -> str:
    """The request the shutdown hook reads: the epoch, the reason on one line, the per-try timeout,
    then one argv word per line. The hook ignores a request with no argv or one older than an hour."""
    return "\n".join([str(int(now)), _one_line(reason), str(hook_try_timeout_sec(timeout)), *argv]) + "\n"


def fallback_argv(
    argv: list, bound: int, unit: str, *, reason: str = "", timeout: float = STEP_TIMEOUT_SEC, trigger: str = ""
) -> list:
    """The transient unit that cycles a hung shutdown. DefaultDependencies=no keeps it out of the
    shutdown transaction, and its '@' argv[0] keeps it alive through systemd-shutdown's killall, so it
    bounds every stage up to the hook, which stops it by its recorded pid and fires instead. The cycle
    command gets the reason in ``TT_DEVICE_MCP_POWER_CYCLE_REASON`` and runs under ``timeout``; the
    ``trigger`` line goes in the unit's description, so the unit's own journal says why."""
    return [
        "systemd-run",
        f"--unit={unit}",
        "--collect",
        "--property=DefaultDependencies=no",
        f"--setenv=TT_DEVICE_MCP_POWER_CYCLE_REASON={_one_line(reason)}",
        "--description=tt-device-mcp: power-cycle a hung shutdown (spec 04 I88)" + (f": {trigger}" if trigger else ""),
        "/bin/bash",
        "-c",
        FALLBACK_LAUNCH_SH,
        FALLBACK_ARGV0,
        FALLBACK_SH,
        fallback_pid_file(),
        str(int(bound)),
        str(max(1, int(timeout))),
        *argv,
    ]


def bmc_query_argv(argv: list, query: list) -> Optional[list]:
    """A read-only BMC query on the same BMC and credentials as the cycle command, or None when there
    is none to derive: ``ipmitool [options] chassis power cycle`` becomes ``ipmitool [options]
    <query>``. Any other command is opaque."""
    argv = [str(a) for a in argv]
    if len(argv) >= 4 and os.path.basename(argv[0]) == "ipmitool" and argv[-3:] == ["chassis", "power", "cycle"]:
        return [*argv[:-3], *query]
    return None


def preflight_argv(argv: list) -> Optional[list]:
    """The pre-flight for a cycle command: ``mc info`` on its BMC (a BMC that does not answer it
    cannot cycle). A command that is not ipmitool gets only the executable check."""
    return bmc_query_argv(argv, ["mc", "info"])


# IPMI 2.0 Get System Restart Cause (NetFn chassis 0x00, cmd 0x07, table 28-11): the low nibble of the
# first response byte. ipmitool's `chassis restart_cause` names only 0x0-0x9 and prints "invalid" for
# the rest, so the raw code is read and named here. 0xc-0xf are reserved.
RESTART_CAUSES = {
    0x0: "unknown",
    0x1: "chassis power control command",
    0x2: "reset via pushbutton",
    0x3: "power-up via pushbutton",
    0x4: "watchdog expired",
    0x5: "oem",
    0x6: "power-up due to always-restore power policy",
    0x7: "power-up due to restore-previous power policy",
    0x8: "reset via pef",
    0x9: "power-cycle via pef",
    0xA: "soft reset",
    0xB: "power-up via rtc wakeup",
}
# The causes that mean the power really went off and on. A warm reboot is none of these: BMCs report
# it as "unknown", a soft reset or another reset.
CYCLE_RESTART_CAUSES = (
    "chassis power control command",
    "power-cycle via pef",
    "power-up via pushbutton",
    "power-up due to always-restore power policy",
    "power-up due to restore-previous power policy",
    "power-up via rtc wakeup",
)


_HEX_BYTES = re.compile(r"^\s*[0-9a-fA-F]{2}(\s+[0-9a-fA-F]{2})*\s*$")


def restart_cause_is_cycle(cause: str) -> bool:
    return cause.strip().lower() in CYCLE_RESTART_CAUSES


def restart_cause_is_mapped(cause: str) -> bool:
    """Whether ``cause`` is one the IPMI spec defines (``unknown`` included); an unmapped code
    (``unmapped code 0x0c``) says nothing about how the host restarted."""
    return cause.strip().lower() in RESTART_CAUSES.values()


def read_restart_cause(argv: list) -> Optional[str]:
    """The BMC's last system restart cause, lower case, for the BMC the cycle command ``argv`` drives:
    the raw Get System Restart Cause (``ipmitool [options] raw 0x00 0x07``, read-only, bounded by
    PREFLIGHT_TIMEOUT_SEC) named from :data:`RESTART_CAUSES`, or ``unmapped code 0x..`` for a
    reserved code. None when there is no query to derive, the BMC does not answer, does not support
    it (some answer "Unspecified error") or the answer does not parse."""
    return read_restart_cause_why(argv)[0]


# The longest BMC answer kept in a "why the cause is unavailable" reason.
_CAUSE_WHY_MAX = 160


def read_restart_cause_why(argv: list) -> tuple:
    """``(cause, why)``: the cause as :func:`read_restart_cause` reads it with ``why`` empty, or None
    and a short reason it could not be read (no query for the command, no executable, no answer in
    time, the BMC's error, an answer that does not parse), so a record says why it has no cause."""
    probe = bmc_query_argv(argv, ["raw", "0x00", "0x07"])
    if probe is None:
        return None, "the cycle command is not ipmitool chassis power cycle: no BMC query to derive"
    exe = _resolve_command(probe[0])
    if exe is None:
        return None, f"{probe[0]} not found"
    rc, out = _run_step([exe, *probe[1:]], PREFLIGHT_TIMEOUT_SEC)
    last = next((ln.strip() for ln in reversed((out or "").splitlines()) if ln.strip()), "")
    if rc is None:
        return None, f"the BMC query did not finish in {PREFLIGHT_TIMEOUT_SEC}s: {last}"[:_CAUSE_WHY_MAX]
    if rc != 0:
        return None, f"the BMC refused the query (rc {rc}): {last or 'no output'}"[:_CAUSE_WHY_MAX]
    # The response bytes are the last all-hex line (stdout follows any stderr warning).
    lines = [ln.split() for ln in (out or "").splitlines() if _HEX_BYTES.match(ln)]
    if not lines:
        return None, f"the BMC answer does not parse: {last or 'no output'}"[:_CAUSE_WHY_MAX]
    code = int(lines[-1][0], 16) & 0x0F
    return RESTART_CAUSES.get(code, f"unmapped code 0x{code:02x}"), ""


# The SEL fallback for a BMC that answers no restart cause: the last records are enough (the power-off
# is logged moments before the boot) and `sel list last N` reads only those, about 40 ms each.
SEL_TAIL_RECORDS = 40
SEL_TIMEOUT_SEC = 5
# Slack around [request, boot] once the record time is moved onto the host's clock (1 s SEL
# resolution, the query's own latency).
SEL_WINDOW_SLACK_SEC = 30
# Power Unit (sensor type 09h) offsets 0 and 1: the power went off, or the BMC cycled it.
SEL_POWER_OFF_EVENTS = ("power off/down", "power cycle")
_SEL_TIME_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%y %H:%M:%S")


def _sel_naive_epoch(text: str) -> Optional[float]:
    """A SEL date and time as ipmitool prints them, read as UTC whatever zone the BMC keeps: only its
    difference to the BMC's own clock (``sel time get``, printed the same way) is used."""
    for fmt in _SEL_TIME_FORMATS:
        try:
            return float(calendar.timegm(time.strptime(" ".join(text.split()), fmt)))
        except ValueError:
            continue
    return None


def read_sel_power_off(argv: list, since: float, until: float) -> Optional[float]:
    """The host-clock epoch of the last Power Unit "Power off/down" or "Power cycle" record (asserted)
    the BMC that the cycle command ``argv`` drives logged within ``[since, until]`` (host epochs, with
    :data:`SEL_WINDOW_SLACK_SEC`), or None. Read-only and bounded: ``sel time get`` gives the BMC
    clock's offset to the host's (zone and skew), then ``sel list last N``. None when there is no
    query to derive, the BMC does not answer or no such record is in the window."""
    clock = bmc_query_argv(argv, ["sel", "time", "get"])
    tail = bmc_query_argv(argv, ["sel", "list", "last", str(SEL_TAIL_RECORDS)])
    if clock is None or tail is None:
        return None
    exe = _resolve_command(clock[0])
    if exe is None:
        return None
    rc, out = _run_query([exe, *clock[1:]], SEL_TIMEOUT_SEC)
    host_now = time.time()
    bmc_now = None
    for ln in reversed((out or "").splitlines()) if rc == 0 else ():
        bmc_now = _sel_naive_epoch(ln)
        if bmc_now is not None:
            break
    if bmc_now is None:
        return None
    offset = host_now - bmc_now
    rc, out = _run_query([exe, *tail[1:]], SEL_TIMEOUT_SEC)
    if rc != 0:
        return None
    found = None
    # " 491 | 10/22/2026 | 03:03:14 | Power Unit #0x02 | Power off/down | Asserted"
    for ln in (out or "").splitlines():
        f = [x.strip() for x in ln.split("|")]
        if len(f) < 5 or not f[3].lower().startswith("power unit") or f[4].lower() not in SEL_POWER_OFF_EVENTS:
            continue
        if len(f) > 5 and f[5].lower() != "asserted":
            continue
        at = _sel_naive_epoch(f"{f[1]} {f[2]}")
        if at is None:
            continue
        at += offset
        if since - SEL_WINDOW_SLACK_SEC <= at <= until + SEL_WINDOW_SLACK_SEC:
            found = at
    return found


def _resolve_command(cmd: str) -> Optional[str]:
    """``cmd`` as an absolute path, or None when it is not an executable on PATH. Tests replace it."""
    exe = shutil.which(cmd)
    return os.path.abspath(exe) if exe else None


def _run_step(argv: list, timeout: float = STEP_TIMEOUT_SEC) -> tuple:
    """``(rc, output tail)``; rc None when it could not start or timed out. Tests replace it."""
    try:
        r = subprocess.run(argv, timeout=timeout, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, repr(e)[:200]
    return r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()[-200:]


def _run_query(argv: list, timeout: float) -> tuple:
    """``(rc, whole stdout)`` of a read-only query whose every line counts (``_run_step`` keeps only a
    tail); rc None when it could not start or timed out. Tests replace it."""
    try:
        r = subprocess.run(argv, timeout=timeout, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, repr(e)[:200]
    return r.returncode, r.stdout or ""


def _write_request(argv: list, reason: str, timeout: float) -> None:
    tmp = REQUEST_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(request_text(argv, reason, time.time(), timeout))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REQUEST_FILE)


# Spec 04 I99: when the reboot was asked, kept across it so the next boot can record how long the
# shutdown took (the BMC's power-off minus this) beside the bound it had.
POWER_CYCLE_OS_FIRST_FILE = "power_cycle_os_first.json"


def _json_seconds(v: Optional[float]):
    """Seconds as JSON can hold them: infinity as the string "infinity"."""
    if v is None:
        return None
    return "infinity" if v == float("inf") else round(v, 1)


def _write_os_first_marker(requested: float, bound: Optional[int], unit_timeout: Optional[float], unit: str) -> None:
    """Best effort: a marker that cannot be written only loses the shutdown's duration."""
    path = health_dir() / POWER_CYCLE_OS_FIRST_FILE
    row = {
        "requested_epoch": requested,
        "bound_sec": bound,
        "unit_timeout_sec": _json_seconds(unit_timeout),
        "unit": unit,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        _logger.warning("power-cycle reboot-request marker not written, its shutdown time is lost: %s", e)


def take_os_first_marker() -> Optional[dict]:
    """The last OS-first reboot request's marker, removed as it is read; None when there is none or it
    does not parse."""
    path = health_dir() / POWER_CYCLE_OS_FIRST_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    try:
        row = json.loads(text)
    except ValueError:
        return None
    if not isinstance(row, dict) or not isinstance(row.get("requested_epoch"), (int, float)):
        return None
    return row


def _drop_request() -> None:
    for p in (REQUEST_FILE, REQUEST_FILE + ".tmp", fallback_pid_file()):
        try:
            os.unlink(p)
        except OSError:
            pass


def _stop_fallback(unit: str) -> bool:
    """Stop the fallback unit after a reboot that did not start. True once it is gone; False while
    it may still be alive (and fire the cycle at its bound), so the caller keeps the action pending."""
    svc = f"{unit}.service"
    rc, out = _run_step(["systemctl", "stop", svc])
    if rc != 0:
        _logger.warning("could not stop %s (rc %s): %s; killing it", svc, rc, out)
        _run_step(["systemctl", "kill", "--signal=SIGKILL", svc])
    rc, _ = _run_step(["systemctl", "is-active", "--quiet", svc])
    # is-active exits 0 while the unit runs and non-zero once it is inactive or gone; a check that
    # could not run proves nothing, so it reads as alive.
    return rc is not None and rc != 0


def fire_os_first(argv: list, reason: str, timeout: float = DEFAULT_POWER_CYCLE_TIMEOUT_SEC) -> str:
    """Power-cycle the host with its OS brought down cleanly first (spec 04 I88). Never cuts power
    under a running OS from here.

    Checks the BMC read-only (:func:`preflight_argv`), writes the request, starts the fallback unit
    (fires ``argv`` once :func:`shutdown_bound_sec` passes), then ``systemctl reboot``; the shutdown
    hook fires ``argv`` once the filesystems are read-only. Returns "os_first" once the reboot is
    queued. When any step cannot start it undoes what it started and raises PowerCycleNotLaunched:
    nothing fired, the caller holds and pages. The one exception: a fallback unit that outlives a
    failed reboot may still fire at its bound, so that returns "fallback_pending" and the action
    stays pending."""
    argv = [str(a) for a in argv]
    why = os_first_unavailable()
    if not why and (not argv or any("\n" in a for a in argv)):
        why = "the power-cycle argv cannot be passed to the shutdown hook"
    if not why:
        exe = _resolve_command(argv[0])
        if exe is None:
            why = f"power-cycle command {argv[0]!r} not found"
        else:
            # Absolute, so neither the hook's nor the fallback's PATH decides what runs.
            argv[0] = exe
    if not why:
        probe = preflight_argv(argv)
        if probe is not None:
            rc, out = _run_step(probe, PREFLIGHT_TIMEOUT_SEC)
            if rc != 0:
                why = f"BMC did not answer the read-only pre-flight {' '.join(probe[-2:])} (rc {rc}): {out}"
    unit_timeout: Optional[float] = None
    bound: Optional[int] = None
    if not why:
        unit_timeout = host_unit_timeout_sec()
        bound = shutdown_bound_sec(unit_timeout)
    unit = f"{FALLBACK_UNIT_PREFIX}-{int(time.time())}"
    started = False
    if not why:
        try:
            _write_request(argv, reason, timeout)
        except OSError as e:
            why = f"could not write {REQUEST_FILE}: {e}"
    if not why:
        trigger = _journal_trigger(reason, unit)
        rc, out = _run_step(fallback_argv(argv, bound, unit, reason=reason, timeout=timeout, trigger=trigger))
        if rc == 0:
            started = True
        else:
            why = f"fallback unit did not start (rc {rc}): {out}"
    if not why:
        rc, out = _run_step(list(REBOOT_ARGV))
        if rc == 0:
            requested = time.time()
            _write_os_first_marker(requested, bound, unit_timeout, unit)
            health_event(
                "power_cycle_os_first",
                phase="reboot_requested",
                reason=reason,
                bound_sec=bound,
                unit_timeout_sec=_json_seconds(unit_timeout),
                unit=unit,
            )
            _logger.warning(
                "POWER CYCLE (OS first): reboot requested; the shutdown hook cycles the host once the "
                "filesystems are read-only, the fallback unit %s at %ss if the shutdown hangs",
                unit,
                bound,
            )
            return "os_first"
        why = f"systemctl reboot failed (rc {rc}): {out}"
    if started and not _stop_fallback(unit):
        # It may still fire at its bound: the action stays pending (no device reset under it) rather
        # than reading as never launched.
        health_event("power_cycle_os_first", phase="fallback_pending", reason=reason, why=why[-300:], unit=unit)
        _logger.error(
            "POWER CYCLE: %s, and the fallback unit %s could not be stopped; it fires at %ss unless "
            "stopped — keeping the action pending",
            why,
            unit,
            bound,
        )
        return "fallback_pending"
    _drop_request()
    health_event("power_cycle_os_first", phase="not_launched", reason=reason, why=why[-300:])
    _logger.error("POWER CYCLE NOT LAUNCHED: %s; nothing fired, the device stays held", why)
    raise PowerCycleNotLaunched(why)


def pre_power_cycle_hook_timeout_sec() -> float:
    """The hook's timeout. A non-positive or malformed value reads as the default; a huge one is
    capped, so no setting makes the hook block recovery for good."""
    raw = os.environ.get("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK_TIMEOUT_SEC", "").strip()
    try:
        v = float(raw)
    except ValueError:
        return PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC
    if not 0 < v < float("inf"):
        return PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC
    return min(v, PRE_POWER_CYCLE_HOOK_TIMEOUT_MAX_SEC)


def run_pre_power_cycle_hook(log: Callable[[str], None], reason: str) -> dict:
    """Run ``TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK`` (spec 04 I26) and wait for it, bounded.

    The command is split like shell words and run without a shell, in its own session, with the
    cycle's reason in ``TT_DEVICE_MCP_POWER_CYCLE_REASON``. Its output goes to a temp file, not a
    pipe, so a background child it leaves holding stdout cannot stretch the wait past its own exit.
    On timeout its whole process group is killed. Every outcome — unset, unparseable, not
    startable, non-zero, timed out — is logged and returned, and the caller fires the cycle anyway:
    the hook is a courtesy to co-tenants, never a gate on recovery. Never raises.

    Returns {"ran": bool, "rc": int|None, "timed_out": bool, "seconds": float, "detail": str}."""
    out = {"ran": False, "rc": None, "timed_out": False, "seconds": 0.0, "detail": ""}
    raw = os.environ.get("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", "").strip()
    if not raw:
        return out
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        out["detail"] = f"does not parse ({e})"
        log(f"PRE-POWER-CYCLE HOOK skipped: TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK {out['detail']}")
        return out
    if not argv:
        return out
    timeout = pre_power_cycle_hook_timeout_sec()
    env = {**os.environ, "TT_DEVICE_MCP_POWER_CYCLE_REASON": reason}
    log(f"PRE-POWER-CYCLE HOOK: running {argv[0]!r} (up to {timeout:g}s) before the power cycle")
    t0 = time.monotonic()
    try:
        with tempfile.TemporaryFile() as buf:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=buf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                )
            except OSError as e:
                out["detail"] = f"could not start: {e}"
                log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} {out['detail']}; power-cycling anyway")
                return out
            out["ran"] = True
            try:
                out["rc"] = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                out["timed_out"] = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)  # reap it, so it never lingers as a zombie
                except subprocess.TimeoutExpired:
                    pass
            out["seconds"] = round(time.monotonic() - t0, 1)
            buf.seek(max(0, buf.seek(0, os.SEEK_END) - HOOK_OUTPUT_TAIL_BYTES))
            out["detail"] = buf.read().decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001 - a broken hook must never stop the cycle
        out["detail"] = f"failed: {e!r}"
        log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} {out['detail']}; power-cycling anyway")
        return out
    tail = f": {out['detail'][-500:]}" if out["detail"] else ""
    if out["timed_out"]:
        log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} killed after {timeout:g}s; power-cycling anyway{tail}")
    else:
        log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} exited {out['rc']} after {out['seconds']:g}s{tail}")
    return out
