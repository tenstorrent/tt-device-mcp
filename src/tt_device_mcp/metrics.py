# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Prometheus telemetry: usage + health metrics, exported as a node_exporter textfile.

A leaf module, peer of ``constants.py`` — imported BY ``fsm.py``, the ``health`` package, and
``server.py``, importing none of them back. That is deliberate, not incidental: ``fsm.py`` must
call into this module on every transition, so if this module reached back into ``fsm`` (e.g. to
import :data:`fsm.FAULTS` directly) the two would import each other. Every closed-label vocabulary
below is therefore a literal tuple, not an import, each commented with where its real source of
truth lives; ``tests/test_metrics.py`` cross-checks every one of them against that source so the
two cannot drift apart silently.

No HTTP route, no listener: the deployed broker runs ``--socket ... --no-http``, so nothing
listens on TCP 8333 and a scrape endpoint would be unreachable. Instead this writes a
node_exporter TEXTFILE on the existing stats-persistence cadence (``STATS_UPDATE_SEC``) plus once
on clean shutdown — a separate scraper job already collects that directory. ``prometheus_client``
supplies the metric objects and ``generate_latest()`` rendering only; its bundled HTTP server is
never started.

Every metric is prefixed ``tt_device_broker_``, not the bare ``tt_device_`` an earlier draft used:
this host's ``/var/lib/prometheus/node-exporter/`` already carries ``tt_device_usage.prom`` from
an unrelated ``tt-device-usage.service`` (``tt_device_usage_seconds_total{pcie_device,state}``,
``tt_device_acquisitions_total`` — a 1s-sampled, per-chip series that counts every holder, not just
broker jobs). No metric NAME collides today, but a bare ``tt_device_`` prefix shared by two
independent producers is how a dashboard ends up built against the wrong series — the extra
``broker_`` segment is this module's, and only this module's, namespace.

Write-only, like the ``health_events.jsonl`` journal: any block may report a metric, none may
read one back. Every label below is a CLOSED ENUM, validated at the call site — an unknown value
raises (so a test catches the routing bug) rather than minting a new Prometheus time series,
which is how a label leak becomes a cardinality outage.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, disable_created_metrics, generate_latest

# One _created sample per counter is a third of the exposition and answers a question nobody
# here asks (counter birth time); the scraper job does not use it either.
disable_created_metrics()

from tt_device_mcp.constants import STAGE_NAMES, user_state_dir

_logger = logging.getLogger(__name__)

# One private registry rather than prometheus_client's process-global default: this module is
# imported by fsm.py, health/, and server.py alike, and a private registry is what lets
# tests re-exercise it (see reset_for_tests, below the metric objects) without a chance of
# colliding with some OTHER default-registry user later added to the process.

# ---- closed label vocabularies ----------------------------------------------------------------
#
# Every tuple below is copied from its real source, not imported from it (see module docstring).

# fsm.ServerState's four values.
STATES = ("boot", "healthy", "recovering", "down")

# fsm.FAULTS — the FSM's own closed vocabulary for why it left HEALTHY. Duplicated, not imported:
# fsm.py calls into this module on every transition, so importing fsm.FAULTS here would make the
# two modules import each other.
FAULTS = (
    "heartbeat",
    "off_bus",
    "eth_frozen",
    "fabric_unverified",
    "arc_dead",
    "job_killed",
    "startup_unverified",
    "gate_error",
    "foreign_holder",
    "probe_unhealthy",
    "operator_reset_unhealthy",
)

# constants.STAGE_NAMES, in severity order (0..4) — imported, not copied. It lives in
# constants.py rather than the health.recovery.stages package the names actually name: importing
# THAT package, even one with an empty __init__.py, still runs health/recovery/__init__.py first
# (Python always executes a parent package before a submodule), which imports this module —
# reintroducing exactly the metrics<->health cycle this module's own docstring avoids for fsm.
# GalaxyRecovery.escalate() calls the underlying primitives (reset_chip_via_bridge,
# reset_with_quiesce, _fire_ubb_reset, RecoveryMechanism._fire_recovery_escalation) directly
# rather than through a dispatching object, so stage_fired() is wired at THOSE call sites using
# these same names. See health/recovery/__init__.py, health/recovery/base.py,
# health/recovery/galaxy.py.
STAGES = STAGE_NAMES

