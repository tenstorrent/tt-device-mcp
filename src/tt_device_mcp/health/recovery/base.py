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
import logging
import math
import os
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

_logger = logging.getLogger("tt-device-mcp")

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
# 1800s (30 min); TT_DEVICE_MCP_POWER_CYCLE_MIN_INTERVAL_SEC overrides.
POWER_CYCLE_MIN_INTERVAL_SEC = int(os.environ.get("TT_DEVICE_MCP_POWER_CYCLE_MIN_INTERVAL_SEC", "1800"))
# Spec 04 I65: that interval is a cooldown counted from the LATEST power cycle or boot of this host,
# whoever caused it — the broker's own reboot or power cycle (the ledger), the host's current boot
# (a manual, datacenter or external cycle shows only there), and an external cycler's history file
# when one is named and readable. After two consecutive broker power cycles no heal followed, it
# doubles for each further one, capped here; a verified release (a passing full health check)
# resets the streak.
log = logging.getLogger(__name__)
POWER_CYCLE_BACKOFF_CAP_SEC = 4 * 3600
# A JSON-lines file an external power cycler appends to (``at_epoch``, optional ``cause``); read only.
POWER_CYCLE_HISTORY_FILE_ENV = "TT_DEVICE_MCP_POWER_CYCLE_HISTORY_FILE"
POWER_CYCLE_HEALED_FILE = "power_cycle_healed.json"
# Used when the env var is unset: this file in the broker's state dir (the parent of the health dir,
# /var/lib/tt-device-broker on a system broker).
POWER_CYCLE_HISTORY_DEFAULT_FILE = "power_cycle_history.jsonl"


def power_cycle_history_path() -> Path:
    """The out-of-band power-cycle log: the env var when set, else ``power_cycle_history.jsonl`` in the
    broker's state dir (spec 04 I65)."""
    name = os.environ.get(POWER_CYCLE_HISTORY_FILE_ENV, "").strip()
    return Path(name) if name else health_dir().parent / POWER_CYCLE_HISTORY_DEFAULT_FILE


def power_cycle_interval_sec(failed_cycles: int) -> int:
    """The cooldown after ``failed_cycles`` consecutive broker power cycles no heal followed: the
    base interval for 0 or 1, then doubled for each further one, never above the cap (spec 04 I65)."""
    base = POWER_CYCLE_MIN_INTERVAL_SEC
    if failed_cycles < 2:
        return base
    return max(base, min(base * 2 ** (failed_cycles - 1), POWER_CYCLE_BACKOFF_CAP_SEC))


def _local_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(sep=" ", timespec="seconds")


def _epoch(value) -> Optional[float]:
    """``value`` as a finite epoch, or None (missing, garbage, NaN or infinite)."""
    try:
        at = float(value)
    except (TypeError, ValueError):
        return None
    return at if math.isfinite(at) else None


def _past_epoch(value, now: float) -> Optional[float]:
    """``value`` as a finite epoch no later than ``now``, or None. A stamp later than now (a clock that
    disagrees) anchors nothing in the I65 cooldown: taken as is it would hold a cycle past the stated
    gaps, and as the max it would hide every valid record before it."""
    at = _epoch(value)
    return at if at is not None and at <= now else None


def _epoch_after(value, than: float, now: float) -> bool:
    """A record stamped after ``than`` and not later than ``now``; an unreadable stamp counts."""
    at = _epoch(value)
    return True if at is None else than < at <= now


POWER_CYCLE_FAILED_SUFFIX = " (cycle failed)"


def _last_external_power_cycle(now_epoch: float) -> Optional[tuple[float, str]]:
    """The latest ``at_epoch`` in the external cycler's history file no later than ``now_epoch``, and
    its cause, or None when the file (env var, else the state dir default) is missing or cannot be read (the host's boot time still covers
    any cycle that took the box down). Malformed, non-finite and future lines are skipped."""
    path = power_cycle_history_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    recs: list[tuple[float, str]] = []
    warned = False
    for ln in lines:
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
            at = _past_epoch(rec.get("at_epoch"), now_epoch)
        except (ValueError, TypeError, AttributeError):
            if not warned:
                warned = True
                log.warning("power-cycle history %s: skipping malformed line(s)", path)
            continue
        if at is not None:
            recs.append((at, str(rec.get("cause") or "external power cycle")))
    # A failed cycle is logged as a second line with the same at_epoch and the attempt's cause plus
    # POWER_CYCLE_FAILED_SUFFIX. It never counts, and it cancels the attempt line it matches; an
    # unmatched failure line is ignored, and an attempt with no failure line still counts.
    failed = {(at, c[: -len(POWER_CYCLE_FAILED_SUFFIX)]) for at, c in recs if c.endswith(POWER_CYCLE_FAILED_SUFFIX)}
    best: Optional[tuple[float, str]] = None
    for at, c in recs:
        if c.endswith(POWER_CYCLE_FAILED_SUFFIX) or (at, c) in failed:
            continue
        if best is None or at > best[0]:
            best = (at, c)
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


