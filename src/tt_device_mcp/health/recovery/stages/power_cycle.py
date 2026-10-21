# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cold BMC chassis power-cycle recovery rung — the final one on the ladder."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Callable

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


def _fire_direct_power_cycle() -> None:
    """Shell out to the BMC chassis power cycle with the OS still up — the I88 fallback. Isolated so
    every test replaces it and no test path can ever power-cycle the box running the suite. Raises on
    a non-zero exit / missing binary so a fire that did NOT take the box down is detectable."""
    r = subprocess.run(list(DEFAULT_POWER_CYCLE_ARGV), timeout=30, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ipmitool power cycle exited {r.returncode}: {r.stderr.strip()[:200]}")


def _fire_power_cycle() -> None:
    """The BMC chassis power cycle — the one action that recovers a whole-bus wedge a warm reboot
    leaves wedged (measured on box D) — with the host OS brought down cleanly first (spec 04 I88).
    Every test replaces it. Raises when neither the clean path nor the direct fire launched."""
    fire_os_first(list(DEFAULT_POWER_CYCLE_ARGV), "auto BMC power cycle", _fire_direct_power_cycle)


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
# A clean reboot of a Galaxy host reached systemd-shutdown in 86 s (measured); past the bound the
# fallback cycles a shutdown that hangs. Configurable within [MIN, MAX], never off.
SHUTDOWN_BOUND_DEFAULT_SEC = 120
SHUTDOWN_BOUND_MIN_SEC = 30
SHUTDOWN_BOUND_MAX_SEC = 300
STEP_TIMEOUT_SEC = 30
# Runs in the fallback unit: wait out the bound, flush what it can (never blocking on it), then cycle.
FALLBACK_SH = 'sleep "$1"; shift; sync & sleep 2; exec "$@"'
REBOOT_ARGV = ("systemctl", "--no-block", "-i", "reboot")


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
    for tool in ("systemctl", "systemd-run"):
        if shutil.which(tool) is None:
            return f"{tool} not found"
    return ""


def request_text(argv: list, reason: str, now: float) -> str:
    """The request the shutdown hook reads: the epoch, the reason on one line, then one argv word per
    line. The hook ignores a request with no argv or one older than an hour."""
    return "\n".join([str(int(now)), " ".join(reason.split())[:300], *argv]) + "\n"


def fallback_argv(argv: list, bound: int, unit: str) -> list:
    """The transient unit that cycles a hung shutdown. DefaultDependencies=no keeps it out of the
    shutdown transaction, so it lives until systemd hands over to systemd-shutdown, by which point
    the hook fires instead."""
    return [
        "systemd-run",
        f"--unit={unit}",
        "--collect",
        "--property=DefaultDependencies=no",
        "--description=tt-device-mcp: power-cycle a hung shutdown (spec 04 I88)",
        "/bin/sh",
        "-c",
        FALLBACK_SH,
        FALLBACK_UNIT_PREFIX,
        str(int(bound)),
        *argv,
    ]


def _run_step(argv: list, timeout: float = STEP_TIMEOUT_SEC) -> tuple:
    """``(rc, output tail)``; rc None when it could not start or timed out. Tests replace it."""
    try:
        r = subprocess.run(argv, timeout=timeout, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, repr(e)[:200]
    return r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()[-200:]


def _write_request(argv: list, reason: str) -> None:
    tmp = REQUEST_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(request_text(argv, reason, time.time()))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REQUEST_FILE)


def _drop_request() -> None:
    for p in (REQUEST_FILE, REQUEST_FILE + ".tmp"):
        try:
            os.unlink(p)
        except OSError:
            pass


def fire_os_first(argv: list, reason: str, fire_direct: Callable[[], None]) -> str:
    """Power-cycle the host with its OS brought down cleanly first (spec 04 I88).

    Writes the request, starts the fallback unit (fires ``argv`` once :func:`shutdown_bound_sec`
    passes), then ``systemctl reboot``; the shutdown hook fires ``argv`` once the filesystems are
    read-only. Returns "os_first" once the reboot is queued. When the clean path cannot start (no
    systemd, no hook, a step that fails), it undoes what it started and calls ``fire_direct`` — the
    live cycle — and returns "direct"; ``fire_direct`` raising propagates, so a cycle that launched
    neither way still reads as not launched."""
    argv = [str(a) for a in argv]
    why = os_first_unavailable()
    if not why and (not argv or any("\n" in a for a in argv)):
        why = "the power-cycle argv cannot be passed to the shutdown hook"
    bound = shutdown_bound_sec()
    unit = f"{FALLBACK_UNIT_PREFIX}-{int(time.time())}"
    started = False
    if not why:
        try:
            _write_request(argv, reason)
        except OSError as e:
            why = f"could not write {REQUEST_FILE}: {e}"
    if not why:
        rc, out = _run_step(fallback_argv(argv, bound, unit))
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
    if started:
        rc, out = _run_step(["systemctl", "stop", f"{unit}.service"])
        if rc != 0:
            _logger.warning("could not stop %s (rc %s): %s", unit, rc, out)
    _drop_request()
    health_event("power_cycle_os_first", phase="unavailable", reason=reason, why=why[-300:])
    _logger.warning("POWER CYCLE: clean OS shutdown first is unavailable (%s); power-cycling directly", why)
    fire_direct()
    return "direct"


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