# health.core.Verdict's three values.
VERDICTS = ("healthy", "unhealthy", "skipped")

# The four probe names HealthMonitor.update() actually records as Observation.monitor (see
# health/monitor.py) — "eth_heartbeat", not "eth": that is the real label the aggregate already
# writes into HealthState, and probe_observed() is wired into the same methods update() calls
# (verify_device_health/verify_fabric_health/verify_eth_heartbeat/heartbeat_verdict) so every
# caller of those — the gate, Recovery._verify_device, and update() itself — is covered from one
# instrumentation point per probe. "aiclk_ceiling" is the opt-in clock-cap step (health/aiclk_ceiling.py).
PROBES = ("hostpci", "heartbeat", "pci", "aiclk_ceiling", "eth_heartbeat", "fabric")

# The JobStatus values server.Stats.record_job_completion counts. There is no separate
# "cancelled" outcome in the real code: _kill_job() cancels a QUEUED job by setting the same
# JobStatus.KILLED a running job gets when killed, so "killed" is the one label for both. "hung"
# IS counted here even though the legacy Stats if/elif chain never counted JobStatus.HUNG (a
# pre-existing gap in the CLI-facing stats/ JSON, not in scope to fix) — HUNG jobs are typically
# the LONGEST device occupancy, so omitting them from busy_seconds_total would make this series
# read systematically lower than Stats.utilization (which DOES count hung time, via
# update_device_state), and two disagreeing "utilization" numbers is worse than one.
OUTCOMES = ("completed", "failed", "killed", "timeout", "hung")

# A stage/action's own firing outcome (independent of whether recovery later verified healthy):
# "ok" the action ran and completed (rc 0, or a reboot/power-cycle that issued and took the box
# down); "failed" it ran and did not (nonzero exit, or a subprocess/launch exception); "timeout" it
# neither completed nor errored inside its own deadline; "blocked" a guard declined to fire it at
# all — a kill-switch off, a rate limit/cooldown, a once-per-episode latch, a foreign tenant, a
# reset already cycling; "not_applicable" nothing was even a candidate — the topology/platform
# gives this stage no target (no bridge behind a chip that left the bus entirely, no UBB tray
# concept on a per-target host). The split matters operationally: "blocked" is a policy knob an
# operator can flip; "not_applicable" is a platform fact that will never change and must not be
# graphed as if it were an outage. Before this split every stage's structural non-applicability
# was folded into "blocked" too, which is why a per-target (non-Galaxy) host's ubb_tray series
# climbed on every single below-floor off-bus drop forever — indistinguishable from an operator
# leaving a kill-switch off.
STAGE_OUTCOMES = ("ok", "failed", "timeout", "blocked", "not_applicable")


def _validate(value: str, allowed: tuple, what: str) -> str:
    if value not in allowed:
        raise ValueError(f"metrics: {value!r} is not a known {what} ({', '.join(allowed)})")
    return value


# ---- metric objects -----------------------------------------------------------------------------
#
# Built by a function, not inline, so :func:`reset_for_tests` can rebuild every object against a
# fresh registry without duplicating each metric's name/help/labels a second time.


