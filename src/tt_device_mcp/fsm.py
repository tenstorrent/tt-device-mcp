# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The broker's root system: the durable BOOT -> HEALTHY/RECOVERING/DOWN state machine that
constructs and owns the health subsystem.

``ServerFsm`` is the one owner of whether the device is fit for a tenant: which of the four states
it is in, the closed-enum reason it left HEALTHY, and the timestamp naming the current episode —
fsync'd to disk on every transition, so a broker that dies mid-hold comes back knowing it, not
guessing from whatever a job happened to leave in memory. The record also carries the boot_id it
was written under, so a reload can tell a same-boot restart (restore everything) apart from one
that crossed a reboot (state/why/latches survive; the job that was running under the old boot did
not, so its context is void — see ``_load``).

It is also the root of the object graph: :meth:`ServerFsm.boot` — the BOOT state's job —
constructs the process's one :class:`HealthMonitor`, :class:`RecoveryMechanism` and the two
platform :class:`Recovery` instances, and every probe pass is triggered through
:meth:`ServerFsm.observe`. Server code and Recovery read the last pass back off
``HealthMonitor.status()``.

BOOT exists only between construction and the first :meth:`ServerFsm.boot_merge` call: it is what
a freshly-constructed FSM reads before startup has reconciled live readings against whatever
episode was open when the process last wrote to disk. Nothing after startup ever re-enters it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from tt_device_mcp import metrics, privileges
from tt_device_mcp.health import (
    OUTCOME_RECOVERED,
    OUTCOME_TERMINAL,
    RESET_MODE_GALAXY,
    RESET_MODE_TARGET,
    GalaxyRecovery,
    HealthMonitor,
    PerTargetRecovery,
    RecoveryDeps,
    RecoveryMechanism,
    _declared_reset_mode,
    _is_galaxy,
    _persistent_recovery,
    _select_recovery,
)
from tt_device_mcp.telemetry import TelemetrySampler

if TYPE_CHECKING:
    from tt_device_mcp.health import HealthState, Recovery

# A boot-attribution clause names how THIS boot came up; it must not ride forward on a long episode's
# carried detail (an 18h-old reboot then reads as this boot's cause). Stripped on carry so only the
# current boot's adjacency-guarded attribution, if any, is present. action may be hyphenated (power-cycle).
_BOOT_ATTRIBUTION_RE = re.compile(r"\s*boot attributed to a broker-fired [\w-]+ recorded at \S+")


def _strip_boot_attribution(detail: str) -> str:
    return _BOOT_ATTRIBUTION_RE.sub("", detail or "").strip()


_logger = logging.getLogger(__name__)


class ServerState(str, Enum):
    BOOT = "boot"
    HEALTHY = "healthy"
    RECOVERING = "recovering"
    DOWN = "down"


# The closed vocabulary `why` may hold — every distinct reason a gate pass or a job's exit has
# ever named for leaving HEALTHY. Closed deliberately: `why` is not a log message (the free-text
# detail carries that), it is what the idle relift and the escalation ladder branch on, so a value
# outside this set is a routing bug, not a typo to shrug off.
FAULTS = (
    "heartbeat",  # the sysfs ARC-heartbeat probe found a chip frozen or gone
    "off_bus",  # chip(s) left the PCIe bus, below the reset floor — self-heals
    "eth_frozen",  # an active-eth-core heartbeat is frozen — self-heals, never reset
    "fabric_unverified",  # enum+ARC passed but the fabric traffic pass exited 77 (could not check)
    "arc_dead",  # enum+ARC snapshot itself failed (silent/unresponsive ARC)
    "job_killed",  # a job ended in a way that can leave the mesh wedged
    "startup_unverified",  # broker start: the mesh has not been re-proven since the last exit
    "gate_error",  # the gate itself raised before it could reach a verdict
    "foreign_holder",  # a non-broker process holds the device; verification deferred
    "probe_unhealthy",  # a read-only pass (with_recover=False) found the mesh unhealthy, no rung attempted
    "operator_reset_unhealthy",  # an operator reset (tool or stream) exited 0 but its verify failed
)


