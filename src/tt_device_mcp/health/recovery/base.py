# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared, platform-invariant recovery machinery: the failed-reset ledger, the auto-recovery
rate-limit ledger, and scoped reset execution. Every reset in the broker — the health gate and
the reset tool alike — goes through :class:`RecoveryMechanism` so they cannot drift apart on
what "safe" means."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from tt_device_mcp import aio, alert, metrics, privileges
from tt_device_mcp.constants import DEVICE_RESET_OVERRUN_SEC, DEVICE_RESET_TIMEOUT_SEC
from tt_device_mcp.health.aiclk_ceiling import CEILING
from tt_device_mcp.health.aiclk_ceiling import settle_sec as aiclk_settle_sec
from tt_device_mcp.health.evidence import health_dir, health_event
from tt_device_mcp.health.monitors.heartbeat import returned_chips
from tt_device_mcp.health.post_reset import FABRIC_DEBT

# System-broker resets run as transient scopes under this prefix so PID 1 owns them.
# A daemon without systemd uses the local lock below for the same restart-adoption contract.
RESET_SCOPE_PREFIX = "ttdev-reset-"
LOCAL_RESET_NAME = "ttdev-local-reset"
LOCAL_RESET_LOCK = "local-reset.lock"

# A reset that did not fix the device will not fix it on the next attempt either,
# and each extra reset is another chance to trip the root complex into the fatal
# path that resets the host. After a failed reset+verify we stop resetting for
# this long. The device stays flagged throughout, so no tenant job reaches it.
RESET_COOLDOWN_SEC = 600

# The escalation latch stops an episode climbing twice, but "does not repeat" must never mean
# "never again": keyed on the episode alone, a recovery that failed left the ladder with nothing to
# do and the device HELD until a human noticed — measured at 25 minutes with a tenant queued behind
# it, and unbounded by construction, because the latch clears only on a recovery that by definition
# never came. So the latch EXPIRES: after this long the episode may climb again. Long enough that a
# rung is never hammered, finite so the ladder always continues.
HOLD_ESCALATION_REARM_SEC = int(os.environ.get("TT_DEVICE_MCP_HOLD_REARM_SEC", "1800"))

# The last recovery rung, and the highest-risk change in this repo: a warm host reboot. A
# reset recovers an idle device cheaply; a reboot takes EVERY tenant's work down with the box.
# It is armed by default because without it a dropped ASIC holds the device forever, but it is
# opt-OUT per host (TT_DEVICE_MCP_AUTO_REBOOT=0) and rate-limited, and the rate limit is durable on
# disk (see read_auto_recovery_ledger) because a reboot the ledger did not record is a reboot the
# next boot repeats: a loop. The min interval is wall-clock and so spans the reboot itself, which
# is what makes it the loop guard; the per-boot cap tightens it within a single long-lived boot.
AUTO_RECOVERY_MIN_INTERVAL_SEC = int(os.environ.get("TT_DEVICE_MCP_AUTO_RECOVERY_INTERVAL_SEC", "3600"))
AUTO_RECOVERY_MAX_PER_BOOT = 1
AUTO_RECOVERY_LEDGER = "auto_recovery.jsonl"
# This boot's failed mesh resets (spec 04 I58): which off-bus set each one ran on and left, and its exit
# code. On disk, so a broker restart in the middle of an episode does not forget a reset that already
# failed and run it again; keyed by the kernel boot id, so the boot a power cycle brings starts empty.
RESET_RECORD_FILE = "reset_record.json"
RESET_RECORD_MAX = 64


def _chip_key(chip: str) -> tuple:
    return (0, int(chip), "") if chip.isdigit() else (1, 0, chip)


# The power cycle's own spacing guard: at least this long between two power cycles. It is
# wall-clock and durable on disk (the same ledger the boot-loop guard reads), so it spans the
# power cycle itself. This is the ONLY spacing guard on the power cycle — for that action it
# REPLACES the general per-severity interval above (a chassis power cycle recovers a box a human
# would otherwise have to; 30 min is enough to keep it from cycling on a wedge no rung can fix,
# and the user asked for no heavier per-box rate limit than this). The per-boot cap and the
# unreadable-ledger fail-closed below are untouched — they remain the boot-loop denial. Default
# 1800s (30 min); TT_DEVICE_MCP_POWER_CYCLE_MIN_INTERVAL_SEC overrides, clamped to 600..3600 s:
# below 10 min a cycle could fire before the boot it caused has run its startup check, and above
# 1 h a tuning knob would delay the first cycle of a whole-tray drop past its budget (spec 03 I67).
POWER_CYCLE_MIN_INTERVAL_BOUNDS_SEC = (600, 3600)


def _power_cycle_min_interval_env() -> int:
    lo, hi = POWER_CYCLE_MIN_INTERVAL_BOUNDS_SEC
    try:
        v = int(os.environ.get("TT_DEVICE_MCP_POWER_CYCLE_MIN_INTERVAL_SEC", "1800"))
    except ValueError:
        return 1800
    return max(lo, min(v, hi))


POWER_CYCLE_MIN_INTERVAL_SEC = _power_cycle_min_interval_env()
# Spec 04 I65: that interval is a cooldown counted from the LATEST power cycle or boot of this host,
# whoever caused it — the broker's own reboot or power cycle (the ledger), the host's current boot
# (a manual, datacenter or external cycle shows only there), and an external cycler's history file
# when one is named and readable. Spec 03 I67: each broker power cycle that took the host down and
# left the SAME fault (class and off-bus chips) doubles it, capped here — 30 min, 1 h, 2 h, then
# 4 h — so a tray dead in hardware costs at most about six host boots a day, not 48. A verified
# release (a passing full health check) or a different fault resets the streak. The cap is not a
# setting: no knob lengthens the wait past it or switches the cycle off.
POWER_CYCLE_BACKOFF_CAP_SEC = 4 * 3600
# A JSON-lines file an external power cycler appends to (``at_epoch``, optional ``cause``); read only.
POWER_CYCLE_HISTORY_FILE_ENV = "TT_DEVICE_MCP_POWER_CYCLE_HISTORY_FILE"
POWER_CYCLE_HEALED_FILE = "power_cycle_healed.json"
# Spec 03 I67: the fault the last power-cycle decision was for, and a request the power-cycle command
# did not carry out (its rc), so a broker restart restores the deadline and /health says why.
POWER_CYCLE_BACKOFF_FILE = "power_cycle_backoff.json"


def power_cycle_interval_sec(failed_cycles: int) -> int:
    """The cooldown after ``failed_cycles`` consecutive broker power cycles that took the host down
    and left the same fault: the base interval for none, doubled for each, never above the cap
    (spec 03 I67)."""
    base = POWER_CYCLE_MIN_INTERVAL_SEC
    n = max(0, min(int(failed_cycles), 16))
    return max(base, min(base * 2**n, POWER_CYCLE_BACKOFF_CAP_SEC))


def _fault_key(fault) -> Optional[tuple]:
    """A power-cycle fault as (class, off-bus chips), or None when not given or unreadable."""
    if not isinstance(fault, dict) or not fault.get("class"):
        return None
    try:
        chips = tuple(sorted(str(c) for c in fault.get("off_bus") or ()))
    except TypeError:
        chips = ()
    return (str(fault["class"]), chips)


_RC_RE = re.compile(r"exited (-?\d+)")


def _local_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(sep=" ", timespec="seconds")


def _epoch_after(value, than: float) -> bool:
    try:
        return float(value) > than
    except (TypeError, ValueError):
        return True


def _last_external_power_cycle() -> Optional[tuple[float, str]]:
    """The latest ``at_epoch`` in the external cycler's history file and its cause, or None when no
    file is named or it cannot be read (the host's boot time still covers any cycle that took the box
    down). Malformed lines are skipped."""
    name = os.environ.get(POWER_CYCLE_HISTORY_FILE_ENV, "").strip()
    if not name:
        return None
    try:
        lines = Path(name).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    best: Optional[tuple[float, str]] = None
    for ln in lines:
        try:
            rec = json.loads(ln)
            at = float(rec.get("at_epoch"))
        except (ValueError, TypeError, AttributeError):
            continue
        if best is None or at > best[0]:
            best = (at, str(rec.get("cause") or "external power cycle"))
    return best


# A host reboot or power cycle the broker has requested takes the box down, and an external cycler
# (a POWER_CYCLE_CMD wrapper) may run it some time after the request returns. A device reset the
# hold watchdog starts in between only races the cycle. So once one is requested the forced ladder
# starts no reset — but only this long: a request the host has not acted on by then reads as
# failed and the ladder resumes, so a cycle that is not coming never leaves the hold open-ended.
# TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC overrides; garbage or non-positive reads as the default,
# and it is capped so no setting means "wait forever".
HOST_ACTION_PENDING_DEFAULT_SEC = 900.0
HOST_ACTION_PENDING_MAX_SEC = 3600.0