def _construct_metrics(registry: CollectorRegistry) -> None:
    global jobs_total, job_wait_seconds, queue_depth, busy_seconds_total
    global free_seconds_total
    global server_state, state_seconds_total, recovery_episodes_total
    global recovery_episode_seconds_total, stages_fired_total, probe_checks_total
    global probe_last_seconds, snapshot_gauges
    global platform_state, fault_state, probe_last_verdict

    jobs_total = Counter("tt_device_broker_jobs_total", "Jobs finished, by outcome", ["outcome"], registry=registry)
    # A labeled counter emits nothing until its first increment, so a freshly started broker
    # published no job counts at all. Zero finished jobs is a real reading — the core one for a
    # job broker — and a series that does not exist cannot be summed or alerted on. The outcome
    # vocabulary is closed, so all five rows exist from the first write, zeros included.
    for outcome in OUTCOMES:
        jobs_total.labels(outcome=outcome)
    # Percentile GAUGES, not Histograms. The default bucket ladder tops out at 10s, and the things
    # worth timing here run far past it — a fabric traffic pass is 45-100s, a recovery episode runs
    # to the 1200s hold ceiling — so every observation that mattered landed in +Inf while 105 of the
    # exposition's 187 lines were buckets reading 0. These carry the same numbers the stats JSON and
    # the CLI already show. The trade is deliberate: quantiles cannot be aggregated across hosts or
    # re-cut over an arbitrary window, which a single-host textfile was never able to do anyway.
    job_wait_seconds = Gauge(
        "tt_device_broker_job_wait_seconds",
        "Queue wait per job, this session (quantile= 0.5 | 0.95 | max)",
        ["quantile"],
        registry=registry,
    )

    queue_depth = Gauge("tt_device_broker_queue_depth", "Jobs queued, not running", registry=registry)
    # Busy and free advance from the SAME clock (Stats.update_device_state, via the snapshot),
    # so busy + free = session elapsed and busy/(busy+free) is utilization — publishing them from
    # two accountings (job runtimes vs the clock) would make that ratio quietly wrong. Busy means
    # a broker-run job held the device, hung time included; it excludes non-broker holders (see
    # tt-device-usage.service's tt_device_usage_seconds_total) and counts broker device ops
    # (resets, fabric passes) as free.
    busy_seconds_total = Counter(
        "tt_device_broker_busy_seconds_total",
        "Seconds a broker-run job held the device; busy/(busy+free) is utilization",
        registry=registry,
    )
    free_seconds_total = Counter(
        "tt_device_broker_free_seconds_total", "Seconds no broker-run job held the device", registry=registry
    )

    server_state = Gauge("tt_device_broker_server_state", "Orchestration FSM, one-hot", ["state"], registry=registry)
    state_seconds_total = Counter(
        "tt_device_broker_state_seconds_total", "Seconds accumulated per FSM state", ["state"], registry=registry
    )
    recovery_episodes_total = Counter(
        "tt_device_broker_recovery_episodes_total", "Recovery episodes opened, by fault", ["why"], registry=registry
    )
    recovery_episode_seconds_total = Counter(
        "tt_device_broker_recovery_episode_seconds_total",
        "Episode lifetime summed, by fault; with recovery_episodes_total gives the mean",
        ["why"],
        registry=registry,
    )
    stages_fired_total = Counter(
        "tt_device_broker_stages_fired_total",
        "Recovery-ladder stage fires, by stage and outcome (see STAGE_OUTCOMES for what each "
        "outcome means). Counts only the broker's OWN automatic recovery escalation. Excludes an "
        "adopted foreign reset scope (one already cycling from a prior broker instance or a "
        "concurrent path, waited out and verified rather than fired) and excludes every reset run "
        "by the operator-facing tt_device_reset tool, which is a deliberate human action, not an "
        "escalation this ladder chose.",
        ["stage", "outcome"],
        registry=registry,
    )
    probe_checks_total = Counter(
        "tt_device_broker_probe_checks_total", "Probe verdicts", ["probe", "verdict"], registry=registry
    )

    # The point-in-time state of the broker, published from one BrokerTelemetry snapshot (see
    # telemetry.py, which owns the declaration of what is tracked). Counters above answer "how much
    # has happened"; these answer "what is true right now", which is what an operator looking at a
    # held device actually needs and what this exposition had none of.
    platform_state = Gauge(
        "tt_device_broker_platform", "Platform the boot flow committed, one-hot", ["platform"], registry=registry
    )
    fault_state = Gauge(
        "tt_device_broker_fault",
        "The fault the CURRENT episode carries, one-hot ('none' while "
        "healthy) — the counters say how often, this says what is wrong right now",
        ["why"],
        registry=registry,
    )
    probe_last_verdict = Gauge(
        "tt_device_broker_probe_last_verdict",
        "Each probe's verdict on the LAST pass, one-hot",
        ["probe", "verdict"],
        registry=registry,
    )

    snapshot_gauges = {
        "chips_present": Gauge(
            "tt_device_broker_chips_present", "Chips enumerated in /dev right now", registry=registry
        ),
        "chips_expected": Gauge(
            "tt_device_broker_chips_expected",
            "Chips this host SHOULD have — high-water baseline, never the " "survivor count",
            registry=registry,
        ),
        "isolated_chips": Gauge(
            "tt_device_broker_isolated_chips", "Chips cut out of the kernel after leaving the bus", registry=registry
        ),
        "device_busy": Gauge("tt_device_broker_device_busy", "1 while a job holds the device", registry=registry),
        "fsm_dirty": Gauge(
            "tt_device_broker_fsm_dirty",
            "1 while the next gate pass owes this episode a reset attempt",
            registry=registry,
        ),
        "reset_in_flight": Gauge(
            "tt_device_broker_reset_in_flight",
            "1 while a reset is executing — chips read all-ones BY DESIGN",
            registry=registry,
        ),
        "reset_cooling": Gauge(
            "tt_device_broker_reset_cooling",
            "1 while a failed reset's 600s cooldown suppresses the next one",
            registry=registry,
        ),
        # Tri-state, so "never ran" is distinguishable from "ran and failed": a skipped fabric
        # pass and an unhealthy one route the ladder completely differently.
        "fabric_ok": Gauge(
            "tt_device_broker_fabric_ok",
            "Last REAL fabric verdict: 1 healthy, 0 unhealthy, -1 never ran",
            registry=registry,
        ),
    }
    probe_last_seconds = Gauge(
        "tt_device_broker_probe_last_seconds",
        "How long the LAST run of each probe took — the one to alert on as the fabric pass creeps "
        "toward its own timeout",
        ["probe"],
        registry=registry,
    )


