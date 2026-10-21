# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bounded off-bus hold (spec 04 I27).

With the host reset gate refusing every reset because a chip is off the bus (I24), the ladder below
the power cycle cannot run, so before this the hold stood until a human power-cycled the box. Here
the hold is bounded: once the episode has stood ``TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC`` (default
300 s) and the ladder's safe rungs have run once, the broker runs the host's own power-cycle command
``TT_DEVICE_MCP_POWER_CYCLE_CMD`` — but only on a tenant-free box, with no operator recovery in
progress and no stand-back marker or command active, and spaced by the power cycle's cooldown (spec 04
I65). ``TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC`` (unset by default) adds a spacing between
attempts on top of it, never above the I65 backoff cap; either wait is stated on /health and the cycle
is asked again the moment it ends. With the command unset it pages instead and the hold stands as
before; a cycle blocked ``TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_BLOCKED_PAGE_SEC`` (default 1800 s) past
the grace pages too.

A below-floor drop that takes a whole UBB tray off the bus goes to the command at once, with no grace
(:func:`note_whole_tray_down`): the below-floor mesh reset inverts it, and no single reset has brought
such a tray back.

Every phase goes to the fsync'd health journal, and each attempt is written to a small state file in
the health dir before the command runs, so a broker killed mid-cycle (the cycle itself) still counts
the attempt against the rate limit.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import shlex
import signal
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from tt_device_mcp import alert, metrics
from tt_device_mcp.health.evidence import health_dir, health_event

STATE_FILE = "offbus_power_cycle.json"
GRACE_DEFAULT_SEC = 300
MIN_INTERVAL_DEFAULT_SEC = 0  # no spacing beyond the I65 cooldown unless an operator sets one
CHECK_TIMEOUT_DEFAULT_SEC = 10.0
FIRE_TIMEOUT_DEFAULT_SEC = 120.0
BLOCKED_PAGE_DEFAULT_SEC = 1800
HOLD_HANDOFF_DEFAULT_SEC = 1200  # two 600 s ceiling windows
# Where the installer stages deploy/tt-manual-recovery-check.sh; the manual-recovery check when
# TT_DEVICE_MCP_MANUAL_RECOVERY_CMD is unset.
MANUAL_RECOVERY_CHECK = "manual-recovery-check.sh"
# Attempts kept in the state file: enough to read the rate limit, never a growing log.
MAX_ATTEMPTS_KEPT = 8

# In-memory, per broker process: the episode whose off-bus hold the gate is refusing (or that holds a
# whole tray down), and whether the ladder's safe rungs have run in it. A
# restart starts empty: the next idle pass re-arms it and the ladder runs its safe rungs once more,
# both harmless.
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
    """Seconds an attempt holds the next one off, on top of the I65 cooldown; 0 (the default) adds
    none. Never above the I65 backoff cap: a wait with no stated, bounded end is an open-ended hold."""
    from tt_device_mcp.health.recovery.base import POWER_CYCLE_BACKOFF_CAP_SEC

    return min(
        _int_env("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", MIN_INTERVAL_DEFAULT_SEC),
        POWER_CYCLE_BACKOFF_CAP_SEC,
    )


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


def manual_recovery_argv() -> Optional[list]:
    """The manual-recovery check: ``TT_DEVICE_MCP_MANUAL_RECOVERY_CMD``, else the installed
    template under ``TTDEV_ROOT`` (default /opt/tt-device-broker), else None — no check at all,
    which never reads as "no operator" (the power cycle stays held)."""
    if os.environ.get("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", "").strip():
        return _argv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD")
    installed = Path(os.environ.get("TTDEV_ROOT", "/opt/tt-device-broker")) / MANUAL_RECOVERY_CHECK
    return [str(installed)] if installed.is_file() else None


def standback_reason(timeout: Optional[float] = None, *, require_manual_check: bool = True) -> str:
    """Why the broker must stand back from the power cycle now, or "" when nothing objects.

    * ``TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE``: paths separated by ``os.pathsep``; any that
      exists is a stand-back marker.
    * the manual-recovery check (:func:`manual_recovery_argv`): exit 0 means an operator is
      recovering the host. No check configured or installed stands back.
    * ``TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD``: exit 0 means stand back (e.g. a workload is
      starting that the device-holder scan cannot see yet).

    Exit 1 clears a check. Any other exit, a timeout, a command that cannot start or a check that
    raises stands back: a check that cannot answer is not a yes. ``require_manual_check=False`` (the
    I33 lifted mesh reset, not a power cycle) does not stand back for a check that is not configured."""
    try:
        return _standback_reason(CHECK_TIMEOUT_DEFAULT_SEC if timeout is None else timeout, require_manual_check)
    except Exception as exc:
        return f"the stand-back checks could not answer ({exc!r}) — standing back"


