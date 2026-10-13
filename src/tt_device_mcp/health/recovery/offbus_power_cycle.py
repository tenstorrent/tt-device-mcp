# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bounded off-bus hold (spec 04 I27).

With the host reset gate refusing every reset because a chip is off the bus (I24), the ladder below
the power cycle cannot run, so before this the hold stood until a human power-cycled the box. Here
the hold is bounded: once the episode has stood ``TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC`` (default
300 s) and the ladder's safe rungs have run once, the broker runs the host's own power-cycle command
``TT_DEVICE_MCP_POWER_CYCLE_CMD`` — but only on a tenant-free box, with no operator recovery in
progress and no stand-back marker or command active, and at most once per
``TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC`` (default 6 h). Past that, or with the command
unset, it pages instead and the hold stands as before.

Every phase goes to the fsync'd health journal, and each attempt is written to a small state file in
the health dir before the command runs, so a broker killed mid-cycle (the cycle itself) still counts
the attempt against the rate limit.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from tt_device_mcp import alert
from tt_device_mcp.health.evidence import health_dir, health_event

STATE_FILE = "offbus_power_cycle.json"
GRACE_DEFAULT_SEC = 300
MIN_INTERVAL_DEFAULT_SEC = 6 * 3600
CHECK_TIMEOUT_DEFAULT_SEC = 10.0
FIRE_TIMEOUT_DEFAULT_SEC = 120.0
# Attempts kept in the state file: enough to read the rate limit, never a growing log.
MAX_ATTEMPTS_KEPT = 8

# In-memory, per broker process: the episode whose off-bus hold the gate is refusing, when its grace
# ends, and whether the ladder's safe rungs have run in it. A restart starts empty: the next idle
# pass re-arms it and the ladder runs its safe rungs once more, both harmless.
_pending: dict = {}


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return v if v > 0 else default


def grace_sec() -> int:
    """Seconds an off-bus hold the gate refuses to reset stands before the power cycle may fire,
    counted from the start of the hold episode. A non-positive or malformed value reads as 300."""
    return _int_env("TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC", GRACE_DEFAULT_SEC)


def min_interval_sec() -> int:
    """At most one attempt per this many seconds per box (default 6 h); past it, page instead."""
    return _int_env("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", MIN_INTERVAL_DEFAULT_SEC)


def _argv(name: str) -> Optional[list]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return shlex.split(raw) or None
    except ValueError:
        return None


def power_cycle_argv() -> Optional[list]:
    """``TT_DEVICE_MCP_POWER_CYCLE_CMD`` split like shell words, or None when unset or unparseable."""
    return _argv("TT_DEVICE_MCP_POWER_CYCLE_CMD")


def _run(argv: list, timeout: float, env: Optional[dict] = None) -> tuple:
    """``(rc, output tail)``; rc None when it could not start or timed out (its group is killed)."""
    try:
        p = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    except OSError as exc:
        return None, f"could not start: {exc}"
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return None, f"timed out after {timeout:.0f}s"
    return p.returncode, out.decode(errors="replace").strip()[-300:]


def standback_reason(timeout: Optional[float] = None) -> str:
    """Why the broker must stand back from the power cycle now, or "" when nothing objects.

    * ``TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE``: paths separated by ``os.pathsep``; any that
      exists is a stand-back marker.
    * ``TT_DEVICE_MCP_MANUAL_RECOVERY_CMD``: exit 0 means an operator is recovering the host.
    * ``TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD``: exit 0 means stand back (e.g. a workload is
      starting that the device-holder scan cannot see yet).

    Exit 1 clears a check. Any other exit, a timeout or a command that cannot start stands back:
    a check that cannot answer is not a yes."""
    for path in filter(None, os.environ.get("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE", "").split(os.pathsep)):
        if Path(path).exists():
            return f"stand-back marker {path} is present"
    for name, what in (
        ("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", "an operator recovery is in progress"),
        ("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD", "the stand-back check says stand back"),
    ):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        argv = _argv(name)
        if argv is None:
            return f"{name} does not parse"
        rc, out = _run(argv, CHECK_TIMEOUT_DEFAULT_SEC if timeout is None else timeout)
        if rc == 1:
            continue
        if rc == 0:
            return f"{what} ({name}: {out[-120:] or 'exit 0'})"
        return f"{name} could not answer ({'exit ' + str(rc) if rc is not None else out}) — standing back"
    return ""