REGISTRY = CollectorRegistry()
_construct_metrics(REGISTRY)


# ---- validated write helpers ---------------------------------------------------------------------
#
# Every block that reports a metric goes through one of these, never `.labels(...).inc()`
# directly on the objects above — that is what makes every label a validated closed enum instead
# of an open door to a new time series.


def job_completed(outcome: str) -> None:
    """One job's terminal outcome. Timing publishes elsewhere: the wait quantiles from the
    snapshot, busy/free from the occupancy clock — this counts, nothing more."""
    _validate(outcome, OUTCOMES, "job outcome")
    jobs_total.labels(outcome=outcome).inc()


def stage_fired(stage: str, outcome: str) -> None:
    """One recovery action's outcome. See :data:`STAGE_OUTCOMES` for what each value means."""
    _validate(stage, STAGES, "recovery stage")
    _validate(outcome, STAGE_OUTCOMES, "stage outcome")
    stages_fired_total.labels(stage=stage, outcome=outcome).inc()


def probe_observed(probe: str, verdict: str, duration_sec: float) -> None:
    """One probe pass: a verdict count plus its duration, for the probe that is the fabric pass's
    watch (the one to alert on if it creeps toward its own timeout)."""
    _validate(probe, PROBES, "probe")
    _validate(verdict, VERDICTS, "probe verdict")
    probe_checks_total.labels(probe=probe, verdict=verdict).inc()
    probe_last_seconds.labels(probe=probe).set(max(0.0, duration_sec))


def recovery_episode_opened(why: str) -> None:
    """A fresh RECOVERING episode opened for ``why`` — called once per episode, from the FSM's own
    transition, never per gate pass (a repeat finding on an already-open episode is not a new
    occurrence of it; see ``ServerFsm.on_fault``)."""
    _validate(why, FAULTS, "fault")
    recovery_episodes_total.labels(why=why).inc()


def recovery_episode_closed(why: str, duration_sec: float) -> None:
    """An episode's total lifetime, observed when it finally closes back to HEALTHY — whether it
    stayed RECOVERING throughout or passed through DOWN along the way. ``why`` is the fault the
    episode carried at the moment it closed, which is what a caller straddling a DOWN excursion
    should report: the LAST fault, not the first."""
    _validate(why, FAULTS, "fault")
    recovery_episode_seconds_total.labels(why=why).inc(max(0.0, duration_sec))


# The FSM's own one-hot gauge + per-state dwell clock. Module-level like the metric objects
# themselves: exactly one FSM exists per process (server.py's module-level ``fsm`` singleton), so
# there is exactly one "current state" to track alongside it.
_current_state: dict = {"name": None, "since": None}

# Last cumulative busy/free the snapshot reported — what publish_snapshot diffs against.
_session_clock: dict = {"busy": 0.0, "free": 0.0}