def _standback_reason(timeout: float, require_manual_check: bool = True) -> str:
    for path in filter(None, os.environ.get("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE", "").split(os.pathsep)):
        if Path(path).exists():
            return f"stand-back marker {path} is present"
    name = "TT_DEVICE_MCP_MANUAL_RECOVERY_CMD"
    argv = manual_recovery_argv()
    if argv is None:
        if require_manual_check:
            return f"no manual-recovery check ({name} unset or unparseable, none installed) — standing back"
    else:
        why = _check(name, argv, "an operator recovery is in progress", timeout)
        if why:
            return why
    name = "TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD"
    if not os.environ.get(name, "").strip():
        return ""
    argv = _argv(name)
    if argv is None:
        return f"{name} does not parse"
    return _check(name, argv, "the stand-back check says stand back", timeout)


def _check(name: str, argv: list, what: str, timeout: float) -> str:
    rc, out = _run(argv, timeout)
    if rc == 1:
        return ""
    if rc == 0:
        return f"{what} ({name}: {out[-120:] or 'exit 0'})"
    return f"{name} could not answer ({'exit ' + str(rc) if rc is not None else out}) — standing back"


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


def last_attempt_within(state: dict, now: float, interval: int, healed=None) -> Optional[float]:
    """Epoch of an attempt younger than ``interval``, or None. An unreadable stamp counts as now. An
    attempt ``healed(rec)`` says a later boot verified healthy is skipped: the hold after it is a new
    episode, not a loop (spec 04 I9); the ledger's backstop still caps every cycle."""
    for rec in state.get("attempts") or []:
        try:
            at = float(rec.get("at_epoch"))
        except (AttributeError, TypeError, ValueError):
            return now
        if now - at < interval and not (healed and healed(rec)):
            return at
    return None


def record_attempt(state: dict, now: float, episode: str, reason: str, boot_id: str = "") -> None:
    attempts = list(state.get("attempts") or [])
    attempts.insert(0, {"at_epoch": now, "episode": episode, "reason": reason, "boot_id": boot_id})
    state["attempts"] = attempts[:MAX_ATTEMPTS_KEPT]
    save_state(state)


def episode_age_sec(episode: str, now: Optional[datetime] = None) -> Optional[float]:
    try:
        return ((now or datetime.now()) - datetime.fromisoformat(episode)).total_seconds()
    except (TypeError, ValueError):
        return None


def _track(episode: str) -> None:
    if _pending.get("episode") != episode:
        if _pending.get("handoff"):
            metrics.offbus_operator_deadline(0)
        _pending.clear()
        _pending.update(episode=episode, ladder_ran=False, paged=set(), journaled=set(), spawned=False)


def arm(episode: str) -> None:
    """The gate refused to reset this episode's off-bus hold: start tracking it (idempotent)."""
    _track(episode)
    if not _pending.get("gated"):
        _pending["gated"] = True
        health_event("offbus_gated_hold", phase="grace", held_since=episode, grace_sec=grace_sec())


def note_whole_tray_down(episode: str, detail: str, now: Optional[datetime] = None) -> None:
    """A whole UBB tray is off the bus in this episode's below-floor drop: no tray re-power and no mesh
    reset is tried for it, so the ladder counts as run and the command fires with no grace (idempotent)."""
    if not episode:
        return
    _track(episode)
    _pending["ladder_ran"] = True
    if _pending.get("whole_tray_at") is None:
        _pending["whole_tray_at"] = now or datetime.now()
        _pending["whole_tray_detail"] = detail[-200:]
        health_event(
            "offbus_gated_hold",
            phase="whole_tray_down",
            held_since=episode,
            detail=detail[-200:],
            fault_class="whole_tray",
            hold_age_sec=episode_age_sec(episode, now),
        )


def clear_whole_tray_down(episode: str, detail: str, fault_class: str = "") -> None:
    """The tray that made this episode a whole-tray drop is back on the bus in part: the no-grace power
    cycle no longer applies (spec 04 I27). The hold keeps its other hand-off clocks; idempotent. Marks the
    episode's one deferral (:func:`whole_tray_deferred`), which this call never undoes."""
    if not episode or _pending.get("episode") != episode or _pending.get("whole_tray_at") is None:
        return
    _pending["whole_tray_at"] = None
    _pending["whole_tray_detail"] = ""
    _pending["whole_tray_deferred"] = True
    health_event(
        "offbus_gated_hold",
        phase="whole_tray_back",
        held_since=episode,
        detail=detail[-200:],
        fault_class=fault_class,
        hold_age_sec=episode_age_sec(episode),
    )