def _state_path() -> Path:
    return health_dir() / STATE_FILE


def load_state() -> dict:
    try:
        rec = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def save_state(state: dict) -> None:
    """Atomic and fsync'd: an attempt written here survives the power cycle it precedes."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def last_attempt_within(state: dict, now: float, interval: int) -> Optional[float]:
    """Epoch of an attempt younger than ``interval``, or None. An unreadable stamp counts as now."""
    for rec in state.get("attempts") or []:
        try:
            at = float(rec.get("at_epoch"))
        except (AttributeError, TypeError, ValueError):
            return now
        if now - at < interval:
            return at
    return None


def record_attempt(state: dict, now: float, episode: str, reason: str) -> None:
    attempts = list(state.get("attempts") or [])
    attempts.insert(0, {"at_epoch": now, "episode": episode, "reason": reason})
    state["attempts"] = attempts[:MAX_ATTEMPTS_KEPT]
    save_state(state)


def episode_age_sec(episode: str, now: Optional[datetime] = None) -> Optional[float]:
    try:
        return ((now or datetime.now()) - datetime.fromisoformat(episode)).total_seconds()
    except (TypeError, ValueError):
        return None


def arm(episode: str) -> None:
    """The gate refused to reset this episode's off-bus hold: start tracking it (idempotent)."""
    if _pending.get("episode") != episode:
        _pending.clear()
        _pending.update(episode=episode, ladder_ran=False, paged=set(), spawned=False)
        health_event("offbus_gated_hold", phase="grace", held_since=episode, grace_sec=grace_sec())


def note_ladder_ran(episode: str) -> None:
    if _pending.get("episode") == episode:
        _pending["ladder_ran"] = True


def ladder_ran(episode: str) -> bool:
    return _pending.get("episode") == episode and bool(_pending.get("ladder_ran"))


def due_for_watchdog(episode: str, now: Optional[datetime] = None) -> bool:
    """True once per episode, when an armed gated hold's grace has run out and the command is set:
    the hold watchdog then forces one escalation pass so the cycle fires at the grace, not at the
    next ceiling window."""
    if not episode or _pending.get("episode") != episode or _pending.get("spawned"):
        return False
    if not _pending.get("ladder_ran") or power_cycle_argv() is None:
        return False
    age = episode_age_sec(episode, now)
    return age is not None and age >= grace_sec()


def mark_spawned(episode: str) -> None:
    if _pending.get("episode") == episode:
        _pending["spawned"] = True


def page_once(kind: str, log, message: str, **fields) -> None:
    """Journal, log and alert ``kind`` once per episode (the journal line is the kill-safe record)."""
    paged = _pending.setdefault("paged", set())
    if kind in paged:
        return
    paged.add(kind)
    log(message)
    event = {"kind": kind, "message": message, "host_at_risk": True, **fields}
    health_event(kind, message=message, host_at_risk=True, **fields)
    alert.send_alert(event)


def fire_command(argv: list, reason: str, timeout: float = FIRE_TIMEOUT_DEFAULT_SEC) -> None:
    """Run the host's power-cycle command with the reason in ``TT_DEVICE_MCP_POWER_CYCLE_REASON``.
    Raises on a non-zero exit, a timeout or a command that cannot start, so the caller's ledger
    retracts an action that never launched. Tests replace it: no test may cycle the host."""
    env = {**os.environ, "TT_DEVICE_MCP_POWER_CYCLE_REASON": reason}
    rc, out = _run(argv, _int_env("TT_DEVICE_MCP_POWER_CYCLE_CMD_TIMEOUT_SEC", int(timeout)), env)
    if rc != 0:
        raise RuntimeError(f"power-cycle command {'exited ' + str(rc) if rc is not None else out}: {out[-200:]}")


def fire_and_record(argv: list, reason: str, state: dict) -> None:
    """``fire_command``, keeping a failure's cause in the state file for the rate-limited page."""
    try:
        fire_command(argv, reason)
    except Exception as exc:
        state["last_error"] = repr(exc)[-300:]
        try:
            save_state(state)
        except OSError:
            pass
        raise


def note_standback(episode: str, why: str) -> None:
    """Journal a stand-back once per change of reason, not once per pass."""
    if _pending.get("episode") == episode and _pending.get("standback") != why:
        _pending["standback"] = why
        health_event("offbus_gated_hold", phase="standing_back", held_since=episode, why=why)


def now_epoch() -> float:
    return time.time()