def host_action_pending_sec() -> float:
    """How long a requested host reboot or power cycle suppresses the forced ladder. Read per call."""
    raw = os.environ.get("TT_DEVICE_MCP_HOST_ACTION_PENDING_SEC", "").strip()
    try:
        v = float(raw)
    except ValueError:
        return HOST_ACTION_PENDING_DEFAULT_SEC
    if not 0 < v < float("inf"):
        return HOST_ACTION_PENDING_DEFAULT_SEC
    return min(v, HOST_ACTION_PENDING_MAX_SEC)


# The host-level recovery rungs, ordered by severity. A warm reboot recovers most wedges; a
# whole-bus wedge survives it and only the chassis power cycle clears it (measured on box D).
# The min-interval loop guard is therefore PER SEVERITY: a stronger rung may escalate past a
# weaker one that just fired — a reboot that did not fix the mesh must not force an hour's wait
# before the power cycle that will — but no rung may re-fire itself inside the interval (its own
# loop guard), and a stronger rung that fired blocks de-escalating back to a weaker one.
AUTO_RECOVERY_RANK = {"reboot": 1, "power-cycle": 2}

# The metrics.STAGES name for each auto-recovery action _fire_recovery_escalation ever fires. An
# explicit table, not a ternary: a third action added here later must fail loud (KeyError) rather
# than silently mislabel itself onto whichever branch a two-way ternary happened to default to.
_ACTION_TO_STAGE = {"reboot": "host_reboot", "power-cycle": "power_cycle"}

# How long after a broker escalation records itself the resulting boot must appear for the boot to be
# ATTRIBUTED to that escalation. An escalation is recorded seconds-to-minutes before the reboot/power
# cycle it fires completes; a boot that comes up long after is a DIFFERENT reboot (an external one, or
# KMD remediation), and attributing it to a stale ledger entry mis-stamps it as a broker auto-recovery
# it never fired. Generous enough for a slow cold BMC power cycle + POST, tight enough to reject a
# stale entry hours old.
BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC = int(
    os.environ.get("TT_DEVICE_MCP_BOOT_ATTRIBUTION_WINDOW_SEC", "900").strip() or "900"
)

# An outage hold turns every hardware rung off at once. Each alone is a normal per-host choice (an
# unconfirmed BMC turns the tray rung off and the ladder still climbs through the mesh reset), but
# all three off together say "touch nothing": the mesh reset is held too.
OUTAGE_HOLD_VARS = ("TT_DEVICE_MCP_AUTO_REBOOT", "TT_DEVICE_MCP_AUTO_POWER_CYCLE", "TT_DEVICE_MCP_AUTO_UBB_RESET")
# The hold reason -> when its page went out (monotonic). A held mesh reset stays HELD until an operator
# acts, so the page is the hold's only bound: while the hold stands it is sent again every
# mesh_hold_repage_sec(), so a page that was missed or forgotten never leaves the device held in silence.
_mesh_hold_paged: dict = {}
MESH_HOLD_REPAGE_DEFAULT_SEC = 3600


def mesh_hold_repage_sec() -> int:
    """``TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC``: how long a standing mesh-reset hold waits before it pages
    again (default 3600). ``0`` pages once per hold; nonsense reads as the default."""
    try:
        value = int(os.environ.get("TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC", "").strip())
    except ValueError:
        return MESH_HOLD_REPAGE_DEFAULT_SEC
    return value if value >= 0 else MESH_HOLD_REPAGE_DEFAULT_SEC


def mesh_reset_hold_reason() -> str:
    """Why the operator forbids an AUTOMATIC mesh-wide reset (the gate's, the idle relift's, the
    watchdog's or the last-chance sweep's ``tt-smi -glx_reset``), or "" when it may fire.

    ``TT_DEVICE_MCP_AUTO_MESH_RESET=0`` holds it explicitly and ``=1`` arms it whatever else is set.
    Unset, an outage hold (every :data:`OUTAGE_HOLD_VARS` set to 0) holds it: a mesh reset on a mesh
    that is already off the bus is what starts an off-bus power-cycle loop. The operator's own reset
    tool is never held."""
    mesh = os.environ.get("TT_DEVICE_MCP_AUTO_MESH_RESET", "").strip()
    if mesh == "0":
        return "TT_DEVICE_MCP_AUTO_MESH_RESET=0"
    if mesh == "1":
        return ""
    if all(os.environ.get(v, "").strip() == "0" for v in OUTAGE_HOLD_VARS):
        return "outage hold (" + ", ".join(f"{v}=0" for v in OUTAGE_HOLD_VARS) + ")"
    return ""


def drop_returned_isolated(isolated: set, context: str, log=None) -> list:
    """Re-read sysfs and take every chip of ``isolated`` that is back on the bus out of it, in place.
    Run after every tray re-power, mesh reset and PCI rescan, whatever the verify that follows says:
    a chip left in the set after it returned keeps /health and the gate reading it as isolated and
    arms a needless bridge reset on the next pass. Never releases anything: only the full health
    check, fabric included, does that (spec 03 I52)."""
    if not isolated:
        return []
    back = returned_chips(isolated)
    for c in back:
        isolated.discard(c)
    if back:
        health_event("isolated_chips_returned", chips=back, context=context, still_isolated=sorted(isolated, key=int))
        if log:
            log(f"chip(s) {back} back on the bus after the {context}: no longer isolated")
    return back


def refuse_mesh_reset(context: str, argv: list, log, off_bus: int = 0) -> bool:
    """True when :func:`mesh_reset_hold_reason` holds the mesh reset: logged, journaled and paged
    once per hold, again every :func:`mesh_hold_repage_sec` while it stands and after a pass that
    found no hold. A refused reset did not happen, so the caller holds exactly as for a gated one and
    arms no cooldown or failed-reset flag. ``off_bus`` (chips off the bus this pass) is named in
    the page: the case a mesh reset turns into an off-bus power-cycle loop."""
    why = mesh_reset_hold_reason()
    if not why:
        _mesh_hold_paged.clear()
        return False
    held = f"{off_bus} chip(s) off the bus, " if off_bus > 0 else ""
    message = (
        f"automatic mesh reset NOT fired ({context}): {held}{why}; device HELD for an operator — "
        f"lift the hold or set TT_DEVICE_MCP_AUTO_MESH_RESET=1 to re-arm it"
    )
    log(message)
    health_event("mesh_reset_held", context=context, argv=argv, reason=why, off_bus=off_bus, host_at_risk=True)
    metrics.stage_fired("smi_reset", "blocked")
    paged_at = _mesh_hold_paged.get(why)
    repage = mesh_hold_repage_sec()
    if paged_at is None or (repage > 0 and time.monotonic() - paged_at >= repage):
        event = {"kind": "mesh_reset_held", "context": context, "reason": why, "off_bus": off_bus, "message": message}
        if alert.send_alert(event) is not None or alert.alert_argv() is None:
            _mesh_hold_paged[why] = time.monotonic()
    return True