def _flush_dwell(now: Optional[float] = None) -> None:
    """Settle the CURRENTLY-active state's elapsed dwell into ``state_seconds_total`` and
    rebaseline the clock to ``now``, without changing which state is current.

    Split out of :func:`state_entered` because a transition is not the only moment this owes an
    accounting: ``state_entered`` only ever settled the OUTGOING state at the NEXT transition, so
    a broker sitting in RECOVERING for hours exported that series flat at its stale value for the
    whole incident — ``increase(...[15m])`` reads 0 during exactly the window an operator would
    alert on — and a process that exits or restarts mid-episode never recorded that dwell at all.
    :func:`write_textfile` calls this before every render, so the open interval is always flushed
    into what gets published, whether or not a transition has happened since the last write."""
    now = time.monotonic() if now is None else now
    prev_name, prev_since = _current_state["name"], _current_state["since"]
    if prev_name is not None and prev_since is not None:
        state_seconds_total.labels(state=prev_name).inc(max(0.0, now - prev_since))
    _current_state["since"] = now


def state_entered(state: str, *, now: Optional[float] = None) -> None:
    """Record one FSM transition: flush the PREVIOUS state's dwell (via :func:`_flush_dwell`),
    flip the one-hot :data:`server_state` gauge to ``state``, and start its clock.

    ``now`` defaults to :func:`time.monotonic` — a wall-clock jump (NTP step, a suspended VM)
    must never appear as negative or absurd dwell time in ``state_seconds_total``. The FSM is
    the sole caller in production; tests pass an explicit ``now`` to make the elapsed math exact.
    """
    _validate(state, STATES, "FSM state")
    now = time.monotonic() if now is None else now
    _flush_dwell(now)
    for s in STATES:
        server_state.labels(state=s).set(1.0 if s == state else 0.0)
    _current_state["name"] = state


def publish_snapshot(snap) -> None:
    """Set every point-in-time gauge from one :class:`telemetry.BrokerTelemetry`.

    Takes the snapshot object and reads attributes off it rather than importing its type: this
    module is a leaf that ``fsm`` and ``health`` both import, and ``telemetry`` imports ``health``,
    so importing the type here would close a cycle (see the module docstring).
    """
    g = snapshot_gauges
    g["chips_present"].set(snap.chips_present)
    g["chips_expected"].set(snap.chips_expected)
    g["isolated_chips"].set(snap.isolated_chips)
    queue_depth.set(snap.queue_depth)
    g["device_busy"].set(1.0 if snap.device_busy else 0.0)
    g["fsm_dirty"].set(1.0 if snap.fsm_dirty else 0.0)
    g["reset_in_flight"].set(1.0 if snap.reset_in_flight else 0.0)
    g["reset_cooling"].set(1.0 if snap.reset_cooling else 0.0)
    g["fabric_ok"].set(-1.0 if snap.fabric_ok is None else (1.0 if snap.fabric_ok else 0.0))

    # The snapshot carries the session clock's cumulative busy/free; the counters advance by the
    # delta since the last publish. A value that went BACKWARDS (a fresh Stats object) rebaselines
    # without incrementing — a counter must never jump by a whole session's worth on a reset.
    for key, cur, counter in (("busy", snap.busy_sec, busy_seconds_total), ("free", snap.free_sec, free_seconds_total)):
        prev = _session_clock[key]
        if cur >= prev:
            counter.inc(cur - prev)
        _session_clock[key] = cur

    job_wait_seconds.labels(quantile="0.5").set(snap.wait_p50)
    job_wait_seconds.labels(quantile="0.95").set(snap.wait_p95)
    job_wait_seconds.labels(quantile="max").set(snap.wait_max)

    # Platform, fault, and per-probe verdict publish ONLY their current value (cleared, then set),
    # not a full one-hot over the vocabulary: 25 always-present rows earned their keep for no one.
    # An alert like fault{why="off_bus"} == 1 still matches — the series simply appears when the
    # value does — and "none"/"unresolved" are published explicitly while healthy/unprobed, so a
    # missing metric always means a dead broker, never a good one.
    _validate(snap.platform, ("galaxy", "per-target", "unresolved"), "platform")
    platform_state.clear()
    platform_state.labels(platform=snap.platform).set(1.0)
    fault_state.clear()
    fault_state.labels(why=_validate(snap.fsm_why, FAULTS, "fault") if snap.fsm_why else "none").set(1.0)
    probe_last_verdict.clear()
    for pr, v in snap.probe_verdicts.items():
        probe_last_verdict.labels(
            probe=_validate(pr, PROBES, "probe"), verdict=_validate(v, VERDICTS, "probe verdict")
        ).set(1.0)