@dataclass
class FsmRecord:
    """One point-in-time snapshot of the FSM. ``why``/``since`` are "" while HEALTHY. ``detail``
    and ``job`` are free-text/structured context for logs and incident capture — never branched
    on, so they may carry anything without touching the closed ``why`` contract above.

    ``dirty`` is a second, orthogonal axis on top of ``why``: whether the NEXT gate pass owes this
    episode a real reset attempt (a job/probe just found something suspicious and nobody has looked
    hard yet) versus an affirmative hold the gate places on purpose and will NOT reset on its own
    (a frozen eth core, an off-bus drop below the reset floor, a fabric pass that could not run, a
    foreign tenant). Two episodes can share a ``why`` — a gate error can arrive dirty (the post-job
    finalizer's own gate raised) or not (the pre-job gate found nothing to check) — so this cannot be
    derived from ``why`` alone."""

    state: ServerState
    why: str = ""
    since: str = ""
    detail: str = ""
    job: dict = field(default_factory=dict)
    dirty: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _episode_elapsed_sec(since: str) -> float:
    """Wall-clock seconds between ``since`` (an :func:`_now_iso` stamp) and now, for the one
    Prometheus histogram (``recovery_episode_seconds``) that needs a duration instead of a raw
    timestamp. Never raises: a ``since`` this FSM did not write itself (empty, or a stale/foreign
    format) reports 0 rather than take the transition down with it — the metric undercounts that
    one episode, which is a far cheaper mistake than a crash in the FSM's own transition path."""
    if not since:
        return 0.0
    try:
        opened = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - opened).total_seconds())


def _validate_why(why: str) -> str:
    """A `why` outside :data:`FAULTS` is a routing bug at the CALL SITE that wrote it: assert loud
    so a test catches the typo, and clamp to ``gate_error`` so a production broker that hit the
    same bug (asserts stripped) still lands on a real fault class instead of persisting a value
    nothing downstream recognises. For the read side (a file written by an older/newer version, or
    hand-edited) see :func:`_clamp_why`, which clamps the same way without the assert — a stale
    file must degrade the value, never crash the process that finds it."""
    assert why == "" or why in FAULTS, f"fsm: {why!r} is not in FAULTS"
    return _clamp_why(why)


def _clamp_why(why) -> str:
    """The read-side twin of :func:`_validate_why`: same clamp, no assert. A `why` this FSM does
    not recognise — a stale value from a version that had a different vocabulary, or a hand-edited
    file — must still land on a real fault class, or nothing downstream (the idle relift, the
    escalation ladder, both of which switch on exact membership in FAULTS) ever matches it and the
    episode sits inert forever."""
    return why if (not why or why in FAULTS) else "gate_error"


