# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cold BMC chassis power-cycle recovery rung — the final one on the ladder."""

from __future__ import annotations

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

from tt_device_mcp.health.evidence import health_event

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
# A clean reboot of a Galaxy host reached systemd-shutdown in 86 s (measured); past the bound the
# fallback cycles a shutdown that hangs. Configurable within [MIN, MAX], never off.
SHUTDOWN_BOUND_DEFAULT_SEC = 120
SHUTDOWN_BOUND_MIN_SEC = 30
SHUTDOWN_BOUND_MAX_SEC = 300
STEP_TIMEOUT_SEC = 30
# The read-only BMC check before anything is written: a BMC that does not answer it cannot cycle, so
# the host would only reboot warm (F2 of the I88 review).
PREFLIGHT_TIMEOUT_SEC = 10
# systemd-shutdown kills a hook still running after 90 s and reboots warm. The hook's worst case —
# the RAID wait, every try at its cap, the pauses between them and the wait for power to drop after a
# try that worked — stays inside it (hook_worst_case_sec).
SYSTEMD_SHUTDOWN_HOOK_TIMEOUT_SEC = 90
HOOK_MDADM_WAIT_SEC = 10
HOOK_TRIES = 3
HOOK_RETRY_PAUSE_SEC = 2
HOOK_POWER_DROP_WAIT_SEC = 25
HOOK_TRY_TIMEOUT_MIN_SEC = 5
HOOK_TRY_TIMEOUT_MAX_SEC = 15
# Runs in the fallback unit under bash, as FALLBACK_ARGV0: record its pid for the hook, wait out the
# bound inside bash (a sleep child would die in the killall and fire the cycle early), flush what it
# can without blocking on it, then cycle under the command's timeout. Args: pid file, bound, timeout,
# argv.
FALLBACK_SH = (
    'printf "%s\\n" "$$" >"$1" 2>/dev/null; b=$2; t=$3; shift 3\n'
    'read -rt "$b" <> <(:)\n'
    "sync & read -rt 2 <> <(:)\n"
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
        HOOK_MDADM_WAIT_SEC
        + HOOK_TRIES * HOOK_TRY_TIMEOUT_MAX_SEC
        + (HOOK_TRIES - 1) * HOOK_RETRY_PAUSE_SEC
        + HOOK_POWER_DROP_WAIT_SEC
    )


def shutdown_bound_sec() -> int:
    """``TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC``: how long the clean shutdown gets before the
    fallback cycles the host anyway. Malformed reads as the default; any value is clamped, so no
    setting turns the bound off or stretches it past SHUTDOWN_BOUND_MAX_SEC."""
    try:
        v = int(float(os.environ.get("TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC", "").strip()))
    except (ValueError, OverflowError):
        return SHUTDOWN_BOUND_DEFAULT_SEC
    return max(SHUTDOWN_BOUND_MIN_SEC, min(v, SHUTDOWN_BOUND_MAX_SEC))


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


def fallback_pid_file() -> str:
    """Where the fallback records its pid for the hook: beside the request, so both share /run."""
    base = REQUEST_FILE[: -len(".request")] if REQUEST_FILE.endswith(".request") else REQUEST_FILE
    return base + ".fallback.pid"


def request_text(argv: list, reason: str, now: float, timeout: float = DEFAULT_POWER_CYCLE_TIMEOUT_SEC) -> str:
    """The request the shutdown hook reads: the epoch, the reason on one line, the per-try timeout,
    then one argv word per line. The hook ignores a request with no argv or one older than an hour."""
    return "\n".join([str(int(now)), _one_line(reason), str(hook_try_timeout_sec(timeout)), *argv]) + "\n"


def fallback_argv(argv: list, bound: int, unit: str, *, reason: str = "", timeout: float = STEP_TIMEOUT_SEC) -> list:
    """The transient unit that cycles a hung shutdown. DefaultDependencies=no keeps it out of the
    shutdown transaction, and its '@' argv[0] keeps it alive through systemd-shutdown's killall, so it
    bounds every stage up to the hook, which stops it by its recorded pid and fires instead. The cycle
    command gets the reason in ``TT_DEVICE_MCP_POWER_CYCLE_REASON`` and runs under ``timeout``."""
    return [
        "systemd-run",
        f"--unit={unit}",
        "--collect",
        "--property=DefaultDependencies=no",
        f"--setenv=TT_DEVICE_MCP_POWER_CYCLE_REASON={_one_line(reason)}",
        "--description=tt-device-mcp: power-cycle a hung shutdown (spec 04 I88)",
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
    probe = bmc_query_argv(argv, ["raw", "0x00", "0x07"])
    if probe is None:
        return None
    exe = _resolve_command(probe[0])
    if exe is None:
        return None
    rc, out = _run_step([exe, *probe[1:]], PREFLIGHT_TIMEOUT_SEC)
    if rc != 0:
        return None
    # The response bytes are the last all-hex line (stdout follows any stderr warning).
    lines = [ln.split() for ln in (out or "").splitlines() if _HEX_BYTES.match(ln)]
    if not lines:
        return None
    code = int(lines[-1][0], 16) & 0x0F
    return RESTART_CAUSES.get(code, f"unmapped code 0x{code:02x}")


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


def _write_request(argv: list, reason: str, timeout: float) -> None:
    tmp = REQUEST_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(request_text(argv, reason, time.time(), timeout))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REQUEST_FILE)


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
    bound = shutdown_bound_sec()
    unit = f"{FALLBACK_UNIT_PREFIX}-{int(time.time())}"
    started = False
    if not why:
        try:
            _write_request(argv, reason, timeout)
        except OSError as e:
            why = f"could not write {REQUEST_FILE}: {e}"
    if not why:
        rc, out = _run_step(fallback_argv(argv, bound, unit, reason=reason, timeout=timeout))
        if rc == 0:
            started = True
        else:
            why = f"fallback unit did not start (rc {rc}): {out}"
    if not why:
        rc, out = _run_step(list(REBOOT_ARGV))
        if rc == 0:
            health_event("power_cycle_os_first", phase="reboot_requested", reason=reason, bound_sec=bound, unit=unit)
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