class RecoveryMechanism:
    """Shared, platform-invariant machinery. ``Recovery`` (recovery/__init__.py) holds this by
    composition as ``self.mechanism`` — never by inheritance — because this object's state
    (``reset_in_flight``, the failed-reset cooldown, the durable ledger) must outlive any one
    ``Recovery`` instance: the telemetry sampler and the gate read/write it across calls and
    across whichever platform ``Recovery`` subclass is selected for a given pass."""

    def __init__(
        self,
        *,
        current_boot_id: Callable[[], str],
        boot_btime_id: Callable[[], str],
        scoped_reset_backend: Callable[[], bool],
        set_device_pollers: Callable[[bool, Callable], Awaitable[list[str]]],
        set_device_op_detail: Callable[[str], None],
        begin_action_row: Callable[[str, str], None],
        write_action_log: Callable[[str, str, float, str, Optional[int]], None],
        device_hold_episode_since: Callable[[], str],
        local_reset_dir: Optional[Callable[[], Path]] = None,
        isolated_chips: Optional[Callable[[], set]] = None,
    ) -> None:
        # Server-state accessors this mechanism cannot own without importing server (a cycle):
        # the broker's boot-id/boot-time readers, its systemd-availability predicate, the poller
        # quiesce/restore pair, the jobs-list detail line and action-log row pair, and the live hold
        # episode clock. Late-bound by the caller (server.py wraps each in a lambda over its own
        # module globals) so a test that monkeypatches the underlying server function still takes
        # effect here.
        self._current_boot_id = current_boot_id
        self._boot_btime_id = boot_btime_id
        self._scoped_reset_backend = scoped_reset_backend
        self._set_device_pollers = set_device_pollers
        self._set_device_op_detail = set_device_op_detail
        self._begin_action_row = begin_action_row
        self._write_action_log = write_action_log
        self._device_hold_episode_since = device_hold_episode_since
        self._local_reset_dir = local_reset_dir or health_dir
        # The broker's isolated-chip set, re-read after every reset's rescan (drop_returned_isolated).
        self._isolated_chips = isolated_chips
        # Why no reset may run now — '' when one may. The broker sets it to its leftover fence (spec
        # 01 I16): a reaped job's process still holding the device, which a reset cannot free.
        # Checked here, where every reset passes, so no caller can reset over one.
        self.reset_fence: Optional[Callable[[], str]] = None

        # Two callers (the auto-recovery reset launcher and the operator-run reset tool) need this
        # sequence and they must build it identically, because the scope name is what every other
        # protection keys on.
        self.reset_scope_seq = 0

        # True while a reset is actually executing. A reset TAKES THE CHIPS OFF THE BUS — that is
        # what a reset is — so during one they read all-ones exactly like a dead chip, and isolating
        # them then tears the endpoints out of the kernel mid-reset and destroys it. This flag is the
        # only thing that separates "the chip died" from "we are resetting the chip". It guards
        # nothing else: during a fabric check or a health probe, chips going all-ones IS a failure and
        # must still be acted on — that is precisely how a host was lost.
        self.reset_in_flight = False
        # Any reset_with_quiesce (the ladder's or a manual one) since the mesh was last released:
        # an off-bus set first seen after it is the reset's doing, never a tray-down onset (04 I19).
        self.reset_since_release = False
        # A mesh reset ran to an exit and left the mesh unhealthy since the mesh was last released.
        # With every chip still off the bus after it, a second one is futile (spec 04 I37): the
        # episode goes straight to the power-cycle rung.
        self.mesh_reset_failed_since_release = False
        # Spec 04 I65: when a power cycle the cooldown denied may be asked again, and the hold episode
        # it was denied in — the hold watchdog forces one escalation at that time. None when nothing waits.
        self.power_cycle_retry: Optional[tuple[float, str]] = None
        # The cooldown in force at the last power-cycle decision (until, last, cause, interval), for /health.
        self.power_cycle_cooldown_state: Optional[dict] = None
        # Spec 03 I67: the fault (I37 class and off-bus chips) the power cycle being decided is for,
        # set by the I37 path around its ask; None for a caller that names none.
        self.power_cycle_fault: Optional[dict] = None

        # The full transcript of the last reset, not the 3-line tail the journal carries: when a
        # mesh is left half-alive, the interesting line is usually somewhere in the middle.
        self.last_reset_output = ""

        self.last_reset_monotonic: float = 0.0
        self.last_reset_failed: bool = False
        # The hold episode (its ``since``) whose reset already came back UNKNOWN — enum+ARC healthy, the
        # fabric a persistent 77. A second identical reset on that episode measures nothing new, so the
        # idle escalation goes to the last-chance sweep instead (spec 04 I29).
        self.unverified_reset_since: Optional[str] = None
        # The episode whose last-chance sweep that path already ran; the next escalation climbs.
        self.unverified_sweep_since: Optional[str] = None
        # The episode whose sweep verify still read a fabric 77: no rung measured a fault, so it never
        # climbs to a host rung; it holds, pages once and the idle relift keeps re-checking.
        self.unverified_sweep_77_since: Optional[str] = None
        # The hold episode (its ``since``) whose galaxy reset REGRESSED an off-bus mesh (more chips off
        # the bus after it than before). Another mesh reset on that episode repeats the action that just
        # made the drop worse — one Galaxy host went 4 -> 31 -> 32 off-bus across three — so the episode
        # goes straight to the host rung, or pages when none is opted in (spec 04 I31).
        self.regressed_reset_since: Optional[str] = None
        # This boot's reset rungs that ran and failed, with the off-bus set before and after each.
        # Firing one again on a set it left unchanged measures nothing new (one Galaxy host ran six
        # identical 263 s mesh resets in one hold, one per forced window), so the next window takes the
        # next stronger rung instead (spec 04 I36). Loaded from RESET_RECORD_FILE on first use and
        # written back before the next rung runs, so a broker restart keeps it (spec 04 I58).
        self._reset_record_cache: Optional[tuple[Path, dict]] = None
        # The exit code of the last reset this process launched; None when it was refused, never
        # launched or timed out still cycling. Only a launched reset that failed is recorded.
        self.last_reset_rc: Optional[int] = None
        # Whether the last reset command EXITED non-zero (a real `reset_done rc=1`, not a timeout
        # still cycling nor a verify that failed after a clean exit). A reset that could not even run
        # leaves the mesh in whatever off-bus state it was, and a warm reboot cannot re-enumerate a
        # dropped Galaxy ASIC — so this routes the escalation to the cold rung rather than a reboot
        # that cannot work.
        self.last_reset_exit_nonzero: bool = False

        # Latched when tt-smi's own output says `-r` cannot recover this host: the banner it prints
        # on a Galaxy whose CPLD firmware predates v1.16 (see `_journal_cpld_too_old`). It is proof
        # of the board class, not a guess about it — tt-smi only prints it on a Galaxy — so it
        # answers the reset-mode question that `_is_galaxy` returns None for on exactly this
        # hardware, since a degraded Galaxy is where tt-smi reports "N/A" board types.
        #
        # Process lifetime, deliberately not durable. Persisting it would need a new state file
        # for a signal whose real fix is an operator declaring TT_DEVICE_MCP_RESET_MODE=galaxy,
        # which the same event tells them to do. The cost of forgetting is bounded and known: one
        # more `-r` after a broker restart, where before it was one per recovery episode.
        self.cpld_forces_galaxy: bool = False

        # The hold episode we last journaled an auto-recovery denial for — dedups the loud "the
        # ladder is blocked / exhausted" event to once per episode instead of every gate pass on a
        # held box.
        self._auto_recovery_denied_episode = ""

        # The host-level action (reboot / power-cycle) last requested and when (monotonic). Set once
        # its record is on disk, cleared if it fails to launch; read by the hold watchdog, which
        # starts no device reset while one is pending (see host_action_pending).
        self.host_action_requested: Optional[tuple[str, float]] = None

    def host_action_pending(self, now: Optional[float] = None) -> Optional[tuple[str, float]]:
        """``(action, age_sec)`` while a requested host reboot or power cycle is still inside its
        pending window, else None. Past the window the request is dropped — the host never acted on
        it — and the caller sees None, so the ladder resumes instead of waiting on it for good."""
        req = self.host_action_requested
        if req is None:
            return None
        action, at = req
        age = (time.monotonic() if now is None else now) - at
        if age >= host_action_pending_sec():
            self.host_action_requested = None
            return None
        return action, age

    def cooling(self) -> bool:
        """True while a reset that already failed is still inside its cooldown window.

        The exact predicate the health gate and the idle-hold escalators each re-derive before
        trying another reset: a reset we already tried and that already failed to revive the mesh
        will not revive it now, and hammering a dead endpoint is precisely what escalates a PCIe
        error to fatal and reboots the host."""
        return self.last_reset_failed and (time.monotonic() - self.last_reset_monotonic) < RESET_COOLDOWN_SEC

    def note_reset_regressed(self, hold_since: str) -> None:
        """Latch that this hold episode's galaxy reset regressed an off-bus mesh (spec 04 I31). A
        regression seen before any hold opened (a gate's first reset) latches the episode about to
        open; the release transition clears the latch either way."""
        self.regressed_reset_since = hold_since or "*"

    def reset_regressed_in(self, hold_since: str) -> bool:
        """True when this hold episode's galaxy reset already regressed the mesh: another mesh
        reset on it is not tried (spec 04 I31)."""
        latched = self.regressed_reset_since
        return latched is not None and (latched == "*" or latched == hold_since)

    def _reset_record(self) -> dict:
        """This boot's failed-reset record, read from the health dir once per process (spec 04 I58).
        A file from another boot, or with no boot id to match, starts a fresh record."""
        path = health_dir() / RESET_RECORD_FILE
        cached = self._reset_record_cache
        if cached is not None and cached[0] == path:
            return cached[1]
        boot = self._current_boot_id() or ""
        rec: dict = {"boot_id": boot, "failed": []}
        raw = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            health_event("reset_record_unreadable", path=str(path), error=repr(exc))
        if isinstance(raw, dict) and boot and raw.get("boot_id") == boot and isinstance(raw.get("failed"), list):
            rec["failed"] = [e for e in raw["failed"] if isinstance(e, dict)][-RESET_RECORD_MAX:]
            if rec["failed"]:
                health_event("reset_record_restored", boot_id=boot, failed=rec["failed"])
        self._reset_record_cache = (path, rec)
        return rec

    def _save_reset_record(self, rec: dict) -> None:
        path = health_dir() / RESET_RECORD_FILE
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            # Kept in memory either way; only a broker restart before the next write forgets it.
            health_event("reset_record_unwritable", path=str(path), error=repr(exc))

    def note_rung_failed(self, hold_since: str, rung: str, off_before, off_after, rc: Optional[int] = None) -> None:
        """Record that ``rung`` ran and failed this boot (spec 04 I36, I58), on disk before the caller
        goes on to any other rung. Every launched failure counts toward class C (I37); only one that
        left the off-bus set exactly as it found it stops a repeat: a set that grew is a regression
        (I31) and one that shrank is progress another try may finish."""
        rec = self._reset_record()
        rec["failed"].append(
            {
                "rung": rung,
                "rc": rc,
                "off_before": None if off_before is None else sorted(map(str, off_before), key=_chip_key),
                "off_after": None if off_after is None else sorted(map(str, off_after), key=_chip_key),
                "episode": hold_since or "",
                "at_epoch": time.time(),
            }
        )
        del rec["failed"][:-RESET_RECORD_MAX]
        self._save_reset_record(rec)

    def failed_unchanged_entry(self, hold_since: str, rung: str, off_now) -> Optional[dict]:
        """The record of ``rung`` failing this boot on exactly the off-bus set seen now, or None. A
        non-empty set matches across hold episodes and broker restarts (I58); an empty set (a present
        mesh) only within its own hold episode, as before (I36)."""
        if off_now is None:
            return None
        now = set(map(str, off_now))
        for entry in reversed(self._reset_record()["failed"]):
            before, after = entry.get("off_before"), entry.get("off_after")
            if entry.get("rung") != rung or before is None or after is None:
                continue
            if set(before) != now or set(after) != now:
                continue
            if not now and (not hold_since or entry.get("episode") != hold_since):
                continue
            return entry
        return None

    def rung_failed_unchanged(self, hold_since: str, rung: str, off_now) -> bool:
        """True when ``rung`` already failed this boot on exactly the off-bus set seen now: no path
        fires it again on that set (spec 04 I36, I58)."""
        return self.failed_unchanged_entry(hold_since, rung, off_now) is not None

    def mesh_reset_failed_this_boot(self, off_now=None) -> bool:
        """A mesh reset already ran and failed on this mesh: in this process since the last release, or
        in this boot's record (a broker restart included) leaving exactly the chips off now. Class C
        reads it (spec 04 I37, I58); a reset that failed on some other set and was later healed does
        not make a new whole-bus drop skip its one reset."""
        if self.mesh_reset_failed_since_release:
            return True
        if off_now is None:
            return False
        now = set(map(str, off_now))
        return any(
            e.get("rung") == "mesh_reset" and e.get("off_after") is not None and set(e["off_after"]) == now
            for e in self._reset_record()["failed"]
        )

    def broker_cycled_this_episode(self) -> bool:
        """The broker started a host reboot or power cycle for this fault: the boot now running is its
        result (:meth:`boot_from_broker_escalation`), or one was requested this boot since the hold
        episode began. Class C reads it with :meth:`mesh_reset_failed_this_boot` (spec 04 I37): a
        whole bus still off after the broker's own cycle gets no mesh reset first."""
        boot = self._current_boot_id() or ""
        if self.boot_from_broker_escalation(boot) is not None:
            return True
        since = self._device_hold_episode_since()
        try:
            start = datetime.fromisoformat(since).timestamp() if since else None
        except ValueError:
            start = None
        if not boot or start is None:
            return False
        for rec in self.read_auto_recovery_ledger() or []:
            if rec.get("boot_id") != boot or rec.get("action") not in AUTO_RECOVERY_RANK:
                continue
            try:
                if float(rec.get("at_epoch")) >= start:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _local_reset_lock_path(self) -> Path:
        return self._local_reset_dir() / LOCAL_RESET_LOCK

    def local_reset_active(self) -> bool:
        """Whether a detached local reset child still owns the cross-process lock.

        Only the local backend takes that lock, so where a PID-1 scope is used this is always
        free — answered by the lock itself rather than by a predicate that could disagree with it.
        """
        path = self._local_reset_lock_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            os.close(fd)
        return False

    def scope_active(self) -> Optional[str]:
        """Name of a restart-safe reset still running, including one started by a
        previous broker process — or by a different user's broker on this host.

        Keyed on systemd being present, NOT on this process being able to start a scope. Listing
        units needs no privilege, and the two questions come apart on exactly the host where the
        answer matters: an unprivileged daemon beside a root broker must still see the root
        broker's reset, or I5's adopt-never-race is lost and it fires its own into a mesh already
        cycling. The dead-chip sampler reads this for the same reason — mid-reset all-ones is not
        a dead chip.
        """
        if privileges.has_systemd():
            try:
                out = subprocess.run(
                    [
                        "systemctl",
                        "list-units",
                        "--type=scope",
                        "--state=running",
                        "--no-legend",
                        "--no-pager",
                        f"{RESET_SCOPE_PREFIX}*",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                out = None
            if out is not None:
                for line in out.stdout.splitlines():
                    unit = line.strip().lstrip("*• ").split(" ", 1)[0]
                    if unit.startswith(RESET_SCOPE_PREFIX):
                        return unit
        if self.local_reset_active():
            return LOCAL_RESET_NAME
        return None

    async def await_foreign_scope(self, log) -> bool:
        """Wait out a restart-safe reset we did not start (ours, from before a restart).

        Returns True if one was found. Starting a second -glx_reset at
        32 ASICs while one is already in flight is the concurrent-reset failure this
        whole module exists to prevent, and a broker restart is exactly when we are
        blind to the reset we ourselves launched.
        """
        unit = await asyncio.to_thread(self.scope_active)
        if not unit:
            return False
        log(f"a reset is already in flight ({unit}); waiting for it rather than starting another")
        health_event("reset_scope_adopted", unit=unit)
        FABRIC_DEBT.note_reset(f"adopted {unit}")
        deadline = time.monotonic() + DEVICE_RESET_TIMEOUT_SEC
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            if not await asyncio.to_thread(self.scope_active):
                log(f"in-flight reset {unit} finished")
                return True
        log(f"in-flight reset {unit} still running after {DEVICE_RESET_TIMEOUT_SEC}s; not starting another")
        return True

    def _reset_scope_argv(self, argv: list[str]) -> tuple[str, list[str]]:
        """``(unit, argv)`` that runs one reset inside its own transient systemd scope.

        Two callers need this and they must build it identically, because the scope is what
        every other protection keys on. Its name is how the dead-chip sampler knows a reset is
        in flight: 32 chips reading all-ones IS a reset, and a sampler that cannot tell the
        difference declares the box a catastrophe and holds it — against the operator whose
        reset it was watching. Its ownership by PID 1 is how a reset survives our restart
        rather than stopping halfway through 32 ASICs.
        """
        self.reset_scope_seq += 1
        unit = f"{RESET_SCOPE_PREFIX}{os.getpid()}-{self.reset_scope_seq}"
        return unit, [
            "systemd-run",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            # Outlive the broker: without this the scope inherits our death signal.
            "--property=KillMode=mixed",
            "--",
            *argv,
        ]

    async def _run_local_reset(
        self,
        argv: list[str],
        log,
        owner: str = "[broker]health-gate",
        on_output: Optional[Callable[[str], None]] = None,
    ) -> tuple[Optional[int], str]:
        """Run a detached no-systemd reset whose inherited flock survives the daemon."""
        self.reset_scope_seq += 1
        unit = LOCAL_RESET_NAME
        started = datetime.now()
        command = " ".join(argv)
        self._begin_action_row(owner, command)
        base = self._local_reset_dir()
        lock_fd = None
        output_file = None
        proc = None
        output_path = base / f"{LOCAL_RESET_NAME}-{os.getpid()}-{self.reset_scope_seq}.log"
        try:
            base.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(self._local_reset_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                dt = (datetime.now() - started).total_seconds()
                text = "a local reset is already in flight; reset not attempted"
                health_event("reset_launch_deferred", unit=unit, argv=argv, reason=text)
                self._write_action_log(owner, command, dt, "deferred", None)
                return None, text
            output_file = open(output_path, "w+b")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=output_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(lock_fd,),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            # The child now owns the inherited lock for exactly its lifetime.
            os.close(lock_fd)
            lock_fd = None

            async def wait_and_stream() -> int:
                with open(output_path, "rb") as reader:
                    while proc.returncode is None:
                        chunk = reader.read()
                        if chunk and on_output is not None:
                            try:
                                on_output(chunk.decode("utf-8", "replace"))
                            except Exception:
                                pass
                        await asyncio.sleep(0.05)
                    await proc.wait()
                    chunk = reader.read()
                    if chunk and on_output is not None:
                        try:
                            on_output(chunk.decode("utf-8", "replace"))
                        except Exception:
                            pass
                return proc.returncode

            wait_task = asyncio.create_task(wait_and_stream())
            try:
                await aio.wait_for(asyncio.shield(wait_task), timeout=DEVICE_RESET_OVERRUN_SEC)
            except asyncio.TimeoutError:
                over = (datetime.now() - started).total_seconds()
                log(
                    f"reset still running after {int(over)}s ({unit}); waiting — it is not "
                    f"killed, and only a reset that never ends is a failure"
                )
                health_event("reset_overran", unit=unit, seconds=over, argv=argv)
                await aio.wait_for(
                    asyncio.shield(wait_task), timeout=max(1, DEVICE_RESET_TIMEOUT_SEC - DEVICE_RESET_OVERRUN_SEC)
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            dt = (datetime.now() - started).total_seconds()
            log(f"reset never finished after {int(dt)}s ({unit} left running, not killed mid-reset)")
            health_event("reset_timeout", unit=unit, seconds=dt, argv=argv)
            self._write_action_log(owner, command, dt, "timeout", None)
            return None, f"reset never finished after {int(dt)}s"
        except (OSError, ValueError) as exc:
            dt = (datetime.now() - started).total_seconds()
            log(f"reset could not be launched: {exc}")
            health_event("reset_launch_failed", unit=unit, error=str(exc), argv=argv)
            self._write_action_log(owner, command, dt, "error", None)
            return None, f"reset could not be launched: {exc}"
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if output_file is not None:
                output_file.close()

        dt = (datetime.now() - started).total_seconds()
        try:
            text = output_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        self.last_reset_output = text
        tail = text.splitlines()
        rc = proc.returncode
        health_event("reset_done", unit=unit, rc=rc, seconds=dt, argv=argv, tail=tail[-3:] if tail else [])
        self._write_action_log(owner, command, dt, "completed" if rc == 0 else "failed", rc)
        output_path.unlink(missing_ok=True)
        return rc, text

    async def run_scoped(
        self,
        argv: list[str],
        log,
        owner: str = "[broker]health-gate",
        on_output: Optional[Callable[[str], None]] = None,
    ) -> tuple[Optional[int], str]:
        """Run one device reset through the available restart-safe backend.

        Root on a systemd host uses a PID-1 scope. Otherwise the reset uses a
        new-session child that inherits an exclusive local lock. Both backends survive
        broker restart and let the next process adopt the in-flight reset.

        ``owner`` names the jobs-list row this reset leaves. It defaults to the gate — the
        broker resetting on its own — because that is who calls this for recovery; the reset
        tool passes the human who asked, so an operator-run reset is one row attributed to
        them, not the gate's row plus a duplicate.

        Returns (exit code, combined output). The code is None if the reset timed out or
        could not be launched.
        """
        if not self._scoped_reset_backend():
            # `tt-smi -r` is an ioctl on a device node the submitter already holds (spec 04
            # I17), so lacking a PID-1 scope selects the other backend, it does not cancel the
            # reset.
            return await self._run_local_reset(argv, log, owner, on_output)
        unit, scoped = self._reset_scope_argv(argv)
        _t0 = datetime.now()
        # Open the jobs-list row now, not on completion: a reset that overruns or never returns is
        # exactly the one an operator goes looking for, and a row written only at the end gives it
        # no id to find until it is over. The terminal _write_action_log calls below settle it.
        self._begin_action_row(owner, " ".join(argv))
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *scoped,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            async def collect_output() -> tuple[bytes, None]:
                chunks = []
                async for raw in proc.stdout:
                    chunks.append(raw)
                    if on_output is not None:
                        try:
                            on_output(raw.decode("utf-8", "replace"))
                        except Exception:
                            pass
                await proc.wait()
                return b"".join(chunks), None

            output_task = asyncio.create_task(collect_output()) if on_output is not None else None

            async def communicate():
                if output_task is not None:
                    return await asyncio.shield(output_task)
                return await proc.communicate()

            try:
                out, _ = await aio.wait_for(communicate(), timeout=DEVICE_RESET_OVERRUN_SEC)
            except asyncio.TimeoutError:
                # Our timer is not the reset's deadline. The scope is deliberately never killed —
                # a reset stopped partway through 32 ASICs is far worse than one that overran —
                # so it is still going, and calling it failed here reports only that we stopped
                # watching. That verdict is expensive: it arms the failed-reset cooldown, which
                # suppresses the NEXT reset, and holds the device on a finding about a reset that
                # went on to work. Measured on this box, resets pass this line and succeed. So
                # say it is slow and keep waiting for the answer.
                over = (datetime.now() - _t0).total_seconds()
                log(
                    f"reset still running after {int(over)}s (scope {unit}); waiting — it is not "
                    f"killed, and only a scope that never ends is a failure"
                )
                health_event("reset_overran", unit=unit, seconds=over, argv=argv)
                out, _ = await aio.wait_for(
                    communicate(), timeout=max(1, DEVICE_RESET_TIMEOUT_SEC - DEVICE_RESET_OVERRUN_SEC)
                )
            rc = proc.returncode
        except asyncio.TimeoutError:
            dt = (datetime.now() - _t0).total_seconds()
            # It never ended. Still not killed, for the same reason; the next caller waits on the
            # scope rather than starting a second reset (see await_foreign_scope).
            log(f"reset never finished after {int(dt)}s (scope {unit} left running, not killed mid-reset)")
            health_event("reset_timeout", unit=unit, seconds=dt, argv=argv)
            self._write_action_log(owner, " ".join(argv), dt, "timeout", None)
            return None, f"reset never finished after {int(dt)}s"
        except (OSError, ValueError) as e:
            dt = (datetime.now() - _t0).total_seconds()
            log(f"reset could not be launched: {e}")
            health_event("reset_launch_failed", unit=unit, error=str(e), argv=argv)
            # A reset that never launched is still a reset attempt, and the one an operator most
            # needs to find later: the recovery ladder tried and could not even start. Record it
            # like the timeout path does, or that attempt leaves no trace in the jobs list.
            self._write_action_log(owner, " ".join(argv), dt, "error", None)
            return None, f"reset could not be launched: {e}"
        dt = (datetime.now() - _t0).total_seconds()
        text = (out.decode("utf-8", "replace") if out else "").strip()
        # The full transcript, not the 3-line tail the journal carries: when a reset leaves a
        # mesh half-alive, the interesting line is usually somewhere in the middle.
        self.last_reset_output = text
        tail = text.splitlines()
        health_event("reset_done", unit=unit, rc=rc, seconds=dt, argv=argv, tail=tail[-3:] if tail else [])
        self._write_action_log(owner, " ".join(argv), dt, "completed" if rc == 0 else "failed", rc)
        return rc, text

    async def reset_with_quiesce(
        self,
        argv: list[str],
        log,
        owner: str = "[broker]health-gate",
        on_output: Optional[Callable[[str], None]] = None,
    ) -> tuple[Optional[int], str]:
        """One reset, done safely: quiet the bus, run it where nothing can interrupt it,
        put the pollers back. Every reset in the broker goes through here — the health
        gate and the reset tool must not be able to drift apart on what "safe" means.

        Caller MUST hold ``_device_op`` (this does not take the lock itself, because the
        caller's critical section usually spans the verification too).

        ``owner`` is passed straight to the jobs-list row (see ``run_scoped``).

        Returns (exit code, combined output); the code is None on timeout/launch failure.
        """
        fenced = self.reset_fence() if self.reset_fence else ""
        if fenced:
            log(f"reset refused: {fenced}")
            health_event("reset_fenced", argv=argv, owner=owner, reason=fenced)
            return None, f"reset refused: {fenced}"
        # Name the command we are actually running. This was hardcoded to the galaxy's -glx_reset, so on
        # every other machine the queue told operators a command that was not the one executing.
        self._set_device_op_detail(f"device reset: {' '.join(argv)} (~60s)")
        # Any reset clears a firmware clock cap; owed until a verified re-apply (see aiclk_ceiling).
        CEILING.mark_owed("reset")
        # And it leaves the fabric unproven: no release until a fabric pass passes (post_reset).
        FABRIC_DEBT.note_reset(" ".join(argv))
        quiesced = await self._set_device_pollers(False, log)
        # A reset takes the chips off the bus — that is what it does — so for its duration they
        # read all-ones exactly like a dead chip. Without this flag the sampler isolates them
        # mid-reset and tears the endpoints out of the kernel, which is how a healthy host ended
        # up with no devices at all.
        self.reset_in_flight = True
        self.reset_since_release = True
        cancelled_mid = False
        rc = None
        try:
            if on_output is None:
                rc, text = await self.run_scoped(argv, log, owner)
            else:
                rc, text = await self.run_scoped(argv, log, owner, on_output)
            return rc, text
        except asyncio.CancelledError:
            cancelled_mid = True  # abandoned mid-reset; the detached backend keeps cycling the chips
            raise
        finally:
            # Clear on every exit, including a mid-reset cancel. The flag gates the dead-chip sampler
            # (it returns early while set), so leaving it set on a now-healthy box blinds the sampler to
            # a real chip drop until the next completed reset. Once clear the sampler still defers on
            # scope_active() while the detached scope keeps cycling, so clearing here cannot
            # amputate a chip mid-reset.
            self.reset_in_flight = False
            # Restore the pollers only when no reset scope is still cycling the chips off the bus.
            # Restarting the tt-telemetry / metrics pollers to read MMIO at off-bus endpoints drives the
            # root complex past its RAS threshold and reboots the whole host — the reboot this quiesce
            # exists to prevent. Two ways a scope is still cycling here: a mid-reset cancel, and a reset
            # TIMEOUT — run_scoped returns rc None with its scope deliberately never killed, so
            # the caller sees a normal return and cancelled_mid stays False. A launch failure also
            # returns rc None but started no reset, so ask the backend whether one is genuinely still running
            # (only on the rc-None path, so the cancel path keeps its exact no-extra-await behavior).
            restore = not cancelled_mid
            if restore and rc is None:
                restore = not await asyncio.to_thread(self.scope_active)
            if restore:
                try:
                    # A reset can leave endpoints un-enumerated — and any chip we isolated earlier is
                    # still out of the kernel, because `remove` is not undone by a reset. Rescan, or the
                    # device comes back short and nobody knows why.
                    await asyncio.to_thread(Path("/sys/bus/pci/rescan").write_text, "1")
                    await asyncio.sleep(3)
                    isolated = self._isolated_chips() if self._isolated_chips is not None else None
                    if isolated:
                        await asyncio.to_thread(drop_returned_isolated, isolated, "reset", log)
                    if rc == 0:
                        await self._reapply_aiclk_ceiling(log)
                except OSError:
                    pass
                finally:
                    # The reset is done and the chips are back, so restoring is safe; put it in the
                    # rescan's finally under shield so a cancellation during the rescan still restores
                    # rather than leaving a telemetry gap.
                    if quiesced:
                        await asyncio.shield(self._set_device_pollers(True, log))

    async def _reapply_aiclk_ceiling(self, log) -> None:
        """Put an operator's AICLK ceiling back on the freshly reset chips, before the pollers
        return and before the caller's verify runs its traffic pass. Only after a completed reset
        (the chips are back on the bus); a no-op when unconfigured. Never raises: a failure here
        leaves the ceiling owed, and the verify's probe pass retries it and fails closed. Freshly
        reset firmware may read its old limit for a few seconds after the cap is sent, so the
        apply re-reads within the settle window before it calls the ceiling unverified."""
        if not CEILING.armed():
            return
        self._set_device_op_detail("device reset: re-applying the AICLK ceiling")
        try:
            await CEILING.apply("post-reset", log=log, settle=aiclk_settle_sec())
        except Exception as e:  # noqa: BLE001 - the verify that follows owns the verdict
            log(f"aiclk-ceiling: post-reset apply raised {type(e).__name__}: {e}")

    def read_auto_recovery_ledger(self) -> Optional[list[dict]]:
        """Every auto-recovery escalation this host has taken, oldest first — the rate limiter's
        durable record. ``[]`` means none (file absent/empty); ``None`` means the file exists but
        could not be read, which the governor treats as "deny": guessing "never rebooted" from an
        unreadable ledger is exactly how a boot loop starts. A single malformed line is skipped,
        not fatal — losing the rest of the record to one bad line would blind the limiter too."""
        path = health_dir() / AUTO_RECOVERY_LEDGER
        try:
            if not path.exists():
                return []
            lines = path.read_text().splitlines()
        except OSError:
            return None
        recs = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                recs.append(json.loads(ln))
            except ValueError:
                continue
        return recs

    def boot_from_broker_escalation(self, current_boot_id: str) -> Optional[dict]:
        """The auto-recovery escalation that brought this boot up, or None if the reboot was not
        broker-initiated.

        A host reboot or BMC power cycle the broker fires records its ledger entry — stamped with the
        boot that is ending — BEFORE the box goes down. So on the next boot, a ledger whose most recent
        escalation carries a boot id OTHER than the one now running is one the box has rebooted since:
        this boot is that escalation's result, and its back-filled row should read as the action it
        actually was. None when nothing attributes this boot — an external/manual reboot or a firmware
        crash (ledger empty), an unreadable ledger, or an unknown current boot id (no 'since' to judge,
        so do not guess)."""
        if not current_boot_id:
            return None
        ledger = self.read_auto_recovery_ledger()
        if not ledger:
            return None
        last = ledger[-1]
        if last.get("action") not in AUTO_RECOVERY_RANK:
            return None
        rec_boot = last.get("boot_id")
        if not rec_boot or rec_boot == current_boot_id:
            return None
        # A different boot_id proves the box rebooted SINCE that escalation — NOT that this boot is its
        # result. Between then and now the box may have rebooted many times (external reboots, KMD
        # remediation); a stale entry would then mis-stamp every later boot as a broker auto-recovery it
        # never fired (the phantom "power cycle — broker auto-recovery (post-reboot verify…)" rows an
        # ansible-driven reboot got, 16 h after the last real escalation). The escalation is recorded just
        # before the reboot it causes completes, so require it to fall within a reboot window before THIS
        # boot started. Fail closed (no attribution) when the timestamps are unreadable — a missing
        # back-fill row is honest; a phantom one lies to triage.
        at = last.get("at_epoch")
        btime = self._boot_btime_id()
        if at is None or not btime:
            return None
        try:
            gap = float(btime) - float(at)
        except (TypeError, ValueError):
            return None
        if not (0 <= gap <= BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC):
            return None
        return last

    def _journal_auto_recovery_denied(self, action: str, why: str) -> None:
        """Make a blocked escalation VISIBLE, not a log line that scrolls past. When the ladder's host
        rung is opted in but the governor denies it, the device stays held; if the denial is a rate
        limit / exhausted cascade (not a tenant deferral), the ladder has climbed as far as it can and a
        human must look. Deduped per hold episode so a held box does not re-journal it every gate pass.
        A tenant deferral is expected (waiting for the job), so it is not flagged host_at_risk."""
        since = self._device_hold_episode_since()
        if since and since == self._auto_recovery_denied_episode:
            return
        self._auto_recovery_denied_episode = since
        tenant = why.startswith("a tenant holds")
        health_event("auto_recovery_denied", action=action, reason=why, host_at_risk=not tenant)

    def auto_recovery_allowed(
        self, action: str, *, tenant_active: bool, now_epoch: Optional[float] = None
    ) -> tuple[bool, str]:
        """Whether an auto-recovery escalation (a host reboot or a BMC power cycle) MAY fire now.

        Enforces the three limits the highest-risk rungs must never be without, independent of the
        per-host env opt-in the caller checks separately:
          * a tenant on the device is absolute — never take a box down from under a running job;
          * a wall-clock min interval, which spans the reboot/power-cycle itself and so is the guard
            against a wedge no rung can fix turning into a loop. It is PER SEVERITY: only a record of
            equal-or-greater severity gates ``action``, so escalating reboot -> power-cycle within the
            interval is allowed while re-firing the same rung, or de-escalating to a weaker one, is not;
          * a per-boot cap, so several wedges within one long-lived boot cannot escalate repeatedly.
        Returns ``(allowed, reason)``. Fails CLOSED: any doubt about the durable record denies."""
        if tenant_active:
            return False, "a tenant holds the device; a reboot would destroy a running job"
        ledger = self.read_auto_recovery_ledger()
        if ledger is None:
            return False, "auto-recovery ledger unreadable; refusing to reboot without its rate-limit record"
        now_epoch = time.time() if now_epoch is None else now_epoch
        rank = AUTO_RECOVERY_RANK.get(action, max(AUTO_RECOVERY_RANK.values()))
        # The power cycle is spaced by its OWN interval (the user's 30-min cap), every other
        # action by the general loop-guard interval. For a power cycle rank==2 gates only prior
        # power cycles, so this interval is exactly "30 min between power cycles".
        # Spec 04 I65: the power cycle's spacing is its cooldown, counted from the latest cycle or
        # boot of any cause (power_cycle_cooldown); the loop below keeps only its fail-closed read.
        cycle = action == "power-cycle"
        min_interval = AUTO_RECOVERY_MIN_INTERVAL_SEC
        for rec in ledger:
            # A weaker prior rung does not gate a stronger escalation — that is what lets a failed
            # reboot hand off to the power cycle without waiting out the interval. Equal-or-stronger
            # rungs do gate it: that is each rung's own loop guard, and the ban on de-escalation. An
            # unrecognised record counts as strongest (gates everything), symmetric with ``rank``:
            # a record we cannot classify must never fail OPEN and wave a repeat action through.
            if AUTO_RECOVERY_RANK.get(rec.get("action"), max(AUTO_RECOVERY_RANK.values())) < rank:
                continue
            # A gating record we cannot read a timestamp from is treated as "just now": the interval
            # is the loop guard, so a missing/garbled stamp must block, never wave the action through.
            try:
                at = float(rec.get("at_epoch"))
            except (TypeError, ValueError):
                return False, (
                    f"an auto-recovery record of equal-or-greater severity carries no "
                    f"usable timestamp; refusing {action} rather than risk a boot loop "
                    f"(fail closed)"
                )
            if cycle:
                continue
            since = now_epoch - at
            if since < min_interval:
                return False, (
                    f"a {rec.get('action')} auto-recovery was {int(since)}s ago (< "
                    f"{min_interval}s min): a {action} that did not fix "
                    f"the device will not fix it now — this is the boot-loop guard; "
                    f"holding {action} until {_local_time(at + min_interval)}"
                )
        if cycle:
            cooldown = self.power_cycle_cooldown(now_epoch=now_epoch, ledger=ledger)
            self.power_cycle_cooldown_state = cooldown
            if cooldown is not None:
                self.power_cycle_retry = (cooldown["until"], self._device_hold_episode_since())
                return False, cooldown["why"]
        boot_id = self._current_boot_id()
        if boot_id:
            # The per-boot cap counts this boot's requests within the action's interval only: a
            # request recorded this boot never took the box down (else this would be a new boot),
            # and holding on it for the rest of the boot was an open-ended hold (spec 04 I65). Past
            # the interval it is asked again; an unreadable stamp counts as now (fail closed).
            # A power cycle recorded this boot never took the box down: it waits the base interval
            # only, never the I67 backoff, which counts cycles that did (spec 03 I67).
            interval = POWER_CYCLE_MIN_INTERVAL_SEC if cycle else AUTO_RECOVERY_MIN_INTERVAL_SEC
            stamps = []
            for r in ledger:
                if r.get("boot_id") != boot_id:
                    continue
                try:
                    stamps.append(float(r.get("at_epoch")))
                except (TypeError, ValueError):
                    stamps.append(now_epoch)
            recent = [t for t in stamps if now_epoch - t < interval]
            if len(recent) >= AUTO_RECOVERY_MAX_PER_BOOT:
                until = max(recent) + interval
                if cycle:
                    self.power_cycle_retry = (until, self._device_hold_episode_since())
                return False, (
                    f"already auto-escalated {len(recent)}x this boot "
                    f"(cap {AUTO_RECOVERY_MAX_PER_BOOT}); holding {action} until {_local_time(until)}"
                )
        if cycle:
            self.power_cycle_retry = None
        return True, ""

    def power_cycle_attempts(self, ledger: Optional[list[dict]] = None, fault: Optional[dict] = None) -> int:
        """Broker power cycles in a row that took the host down and left the fault ``fault`` (spec 03
        I67): those after the last verified release, newest first, up to the first one recorded for a
        different fault. One recorded this boot never took the box down (requested, or the command
        did not carry it out) and is not counted. With no ``fault``, or a record that names none, every
        such cycle counts (the longer cooldown is the safe side)."""
        ledger = self.read_auto_recovery_ledger() if ledger is None else ledger
        healed = self._power_cycle_healed_at()
        boot = self._current_boot_id()
        key = _fault_key(fault)
        n = 0
        for rec in reversed(ledger or []):
            if rec.get("action") != "power-cycle":
                continue
            if not _epoch_after(rec.get("at_epoch"), healed):
                continue  # unreadable stamps read as after: counted, the longer cooldown
            if boot and rec.get("boot_id") == boot:
                continue
            rec_key = _fault_key(rec.get("fault"))
            if key is not None and rec_key is not None and rec_key != key:
                break
            n += 1
        return n

    def power_cycle_interval(self, ledger: Optional[list[dict]] = None, fault: Optional[dict] = None) -> int:
        """The power-cycle cooldown's length now: :func:`power_cycle_interval_sec` of
        :meth:`power_cycle_attempts` (spec 04 I65, spec 03 I67)."""
        return power_cycle_interval_sec(self.power_cycle_attempts(ledger, fault))

    def power_cycle_cooldown(
        self,
        *,
        now_epoch: Optional[float] = None,
        ledger: Optional[list[dict]] = None,
        fault: Optional[dict] = None,
    ) -> Optional[dict]:
        """The power-cycle cooldown in force now, or None when a cycle may fire at once (spec 04 I65).

        Counted from the latest of: the broker's own last reboot or power cycle (its ledger); this
        host's current boot, whatever caused it; and the last record of the external cycler's history
        file when one is named and readable. A stamp later than now anchors nothing. The interval is
        the I67 backoff for ``fault`` (default: the fault of the I37 ask in progress), except from a
        power cycle requested this boot that did not take the box down — recorded, or refused by the
        command — which waits the base interval only (spec 03 I67). Returns ``until``/``last``
        (epochs), ``cause``, ``interval_sec``, ``attempts``, ``fault_class``, ``not_performed`` and
        ``why``, the line the journal and /health carry."""
        now_epoch = time.time() if now_epoch is None else now_epoch
        ledger = self.read_auto_recovery_ledger() if ledger is None else ledger
        fault = self.power_cycle_fault if fault is None else fault
        attempts = self.power_cycle_attempts(ledger, fault)
        interval = power_cycle_interval_sec(attempts)
        base = POWER_CYCLE_MIN_INTERVAL_SEC
        boot = self._current_boot_id()
        anchors: list[tuple[float, str, int]] = []
        broker_last: Optional[tuple[float, str, bool]] = None
        for rec in ledger or []:
            if rec.get("action") not in AUTO_RECOVERY_RANK:
                continue
            try:
                at = float(rec.get("at_epoch"))
            except (TypeError, ValueError):
                continue
            if broker_last is None or at > broker_last[0]:
                this_boot = bool(boot) and rec.get("boot_id") == boot and rec.get("action") == "power-cycle"
                broker_last = (at, str(rec.get("action")), this_boot)
        if broker_last is not None:
            at, action, this_boot = broker_last
            anchors.append((at, f"broker {action}", base if this_boot else interval))
        try:
            btime = float(self._boot_btime_id() or "")
        except ValueError:
            btime = None
        if btime is not None:
            by_broker = (
                broker_last is not None and 0 <= btime - broker_last[0] <= BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC
            )
            anchors.append(
                (btime, f"host boot after the broker {broker_last[1]}" if by_broker else "host boot", interval)
            )
        external = _last_external_power_cycle()
        if external is not None:
            anchors.append((external[0], external[1], interval))
        not_performed = self._power_cycle_not_performed()
        if not_performed is not None:
            anchors.append((not_performed["at_epoch"], "power cycle not performed", base))
        anchors = [a for a in anchors if a[0] <= now_epoch]
        if not anchors:
            return None
        last, cause, gap = max(anchors, key=lambda a: a[0] + a[2])
        until = last + gap
        if now_epoch >= until:
            return None
        key = _fault_key(fault)
        rc_note = ""
        if not_performed is not None and cause == "power cycle not performed":
            rc_note = f", rc {not_performed.get('rc')}"
        return {
            "until": until,
            "last": last,
            "cause": cause,
            "interval_sec": gap,
            "attempts": attempts,
            "fault_class": key[0] if key else None,
            "off_bus": list(key[1]) if key else None,
            "not_performed": not_performed,
            "why": (
                f"power cycle in cooldown until {_local_time(until)} "
                f"(last cycle/boot {_local_time(last)}, {cause}{rc_note}; {gap}s cooldown"
                f"{f', {attempts} cycle(s) left this fault' if attempts else ''})"
            ),
        }

    def _power_cycle_healed_at(self) -> float:
        try:
            return float(json.loads((health_dir() / POWER_CYCLE_HEALED_FILE).read_text()).get("at_epoch", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0.0

    def _read_power_cycle_backoff(self) -> dict:
        try:
            data = json.loads((health_dir() / POWER_CYCLE_BACKOFF_FILE).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_power_cycle_backoff(self, data: dict) -> None:
        path = health_dir() / POWER_CYCLE_BACKOFF_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, default=str))
            os.replace(tmp, path)
        except OSError:
            pass

    def _power_cycle_not_performed(self) -> Optional[dict]:
        """The last power-cycle request the command did not carry out, while this boot lasts (a boot
        since then means the box went down after all), or None."""
        rec = self._read_power_cycle_backoff().get("not_performed")
        if not isinstance(rec, dict):
            return None
        boot = self._current_boot_id()
        if boot and rec.get("boot_id") not in (boot, None):
            return None
        try:
            return {**rec, "at_epoch": float(rec.get("at_epoch"))}
        except (TypeError, ValueError):
            return None

    def note_power_cycle_not_performed(self, error: BaseException, *, now_epoch: Optional[float] = None) -> None:
        """A power-cycle request the command did not carry out (non-zero rc, timeout, missing binary):
        it is no cycle for the I67 backoff (its ledger entry is retracted), it is asked again after
        the base interval, and /health shows it with the command's rc (spec 03 I67). Never raises."""
        now_epoch = time.time() if now_epoch is None else now_epoch
        m = _RC_RE.search(str(error))
        rec = {
            "at_epoch": now_epoch,
            "at": _local_time(now_epoch),
            "boot_id": self._current_boot_id(),
            "rc": int(m.group(1)) if m else None,
            "detail": repr(error)[-300:],
        }
        data = self._read_power_cycle_backoff()
        data["not_performed"] = rec
        if self.power_cycle_fault is not None:
            data["fault"] = self.power_cycle_fault
        self._write_power_cycle_backoff(data)
        cooldown = self.power_cycle_cooldown(now_epoch=now_epoch)
        self.power_cycle_cooldown_state = cooldown
        if cooldown is not None:
            self.power_cycle_retry = (cooldown["until"], self._device_hold_episode_since())

    def note_power_cycle_fault(self, fault: Optional[dict]) -> None:
        """Keep the fault of the I37 ask on disk, so a broker restart rebuilds its backoff (I67)."""
        self.power_cycle_fault = fault
        if fault is None:
            return
        data = self._read_power_cycle_backoff()
        if data.get("fault") != fault:
            data["fault"] = fault
            self._write_power_cycle_backoff(data)

    def restore_power_cycle_backoff(self, *, now_epoch: Optional[float] = None) -> Optional[dict]:
        """At broker start in a held episode: rebuild the cooldown, its deadline and the watchdog's
        retry from disk — the ledger, the boot, the heal marker and the saved fault (spec 03 I67).
        Never raises."""
        try:
            fault = self._read_power_cycle_backoff().get("fault")
            cooldown = self.power_cycle_cooldown(now_epoch=now_epoch, fault=fault if isinstance(fault, dict) else None)
        except Exception:  # noqa: BLE001 - the governor recomputes it at its next ask anyway
            return None
        self.power_cycle_cooldown_state = cooldown
        if cooldown is not None:
            self.power_cycle_retry = (cooldown["until"], self._device_hold_episode_since())
        return cooldown

    def note_mesh_verified(self, *, now_epoch: Optional[float] = None) -> None:
        """A verified release (a passing full health check, fabric included) ends any power-cycle
        backoff streak (spec 04 I65, spec 03 I67), any cooldown retry waiting and any record of a
        cycle not performed. Writes only when there is something to end. Never raises."""
        self.power_cycle_retry = None
        self.power_cycle_cooldown_state = None
        self.power_cycle_fault = None
        try:
            (health_dir() / POWER_CYCLE_BACKOFF_FILE).unlink()
        except OSError:
            pass
        ledger = self.read_auto_recovery_ledger() or []
        healed = self._power_cycle_healed_at()
        if not any(r.get("action") == "power-cycle" and _epoch_after(r.get("at_epoch"), healed) for r in ledger):
            return
        now_epoch = time.time() if now_epoch is None else now_epoch
        try:
            path = health_dir() / POWER_CYCLE_HEALED_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"at_epoch": now_epoch, "at": _local_time(now_epoch)}))
        except OSError:
            pass

    def _reboot_already_attempted(self, *, now_epoch: Optional[float] = None) -> bool:
        """True if the broker already auto-rebooted for the wedge it is still looking at: a reboot
        record from a PREVIOUS boot (this boot is the one that came back up) within the loop-guard
        interval. That is the signal a warm reboot did not clear the mesh, and only then is the more
        drastic power cycle the right rung. An unreadable ledger reads as 'not attempted' here — the
        governor is what fails closed on it; this only chooses which rung to offer the governor."""
        ledger = self.read_auto_recovery_ledger()
        if not ledger:
            return False
        boot_id = self._current_boot_id()
        if not boot_id:
            # Without a boot id we cannot tell a reboot that crossed a boot from one recorded this
            # boot that has not taken the box down yet. Fail closed: do NOT offer the power cycle, so
            # the chooser stays on the reboot, whose own interval guard still rate-limits it. Offering
            # the power cycle here would let it fire in the SAME boot as the reboot — the interval
            # (weaker-rank record) and the per-boot cap (no boot id) both no-op, so it would be the one
            # path with no loop guard at all.
            return False
        now_epoch = time.time() if now_epoch is None else now_epoch
        for rec in reversed(ledger):
            if rec.get("action") != "reboot":
                continue
            if rec.get("boot_id") == boot_id:
                continue  # a reboot recorded under THIS boot has not taken the box down yet
            try:
                at = float(rec.get("at_epoch"))
            except (TypeError, ValueError):
                continue
            if now_epoch - at < AUTO_RECOVERY_MIN_INTERVAL_SEC:
                return True
        return False

    def record_auto_recovery(self, action: str, reason: str, *, now_epoch: Optional[float] = None) -> bool:
        """Append one escalation to the durable ledger and fsync it BEFORE the action fires.

        Returns True ONLY if the record reached disk. The caller MUST NOT fire the action on False:
        this record IS the rate limiter, and a reboot the ledger did not capture is one the next
        boot cannot see was already tried — a loop. So an unwritable ledger (full disk, read-only
        rootfs) must abort the reboot, not silently proceed. Never raises."""
        rec = {
            "action": action,
            "reason": reason,
            "boot_id": self._current_boot_id(),
            "at_epoch": time.time() if now_epoch is None else now_epoch,
            "at": datetime.now().isoformat(),
        }
        if action == "power-cycle" and self.power_cycle_fault is not None:
            rec["fault"] = self.power_cycle_fault  # spec 03 I67: the backoff streak is per fault
        path = health_dir() / AUTO_RECOVERY_LEDGER
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            return False

    def retract_last_auto_recovery(self, action: str) -> None:
        """Remove the most recent ledger entry for ``action`` on THIS boot. Used ONLY when the fire it
        rate-limited failed to launch — the record would otherwise count a recovery that never happened
        and block the retry a still-wedged box needs (a power cycle whose ipmitool is missing/errored is
        the case that burned us: recorded, never fired, per-boot cap then spent). Safe: the gate is
        serialized so the entry we just wrote is the last one; if it cannot be rewritten the record
        stands (the conservative side — a spurious block, never a reboot loop). Never raises."""
        path = health_dir() / AUTO_RECOVERY_LEDGER
        boot = self._current_boot_id()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for i in range(len(lines) - 1, -1, -1):
            try:
                rec = json.loads(lines[i])
            except ValueError:
                continue
            if rec.get("action") == action and rec.get("boot_id") == boot:
                del lines[i]
                try:
                    path.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
                except OSError:
                    pass
                return

    async def _fire_recovery_escalation(
        self, action: str, *, row_owner: str, label: str, detail: str, fire, log, reason: str, pre_fire=None
    ) -> None:
        """Shared body of the two host-level recovery rungs (reboot, BMC power cycle). Records the
        escalation durably — the rate limiter — and leaves a visible jobs-list row BEFORE the action
        takes the box down, so both survive it; G6b back-fills the boot when the broker restarts. If
        the record cannot be persisted the action is ABORTED: an unrecorded escalation is one the next
        boot repeats — a loop. ``fire`` is the isolated one-liner that acts, replaced wholesale in
        tests so no test path can take the box running the suite down.

        Reports the ``stages_fired_total`` metric under the Stage name matching ``action``
        ("reboot" -> "host_reboot", "power-cycle" -> "power_cycle" — see
        ``health.recovery.stages.host_reboot``/``power_cycle``): "blocked" when the ledger write
        itself fails (nothing fired), "failed" when ``fire`` raised (attempted, did not launch),
        "ok" when it returned normally (the action was issued — a SUCCESSFUL reboot/power-cycle
        takes the box down from inside ``fire`` and never reaches here at all).

        ``pre_fire(log, reason)``, when given, runs after the record and row and just before
        ``fire`` (spec 04 I26): a bounded, blocking call that never raises and whose outcome never
        stops the fire. Running it after the record means it only runs for an action that is
        really about to happen, never for one the ledger then aborts."""
        ev = action.replace("-", "_")
        stage = _ACTION_TO_STAGE[action]
        if not self.record_auto_recovery(action, reason):
            log(
                f"{label.upper()} ABORTED: could not persist the escalation record; refusing to act "
                f"without the rate-limit entry that stops it looping"
            )
            health_event(f"auto_{ev}_aborted", reason=reason, why="ledger write failed")
            metrics.stage_fired(stage, "blocked")
            return
        # From here the host is going down: no device reset may start under it.
        self.host_action_requested = (action, time.monotonic())
        self._write_action_log(row_owner, f"{label} — {reason}", 0.0, action, None)
        health_event(f"auto_{ev}_request", reason=reason)
        log(f"{label.upper()}: {detail} ({reason}); opted in + within limits — requesting it now")
        if pre_fire is not None:
            try:
                hook = await asyncio.to_thread(pre_fire, log, reason)
            except asyncio.CancelledError:
                # Stopped while waiting on the hook (broker shutdown): the action never fired, so
                # its ledger entry must not spend the retry the still-wedged box needs.
                self.retract_last_auto_recovery(action)
                self.host_action_requested = None
                raise
            except Exception as exc:  # noqa: BLE001 - the hook never gates recovery
                hook = {"ran": False, "detail": f"failed: {exc!r}"}
                log(f"{label.upper()}: pre-{action} hook failed ({exc!r}); going ahead")
            if hook and (hook.get("ran") or hook.get("detail")):
                health_event(
                    f"pre_{ev}_hook",
                    reason=reason,
                    rc=hook.get("rc"),
                    timed_out=hook.get("timed_out", False),
                    seconds=hook.get("seconds"),
                    detail=str(hook.get("detail", ""))[-500:],
                )
        try:
            await asyncio.to_thread(fire)
        except Exception as exc:  # noqa: BLE001 - a fire that did not launch must not leave a poisoned limiter
            # A SUCCESSFUL fire takes the box down inside asyncio.to_thread, so reaching here means the
            # action never launched — the binary is missing, exited non-zero, or timed out. The ledger
            # entry written above to rate-limit it would now count a recovery that never happened and
            # block the retry the still-wedged box needs, so retract it and surface loudly.
            self.retract_last_auto_recovery(action)
            self.host_action_requested = None
            log(
                f"{label.upper()} FAILED TO LAUNCH: {exc!r}; retracted the rate-limit entry so the rung "
                f"can retry once the cause is fixed"
            )
            health_event(f"auto_{ev}_failed", reason=reason, error=repr(exc))
            metrics.stage_fired(stage, "failed")
            if action == "power-cycle":
                # Spec 03 I67: not a cycle for the backoff; asked again after the base interval.
                self.note_power_cycle_not_performed(exc)
                log(f"POWER CYCLE NOT PERFORMED: {exc!r}; asking again at the end of the base interval")
            return
        # The pending window counts from the hand-off, not from before a slow pre-fire hook.
        self.host_action_requested = (action, time.monotonic())
        metrics.stage_fired(stage, "ok")