# Spec 04 I68: a power-cycle command may exit 0 once the cycle is accepted and fail after that, so
# the broker confirms each cycle by the boot id. The request is kept in this state-dir file (it
# survives a broker restart); if the boot id has not changed this long after the hand-off, the cycle
# reads as unconfirmed: it is logged at error level, it counts as no cycle for the cooldown, and it
# is re-fired once at once. Each later unconfirmed attempt waits a stated backoff (1 h, 2 h, then
# 4 h) before the next. The device stays held throughout. TT_DEVICE_MCP_POWER_CYCLE_CONFIRM_SEC
# overrides the bound; garbage or non-positive reads as the default, and it is capped.
POWER_CYCLE_CONFIRM_FILE = "power_cycle_confirm.json"
POWER_CYCLE_CONFIRM_DEFAULT_SEC = 900.0
POWER_CYCLE_CONFIRM_MAX_SEC = 3600.0
POWER_CYCLE_UNCONFIRMED_BACKOFF_BASE_SEC = 3600


def power_cycle_confirm_sec() -> float:
    """How long after a power cycle's hand-off the boot id must have changed. Read per call."""
    raw = os.environ.get("TT_DEVICE_MCP_POWER_CYCLE_CONFIRM_SEC", "").strip()
    try:
        v = float(raw)
    except ValueError:
        return POWER_CYCLE_CONFIRM_DEFAULT_SEC
    if not 0 < v < float("inf"):
        return POWER_CYCLE_CONFIRM_DEFAULT_SEC
    return min(v, POWER_CYCLE_CONFIRM_MAX_SEC)


def power_cycle_unconfirmed_backoff_sec(unconfirmed: int) -> int:
    """The wait before the next power cycle after ``unconfirmed`` cycles in a row that the host did not
    act on (spec 04 I68): none after the first (the one re-fire), then 1 h, 2 h, and 4 h at most."""
    if unconfirmed < 2:
        return 0
    return min(POWER_CYCLE_UNCONFIRMED_BACKOFF_BASE_SEC * 2 ** (unconfirmed - 2), POWER_CYCLE_BACKOFF_CAP_SEC)


class HostActionTimedOut(RuntimeError):
    """A host reboot / power-cycle command that timed out rather than exited non-zero. It may have
    launched its detached cycler before it stopped answering, so the request stays pending (with its
    normal bound) instead of reading as "never launched"."""


# The host-level recovery rungs, ordered by severity. A warm reboot recovers most wedges; a
# whole-bus wedge survives it and only the chassis power cycle clears it (measured on box D).
# The min-interval loop guard is therefore PER SEVERITY: a stronger rung may escalate past a
# weaker one that just fired — a reboot that did not fix the mesh must not force an hour's wait
# before the power cycle that will — but no rung may re-fire itself inside the interval (its own
# loop guard), and a stronger rung that fired blocks de-escalating back to a weaker one.
AUTO_RECOVERY_RANK = {"reboot": 1, "power-cycle": 2}

# A power cycle whose following boot reached a VERIFIED-healthy release (a gate pass whose fabric
# check ran and passed) recovered the box: a new fault after it is a new episode, not a boot loop, so
# that cycle gates no later rung — neither the next cycle nor a reboot (spec 04 I9, I10). The stamp is
# durable beside the ledger. Only a cycle no later boot ever verified still gates — the true loop. The
# hard backstop below counts every power cycle, verified or not: past it the broker pages instead of
# cycling a flapping tray on a serving host forever, and every cycle in the window gates the reboot
# again as an unverified one would.
AUTO_RECOVERY_VERIFIED = "auto_recovery_verified.json"
POWER_CYCLE_BACKSTOP_MAX = 3
POWER_CYCLE_BACKSTOP_WINDOW_SEC = 7200

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


def _later_boot_verified(boot_id, at: Optional[float], verified: Optional[dict], now: float) -> bool:
    """Whether the verified-healthy release ``verified`` came on a boot other than ``boot_id`` and
    after ``at``: the box came up from that event and proved itself healthy. Any unknown boot id, or
    a missing, non-finite or future stamp, reads False: the event still gates."""
    if not verified or at is None:
        return False
    ver_boot = verified.get("boot_id")
    ver_at = _past_epoch(verified.get("at_epoch"), now)
    if not boot_id or not ver_boot or boot_id == ver_boot or ver_at is None:
        return False
    return ver_at > at


def _verified_since(rec: dict, verified: dict, now: float) -> bool:
    """Whether power-cycle ledger row ``rec`` was followed, on a LATER boot, by the verified-healthy
    release ``verified``. Any unknown boot id or unreadable stamp reads False: the row still gates."""
    if rec.get("action") != "power-cycle":
        return False
    return _later_boot_verified(rec.get("boot_id"), _epoch(rec.get("at_epoch")), verified, now)


def _power_cycle_backstop_stamps(ledger: list[dict], now_epoch: float) -> list[float]:
    """The times of every power cycle, verified or not, inside the backstop window. A row with no
    readable stamp, or one later than now, is left out here; the interval loop fails closed on it."""
    stamps = []
    for rec in ledger:
        if rec.get("action") != "power-cycle":
            continue
        at = _past_epoch(rec.get("at_epoch"), now_epoch)
        if at is not None and now_epoch - at < POWER_CYCLE_BACKSTOP_WINDOW_SEC:
            stamps.append(at)
    return sorted(stamps)


