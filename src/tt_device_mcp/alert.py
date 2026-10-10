# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Opt-in operator alert hook (spec 03 I30).

``TT_DEVICE_MCP_ALERT_CMD`` names a command (split like a shell word list, run without a shell) that
the broker runs with one event as JSON on stdin. Unset or blank, nothing runs. The command runs on
its own daemon thread under a timeout, so a slow or hung hook never blocks the broker's loop; on
timeout its whole process group is killed. At most one runs at a time: a new event while one is
still in flight is dropped and logged, never queued behind it. Its output is read in chunks and only
the last few KB are kept, so a chatty or runaway hook cannot grow the broker's memory.
"""

import json
import logging
import os
import selectors
import shlex
import signal
import subprocess
import threading
import time
from typing import Optional

ALERT_TIMEOUT_DEFAULT_SEC = 30.0
# Ceiling on the gap between repeat alerts for one stuck hold: still stuck a day later is worth
# one more page, more often than that is noise.
ALERT_BACKOFF_CAP_SEC = 24 * 3600
# The hook's output is only kept for the failure log line: its last few KB, never all of it. Past
# the read cap the pipe is left unread, so a runaway writer blocks until its timeout kills it
# instead of spinning the broker's reader thread.
OUTPUT_TAIL_BYTES = 4096
OUTPUT_READ_CAP_BYTES = 1024 * 1024
# Once the hook itself has exited, how long to keep draining a pipe that a child it left behind
# still holds open. Its exit, not that child, ends the run and frees the slot.
EXIT_DRAIN_SEC = 0.5

_in_flight = threading.Lock()
_warned_unparseable = ""


def alert_argv() -> Optional[list[str]]:
    """The hook's argv, or None when it is unset, blank, or does not parse (logged once per value)."""
    global _warned_unparseable
    raw = os.environ.get("TT_DEVICE_MCP_ALERT_CMD", "").strip()
    if not raw:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        if raw != _warned_unparseable:  # once per value, not once per deadline window
            _warned_unparseable = raw
            logging.getLogger("tt-device-mcp").warning(
                f"ALERT-HOOK TT_DEVICE_MCP_ALERT_CMD does not parse ({e}); the alert hook is off"
            )
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


def _read_tail(proc: subprocess.Popen, deadline: float) -> tuple[bytes, bool, bool]:
    """Read the hook's output until EOF, the deadline, or a short drain after the hook exits.

    Returns (last OUTPUT_TAIL_BYTES, hit EOF, past the read cap). Memory stays bounded by the tail
    whatever the hook writes."""
    tail = bytearray()
    total = 0
    eof = False
    fd = proc.stdout.fileno()
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while not eof:
            now = time.monotonic()
            if proc.poll() is not None:
                deadline = min(deadline, now + EXIT_DRAIN_SEC)
            if now >= deadline:
                break
            if total >= OUTPUT_READ_CAP_BYTES:
                time.sleep(min(0.2, deadline - now))  # leave the pipe full: the writer blocks
                continue
            if not sel.select(min(0.2, deadline - now)):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                eof = True
            total += len(chunk)
            tail += chunk
            del tail[:-OUTPUT_TAIL_BYTES]
    return bytes(tail), eof, total >= OUTPUT_READ_CAP_BYTES


def _run(argv: list[str], payload: bytes, timeout: float, log: logging.Logger) -> None:
    proc = None
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
        deadline = time.monotonic() + timeout
        try:
            proc.stdin.write(payload)  # one small event: fits the pipe buffer, never blocks
            proc.stdin.close()
        except OSError:
            pass  # the hook does not read stdin, or already exited: its exit code says the rest
        out, eof, capped = _read_tail(proc, deadline)
        try:
            rc = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=5)  # reap it, so it never lingers as a zombie
            except subprocess.TimeoutExpired:
                pass
            note = " (output past 1 MiB left unread)" if capped else ""
            log.warning(f"ALERT-HOOK {argv[0]!r} killed after {timeout:g}s{note}")
            return
        if rc != 0:
            tail = out.decode("utf-8", "replace").strip()[-500:]
            log.warning(f"ALERT-HOOK {argv[0]!r} exited {rc}: {tail}")
        if not eof:
            log.warning(
                f"ALERT-HOOK {argv[0]!r} exited {rc} but a process it left behind still holds its "
                f"output; stopped reading"
            )
    except Exception as e:  # a thread's exception would only reach stderr; keep it in the log
        log.warning(f"ALERT-HOOK {argv[0]!r} failed: {e}")
    finally:
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
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