class ServerFsm:
    """The root system: the one durable record of whether the device is fit for a tenant — why
    not when it isn't, and since when — and the owner of the health subsystem it judges with.
    Constructed once (see server.py's module-level ``fsm``), never per gate pass; :meth:`boot`
    then builds the monitor/mechanism/platform singletons whose state must equally outlive any
    one gate call."""

    def __init__(self, path: Path, *, current_boot_id: str = "") -> None:
        self._path = path
        # This boot's kernel id (server.py's own _current_boot_id() — the SAME per-boot dedup key
        # the auto-recovery ledger already attributes reboots against, not a second mechanism),
        # stamped into every write and compared against whatever the file already had on load —
        # see _load. "" (unreadable, or a caller that does not care) never matches itself: an
        # unknown boot id must never be trusted to mean "the same boot" on either side.
        self._boot_id = current_boot_id
        self._record = FsmRecord(ServerState.BOOT)
        # Per-episode latches: reset every time a new episode opens (see _open_episode), so a
        # once-per-episode rung re-arms once the device has actually recovered and gone bad again.
        self._latches: dict[str, bool] = {}
        # Whether a persist failure has already been logged this process — see _persist. A broker
        # on a read-only or full state dir would otherwise downgrade to in-memory-only silently,
        # every single mutation, which is the exact durability this file exists to provide.
        self._persist_failed_logged = False
        # The health subsystem this FSM roots — None until boot() constructs it. A bare unit
        # test of the durable record never needs them; anything that probes or escalates does.
        self.monitor: Optional[HealthMonitor] = None
        self.mechanism: Optional[RecoveryMechanism] = None
        self.galaxy: Optional[GalaxyRecovery] = None
        self.per_target: Optional[PerTargetRecovery] = None
        self.sampler: Optional[TelemetrySampler] = None
        self.deps: Optional[RecoveryDeps] = None
        # The platform this host IS, fixed once by resolve_platform(). None means it could not be
        # determined at boot — the mesh could not be read and the operator declared nothing — and
        # select_recovery() falls back to deciding per pass, which self-corrects the moment a later
        # snapshot caches board types. Committing a guess here instead would pin the wrong ladder
        # for the process lifetime, and a wedged Galaxy is exactly the host that cannot answer.
        self.recovery: Optional[Recovery] = None
        self._load()
        # Baseline the one-hot gauge + dwell clock at whatever state _load() landed on (BOOT for
        # a fresh process, or a restored RECOVERING/DOWN/HEALTHY for a same-boot restart) — every
        # later transition goes through _open_episode/on_outcome/boot_merge, but the very first
        # state this process is in needs its own starting mark.
        metrics.state_entered(self._record.state.value)

    # ---- boot-stage construction of the health subsystem ---------------------------------------

    def boot(self, deps: RecoveryDeps) -> None:
        """Construct the health subsystem this FSM roots — the BOOT state's job, run once. Never
        per gate pass: a second :class:`HealthMonitor` would lose the fabric-verdict latch and the
        in-flight fabric-process handle, a second :class:`RecoveryMechanism` the reset
        cooldown/ledger state.

        ``deps``' four monitor-bound fields (``board_types_provider``, ``glx_board_types_provider``,
        ``chip_buses_provider``, ``journal_skip_once``) read state that lives on the monitor
        being built here, so they are wired here rather than demanded of the caller — the
        chicken-and-egg this method exists to resolve. Bindings the caller supplied itself (a
        test's own deps bag, aimed at its own monitor) are left untouched.
        """
        # Measured once here, beside resolve_platform's commit: the two halves of a rung's arming
        # (spec 04 I17) are then the same measurement the boot rung inventory prints.
        privileges.latch()
        self.mechanism = RecoveryMechanism(
            current_boot_id=deps.current_boot_id,
            boot_btime_id=deps.boot_btime_id,
            scoped_reset_backend=deps.scoped_reset_backend,
            set_device_pollers=deps.set_device_pollers,
            set_device_op_detail=deps.set_device_op_detail,
            begin_action_row=deps.begin_action_row,
            write_action_log=deps.write_action_log,
            device_hold_episode_since=deps.device_hold_episode_since,
        )
        monitor = HealthMonitor(deps)
        if deps.board_types_provider is None:
            deps.board_types_provider = lambda: monitor._board_types
        if deps.glx_board_types_provider is None:
            deps.glx_board_types_provider = lambda: monitor._glx_board_types()
        if deps.chip_buses_provider is None:
            deps.chip_buses_provider = lambda: monitor._chip_buses
        if deps.journal_skip_once is None:
            deps.journal_skip_once = lambda kind, reason, **f: monitor._journal_skip_once(kind, reason, **f)
        self.monitor = monitor
        self.deps = deps
        self.galaxy = _persistent_recovery(GalaxyRecovery, monitor, self.mechanism, deps)
        self.per_target = _persistent_recovery(PerTargetRecovery, monitor, self.mechanism, deps)
        self.sampler = TelemetrySampler(monitor, self.mechanism, deps)

    def resolve_platform(self, probe: "Optional[Callable[[], None]]" = None) -> Optional[str]:
        """Fix this host's platform once, at broker start, and commit the Recovery that fits it.

        A host is one platform for the life of the process, so deciding once is both cheaper and
        clearer than re-deriving on every pass. An operator's declared ``TT_DEVICE_MCP_RESET_MODE``
        settles it without touching the device; otherwise ``probe`` runs one tt-smi snapshot to
        cache board types and the verdict comes from those.

        Returns the resolved mode, or None when it stays unresolved — an unreadable mesh with
        nothing declared. That case deliberately commits NOTHING: ``_is_galaxy`` answers None on
        exactly the degraded Galaxy whose board reads fail, and pinning per-target there would ship
        the reset that cannot recover it. Leaving it open keeps the per-pass fallback, which
        resolves itself as soon as any later snapshot succeeds.
        """
        mode = _declared_reset_mode(self.deps.journal_skip_once)
        if mode is None and probe is not None:
            try:
                probe()
            except Exception:  # noqa: BLE001 - an unreadable mesh must not stop the broker booting
                pass
            is_glx = _is_galaxy(self.deps.board_types_provider(), self.deps.glx_board_types_provider())
            if is_glx is not None:
                mode = RESET_MODE_GALAXY if is_glx else RESET_MODE_TARGET
        if mode == RESET_MODE_GALAXY:
            self.recovery = self.galaxy
        elif mode == RESET_MODE_TARGET:
            self.recovery = self.per_target
        return mode

    def select_recovery(self) -> "Recovery":
        """The :class:`Recovery` fitting this host — the one :meth:`resolve_platform` committed at
        boot, or, when boot could not tell, chosen fresh per pass from the singletons :meth:`boot`
        built. Either way the instance is persistent; only an unresolved host re-decides."""
        if self.recovery is not None:
            return self.recovery
        return _select_recovery(self.monitor, self.mechanism, self.deps)

    async def observe(
        self,
        expected: int,
        log: Callable[[str], None],
        *,
        phase: str = "verify_device",
        run_fabric: bool = True,
        run_eth: bool = False,
        fabric_stale: bool = True,
        recovery: "Optional[Recovery]" = None,
    ) -> tuple[bool, dict]:
        """Trigger one probe pass of the system — the root's read of the mesh, routed through the
        platform's ``_verify_device`` seam so a test that replaces that seam governs this too.
        Returns ``(healthy, evidence)`` and stores the pass on ``monitor.status()``; folds no
        verdict into the FSM itself — whether a healthy read RELEASES (or an unhealthy one holds,
        and with which flavour) is the caller's classification, made with context (a runtime-named
        fault, a foreign holder) no probe pass carries.

        ``recovery`` lets a caller that already chose its pass-scoped platform keep every read of
        that pass on the same choice; omitted, the platform is selected fresh. ``run_eth`` asks for
        the passive eth read without the traffic pass (spec 03 I30); it and ``fabric_stale`` (may a
        link-drop skip of that read buy the traffic pass) are passed on only when ``run_eth`` is
        set, so a ``_verify_device`` test fake that predates them keeps its signature."""
        r = recovery if recovery is not None else self.select_recovery()
        if run_eth:
            return await r._verify_device(
                expected, log, run_fabric=run_fabric, phase=phase, run_eth=True, fabric_stale=fabric_stale
            )
        return await r._verify_device(expected, log, run_fabric=run_fabric, phase=phase)

    # ---- durable load/persist -----------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return  # no prior record, or one we cannot read — start clean at BOOT
        if not isinstance(raw, dict):
            # Valid JSON, wrong shape (e.g. a bare list or number) — not a record this FSM ever
            # wrote. Degrade to BOOT rather than let a .get()/.items() below raise into the
            # constructor of a module-scope singleton and take the whole broker down with it.
            return
        try:
            raw_job = raw.get("job")
            record = FsmRecord(
                state=ServerState(raw.get("state", ServerState.BOOT.value)),
                why=_clamp_why(raw.get("why") or ""),
                since=str(raw.get("since") or ""),
                detail=str(raw.get("detail") or ""),
                # A non-dict `job` (a hand-edited or truncated file) must not survive load: every
                # reader downstream — record.job's dict() copy, the health-payload job field — does
                # dict-shaped access on this, and a str/list/bool here would fail there instead,
                # far from whatever wrote the bad file.
                job=raw_job if isinstance(raw_job, dict) else {},
                dirty=bool(raw.get("dirty", False)),
            )
            latches = {k: bool(v) for k, v in (raw.get("latches") or {}).items()}
        except (ValueError, TypeError, AttributeError):
            # A field is present but the wrong type (latches not a mapping, an unknown state
            # string) — same fail-safe as above: BOOT, not a crash at import time. Explicit,
            # rather than relying on __init__'s defaults still being in place, so this stays
            # correct if _load is ever called again after a mutation.
            self._record = FsmRecord(ServerState.BOOT)
            self._latches = {}
            return

        # Same boot as whoever wrote this: a broker restart or crash within one boot, the case
        # this file mainly exists for. Restore everything exactly as persisted.
        file_boot_id = raw.get("boot_id") or ""
        same_boot = bool(file_boot_id) and bool(self._boot_id) and file_boot_id == self._boot_id
        if not same_boot and record.job:
            # A previous boot wrote this (or the file predates boot-scoping and carries no
            # boot_id at all, which reads the same way: unknown, so not "same"). The job this ran
            # under cannot have survived the reboot that took the box down since — job status
            # already does not survive a restart (it lives under /run, tmpfs) — so its context is
            # void. dirty is NOT cleared here: this FSM does not track separately whether a dirty
            # mark came from a job's own exit or from a probe/gate finding, and guessing "it was
            # only the job" is the permissive read — fail closed instead, so a device-caused dirty
            # mark still gets the reset attempt it's owed on the next pass. state/why/since/detail/
            # latches all survive untouched: this is exactly what lets the host-reboot stage (and
            # any rung above it) resume where it left off instead of re-entering the ladder at the
            # bottom, which is the loop the auto-recovery ledger's own min-interval guard exists to
            # prevent.
            record.job = {}
            _logger.warning(
                "fsm: a %s episode (why=%r) written by boot %r was reloaded under boot %r — "
                "voided its job context; state/why/since/latches carried across the reboot",
                record.state.value,
                record.why,
                file_boot_id or "(none)",
                self._boot_id or "(unknown)",
            )
        self._record = record
        self._latches = latches

    def _persist(self) -> None:
        """Write the whole record as one atomic replace: a temp file in the same directory,
        fsync'd, then ``os.replace`` — so a reader (or a crash) never observes a half-written
        file, and the record on disk after any crash is always the last one that fully landed.
        Never raises: a durable record we could not write must not take the gate down with it."""
        payload = {
            "state": self._record.state.value,
            "why": self._record.why,
            "since": self._record.since,
            "detail": self._record.detail,
            "job": self._record.job,
            "dirty": self._record.dirty,
            "latches": self._latches,
            "boot_id": self._boot_id,
            # Not read back by anything in this class — purely so an operator staring at the raw
            # file can tell how stale it is without cross-referencing a log timestamp.
            "written_at": _now_iso(),
        }
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            if not self._persist_failed_logged:
                self._persist_failed_logged = True
                _logger.error(
                    "fsm: could not persist %s (%s) — the FSM is in-memory-only from here on "
                    "this process; a restart will not remember this episode",
                    self._path,
                    e,
                )

    # ---- read-only surface ---------------------------------------------------------------------

    @property
    def state(self) -> ServerState:
        return self._record.state

    @property
    def healthy(self) -> bool:
        """Whether the device is fit for a tenant right now — the one predicate consumers with
        no business importing :class:`ServerState` (the sampler) branch on."""
        return self._record.state is ServerState.HEALTHY

    @property
    def record(self) -> FsmRecord:
        """A snapshot, not the live record: a caller that holds onto ``fsm.record.job`` (an
        incident-capture call, say) must not find it mutated out from under it by the FSM's own
        next transition."""
        return dataclasses.replace(self._record, job=dict(self._record.job))

    def latch(self, name: str) -> bool:
        """Peek a per-episode latch without consuming it — for a caller (the health payload, a
        durable event) that only wants to report whether a rung has already fired this episode."""
        return bool(self._latches.get(name))

    def set_latch(self, name: str, value: bool) -> None:
        """Set a per-episode latch directly — for a caller (the escalation ladder) that must
        check the latch, act, and only set it once the action has actually been taken, rather
        than atomically in one call: an atomic check-and-set would mark the latch spent the
        moment it is checked, before the guards between the check and the action decide whether
        anything actually ran."""
        self._latches[name] = value
        self._persist()

    # ---- transitions ----------------------------------------------------------------------------

    def _open_episode(
        self,
        state: ServerState,
        why: str,
        *,
        detail: str = "",
        job: Optional[dict] = None,
        since: str = "",
        dirty: bool = False,
    ) -> None:
        prev = self._record
        # Closing an open fault episode: HEALTHY reached from RECOVERING/DOWN. `prev.why` is the
        # LAST fault the episode carried (it may have changed mid-episode, or crossed a DOWN
        # excursion) — that is the one worth reporting a duration against, not whichever fault
        # first opened it.
        if state is ServerState.HEALTHY and prev.state in (ServerState.RECOVERING, ServerState.DOWN) and prev.why:
            metrics.recovery_episode_closed(prev.why, _episode_elapsed_sec(prev.since))
        self._record = FsmRecord(
            state=state, why=_validate_why(why), since=since or _now_iso(), detail=detail, job=job or {}, dirty=dirty
        )
        self._latches = {}
        # Opening a fresh fault episode (from HEALTHY/BOOT, or boot_merge's own fresh RECOVERING).
        if state is ServerState.RECOVERING and self._record.why:
            metrics.recovery_episode_opened(self._record.why)
        metrics.state_entered(state.value)
        self._persist()

    def on_readings(self, state: "HealthState") -> None:
        """Fold one HEALTHY probe pass into the FSM, closing whatever episode was open. Named for
        symmetry with :meth:`on_fault`/:meth:`on_outcome`, but only the healthy direction is live:
        every unhealthy verdict this broker produces — a job's exit, a probe's own reading, the
        gate's evidence-based hold decision — reaches the FSM through :meth:`on_fault` instead,
        because none of them hand this method a real :class:`HealthState` to begin with (the
        gate's evidence is a raw dict, not yet shaped into one). A caller with only a synthetic
        "nothing failed" reading (``_healthy_reading()`` in server.py) is exactly this method's
        shape; a caller that DOES have unhealthy evidence should still go through ``on_fault`` with
        its own classification, never invent one by threading a HealthState through here."""
        if state.healthy and self._record.state is not ServerState.HEALTHY:
            self._open_episode(ServerState.HEALTHY, "")

    def on_fault(self, why: str, *, detail: str = "", job: Optional[dict] = None, dirty: bool = True) -> None:
        """The universal "something is wrong" entry: a job's own exit (killed, timed out, crashed
        by signal), the lock-free sampler isolating a dead chip with no job in sight, or the gate's
        own evidence-based hold decision, alike. Opens a new episode from HEALTHY/BOOT, or — while
        already RECOVERING/DOWN — refreshes ``why``/``detail`` for THIS finding, leaving the
        episode's ``since`` and latches exactly as they were: the episode is what has not yet
        recovered, and a repeat finding is not a new occurrence of it.

        ``dirty`` (default True, this call's usual shape) marks the episode as owing the NEXT gate
        pass a real reset attempt — nobody has looked hard yet. A caller placing an affirmative
        hold instead (the gate itself, deciding NOT to reset — a frozen eth core, a fabric pass
        that could not run, a foreign tenant) passes ``dirty=False``: the next gate pass must not
        treat an intentional hold as still owing it a reset just because the episode is open.

        A dirty mark arriving over an EXISTING hold (dirty=True landing on a record that is
        currently dirty=False — the lock-free sampler isolating a second, different chip mid-relift
        while the first sits held) sets ``dirty`` without touching ``why``/``detail``: the hold's own
        classification must survive so the relift's next guard still recognises it, and only a
        hold call (dirty=False) or another dirty mark landing on an already-dirty record may
        replace it."""
        why = _validate_why(why) or "gate_error"
        if self._record.state in (ServerState.HEALTHY, ServerState.BOOT):
            self._open_episode(ServerState.RECOVERING, why, detail=detail, job=job, dirty=dirty)
        else:
            if not (dirty and not self._record.dirty):
                self._record.why = why
                self._record.detail = detail or self._record.detail
            self._record.dirty = dirty
            if job is not None:
                self._record.job = job
            self._persist()

    def note(self, detail: str) -> None:
        """Reword the current episode's free text without touching state, why, since, or the
        latches — for a caller (a foreign-holder rescan) that only has a better sentence to report
        for the SAME open episode, not a new verdict about it. A no-op while HEALTHY: there is no
        episode to reword."""
        if self._record.state is ServerState.HEALTHY:
            return
        self._record.detail = detail
        self._persist()

    def on_outcome(self, outcome: str) -> None:
        """Fold one escalation attempt's outcome into the FSM. ``recovered`` closes the episode
        (-> HEALTHY); ``terminal`` — the ladder ran and could not help — is the one path to DOWN.
        ``waiting`` is deliberately a no-op: ``Recovery.escalate`` (base.py) returns it both when a
        rung is legitimately deferring (a cooldown, a tenant) AND, unconditionally, when the
        platform has no ladder at all (PerTargetRecovery) — the latter is not a failed ladder, it
        is the absence of one, so mapping it to DOWN would wedge every per-target host there
        forever the first time it went unhealthy."""
        if outcome == OUTCOME_RECOVERED:
            self._open_episode(ServerState.HEALTHY, "")
        elif outcome == OUTCOME_TERMINAL:
            self._record.state = ServerState.DOWN
            metrics.state_entered(ServerState.DOWN.value)
            self._persist()
        # OUTCOME_WAITING: hold exactly where we are — see the docstring above.

    def boot_merge(self, open_episode: Optional[FsmRecord], attributed_boot: Optional[dict] = None) -> ServerState:
        """Reconcile BOOT into a real state, once, at startup. Always closed: a restart loses the
        in-memory verification state, and sysfs reads perfectly healthy across a wedged ethernet
        link, so nothing available at this point is proof the mesh is fit — only the startup
        gate's own forced probe pass (enum+ARC+eth+fabric, run right after this) can open the
        door, through the ordinary :meth:`on_readings` a healthy verdict there already goes
        through. There is no "clean boot, skip the verify" branch here to take that shortcut.

        ``open_episode`` — an episode still open when this broker last wrote (this FSM's own
        loaded record, or one reconstructed from the durable health-event journal on the first
        boot after this file existed) — wins unconditionally when present: the same reasoning
        that closes a boot with no episode applies doubly to one that was already mid-recovery.
        With none, the fresh episode is RECOVERING(startup_unverified).

        ``attributed_boot`` — this boot's own auto-recovery ledger entry, if a broker-fired reboot
        or power cycle brought it up — is not part of the state decision (only the startup gate
        proves whether that escalation actually worked); it is folded into ``detail`` purely so the
        FSM's own record names the boot's cause for anyone reading it later."""
        if open_episode is not None and open_episode.state in (ServerState.RECOVERING, ServerState.DOWN):
            self._record = FsmRecord(
                state=open_episode.state,
                why=open_episode.why,
                since=open_episode.since or _now_iso(),
                detail=_strip_boot_attribution(open_episode.detail),
                job=open_episode.job,
                dirty=open_episode.dirty,
            )
            metrics.state_entered(open_episode.state.value)
        else:
            self._open_episode(ServerState.RECOVERING, "startup_unverified")
        if attributed_boot:
            action = attributed_boot.get("action", "?")
            at = attributed_boot.get("at", "?")
            self._record.detail = (f"{self._record.detail} " if self._record.detail else "") + (
                f"boot attributed to a broker-fired {action} recorded at {at}"
            )
        self._persist()
        return self._record.state
