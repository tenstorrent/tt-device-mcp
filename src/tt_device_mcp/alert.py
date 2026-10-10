# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Opt-in operator alert hook (spec 03 I30).

``TT_DEVICE_MCP_ALERT_CMD`` names a command (split like a shell word list, run without a shell) that
the broker runs with one event as JSON on stdin. Unset or blank, nothing runs. The command runs on
its own daemon thread under a timeout, so a slow or hung hook never blocks the broker's loop; on
timeout its whole process group is killed. At most one runs at a time: a new event while one is
still in flight is dropped and logged, never queued behind it.
"""

import json
import logging
import os
import shlex
import signal
import subprocess
import threading
from typing import Optional

ALERT_TIMEOUT_DEFAULT_SEC = 30.0
# Ceiling on the gap between repeat alerts for one stuck hold: still stuck a day later is worth
# one more page, more often than that is noise.
ALERT_BACKOFF_CAP_SEC = 24 * 3600

_in_flight = threading.Lock()


def alert_argv() -> Optional[list[str]]:
    """The hook's argv, or None when it is unset, blank, or does not parse."""
    raw = os.environ.get("TT_DEVICE_MCP_ALERT_CMD", "").strip()
    if not raw:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError:
        return None
    return argv or None


def alert_timeout_sec() -> float:
    """Per-run timeout. A non-positive or malformed value reads as the default, never 0."""
    raw = os.environ.get("TT_DEVICE_MCP_ALERT_TIMEOUT_SEC", "").strip()
    try:
        v = float(raw)
    except ValueError:
        return ALERT_TIMEOUT_DEFAULT_SEC
    return v if 0 < v < float("inf") else ALERT_TIMEOUT_DEFAULT_SEC


def next_gap_windows(gap: int, window_sec: float) -> int:
    """The gap, in deadline windows, before the next repeat alert: double the last one (2 to
    start), capped at ALERT_BACKOFF_CAP_SEC and never below one window."""
    cap = max(1, int(ALERT_BACKOFF_CAP_SEC // window_sec)) if window_sec > 0 else 1
    return max(1, min(2 * gap if gap > 0 else 2, cap))


def _run(argv: list[str], payload: bytes, timeout: float, log: logging.Logger) -> None:
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            log.warning(f"ALERT-HOOK could not start {argv[0]!r}: {e}")
            return
        try:
            out, _ = proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.communicate(timeout=5)  # reap it; a child that left the group may hold the pipe
            except subprocess.TimeoutExpired:
                pass
            log.warning(f"ALERT-HOOK {argv[0]!r} killed after {timeout:g}s")
            return
        if proc.returncode != 0:
            tail = (out or b"").decode("utf-8", "replace").strip()[-500:]
            log.warning(f"ALERT-HOOK {argv[0]!r} exited {proc.returncode}: {tail}")
    except Exception as e:  # a thread's exception would only reach stderr; keep it in the log
        log.warning(f"ALERT-HOOK {argv[0]!r} failed: {e}")
    finally:
        _in_flight.release()


def send_alert(event: dict, log: Optional[logging.Logger] = None) -> Optional[threading.Thread]:
    """Run the hook on ``event`` in the background. Returns the thread, or None when the hook is
    unset or a previous run is still in flight. Never blocks and never raises."""
    argv = alert_argv()
    if argv is None:
        return None
    log = log or logging.getLogger("tt-device-mcp")
    if not _in_flight.acquire(blocking=False):
        log.warning(f"ALERT-HOOK previous run still in flight, dropped {event.get('kind', '?')!r}")
        return None
    try:
        payload = (json.dumps(event, default=str) + "\n").encode()
        t = threading.Thread(
            target=_run, args=(argv, payload, alert_timeout_sec(), log), name="alert-hook", daemon=True
        )
        t.start()
    except Exception as e:
        _in_flight.release()
        log.warning(f"ALERT-HOOK could not start: {e}")
        return None
    return t