def render() -> bytes:
    """The full exposition, in Prometheus text format."""
    return generate_latest(REGISTRY)


def reset_for_tests() -> None:
    """Rebuild every metric object against a fresh registry, and clear the FSM dwell-clock and the
    write-failure dedup — for test isolation ONLY. Production never calls this: the module-level
    objects live for the whole process, same as every other singleton here (``health_monitor``,
    ``recovery_mechanism``, ...).

    Needed because Counters are cumulative and this module is a process-global: any OTHER test
    that builds a :class:`~tt_device_mcp.fsm.ServerFsm` or runs a probe check calls straight into
    these SAME objects, exactly as production would, so a test asserting an absolute value (not a
    delta) must first know it is starting from zero."""
    global REGISTRY, _write_failed_logged
    REGISTRY = CollectorRegistry()
    _construct_metrics(REGISTRY)
    _current_state["name"] = None
    _current_state["since"] = None
    _session_clock["busy"] = 0.0
    _session_clock["free"] = 0.0
    _write_failed_logged = False


TEXTFILE_NAME = "tt_device_mcp.prom"

# Logged once per process, never reset in production — mirrors fsm.py's own
# ``_persist_failed_logged``: the first failure is the whole signal, and a directory that stays
# unwritable would otherwise repeat it on every 30s write for the life of the process.
_write_failed_logged = False


def _textfile_dir() -> Path:
    """Read fresh every call, not cached at import time, like every other env-derived path in this
    codebase (``constants.user_state_dir``, ``HealthMonitor._fabric_check_cmd``, ...) — a cached
    value would be immune to ``TT_DEVICE_MCP_TEXTFILE_DIR`` set after this module first imports,
    which is exactly when a test sets it.

    Root (the system broker) writes straight into node_exporter's real collector directory.
    Anyone else cannot create a dir there, so this instead resolves under the daemon's own
    per-user state base — a path that is at least writable, unlike the ``/var/lib/prometheus``
    ``ERROR | metrics: could not write ...`` a non-root process used to log on every cadence.
    Writable is not the same as scraped, though: nothing reads a per-user path by default, so a
    per-user deployment that wants these metrics collected still has to point node_exporter's
    own ``--collector.textfile.directory`` at it."""
    override = os.environ.get("TT_DEVICE_MCP_TEXTFILE_DIR", "").strip()
    if override:
        return Path(override)
    if os.geteuid() == 0:
        return Path("/var/lib/prometheus/node-exporter")
    return user_state_dir() / "metrics"


def write_textfile() -> None:
    """Publish the exposition for node_exporter's textfile collector.

    Flushes the open dwell interval first (see :func:`_flush_dwell`) so a state that has been
    current since the last write is never reported flat. Written to a PID-scoped ``.tmp`` sibling
    in the SAME directory and ``os.replace``'d over the real name: node_exporter ignores ``.tmp``
    files, and a reader must never observe a half-written exposition; the PID in the name means two
    writers (a restart racing its predecessor's still-draining write) can never collide on the same
    temp path. Never raises — telemetry is not allowed to stall or crash the broker; the one thing
    it owes an operator is a single loud log line the first time the directory turns out to be
    unwritable, not a silent, permanent loss of every metric this process will ever report."""
    global _write_failed_logged
    _flush_dwell()
    target_dir = _textfile_dir()
    tmp = target_dir / f"{TEXTFILE_NAME}.{os.getpid()}.tmp"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(render())
        os.replace(tmp, target_dir / TEXTFILE_NAME)
    except OSError as e:
        if not _write_failed_logged:
            _write_failed_logged = True
            _logger.error(
                "metrics: could not write %s (%s) — this broker's Prometheus metrics will not "
                "reach node_exporter until the directory is writable again",
                target_dir,
                e,
            )
    except Exception:  # noqa: BLE001 - telemetry must never take the broker down with it
        if not _write_failed_logged:
            _write_failed_logged = True
            _logger.exception("metrics: unexpected error rendering/writing the Prometheus textfile")
    finally:
        # A failure between write_bytes (the tmp now exists) and os.replace would otherwise leave
        # it behind indefinitely — holding space on the exact ENOSPC that could have caused it.
        # A successful replace has already moved it, so this is a no-op on the common path.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