def _power_cycle_backstop_until(ledger: list[dict], now_epoch: float) -> Optional[float]:
    """The epoch at which the backstop allows a power cycle again, or None when it is under the cap.
    That is when enough of the oldest cycles left the window to bring the count below the cap."""
    stamps = _power_cycle_backstop_stamps(ledger, now_epoch)
    if len(stamps) < POWER_CYCLE_BACKSTOP_MAX:
        return None
    return stamps[-POWER_CYCLE_BACKSTOP_MAX] + POWER_CYCLE_BACKSTOP_WINDOW_SEC


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
        # The newest power-cycle stamp the backstop already paged for, so a held box pages once per
        # cap reached, not on every gate pass.
        self._power_cycle_backstop_paged: float = 0.0
        self._power_cycle_backstop_logged: float = 0.0

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
        # The episode whose sweep verify still read a fabric 77. With no known no-reset cause it climbs to
        # the host rung once (unverified_77_host_rung_since), then hands the hold to an operator.
        self.unverified_sweep_77_since: Optional[str] = None
        # The episode whose 77 already climbed to a host reboot or power cycle (spec 04 I29).
        self.unverified_77_host_rung_since: Optional[str] = None
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
        # One early forced retry per hold episode after a failed mesh reset that brought chips back
        # (spec 04 I39): (episode, monotonic due time), and the episode that already spent it.
        self.progress_retry: Optional[tuple] = None
        self.progress_retry_spent_since: str = ""
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
        # The fault class the next power cycle is for (spec 04 I68's confirm record and /health); set
        # by the rung that asks for it, read once by the fire.
        self.power_cycle_fault_class = ""
        # The request a device reset was last refused for (one log line per request, spec 04 I34).
        self._host_action_reset_refused: Optional[tuple[str, float]] = None

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
            health_event("host_action_request_expired", action=action, bound_sec=int(host_action_pending_sec()))
            return None
        return action, age

    def host_action_blocks_reset(self, log: Callable[[str], None], *, context: str) -> bool:
        """True while a requested host reboot or power cycle is pending: no device reset (bridge,
        tray re-power, galaxy/SMI reset) may start under it, whichever path asks (spec 04 I34). One
        log line and one event per request; the device stays held, and the full health check runs
        after the host comes back. Bounded by host_action_pending()'s window."""
        pending = self.host_action_pending()
        if pending is None:
            return False
        action, age = pending
        req = self.host_action_requested
        if self._host_action_reset_refused != req:
            self._host_action_reset_refused = req
            bound = int(host_action_pending_sec())
            health_event(
                "reset_suppressed_host_action", action=action, age_sec=int(age), bound_sec=bound, context=context
            )
            log(
                f"{context}: no device reset — a host {action} was requested {int(age)}s ago and is pending "
                f"(up to {bound}s); the device stays held until the host comes back and passes the full check"
            )
        return True

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

    def note_progress_retry(self, hold_since: str, delay_sec: float) -> bool:
        """Arm this episode's one early retry after a failed mesh reset whose off-bus set shrank (spec 04
        I39). False when the episode has no clock or already spent its retry."""
        if not hold_since or self.progress_retry_spent_since == hold_since:
            return False
        self.progress_retry = (hold_since, time.monotonic() + delay_sec)
        return True

    def progress_retry_due(self, hold_since: str, now: Optional[float] = None) -> bool:
        """True once this episode's armed early retry is due (spec 04 I39)."""
        armed = self.progress_retry
        if not hold_since or armed is None or armed[0] != hold_since:
            return False
        return (time.monotonic() if now is None else now) >= armed[1]

    def spend_progress_retry(self) -> None:
        """The early retry started: it is spent for its episode and never re-armed in it."""
        if self.progress_retry is not None:
            self.progress_retry_spent_since = self.progress_retry[0]
        self.progress_retry = None

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
        CEILING.mark_owed(f"adopted {unit}")
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
            interval is allowed while re-firing the same rung, or de-escalating to a weaker one, is not.
            A power cycle a later boot verified healthy gates nothing, below the backstop (spec 04 I10);
          * a per-boot cap, so several wedges within one long-lived boot cannot escalate repeatedly.
        Returns ``(allowed, reason)``. Fails CLOSED: any doubt about the durable record denies."""
        if tenant_active:
            return False, "a tenant holds the device; a reboot would destroy a running job"
        if action == "power-cycle":
            # Spec 04 I68: a cycle asked for this boot waits its confirm bound; one the host did not
            # act on is retracted here (it caused no boot), so the cooldown below never counts it.
            # Its own clock read: ``now_epoch`` below is taken after the ledger read, as before.
            at = time.time() if now_epoch is None else now_epoch
            wait = self._power_cycle_confirm_wait(self.settle_power_cycle_confirm(now_epoch=at), at)
            if wait is not None:
                return self._deny_power_cycle(wait)
        ledger = self.read_auto_recovery_ledger()
        if ledger is None:
            return False, "auto-recovery ledger unreadable; refusing to reboot without its rate-limit record"
        now_epoch = time.time() if now_epoch is None else now_epoch
        # A cycle a later boot verified healthy gates no rung, the reboot included: the fault after
        # it is a new episode, and the reboot the chooser picks for it is the shortest path, not a
        # de-escalation inside a loop (spec 04 I9/I10). Past the backstop no cycle counts as proof.
        verified = self.last_verified_release()
        if action == "power-cycle":
            backstop = self._power_cycle_backstop(ledger, now_epoch)
            if backstop is not None:
                return self._deny_power_cycle(backstop)
        elif verified and _power_cycle_backstop_until(ledger, now_epoch) is not None:
            verified = None
        rank = AUTO_RECOVERY_RANK.get(action, max(AUTO_RECOVERY_RANK.values()))
        # Spec 04 I65: the power cycle's spacing is its cooldown, counted from the latest cycle or
        # boot of any cause that no verified release followed (power_cycle_cooldown); the loop below
        # keeps only its fail-closed read for it. Every other action keeps the general loop guard.
        cycle = action == "power-cycle"
        min_interval = AUTO_RECOVERY_MIN_INTERVAL_SEC
        boot_start = _past_epoch(self._boot_btime_id() or None, now_epoch)
        interval_why = ""
        interval_ends = 0.0
        for rec in ledger:
            # A weaker prior rung does not gate a stronger escalation — that is what lets a failed
            # reboot hand off to the power cycle without waiting out the interval. Equal-or-stronger
            # rungs do gate it: that is each rung's own loop guard, and the ban on de-escalation. An
            # unrecognised record counts as strongest (gates everything), symmetric with ``rank``:
            # a record we cannot classify must never fail OPEN and wave a repeat action through.
            if AUTO_RECOVERY_RANK.get(rec.get("action"), max(AUTO_RECOVERY_RANK.values())) < rank:
                continue
            if verified and _verified_since(rec, verified, now_epoch):
                continue  # a later boot proved that cycle healed the box: not a loop
            # A gating record we cannot read a timestamp from is treated as "just now": the interval
            # is the loop guard, so a missing/garbled stamp must block, never wave the action through.
            # The power cycle's one exception (spec 04 I65): with the boot's start known, such a
            # record is bounded without it, by the host-boot anchor (a cycle that went down) or the
            # per-boot cap below (one this boot), so it never refuses the rung for good.
            if cycle and boot_start is not None:
                continue
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
            if since < min_interval and at + min_interval > interval_ends:
                # The prior action may well have recovered the device and a new fault followed, so the
                # text says only what is known: when it ran and when this one may.
                interval_ends = at + min_interval
                interval_why = (
                    f"a {rec.get('action')} auto-recovery was {int(since)}s ago (< "
                    f"{min_interval}s min): no {action} for {int(min_interval - since) + 1}s more, "
                    f"until {_local_time(at + min_interval)} — this is the boot-loop guard"
                )
        if interval_why:
            return False, interval_why
        if cycle:
            cooldown = self.power_cycle_cooldown(now_epoch=now_epoch, ledger=ledger)
            if cooldown is not None:
                return self._deny_power_cycle(cooldown)
        boot_id = self._current_boot_id()
        if boot_id:
            # The per-boot cap counts this boot's requests within the action's interval only: a
            # request recorded this boot never took the box down (else this would be a new boot),
            # and holding on it for the rest of the boot was an open-ended hold (spec 04 I65). Past
            # the interval it is asked again. An unreadable stamp counts as the boot's start, a fixed
            # end (as now only when that is unknown, fail closed). For the power cycle a stamp later
            # than now counts as nothing: the cooldown already runs from the host boot (I65).
            interval = (
                self.power_cycle_interval(ledger, now_epoch=now_epoch) if cycle else AUTO_RECOVERY_MIN_INTERVAL_SEC
            )
            stamps = []
            for r in ledger:
                if r.get("boot_id") != boot_id:
                    continue
                at = _epoch(r.get("at_epoch"))
                if at is None:
                    at = boot_start if boot_start is not None else now_epoch
                elif cycle and at > now_epoch:
                    continue
                stamps.append(at)
            recent = [t for t in stamps if now_epoch - t < interval]
            if len(recent) >= AUTO_RECOVERY_MAX_PER_BOOT:
                until = max(recent) + interval
                why = (
                    f"already auto-escalated {len(recent)}x this boot "
                    f"(cap {AUTO_RECOVERY_MAX_PER_BOOT}); holding {action} until {_local_time(until)}"
                )
                if cycle:
                    return self._deny_power_cycle(
                        {
                            "until": until,
                            "last": max(recent),
                            "cause": "broker request this boot",
                            "interval_sec": interval,
                            "why": why,
                        }
                    )
                return False, why
        if cycle:
            self.power_cycle_retry = None
            self.power_cycle_cooldown_state = None
        return True, ""

    def power_cycle_interval(self, ledger: Optional[list[dict]] = None, *, now_epoch: Optional[float] = None) -> int:
        """The power-cycle cooldown's length now: :func:`power_cycle_interval_sec` of the broker power
        cycles recorded since the last verified release (spec 04 I65). A record stamped later than now
        is not counted, so it cannot outlive a verified release."""
        now_epoch = time.time() if now_epoch is None else now_epoch
        ledger = self.read_auto_recovery_ledger() if ledger is None else ledger
        healed = self._power_cycle_healed_at(now_epoch)
        failed = 0
        for rec in ledger or []:
            # An unreadable stamp counts: the longer (still capped) cooldown is the safe side.
            if rec.get("action") == "power-cycle" and _epoch_after(rec.get("at_epoch"), healed, now_epoch):
                failed += 1
        return power_cycle_interval_sec(failed)

    def power_cycle_cooldown(
        self, *, now_epoch: Optional[float] = None, ledger: Optional[list[dict]] = None
    ) -> Optional[dict]:
        """The power-cycle cooldown in force now, or None when a cycle may fire at once (spec 04 I65).

        Counted from the latest of: the broker's own last reboot or power cycle (its ledger); this
        host's current boot, whatever caused it; and the last record of the external cycler's history
        file when one is named and readable. A stamp later than now (a clock that disagrees) anchors
        nothing, and neither does a non-finite or unreadable one; each source's anchor is its latest valid
        past record. Returns ``until``/``last`` (epochs), ``cause``, ``interval_sec`` and ``why``, the
        line the journal and /health carry."""
        now_epoch = time.time() if now_epoch is None else now_epoch
        ledger = self.read_auto_recovery_ledger() if ledger is None else ledger
        # A cycle or boot a verified-healthy release (a gate pass whose fabric check ran and passed)
        # followed recovered the box: a new fault after it is a new episode, not a boot loop, so it
        # anchors nothing (spec 04 I9). The backstop still counts every broker cycle.
        verified = self.last_verified_release()
        verified_at = _past_epoch((verified or {}).get("at_epoch"), now_epoch)
        boot_id = self._current_boot_id()
        verified_this_boot = verified_at is not None and bool(boot_id) and (verified or {}).get("boot_id") == boot_id
        anchors: list[tuple[float, str]] = []
        broker_last: Optional[tuple[float, str]] = None
        for rec in ledger or []:
            if rec.get("action") not in AUTO_RECOVERY_RANK:
                continue
            at = _past_epoch(rec.get("at_epoch"), now_epoch)
            if at is None or _later_boot_verified(rec.get("boot_id"), at, verified, now_epoch):
                continue
            if broker_last is None or at > broker_last[0]:
                broker_last = (at, str(rec.get("action")))
        if broker_last is not None:
            anchors.append((broker_last[0], f"broker {broker_last[1]}"))
        btime = _past_epoch(self._boot_btime_id() or None, now_epoch)
        if btime is not None and not (verified_this_boot and verified_at > btime):
            by_broker = (
                broker_last is not None and 0 <= btime - broker_last[0] <= BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC
            )
            anchors.append((btime, f"host boot after the broker {broker_last[1]}" if by_broker else "host boot"))
        external = _last_external_power_cycle(now_epoch)
        unconfirmed_since = self._power_cycle_unconfirmed_since()
        if external is not None and unconfirmed_since is not None and external[0] >= unconfirmed_since:
            external = None  # the external cycler's stamp for a cycle that caused no boot (spec 04 I68)
        if external is not None and not (verified_at is not None and verified_at > external[0]):
            anchors.append(external)
        if not anchors:
            return None
        last, cause = max(anchors, key=lambda a: a[0])
        interval = self.power_cycle_interval(ledger, now_epoch=now_epoch)
        until = last + interval
        if now_epoch >= until:
            return None
        return {
            "until": until,
            "last": last,
            "cause": cause,
            "interval_sec": interval,
            "why": (
                f"power cycle in cooldown until {_local_time(until)}, {int(until - now_epoch) + 1}s more "
                f"(last cycle/boot {_local_time(last)}, {cause}; {interval}s cooldown) — this is the "
                f"boot-loop guard"
            ),
        }

    def _power_cycle_healed_at(self, now_epoch: float) -> float:
        """When the last verified release ended the streak; 0.0 (no heal: every cycle counts, the
        longer capped cooldown) when unknown, unreadable or later than now."""
        try:
            at = json.loads((health_dir() / POWER_CYCLE_HEALED_FILE).read_text()).get("at_epoch", 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return 0.0
        return _past_epoch(at, now_epoch) or 0.0

    def note_mesh_verified(self, *, now_epoch: Optional[float] = None) -> None:
        """A verified release (a passing full health check, fabric included) ends any power-cycle
        backoff streak (spec 04 I65) and any cooldown retry waiting. Writes only when a broker power
        cycle is recorded after the last heal. Never raises."""
        self.power_cycle_retry = None
        self.power_cycle_cooldown_state = None
        now_epoch = time.time() if now_epoch is None else now_epoch
        ledger = self.read_auto_recovery_ledger() or []
        healed = self._power_cycle_healed_at(now_epoch)
        if not any(
            r.get("action") == "power-cycle" and _epoch_after(r.get("at_epoch"), healed, now_epoch) for r in ledger
        ):
            return
        try:
            path = health_dir() / POWER_CYCLE_HEALED_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"at_epoch": now_epoch, "at": _local_time(now_epoch)}))
        except OSError:
            pass

    def _deny_power_cycle(self, state: dict) -> tuple[bool, str]:
        """Refuse the power cycle for a stated, bounded wait (spec 04 I65): ``state`` (``until``,
        ``last``, ``cause``, ``interval_sec``, ``why``) is what /health shows, and the hold watchdog
        asks again at ``until`` in this hold episode."""
        self.power_cycle_cooldown_state = state
        self.power_cycle_retry = (state["until"], self._device_hold_episode_since() or "")
        return False, state["why"]

    def power_cycle_retry_due(self, now_epoch: Optional[float] = None) -> bool:
        """True once a power cycle the cooldown (or the cap or backstop) refused may be asked again, in
        the hold episode that was refused. The watchdog then forces one ladder pass; the governor still
        decides."""
        retry = self.power_cycle_retry
        if retry is None:
            return False
        until, episode = retry
        if not episode or episode != (self._device_hold_episode_since() or ""):
            return False
        return (time.time() if now_epoch is None else now_epoch) >= until

    def clear_power_cycle_retry(self) -> None:
        self.power_cycle_retry = None

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

    def _power_cycle_backstop(self, ledger: list[dict], now_epoch: float) -> Optional[dict]:
        """The refusal when POWER_CYCLE_BACKSTOP_MAX power cycles already ran inside the backstop
        window, verified or not; None otherwise. Journals and pages once per cap reached: a tray that
        keeps dropping after cycles is a hardware fault for its owners. The page is retried on later
        passes until the alert hook takes it. The hold is bounded all the same (spec 04 I65):
        ``until`` is when the oldest counted cycle leaves the window, and the cycle is asked again
        then. A missing, non-finite or future stamp counts as nothing here (the interval loop fails
        closed on an unreadable one), so no corrupt record can hold the cycle for good."""
        until = _power_cycle_backstop_until(ledger, now_epoch)
        if until is None:
            return None
        stamps = _power_cycle_backstop_stamps(ledger, now_epoch)
        why = (
            f"{len(stamps)} power cycles, verified or not, in the last {POWER_CYCLE_BACKSTOP_WINDOW_SEC}s "
            f"(backstop {POWER_CYCLE_BACKSTOP_MAX}): paging an operator and holding the cycle until "
            f"{_local_time(until)} (epoch {int(until)}), {int(until - now_epoch) + 1}s more, once the "
            f"oldest leaves the window"
        )
        newest = stamps[-1]
        if newest != self._power_cycle_backstop_logged:
            self._power_cycle_backstop_logged = newest
            health_event(
                "power_cycle_backstop", count=len(stamps), reason=why, allowed_at_epoch=until, host_at_risk=True
            )
        if newest != self._power_cycle_backstop_paged:
            event = {"kind": "power_cycle_backstop", "count": len(stamps), "message": why}
            if alert.send_alert(event) is not None or alert.alert_argv() is None:
                self._power_cycle_backstop_paged = newest
        return {
            "until": until,
            "last": newest,
            "cause": "power-cycle backstop",
            "interval_sec": POWER_CYCLE_BACKSTOP_WINDOW_SEC,
            "why": why,
        }

    def last_verified_release(self) -> Optional[dict]:
        """The last verified-healthy release (``boot_id``, ``at_epoch``), or None when none was
        recorded or it cannot be read — read as "never verified", so the guard keeps every cycle."""
        try:
            rec = json.loads((health_dir() / AUTO_RECOVERY_VERIFIED).read_text())
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) else None

    def power_cycle_healed(self, rec: dict) -> bool:
        """Whether power cycle ``rec`` (``boot_id``, ``at_epoch``) was followed, on a later boot, by
        the verified-healthy release on record: it healed the box, so a fault after it is a new
        episode (spec 04 I9). No stamp, no boot id or an unreadable field reads False. A cycle asked
        for on this boot whose confirm bound has passed caused no boot, so it counts as no cycle and
        reads True too (spec 04 I68)."""
        boot_id = self._current_boot_id()
        at = _epoch(rec.get("at_epoch"))
        if boot_id and rec.get("boot_id") == boot_id and at is not None:
            if time.time() - at >= power_cycle_confirm_sec():
                return True
        verified = self.last_verified_release()
        return bool(verified) and _verified_since({**rec, "action": "power-cycle"}, verified, time.time())

    def record_verified_release(self, via: str, *, now_epoch: Optional[float] = None) -> None:
        """Stamp a verified-healthy release (a gate pass whose fabric check ran and passed) on this
        boot. Written atomically; never raises. A lost stamp only keeps the boot-loop guard on. The
        first stamp of a boot is the one that counts (every cycle it clears was recorded under an
        earlier boot), so later passes on the same boot write nothing."""
        boot_id = self._current_boot_id()
        if not boot_id or (self.last_verified_release() or {}).get("boot_id") == boot_id:
            return
        rec = {
            "boot_id": boot_id,
            "at_epoch": time.time() if now_epoch is None else now_epoch,
            "via": via,
        }
        path = health_dir() / AUTO_RECOVERY_VERIFIED
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(rec, default=str))
            os.replace(tmp, path)
        except OSError:
            pass

    # -- spec 04 I68: each power cycle confirmed by the boot id --------------------------------------

    def read_power_cycle_confirm(self) -> Optional[dict]:
        """The pending power-cycle confirm record, or None (none, or unreadable)."""
        try:
            rec = json.loads((health_dir() / POWER_CYCLE_CONFIRM_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return rec if isinstance(rec, dict) else None

    def _write_power_cycle_confirm(self, rec: Optional[dict]) -> None:
        """Write ``rec`` atomically and fsync it, or remove the file for None. Never raises: a lost
        record only falls back to the I65 cooldown, which still bounds the next ask."""
        path = health_dir() / POWER_CYCLE_CONFIRM_FILE
        try:
            if rec is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            pass

    def _note_power_cycle_request(self, fault_class: str, reason: str, now_epoch: float) -> Optional[dict]:
        """Record, before the fire, that a power cycle is asked for on this boot; returns the record it
        replaced (restored if the fire does not launch). Without a boot id nothing can be confirmed,
        so nothing is written and the I65 cooldown alone bounds the next ask."""
        prev = self.read_power_cycle_confirm()
        boot_id = self._current_boot_id()
        if not boot_id:
            return prev
        rec = dict(prev) if prev and prev.get("boot_id") == boot_id else {"boot_id": boot_id, "unconfirmed": 0}
        rec.setdefault("first_epoch", now_epoch)
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["requests"] = (list(rec.get("requests") or []) + [now_epoch])[-8:]
        rec.update(
            state="awaiting_reboot",
            fault_class=fault_class,
            reason=reason,
            hold_since=self._device_hold_episode_since() or "",
            next_attempt_epoch=None,
        )
        self._stamp_power_cycle_request(rec, now_epoch)
        return prev

    def _stamp_power_cycle_request(self, rec: dict, now_epoch: float) -> None:
        bound = power_cycle_confirm_sec()
        rec.update(
            requested_at_epoch=now_epoch,
            requested_at=_local_time(now_epoch),
            deadline_epoch=now_epoch + bound,
            bound_sec=int(bound),
        )
        self._write_power_cycle_confirm(rec)
        # The hold watchdog asks again at the bound, so an unconfirmed cycle is re-fired on time.
        self.power_cycle_retry = (now_epoch + bound, self._device_hold_episode_since() or "")

    def settle_power_cycle_confirm(self, *, now_epoch: Optional[float] = None) -> Optional[dict]:
        """Settle the confirm record against the boot id now; returns it while it still applies to
        this boot, else None. A new boot id confirms the cycle and clears the record (the startup full
        health check, fabric included, then gates release as on every boot). The same boot id past
        the bound marks it unconfirmed: logged at error level and paged, this boot's power-cycle
        ledger records are retracted (they caused no boot, so they start no cooldown, cap or
        backstop), and the next attempt is due after :func:`power_cycle_unconfirmed_backoff_sec`.
        A stamp later than the bound allows (a clock that disagrees) is clamped to it."""
        rec = self.read_power_cycle_confirm()
        if rec is None:
            return None
        boot_id = self._current_boot_id()
        if not boot_id:
            return None
        if rec.get("boot_id") != boot_id:
            health_event(
                "power_cycle_confirmed",
                fault_class=rec.get("fault_class", ""),
                attempts=rec.get("attempts"),
                requested_at=rec.get("requested_at", ""),
                boot_id=boot_id,
            )
            self._write_power_cycle_confirm(None)
            return None
        now_epoch = time.time() if now_epoch is None else now_epoch
        bound = power_cycle_confirm_sec()
        deadline = _epoch(rec.get("deadline_epoch"))
        if deadline is None or deadline > now_epoch + bound:
            rec["deadline_epoch"] = deadline = now_epoch + bound
            self._write_power_cycle_confirm(rec)
        if rec.get("state") == "unconfirmed":
            nxt = _epoch(rec.get("next_attempt_epoch"))
            if nxt is None or nxt > now_epoch + POWER_CYCLE_BACKOFF_CAP_SEC:
                rec["next_attempt_epoch"] = now_epoch + POWER_CYCLE_BACKOFF_CAP_SEC
                self._write_power_cycle_confirm(rec)
            return rec
        if now_epoch < deadline:
            return rec
        unconfirmed = int(rec.get("unconfirmed") or 0) + 1
        backoff = power_cycle_unconfirmed_backoff_sec(unconfirmed)
        rec.update(state="unconfirmed", unconfirmed=unconfirmed, next_attempt_epoch=now_epoch + backoff)
        self._write_power_cycle_confirm(rec)
        first = _epoch(rec.get("first_epoch"))
        self._retract_power_cycles_this_boot(boot_id, first if first is not None else 0.0)
        what = (
            f"power cycle requested {rec.get('requested_at', '?')} ({rec.get('fault_class') or 'unknown class'}) "
            f"did not reboot the host within {int(bound)}s (boot id unchanged; attempt {rec.get('attempts')}, "
            f"{unconfirmed} unconfirmed); "
            + (
                "re-firing it once now"
                if backoff == 0
                else f"next attempt at {_local_time(now_epoch + backoff)} ({backoff}s backoff)"
            )
            + "; the device stays held"
        )
        _logger.error(f"POWER-CYCLE UNCONFIRMED: {what}")
        event = health_event(
            "power_cycle_unconfirmed",
            fault_class=rec.get("fault_class", ""),
            attempts=rec.get("attempts"),
            unconfirmed=unconfirmed,
            requested_at=rec.get("requested_at", ""),
            next_attempt_epoch=now_epoch + backoff,
            bound_sec=int(bound),
            host_at_risk=True,
        )
        alert.send_alert(event)
        return rec

    def _power_cycle_confirm_wait(self, rec: Optional[dict], now_epoch: float) -> Optional[dict]:
        """The bounded wait (the :meth:`_deny_power_cycle` state) a settled confirm record imposes now,
        or None when the next cycle may be asked for."""
        if rec is None:
            return None
        last = _epoch(rec.get("requested_at_epoch")) or now_epoch
        if rec.get("state") == "unconfirmed":
            until = _epoch(rec.get("next_attempt_epoch")) or now_epoch
            if now_epoch >= until:
                return None
            return {
                "until": until,
                "last": last,
                "cause": "power cycle unconfirmed",
                "interval_sec": int(until - now_epoch) + 1,
                "why": (
                    f"{rec.get('unconfirmed')} power cycle(s) in a row did not reboot the host; next attempt "
                    f"at {_local_time(until)}, {int(until - now_epoch) + 1}s more (spec 04 I68 backoff)"
                ),
            }
        until = _epoch(rec.get("deadline_epoch")) or now_epoch
        return {
            "until": until,
            "last": last,
            "cause": "power cycle awaiting reboot",
            "interval_sec": int(power_cycle_confirm_sec()),
            "why": (
                f"a power cycle was requested {_local_time(last)} and the host has not rebooted yet; it is "
                f"confirmed or re-fired at {_local_time(until)}, {int(until - now_epoch) + 1}s more"
            ),
        }

    def _power_cycle_unconfirmed_since(self) -> Optional[float]:
        """When this boot's first unconfirmed power cycle was asked for, or None."""
        rec = self.read_power_cycle_confirm()
        boot_id = self._current_boot_id()
        if not rec or not boot_id or rec.get("boot_id") != boot_id or rec.get("state") != "unconfirmed":
            return None
        return _epoch(rec.get("first_epoch"))

    def power_cycle_confirm_status(self) -> Optional[dict]:
        """What /health shows of the confirm record on this boot (read-only), or None."""
        rec = self.read_power_cycle_confirm()
        boot_id = self._current_boot_id()
        if not rec or not boot_id or rec.get("boot_id") != boot_id:
            return None
        unconfirmed = rec.get("state") == "unconfirmed"
        nxt = _epoch(rec.get("next_attempt_epoch" if unconfirmed else "deadline_epoch"))
        return {
            "state": rec.get("state", ""),
            "fault_class": rec.get("fault_class", ""),
            "attempts": rec.get("attempts"),
            "unconfirmed": rec.get("unconfirmed"),
            "requested_at": rec.get("requested_at", ""),
            "next_deadline": datetime.fromtimestamp(nxt).isoformat(timespec="seconds") if nxt is not None else "",
            "next_deadline_epoch": nxt,
            "bound_sec": rec.get("bound_sec"),
        }

    def _retract_power_cycles_this_boot(self, boot_id: str, since: float) -> None:
        """Drop this boot's power-cycle ledger records stamped at or after ``since``: they provably
        caused no boot. Atomic; a failed rewrite leaves them (the I65 cooldown then bounds the ask)."""
        path = health_dir() / AUTO_RECOVERY_LEDGER
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return
        kept = []
        for ln in lines:
            try:
                rec = json.loads(ln)
            except ValueError:
                kept.append(ln)
                continue
            at = _epoch(rec.get("at_epoch")) if isinstance(rec, dict) else None
            if (
                isinstance(rec, dict)
                and rec.get("action") == "power-cycle"
                and rec.get("boot_id") == boot_id
                and at is not None
                and at >= since - 1
            ):
                continue
            kept.append(ln)
        if len(kept) == len(lines):
            return
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(f"{ln}\n" for ln in kept))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            pass

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
        cycle = action == "power-cycle"
        if cycle:
            fault_class, self.power_cycle_fault_class = self.power_cycle_fault_class or label, ""
            prev_confirm = self._note_power_cycle_request(fault_class, reason, time.time())
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
                if cycle:
                    self._write_power_cycle_confirm(prev_confirm)
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
            timed_out = isinstance(exc, (HostActionTimedOut, subprocess.TimeoutExpired))
            if timed_out:
                # It may have launched its detached cycler before it stopped answering: keep the
                # request pending (normal bound) so no device reset starts under a cycle that may
                # still come.
                self.host_action_requested = (action, time.monotonic())
            else:
                self.host_action_requested = None
                if cycle:
                    # Never launched: it is not a cycle to confirm. A timed-out one may still act, so
                    # its record stays and is confirmed or re-fired at the bound (spec 04 I68).
                    self._write_power_cycle_confirm(prev_confirm)
                    self.power_cycle_retry = None
            log(
                f"{label.upper()} FAILED TO LAUNCH: {exc!r}; retracted the rate-limit entry so the rung "
                f"can retry once the cause is fixed"
                + (
                    f"; it timed out and may still act, so device resets stay off for up to "
                    f"{int(host_action_pending_sec())}s"
                    if timed_out
                    else ""
                )
            )
            health_event(f"auto_{ev}_failed", reason=reason, error=repr(exc), timed_out=timed_out)
            metrics.stage_fired(stage, "failed")
            return
        # The pending window counts from the hand-off, not from before a slow pre-fire hook.
        self.host_action_requested = (action, time.monotonic())
        if cycle:
            # So does the confirm bound (spec 04 I68).
            rec = self.read_power_cycle_confirm()
            if rec is not None and rec.get("boot_id") == self._current_boot_id():
                self._stamp_power_cycle_request(rec, time.time())
        metrics.stage_fired(stage, "ok")