def whole_tray_deferred(episode: str) -> bool:
    """True once this episode's no-grace cycle was held back because the tray came back in part
    (:func:`clear_whole_tray_down`). That deferral happens once per episode: a whole tray seen off again
    in the same episode fires without the re-check, so a tray whose read flaps cannot hold it forever."""
    return bool(episode) and _pending.get("episode") == episode and bool(_pending.get("whole_tray_deferred"))


def whole_tray_down_age_sec(episode: str, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds since this episode's drop was found to hold a whole tray, or None when it has not."""
    if not episode or _pending.get("episode") != episode or _pending.get("whole_tray_at") is None:
        return None
    return ((now or datetime.now()) - _pending["whole_tray_at"]).total_seconds()


def whole_tray_detail(episode: str) -> str:
    return _pending.get("whole_tray_detail", "") if _pending.get("episode") == episode else ""


def note_ladder_ran(episode: str) -> None:
    """The safe rungs ran for this episode. Tracks it even when nothing armed it yet (tracking alone
    forces no pass)."""
    if not episode:
        return
    _track(episode)
    _pending["ladder_ran"] = True
    if _PARTIAL_PASS.get() == episode:
        _pending["partial_reset"] = True  # the lifted pass reached its rungs: spent, whatever its outcome


def ladder_ran(episode: str) -> bool:
    return _pending.get("episode") == episode and bool(_pending.get("ladder_ran"))


def partial_reset_due(episode: str, now: Optional[datetime] = None) -> bool:
    """True once per episode when, with the power cycle configured, a gated hold has run its ladder and
    outlived its grace and has not had its one lifted pass yet: a partial off-bus drop then gets the
    ladder's own rungs (the mesh reset included) with the gate lifted before the power cycle (spec 04
    I27). A whole tray down never gets it; the caller decides that."""
    if not episode or _pending.get("episode") != episode or _pending.get("partial_reset"):
        return False
    if not (_pending.get("gated") and _pending.get("ladder_ran")):
        return False
    age = episode_age_sec(episode, now)
    return age is not None and age >= grace_sec()


_PARTIAL_PASS: contextvars.ContextVar = contextvars.ContextVar("tt_device_mcp_partial_pass", default="")


@contextlib.contextmanager
def partial_pass(episode: str):
    """Scope this episode's one lifted pass (spec 04 I27). It is spent only once its climb has passed
    the climb's own guards (ceiling, latch, reset scope, tenant, cooldown) and reaches the rungs
    (:func:`note_ladder_ran`): a climb those guards decline leaves it due, so the mesh reset is never
    skipped on the way to the power cycle. A spent pass that leaves the hold standing re-arms the
    watchdog once (:func:`rearm_after_partial_reset`). With no power cycle configured the lift is the
    I33 operator hand-off's, not this pass: nothing is tracked."""
    if power_cycle_argv() is None:
        yield
        return
    token = _PARTIAL_PASS.set(episode)
    try:
        yield
    finally:
        _PARTIAL_PASS.reset(token)
        rearm_after_partial_reset(episode)


def rearm_after_partial_reset(episode: str) -> None:
    """The lifted pass ran and ended with the hold still standing: the watchdog forces one more pass,
    which fires the power cycle, instead of waiting for the next ceiling window (once per episode). A
    pass that never reached its rungs re-arms nothing."""
    if _pending.get("episode") == episode and _pending.get("partial_reset") and not _pending.get("partial_rearmed"):
        _pending["partial_rearmed"] = True
        _pending["spawned"] = False


def hold_handoff_sec() -> Optional[int]:
    """Seconds, from the episode start, an off-bus hold the gate refuses with no power cycle configured
    stands for an operator before the broker lifts the gate for its own ladder (spec 04 I33). A
    non-positive or malformed value reads as 1200; ``off`` is the operator's latch: None, never lifted."""
    if os.environ.get("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", "").strip().lower() == "off":
        return None
    return _int_env("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", HOLD_HANDOFF_DEFAULT_SEC)


def _handoff_deadline(episode: str) -> Optional[float]:
    sec = hold_handoff_sec()
    try:
        return None if sec is None else datetime.fromisoformat(episode).timestamp() + sec
    except (TypeError, ValueError):
        return None


def note_hold_handoff(
    episode: str, off_bus: int, expected: int, why: str, log, *, latched: str = "", cause: str = ""
) -> None:
    """The gate refuses every reset of this episode's off-bus hold and no power cycle is configured: hand
    the hold to an operator with a deadline, once per episode (spec 04 I33). The manual-recovery check
    runs once here, so the page says whether an operator is already on it. ``latched`` names an operator
    gate (the I25 latch) that keeps the gate shut past the deadline. ``cause`` replaces the page's
    default reason (no power-cycle command) when another path hands the hold off (spec 04 I36)."""
    if not episode:
        return
    _track(episode)
    if not _pending.get("handoff"):
        deadline = None if latched else _handoff_deadline(episode)
        argv = manual_recovery_argv()
        seen = standback_reason(require_manual_check=False) if argv is not None else ""
        _pending["handoff"] = {
            "deadline": deadline,
            "why": why,
            "latched": latched or ("" if deadline else "TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC=off"),
            "operator_seen": seen or ("none seen" if argv is not None else "no manual-recovery check configured"),
        }
        metrics.offbus_operator_deadline(deadline or 0)
        health_event(
            "offbus_gated_hold",
            phase="operator_handoff",
            held_since=episode,
            deadline_epoch=deadline,
            latched=_pending["handoff"]["latched"],
            operator_seen=_pending["handoff"]["operator_seen"],
        )
    h = _pending["handoff"]
    if h["deadline"]:
        then = (
            f"an operator must recover the host by {datetime.fromtimestamp(h['deadline']).isoformat(timespec='seconds')}; "
            f"after that the broker lifts the gate for this hold and runs its own reset ladder (bridge reset, "
            f"then the mesh reset), the full health check, fabric included, before any release"
        )
    else:
        then = f"the hold stands until an operator recovers the host ({h['latched']})"
    page_once(
        "offbus_gated_hold_page",
        log,
        f"{off_bus}/{expected} chip(s) off the bus and no reset can end the hold ({why}); "
        f"{cause or 'TT_DEVICE_MCP_POWER_CYCLE_CMD is not set'}, so {then}; manual-recovery check: {h['operator_seen']}",
        held_since=episode,
        off_bus=off_bus,
        expected=expected,
        deadline_epoch=h["deadline"],
    )


def hold_handoff_due(episode: str, now: Optional[float] = None) -> bool:
    """True once this episode's operator hand-off deadline has passed (never for a latched one)."""
    h = _pending.get("handoff") if _pending.get("episode") == episode else None
    return bool(h and h["deadline"] and (now_epoch() if now is None else now) >= h["deadline"])


def hold_handoff_status(episode: str, now: Optional[float] = None) -> Optional[dict]:
    """The /health ``operator_handoff`` field while this episode's hold is handed off: who must act and by
    when, or None."""
    h = _pending.get("handoff") if episode and _pending.get("episode") == episode else None
    if not h:
        return None
    out = {"reason": h["why"], "operator_seen": h["operator_seen"]}
    if not h["deadline"]:
        return {**out, "owner": "operator", "act_by": None, "then": f"hold ({h['latched']})"}
    due = hold_handoff_due(episode, now)
    return {
        **out,
        "owner": "broker" if due else "operator",
        "act_by": datetime.fromtimestamp(h["deadline"]).isoformat(timespec="seconds"),
        "act_by_epoch": h["deadline"],
        "then": "the broker runs its reset ladder with the gate lifted, then the full health check",
    }


def end_hold_handoff() -> None:
    """The hold ended: the deadline metric goes back to 0."""
    if _pending.pop("handoff", None):
        metrics.offbus_operator_deadline(0)


def due_for_watchdog(episode: str, now: Optional[datetime] = None) -> bool:
    """True once per episode, when an armed gated hold's grace has run out and the command is set: the hold watchdog then forces one escalation pass so the
    cycle fires at the bound, not at the next ceiling window."""
    if not episode or _pending.get("episode") != episode or _pending.get("spawned"):
        return False
    if power_cycle_argv() is None:
        return hold_handoff_due(episode, None if now is None else now.timestamp())
    if not _pending.get("ladder_ran"):
        return False
    age = episode_age_sec(episode, now)
    return bool(_pending.get("gated")) and age is not None and age >= grace_sec()


def watchdog_due_at(episode: str) -> Optional[tuple[float, str]]:
    """``(epoch, kind)``: when :func:`due_for_watchdog` turns true for this episode and the bound that
    turns it, named as the hold watchdog names it; the earliest when several are armed. None when it
    never will as things stand (not tracked, already spawned, the ladder has not run, nothing armed).
    Read-only, for the /health hold report (spec 04 I75): moves no clock."""
    if not episode or _pending.get("episode") != episode or _pending.get("spawned"):
        return None
    if power_cycle_argv() is None:
        h = _pending.get("handoff")
        return (float(h["deadline"]), "gated_hold_handoff_bound") if h and h["deadline"] else None
    if not _pending.get("ladder_ran"):
        return None
    out = []
    if _pending.get("gated"):
        try:
            out.append((datetime.fromisoformat(episode).timestamp() + grace_sec(), "gated_offbus_grace"))
        except (TypeError, ValueError):
            pass
    return min(out) if out else None


def mark_spawned(episode: str) -> None:
    if _pending.get("episode") == episode:
        _pending["spawned"] = True


def blocked_page_sec() -> int:
    """Seconds past the grace a power cycle may stand blocked (a tenant, a reset in its own scope, a
    stand-back) before the broker pages that the hold is not ending (default 1800)."""
    return _int_env("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_BLOCKED_PAGE_SEC", BLOCKED_PAGE_DEFAULT_SEC)


def page_once(kind: str, log, message: str, **fields) -> None:
    """Journal, log and alert ``kind`` once per episode (the journal line is the kill-safe record).
    A page the alert hook drops (a previous run still in flight) is sent again on the next pass."""
    journaled = _pending.setdefault("journaled", set())
    paged = _pending.setdefault("paged", set())
    if kind in paged:
        return
    if kind not in journaled:
        journaled.add(kind)
        log(message)
        health_event(kind, message=message, host_at_risk=True, severity="high", **fields)
    event = {
        "kind": kind,
        "message": message,
        "host": socket.gethostname(),
        "host_at_risk": True,
        "severity": "high",
        **fields,
    }
    if alert.send_alert(event) is not None or alert.alert_argv() is None:
        paged.add(kind)


def page_if_blocked(episode: str, age: float, log, why: str, *, bound: Optional[int] = None, **fields) -> None:
    """Hold for ``why`` past the grace (or ``bound``, the clock ``age`` counts against); once it has
    blocked the power cycle for :func:`blocked_page_sec` more, page once: the hold ends in a cycle or a
    page, never silence."""
    limit = (grace_sec() if bound is None else bound) + blocked_page_sec()
    if age >= limit:
        page_once(
            "offbus_power_cycle_blocked",
            log,
            f"off-bus hold {int(age)}s old: the power cycle has been blocked past {limit}s — {why}; "
            f"the hold stands until that clears or an operator recovers the host",
            held_since=episode,
            why=why,
            **fields,
        )


def fire_command(argv: list, reason: str, timeout: float = FIRE_TIMEOUT_DEFAULT_SEC) -> None:
    """Run the host's power-cycle command with the host OS brought down cleanly first (spec 04 I88):
    the shutdown hook runs ``argv`` once the filesystems are read-only. Where that path cannot start
    it runs the command directly (:func:`fire_command_direct`). Tests replace it: no test may cycle
    the host."""
    from tt_device_mcp.health.recovery.stages.power_cycle import fire_os_first

    fire_os_first(argv, reason, lambda: fire_command_direct(argv, reason, timeout))


def fire_command_direct(argv: list, reason: str, timeout: float = FIRE_TIMEOUT_DEFAULT_SEC) -> None:
    """Run the host's power-cycle command with the reason in ``TT_DEVICE_MCP_POWER_CYCLE_REASON``.
    Raises on a non-zero exit, a timeout or a command that cannot start, so the caller's ledger
    retracts an action that never launched; a timeout raises HostActionTimedOut, which keeps the
    request pending (it may still act)."""
    env = {**os.environ, "TT_DEVICE_MCP_POWER_CYCLE_REASON": reason}
    rc, out = _run(argv, _int_env("TT_DEVICE_MCP_POWER_CYCLE_CMD_TIMEOUT_SEC", int(timeout)), env)
    if rc is None and out.startswith("timed out"):
        # It may have started its detached cycler before it stopped answering: the request stays
        # pending (spec 04 I34) rather than reading as never launched.
        from tt_device_mcp.health.recovery.base import HostActionTimedOut

        raise HostActionTimedOut(f"power-cycle command {out}")
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
