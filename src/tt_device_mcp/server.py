# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""
TT Device MCP Server (Streamable HTTP)

Manages the shared Tenstorrent device across multiple AI agents/workspaces.
Runs as a persistent HTTP service - all Claude sessions connect to the same instance.

Uses the modern MCP Streamable HTTP transport (not deprecated SSE).

Usage:
    tt-device-mcp [--port PORT]

    Default port: 8333

Claude Configuration:
    claude mcp add -s user -t http tt-device-mcp http://localhost:8333/mcp
"""

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import yaml
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from tt_device_mcp import __version__, metrics, privileges, telemetry
from tt_device_mcp.constants import (
    DEFAULT_PORT,
    FABRIC_CHECK_CANNOT_CHECK_RC,
    GRACEFUL_KILL_GRACE_SEC,
    JOB_RETENTION_SEC,
    POST_STEP_DEADLINE_DEFAULT_SEC,
    POST_STEP_DEADLINE_ENV,
    PRE_STEP_DEADLINE_DEFAULT_SEC,
    PRE_STEP_DEADLINE_ENV,
    RESET_STREAM_KEEPALIVE_LINE,
    RESET_STREAM_KEEPALIVE_SEC,
    SIGTERM_GRACE_SEC,
    STAGE_BRIDGE_RESET,
    STAGE_HOST_REBOOT,
    STAGE_POWER_CYCLE,
    STAGE_SMI_RESET,
    STAGE_UBB_TRAY,
    STATS_UPDATE_SEC,
)
from tt_device_mcp.constants import step_deadline_sec as _shared_step_deadline_sec

# Device node directory; module-level so tests can point it at a fixture dir.
TT_DEV_DIR = "/dev/tenstorrent"
from tt_device_mcp.device_holders import (
    MIN_TENANT_UID,
    ReclaimResult,
    enumerate_device_holders,
    evaluate_reset_gate,
    reclaim_foreign_holders,
)
from tt_device_mcp.fsm import ServerFsm, ServerState
from tt_device_mcp.health import (
    _HOST_ESCALATION_ACTION,
    BLOCKED,
    DEFER,
    HOLD_ESCALATION_REARM_SEC,
    HOLD_FABRIC_UNVERIFIED,
    OUTCOME_RECOVERED,
    OUTCOME_WAITING,
    RELEASE,
    RESET_COOLDOWN_SEC,
    RESET_MODE_GALAXY,
    RESET_MODE_TARGET,
    SAMPLE_RING_SIZE,
    WAIT,
    Evidence,
    HealthState,
    RecoveryDeps,
    Verdict,
    _declared_reset_mode,
    _eth_freeze_holds,
    _fire_power_cycle,
    _galaxy_reset_min_dead_chips,
    _offbus_hold_ceiling_sec,
    _reboot_min_dead_chips,
    _stuck_hold_ceiling_sec,
    _ubb_reset_enabled,
    aer_totals,
    append_trace,
    bridge_reset_enabled,
    bridge_reset_unavailable_reason,
    capture_incident,
    chip_node_present,
    chip_pci_bdf,
    chip_sample,
    chip_snapshot,
    chip_snapshot_event,
    dead_chips,
    eth,
    fabric,
    gone_chip_bridge_reset_enabled,
    health_dir,
    health_event,
    heartbeat_supported,
    heartbeat_verdict,
    isolate_chip,
    mark_boot,
    previous_boot_bus_locks,
    previous_boot_error,
    read_health_events,
    read_heartbeats,
    version_floor_warnings,
)
from tt_device_mcp.peercred import username_for_uid
from tt_device_mcp.privsep import privsep_enabled, privsep_prefix_for, privsep_refusal, should_privsep
from tt_device_mcp.socket_transport import (
    PeerCredMiddleware,
    current_peer_uid,
    current_via_mcp,
    resolve_socket_path,
    serve_unix_socket,
)

DEFAULT_TIMEOUT_SEC = 600

# A hard ceiling, not a default anyone may raise. The device is shared, and a run that holds
# it for longer than this is not a long run — it is a queue nobody else can use, and a wedge
# nobody notices for half an hour. There is always a way to fit inside it: move the parts
# that do not need silicon off the device, or split the work. This is not negotiable, so the
# tool does not offer a knob that pretends otherwise.
MAX_TIMEOUT_SEC = 1500  # 25 minutes

# Exec is unqueued and can run concurrently with a tenant job (diagnostics on a hung one), so
# it is bounded far tighter than a queued job: a triage that cannot answer in this many seconds
# is wedged itself, and letting it run unbounded adds a second stuck process to the mesh.
EXEC_MAX_TIMEOUT_SEC = 600

# In-memory job output is a bounded tail (the log file is the complete record).
# Unbounded accumulation of a chatty job's output would starve the event loop:
# string `+=` is O(n), so a multi-MB buffer re-copies on every line. A deque
# capped by line count keeps appends O(1) and memory flat. The cap is high enough
# to hold the full output of all but pathologically chatty jobs (e.g. a per-op
# profiler spam loop); those keep only the most recent lines in the result, while
# the log file still has everything.
JOB_CAPTURE_MAX_LINES = 50000

JOB_ID_MODULUS = 1000  # ids are "000".."999"


def timeout_hint(timeout_sec: int) -> str:
    """Message for a timed-out job. Names the limit that fired, and — at the ceiling — says
    the one thing that is actually true: the answer is not a bigger number.

    Below the cap this points at the knob. AT the cap it must not, because there is no knob
    left and offering one would only teach the next agent to ask for it. A run that cannot
    finish in 25 minutes on a shared device is a run that has to be reshaped, and it always
    can be."""
    if timeout_sec >= MAX_TIMEOUT_SEC:
        return (
            f"Auto-killed: this run hit the HARD MAXIMUM of {MAX_TIMEOUT_SEC}s "
            f"({MAX_TIMEOUT_SEC // 60} minutes). THIS LIMIT CANNOT BE RAISED.\n\n"
            f"MOVE WORK OFF THE DEVICE, OR BREAK THE WORK INTO PIECES. IT IS ALWAYS POSSIBLE.\n\n"
            f"Compilation, data prep, model loading, analysis and plotting do not need silicon "
            f"— run them off-device and cache the result. What is left is the part that does, "
            f"and it fits: run one test, one shape, or one stage per job."
        )
    return (
        f"Auto-killed: this run exceeded its {timeout_sec}s timeout. "
        f"If you know it should legitimately take longer, re-run with a higher "
        f"timeout: `tt-device-mcp run -t <seconds> ...` (CLI), or pass timeout_sec "
        f"(MCP). Hard maximum {MAX_TIMEOUT_SEC}s ({MAX_TIMEOUT_SEC // 60} min) — beyond that "
        f"the work must be moved off-device or split up."
    )


async def _json_body(request: Request) -> dict:
    """Every REST route reads the body via ``.get(key, default)``, so a bodyless or
    non-JSON POST — a bare health probe — means "use the defaults", not a 500."""
    try:
        return await request.json()
    except Exception:
        return {}


def _read_new(path: str, offset: int) -> tuple[str, int]:
    """Read bytes appended to ``path`` since ``offset``. Returns (text, new_offset).
    Meant to run off the event loop (via to_thread) so a chatty job's log doesn't
    block request handling."""
    try:
        with open(path, "r") as f:
            f.seek(offset)
            text = f.read()
            return text, f.tell()
    except Exception:
        return "", offset


def _tail_lines(path: str, n: int) -> list[str]:
    """Last ``n`` lines of ``path`` without loading the whole file — reads from the
    end in blocks. Off-loop helper (a job log can be gigabytes)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= n:
                step = min(65536, end)
                end -= step
                f.seek(end)
                data = f.read(step) + data
        return data.decode("utf-8", errors="replace").rstrip().split("\n")[-n:]
    except Exception:
        return []


# ============== Pydantic Input Models ==============
# These provide rich schema information for MCP clients (LLMs) to understand tool parameters


class JobSubmitInput(BaseModel):
    """Input for submitting a job to the Tenstorrent device queue."""

    workspace: str = Field(
        ...,
        description=(
            "Absolute path to workspace directory containing tt-metal. "
            "Example: '/localdev/bjones/workspaces/my_feature'"
        ),
    )
    command: str = Field(
        ...,
        description=(
            "Shell command to execute in the workspace. "
            "Examples: 'pytest tests/test_llama.py -v', "
            "'python models/demos/llama3/demo.py'"
        ),
    )
    env: Optional[str] = Field(
        None,
        description=(
            "Path to YAML environment file (relative to workspace or absolute). "
            "Contains key-value pairs for environment variables. "
            "If omitted, uses workspace defaults (TT_METAL_HOME, etc.)."
        ),
    )
    inherited_env: Optional[dict] = Field(
        None,
        description=(
            "Environment variables inherited from caller (CLI captures these automatically). "
            "Dict of {VAR_NAME: value}. Takes precedence over env file."
        ),
    )
    timeout_sec: int = Field(
        DEFAULT_TIMEOUT_SEC,
        ge=1,
        le=MAX_TIMEOUT_SEC,
        description=(
            f"Maximum runtime in seconds before auto-kill. Default: {DEFAULT_TIMEOUT_SEC} "
            f"(10 minutes). Hard maximum {MAX_TIMEOUT_SEC}s ({MAX_TIMEOUT_SEC // 60} minutes) — "
            f"a run that cannot fit is reshaped (moved off-device or split), not given a bigger number."
        ),
    )


class JobRunInput(JobSubmitInput):
    """Input for running a job and waiting for completion (blocking)."""

    output_lines: int = Field(
        20,
        ge=0,
        le=1000,
        description="Number of output lines to include in the result. Default: 20. Set to 0 to omit output.",
    )


class JobIdInput(BaseModel):
    """Input for operations that require a job ID."""

    job_id: str = Field(
        ...,
        description=(
            "Job identifier returned by tt_device_job_run or tt_device_job_run_bg. " "Format: 3 digits (e.g., '042')."
        ),
    )


class JobWaitInput(JobIdInput):
    """Input for waiting on a job."""

    stream_logs: bool = Field(
        True,
        description="If true, stream log output in real-time as the job runs. Default: true.",
    )
    output_lines: int = Field(
        20,
        ge=0,
        le=1000,
        description="Number of output lines to include in the final result. Default: 20.",
    )


class JobLogsInput(JobIdInput):
    """Input for fetching job logs."""

    tail: int = Field(
        100,
        ge=1,
        le=10000,
        description="Number of lines to return from end of log file. Default: 100.",
    )


class JobKillInput(BaseModel):
    """Input for killing/cancelling a job."""

    job_id: str = Field(
        ...,
        description="Job identifier to kill or cancel.",
    )


class DeviceExecInput(BaseModel):
    """Input for direct device command execution."""

    command: str = Field(
        ...,
        description=(
            "Shell command to execute directly (not queued). "
            "For diagnostic tools only. Examples: 'tt-smi', 'tt-smi -r 0', 'tt-triage 0'."
        ),
    )
    timeout_sec: int = Field(
        180,
        ge=1,
        le=EXEC_MAX_TIMEOUT_SEC,
        description=(
            "Seconds before the command is killed. Default 180 — triage of a wedged "
            f"mesh outruns 60s. Hard ceiling {EXEC_MAX_TIMEOUT_SEC}s."
        ),
    )
    force: bool = Field(
        False,
        description=(
            "Run even when another tenant owns the running job. For triaging a foreign "
            "hung job that is wedging the shared device: the diagnostic runs concurrently "
            "with theirs, so it is the caller's judgement that it is read-only enough to be "
            "safe. The foreign owner and command are logged first. Default: refuse."
        ),
    )


class DeviceResetInput(BaseModel):
    """Input for resetting the Tenstorrent device(s)."""

    force: bool = Field(
        False,
        description=(
            "Override the reset gate even if another user's process is holding "
            "the device. DANGEROUS: a board-level reset will abort their run and "
            "can wedge the mesh. The foreign holders are logged before reset. "
            "Default: false (refuse if any foreign-uid holder is on the device)."
        ),
    )


def get_machine_name() -> str:
    """Get machine name from hostname, stripping -special suffix if present."""
    hostname = socket.gethostname()
    if "-special" in hostname:
        return hostname.split("-special")[0]
    return hostname


def percentile(values: list[float], p: float) -> float:
    """Calculate percentile of a sorted list."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * (p / 100)
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_values) else f
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f]) if f != c else sorted_values[f]


# Precompiled regex for stripping log prefixes
_LOG_PREFIX_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*\[(stdout|stderr)\]\s*")


def strip_log_prefix(line: str) -> str:
    """Strip timestamp prefix like '[HH:MM:SS] [stdout] ' from log lines."""
    return _LOG_PREFIX_RE.sub("", line)


def authz_owner(reported_owner: str | None) -> tuple[str, bool]:
    """Resolve the authoritative owner for the current request.

    Over the unix socket the caller's real uid (from SO_PEERCRED) is the
    authority and the self-reported ``owner`` field is ignored for authz. Over
    HTTP there are no peer credentials, so we fall back to the reported owner
    (legacy behavior).

    Returns (owner, authenticated): ``authenticated`` is True when the owner was
    derived from peer credentials rather than the (spoofable) request field.
    """
    uid = current_peer_uid.get()
    if uid is not None:
        return username_for_uid(uid), True
    return (reported_owner or "unknown"), False


def _reset_action_owner() -> str:
    """Jobs-list owner for an operator-initiated reset.

    Derived, never reported. Keeping a caller's tag because it merely MATCHED their uid let a
    CLI request carry `[agent]<self>` and be believed, which is the forgery the derivation exists
    to stop.
    """
    return submitting_owner()


def privsep_identity_error() -> dict | None:
    """Error dict if privsep is on but the caller has no peer identity, else None.

    Under privsep a job must run as its real submitter, which we only know from
    SO_PEERCRED on the unix socket. Rather than silently run an identity-less
    (HTTP) submission as the broker's own uid (root), refuse it and point the
    caller at the socket.
    """
    if privsep_enabled() and current_peer_uid.get() is None:
        return {
            "error": "privsep is enabled: connect over the unix socket (tt-device-mcp), not HTTP — "
            "there is no peer identity to run the job as.",
        }
    return None


AGENT_OWNER_PREFIX = "[agent]"


def submitting_owner() -> str:
    """The jobs-list owner for this request: `[agent]<user>` from an agent, `<user>` from the CLI.

    Both come from the same peer uid; only the tag differs, and it is derived from which surface
    the request arrived on (`current_via_mcp`), never from a field the caller sends. It records
    which surface was used, nothing more: an agent that shells out to the CLI is indistinguishable
    from a person, so this is attribution and never an authz input.

    Without a peer identity there is nobody to attribute to, so the label stays `unknown` rather
    than becoming `[agent]unknown` — a string that names no one and that `owner_matches` would
    read as every other HTTP caller's own.
    """
    user, authenticated = authz_owner(None)
    if not authenticated:
        return user
    return f"{AGENT_OWNER_PREFIX}{user}" if current_via_mcp.get() else user


def owner_matches(job_owner: str, caller_owner: str) -> bool:
    """Match a job's owner against the caller, allowing the [agent]<user> form.

    An agent's job is tagged ``[agent]<user>`` while the peer-credential owner is the bare
    ``<user>``; the same person should manage either.
    """
    if job_owner == caller_owner:
        return True
    return job_owner == f"{AGENT_OWNER_PREFIX}{caller_owner}"


@dataclass
class Stats:
    """Session statistics for the device queue."""

    machine: str = field(default_factory=get_machine_name)
    session_start: str = field(default_factory=lambda: datetime.now().isoformat())
    session_end: Optional[str] = None

    # Device timing
    busy_sec: float = 0.0
    idle_sec: float = 0.0
    last_state_change: str = field(default_factory=lambda: datetime.now().isoformat())
    device_busy: bool = False

    # Job counts
    jobs_completed: int = 0
    jobs_failed: int = 0
    jobs_killed: int = 0
    jobs_timeout: int = 0

    # Wait times (all values for percentile calculation)
    wait_times: list[float] = field(default_factory=list)

    def update_device_state(self, now_busy: bool):
        """Update device busy/idle tracking when state changes."""
        now = datetime.now()
        last_change = datetime.fromisoformat(self.last_state_change)
        elapsed = (now - last_change).total_seconds()

        if self.device_busy:
            self.busy_sec += elapsed
        else:
            self.idle_sec += elapsed

        self.device_busy = now_busy
        self.last_state_change = now.isoformat()

    def record_job_completion(
        self, status: "JobStatus", wait_sec: Optional[float], runtime_sec: Optional[float] = None
    ):
        """Record a job completion.

        Also reports the Prometheus jobs_total outcome in the same branches this already counts
        in, PLUS one Prometheus-only branch for JobStatus.HUNG: the legacy
        jobs_completed/failed/killed/timeout counters below still never count it (that CLI-facing
        gap predates this task and stays as-is), and a hung job is typically the LONGEST device
        occupancy — a dashboard reader deserves to see it as its own outcome. Timing publishes
        from the snapshot (wait quantiles, the busy/free occupancy clock), not from here."""
        if status == JobStatus.COMPLETED:
            self.jobs_completed += 1
            metrics.job_completed("completed")
        elif status == JobStatus.FAILED:
            self.jobs_failed += 1
            metrics.job_completed("failed")
        elif status == JobStatus.KILLED:
            self.jobs_killed += 1
            metrics.job_completed("killed")
        elif status == JobStatus.TIMEOUT:
            self.jobs_timeout += 1
            metrics.job_completed("timeout")
        elif status == JobStatus.HUNG:
            metrics.job_completed("hung")

        if wait_sec is not None:
            self.wait_times.append(wait_sec)

    @property
    def utilization(self) -> float:
        """Calculate device utilization as percentage (0-100)."""
        total = self.busy_sec + self.idle_sec
        return (self.busy_sec / total * 100) if total > 0 else 0.0

    @property
    def total_jobs(self) -> int:
        """Total number of completed jobs."""
        return self.jobs_completed + self.jobs_failed + self.jobs_killed + self.jobs_timeout

    @property
    def wait_p50(self) -> float:
        """50th percentile wait time."""
        return percentile(self.wait_times, 50)

    @property
    def wait_p95(self) -> float:
        """95th percentile wait time."""
        return percentile(self.wait_times, 95)

    @property
    def wait_max(self) -> float:
        """Maximum wait time."""
        return max(self.wait_times) if self.wait_times else 0.0

    @property
    def wait_total(self) -> float:
        """Total wait time across all jobs."""
        return sum(self.wait_times)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        # Finalize timing to include current state
        now = datetime.now()
        last_change = datetime.fromisoformat(self.last_state_change)
        elapsed = (now - last_change).total_seconds()
        final_busy = self.busy_sec + (elapsed if self.device_busy else 0)
        final_idle = self.idle_sec + (elapsed if not self.device_busy else 0)
        total = final_busy + final_idle

        return {
            "machine": self.machine,
            "session_start": self.session_start,
            "session_end": self.session_end,
            "last_update": now.isoformat(),
            "device": {
                "busy_sec": round(final_busy, 1),
                "idle_sec": round(final_idle, 1),
                "utilization": round(final_busy / total * 100, 1) if total > 0 else 0.0,
            },
            "jobs": {
                "completed": self.jobs_completed,
                "failed": self.jobs_failed,
                "killed": self.jobs_killed,
                "timeout": self.jobs_timeout,
                "total": self.total_jobs,
            },
            "waits": {
                "p50": round(self.wait_p50, 1),
                "p95": round(self.wait_p95, 1),
                "max": round(self.wait_max, 1),
                "total": round(self.wait_total, 1),
            },
        }


# Setup logging
try:
    from zoneinfo import ZoneInfo

    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # tzdata missing — degrade to UTC-only rather than crash logging
    _PACIFIC = None


class _DualTZFormatter(logging.Formatter):
    """Stamp each record with both UTC and Pacific wall-clock so logs are readable
    by a UTC host and a Pacific reader without mental conversion, e.g.
    ``2026-06-11 15:25:04 UTC / 08:25:04 PDT``."""

    def formatTime(self, record, datefmt=None):  # noqa: ARG002 - datefmt unused by design
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        utc = dt.strftime("%Y-%m-%d %H:%M:%S")
        if _PACIFIC is None:
            return f"{utc} UTC"
        return f"{utc} UTC / {dt.astimezone(_PACIFIC).strftime('%H:%M:%S %Z')}"


def setup_logging(log_file: Path) -> logging.Logger:
    """Configure file-based logging."""
    root = logging.getLogger()
    root.handlers.clear()

    formatter = _DualTZFormatter(fmt="%(asctime)s | %(levelname)-5s | %(message)s")

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)

    root.setLevel(logging.INFO)
    root.addHandler(file_handler)

    logger = logging.getLogger("tt-device-mcp")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(file_handler)

    return logger


class JobStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    HUNG = "hung"
    KILLED = "killed"


@dataclass
class Job:
    id: str  # "000".."999" (see next_job_id)
    owner: str  # "bjones" or "[agent]bjones"
    workspace: str
    command: str
    queued_at: str  # ISO timestamp
    peer_uid: Optional[int] = None  # SO_PEERCRED uid of the submitter (privsep target); None over HTTP
    env: Optional[str] = None  # Path to env file
    env_vars: dict = field(default_factory=dict)  # Resolved env vars (for logging/MCP)
    status: JobStatus = JobStatus.QUEUED
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    output: str = ""  # materialized from out_buf when the job ends
    error: str = ""
    # Bounded live capture during the run (full output goes to the log file).
    out_buf: deque = field(default_factory=lambda: deque(maxlen=JOB_CAPTURE_MAX_LINES))
    err_buf: deque = field(default_factory=lambda: deque(maxlen=JOB_CAPTURE_MAX_LINES))
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    last_output_monotonic: float = 0.0  # set on every line; the hung watchdog's clock
    doomed_monotonic: Optional[float] = None  # first sight of an unrecoverable-device line, if any
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    log_file: Optional[str] = None

    @property
    def runtime_sec(self) -> Optional[float]:
        """Calculate runtime in seconds, or None if not started/finished."""
        if self.started_at and self.finished_at:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at)
            return (end - start).total_seconds()
        return None

    @property
    def wait_sec(self) -> Optional[float]:
        """Calculate queue wait time in seconds, or None if not started."""
        if self.queued_at and self.started_at:
            queued = datetime.fromisoformat(self.queued_at)
            started = datetime.fromisoformat(self.started_at)
            return (started - queued).total_seconds()
        return None


# Server state (shared across all connections)
jobs: dict[str, Job] = {}
job_queue: asyncio.Queue[str] | None = None  # Lazy init - created at runtime
job_counter: int = 0  # Monotonic counter for job IDs; wraps at JOB_ID_MODULUS
current_process: asyncio.subprocess.Process | None = None
current_job_id: str | None = None  # job_id of current_process, so a kill can scope-route it
lock: asyncio.Lock | None = None  # Lazy init - created at runtime


def _update_queue_depth_metric() -> None:
    """Refresh the Prometheus queue_depth gauge from the live QUEUED-job count.

    Recomputed fresh from ``jobs`` at each mutation point, rather than incremented/decremented —
    a snapshot cannot drift, and the gauge is exported on the stats-persistence textfile cadence
    (not scraped live), so a fresh recompute at each of the few call sites (plus the persistence
    loop itself as a safety net) costs nothing extra."""
    metrics.queue_depth.set(sum(1 for j in jobs.values() if j.status == JobStatus.QUEUED))


def _publish_telemetry_snapshot() -> None:
    """Read the live subsystems into one BrokerTelemetry and push it to the gauges.

    On the stats cadence, immediately before the textfile render, so what an operator scrapes is
    the broker's state at write time rather than whatever the last transition happened to leave
    behind. Never raises — see BrokerTelemetry.publish."""
    telemetry.snapshot(
        fsm,
        health_monitor,
        recovery_mechanism,
        stats,
        queue_depth=sum(1 for j in jobs.values() if j.status == JobStatus.QUEUED),
        isolated_chips=len(isolated_chips),
    ).publish()


def _current_boot_id() -> str:
    """This boot's kernel id, or '' if unreadable. A new boot means a new id, which is how the
    per-boot escalation cap resets across a reboot with no state to clear — and how ``fsm``
    (below) tells a job-scoped fact left over from before a reboot apart from one still live
    within the same boot. Defined here, ahead of ``fsm``'s construction, rather than down with
    the rest of the boot-id/reboot-history helpers: everything else that reads it does so through
    a lazy lambda resolved long after import, but ``fsm`` needs the value itself, now."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


# The durable BOOT/HEALTHY/RECOVERING/DOWN record: whether the device is fit for a tenant, the
# closed-enum reason when it is not, and the episode clock/latches every hold-related call site
# below reads and writes through. Constructed once, here, next to recovery_mechanism/
# health_monitor below — never per gate pass, for the same reason those are not: a fresh instance
# would lose the episode it is mid-way through recording.
fsm = ServerFsm(health_dir() / "fsm.json", current_boot_id=_current_boot_id())

# why values the idle relift may re-verify READ-ONLY and lift without a reset: a frozen active-eth
# core, or an off-bus drop held below the reset floor. Both self-heal; nothing else
# does. "eth_frozen" needs a confirmed-advancing eth heartbeat to lift (enum+ARC+sysfs are blind to
# it); "off_bus" needs only enum+ARC+sysfs, which see a chip return directly.
SELFHEAL_WHYS = frozenset({"eth_frozen", "off_bus"})
# The one why the idle relift re-runs the (perturbing) fabric traffic pass for, because a fabric
# that could only not-run (a 77) is proven fit only by a pass that gets a real verdict.
FABRIC_RELIFT_WHY = "fabric_unverified"
# why values with no read-only story at all — a foreign holder that blocked verification, a gate
# that errored out, or a startup boot still awaiting its first fabric pass. enum+ARC prove nothing
# these were placed for, so they never lift on a read; past the ceiling they escalate to the gate's
# own recovery ladder instead of standing until a broker restart.
GENERIC_ESCALATE_WHYS = frozenset({"gate_error", "foreign_holder", "startup_unverified"})

# Set when the RUNTIME named a device fault in a job's output. Outranks our own checks,
# which are blind to this class of fault, and only a reset retires it. Independent of the FSM:
# a fault a job's own log named stands until a reset clears it regardless of what a later,
# unrelated gate pass proves the mesh's enum/ARC/fabric state to be.
device_fault_reported: str = ""

# Every path that touches the device serializes on this: the health gate, the
# fabric traffic check, and the reset tool. Without it a post-job gate and an
# operator's reset issue concurrent -glx_reset calls at the same 32 ASICs, which
# is how a single wedged chip becomes a mesh that only a power-cycle recovers.
# One device, one operation.
device_op_lock: asyncio.Lock | None = None
# Non-empty while a device op holds the lock. These are surfaced as RUNNING work: a
# reset or a fabric pass owns the device for 45-60s, and a queue that shows nothing
# running while nothing can start is the broker looking hung to the person waiting.
device_op_active: str = ""
device_op_started_at: str = ""
device_op_detail: str = ""
# When the CURRENT stage of that op began. The op start and the stage start are different
# clocks and answer different questions ("how long has the device been held?" vs "how long
# has this check been running?"), and a reader shown one against the other's label reads a
# hung check: a 45s fabric pass on its second attempt, labelled with the 12-minute total,
# is a fabric pass that looks stuck when it is 40s old and fine.
device_op_stage_started_at: str = ""
# Who asked for the in-flight device op. The broker for its own gates; the operator for a
# reset they ran, so the running row names them while it runs and not only once it is over.
device_op_owner: str = "[broker]"

# Every reservation an external scheduler's pre-step/post-step currently holds against job
# dispatch, keyed by a token unique to that one reservation. A plain flag/refcount is not enough:
# an Epilog and the next Prolog (or an operator's own `post-step` beside the hook) can each hold a
# reservation concurrently — both pass the in-flight check during the other's pre-`_device_op`
# window (for `post-step` that is the reclaim, which sleeps for its grace period) — and whichever
# gate finishes first must not free the device out from under the other's still-running one.
# Keying by token makes a double release (a bug, or a caller racing its own cleanup) a no-op
# instead of an under-count: popping a token that is already gone does nothing, so the device
# stays reserved for as long as ANY genuine holder remains. Cleared per-token only when that
# token's gate task actually finishes — see _release_external_step — not when that step's own
# HTTP reply is sent, so a deadline-expired "inconclusive" reply does not free a device the gate
# is still touching in a background thread.
_external_step_holders: dict[int, str] = {}
_external_step_token_seq: int = 0
# Display string derived from _external_step_holders — every current holder's phase name, comma
# joined — kept as its own global because it is what the queue/status surface and job_runner's own
# deferred-dispatch log show; "" when no step holds a reservation. Not the source of truth (that
# is _external_step_holders); recomputed by _reserve_external_step/_release_external_step.
external_step_active: str = ""
# Set (free) when no step holds a reservation; cleared while at least one does. Lazily created by
# get_external_step_free_event(), the same reason get_device_op_lock() is lazy: an asyncio
# primitive binds to whichever event loop first awaits it, and that must be the loop actually
# running at the time, not import time.
external_step_free_event: asyncio.Event | None = None

# The ledger identity a broker sub-action (a reset, a fabric pass) reserves the moment it begins:
# its id, its start, its name. The in-flight row and the durable row it becomes both read from
# this, so one stable row — same id, same start, same name — represents the sub-action from first
# appearance to last, instead of a synthetic `--` row that the durable write then supersedes with a
# fresh id, a reconstructed start, and a different name. Cleared when the sub-action's durable row
# is written (and on op-scope exit, in case one began but never wrote). One at a time: the device
# op lock serializes sub-actions, so there is never more than one reservation outstanding.
_action_row: Optional[dict] = None

# The same reservation for a HOLD episode: its id, start, and name, taken when the device first
# refuses a tenant. A hold is a latched state, not a bracketed op, but the ledger invariant is the
# same — the live row an operator watches while the box sits refused must be the row that lands
# when it releases, not a synthetic `--` the durable write supersedes with a fresh id and a
# different name. Held across recovery ops (which suppress the hold row for their own), and cleared
# when the episode ends.
_hold_row: Optional[dict] = None

# When the device entered a HELD state: degraded (a chip off the bus, or dirty and
# unverified) with no broker op running. Latched so the synthetic HOLD row can report how
# long the device has sat refused to tenants, and cleared the moment it is fit again — or a
# broker op takes over the RUNNING row (that op IS the recovery, and owns the row while it runs).
device_held_since: str = ""

# Whether the CURRENT tenant-refused episode has been written to the durable health
# timeline. The synthetic HOLD row lives only in memory and the per-job refusals only in
# the jobs list; neither survives a broker restart, and a spontaneous chip drop the live
# probe caught opens no fsm episode either. This latch drives one durable
# `device_held`/`device_released` pair per episode — the trace of when the device went bad
# and recovered — without a health event per refused job.
device_hold_logged: bool = False
# The open hold episode: when it started and what it was for. The live HOLD row is synthetic
# and dies with the hold, so this is what lets the episode be written to the ledger once it
# has an end and a duration.
device_hold_episode_since: str = ""
device_hold_episode_reason: str = ""
# The per-tray BMC reset's once-per-episode limit and the idle escalation's ran-at-all marker live
# as named latches on ``fsm`` (see RecoveryDeps below): ``fsm.latch("escalated")`` (visibility for
# the deadline watchdog — the present-mesh escalation itself retries on the grace cadence) and
# ``fsm.latch("ubb_reset_fired")`` (a hard cap — a tray walk that did not recover must not loop),
# re-armed whenever ``fsm`` opens a fresh episode.

# When the escalation latch was set (monotonic, process-local exactly as the ladder needs it: a
# restart is a fresh chance to climb, never a reason to stay blocked). 0.0 = not latched.
device_hold_escalated_monotonic: float = 0.0


def _hold_escalation_latched() -> bool:
    """Whether this episode's escalation latch still blocks another climb.

    The latch itself is fsm's; what this adds is that it EXPIRES: after
    ``HOLD_ESCALATION_REARM_SEC`` a still-RECOVERING episode may climb the guarded ladder again.
    The expiry matters only for an episode the ladder never actually climbed (a declined fire —
    tenant present, cooldown, unreadable scan — or a restored cross-reboot latch): a climb that
    RAN and left the box degraded folds to OUTCOME_TERMINAL -> DOWN, where the guarded relift is
    disarmed and the forced hold-deadline watchdog (which bypasses this latch) is what keeps
    climbing, once per ceiling window."""
    if not fsm.latch("escalated"):
        return False
    if time.monotonic() - device_hold_escalated_monotonic >= HOLD_ESCALATION_REARM_SEC:
        return False
    return True


def _set_hold_escalated(value: bool) -> None:
    """Set fsm's escalation latch and stamp its expiry clock — one fact, one writer."""
    global device_hold_escalated_monotonic
    fsm.set_latch("escalated", value)
    device_hold_escalated_monotonic = time.monotonic() if value else 0.0


# Which deadline window (_hold_deadline_sec) this episode's stuck-hold alert has already fired for,
# so the watchdog writes one durable event per window rather than one per sample. Re-arms (0) with
# the episode clock the moment the device comes back fit — the next episode gets its own deadline.
device_hold_deadline_bucket: int = 0
# Which ceiling window this episode has already FORCE-ESCALATED for. The relift and the class
# escalators fail closed to "keep holding" on their guards (an unconfigured eth reader, a last-fabric
# verdict that never re-runs on an idle box, an unreadable tenant scan, the one-reset latch), which is
# how a held box sat 21 h. Past the ceiling a held device has no tenant to protect, so this drives an
# UNCONDITIONAL run of the recovery ladder, once per window, until it recovers or reaches the loud
# terminal rung — the mechanical guarantee that a hold always terminates. Re-arms (0) with the clock.
device_hold_escalate_bucket: int = 0
# One early forced escalation per off-bus episode, tracked apart from the ceiling windows so it adds
# an attempt rather than consuming one (see _offbus_hold_ceiling_sec).
device_hold_offbus_escalated: bool = False
# Which watchdog window spawned the forced escalation now in flight, in the words its log line
# names: the off-bus one-shot or a ceiling window. Set by _check_hold_deadline right before each
# spawn so the escalation reports the clock that actually fired rather than the general ceiling.
device_hold_escalation_trigger: str = ""
_forced_escalation_task: "Optional[asyncio.Task]" = None
# Jobs a reset SIGKILLed to take the device. Their deaths are ours, not the mesh's: a reset
# kills the running job, the job dies mid-CCL, and that death then reads as evidence the
# device needs resetting — by the reset that caused it. The set is drained as each is seen.
reset_killed_job_ids: set = set()

# Everything the health package needs FROM this module: server-state accessors wrapped in
# lambdas over this module's OWN globals (not bound directly) so a test that monkeypatches e.g.
# ``server._current_boot_id`` still takes effect — the lambda re-resolves the bare name from this
# module's namespace on every call, exactly like the functions that used to read these globals
# directly. fsm.boot() below builds the health subsystem from this one bag
# (board_types_provider/glx_board_types_provider/journal_skip_once are left to their None
# defaults — they read the HealthMonitor boot() is building, so it wires those itself; see
# ServerFsm.boot's own docstring).
_recovery_deps = RecoveryDeps(
    current_boot_id=lambda: _current_boot_id(),
    boot_btime_id=lambda: _boot_btime_id(),
    scoped_reset_backend=lambda: _scoped_reset_backend(),
    set_device_pollers=lambda active, log: _set_device_pollers(active, log),
    set_device_op_detail=lambda detail: _set_device_op_detail(detail),
    begin_action_row=lambda *a, **k: _begin_action_row(*a, **k),
    write_action_log=lambda *a, **k: write_action_log(*a, **k),
    device_hold_episode_since=lambda: device_hold_episode_since,
    auto_reboot_enabled=lambda: _auto_reboot_enabled(),
    auto_power_cycle_enabled=lambda: _auto_power_cycle_enabled(),
    terminate_process_group=lambda pid: _terminate_process_group(pid),
    logger=lambda: logger,
    present_chip_indices=lambda: _present_chip_indices(),
    enumerate_device_holders=lambda: enumerate_device_holders(),
    job_running=lambda: any(j.status == JobStatus.RUNNING for j in jobs.values()),
    isolated_chips=lambda: isolated_chips,
    device_pci_map=lambda: device_pci_map,
    device_hold_episode_reason=lambda: device_hold_episode_reason,
    device_hold_episode_escalated=lambda: fsm.latch("escalated"),
    hold_escalation_latched=lambda: _hold_escalation_latched(),
    set_device_hold_episode_escalated=_set_hold_escalated,
    device_hold_episode_ubb_reset_fired=lambda: fsm.latch("ubb_reset_fired"),
    set_device_hold_episode_ubb_reset_fired=lambda v: fsm.set_latch("ubb_reset_fired", v),
    clear_device_reported_fault=lambda why: _clear_device_reported_fault(why),
    clear_device_dirty=lambda **kw: _clear_device_dirty(**kw),
    device_degraded=lambda: _recovery_degraded(),
    auto_power_cycle_host=lambda log, reason: _auto_power_cycle_host(log, reason),
    emit_all_off_bus_power_cycle_required=lambda log, off_bus, expected, context: (
        _emit_all_off_bus_power_cycle_required(log, off_bus, expected, context=context)
    ),
    host_escalation_kwargs=lambda: _host_escalation_kwargs(),
    read_heartbeats=lambda: read_heartbeats(),
    heartbeat_supported=lambda: heartbeat_supported(),
    heartbeat_verdict=lambda expected: heartbeat_verdict(expected),
    gone_chip_confirm_settle_sec=lambda: GONE_CHIP_CONFIRM_SETTLE_SEC,
    chip_sample=lambda: chip_sample(),
    append_trace=lambda s: append_trace(s),
    capture_incident=lambda *a, **k: capture_incident(*a, **k),
    isolate_chip=lambda idx: isolate_chip(idx),
    chip_pci_bdf=lambda idx: chip_pci_bdf(idx),
    health_event=lambda kind, **f: health_event(kind, **f),
    kill_device_holders=lambda reason: _kill_device_holders(reason),
    mark_device_dirty=lambda reason, **kw: _mark_device_dirty(reason, **kw),
    refresh_idle_hold_ledger=lambda: _refresh_idle_hold_ledger(),
    spawn_idle_relift=lambda: _maybe_spawn_idle_relift(),
    episode_dirty=lambda: fsm.record.dirty,
    episode_job=lambda: dict(fsm.record.job),
)

# The root constructs the health subsystem, once, while still in its BOOT state — never per gate
# pass, or a fresh HealthMonitor/RecoveryMechanism would lose the fabric-verdict latch / reset
# cooldown each already carries by the next call (see ServerFsm.boot's own docstring). The module
# names below are aliases onto the root's members, kept because they are this module's test seams:
# tests monkeypatch srv.galaxy_recovery/srv.per_target_recovery/srv.recovery_mechanism by name,
# and the gate reaches galaxy_recovery's Galaxy-only rungs (the per-tray UBB reset, the host
# reboot/power-cycle escalation, the post-reboot verify) through the alias directly.
health_monitor: "Optional[HealthMonitor]" = None  # noqa: F821
recovery_mechanism: "Optional[RecoveryMechanism]" = None  # noqa: F821
galaxy_recovery: "Optional[GalaxyRecovery]" = None  # noqa: F821
per_target_recovery: "Optional[PerTargetRecovery]" = None  # noqa: F821
select_recovery: "Optional[Callable[[], Recovery]]" = None  # noqa: F821
sampler: "Optional[TelemetrySampler]" = None  # noqa: F821
_booted = False


# Bound on the boot-time platform snapshot. Well under the 90s a post-reset verify may take: this
# one runs before the broker serves anything, and a mesh too wedged to name its own boards inside
# this window is one the per-pass fallback should keep re-deriving rather than stall the boot on.
BOOT_PLATFORM_PROBE_TIMEOUT_SEC = float(os.environ.get("TT_DEVICE_MCP_BOOT_PROBE_TIMEOUT_SEC", "20").strip() or "20")


def _boot_platform_probe() -> None:
    """One read-only tt-smi snapshot, purely to cache board types for the platform verdict.

    ``expected_count=0`` because the count is not the question here — a degraded mesh still tells
    us what boards it has, and refusing to resolve the platform just because chips are missing
    would deny the ladder its identity on exactly the host that needs recovering."""
    health_monitor.verify_device_health(0, timeout_sec=BOOT_PLATFORM_PROBE_TIMEOUT_SEC)


def boot_broker(*, probe_platform: bool = False) -> Optional[str]:
    """The broker's boot flow: construct the health subsystem, then fix this host's platform.

    Runs at broker start rather than at import, because resolving the platform means a tt-smi
    snapshot and importing a module must never touch the device — the test suite and any CLI that
    merely imports this module would both shell out. Idempotent, so the several entry points that
    reach the server (the transports, the stdio shim, a test fixture) can each call it blind.

    ``probe_platform`` is the device-touching half, so a caller that has no device (or must not
    touch one) leaves it off and gets construction only; the platform then resolves per pass as
    before. Returns the resolved mode, or None when it stayed open.
    """
    global health_monitor, recovery_mechanism, galaxy_recovery, per_target_recovery
    global select_recovery, sampler, _booted
    if not _booted:
        fsm.boot(_recovery_deps)
        health_monitor = fsm.monitor
        recovery_mechanism = fsm.mechanism
        galaxy_recovery = fsm.galaxy
        per_target_recovery = fsm.per_target
        select_recovery = fsm.select_recovery
        sampler = fsm.sampler
        _booted = True
    return fsm.resolve_platform(_boot_platform_probe if probe_platform else None)


# A device-saturating job drained back-to-back with the next, with no gap, is what walked a
# marginal tray off the bus: hours of continuous 32-chip CCL/fabric traffic and zero idle
# between runs pushed chips 0-7 off PCIe under sustained load. This is the minimum the mesh is
# guaranteed to rest between one job's device work ending and the next job's beginning. It is not
# per-job classified — the broker cannot tell a saturating job from a light one, so an armed host
# pays it between every consecutive pair. Default 0 (off): it trades tenant throughput for
# thermal/link headroom, so like the cold rungs it is the host operator's opt-in, not fleet-wide.
JOB_COOLDOWN_SEC = float(os.environ.get("TT_DEVICE_MCP_JOB_COOLDOWN_SEC", "0"))
# When the last job's device work (including its post-job fabric pass) finished, in monotonic
# seconds. 0.0 until a job ends this process, so the first job after a boot never waits.
last_job_end_monotonic: float = 0.0

# Per-owner submission burst cap. The incident's LTX vbench sweep queued ten 32-chip jobs in
# ~90s — one every ~11s — and draining them back-to-back with no idle is what walked a tray off
# the bus. This caps how fast a single owner may ADMIT work: a burst is refused at submission,
# before it fills the queue, rather than throttled at dispatch after the run is committed and the
# damage begins. Default 0 (off): like the inter-job cooldown it trades one tenant's submission
# rate for mesh headroom, so it is the host operator's opt-in, not a fleet-wide admission change.
JOB_BURST_MAX = int(os.environ.get("TT_DEVICE_MCP_JOB_BURST_MAX", "0"))
JOB_BURST_WINDOW_SEC = float(os.environ.get("TT_DEVICE_MCP_JOB_BURST_WINDOW_SEC", "60"))
# owner -> monotonic timestamps of that owner's recent admitted submissions, pruned to the
# trailing window on every submit so it never grows past one window of entries per owner. Only
# populated while the cap is armed, so a default-off host accumulates nothing.
owner_submit_times: dict[str, list[float]] = {}

# The hold episode's start, on disk. In memory alone it resets on every broker restart — and the
# escalation ceiling is measured FROM it, so a box that restarts (a deploy, a crash, the reconcile
# timer) has its ladder clock silently rewound and never reaches the rung that would recover it.
# Measured: three consecutive laps held at 5/8 chips, the relift declining each time because the
# episode looked seconds old, while the queue sat empty so no gate ever ran either.
HOLD_EPISODE_FILE = "hold_episode_since"


def _persist_hold_episode(since: str) -> None:
    """Record (or clear) the episode start so a restart cannot rewind the escalation clock."""
    try:
        path = health_dir() / HOLD_EPISODE_FILE
        if since:
            path.write_text(since)
        elif path.exists():
            path.unlink()
    except OSError:
        pass  # never let bookkeeping break the gate


def _restore_hold_episode() -> str:
    """The episode start from a previous process, or '' if none. The caller decides whether the
    device is still degraded — a stale file on a recovered box must not resurrect a dead hold."""
    try:
        return (health_dir() / HOLD_EPISODE_FILE).read_text().strip()
    except OSError:
        return ""


# A surgical bridge reset fired seconds after a chip leaves the bus can lose the race
# with the endpoint's link retrain and report failure, yet the same chip re-binds in ~2s
# once the link settles. Retry the bridge reset a few times before escalating to a much
# heavier galaxy reset — this is what turns a ~13 min recovery into a ~seconds one. The
# retry COUNT moved with Recovery._recover_isolated_chips (Task 7); the settle stays here,
# injected via RecoveryDeps.bridge_reset_settle_sec, because tests monkeypatch it directly.
BRIDGE_RESET_SETTLE_SEC = 4

# Before treating a chip as GONE FROM THE BUS and resetting its bridge, confirm it is absent
# across two reads this far apart. A chip still enumerated but whose heartbeat read momentarily
# failed drops out of a single read the same way a truly-gone one does, and an SBR on its (live)
# bridge would knock a healthy chip off the bus — the two-sample discipline the sampler already
# enforces against a single untrusted sample. Used both by the gate below and by
# GalaxyRecovery._escalate_offbus_stuck_hold (injected via RecoveryDeps.gone_chip_confirm_settle_sec
# for the same monkeypatch reason as the constant above).
GONE_CHIP_CONFIRM_SETTLE_SEC = 2.0

# The fabric traffic pass is the only check that proves data moves between chips, and
# it costs ~45s. It runs whenever there is something to explain (a job ended badly, a
# reset needs proving) and, on clean exits, no more often than this.
#
# The interval is the whole trade: a wedged ethernet core is invisible to every cheap
# check — chips enumerate, ARC answers, the job exits 0 — and the next tenant is the one
# who meets it as "waiting for active ethernet core". This bounds how long that can hide.
#
# It is also device exposure. The traffic pass is the heaviest thing the broker does to the
# mesh, and a host was lost with one in flight — a chip fell off the PCIe bus 17s into it,
# and the stalled access to the dead endpoint took a CPU core down with it. That does not
# make the check the cause, but it is a reason not to run it more often than the coverage
# actually requires. On a clean exit, nothing is owed.
FABRIC_CHECK_MIN_INTERVAL_SEC = int(os.environ.get("TT_DEVICE_MCP_FABRIC_CHECK_INTERVAL_SEC", "1200"))
last_fabric_check_monotonic: float = 0.0
# The last REAL fabric verdict lives on health_monitor.last_fabric_ok now (see health_monitor
# above): True healthy / False unhealthy / None until one runs. Kept across checks so the
# read-only relift can refuse to lift a recovered-on-ARC hold back onto a fabric whose last
# verdict was unhealthy — enum+ARC recover before the eth cores do, and lifting there admits the
# next tenant onto a fabric that still cannot move data. A skip (None) never overwrites it: not
# knowing is not the same as knowing it is fit.

# Fix A recurrence backstop. A runtime eth fault ('waiting for active ethernet core') that the fabric
# traffic pass then clears (a real OK verdict) is retired immediately — the fabric check IS the eth/
# fabric validator, and waiting 20 min to galaxy-reset 32 healthy ASICs for a wedge it says is gone is
# waste. But the validator drives only one of each link's two eriscs, so a stuck SECOND-erisc core can
# pass the check: if the SAME fault keeps coming back fast despite the fabric passing each time, the
# pass is not reaching this wedge — stop retiring on it and let it escalate to the reset that re-inits
# both eriscs. This makes fix A safe whether or not the second-erisc blind spot is real.
FABRIC_RETIRE_RECURRENCE_SEC = 900  # a fault back within this of a fabric-OK retire counts as recurring
FABRIC_RETIRE_MAX_STREAK = 3  # after this many fast recurrences, the fabric-OK is not clearing it
_fabric_ok_retire_monotonic: float = 0.0
_fabric_ok_retire_streak: int = 0


def _relift_interval_from_env() -> int:
    """Seconds between idle self-heal re-verifies (see _attempt_idle_relift). A self-heal hold
    clears, so a couple of minutes reopens a recovered mesh promptly without
    spinning tt-smi at it. Parsed defensively: a malformed value reads as the default rather
    than crashing the broker at start, and it is floored at 1s so it can never busy-spin."""
    raw = os.environ.get("TT_DEVICE_MCP_SELFHEAL_RELIFT_SEC", "").strip()
    if not raw:
        return 120
    try:
        return max(1, int(raw))
    except ValueError:
        return 120


SELFHEAL_RELIFT_INTERVAL_SEC = _relift_interval_from_env()
last_relift_monotonic: float = 0.0
# Single-flight handle for the idle relift. It runs as its OWN task, never inline in the sampler,
# so the sampler keeps sampling and stays the dead-chip tripwire even while the relift's tt-smi/eth
# reads are in flight — a chip that drops mid-read is exactly what the sampler must SIGKILL the
# reader for, and it cannot if it is the blocked caller. Kept so a slow pass never overlaps itself.
_relift_task: Optional[asyncio.Task] = None


def _hold_deadline_sec() -> int:
    """Hard ceiling past which a still-standing hold is flagged STUCK to the durable timeline. A
    hold must terminate — in a verified recovery or the next rung — so one that outlives this is a
    bug by definition, and the timeline must SAY so rather than go silent after the opening
    device_held. Default is twice the escalation ceiling: a hold the idle escalation was going to
    clear is long gone by then, so only a genuinely stuck one (the escalation opted out, a
    persistent tenant or unreadable holder scan it defers on, its one reset already spent, or a
    killed relift) reaches it. Parsed defensively — a non-positive or malformed value reads as the
    default, never 0 (which would flag the instant a hold latched)."""
    raw = os.environ.get("TT_DEVICE_MCP_HOLD_DEADLINE_SEC", "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 2 * _stuck_hold_ceiling_sec()
    return v if v > 0 else 2 * _stuck_hold_ceiling_sec()


# Direct-exec diagnostics (tt-smi/tt-triage outside the queue) run under this prefix so a
# timed-out exec is reaped by its scope, not by killpg on the systemd-run wrapper — see
# _exec_impl. Each call takes a fresh sequence number so concurrent execs never collide.
EXEC_SCOPE_PREFIX = "ttdev-exec-"
exec_scope_seq: int = 0

# Chips cut out of the kernel because they left the PCIe bus, and the PCI address of every
# chip, cached while they are still alive. The cache matters: isolate_chip() unbinds the
# device, which takes its sysfs link — and with it the only way to find the bridge we need
# to reset it through. Resolve the map up front or lose the chip for good.
isolated_chips: set = set()
device_pci_map: dict = {}

# Daemons that continuously poll every chip. They are stopped for the duration of
# a reset: MMIO aimed at a chip that is mid-reset (or already dead) is what pushes
# the root complex into the fatal error path that reboots the host.
DEVICE_POLLER_SERVICES = tuple(
    s
    for s in os.environ.get("TT_DEVICE_MCP_POLLER_SERVICES", "tt-telemetry.service,tt-metrics-exporter.service").split(
        ","
    )
    if s.strip()
)
logger: logging.Logger | None = None
job_log_dir: Path | None = None
job_runner_task: asyncio.Task | None = None  # Singleton job runner
_startup_tasks_done = False  # run_startup_tasks() is once-per-process, whoever gets there first
readopted_scopes: dict[str, str] = {}  # job_id -> scope unit, for jobs re-adopted after a restart
stats: Stats | None = None  # Session statistics
stats_dir: Path | None = None  # Directory for stats files
stats_file: Path | None = None  # Current stats file
stats_task: asyncio.Task | None = None  # Stats persistence task


# Constants
COMMAND_DISPLAY_LENGTH = 60  # Max chars to show in queue_status


def _ensure_async_primitives():
    """Ensure async primitives are initialized (must be called with running event loop)."""
    global job_queue, lock
    if job_queue is None:
        job_queue = asyncio.Queue()
    if lock is None:
        lock = asyncio.Lock()


def get_lock() -> asyncio.Lock:
    """Get the async lock, lazily initializing if needed."""
    global lock
    if lock is None:
        lock = asyncio.Lock()
    return lock


# Where a job leaves its exit status for a broker that may not be the one that started it.
# Under /run (tmpfs): the status is meaningless across a reboot, since a job cannot survive one.
JOB_EXIT_DIR_DEFAULT = "/run/tt-device-broker/jobexit"


def job_exit_dir() -> Path:
    """Where jobs record their own exit status. Read per call, not bound at import: only root can
    create a directory under /run, so a per-user daemon must redirect this or lose every job's
    status silently — and an import-time constant cannot be redirected by the process that spawns
    the daemon, nor exercised by a test."""
    return Path(os.environ.get("TT_DEVICE_MCP_JOB_EXIT_DIR", "").strip() or JOB_EXIT_DIR_DEFAULT)


def job_exit_file(job_id: str) -> Path:
    return job_exit_dir() / f"{job_id}.exit"


def _exit_trap_preamble(exit_file: str) -> str:
    """Shell preamble that makes a job record its OWN exit status, honestly.

    The broker cannot be relied on to be alive when the job finishes: a job outlives a
    broker restart by design (it runs in its own systemd scope), and the exit status of a
    scope is reported only to whoever spawned it. The status the user reads has to come
    from the job.

    The signal traps are what make that record true. `$?` inside an EXIT trap is the status
    of the last command to COMPLETE, and a shell terminated by a signal has run no failing
    command — so a killed job wrote 0 and was reported "completed", which is the same lie
    the exit file was introduced to stop, arriving by a different road. Turning each signal
    into a real exit status first means the trap has something honest to report.

    `set -e` means a failing command exits the shell before reaching the trap, so the write
    is on EXIT, not the last line.
    """
    return (
        f"""_ttdev_record_exit() {{ printf '%s' "$?" > '{exit_file}' 2>/dev/null || true; }}\n"""
        "trap _ttdev_record_exit EXIT\n"
        "trap 'exit 143' TERM\n"  # 128+SIGTERM: how the broker and systemd stop a job
        "trap 'exit 130' INT\n"  # 128+SIGINT
    )


def ensure_job_exit_dir() -> None:
    try:
        d = job_exit_dir()
        d.mkdir(parents=True, exist_ok=True)
        # Sticky world-writable only for the shared default, where jobs run as different users
        # (privsep) and each must write its own file without being able to remove another's. A
        # redirected dir belongs to one user inside their own 0700 state dir; widening that would
        # publish their job records to every tenant on the box.
        if d == Path(JOB_EXIT_DIR_DEFAULT):
            os.chmod(d, 0o1777)
    except OSError:
        pass


def read_job_exit_code(job_id: str) -> Optional[int]:
    """The exit status the job recorded for itself, or None if it never got to."""
    try:
        raw = job_exit_file(job_id).read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def clear_job_exit_file(job_id: str) -> None:
    try:
        job_exit_file(job_id).unlink()
    except OSError:
        pass


def get_device_op_lock() -> asyncio.Lock:
    """The one lock every device-touching operation holds. Lazily initialized."""
    global device_op_lock
    if device_op_lock is None:
        device_op_lock = asyncio.Lock()
    return device_op_lock


def get_external_step_free_event() -> asyncio.Event:
    """The one thing job_runner waits on before it dispatches. Lazily initialized like
    get_device_op_lock(), for the same reason. Starts SET (free): nothing reserves the device
    until a step actually runs one."""
    global external_step_free_event
    if external_step_free_event is None:
        external_step_free_event = asyncio.Event()
        external_step_free_event.set()
    return external_step_free_event


# Presence of this file means a device operation is in flight. It is what tells
# the auto-updater (a separate process, so an asyncio lock is invisible to it) not
# to restart the broker right now — a restart mid-reset is how 32 ASICs end up
# half-reset. It carries the pid so a stale file from a killed broker is ignorable.
DEVICE_OP_INHIBIT_DEFAULT = "/run/tt-device-broker/device-op.lock"


def device_op_inhibit() -> Path:
    """The restart-inhibit file for an in-flight device op. Read per call for the same reason as
    job_exit_dir(): only root can create a directory under /run, so a per-user daemon cannot write
    the default and the inhibit it is supposed to provide is silently absent."""
    return Path(os.environ.get("TT_DEVICE_MCP_DEVICE_OP_LOCK", "").strip() or DEVICE_OP_INHIBIT_DEFAULT)


@asynccontextmanager
async def _device_op(name: str, owner: str = "[broker]"):
    """Serialize one device-touching operation and shield it from restarts.

    Two guarantees, and both were missing:
      * exclusion — no two device operations overlap, so a health gate and an
        operator's reset can never issue concurrent -glx_reset calls;
      * inhibition — while this is held, the auto-updater defers, so nothing
        restarts the broker (and, via KillMode=control-group, kills the device
        command) partway through.

    ``owner`` is who asked for it. The running row used to say "[broker]" for every op,
    so an operator watching their own reset saw the broker doing something to the device
    and no sign of their request — and the row it left behind once finished named them
    correctly, so the same reset changed hands halfway through. The broker is the default
    because most ops are genuinely its own.
    """
    global device_op_active, device_op_started_at, device_op_detail
    global device_op_stage_started_at, device_op_owner, _action_row
    lk = get_device_op_lock()
    waited = time.monotonic()
    async with lk:
        held = time.monotonic() - waited
        if held > 1 and logger:
            logger.info(
                f"DEVICE-OP {name}: waited {held:.0f}s for the device lock "
                f"(previous op: {device_op_active or 'unknown'})"
            )
        device_op_active = name
        device_op_owner = owner
        device_op_started_at = datetime.now().isoformat()
        device_op_stage_started_at = device_op_started_at
        device_op_detail = ""
        try:
            inhibit = device_op_inhibit()
            inhibit.parent.mkdir(parents=True, exist_ok=True)
            inhibit.write_text(f"{os.getpid()} {name}\n")
        except OSError:
            pass
        health_event("device_op_begin", op=name, waited_sec=round(held, 1))
        t0 = time.monotonic()
        try:
            yield
        finally:
            health_event("device_op_end", op=name, seconds=round(time.monotonic() - t0, 1))
            device_op_active = ""
            device_op_owner = "[broker]"
            device_op_started_at = ""
            device_op_stage_started_at = ""
            device_op_detail = ""
            # A sub-action that reserved a row but died before writing its durable entry leaves a
            # stale reservation; clear it here so it cannot leak into the next op's in-flight row.
            _action_row = None
            try:
                device_op_inhibit().unlink()
            except OSError:
                pass


# A step's gate task — and, on a deadline overrun, its reclaim task too (see _run_step_gate /
# _run_step_reclaim below) — must outlive the step's own HTTP reply, so something at module scope
# has to hold a reference to it: an asyncio.Task with nothing referencing it is only weakly held
# by the loop and can be garbage-collected mid-flight.
_step_background_tasks: set = set()


def _recompute_external_step_active() -> None:
    global external_step_active
    external_step_active = ", ".join(_external_step_holders.values())


def _reserve_external_step(name: str) -> int:
    """Claim the device against job dispatch; returns a token that MUST be passed to
    _release_external_step exactly once. Keyed by token, not a plain flag, so a double release is
    a no-op rather than an under-count that frees the device out from under a live holder — and
    so a holder is only ever freed by its own release. Two simultaneous holders are no longer
    reachable through the routes themselves (they serialize via _await_external_steps_clear), but
    the keying is what makes that serialization safe to rely on rather than a second guess: a
    release cannot clear someone else's claim. Callers must not await between their
    _broker_work_in_flight()
    check and this call: with no await in between, the check-and-reserve is atomic with respect to
    job_runner (the only other reader of this reservation) on a single-threaded event loop, so no
    lock is needed here — and _device_op itself cannot be held across the gate call without
    deadlocking, since device_health_gate acquires that same non-reentrant lock internally."""
    global _external_step_token_seq
    _external_step_token_seq += 1
    token = _external_step_token_seq
    _external_step_holders[token] = name
    _recompute_external_step_active()
    get_external_step_free_event().clear()
    return token


def _release_external_step(token: int) -> None:
    """Release exactly the reservation ``token`` names — called from that reservation's gate
    task's done-callback, when its device work has actually finished, not from a `finally` around
    the step's reply. A deadline-expired 'inconclusive' reply must not free the device while the
    gate (still holding _device_op) keeps running underneath it in the background. Popping a token
    that is already gone (a double release, or a caller's cleanup racing the task's own) is a
    no-op rather than an under-count, so it can never free the device while a genuine holder with
    a DIFFERENT token remains. The free event is set only once every holder has released."""
    _external_step_holders.pop(token, None)
    _recompute_external_step_active()
    if not _external_step_holders:
        get_external_step_free_event().set()


async def _run_step_gate(
    phase: str, coro, *, deadline_sec: float, mark_dirty_on_error: bool, handoff: dict, token: int
):
    """Run one externally-driven step's health-gate pass as a background task. The caller must
    already hold the reservation (_reserve_external_step, this call's ``token``) before calling
    this — for post-step that has to happen before the reclaim runs, well before this function is
    ever reached — this function only ever RELEASES it, once the task itself finishes, never
    acquires it.

    ``handoff`` is how that release is handed off safely across a cancellation of THIS coroutine
    (e.g. the surrounding HTTP request being torn down): it is stamped the moment the gate task
    and its done-callback both exist, synchronously, before the one `await` below — so even if
    that await raises CancelledError, the caller's own cleanup (see _pre_step_impl/_post_step_impl)
    can tell the task now owns the release and must not also release it itself. Without this, a
    cancelled caller would free the reservation while the still-running task's device work
    continued underneath it — the same class of bug as the deadline that used to cancel the gate
    outright, reached by a different door.

    Returns (timed_out, exc). ``timed_out`` True means the deadline elapsed with the gate still
    running — still reserved, still holding _device_op — and the caller replies 'inconclusive'
    without touching either. ``exc`` is the gate's exception when it finished within the
    deadline (None on a clean pass).

    Why a task instead of `asyncio.wait_for(coro, timeout=...)`: the gate's device work runs in
    `asyncio.to_thread` calls (holder scans, heartbeat reads, snapshots, incident capture), and a
    cancellation cannot stop a thread already running — `wait_for` cancelling the coroutine at the
    deadline unwinds the gate's `async with _device_op(...)`, releasing the lock and this
    reservation, while the worker thread keeps touching the device. That let a second gate or a
    freshly dispatched job overlap it — worse on a wedged chip, where a hung read is *why* the
    deadline fired in the first place. Never cancelling, and holding the reservation until the
    task's own done-callback runs, is what closes that window.
    """
    task = asyncio.ensure_future(coro)
    _step_background_tasks.add(task)

    def _on_done(t) -> None:
        # Everything here runs inside a try/finally: a raise from the logging call or from
        # _mark_device_dirty (sysfs reads via chip_snapshot_event, fsm.on_fault's persist) would
        # otherwise skip the release below entirely. asyncio swallows a done-callback's own
        # exception into the loop's exception handler — it never propagates to anyone who could
        # notice — so a bare (non-finally) release here would leak the reservation permanently on
        # exactly the kind of error this callback exists to report, wedging every future dispatch
        # until the broker restarts. That is a worse outcome than any race this mechanism fixes.
        try:
            # MUST retrieve the exception even when we do nothing else with it: an asyncio.Task
            # whose exception is never fetched logs "Task exception was never retrieved" the
            # moment it is garbage collected, which is the exact traceback this project's own
            # history already recorded once for the previous wait_for-based version of this code.
            exc = None if t.cancelled() else t.exception()
            if exc is not None:
                if logger:
                    logger.error(f"{phase} health gate error: {exc}")
                if mark_dirty_on_error:
                    _mark_device_dirty(f"{phase} health gate error: {exc}", why="gate_error")
        finally:
            _step_background_tasks.discard(t)
            _release_external_step(token)

    task.add_done_callback(_on_done)
    handoff["reserved_by_task"] = True

    done, pending = await asyncio.wait({task}, timeout=deadline_sec)
    if pending:
        return True, None
    return False, (None if task.cancelled() else task.exception())


async def _await_external_steps_clear(deadline_at: float) -> bool:
    """Wait out any external step already holding the device. True if it cleared in time.

    Step routes serialize against each other rather than overlapping. A prologue that answers
    while the previous allocation's epilogue is still reclaiming reads that epilogue's stragglers
    as foreign holders, reports the mesh unfit, and drains the node plus requeues the job over a
    device that was seconds from being handed over clean — and its probes run concurrently with
    the reclaim's own signals besides.

    Refusing the overlap instead of waiting has the identical drain outcome, so this waits. It
    costs nothing in the ordinary case: nothing is reserved, so the loop body never runs. The
    wait is bounded by the caller's own route deadline, and a step still holding the device when
    that expires means a recovery is genuinely in progress — reporting the device not ready then
    is the true answer, not a false one.

    The loop re-checks after every wake instead of trusting one event: the event being set and
    this coroutine resuming are separate moments, so a step that reserves in between is waited on
    too rather than raced past. Callers must not await between this returning and their own
    _broker_work_in_flight()/_reserve_external_step() pair, which is what keeps that pair atomic.
    """
    while external_step_active:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(get_external_step_free_event().wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return False
    return True


def _holder_rows(holders) -> list[dict]:
    """Device holders as the shape both the audit and the step reply report them in."""
    return [{"pid": h.pid, "uid": h.uid, "username": h.username} for h in holders]


def _audit_straggler_reclaim(res: ReclaimResult, started: datetime, *, late: bool) -> None:
    """Durably record what a straggler reclaim actually signalled.

    The one destructive thing post-step does to a process it does not own — root SIGKILLing
    another user's pids — so it gets the same durable record every other privileged device action
    gets (the reset stream, exec). Quiet on a no-op reclaim: nothing happened to anyone, so
    nothing is owed to the ledger.

    Called from both the in-budget path and the late done-callback, because the audit contract
    has no deadline: a reclaim that overran its reply still killed real processes, and reporting
    'inconclusive' to the scheduler is not a reason for those kills to go unrecorded. ``late``
    distinguishes the two in the journal — the reply that went out did not mention them.
    """
    if not res.signalled:
        return
    signalled, survivors = _holder_rows(res.signalled), _holder_rows(res.survivors)
    health_event(
        "straggler_reclaim",
        signalled=signalled,
        survivors=survivors,
        scan_complete=res.scan_complete,
        late=late,
    )
    write_action_log(
        "[broker]post-step",
        "straggler reclaim: " + ", ".join(f"pid {h['pid']} ({h['username']})" for h in signalled),
        (datetime.now() - started).total_seconds(),
        "completed",
        0,
    )


async def _run_step_reclaim(deadline_at: float, handoff: dict, token: int) -> tuple[bool, Optional[ReclaimResult]]:
    """Run post-step's straggler reclaim as its own background task, under the same never-cancel
    discipline as _run_step_gate: reclaim SIGTERMs/SIGKILLs real processes, so cancelling it
    mid-round is exactly as unsafe as cancelling the gate mid-device-op. Bounded by ``deadline_at``
    (a `time.monotonic()` value) — the SAME absolute route deadline the gate that may follow it
    also answers to, not a fresh timeout of its own, so reclaim plus gate together never outlast
    one reply deadline (the old code started the deadline only after reclaim returned, so a
    two-round reclaim with its grace sleeps could make the whole route run well past what
    "inconclusive" promised).

    Returns (timed_out, result). ``timed_out`` True means the deadline elapsed with reclaim still
    running: the caller must reply 'inconclusive' without ever calling the gate, and without
    reading ``result`` (None). The reservation is left held for it — ``handoff`` is stamped, and a
    done-callback attached HERE (not in _run_step_gate, which never runs in this branch) owns the
    eventual release once reclaim actually finishes.

    On a normal, in-budget finish, no callback is attached and nothing here releases the
    reservation: the caller is about to hand the same token straight to _run_step_gate, which
    still needs it.
    """
    started = datetime.now()
    task = asyncio.ensure_future(asyncio.to_thread(reclaim_foreign_holders))
    _step_background_tasks.add(task)

    def _hand_off() -> None:
        """Give the still-running reclaim ownership of the reservation and of its own audit.

        Attached here rather than before the wait, unlike _run_step_gate: this callback releases
        the reservation, and the gate that follows an in-budget reclaim still needs the same
        token. Attaching up front would fire the callback while `asyncio.wait` is still resuming
        (add_done_callback is FIFO, and wait's own callback is registered after ours), freeing the
        device before the gate ever starts.
        """

        def _on_done(t) -> None:
            try:
                # MUST retrieve the exception even where nothing acts on it, or a GC'd task with
                # an unfetched exception logs "Task exception was never retrieved" at GC.
                exc = None if t.cancelled() else t.exception()
                if exc is not None:
                    if logger:
                        logger.error(f"post-step straggler reclaim error: {exc}")
                else:
                    # The route has already replied 'inconclusive' (or unwound on a cancel), so
                    # this is the ONLY place a late reclaim's kills can be recorded. Skipping it
                    # would leave root SIGKILLing another user's pids with no journal entry and no
                    # action-log line — the one destructive thing this route does to a process it
                    # does not own, and the audit contract does not have a deadline.
                    _audit_straggler_reclaim(t.result(), started, late=True)
            finally:
                _step_background_tasks.discard(t)
                _release_external_step(token)

        task.add_done_callback(_on_done)
        handoff["reserved_by_task"] = True

    remaining = max(0.0, deadline_at - time.monotonic())
    try:
        done, pending = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        # The client went away mid-reclaim. `to_thread` cannot be cancelled, so the reclaim is
        # still scanning and signalling; without this handoff the caller's `finally` sees an
        # unstamped handoff, frees the reservation, and job_runner dispatches a tenant onto a
        # device whose holders are still being killed.
        _hand_off()
        raise
    if pending:
        _hand_off()
        return True, None

    _step_background_tasks.discard(task)
    res = task.result()
    _audit_straggler_reclaim(res, started, late=False)
    return False, res


def _set_device_op_detail(detail: str) -> None:
    """Narrate what the in-flight device op is doing right now, for anyone watching
    the queue. A reset+verify holds the device for over a minute; 'health-gate' alone
    does not tell the person waiting whether it is resetting, or proving the fabric.

    A new stage restarts the stage clock, because the label and the elapsed time shown
    beside it have to measure the same thing. An op that resets and then re-proves the
    fabric is on its second ~45s pass at minute twelve; timing that pass from the start
    of the whole op reports a 45s check that has been running for twelve minutes, which
    is the picture of a hung device and not of a healthy one nearly done."""
    global device_op_detail, device_op_stage_started_at
    device_op_detail = detail
    device_op_stage_started_at = datetime.now().isoformat()


def get_job_queue() -> asyncio.Queue[str]:
    """Get the job queue, lazily initializing if needed."""
    global job_queue
    if job_queue is None:
        job_queue = asyncio.Queue()
    return job_queue


def _reset_for_testing():
    """Reset global async primitives for test isolation.

    This allows tests to get fresh queue/lock instances in each test's event loop.
    Only used by test fixtures - not part of the public API.
    """
    global job_queue, lock, _action_row, _hold_row, device_hold_logged, device_hold_episode_since, device_hold_episode_reason
    job_queue = None
    lock = None
    _action_row = None
    _hold_row = None
    device_hold_logged = False
    device_hold_episode_since = ""
    device_hold_episode_reason = ""


def load_env_file(env_path: str, workspace: str) -> dict[str, str]:
    """Load environment variables from a YAML file.

    Args:
        env_path: Path to env file (relative to workspace or absolute)
        workspace: Workspace directory for resolving relative paths

    Returns:
        Dictionary of environment variable name -> value

    Raises:
        FileNotFoundError: If env file doesn't exist
        yaml.YAMLError: If env file is not valid YAML
        ValueError: If env file content is not a dict of key-value pairs
    """
    # Resolve path relative to workspace if not absolute
    if not os.path.isabs(env_path):
        env_path = os.path.join(workspace, env_path)

    with open(env_path, "r") as f:
        env_vars = yaml.safe_load(f)

    # Handle empty file or non-dict content
    if env_vars is None:
        return {}
    if not isinstance(env_vars, dict):
        raise ValueError(f"Env file must contain key-value pairs, got {type(env_vars).__name__}")

    # Convert all values to strings
    return {str(k): str(v) for k, v in env_vars.items()}


def get_activation_script(
    workspace: str,
    env_file: str | None = None,
    inherited_env: dict[str, str] | None = None,
    validate: bool = False,
) -> tuple[str, dict[str, str]]:
    """Generate shell commands to activate a workspace environment.

    Args:
        workspace: Path to workspace directory
        env_file: Optional path to env file (relative to workspace or absolute)
        inherited_env: Optional dict of env vars inherited from caller (CLI captures these)
        validate: If True, check that python_env exists and raise FileNotFoundError if not

    Returns:
        Tuple of (shell script string, resolved env vars dict)

    Raises:
        FileNotFoundError: If validate=True and python_env doesn't exist
    """
    env_vars: dict[str, str] = {}

    # Priority: env_file > inherited_env > workspace defaults
    if env_file:
        env_vars = load_env_file(env_file, workspace)
    elif inherited_env:
        env_vars = inherited_env.copy()
    else:
        # Default environment based on workspace structure
        tt_metal = f"{workspace}/tt-metal"
        cache_dir = f"{workspace}/.tt-metal-cache"
        python_env = f"{tt_metal}/python_env"
        env_vars = {
            "TT_METAL_HOME": tt_metal,
            "TT_METAL_CACHE": cache_dir,
            "PYTHONPATH": tt_metal,
            "PYTHON_ENV_DIR": python_env,
            "TT_METAL_ENV": "dev",
            "VLLM_TARGET_DEVICE": "tt",
        }

    # Determine python env to activate (priority order):
    # 1. PYTHON_ENV_DIR explicitly set (from env file or inherited)
    # 2. VIRTUAL_ENV from inherited env only (caller's active venv, not from env file -
    #    VIRTUAL_ENV in env file would be confusing since it's meant to reflect active state)
    # 3. {TT_METAL_HOME}/python_env if TT_METAL_HOME is set
    # 4. {workspace}/tt-metal/python_env
    if "PYTHON_ENV_DIR" in env_vars:
        python_env = env_vars["PYTHON_ENV_DIR"]
    elif inherited_env and "VIRTUAL_ENV" in inherited_env:
        python_env = inherited_env["VIRTUAL_ENV"]
    elif "TT_METAL_HOME" in env_vars:
        python_env = f"{env_vars['TT_METAL_HOME']}/python_env"
    else:
        python_env = f"{workspace}/tt-metal/python_env"

    # Validate python env exists
    if validate and not os.path.exists(python_env):
        raise FileNotFoundError(
            f"Python env not found at {python_env}. " "Set PYTHON_ENV_DIR in your env file or activate a virtualenv."
        )

    # Build script
    lines = []
    # Export env vars, excluding VIRTUAL_ENV (we handle activation separately)
    for key, value in sorted(env_vars.items()):
        if key != "VIRTUAL_ENV":
            lines.append(f'export {key}="{value}"')

    # Activate python env and cd to workspace
    lines.extend(
        [
            f'source "{python_env}/bin/activate"',
            f'cd "{workspace}"',
        ]
    )

    return "\n".join(lines), env_vars


# ============== Job Runner ==============


def append_job_log(job_log_file: Path, stream: str, text: str):
    """Append a line to the job log file."""
    with open(job_log_file, "a") as f:
        timestamp = datetime.now().strftime("%H:%M:%S")
        f.write(f"[{timestamp}] [{stream}] {text}")


HEADER_FIELDS = {"JOB ID": "job_id", "OWNER": "owner", "COMMAND": "command", "QUEUED": "queued_at"}
FOOTER_FIELDS = {
    "FINISHED": "finished_at",
    "STATUS": "status",
    "EXIT CODE": "exit_code",
    "WAIT TIME": "wait",
    "RUNTIME": "runtime",
}

# How far back from the end of a job log the footer may sit. The post-job health gate
# appends what it found — and, if it resets, the reset's whole output — AFTER the footer,
# so "the last few lines" is not where the footer lives.
FOOTER_TAIL_LINES = 512


def _parse_job_log_footer(tail: list[str]) -> dict:
    """The footer's fields, or ``{}`` if the job never wrote one (it is still running, or
    the broker died under it).

    Anchored on the footer's own FINISHED: line rather than read at a fixed offset from the
    end of the file, because the footer is NOT the end of the file: the post-job health gate
    writes its findings after it. A window that missed STATUS: by one line reported finished
    jobs — successful ones, exit 0 — as "interrupted", which reads as "the broker killed
    your job" to the person whose job it was.

    Anchoring also keeps a job that prints a line like "STATUS: ..." of its own from being
    read as a footer that isn't there.
    """
    for i in range(len(tail) - 1, -1, -1):
        if not tail[i].startswith("FINISHED:"):
            continue
        found = {}
        for line in tail[i:]:
            if line.startswith("=" * 10):  # the footer's closing rule
                break
            key, sep, val = line.partition(":")
            if sep and key.strip() in FOOTER_FIELDS:
                found[FOOTER_FIELDS[key.strip()]] = val.strip()
        return found
    return {}


def write_job_log_footer(job_log_file: Path, job: Job):
    """Write job completion footer to the job log file."""
    with open(job_log_file, "a") as f:
        f.write("\n" + "=" * 70 + "\n")
        f.write(f"FINISHED:    {job.finished_at}\n")
        f.write(f"STATUS:      {job.status.value}\n")
        f.write(f"EXIT CODE:   {job.exit_code}\n")
        if job.wait_sec is not None:
            f.write(f"WAIT TIME:   {job.wait_sec:.1f}s\n")
        if job.runtime_sec is not None:
            f.write(f"RUNTIME:     {job.runtime_sec:.1f}s\n")
        f.write("=" * 70 + "\n")


def seed_job_counter() -> None:
    """Resume the id sequence where the last broker left off. The counter lives in
    memory, so without this a restart hands out 001 again while 019 is still in the
    recent list — ids would run backwards. The newest job log names the last id
    issued (logs are `<date>_<time>_<id>.log`, so name order is time order)."""
    global job_counter
    if not job_log_dir:
        return
    for name in sorted((p.name for p in Path(job_log_dir).glob("*.log")), reverse=True):
        m = re.search(r"_(\d{3})\.log$", name)
        if m:
            job_counter = int(m.group(1)) % JOB_ID_MODULUS
            return


# How far back an id is still spoken for. An id may be recycled only once nothing a user
# can still be shown answers to it, and what they are shown is `recent_jobs`, which reads
# the newest log files. Comfortably past any recent_jobs view, and far short of
# JOB_ID_MODULUS, so the pool cannot run dry.
JOB_ID_RECENT_WINDOW = 200


def recent_log_ids() -> frozenset[str]:
    """The ids the newest log files still answer to.

    In-memory `jobs` is not the set of ids in use, and treating it as one is what let two
    jobs share id 482. A restart empties `jobs` while every log survives; the counter is then
    rebuilt from the newest log NAME — and an action log is named for the moment its action
    STARTED, though its id is drawn when the action FINISHES. A 45s fabric check therefore
    files itself 45s back, behind a job that queued in the meantime and holds a LOWER id, and
    the counter is seeded a step behind an id the log directory already owns. The next job
    walks straight onto it. The log directory is the only thing that knows this, so it is
    the thing that gets asked."""
    if not job_log_dir:
        return frozenset()
    try:
        with os.scandir(job_log_dir) as it:
            names = sorted((e.name for e in it if e.name.endswith(".log")), reverse=True)
    except OSError:
        return frozenset()
    found = (re.search(r"_(\d{3})\.log$", n) for n in names[:JOB_ID_RECENT_WINDOW])
    return frozenset(m.group(1) for m in found if m)


def next_job_id(extra_in_use=frozenset()) -> str:
    """Next 3-digit id (000-999, wrapping). Ids are short enough to type, so they
    are recycled — but never while something still answers to one: an id held by an
    in-memory job, a surviving scope, or a log recent enough to still be shown is
    skipped. Caller holds the lock."""
    global job_counter
    in_use = set(jobs) | set(extra_in_use) | recent_log_ids()
    # A reserved-but-not-yet-written action or hold row holds its id in memory with no log file to
    # find, so it must be excluded here or a job allocated in that window could take the same id.
    if _action_row is not None:
        in_use.add(_action_row["id"])
    if _hold_row is not None:
        in_use.add(_hold_row["id"])
    for _ in range(JOB_ID_MODULUS):
        job_counter = (job_counter + 1) % JOB_ID_MODULUS
        jid = f"{job_counter:03d}"
        if jid not in in_use:
            return jid
    raise RuntimeError(f"no free job id ({JOB_ID_MODULUS} live jobs)")


def _write_action_log_file(
    aid: str, owner: str, command: str, started: datetime, status: str, exit_code, runtime_sec: float
) -> None:
    """Write one non-queued action's header+footer log under a KNOWN id and start — the shared body
    behind both write_action_log (op sub-actions) and the hold release. Naming the file by the START
    keeps on-disk order in execution order, the same clock the queued jobs' names use."""
    if not job_log_dir:
        return
    now = datetime.now()
    try:
        log_file = Path(job_log_dir) / f"{started.strftime('%Y-%m-%d_%H%M%S')}_{aid}.log"
        with open(log_file, "w") as f:
            f.write("=" * 70 + "\n")
            f.write(f"JOB ID:      {aid}\nOWNER:       {owner}\nCOMMAND:     {command}\n")
            # QUEUED == started: an action waits for nothing, so its wait is zero, not the
            # length of whatever queue happened to exist when it ran.
            f.write(f"QUEUED:      {started.isoformat()}\n" + "=" * 70 + "\n")
            f.write(f"[Started at {started.isoformat()}]\n")
            f.write(f"FINISHED:    {now.isoformat()}\nSTATUS:      {status}\n")
            f.write(f"EXIT CODE:   {exit_code}\nRUNTIME:     {runtime_sec:.1f}s\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _begin_action_row(owner: str, command: str) -> None:
    """Reserve a broker sub-action's ledger identity at the instant it begins, so the in-flight
    row and the durable row it becomes are one row: same id, same start, same name. The id is
    allocated now (not at completion), and the start is the real begin time (not reconstructed
    from a runtime at the end) — the two mismatches that made a running op re-sort and rename when
    it finished. Caller holds the device op lock, so the id allocation is safe and single-flight."""
    global _action_row
    if not job_log_dir:
        return
    if _action_row is not None:
        raise RuntimeError(f"action row already active: {_action_row['owner']} {_action_row['command']}")
    aid = next_job_id()
    started = datetime.now()
    _action_row = {"id": aid, "started_at": started.isoformat(), "owner": owner, "command": command}
    try:
        log_file = Path(job_log_dir) / f"{started.strftime('%Y-%m-%d_%H%M%S')}_{aid}.log"
        with open(log_file, "w") as f:
            f.write("=" * 70 + "\n")
            f.write(f"JOB ID:      {aid}\nOWNER:       {owner}\nCOMMAND:     {command}\n")
            f.write(f"QUEUED:      {started.isoformat()}\n" + "=" * 70 + "\n")
            f.write(f"[Started at {started.isoformat()}]\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def write_action_log(owner: str, command: str, runtime_sec: float, status: str, exit_code) -> None:
    """Record a non-queued device action (a reset, a fabric check) in the same
    header/footer log format jobs use, so it shows up in recent history. It shares the
    jobs' id space — what ran is told by COMMAND.

    These actions are never queued: they run the instant the device needs them, between
    jobs. So the timestamp that places one in history is when it STARTED. A sub-action that
    reserved its identity with ``_begin_action_row`` writes its durable row under that same id
    and real start, so the row it already showed while running does not change when it finishes.
    A one-shot event that reserved nothing (a hold, a startup probe) has no running phase to
    match, so its start is reconstructed from the runtime — the finish time would put a 60s reset
    ahead of the job it interrupted and a 45s fabric check ahead of the job that triggered it.
    """
    global _action_row
    if not job_log_dir:
        return
    now = datetime.now()
    row = _action_row
    if row is not None and row["owner"] == owner and row["command"] == command:
        aid = row["id"]
        started = datetime.fromisoformat(row["started_at"])
        _action_row = None
    else:
        started = now - timedelta(seconds=max(runtime_sec, 0.0))
        aid = next_job_id()
    _write_action_log_file(aid, owner, command, started, status, exit_code, runtime_sec)


def _span_str(start_iso: Optional[str], end_iso) -> Optional[str]:
    """Format ``end - start`` as ``"12.3s"``; None if either timestamp is unusable.

    ``end_iso`` may be a datetime (e.g. ``now`` for an elapsed-up-to-now span)."""
    if not start_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso)
        end = end_iso if isinstance(end_iso, datetime) else datetime.fromisoformat(end_iso)
        return f"{(end - start).total_seconds():.1f}s"
    except (ValueError, TypeError):
        return None


def _present_chip_indices() -> list[str]:
    """The /dev/tenstorrent device-node indices currently enumerated, by name (``"0"``, ``"1"``,
    ...) — the one place this directory listing happens, so every caller (the gate, the idle
    escalators, the reset routes, and HealthMonitor.update() via the injected
    ``present_chip_indices`` dep) agrees on what "present" means. Empty when the directory is
    absent — every chip off the bus, or the driver not loaded — never raises."""
    tt_dev = Path(TT_DEV_DIR)
    return [f.name for f in tt_dev.iterdir() if f.name.isdigit()] if tt_dev.exists() else []


def _scoped_reset_backend() -> bool:
    """Whether a reset can run in a PID-1 systemd scope, as opposed to a detached local child.

    Root AND systemd. `systemd-run --scope` on the system bus refuses an unprivileged caller with
    "Interactive authentication required" and exits 1 — which the caller reads as a failed reset
    and answers with the 600 s cooldown, so a non-root daemon would suppress its own next attempt
    after a reset that never ran. The binaries being on PATH does not detect this; only the euid
    does. Without both, the reset runs through the local-lock backend, which needs neither.
    """
    return privileges.has_systemd() and privileges.is_root()


async def _group_gone(pid: int, deadline: float) -> bool:
    """Poll until the process group is fully gone, or ``deadline`` passes."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            os.killpg(pid, 0)  # probe: raises once the group is gone
        except (ProcessLookupError, OSError):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _terminate_process_group(pid: int, grace_sec: float = GRACEFUL_KILL_GRACE_SEC) -> None:
    """Stop a job's process group so it RELEASES THE DEVICE: SIGINT, then SIGTERM,
    then SIGKILL only for a job that is genuinely hung.

    Killing a device job without letting it release the chip is how the mesh gets
    wedged — the eth cores are left mid-transaction and the next job inherits a dead
    fabric. A ttnn job releases the chip only from its teardown path (pytest fixture
    teardown, ttnn's atexit), and that path runs only if the interpreter UNWINDS.

    SIGINT is the only signal that unwinds it: Python raises KeyboardInterrupt.
    SIGTERM does NOT — Python installs no handler, so the default action terminates
    the process outright, atexit never runs, and the mesh is never closed. For a
    Python job SIGTERM is therefore no gentler than SIGKILL, which is why the ladder
    leads with SIGINT and why SIGTERM is only a formality on the way to the reap.

    Reaching SIGKILL means the job did NOT release the device: log it loudly, because
    the mesh is probably wedged and the clean-device gate will have to reset it.

    ``pid`` is the pgid (jobs are spawned with os.setsid, so pid == pgid)."""
    loop = asyncio.get_event_loop()
    for sig, window in ((signal.SIGINT, grace_sec), (signal.SIGTERM, SIGTERM_GRACE_SEC)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, OSError):
            return  # already gone
        if await _group_gone(pid, loop.time() + window):
            return
        if logger:
            logger.info(f"TERMINATE pgid={pid} survived {sig.name} for {window}s; escalating")
    try:
        os.killpg(pid, signal.SIGKILL)
        if logger:
            logger.warning(
                f"TERMINATE pgid={pid} escalated to SIGKILL — the job never released the device. "
                f"The mesh is likely wedged; the clean-device gate will need to reset it."
            )
    except (ProcessLookupError, OSError):
        pass


def _is_wedge_risk_exit(status: "JobStatus", exit_code: Optional[int]) -> bool:
    """True if a finished job may have left the device/mesh wedged, so the next
    job must not start until the device is reset + verified.

    Wedge-risk: KILLED / TIMEOUT / HUNG (we terminated it, possibly mid-CCL), or FAILED
    by a signal — it crashed and did not release the device cleanly. NOT wedge-risk: a
    clean COMPLETED (exit 0) or an application FAILED with a normal exit code (e.g. a
    pytest assertion -> 1), which exited on its own and released the device normally.

    A signal death reaches us under either of two conventions, and reading only one is
    how a wedge gets handed to the next tenant: waitpid reports -N (what asyncio gives
    us directly), while a shell reports 128+N (what the job's own exit file records, and
    the only convention available for a job re-adopted across a broker restart). A mesh
    left unusable by a SIGABRT does not care which number described it."""
    if status in (JobStatus.KILLED, JobStatus.TIMEOUT, JobStatus.HUNG):
        return True
    if status == JobStatus.FAILED and isinstance(exit_code, int):
        if exit_code < 0:  # waitpid: -N
            return True
        if 129 <= exit_code <= 159:  # shell: 128+N, N = 1..31
            return True
    return False


# Runtime fault text that means the eth/fabric link layer was left wedged — the
# mesh is unusable for the next runner regardless of how this process exited.
# Exit code alone misses these: the fault surfaces at mesh-open as a C++ exception
# the harness may catch and exit 0/1 with, handing a flapping board onward. These
# strings come from the metal runtime's own link/heartbeat bring-up path; matching
# them lets the gate reset before handoff. Kept specific to avoid resetting on a
# job that merely mentions ethernet in a benign log line.
_DEVICE_FAULT_SIGNATURES = (
    "waiting for active ethernet core",  # eth core never (re)trained at open/teardown
    "fabric router sync: timeout",  # fabric init never reached sync
    "return_to_base_firmware_and_wait_for_heartbeat",  # erisc heartbeat never resumed
)


def _scan_output_for_device_fault(job_log_file: Optional[Path], *, tail_bytes: int = 262144) -> Optional[str]:
    """Scan the tail of a finished job's log for a wedged-eth/fabric signature
    (see ``_DEVICE_FAULT_SIGNATURES``). Returns a short reason if found, else None.
    Only the tail is read because the signature, if present, is at the failure
    point near the end. Read-only, bounded, never raises — health bookkeeping must
    not crash the runner."""
    if not job_log_file:
        return None
    try:
        with open(job_log_file, "rb") as f:
            try:
                f.seek(-tail_bytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            blob = f.read().decode("utf-8", "replace").lower()
    except OSError:
        return None
    for sig in _DEVICE_FAULT_SIGNATURES:
        if sig in blob:
            return f"eth/fabric fault in output: '{sig}'"
    return None


def _healthy_reading() -> HealthState:
    """A minimal, vacuously-healthy :class:`HealthState` (no observations, so none is UNHEALTHY) —
    for the many callers that already know the device just verified fit (an operator's own tt-smi
    check, a reset's post-verify) but hold no HealthMonitor reading of their own to hand ``fsm``."""
    return HealthState(phase="verified", at=datetime.now(), expected=0)


def _job_snapshot(job: "Job | None") -> dict:
    return (
        {
            "id": job.id,
            "owner": job.owner,
            "command": job.command[:500],
            "exit_code": job.exit_code,
            "status": job.status.value if hasattr(job.status, "value") else str(job.status),
            "started_at": job.started_at,
            "workspace": str(job.workspace),
        }
        if job is not None
        else {}
    )


def _mark_device_dirty(reason: str, job: "Job | None" = None, *, why: str = "job_killed") -> None:
    """Flag the device as possibly-wedged so the runner cleans it before the next job.

    The job that did it is recorded, not just the reason. "job 063 ended timeout" cannot
    answer the question anyone will actually be asking six months from now — which
    workloads wedge chips — and the job's own log is in /var/log, which rotates and which
    an ungraceful reboot is free to eat. The whole point of the durable journal is that
    the evidence outlives the event.
    """
    job_snapshot = _job_snapshot(job)
    if logger:
        logger.info(f"CLEAN-GATE device marked dirty: {reason}")
    # A snapshot NOW, while the wreckage is fresh: this is the closest we get to the
    # chip's state at the moment it went bad.
    chip_snapshot_event("device_dirty", reason=reason, job=job_snapshot)
    fsm.on_fault(why, detail=reason, job=job_snapshot)


def _mark_device_reported_fault(reason: str, job: "Job | None" = None) -> None:
    """The runtime itself named a device fault in a job's output.

    Kept apart from an ordinary dirty flag because the evidence outranks our checks. Every
    verification the broker owns is structurally blind to this class of fault: a tt-smi
    snapshot reads a wedged ethernet core as perfectly healthy, and the fabric validator
    runs with a second erisc disabled, so it cannot reach the core that is stuck. Both then
    report a clean mesh, the gate clears the flag "verified", and the next tenant meets the
    same wedge — while the runtime's own words, "Try resetting the board", sit in the log.

    So a reported fault is not answered by re-running the checks that cannot see it. It is
    answered by a reset, and it stands until one runs.
    """
    global device_fault_reported
    device_fault_reported = reason
    _mark_device_dirty(reason, job=job)
    health_event("device_fault_reported", reason=reason)
    if logger:
        logger.error(
            f"CLEAN-GATE runtime reported a device fault: {reason}. Our checks cannot "
            f"see this class of fault; the device is not fit until a reset clears it."
        )


def _note_reset_killed_job() -> None:
    """Remember that the job we are about to SIGKILL is dying to make room for a reset.

    The broker serializes, so the running job is unambiguous. Without this its death lands
    as an ordinary wedge-risk exit and the device is flagged for a reset — on the strength of
    a kill the reset itself performed."""
    victim = next((j.id for j in jobs.values() if j.status == JobStatus.RUNNING), None)
    if victim:
        _mark_job_device_fault_failed(victim, "killed to make room for a device reset after a wedge")


def _clear_device_reported_fault(why: str) -> None:
    """Retire a runtime-reported fault. Earned by a reset that verified — the checks the verify
    runs are blind to the fault, so it is the reset, not the verify, that clears it. The one
    exception is the frozen-eth hold: every fault the runtime reports here is an eth/fabric-wedge
    signature the heartbeat reader CAN see, so a frozen read lets the hold retire the fault instead
    of a reset — the reset it would otherwise demand is the galaxy drop the hold exists to avoid."""
    global device_fault_reported
    if device_fault_reported:
        health_event("device_fault_cleared", why=why, prior=device_fault_reported)
    device_fault_reported = ""


def _journal_fsm_transition(why: str, *, verified: bool) -> None:
    """Record a ``device_dirty_cleared`` event naming who moved the device off its PRIOR episode
    (``why``) and whether a check actually proved the mesh healthy first (``verified``) — emitted
    only while there WAS a prior episode to move off of. The one state change a postmortem cannot
    reconstruct later: the FSM lives in memory, so a device that reads degraded at 14:00 and fit at
    14:05 with no event between is a hole nothing can close. ``verified`` is the load-bearing field
    — a clear that never touched silicon (a foreign holder, a gate error, a reset checked only by
    its exit code) must not read the same as one a full snapshot+fabric pass stood behind."""
    if fsm.state is not ServerState.HEALTHY:
        health_event(
            "device_dirty_cleared", why=why, verified=verified, prior_reason=fsm.record.detail or fsm.record.why
        )


def _clear_device_dirty(*, verified: bool = False, why: str = "unrecorded") -> None:
    """Mark the device clean, and record the transition on the durable health timeline.

    Dropping the flag ends the RESETTING, not the doubt. Where there was something to
    doubt and nothing proved it wrong, the device stays unfit for a tenant: the two are
    separate states, and collapsing them is what let a job onto a mesh whose fabric no
    pass had ever cleared. The doubt lifts when a later gate verifies, not by elapsed time.
    """
    was_degraded = fsm.state is not ServerState.HEALTHY
    _journal_fsm_transition(why, verified=verified)
    if verified:
        # Only proof retires it, whatever the device was flagged for — dirty or held.
        fsm.on_readings(_healthy_reading())
    elif was_degraded:
        # Not proven fit, and nothing here names a new fault class — keep the open episode's own
        # why, just reword the detail and drop the dirty bit: dropping the flag ends the RESETTING
        # (this pass owes the mesh no further reset attempt), never the doubt. A caller that DOES
        # know a new fault class calls _hold_device_unverified/_hold_device_fabric_unverified
        # instead, which set it directly.
        fsm.on_fault(fsm.record.why, detail=why, dirty=False)


def _clear_device_dirty_unverified(why: str, *, holds: bool = True, fault: str = "gate_error") -> None:
    """Drop the dirty flag on a path that did NOT verify the silicon — a foreign tenant
    holds the device, no device nodes are present, health checks are off, or the gate
    errored out. Named so those call sites say out loud that nobody checked the mesh.

    ``holds=False`` for the paths where verification is not merely unavailable but
    switched off: no device nodes to check, or an operator who set health checks to 0.
    Holding those would not protect silicon — nothing on that host will ever verify, so
    the door would shut once and never reopen. Every other unverified clear holds: it
    means we tried and could not tell, which is the case the hold exists for.
    """
    _journal_fsm_transition(why, verified=False)
    if not holds:
        fsm.on_readings(_healthy_reading())
        return
    if fsm.state is ServerState.HEALTHY:
        return  # nothing was dirty or held; an unverified clear invents no doubt of its own
    fsm.on_fault(fault, detail=why, dirty=False)


def _hold_device_unverified(why: str, *, needs_eth_advancing: bool = False) -> None:
    """Hold the tenant door on an affirmative fault the broker will NOT reset — a detected
    frozen eth core, which self-heals and drops off the bus if pushed.

    ``needs_eth_advancing`` picks which of the two self-heal ``why`` values this hold gets:
    ``eth_frozen`` needs a confirmed-advancing eth heartbeat to lift (enum+ARC cannot see a frozen
    core); ``off_bus`` needs only enum+ARC+sysfs, which see a dropped chip return directly. The
    idle relift branches on the value, not on a separate flag.

    A frozen verdict is positive evidence the mesh is wedged, so the door holds on every gate path
    that reaches here, dirty or not — otherwise the next tenant is dispatched onto the frozen mesh
    and the traffic pass takes all 32 chips to 0xFFFFFFFF."""
    _journal_fsm_transition(why, verified=False)
    fsm.on_fault("eth_frozen" if needs_eth_advancing else "off_bus", detail=why, dirty=False)


def _hold_device_fabric_unverified(why: str) -> None:
    """Hold the tenant door for a FABRIC-UNVERIFIED wedge: enum+ARC passed but the fabric traffic
    check exited 77 (could-not-run), so no pass ever cleared the fabric. Unlike
    ``_hold_device_unverified`` this is not a self-healing wedge — a frozen core or an off-bus drop
    that recovers by itself — it is doubt the broker can only settle by running the fabric pass
    again for a real verdict. The door holds unconditionally, dirty or not: this marker arms the
    PERTURBING traffic-pass relift, so it must not be placed with the door open — a tenant let
    through here races the relift's own traffic pass across the same fabric. The doubt lifts when a
    fabric pass returns healthy, never on elapsed time."""
    _journal_fsm_transition(why, verified=False)
    fsm.on_fault("fabric_unverified", detail=why, dirty=False)


# A detector whose cost is a property of the box proves itself here, once per broker start, rather
# than waiting on an operator to arm it. The eth read sat inert for weeks — 25 SKIPPED, 0 verdicts —
# because "nobody armed it" and "it is broken" look identical from outside. An opt-in nobody exercises
# is not a safe default, it is an absent rung. So the box times its own read, arms on a fast answer,
# and when it cannot, says which rung is off and why at WARNING.
ETH_CHECK_SELFTEST_BUDGET_SEC = 10.0
eth_check_armed: bool = False
eth_check_disarm_reason: str = "startup self-test has not run yet"


async def selftest_eth_heartbeat() -> None:
    """Time the passive eth read once per broker start; arm the rung only on a fast answer.

    Slow is the dangerous outcome, not failing: past its timeout the gate reads a frozen core and
    HOLDS the box, so a host that cannot answer well inside the budget keeps the rung off. A
    frozen verdict still counts as ARMED — the read worked, and it found something. Only a read
    that never measured (``classify_exit`` -> None) leaves the rung off.

    An opt-in nobody exercises is not a safe default, it is an absent rung: the read sat inert for
    weeks — 25 SKIPPED, 0 verdicts — because "nobody armed it" and "it is broken" look identical
    from outside. So the box proves the detector on itself, here, once, and says at WARNING which
    rung is off and why when it cannot.
    """
    global eth_check_armed, eth_check_disarm_reason
    # Arm provisionally: eth.build() refuses to hand back a runnable probe on an unarmed host, and
    # this self-test IS the arming. Rolled back below unless the attach answers inside budget.
    prior = os.environ.get("TTDEV_ETH_CHECK_ARMED")
    os.environ["TTDEV_ETH_CHECK_ARMED"] = "1"
    built = eth.build()
    if built is None:
        eth_check_disarm_reason = "no runnable eth read on this host (no python that imports ttexalens)"
    else:
        argv, env = built
        t0 = datetime.now()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=ETH_CHECK_SELFTEST_BUDGET_SEC)
            rc = proc.returncode
            dt = (datetime.now() - t0).total_seconds()
            tail = (out.decode("utf-8", "replace") if out else "").strip().splitlines()
            last = tail[-1][:200] if tail else ""
            # Same override/built-in split verify_eth_heartbeat applies: an operator's own command
            # reports purely through its exit code, so a bare 1 there IS a verdict; only the
            # built-in probe reserves 1 for a crash and signals frozen with its own sentinel (3).
            if health_monitor._eth_heartbeat_cmd():
                verdict = True if rc == 0 else None if rc == FABRIC_CHECK_CANNOT_CHECK_RC else False
            else:
                verdict, _detail = eth.classify_exit(rc)
            if verdict is None:
                eth_check_disarm_reason = f"read could not check in {dt:.1f}s: {last}"
            else:
                eth_check_armed = True
                eth_check_disarm_reason = ""
                if logger:
                    logger.info(
                        f"RUNG ARMED eth-heartbeat: self-test answered in {dt:.2f}s "
                        f"(budget {ETH_CHECK_SELFTEST_BUDGET_SEC:.0f}s, rc={rc})"
                    )
        except asyncio.TimeoutError:
            # The OUTER budget, not the probe's own: a read this slow cannot be told apart from a
            # frozen core at gate time, and the gate would HOLD the box on it.
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            eth_check_disarm_reason = (
                f"read hung past the {ETH_CHECK_SELFTEST_BUDGET_SEC:.0f}s self-test budget — too slow "
                "to distinguish from a frozen core"
            )
        except Exception as e:  # noqa: BLE001 - a self-test must never block startup
            eth_check_disarm_reason = f"read not runnable: {e}"
    if not eth_check_armed:
        # Leave the process exactly as unarmed as it was, so eth.build() keeps returning None.
        if prior is None:
            os.environ.pop("TTDEV_ETH_CHECK_ARMED", None)
        else:
            os.environ["TTDEV_ETH_CHECK_ARMED"] = prior
        if logger:
            logger.warning(
                f"RUNG OFF eth-heartbeat: {eth_check_disarm_reason}. The passive frozen-eth detector "
                "produces NO verdict this run, so a frozen core is left to the traffic pass, which can "
                "knock it off the PCIe bus instead of naming it."
            )


def _opted_out(var: str) -> str:
    """The operator's own opt-out as an inventory cause, or "" when they never set it — in which
    case the rung is off for a reason the caller names instead."""
    return f"{var}=0" if os.environ.get(var, "1").strip() == "0" else ""


def _no_bmc_reason() -> str:
    """Which half of the BMC capability is missing. The two have different fixes: one is a package,
    the other is a host or container that exposes no local IPMI interface at all."""
    if not privileges.snapshot()["ipmitool"]:
        return "ipmitool is not on PATH"
    return "no openable /dev/ipmi* — this host or container exposes no local BMC interface"


def _no_reboot_privilege() -> str:
    """Which half of root+systemd the warm-reboot rung is missing."""
    if not privileges.is_root():
        return "not root — systemctl reboot is refused"
    return "no systemd — there is no systemctl to reboot through"


def _no_tray_platform() -> str:
    """Why the tray rung has no platform here, or "" when it might. Only a host boot committed to
    per-target lacks trays: an unresolved one may be the degraded Galaxy this rung exists for."""
    if fsm.recovery is not None and fsm.recovery is fsm.per_target:
        return "per-target host — there are no UBB trays to re-power"
    return ""


def log_rung_inventory() -> None:
    """State every optional rung at startup, on or off. A rung that is off has to be readable in the
    journal — inferring it from an absence of verdicts is exactly how the eth read stayed dark."""
    rungs = [
        (
            "health-gate",
            health_monitor._health_check_enabled(),
            _opted_out("TT_DEVICE_MCP_HEALTH_CHECK") or "no tt-smi to probe with — serializing only",
        ),
        ("eth-heartbeat", eth_check_armed, eth_check_disarm_reason or "disarmed"),
        (
            "pre-job-dispatch-probe",
            _prejob_dispatch_enabled(),
            "TT_DEVICE_MCP_PREJOB_DISPATCH is not 1 — the mesh is not proven to run a kernel before a "
            "tenant is admitted",
        ),
        ("forced-escalation", _force_escalate_enabled(), "TT_DEVICE_MCP_FORCE_ESCALATE=0"),
        # Each host rung has two distinct OFF causes and the inventory must name the right one: an
        # operator who armed a rung the process cannot execute would otherwise read their own "=0"
        # opt-out back at them, and go looking in the wrong place.
        ("auto-reboot", _auto_reboot_enabled(), _opted_out("TT_DEVICE_MCP_AUTO_REBOOT") or _no_reboot_privilege()),
        (
            "auto-power-cycle",
            _auto_power_cycle_enabled(),
            _opted_out("TT_DEVICE_MCP_AUTO_POWER_CYCLE") or f"{_no_bmc_reason()} — the cold rung cannot fire",
        ),
        # The arming predicate stays privilege-only: off-platform the fire paths decline as
        # not_applicable, which the predicate would turn into blocked.
        (
            "auto-tray-reset",
            _ubb_reset_enabled() and not _no_tray_platform(),
            _no_tray_platform()
            or _opted_out("TT_DEVICE_MCP_AUTO_UBB_RESET")
            or f"{_no_bmc_reason()} — the per-tray re-power cannot fire",
        ),
        ("bridge-reset", bridge_reset_enabled(), bridge_reset_unavailable_reason()),
        ("self-heal-relift", _selfheal_relift_enabled(), "TT_DEVICE_MCP_SELFHEAL_RELIFT=0"),
        ("tenant-hold", _tenant_hold_enabled(), "TT_DEVICE_MCP_TENANT_HOLD=0"),
    ]
    if not logger:
        return
    priv = privileges.snapshot()
    logger.info(
        "BOOT privilege: euid={euid} root={root} systemd={systemd} setpci_bin={setpci_bin} "
        "ipmitool={ipmitool} ipmi_node={ipmi_node}".format(**priv)
    )
    logger.info("RUNG INVENTORY: " + ", ".join(f"{name}={'on' if on else 'OFF'}" for name, on, _ in rungs))
    for name, on, why in rungs:
        # eth states its own reason in the self-test — but that only runs on the privsep path, so
        # elsewhere the inventory is the only place its OFF is ever explained.
        if not on and not (name == "eth-heartbeat" and should_privsep()):
            logger.warning(f"RUNG OFF {name}: {why}")


# The telemetry trace: what the chips were doing while the job ran. Snapshots at the gate
# only ever show the aftermath, so a chip that heated up, throttled, and died slowly is
# indistinguishable from one that dropped dead instantly. The sample is a sysfs read —
# ~0.03s for all 32 chips, and it touches nothing — so taking one every few seconds costs
# effectively nothing and is the only way to see the run-up.
telemetry_ring: deque = deque(maxlen=SAMPLE_RING_SIZE)
sampler_task: asyncio.Task | None = None
# The fabric probe's last output and its in-flight process handle now live on health_monitor

# The fabric probe's last output and its in-flight process handle live on health_monitor
# (see health_monitor.last_fabric_output / health_monitor.fabric_check_proc): the latter is what
# _kill_device_holders reads to SIGKILL the reader when a chip leaves the bus mid-check, and it
# must stay the exact attribute the probe itself writes.


def _selfheal_relift_enabled() -> bool:
    """Whether a self-heal HOLD is re-verified from the idle sampler and lifted on its own.
    ON by default: without it a held-but-clean mesh stays refused with an empty queue, because the
    pre-job gate and the tenant hold-poll both only re-verify a DIRTY device and a hold drops the
    dirty flag — so a self-healed box waits on the next job or a broker restart to reopen. That gap
    is exactly what strands a below-floor hold, so this must be live for the reset floor to be safe.
    The kill switch (TT_DEVICE_MCP_SELFHEAL_RELIFT=0) restores the wait for an operator who needs it."""
    return os.environ.get("TT_DEVICE_MCP_SELFHEAL_RELIFT", "1").strip() != "0"


def _fabric_relift_enabled() -> bool:
    """Whether the idle relift also re-verifies a FABRIC-UNVERIFIED hold (a fabric-check exit-77 after
    a job crash) by re-running the fabric pass, and lifts on a healthy verdict.

    OFF by default, unlike the self-heal relift: this is the one relift path that RUNS THE TRAFFIC
    PASS, and a traffic pass is exactly what shoves a marginal/frozen chip off the PCIe bus. It is
    only defensible because the hold means enum+ARC already passed and the fabric merely could-not-
    run (77 is could-not-check, not fabric-wedged), and because _verify_device reads the eth heartbeat
    first and skips the traffic pass on a frozen core WHEN the reader is configured. Until that reader
    is wired and validated on a real box, leave it off; TT_DEVICE_MCP_FABRIC_RELIFT=1 opts in."""
    return os.environ.get("TT_DEVICE_MCP_FABRIC_RELIFT", "0").strip() not in ("", "0")


def _generic_hold_escalate_enabled() -> bool:
    """Whether a hold no read-only relift can lift escalates to the idle galaxy reset once past the
    ceiling, instead of standing until a broker restart. These holds arm neither the self-heal nor
    the fabric relift — a foreign holder that blocked verification, or a gate that errored out — and
    enum+ARC proves nothing they were placed for, so read-only they never reopen. This path never
    lifts on a read: it only escalates a present, tenant-free mesh past the ceiling to the reset the
    next job's gate would run, riding every _escalate_stuck_hold guard. ON by default;
    TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE=0 is the operator kill-switch. Also gated by that
    escalation's own TT_DEVICE_MCP_STUCK_HOLD_RESET kill-switch."""
    return os.environ.get("TT_DEVICE_MCP_GENERIC_HOLD_ESCALATE", "1").strip() == "1"


def _idle_relift_armed() -> tuple[bool, bool, bool]:
    """``(selfheal, fabric, generic)`` — which of the three idle re-verify/escalate categories the
    CURRENT episode falls into, per the opt-in for each. All False outside RECOVERING: HEALTHY has
    nothing to lift, and DOWN is the terminal rung — only an operator acts on it from here.

    A fabric-unverified hold WITHOUT the fabric-relift opt-in (the default) falls into the GENERIC
    category, exactly as the pre-fsm fold did (``generic`` keyed on the unverified marker with
    ``not fabric``): the perturbing traffic-pass retry is what needs the opt-in, not escalation
    itself, and a hold that arms neither would stand idle for the full forced-watchdog ceiling and
    then fire with force=True — bypassing the due and retry-pacing guards the guarded idle path
    honors — where the old code escalated guarded once past the grace."""
    if fsm.state is not ServerState.RECOVERING:
        return False, False, False
    why = fsm.record.why
    fabric = _fabric_relift_enabled() and why == FABRIC_RELIFT_WHY
    return (
        _selfheal_relift_enabled() and why in SELFHEAL_WHYS,
        fabric,
        _generic_hold_escalate_enabled()
        and (why in GENERIC_ESCALATE_WHYS or (why == FABRIC_RELIFT_WHY and not fabric)),
    )


async def _attempt_idle_relift() -> None:
    """Re-verify a device HELD while idle and lift the hold once the mesh proves fit — never a reset.

    Two hold classes reach here, and nothing else re-checks either: the pre-job gate and the tenant
    hold-poll only re-verify a DIRTY device, and both these holds drop the dirty flag, so a held-but-
    undirty mesh stays refused until the next re-dirty or a broker restart. This is the path that
    lifts each on its own.

    A SELF-HEAL hold (why in SELFHEAL_WHYS: a frozen eth core, or an off-bus drop held below the
    reset floor) is re-verified READ-ONLY — enum+ARC and the passive eth-heartbeat read only,
    pointedly NOT the traffic pass, which is the very thing that shoves a frozen-but-present chip
    off the bus. A still-frozen or still-off-bus device is left held; only a mesh that reads healthy
    AND whose eth cores are not frozen is released. ON by default.

    A FABRIC-UNVERIFIED hold (why == FABRIC_RELIFT_WHY: enum+ARC passed but the fabric check exited
    77) is the one path that RE-RUNS THE TRAFFIC PASS, because a fabric that could only not-run is
    proven fit only by a pass that gets a real verdict. It lifts ONLY on an explicit healthy fabric
    verdict — a 77 again on retry (_verify_device returns healthy=True on a skipped fabric) and a
    failure both hold, so the door never reopens onto fabric no pass cleared. OFF by default (see
    _fabric_relift_enabled): it perturbs, so it is opt-in. The two categories are mutually exclusive.
    A why in GENERIC_ESCALATE_WHYS (a foreign holder that blocked verification, a gate error, or a
    startup boot still awaiting its first fabric pass) arms neither — an enum+ARC pass proves
    nothing those were placed for — so it is never lifted here; when TT_DEVICE_MCP_GENERIC_HOLD_
    ESCALATE is set it instead escalates to the idle galaxy reset once past the ceiling (see
    _generic_hold_escalate_enabled), rather than standing until a broker restart."""
    selfheal, fabric, generic = _idle_relift_armed()
    if not ((selfheal or fabric or generic) and not device_op_active):
        return
    global last_relift_monotonic
    if last_relift_monotonic and (time.monotonic() - last_relift_monotonic) < SELFHEAL_RELIFT_INTERVAL_SEC:
        return
    last_relift_monotonic = time.monotonic()

    def _log(msg: str) -> None:
        if logger:
            logger.info(f"IDLE-RELIFT {msg}")

    # Runs as a detached task (see _maybe_spawn_idle_relift), so an escaping exception would surface
    # only as an unretrieved-task-exception log and mask the real error. A relift failure is never
    # fatal — the hold stands and the sampler retries next window — so swallow it and log instead.
    try:
        indices = _present_chip_indices()
        if not indices:
            return
        expected = health_monitor.expected(len(indices))

        async with _device_op("idle-relift"):
            # Re-checked under the lock: a job's gate or a reset can have taken over — and lifted or
            # re-dirtied the device — between the sampler's unlocked read and here. Recompute the mode
            # too: the hold class can have changed under the lock. The dirty axis is re-read
            # explicitly because on_fault deliberately preserves a hold's why when a dirty mark
            # lands on it, so the categories alone still read armed on a re-dirtied hold — but a
            # dirty device is the PRE-JOB GATE's to reset+verify, not this read-only path's to
            # probe or escalate around. A detached PID-1 reset scope may also still be cycling the
            # chips after its asyncio timer returned, and nothing may probe across that (the
            # concurrent-reset hazard this module exists to prevent), so bail on it exactly as the
            # gate does.
            selfheal, fabric, generic = _idle_relift_armed()
            if not (selfheal or fabric or generic) or fsm.record.dirty:
                return
            if await asyncio.to_thread(recovery_mechanism.scope_active):
                return

            async def _escalate(rung: str) -> bool:
                """Run one rung and fold its outcome into ``fsm``. Returns True iff something
                other than WAITING came back — the caller's cue to stop, the same way a direct
                ``!= OUTCOME_WAITING`` check did before ``fsm`` needed telling too.

                Through the module-global Galaxy instance, not select_recovery, for the same
                reason the gate's rungs are: the idle ladders apply on every platform (reset argv
                is re-derived per platform inside _reset_and_verify_device), while a per-target
                host's own escalate() has no ladder and reports WAITING forever — an idle hold
                there would outlive every ceiling with no rung ever attempted."""
                outcome = await galaxy_recovery.escalate(rung, indices, expected, _log)
                fsm.on_outcome(outcome)
                return outcome != OUTCOME_WAITING

            if generic:
                # A hold no read-only check can lift — the foreign holder that blocked verification,
                # or a gate that errored out. enum+ARC reads clean across a wedged eth link, so a read
                # never proves such a mesh fit; this branch never lifts on one. Past the ceiling on an
                # idle, tenant-free present mesh it escalates to the gate's galaxy reset (the one
                # authoritative proof), and holds otherwise — every guard lives in _escalate_stuck_hold.
                if await _escalate("present"):
                    return
                _log(
                    "held-unverified and no read-only check clears this hold class — holding; "
                    "escalates to the gate's galaxy reset once idle past the ceiling"
                )
                return
            # The fabric-unverified hold is the ONLY case that re-runs the traffic pass: enum+ARC
            # already passed and the fabric merely 77'd, so a retry that gets a real verdict is the
            # only thing that proves the mesh. The self-heal hold must never run it — a frozen core
            # is exactly what a traffic pass shoves off the bus.
            healthy, evidence = await fsm.observe(expected, _log, run_fabric=fabric, recovery=galaxy_recovery)
            if not healthy:
                # A chip is still off the bus: read-only there is nothing left to re-verify, and an
                # idle box has no next-job gate to run the gone-chip recovery cascade, so left here
                # the hold outlives the ceiling forever (the off-bus twin of the eth strand the
                # frozen-eth branch below closes). Past the ceiling, idle and tenant-free, hand it to
                # the gate's own gentlest-first recovery. A self-heal hold only — the fabric-
                # unverified hold is a present mesh and never routes through the off-bus cascade.
                if selfheal and await _escalate("offbus"):
                    return
                # A present mesh (nothing off the bus) can still verify UNHEALTHY on a MASS ARC
                # wedge — every chip present but its heartbeat frozen. The off-bus escalator above
                # cannot see it (off_bus==0) and the frozen-eth branch below is unreachable (that
                # needs enum+ARC HEALTHY), so read-only this held forever (blx04, 37h). It is the one
                # present-mesh fault a galaxy reset both cures and cannot invert — every chip is
                # present — so route a confirmed mass wedge (frozen chips >= the same mass-drop floor
                # the gate keys on) to the present-mesh reset the eth strand runs. A sub-floor frozen
                # count is a single self-healing chip and still holds, exactly as the gate does.
                floor = _galaxy_reset_min_dead_chips(expected)
                mass_wedge = floor is None or _frozen_chip_count(evidence) >= floor
                if selfheal and mass_wedge and await _escalate("present"):
                    return
                _log(
                    "still degraded — holding; the wedge clears on its own or the next gate resets, "
                    "never a reset here"
                )
                return
            if fabric:
                # _verify_device returns healthy=True even when the fabric pass SKIPS (77 -> ok None),
                # so gate the lift on an explicit True. An unverifiable or failed fabric on retry is
                # not proof, and reopening on it hands the next tenant a mesh no pass cleared.
                fabric_ok = (evidence.get("fabric") or {}).get("ok")
                if fabric_ok is not True:
                    _log(
                        f"fabric still could not verify on retry ({fabric_ok!r}) — holding; "
                        "the door never reopens onto unverified fabric"
                    )
                    return
            else:
                eok, edetail = await health_monitor.verify_eth_heartbeat()
                if fsm.record.why == "eth_frozen":
                    # Held for a frozen eth core, which enum+ARC cannot see. Lift ONLY on a positively-
                    # advancing heartbeat. `None` is "could not tell" (reader unconfigured, or a read a
                    # frozen core can itself hang), `False` is still frozen — both hold. The gate never
                    # lifts on those either; it follows enum+ARC with the traffic pass the relift must
                    # never run.
                    if eok is not True:
                        # enum+ARC read healthy (every chip present) but the eth verdict is frozen or
                        # unreadable — a present-mesh wedge no read the broker owns can clear. Left here
                        # the hold stands forever on an idle box (the 8h strand). Past the ceiling, with
                        # no tenant, escalate to the gate's galaxy reset, which re-inits every eth core.
                        if await _escalate("present"):
                            return
                        _log(
                            f"held for a frozen eth core but the heartbeat is not confirmed advancing "
                            f"({'unavailable' if eok is None else 'frozen'}: {edetail}) — holding"
                        )
                        return
                    if health_monitor.last_fabric_ok is False:
                        # The heartbeat advancing proves the eth FIRMWARE is alive, not that the links
                        # move data — a gate can place this hold on a measured-UNHEALTHY fabric verdict
                        # with every heartbeat ticking (a routing/link wedge the passive read is blind
                        # to). Lifting on the heartbeat alone readmits tenants onto a mesh whose last
                        # real traffic verdict was FAIL and that no pass has cleared since. Same
                        # last-verdict guard as the off-bus branch below, but here the mesh is fully
                        # present, so this is exactly the state the present-mesh galaxy reset cures:
                        # ride its guards (grace, pacing, tenant-free) instead of standing forever.
                        if await _escalate("present"):
                            return
                        _log(
                            "eth heartbeat advancing but the last fabric verdict was UNHEALTHY — "
                            "holding; the door reopens only on a healthy fabric pass, or the "
                            "present-mesh reset past the grace"
                        )
                        return
                else:
                    # Held for an off-bus drop below the reset floor: the chip LEFT THE BUS, which
                    # enum+ARC+sysfs see directly, so their agreement that it is back and answering is
                    # enough to lift — the eth reader being unconfigured (None) must not strand it, which
                    # is the whole point of this path. A configured reader that reads FROZEN still blocks:
                    # an active eth wedge is a wedge whatever first put the chip off the bus.
                    if eok is False:
                        _log(f"chip back on the bus but the eth heartbeat reads frozen ({edetail}) — holding")
                        return
                    if health_monitor.last_fabric_ok is False:
                        # enum+ARC recovered, but the last fabric verdict was UNHEALTHY: the eth cores a
                        # crashing job left unretrained come back slower than the ARC, so lifting on
                        # enum+ARC alone readmits the next tenant onto a fabric that still cannot move
                        # data — which re-wedges the chip and flaps the hold (the chip drops, self-heals,
                        # the relift lifts on ARC, the next job re-wedges it, repeat). Hold until a fabric
                        # pass returns healthy: the dirty-device pre-job gate re-runs it and lifts on
                        # recovery, and the fabric relift covers an idle box with no queued job.
                        _log(
                            "chip back on the bus and ARC-healthy, but the last fabric verdict was "
                            "UNHEALTHY — holding; the door reopens only on a healthy fabric pass"
                        )
                        return
            # Re-assert under the lock AFTER the long awaits above: the sampler's dead-chip path is
            # lock-free (_isolate_dead_chips marks the device dirty without _device_op), so a chip can
            # have dropped during _verify_device or the eth read, re-raising dirty after the guard at
            # the top of this block already saw it clear. Clearing verified now would wipe that real
            # dead-chip flag and reopen the door on a mesh short a chip. No await follows, so this
            # check holds through the clear.
            if (
                fsm.state is not ServerState.RECOVERING
                or fsm.record.dirty
                or fsm.record.why not in (SELFHEAL_WHYS | {FABRIC_RELIFT_WHY})
            ):
                _log(
                    "state changed mid-verify (a chip dropped, or a gate took over) — holding; "
                    "the pre-job gate owns it now"
                )
                return
            _log("mesh verified healthy — hold lifted")
            health_event("device_relift", evidence=evidence)
            _clear_device_dirty(verified=True, why="idle relift: verified healthy")
    except Exception:  # noqa: BLE001 - a detached relift task must never raise unretrieved
        _log("pass aborted on an unexpected error — holding; the sampler retries next window")


def _maybe_spawn_idle_relift() -> None:
    """Start one idle relift as its own task when a re-verifiable hold is up and none is in flight.
    Detached from the sampler by design (see _relift_task): awaiting a possibly-60s eth read or a
    ~45s traffic pass inline would blind the sampler's dead-chip tripwire and leave the probe's
    dead-BAR read with nothing to kill it. The spawned task re-checks every guard itself."""
    global _relift_task
    if _relift_task is not None and not _relift_task.done():
        return
    selfheal, fabric, generic = _idle_relift_armed()
    if not ((selfheal or fabric or generic) and not device_op_active):
        return
    _relift_task = asyncio.create_task(_attempt_idle_relift())


async def _kill_device_holders(reason: str) -> list:
    """SIGKILL every process holding the device, because their mappings outlive the endpoint.

    This is the one place the broker kills a foreign user's process without asking, and the
    reason is that the alternative is worse for that same user: a chip has left the bus, and
    anything still touching it — through a mapping the kernel cannot revoke — will stall a CPU
    core and reboot the machine, taking every tenant's work with it. Their job is already dead;
    only the host is still saveable.

    SIGKILL, not SIGTERM: a process wedged on a non-completing MMIO read is not going to run a
    signal handler. Never raises.
    """
    killed = []
    survivors = []
    try:
        scan = await asyncio.to_thread(enumerate_device_holders)
    except Exception:  # noqa: BLE001 - never let the scan block the rescue
        return killed
    for h in scan.holders:
        try:
            os.kill(h.pid, signal.SIGKILL)
            killed.append({"pid": h.pid, "user": h.username})
        except ProcessLookupError:
            continue  # already gone — its mapping went with it, which is the goal
        except (PermissionError, OSError) as e:
            # A holder we could NOT kill still maps the dead endpoint, and its next read stalls a
            # core and reboots the host — the exact outcome this kill exists to prevent. Swallowing
            # it read as "all holders cleared"; name the survivor so the residual host risk is on
            # the record instead.
            survivors.append({"pid": h.pid, "user": h.username, "error": str(e)})
    # The broker's own device work. A fabric check mid-traffic-pass is the likeliest holder of
    # all — it maps every chip — and it is the one that killed a host, twice.
    for proc, who in ((health_monitor.fabric_check_proc, "[broker]fabric-check"), (current_process, "[broker]job")):
        if proc is None:
            continue
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            killed.append({"pid": proc.pid, "user": who})
        except (ProcessLookupError, OSError):
            pass
    if logger:
        logger.error(
            f"DEAD-CHIP killed {len(killed)} device holder(s) — {reason}. Their "
            f"mappings outlive the endpoint, and a read through one of them stalls a "
            f"CPU core and reboots the host. Killed: {killed}"
        )
    if survivors and logger:
        logger.error(
            f"DEAD-CHIP could NOT kill {len(survivors)} device holder(s): {survivors}. "
            f"Each still maps the dead endpoint; a read through one stalls a CPU core and "
            f"reboots the host — the rescue did not fully take."
        )
    # Name the job we just SIGKILLed. The pids alone could not answer "what happened to my
    # job?" — the broker knew, recorded everything except the one field that ties it to the
    # job, and then reported the job as "interrupted" with no cause. The broker serializes,
    # so the running job is unambiguous.
    victim = next((j.id for j in jobs.values() if j.status == JobStatus.RUNNING), None)
    health_event(
        "device_holders_killed",
        reason=reason,
        killed=killed,
        job_id=victim,
        survivors=survivors,
        host_at_risk=bool(survivors),
    )
    if victim:
        # This job's SIGKILL is OURS — we killed it to stop its mapping of an off-bus chip from
        # hanging the host, not because its own code crashed. Record it the same as a reset-killed
        # job so the runner (a) does not read the kill as fresh evidence the mesh needs a reset (the
        # reset justifying itself) and (b) tells the submitter why, instead of a bare signal exit.
        # Persist it too, so the reboot this recovery may trigger does not re-run the exact job that
        # wedged the device (a boot loop) — restore + scope reconciliation drop it on the way back up.
        _mark_job_device_fault_failed(victim, f"device recovery kill: {reason}")
    _note_tenant_gate_verdict(f"device recovery: chip(s) left PCIe bus ({reason})")
    return killed


def _boot_btime_id() -> str:
    """This boot's ``/proc/stat`` btime as a stable id string, or "" if unreadable. It is the
    raw boot epoch, constant across the many broker restarts within one boot and changing only
    on a real reboot — the per-boot dedup key when the kernel random boot_id cannot be read."""
    try:
        for ln in Path("/proc/stat").read_text().splitlines():
            if ln.startswith("btime "):
                return ln.split()[1]
    except (OSError, IndexError):
        pass
    return ""


def _since_boot_sec() -> float:
    """Seconds from this boot's start to now, read from ``/proc/stat`` btime. Used to
    anchor the reboot's history row at when the box came back, not at whatever second the
    broker got around to recording it. 0.0 if btime is unreadable — the row then lands at
    now, which on a systemd-at-boot start is still within seconds of the reboot."""
    btime = _boot_btime_id()
    if btime:
        try:
            return max(0.0, time.time() - float(btime))
        except ValueError:
            pass
    return 0.0


def _reboot_why_status(rec: dict) -> tuple[str, str]:
    """The one-line why + status for a retroactively-detected reboot's history row.

    A firmware crash record means the box did not come down cleanly, and its class (PCIe
    vs processor) points at opposite investigations; its absence means a clean shutdown or
    an intentional reboot. The two must not read the same in the jobs list."""
    if not rec.get("present"):
        return "reboot — no firmware crash record (clean shutdown or intentional)", "reboot"
    kind = "PCIe" if rec.get("is_pcie") else "processor" if rec.get("is_processor") else "other"
    return (f"reboot — previous boot ended in a {kind} hardware error " f"({rec.get('event_severity', '?')})", "crash")


def _consume_pre_down_escalation_row(action: str) -> None:
    """Remove the '[broker]{action}-request' row _fire_recovery_escalation wrote just before it
    took the box down, now that the boot it caused is back up and the post-boot back-fill is about
    to write the authoritative '[broker]{action}' row for the SAME escalation.

    That request row exists so the escalation is visible even if the box never returns; once it
    does, the back-fill supersedes it with a row anchored at the real reboot time and carrying the
    true downtime, so keeping both shows one reboot as two — the very confusion G6 exists to remove.
    An abrupt power cycle may never have synced the request row; then there is nothing to remove and
    the back-fill still stands alone. Best-effort: a stranded duplicate is cosmetic, never a fault."""
    if not job_log_dir:
        return
    owner = f"[broker]{action}-request"
    for p in sorted(
        (q for q in Path(job_log_dir).glob("*.log") if q.name != "server.log"), key=lambda q: q.name, reverse=True
    ):
        try:
            with open(p, errors="replace") as fh:
                head = [next(fh, "") for _ in range(6)]
        except OSError:
            continue
        row_owner = next(
            (v.strip() for k, sep, v in (ln.partition(":") for ln in head) if sep and k.strip() == "OWNER"), None
        )
        if row_owner == owner:
            try:
                p.unlink()
            except OSError:
                pass
            return


def _record_previous_boot_error() -> None:
    """Journal the firmware's account of the last crash, once per boot.

    Deduplicated on boot time: the broker restarts many times within one boot (updates,
    watchdog), and the same crash record re-logged on each restart would make a single
    reboot look like many, which is precisely the kind of error that has been making
    these boxes hard to reason about.
    """
    boot_id = _current_boot_id()
    # Dedup key for the once-per-boot guard: the kernel random boot_id, or /proc/stat btime when
    # that is unreadable. btime also changes only on a real reboot, so a host that cannot serve a
    # boot_id still records one row per boot instead of a fresh back-fill on every broker restart
    # within the boot. boot_id itself, not this key, drives the escalation attribution and
    # mark_boot below — those need the kernel id and fail closed without it.
    boot_key = boot_id or _boot_btime_id()
    marker = health_dir() / f".boot_recorded_{boot_key or 'unknown'}"
    if boot_key and marker.exists():
        return

    rec = previous_boot_error()

    # The discriminator. Both live theories for these reboots — AMD erratum 1431, and a core
    # stalled on an MMIO access to a hung accelerator — produce the SAME EX-unit watchdog
    # record, so the firmware's account cannot tell them apart. Only 1431 needs a bus lock.
    # A crash whose boot recorded zero bus locks cannot be 1431.
    bus_locks = previous_boot_bus_locks()
    if rec.get("present"):
        kind = "PCIe" if rec.get("is_pcie") else "processor" if rec.get("is_processor") else "other"
        if logger:
            logger.warning(
                f"PREVIOUS BOOT ended in a {rec.get('event_severity', '?')} hardware error "
                f"[{kind}]: {rec.get('section_type', '?')} / "
                f"{rec.get('error_structure_type', '?')} — this box did not shut down cleanly"
            )
            if bus_locks is not None:
                logger.warning(
                    f"PREVIOUS BOOT bus locks: {bus_locks} — "
                    + (
                        "erratum 1431 needs a bus lock and there were none, so it is NOT 1431"
                        if bus_locks == 0
                        else "consistent with AMD erratum 1431 (SMT + bus lock hangs a core)"
                    )
                )
        capture_incident(
            "previous_boot", job={"id": "boot"}, evidence={"bert": rec, "previous_boot_bus_locks": bus_locks}
        )
    elif logger:
        logger.info("PREVIOUS BOOT: no firmware error record (clean shutdown, or nothing logged)")

    health_event(
        "previous_boot_error",
        boot_id=boot_id,
        previous_boot_bus_locks=bus_locks,
        **{k: v for k, v in rec.items() if k != "raw"},
    )

    # A reboot takes the whole box off the bus and back, exactly the device event a reset
    # is — but it was only ever a health_event, absent from the jobs list that is the
    # operator's record of what touched the device. Back-fill a visible row for it, once
    # per boot (the boot-id dedup above already guards that), anchored at this boot's start
    # so it lands in history where the reboot happened rather than at broker-start.
    escalation = recovery_mechanism.boot_from_broker_escalation(boot_id)
    if escalation:
        action = escalation["action"]
        why = escalation.get("reason") or "device wedged, reset ineffective"
        reboot_owner = f"[broker]{action}"
        reboot_why = f"{action.replace('-', ' ')} — broker auto-recovery ({why})"
        reboot_status = action
        _consume_pre_down_escalation_row(action)
    else:
        reboot_owner = "[broker]reboot"
        reboot_why, reboot_status = _reboot_why_status(rec)
    write_action_log(reboot_owner, reboot_why, _since_boot_sec(), reboot_status, None)

    if boot_id:
        mark_boot(boot_id)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat())
        # One marker per boot; the old ones are noise.
        for old in health_dir().glob(".boot_recorded_*"):
            if old != marker:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _stack_versions() -> dict:
    """The versions that define this host's behaviour: kernel driver, chip firmware,
    tt-smi, host kernel. An unvalidated mix of these is a live hypothesis for the
    instability, and it cannot even be tested unless each event knows what it ran on."""
    env: dict = {"kernel": platform.release()}
    try:
        env["kmd"] = Path("/sys/module/tenstorrent/version").read_text().strip()
    except OSError:
        pass
    # Firmware is per-chip and CAN differ across a mesh; record the distinct set, because
    # "all 32 agree" and "one chip is a version behind" are very different situations.
    fws = sorted({rec.get("tt_fw_bundle_ver") for rec in chip_snapshot().values() if rec.get("tt_fw_bundle_ver")})
    if fws:
        env["fw_bundle"] = fws[0] if len(fws) == 1 else fws
    try:
        out = subprocess.run(["tt-smi", "--version"], capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            env["tt_smi"] = (out.stdout or out.stderr).strip().splitlines()[-1][:80]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return env


async def _set_device_pollers(active: bool, log) -> list[str]:
    """Stop (or restart) the daemons that continuously poll every chip.

    A reset must land on a quiet bus. These pollers keep issuing MMIO at a chip
    that is mid-reset or already wedged, and non-completing transactions at a dead
    endpoint are what drive the root complex past its error threshold — at which
    point firmware-first RAS resets the whole host. Quiescing them is the
    difference between a 60s device reset and an ungraceful reboot.

    Best-effort by design: a poller we cannot stop must not block the recovery.
    """
    verb = "start" if active else "stop"
    touched = []
    for svc in DEVICE_POLLER_SERVICES:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl",
                verb,
                svc,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode == 0:
                touched.append(svc)
        except (asyncio.TimeoutError, OSError, ValueError):
            continue
    if touched:
        log(f"{'restarted' if active else 'stopped'} device pollers: {', '.join(touched)}")
    elif DEVICE_POLLER_SERVICES:
        # Pollers were configured, yet the verb reached none of them: on this host they resolve
        # to no active unit. Quiescing was a no-op, so a reset about to run cannot assume it lands
        # on a quiet bus — the real pollers may still be hammering MMIO under a name we were never
        # told. On the stop path that is the host-reboot hazard the quiesce exists to prevent, so
        # flag it host_at_risk; either way, do not let the absence read as a clean quiesce.
        log(f"WARNING: no configured device poller answered '{verb}': " f"{', '.join(DEVICE_POLLER_SERVICES)}")
        health_event("pollers_none_active", verb=verb, configured=list(DEVICE_POLLER_SERVICES), host_at_risk=not active)
    health_event("pollers_" + ("restarted" if active else "quiesced"), services=touched)
    return touched


def _auto_reboot_enabled() -> bool:
    """Whether the broker may reboot the host as a recovery rung. ON by default, because a ladder
    whose upper rungs are opt-in is a ladder that ends in a permanent hold on any host nobody
    remembered to arm — and a device held forever takes every tenant's work down just as surely as
    the reboot does, only silently and for longer. The gentler rungs still run first and this one
    fires only once they have failed; it is also skipped wherever it provably cannot help (see
    _host_escalation_for_drop). Opt OUT with TT_DEVICE_MCP_AUTO_REBOOT=0 on a host where an
    unattended reboot is worse than a stuck queue.

    Armed also means fireable (I14, I17). The reboot is issued as `systemctl reboot`, so a daemon
    that is not root, or a container with no init, reports the rung OFF instead of climbing to a
    call the kernel refuses — which reads to the router as a ladder whose last step exists."""
    if os.environ.get("TT_DEVICE_MCP_AUTO_REBOOT", "1").strip() == "0":
        return False
    return privileges.is_root() and privileges.has_systemd()


def _auto_power_cycle_enabled() -> bool:
    """Whether the broker may BMC-power-cycle the host as the final recovery rung. ON by default,
    and still a SEPARATE switch from the reboot because it is the more drastic of the two: it pulls
    chassis power, the only thing that recovers a chip that has LEFT the PCIe bus. Nothing below it
    can: the per-chip rung needs a parent bridge the departed chip no longer has, the tray rung
    exists only on a Galaxy, and a warm reboot does not re-power the silicon. Without this rung a
    dropped ASIC is unrecoverable in software, which is the permanent hold this default exists to
    end. It needs a reachable BMC + ipmitool; where those are absent the fire raises and the ladder
    reports it rather than silently holding. Opt OUT with TT_DEVICE_MCP_AUTO_POWER_CYCLE=0.

    Enabled means ARMED AND FIREABLE. A host with no ipmitool cannot cold-cycle, and reporting the
    rung as on there would make the ladder look terminating when its last step cannot run — the
    router would keep choosing a rung that always raises. Reading the binary here instead collapses
    to the already-loud BLOCKED path: the cascade emits its power-cycle-required event and holds."""
    if os.environ.get("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1").strip() == "0":
        return False
    return privileges.can_ipmi()


def _host_rung_opted_in() -> bool:
    """Whether ANY host-level rung is armed on this host — the one thing the gate still asks about
    the host escalation the router already chose for it, and only to pick the journal line a hold
    deserves: a rung suppressed over an in-flight reset or below the reboot floor is a real
    decline, while a host rung nobody opted into is the default. Equivalent to
    ``_choose_recovery_escalation() is not None`` (which returns None exactly when neither rung is
    opted in) without repeating that fold's durable-ledger read, which the router already paid."""
    return _auto_reboot_enabled() or _auto_power_cycle_enabled()


def _host_escalation_kwargs() -> dict:
    """The callables :func:`_host_escalation_for_drop` (health.recovery.galaxy) needs injected:
    this broker's own auto-reboot/auto-power-cycle opt-ins and its RecoveryMechanism's loop-guard
    read. Bundled once so the gate's several call sites do not each re-list them."""
    return dict(
        auto_reboot_enabled=_auto_reboot_enabled,
        auto_power_cycle_enabled=_auto_power_cycle_enabled,
        reboot_already_attempted=recovery_mechanism._reboot_already_attempted,
    )


def _frozen_chip_count(evidence: dict) -> int:
    """Chips present on the bus but ARC-frozen — the two-sample ``stalled`` list a heartbeat verdict
    records. A galaxy reset re-inits them and, unlike an off-bus drop, cannot invert the mesh doing
    so: every frozen chip is already present, so there is nothing for the reset to take down with it.
    The mass-reset floor counts these alongside the off-bus chips so an all-frozen present mesh does
    not read as 0-off-bus and hold below the floor forever (the blx04 present-mesh deadlock)."""
    return len((evidence.get("heartbeat") or {}).get("stalled") or [])


def _emit_all_off_bus_power_cycle_required(log, off_bus: int, expected: int, *, context: str) -> None:
    """Fail closed, loudly, on the one wedge no opted-in rung can recover: every chip off the bus on
    a multi-chip host with no auto power cycle. A warm reboot cannot re-enumerate dropped Galaxy
    ASICs, so there is no automatic action left — name the manual one and leave the device HELD.
    Absence is never health: this loud escalation is what must never be swallowed into a release."""
    health_event(
        "all_chips_off_bus_power_cycle_required",
        off_bus=off_bus,
        expected=expected,
        present=max(0, expected - off_bus),
        auto_power_cycle_enabled=_auto_power_cycle_enabled(),
        host_at_risk=True,
    )
    log(
        f"ALL {expected} chips are off the PCIe bus [{context}] — a whole-bus wedge a warm reboot "
        f"CANNOT clear (a 6U-Galaxy reboot does not power-cycle the UBBs, so dropped ASICs stay "
        f"off). No opted-in rung can recover this: set TT_DEVICE_MCP_AUTO_POWER_CYCLE=1 to let the "
        f"broker cold-cycle the chassis, or power-cycle it manually (BMC/ipmitool). Device stays HELD."
    )


async def _auto_power_cycle_host(log, reason: str) -> None:
    """Fire the final recovery rung: a BMC chassis power cycle, above the warm reboot because it
    recovers a whole-bus wedge the reboot cannot. The caller has already confirmed both the env
    opt-in AND ``RecoveryMechanism.auto_recovery_allowed``."""
    await recovery_mechanism._fire_recovery_escalation(
        "power-cycle",
        row_owner="[broker]power-cycle-request",
        label="auto BMC power cycle",
        detail="reset and a host reboot could not recover the device",
        fire=_fire_power_cycle,
        log=log,
        reason=reason,
    )


# The pre-job gate's dispatch proof runs on a tenant's critical path, so it is the minimal
# single-kernel program, not the whole-mesh probe. distributed_program_dispatch opens all eight
# chips in ~14s and is the fabric check's stage; paying that per job is the cost this gate exists
# to avoid. add_2_integers_in_compute enqueues one compute kernel — the cheapest proof a kernel runs.
DISPATCH_PROBE_BIN = os.environ.get(
    "TT_DEVICE_MCP_DISPATCH_BIN",
    "/opt/tt-device-broker/validator/current/build/programming_examples/" "metal_example_add_2_integers_in_compute",
)
DISPATCH_PROBE_TIMEOUT_SEC = int(os.environ.get("TT_DEVICE_MCP_DISPATCH_TIMEOUT_SEC", "90"))


def _prejob_dispatch_enabled() -> bool:
    """Whether the PRE-job gate proves dispatch before admitting a tenant. OFF, and it should stay
    off until this probe is fast AND stable: measured 14.2s once, then 38.4s and an abort (exit -6)
    on the next real submit, whose dirty flag then dragged a 102s fabric pass onto the same
    submitter. A pre-job check must be fast and certain; this one is neither yet."""
    return os.environ.get("TT_DEVICE_MCP_PREJOB_DISPATCH", "0").strip() == "1"


async def _dispatch_probe_ok(job_log_file: "Optional[Path]" = None) -> bool:
    """PRE-job gate predicate: may a tenant be admitted, as far as dispatch is concerned?

    Wraps the probe with the gate's policy. Returns True to admit — dispatch is proven, the probe
    is opted out on this host, or it could not run (a skip is not a failure: 'I could not ask' is
    not 'the answer is no', and flagging on it would hold every queue on a box with no probe yet).
    Returns False only when a kernel was enqueued and failed or hung; in that case it flags the
    device so the tenant hold keeps the job at the door and the recovery ladder owns what happens
    next. Flag, never reset. Never raises."""
    if not _prejob_dispatch_enabled():
        return True
    ok, detail = await _dispatch_probe()
    verdict = (
        f"HEALTH-GATE[pre-job] dispatch probe: "
        f"{'SKIPPED' if ok is None else ('OK' if ok else 'UNHEALTHY')} — {detail}"
    )
    if logger:
        logger.info(verdict)
    # Record the verdict in the tenant's OWN job log, not just server.log. The per-job log is where
    # a run's history reads back and is the tenant-visible record — every other health-gate line
    # lands there (see _device_health_gate). logger.info alone leaves the per-job log with no proof
    # the gate ever proved dispatch, so a real gate run is indistinguishable from a silent no-op.
    if job_log_file:
        try:
            append_job_log(job_log_file, "broker", f"{verdict}\n")
        except OSError:
            pass
    if ok is False:
        health_event("prejob_dispatch_unhealthy", detail=detail)
        _mark_device_dirty(f"pre-job dispatch probe: {detail}")
        return False
    return True


async def _dispatch_probe() -> tuple[Optional[bool], str]:
    """Can the mesh still RUN A KERNEL? Enqueues one tiny workload and waits for it.

    Every other check passes on a mesh that cannot dispatch: enumeration and the ARC heartbeat are
    reads, and the fabric pass drives inter-chip ethernet without ever enqueuing a program — it
    returned exit 0 seventy seconds before a cold worker hung in warmup. So a tenant was admitted
    onto a dead mesh and died at exit 134 in 24s, and the device was only marked dirty afterwards.

    A BINARY from the pinned tt-metal SHA, never a python script: the script cost ~15s per device
    to open, tripped its own cap on a HEALTHY mesh, and its false verdict drove two reboots. Same
    three-state contract as the fabric check — ``None`` is SKIPPED and the caller must never reset
    on it, because 'I could not ask' is not 'the answer is no'. Never raises."""
    bin_path = Path(DISPATCH_PROBE_BIN)
    if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
        health_monitor._journal_skip_once("dispatch_probe_unavailable", "not_built")
        return None, f"skipped (no dispatch probe at {bin_path})"
    _t0 = datetime.now()
    # Same runtime contract the fabric check gives the validator: these binaries JIT their kernels
    # from the runtime root, so without TT_METAL_HOME the probe fails on a perfectly healthy mesh —
    # and a false UNHEALTHY here holds every job on the box. Run from the pinned tree, and keep the
    # kernel cache off any tenant's.
    root = Path(os.environ.get("TTDEV_DISPATCH_RUNTIME_ROOT", "/opt/tt-device-broker/validator/current"))
    cache = os.environ.get("TTDEV_DISPATCH_CACHE", "/var/cache/tt-device-broker/dispatch-tt-metal-cache")
    # The sibling fabric and eth-heartbeat checks mkdir their cache before launching; the probe
    # must too, or a missing dir turns a healthy mesh into a non-zero exit — a false UNHEALTHY that
    # holds the whole box. Best-effort: if the dir cannot be made, let the binary report why.
    try:
        Path(cache).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env = {**os.environ, "TT_METAL_HOME": str(root), "TT_METAL_RUNTIME_ROOT": str(root), "TT_METAL_CACHE": cache}
    try:
        proc = await asyncio.create_subprocess_exec(
            str(bin_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(root) if root.is_dir() else str(bin_path.parent),
            env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), DISPATCH_PROBE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            dt = (datetime.now() - _t0).total_seconds()
            # A hang IS the finding here: the probe is a few seconds of work on a live mesh.
            return False, (
                f"dispatch probe still running after {DISPATCH_PROBE_TIMEOUT_SEC}s "
                f"({dt:.0f}s) — the mesh accepted a workload and never finished it"
            )
    except OSError as e:
        return None, f"skipped (dispatch probe could not run: {e})"
    dt = (datetime.now() - _t0).total_seconds()
    if proc.returncode == 0:
        return True, f"a kernel ran to completion on the mesh ({dt:.1f}s)"
    tail = (out or b"").decode("utf-8", "replace").strip().splitlines()
    return False, (f"dispatch probe exited {proc.returncode} ({dt:.1f}s)" + (f": {tail[-1][:160]}" if tail else ""))


async def device_health_gate(
    job_log_file: Optional[Path],
    *,
    phase: str,
    run_fabric: bool,
    force_fabric: bool = False,
    with_recover: bool = True,
) -> None:
    """Unified health gate run while the device is IDLE around a job.

    ``phase`` labels the caller ('pre-job', 'post-job', 'startup') in the logs and the
    device op; it steers nothing. Always (when enabled) runs a tt-smi snapshot (chips +
    ARC), and runs the configured fabric traffic check when ``run_fabric``. If the device
    is flagged dirty, or any check finds it
    unhealthy, it resets + verifies here so no job starts on — and no wedge is left
    behind by — a half-dead mesh. Logs every step into the job's own log. Never
    raises; on any error it clears the dirty flag and proceeds (so the queue never
    stalls).

    ``with_recover=False`` runs the pass and records its verdict but never enters the recovery
    ladder — the read-only shape an external scheduler drives before a job, where the ladder's
    minutes would be paid on the critical path of every node in an allocation. The FSM is still
    written: the pass's finding is the one truth about the mesh, and a pass that saw a fault and
    recorded nothing would admit the next job onto it."""

    def _log(msg: str) -> None:
        if logger:
            logger.info(f"HEALTH-GATE[{phase}] {msg}")
        if job_log_file:
            try:
                append_job_log(job_log_file, "broker", f"[health-gate/{phase}] {msg}\n")
            except OSError:
                pass

    # Safety: never auto-reset over a real tenant who somehow holds the device
    # (e.g. a non-broker process). A board reset would abort their run.
    scan = await asyncio.to_thread(enumerate_device_holders)
    foreign = [h for h in scan.holders if h.uid >= MIN_TENANT_UID]
    if foreign:
        who = ", ".join(f"{h.username}(pid {h.pid})" for h in foreign)
        _log(f"skip: device held by {who}; not touching it")
        _clear_device_dirty_unverified(f"foreign holder present: {who}", fault="foreign_holder")
        # A door already held (a startup verify, a prior fault) outlives this skip: the verify
        # cannot run beside a foreign tenant, so the hold waits them out — and reads to the tenant
        # queued behind it as a bare "device unverified", a device fault, when the blocker is
        # another user's process. Name them so the status says who to wait on. Reword only: a
        # foreign holder on an unheld device is ordinary contention, not a hold to invent.
        fsm.note(f"held: {who} holds the device; verify deferred until it releases")
        return

    indices = _present_chip_indices()
    if not indices:
        # Absence is never health. On a host whose baseline expects chips, an empty device-node
        # dir is every chip gone off the bus — the one unambiguously catastrophic state, not a
        # device-less box. Clearing here is the fail-open that read a dead mesh as fit and dropped
        # the hold the startup probe had just correctly raised, so the escalation ladder never ran
        # and the box sat dead while the broker reported itself fine. Stay dirty and held.
        expected = health_monitor.expected(0)
        if expected > 0:
            _log(
                f"no /dev/tenstorrent devices present but the baseline expects {expected} "
                f"chips; every chip is off the bus — holding, not releasing"
            )
            health_event("all_chips_missing", present=0, expected=expected, host_at_risk=True)
            _mark_device_dirty(
                f"all {expected} chips off the bus: no /dev/tenstorrent devices present", why="heartbeat"
            )
            return
        # A host that has never shown a chip has nothing to hold for and nothing that will ever
        # verify; holding would shut the door once and never reopen it.
        _log("no /dev/tenstorrent devices present; skipping")
        _clear_device_dirty_unverified("no /dev/tenstorrent devices present", holds=False)
        return
    expected = health_monitor.expected(len(indices))

    if not health_monitor._health_check_enabled():
        _log("health checks disabled (TT_DEVICE_MCP_HEALTH_CHECK=0)")
        _clear_device_dirty_unverified("health checks disabled", holds=False)
        return

    # Chosen once per gate pass, not per call within it: select_recovery is cheap (cached
    # board-type reads) but its RESULT — which platform's reset command/verify loop this pass
    # uses — must be the same answer at every call site below, not re-derived and possibly
    # flip mid-pass if the board-type cache changed between calls.
    recovery = select_recovery()

    # Everything past this point touches the device. Serialize it against the reset
    # tool and against a gate running for another job: concurrent -glx_reset calls
    # at the same 32 ASICs turn one wedged chip into a mesh that needs a power-cycle.
    async with _device_op("health-gate/" + phase):
        # This pass's own snapshot of what the FSM already knew coming in — read once, used
        # throughout, never re-derived: a mid-pass fsm mutation (e.g. the eth-frozen hold below)
        # must not retroactively change what THIS pass decided "coming in dirty" meant.
        dirty = fsm.state is not ServerState.HEALTHY and fsm.record.dirty
        dirty_reason = fsm.record.detail or fsm.record.why
        dirty_job = fsm.record.job

        # A job that ended badly is a reason to LOOK HARD at the device, not a verdict
        # about it. A timeout or a kill says something about the job; the silicon it ran
        # on is a separate question, and we can answer that question directly. So when
        # the device is flagged dirty we pay for the full check — including the fabric
        # traffic pass, which is the only thing that can see an ethernet core a crashing
        # job left unretrained — and then believe the answer.
        #
        # The traffic pass costs ~45s, so it is gated: a dirty device or a failed job
        # (force_fabric) always pays; otherwise it runs only when a caller asks
        # (run_fabric) and no recent pass is fresh. The post-job gate does NOT ask on a
        # clean exit — a mesh whose chips all tick has nothing the pass would find that a
        # later job failure would not surface, and charging every clean job 45s is how a
        # device ends up being checked instead of used. Only startup asks; the stale
        # window bounds any run_fabric caller to one pass per interval.
        global last_fabric_check_monotonic
        # 0.0 is the sentinel for "no pass this process" — the startup check after a boot.
        # It must count as stale outright: subtracting it treats the monotonic clock's own
        # value as an elapsed time, so on a host whose uptime is still below the interval
        # (exactly a box that reboots a lot) the pass would be skipped, and the one moment
        # it most needs to run — freshly-booted silicon — is the one it would skip.
        never_run = last_fabric_check_monotonic == 0.0
        stale = never_run or (time.monotonic() - last_fabric_check_monotonic) > FABRIC_CHECK_MIN_INTERVAL_SEC
        # NOTHING SLOW RUNS BEFORE A JOB. The traffic pass takes ~45-100s, and pre-job is the one
        # phase where that lands on a submitter who is only waiting to start: a dirty flag alone
        # used to force it, so an ordinary submit paid 102s and was held anyway. A device that is
        # dirty pre-job is held on the flag itself — the flag is already the answer, and measuring
        # it again does not change the verdict, it only bills the tenant for it. The pass still runs
        # where it costs nobody: post-job, at startup, and inside the recovery ladder, all of which
        # pass force_fabric or run with the device idle.
        prejob = phase == "pre-job"
        # with_recover=False never pays for the traffic pass: it is the one thing in this
        # function that costs real time, and a read-only pass has no rung to spend it on.
        full = with_recover and (not prejob) and (dirty or force_fabric or (run_fabric and stale))
        healthy, evidence = await fsm.observe(expected, _log, run_fabric=full, phase=phase, recovery=recovery)
        state = HealthState.from_evidence(evidence, phase=phase, expected=expected)
        fabric_ok = state.fabric_ok
        if fabric_ok is not None:
            last_fabric_check_monotonic = time.monotonic()

        # The full chip state behind every decision, and the bus-error totals inline so a
        # rising trend is visible without opening the chip journal. If the theory that a
        # wedged chip walks the root complex to a fatal error is right, it shows here.
        chips = await asyncio.to_thread(chip_snapshot_event, f"gate/{phase}", healthy=healthy, dirty=dirty)
        health_event(
            "gate",
            phase=phase,
            healthy=healthy,
            dirty=dirty,
            dirty_reason=dirty_reason,
            dirty_job=dirty_job,
            aer=aer_totals(chips),
            evidence=evidence,
        )

        # Something went wrong: freeze the evidence while it still exists. The kernel's
        # account of the bus is the piece that has never been captured, and an ungraceful
        # reboot — the failure mode we are chasing — destroys it.
        if not healthy or dirty:
            bundle = await asyncio.to_thread(
                capture_incident,
                "unhealthy" if not healthy else "dirty",
                job=dirty_job,
                job_log=job_log_file,
                trace=list(sampler.ring),
                reset_output=recovery_mechanism.last_reset_output,
                fabric_output=health_monitor.last_fabric_output,
                evidence=evidence,
            )
            if bundle:
                _log(f"incident evidence saved: {bundle}")
                health_event(
                    "incident_captured", path=str(bundle), healthy=healthy, dirty=dirty, trace_samples=len(sampler.ring)
                )

        if not with_recover:
            # A read-only pass stops at the verdict. Everything past the router is an action on
            # the device, and this caller's contract is that it takes none.
            _log(f"read-only pass: {'healthy' if healthy else 'unhealthy'}, no recovery attempted")
            health_event("gate_read_only", phase=phase, healthy=healthy, dirty=dirty)
            if not healthy:
                # The FSM is still written: a fault this pass saw and did not record is a fault
                # the very next queue job would be dispatched onto.
                _mark_device_dirty(f"gate/{phase}: read-only pass found the device unhealthy", why="probe_unhealthy")
            return

        # Fix A: a runtime eth fault the fabric traffic pass now CLEARS (a real OK verdict — every
        # inter-chip link moved data) is retired here, so the door reopens the moment the fabric proves
        # the mesh fit — not 20 minutes later after a galaxy reset of 32 healthy ASICs. The fabric pass
        # is the authoritative eth/fabric check; enum+ARC are blind to a wedged core, but a pass that
        # got traffic across every link is not silence, it is proof. Guarded against the validator's
        # second-erisc blind spot: if the same fault keeps recurring fast despite the fabric passing,
        # the pass is not reaching this wedge — stop retiring and let it escalate to a reset.
        if healthy and device_fault_reported and fabric_ok is True:
            global _fabric_ok_retire_monotonic, _fabric_ok_retire_streak
            now_mono = time.monotonic()
            if now_mono - _fabric_ok_retire_monotonic < FABRIC_RETIRE_RECURRENCE_SEC:
                _fabric_ok_retire_streak += 1
            else:
                _fabric_ok_retire_streak = 1
            _fabric_ok_retire_monotonic = now_mono
            if _fabric_ok_retire_streak <= FABRIC_RETIRE_MAX_STREAK:
                _log(
                    f"runtime reported '{device_fault_reported}', but the fabric traffic pass now "
                    f"moves data across every link — retiring the fault and reopening the door "
                    f"(the fabric check is the authoritative eth/fabric validator; a galaxy reset "
                    f"here would be 32 healthy ASICs for a wedge the fabric says is gone)"
                )
                health_event("fault_retired_on_fabric_ok", phase=phase, streak=_fabric_ok_retire_streak)
                _clear_device_reported_fault(f"gate/{phase}: fabric verified healthy — runtime fault retired")
                # device_fault_reported is now clear -> the healthy branch below clears + reopens
            else:
                _log(
                    f"runtime reported '{device_fault_reported}' and it has recurred "
                    f"{_fabric_ok_retire_streak}x despite the fabric passing each time — the traffic "
                    f"pass is not reaching this wedge (a second-erisc core it cannot drive); NOT "
                    f"retiring on the fabric-OK, letting it escalate to a reset that re-inits both eriscs"
                )
                health_event("fault_retire_refused_recurring", phase=phase, streak=_fabric_ok_retire_streak)

        # Every escalation decision from here on is the router's. The gate builds the evidence THIS
        # pass read, asks for the one gentlest-first rung that evidence justifies, and then either
        # records the hold — whose flavour, wording and journal only the gate has — or hands the
        # rung to escalate() to fire. The router asked is the GALAXY platform's, because the gate's
        # ladder has always been one ladder for every host: the reset floor, the surgical rung, the
        # tray walk and the host rungs are the policy at a job boundary whatever the board is, and
        # only the reset argv is platform-specific (resolved inside the reset itself, per pass).
        # PerTargetRecovery.next_stage names a strictly smaller ladder for a caller that wants the
        # platform's own view of one; steering the gate with it would fire the mesh-wide reset on
        # the single-chip wedge the floor exists to hold.
        #
        # The kill switch is folded in here rather than read inside the router: with the hold
        # switched off a frozen core must fall through to the ordinary unhealthy ladder, and it is
        # the gate that then owes the hold its flavour either way.
        eth_frozen = state.eth_frozen and _eth_freeze_holds()
        # The door-closed decisions — release, the fabric-unverified hold, and the eth-frozen and
        # cooldown holds — are the router's first branches and read nothing but this pass's probe:
        # no off-bus count, no reset scope, no bridge-reset candidate. Ask them BEFORE paying for
        # those reads (a systemctl scope query, a two-sample gone-chip settle window), so a clean
        # gate pass costs neither. off_bus/scope_active carry their unestablished defaults until the
        # mesh has actually failed the door-closed checks, and are filled in below.
        ev = Evidence(
            off_bus=0,
            frozen_chips=state.frozen_chips,
            expected=expected,
            sbr_candidates=len(isolated_chips),
            eth_frozen=eth_frozen,
            cooling=recovery_mechanism.cooling(),
            scope_active=False,
            # Whether a prior full recovery cycle ALREADY failed to revive this device. Read before
            # this pass resets, because _reset_and_verify_device overwrites it: reaching the host
            # rung with this True means a reset failed, the reset cooldown then elapsed, and the
            # reset below fails too — sustained unrecoverability, which is what justifies taking the
            # box down. One failed reset is not "clearly won't recover".
            was_failing=recovery_mechanism.last_reset_failed,
            healthy=healthy,
            fault_reported=bool(device_fault_reported),
            fabric_ok=fabric_ok,
            fabric_ran=state.fabric_ran,
            dirty=dirty,
            fabric_forced=full,
            last_action=None,
            last_action_recovered=None,
            off_bus_before=None,
            reset_exit_nonzero=False,
            holding_fabric_unverified=bool(fsm.record and fsm.record.why == "fabric_unverified"),
        )
        stage = galaxy_recovery.next_stage(ev)

        if stage == RELEASE:
            # Every check the host can run says the mesh is fine. Resetting anyway —
            # because a job happened to time out — is a 60s reset of 32 healthy ASICs,
            # and doing that on every abnormal exit is how the device spends its day
            # being reset instead of being used.
            #
            # A fault the runtime itself named is the exception, and the only one: "healthy"
            # here is the verdict of a tt-smi snapshot and a fabric pass that runs with a
            # second erisc disabled, and neither can see a stuck ethernet core. Their
            # agreement is not evidence against a fault they cannot observe — it is silence.
            # Believing it is what let a box pass every check while every tenant job died on
            # the same core.
            if dirty:
                _log(f"device verified healthy (incl. fabric) despite: {dirty_reason}" f" — no reset needed")
            else:
                _log("device healthy; no reset needed")
            _clear_device_dirty(verified=True, why=f"gate/{phase}: verified healthy")
            if device_hold_logged:
                _note_tenant_gate_verdict("")
            return

        if stage == HOLD_FABRIC_UNVERIFIED:
            # The fabric pass RAN but returned no verdict (77 — the validator tested no link, e.g.
            # eth links that never trained). On a multi-chip host that is NOT "the mesh is fine":
            # enum+ARC are blind to a wedged eth core, so reading it healthy admits tenants onto a
            # possibly-dead fabric — measured on blx04, a box whose chip 4/14 eth links were down
            # read `ok` on enum+ARC and every full-mesh job died. Fail CLOSED: hold it fabric-
            # unverified whether or not it was flagged dirty, so no tenant runs until a pass
            # returns a real healthy verdict. Do NOT reset — a 77 is not resettable, and a galaxy
            # reset on unverifiable fabric harms marginal silicon more than the risk it guards
            # (measured: a forced reset here dropped chip 26 and took all 32 to 0xFFFFFFFF).
            _log(
                "fabric ran but returned no verdict (could not confirm the mesh moves data) — "
                "holding the device fabric-unverified; enum+ARC cannot see a wedged eth core, so "
                "no tenant runs until a pass returns healthy. NOT resetting: a 77 is not a fault "
                "a reset fixes, and a galaxy reset on unverifiable fabric harms marginal silicon."
            )
            # Enum + ARC are the only things actually proven here, so say so: claiming the
            # fabric was verified when the pass never ran is how a mesh with a wedged eth
            # core reads "verified healthy" in the timeline and the next tenant finds out.
            # Mark it fabric-unverified so the opt-in fabric relift can re-run the pass while
            # idle — a transient 77 otherwise strands the mesh here until a broker restart.
            _hold_device_fabric_unverified(f"gate/{phase}: enum+ARC healthy, fabric unverified")
            _note_tenant_gate_verdict(f"device unverified: gate/{phase}: enum+ARC healthy, fabric unverified (rc 77)")
            return

        if stage == WAIT:
            # The router names WAIT here for exactly two wedges, and which one it is decides the
            # hold's flavour — what a later read-only relift must see before it may lift it — so
            # the gate reads that off its own evidence rather than the single WAIT.
            if eth_frozen:
                # A frozen active-eth-core heartbeat is the wedge itself — hold it, never reset. A
                # frozen verdict can only exist once enum + ARC + the tt-smi snapshot have all
                # passed (the probe pass returns before the eth read otherwise), so this is provably
                # a single-chip eth-firmware freeze — the fault that self-heals — and
                # NOT a mass drop. The galaxy reset the unhealthy path would run instead is the
                # measured drop that takes all 32 chips off the bus; the passive heartbeat read
                # exists precisely so a frozen core is held, not pushed harder. This sits inert
                # until the reader is wired (verify_eth_heartbeat returns None while unconfigured).
                _log(
                    "active-eth-core heartbeat frozen — a single-chip wedge; NOT resetting (a "
                    "galaxy reset here is the measured all-chip drop). Holding the door: no tenant "
                    "runs on the frozen mesh until a later gate verifies the core came back. The "
                    "ladder escalates at the 10-min hold ceiling — do not plan around waiting the "
                    "wedge out."
                )
                # A runtime-reported fault otherwise stands until a reset — the checks that cleared
                # it "healthy" are blind to the wedge, but the heartbeat read is not, and every
                # fault the runtime can report here is itself an eth/fabric-wedge signature
                # (_DEVICE_FAULT_SIGNATURES), so the frozen read is direct evidence for it. Retire
                # it into the hold: left set, it would survive self-heal and make a later
                # enum+ARC-healthy gate skip the clear and reset an already-recovered mesh. The door
                # still holds on the fsm episode.
                if device_fault_reported:
                    _clear_device_reported_fault(
                        f"gate/{phase}: attributed to the held frozen eth core, retired into the hold"
                    )
                _hold_device_unverified(
                    f"gate/{phase}: eth-core heartbeat frozen — held, not reset", needs_eth_advancing=True
                )
                _note_tenant_gate_verdict(
                    f"device unverified: gate/{phase}: eth-core heartbeat frozen — held, not reset"
                )
            elif ev.cooling:
                # A reset we already tried and that already failed to revive the mesh will not
                # revive it now, and hammering a dead endpoint is precisely what escalates a PCIe
                # error to fatal and reboots the host. Sit out the cooldown instead.
                since = int(time.monotonic() - recovery_mechanism.last_reset_monotonic)
                left = RESET_COOLDOWN_SEC - (time.monotonic() - recovery_mechanism.last_reset_monotonic)
                _log(
                    f"device unhealthy but a reset already failed {since}s ago; "
                    f"holding off for {int(left)}s. The device stays flagged: tenant jobs wait at "
                    f"the door, or are refused, but none reaches it."
                )
                health_event("reset_suppressed_cooldown", phase=phase, seconds_left=int(left))
                metrics.stage_fired("smi_reset", "blocked")
                _note_tenant_gate_verdict(f"device unverified: gate/{phase}: reset cooldown active ({int(left)}s left)")
            return

        reasons = ["failed health check"] if not healthy else []
        if device_fault_reported:
            # Say what actually brought us here. Every check passed; the runtime disagreed.
            reasons.append(f"runtime reported: {device_fault_reported}")

        # A chip that LEFT THE BUS ENTIRELY — gone from sysfs, not merely reading all-ones —
        # never entered isolated_chips (_isolate_dead_chips tracks only all-ones chips), so the
        # surgical rung below skips it and it strands at the below-floor hold until a human reset.
        # When opted in, route a single/few-chip gone drop to that same gentle rung: it is already
        # off the bus (no isolate needed), device_pci_map still holds its address, and
        # reset_chip_via_bridge finds its bridge by the secondary bus it still points at even with
        # the endpoint's own sysfs node gone. Only below the galaxy-reset floor — a floor's worth of
        # Secondary Bus Resets is not the gentle path, so a mass drop is left to the galaxy reset.
        gone_queued: list = []
        if gone_chip_bridge_reset_enabled() and not await asyncio.to_thread(recovery_mechanism.scope_active):
            beats_a = await asyncio.to_thread(read_heartbeats)
            candidates = (set(device_pci_map) - set(beats_a)) - isolated_chips
            if candidates:
                # Confirm the drop against a second read: a chip absent from only one sample may
                # be a transient heartbeat-read miss on a still-live chip, and its bridge is shared
                # with silicon we must not reset. Only a chip absent from BOTH is treated as gone.
                await asyncio.sleep(GONE_CHIP_CONFIRM_SETTLE_SEC)
                beats_b = await asyncio.to_thread(read_heartbeats)
                gone = sorted(candidates - set(beats_b), key=int)
                # Absent from both reads is not proof a chip LEFT THE BUS: read_heartbeats omits an
                # enumerated chip whose ARC read merely stalled the same way it omits one whose node
                # is gone, and that chip's parent bridge still hosts live silicon an SBR must not
                # touch. Route only chips whose own PCIe node is actually absent; the rest fall to
                # the hold below.
                gone = [c for c in gone if not await asyncio.to_thread(chip_node_present, device_pci_map[c])]
                off_bus_gone = len(dead_chips(beats_b)) + max(0, expected - len(beats_b))
                floor_gone = _galaxy_reset_min_dead_chips(expected)
                if gone and floor_gone is not None and off_bus_gone < floor_gone:
                    _log(
                        f"chip(s) {gone} left the PCIe bus entirely and are below the galaxy-reset "
                        f"floor ({off_bus_gone}/{expected} off-bus); routing to a per-chip bridge "
                        f"reset via the secondary bus their bridge still points at"
                    )
                    health_event(
                        "gone_chip_bridge_reset_queued",
                        chips=gone,
                        off_bus=off_bus_gone,
                        expected=expected,
                        floor=floor_gone,
                    )
                    isolated_chips.update(gone)
                    gone_queued = gone

        # The off-bus count just BEFORE any rung runs, read once: the floor keys on it, the per-tray
        # walk maps its chip set onto trays, and the post-reset escalation compares against it to
        # tell a reset that recovered ground from one that REGRESSED the mesh off the bus. One read,
        # so all three decisions are made against the same picture of the mesh.
        beats = await asyncio.to_thread(read_heartbeats)
        # A reset already cycling in its own PID-1 scope must not read as a mass drop (it takes
        # every chip off the bus while it runs) and must not have a second reset started under it —
        # the concurrent-reset hazard this module exists to prevent. The router turns a live scope
        # into DEFER: skip the surgical rung and the floor, adopt that reset and verify its result.
        ev = replace(
            ev,
            off_bus=len(dead_chips(beats)) + max(0, expected - len(beats)),
            sbr_candidates=len(isolated_chips),
            scope_active=bool(await asyncio.to_thread(recovery_mechanism.scope_active)),
        )
        stage = galaxy_recovery.next_stage(ev)

        async def _fire(rung: str) -> bool:
            """Fire the rung the router named and fold its outcome into ``fsm``. True iff it
            recovered the mesh — in which case it has already retired what its own outcome
            semantics allow and reopened the door (see ``GalaxyRecovery._fire_gate_rung``), so the
            caller is done. Reads ``ev``/``beats`` as they stand at the call, which is the evidence
            the decision being fired was made from."""
            _note_tenant_gate_verdict(f"device unverified: gate/{phase}: {dirty_reason or 'unhealthy'}")
            outcome = await galaxy_recovery.escalate(
                f"gate/{phase}", indices, expected, _log, stage=rung, ev=ev, beats=beats
            )
            fsm.on_outcome(outcome)
            return outcome == OUTCOME_RECOVERED

        if stage == DEFER and gone_queued:
            # A foreign reset scope opened between queueing these gone chips and here (the
            # two-sample settle is the window), so the surgical rung is skipped and the reset below
            # adopts that scope instead — un-queue the chips this pass speculatively added. Unlike
            # an all-ones chip, which _isolate_dead_chips already removed from the bus and which
            # stays isolated until that rung rescans it back, a gone chip the in-flight reset
            # revives returns on its own; left in isolated_chips it would trip a needless bridge
            # reset of an already-recovered chip at the next gate. A chip still gone next pass is
            # re-detected and re-queued.
            for idx in gone_queued:
                isolated_chips.discard(idx)
            health_event("gone_chip_bridge_reset_unqueued", chips=gone_queued, reason="reset_scope_active")

        if stage == STAGE_BRIDGE_RESET:
            # A chip that left the bus gets the surgical repair first: reset it through its own
            # bridge and rescan. It costs ~10s against ~60s for a galaxy reset, it never touches
            # the other 31 chips, and the fabric survives it (measured). Only if the chip refuses
            # to come back do we reach for anything heavier.
            _log(
                f"chip(s) {sorted(isolated_chips, key=int)} left the PCIe bus and were "
                f"isolated; attempting a per-chip bridge reset before anything heavier"
            )
            if await _fire(STAGE_BRIDGE_RESET):
                return
            # That rung ended in a system-wide PCI rescan, so re-read the bus before the floor
            # decides on it: a chip that came back changes the count the floor keys on. No scope
            # re-read — our own rescan cannot open a foreign reset scope, and each rung left below
            # carries its own scope guard (the tray walk checks it, the reset adopts it).
            beats = await asyncio.to_thread(read_heartbeats)
            ev = replace(
                ev,
                off_bus=len(dead_chips(beats)) + max(0, expected - len(beats)),
                last_action=STAGE_BRIDGE_RESET,
                last_action_recovered=False,
                # Carry the per-chip SBR window this rung just read into the evidence the tray stage
                # classifies from: a chip that reported no_bridge here is the tray-down-no-window
                # signature. next_stage stays pure (it never reads this); only the STAGE_UBB_TRAY
                # gate rung does, via _classify_hold.
                bridge_reset_failed=galaxy_recovery.last_bridge_reset_reasons,
            )
            stage = galaxy_recovery.next_stage(ev)

        if stage == STAGE_UBB_TRAY:
            # Below the mass-drop floor. A mesh-wide galaxy reset is the measured all-chip drop —
            # run against a single/few-chip wedge it inverts the mesh, one chip off the bus becomes
            # 31 at 0xFFFFFFFF — so it stays suppressed here. The per-tray BMC reset is the lightest
            # rung that can still clear a whole-tray drop, and it declines itself (naming the
            # command instead) where there is no tray-down to fire on.
            if await _fire(STAGE_UBB_TRAY):
                return
            reset_floor = _galaxy_reset_min_dead_chips(expected)
            _log(
                f"{ev.off_bus} of {expected} chip(s) off the bus — below the galaxy-reset "
                f"floor ({reset_floor}). A galaxy reset here is the measured all-chip drop, so "
                f"holding the mesh degraded rather than resetting in-flight work away. The off-bus "
                f"ladder escalates in ~2 min and no hold outlives the 10-min ceiling — do not plan "
                f"around waiting the wedge out."
            )
            health_event(
                "galaxy_reset_suppressed_below_mass_threshold", off_bus=ev.off_bus, expected=expected, floor=reset_floor
            )
            # The idle relift lifts this hold read-only, so it may lift ONLY when enum+ARC+sysfs
            # can see the fault clear. A chip that physically left the bus (off_bus>=1) returns
            # visibly to those reads, so they suffice. But off_bus==0 means nothing left the bus —
            # the hold is a fabric/eth wedge, every chip present and ARC-ticking, which the relift's
            # enum+ARC pass is blind to (it never runs the traffic probe); so is a runtime-reported
            # fault. Both must wait for an advancing eth heartbeat to lift, never enum+ARC alone, or
            # the relift admits the next tenant onto the still-wedged mesh. With the reader
            # unconfigured the relift never lifts it, so it clears on the next startup gate (a
            # broker restart re-runs the fabric pass) — refusing tenants until then, the safe side.
            needs_eth = bool(device_fault_reported) or ev.off_bus == 0
            # Retire any runtime-reported fault into the hold: left set it would survive self-heal
            # and make a later enum+ARC-healthy gate skip the clear and reset an already-recovered
            # mesh. The door still holds on the fsm episode until a gate verifies recovery.
            if device_fault_reported:
                _clear_device_reported_fault(f"gate/{phase}: below the reset floor, attributed to a self-healing wedge")
            # Name the ACTUAL cause. off_bus==0 is a present mesh (every chip on the bus) held for
            # an eth/fabric fault — "0/32 off-bus below the reset floor" reads as nonsense to
            # anyone watching. Only a real drop (off_bus>0) is an off-bus hold.
            if ev.off_bus == 0:
                why = (
                    f"gate/{phase}: eth/fabric fault on a present mesh (all {expected} chips on "
                    f"the bus) — held for self-heal, not reset"
                )
            else:
                why = (
                    f"gate/{phase}: {ev.off_bus}/{expected} chip(s) off the bus, below the "
                    f"reset floor — held for self-heal, not reset"
                )
            _hold_device_unverified(why, needs_eth_advancing=needs_eth)
            _note_tenant_gate_verdict(f"device unverified: {why}")
            return

        _log("device needs reset — " + "; ".join(reasons))
        if await _fire(stage):
            return

        # Last rungs: the reset did not bring the mesh back. The host-level escalations — a warm
        # reboot, then a BMC power cycle above it — are the highest-risk actions here: each takes
        # every tenant's work down with the box. So one fires only when ALL of the following hold:
        # the host opted into that rung (default OFF), a prior recovery cycle already failed too
        # (was_failing — one bad pass is not "clearly won't recover"), the drop is a mass drop or a
        # present mesh the reset could not clear, no tenant is on the device right now, and the
        # durable rate limits pass. The first four are the router's; the last two are read here,
        # after it, because both are live IO a pure decision must not do. Any of them absent and the
        # device stays flagged exactly as before.
        beats_now = await asyncio.to_thread(read_heartbeats)
        off_bus = len(dead_chips(beats_now)) + max(0, expected - len(beats_now))
        reboot_floor = _reboot_min_dead_chips(expected)
        # A reset that timed out is deliberately left cycling in its own scope for the next gate to
        # adopt and verify, and a reset takes every chip off the bus — so the count above reads
        # all-ones exactly like a mass drop / whole-bus wedge while it runs. Ask the scope, not that
        # read: neither fire a host rung nor raise the power-cycle-required alert over a live reset's
        # own all-ones. The sampler and the galaxy-reset floor guard this same signal the same way.
        reset_cycling = bool(await asyncio.to_thread(recovery_mechanism.scope_active))
        off_bus_before = ev.off_bus
        ev = replace(
            ev,
            off_bus=off_bus,
            off_bus_before=off_bus_before,
            scope_active=reset_cycling,
            reset_exit_nonzero=recovery_mechanism.last_reset_exit_nonzero,
            last_action=STAGE_SMI_RESET,
            last_action_recovered=False,
        )
        stage = galaxy_recovery.next_stage(ev)
        # A reset that made the mesh WORSE off the bus (the incident's 8->32 inversion), or that
        # hard-exited leaving chips off it, cannot be undone by a warm reboot — a 6U-Galaxy reboot
        # does not power-cycle the UBBs, so a dropped ASIC stays off across it. The router routes
        # such a reset to the cold rung, never the reboot; flagging it loudly is the gate's half of
        # that verdict, and it names the drop in the fail-closed event below.
        reset_regressed = expected > 1 and off_bus > off_bus_before and not reset_cycling
        if reset_regressed:
            health_event(
                "reset_regressed_offbus",
                phase=phase,
                off_bus_before=off_bus_before,
                off_bus_after=off_bus,
                expected=expected,
                host_at_risk=True,
            )
            _log(
                f"the reset REGRESSED the mesh — {off_bus_before} -> {off_bus} of {expected} "
                f"chip(s) off the bus. A warm reboot cannot re-enumerate a dropped Galaxy ASIC, so "
                f"climbing to the cold rung, never the reboot."
            )
        if stage == BLOCKED and reset_cycling:
            _log(
                "every chip reads off the bus, but a reset is still cycling in its own scope — that "
                "all-ones is the in-flight reset, not a confirmed whole-bus wedge; the next gate "
                "adopts and verifies it before any power-cycle-required alert"
            )
            health_event("reboot_suppressed_reset_in_flight", phase=phase, off_bus=off_bus)
        elif stage == BLOCKED:
            # BLOCKED means specifically: this drop needs the cold rung (a warm reboot cannot
            # re-enumerate it) and no power cycle is opted in. Fail closed loudly and keep holding
            # rather than fire a reboot that lands right back in the dead state. A whole-bus drop and
            # a reset that regressed/hard-failed a partial drop off the bus each need that rung, but
            # name them apart so the count is not misreported as all-off-bus.
            if off_bus >= expected:
                _emit_all_off_bus_power_cycle_required(_log, off_bus, expected, context=f"gate/{phase}")
            else:
                galaxy_recovery._emit_reset_unrecoverable_power_cycle_required(
                    _log, off_bus, expected, regressed=reset_regressed, context=f"gate/{phase}"
                )
            metrics.stage_fired("power_cycle", "blocked")
        elif stage in (STAGE_HOST_REBOOT, STAGE_POWER_CYCLE):
            action = _HOST_ESCALATION_ACTION[stage]
            scan2 = await asyncio.to_thread(enumerate_device_holders)
            # A scan that could not read every holder (no CAP_DAC_READ_SEARCH on a tenant's
            # /proc) must count as "a tenant is here": the highest-risk action does not get to
            # take the box down over a tenant it merely could not see.
            tenant_active = not scan2.complete or any(h.uid >= MIN_TENANT_UID for h in scan2.holders)
            allowed, why = recovery_mechanism.auto_recovery_allowed(action, tenant_active=tenant_active)
            if allowed:
                await _fire(stage)
                return
            _log(f"auto-{action} opted in but held off: {why}")
            recovery_mechanism._journal_auto_recovery_denied(action, why)
            metrics.stage_fired(stage, "blocked")
        # The router held (WAIT). Which of its reasons it was decides what to say about it, and
        # whether the declined rung owes the metric a "blocked": a rung suppressed over an in-flight
        # reset or below the reboot floor is a real decline, while a host rung nobody opted into is
        # the default and only ever the gentlest one.
        elif _host_rung_opted_in() and ev.was_failing and reset_cycling:
            _log(
                "a reset is still cycling in its own scope; not escalating to the destructive rung "
                "over its in-flight all-ones — the next gate adopts that reset and verifies it"
            )
            health_event("reboot_suppressed_reset_in_flight", phase=phase, off_bus=off_bus)
        elif _host_rung_opted_in() and ev.was_failing:
            _log(
                f"{off_bus} of {expected} chip(s) dead — below the reboot floor ({reboot_floor}). "
                f"The holder-kill covers the MCE path, so holding the mesh degraded rather than "
                f"rebooting in-flight work away. The ladder escalates at the 10-min hold ceiling — "
                f"do not plan around waiting the wedge out."
            )
            health_event(
                "reboot_suppressed_below_mass_threshold", off_bus=off_bus, expected=expected, floor=reboot_floor
            )
        elif ev.was_failing:
            metrics.stage_fired("host_reboot", "blocked")

        # Deliberately NOT cleared: the device did not come back, and pretending it is
        # clean is how the next job's gate resets it all over again, and the one after
        # that. The flag stays set so the state is visible and honest.
        _log(
            "device did NOT verify healthy this pass. It stays flagged, and no tenant job "
            "runs on it: with the tenant hold armed they wait at the door, otherwise they are "
            "refused at the gate. A reset already in flight plus the next gate's PCI rescan "
            "usually bring it back within minutes; a power-cycle is only needed if it stays "
            "short across many gates."
        )


# The three in-process call sites and the suite still reach the gate by its private name.
_device_health_gate = device_health_gate


def _recovery_degraded() -> bool:
    """Whether the mesh is CURRENTLY dirty, unverified, or carrying a runtime-reported fault — the
    narrow predicate the escalation ladder reads (via RecoveryDeps.device_degraded) to tell a rung
    that RECOVERED the mesh from one that merely ran. Deliberately narrower than
    ``_device_degraded_for_tenant``: no broker-op suppression, no live sysfs probe — those answer
    "may a tenant touch the device right now", a different question from "did the ladder's last
    action fix anything"."""
    return fsm.state is not ServerState.HEALTHY or bool(device_fault_reported)


def _device_unavailable_for_tenant() -> str:
    """Why tenant work may not touch the device right now — '' if it may.

    THE predicate. Every caller that used to ask "is the device idle?" answered it by
    looking for a RUNNING tenant job, which sees neither of the two ways the device is
    actually taken: a broker device op holding it (a reset, a fabric pass — the device is
    being worked ON), and the FSM (the last job or probe left it in a state nobody has
    verified). Both mean "not yours", and asking about tenant jobs alone reports an idle
    device in the middle of a galaxy reset.
    """
    if device_op_active:
        return f"broker device op in flight: {device_op_detail or device_op_active}"
    if fsm.state is not ServerState.HEALTHY:
        reason = fsm.record.detail or fsm.record.why
        # dirty (owes the next gate a reset attempt) reads differently from an affirmative hold
        # (the gate decided NOT to reset) — same distinction the pre-FSM device_dirty/
        # device_unverified_why pair carried in this same message.
        if fsm.record.dirty:
            return f"device is dirty and unverified: {reason}"
        return f"device unverified: {reason}"
    # The runtime said the mesh is wedged. Nothing we can run refutes that — our checks are
    # blind to it — so it stands until a reset does.
    if device_fault_reported:
        return f"runtime reported a device fault, pending reset: {device_fault_reported}"
    return ""


def _slurm_step_verdict(*, require_free: bool) -> dict:
    """The externally-driven step's answer: is the mesh fit, and (``require_free``) is it free?

    Fit is the FSM's own verdict plus one live sysfs sample, reusing the two predicates the
    queue's own admission reads — an external driver and a queued job must never get different
    answers about the same device. Free is the reset gate's tenant rule: a holder at or above
    MIN_TENANT_UID, or a scan too blind to rule one out (04 I7 fails closed).
    """
    reason = _device_degraded_for_tenant()
    scan = enumerate_device_holders()
    foreign = [h for h in scan.holders if h.uid >= MIN_TENANT_UID]
    holders = [{"pid": h.pid, "uid": h.uid, "username": h.username} for h in foreign]

    if require_free and not reason:
        if foreign:
            who = ", ".join(f"pid {h.pid} ({h.username})" for h in foreign)
            reason = f"device is held by {len(foreign)} tenant process(es): {who}"
        elif not scan.complete:
            reason = "holder scan incomplete; cannot rule out a tenant on the device"

    return {
        "ok": not reason,
        "reason": reason,
        "fsm_state": fsm.state.value,
        "fsm_why": fsm.record.why,
        "holders": holders,
    }


def _device_liveness_reason() -> str:
    """A chip off the PCIe bus RIGHT NOW, read live from one root-free sysfs sample — ''
    if none. Catches the two degraded states no in-memory flag records: a chip gone to
    0xFFFFFFFF (off the bus — the reads that stall a CPU core and take the host down) and
    an empty sysfs on a host whose driver DID expose chips at startup (driver wedged). A
    present-but-frozen ARC is NOT caught here: that needs the two-sample heartbeat_verdict
    the between-job gate runs, and a single sample must not sleep on a caller's path."""
    if not heartbeat_supported():
        return ""  # detector absent on this host — the in-memory signals still stand
    beats = read_heartbeats()
    if not beats:
        return "no chips exposed in sysfs (driver wedged or all chips off the bus)"
    dead = dead_chips(beats)
    if dead:
        return f"chip(s) [{','.join(dead)}] fell off the PCIe bus (reads return 0xFFFFFFFF)"
    return ""


def _device_degraded_for_tenant() -> str:
    """The one authoritative "is the device degraded, and why" — '' if a tenant job may
    have it. Combines the in-memory record (a broker op holding the device, or the last
    job leaving it dirty and unverified) with a live sysfs probe for a chip that has since
    fallen off the bus — a wedge no flag was set for: a spontaneous drop, or a broker
    restart that dropped the in-memory dirty flag. Root-free, no device touch, no sleep,
    so it is cheap enough to answer a status query."""
    return _device_unavailable_for_tenant() or _device_liveness_reason()


def _device_hold_state(now: Optional[datetime] = None) -> Optional[tuple[str, str]]:
    """``(reason, held_since_iso)`` when the device is HELD — degraded and refused to
    tenants with no broker op running — else ``None``. This is what turns the
    ``device_degraded`` string into a visible HOLD job: a watcher sees the device sitting
    refused, not just a boolean off to the side.

    Suppressed while a broker op is active: that op already occupies the RUNNING row and IS
    the recovery, so a second row for the same device would read as two things happening at
    once. ``held_since`` is latched on the way into the hold and preserved across recovery
    attempts (each op is its own row with its own clock), so it answers "how long has this
    device been unusable", and cleared the instant the device is fit for a tenant again."""
    global device_held_since
    reason = _device_degraded_for_tenant()
    if not reason:
        device_held_since = ""  # fit for a tenant -> not held
        return None
    if device_op_active:
        return None  # the op's row is the recovery; don't double it. Keep the latch running.
    if not device_held_since:
        device_held_since = (now or datetime.now()).isoformat()
    return reason, device_held_since


def _health_payload(now: Optional[datetime] = None) -> dict:
    """The /health body. Beyond broker liveness it reports the DEVICE hold state, so a poller
    cannot read a box sitting held-and-refused as 'ok' — the gap that let a stuck hold go
    unseen for hours. ``status`` is 'degraded' exactly while the device is HELD (degraded,
    refused to tenants, no recovery op running); a transient broker op does not flip it, so a
    routine reset does not read as a stuck box. ``held``/``held_age_sec``/``held_reason`` carry
    the precise latch for a monitor that alarms on how long the device has been unusable, and
    ``device_degraded`` still names an in-flight recovery that ``status`` deliberately hides."""
    running = sum(1 for j in jobs.values() if j.status == JobStatus.RUNNING)
    queued = sum(1 for j in jobs.values() if j.status == JobStatus.QUEUED)
    hold = _device_hold_state(now)
    payload = {
        "status": "degraded" if hold else "ok",
        "version": __version__,
        "running": running,
        "queued": queued,
        "total_jobs": len(jobs),
        "device_degraded": _device_degraded_for_tenant(),
        "held": hold is not None,
        # The FSM's own record, straight from the source every field above is now derived from.
        "fsm_state": fsm.state.value,
        "fsm_why": fsm.record.why,
    }
    if hold:
        reason, since = hold
        payload["held_since"] = since
        payload["held_reason"] = reason
        try:
            payload["held_age_sec"] = int(((now or datetime.now()) - datetime.fromisoformat(since)).total_seconds())
        except ValueError:
            payload["held_age_sec"] = None  # a since we cannot parse is still a hold; report it without an age
    return payload


def _note_tenant_gate_verdict(reason: str) -> None:
    """Record the device entering or leaving a tenant-refused hold in the durable health
    timeline, once per episode. ``reason`` is the tenant gate's verdict: non-empty while a
    job is being refused, '' the moment one passes. The jobs list already shows each
    refusal, but that is in-memory and per-job; a spontaneous chip drop the live probe
    caught opens an fsm episode but fires no health event of its own — this is the
    only durable trace of when the device went bad. Latched so a queue's worth of refusals
    onto one degraded device writes a single ``device_held`` event, not one per job."""
    global device_hold_logged, device_hold_episode_since, device_hold_episode_reason
    global device_hold_deadline_bucket, device_hold_escalate_bucket
    global device_hold_offbus_escalated
    global device_hold_escalated_monotonic, _hold_row
    if reason and not device_hold_logged:
        device_hold_logged = True
        # Prefer a start this box already recorded: if the device was still held across a restart,
        # the ceiling must keep counting from the ORIGINAL drop, not from the new process.
        row_since = datetime.now().isoformat()
        device_hold_episode_since = _restore_hold_episode() or row_since
        _persist_hold_episode(device_hold_episode_since)
        device_hold_episode_reason = reason
        # Reserve the ledger row now, so the live row and the durable one share id, start, and name.
        # Its start is THIS process's segment (row_since), NOT the restored escalation clock: a hold
        # split by a restart then reads as adjacent rows — the orphaned-close for the prior segment,
        # this for the current — instead of two overlapping rows anchored at the same original drop.
        hold_id = next_job_id()
        _hold_row = {
            "id": hold_id,
            "started_at": row_since,
            "owner": "[broker]hold",
            "command": f"device HELD, refused to tenants: {reason}",
        }
        _write_action_log_file(
            hold_id,
            "[broker]hold",
            f"device HELD, refused to tenants: {reason}",
            datetime.fromisoformat(row_since),
            "started",
            0,
            0.0,
        )
        health_event("device_held", reason=reason)
    elif not reason and device_hold_logged:
        device_hold_logged = False
        health_event("device_released")
        # The hold's own row, written now that it has an end and a duration. While it was
        # held the jobs list showed it live, and that row is synthetic — it exists only for
        # as long as the hold does, so the moment the device came back the window it sat
        # refused vanished from the one place anyone looks. A reset gets a row; the hold it
        # was recovering from left the list jumping from the failed reset to the next boot,
        # which reads as nothing having happened for those minutes.
        held_for = 0.0
        if device_hold_episode_since:
            try:
                held_for = (datetime.now() - datetime.fromisoformat(device_hold_episode_since)).total_seconds()
            except ValueError:
                held_for = 0.0
        held_for = max(0.0, held_for)
        end_time = datetime.now()
        end_id = next_job_id()
        _write_action_log_file(
            end_id,
            "[broker]hold",
            "device RECOVERED, hold ended: ready for tenants",
            end_time,
            "ended",
            0,
            held_for,
        )
        _hold_row = None
        device_hold_episode_since = ""
        _persist_hold_episode("")  # the device came back fit: the episode is over, on disk too
        device_hold_episode_reason = ""
        device_hold_escalated_monotonic = 0.0
        # ...and its own deadline watchdog: re-arm the stuck-hold alert for the next episode.
        device_hold_deadline_bucket = 0
        # ...and its own forced-escalation windows.
        device_hold_escalate_bucket = 0
        # ...and its own early off-bus attempt.
        device_hold_offbus_escalated = False
        # A new episode gets its own single escalation and its own single per-tray BMC reset — the
        # spent latches reset with this ledger's own clock. Not derived from fsm opening a fresh
        # episode: this release can land before or after fsm's own clear, and a latch left set
        # across that gap would silence the very next hold's first escalation attempt.
        fsm.set_latch("escalated", False)
        fsm.set_latch("ubb_reset_fired", False)


def _force_escalate_enabled() -> bool:
    """The past-ceiling forced escalation. ON by default; TT_DEVICE_MCP_FORCE_ESCALATE=0 is the
    operator kill-switch. On by default because a hold that outlived the ceiling with no tenant on the
    device must terminate — the alternative is the 21 h silent hold this exists to end."""
    return os.environ.get("TT_DEVICE_MCP_FORCE_ESCALATE", "1").strip() != "0"


async def _force_escalate_stuck_hold() -> None:
    """Teeth for the hold-deadline watchdog: a hold that outlived the escalation ceiling gets the gate's
    OWN gentlest-first ladder — surgical per-chip bridge reset -> per-tray UBB re-power -> galaxy reset
    (present mesh / mass drop) -> warm reboot -> BMC power cycle — FORCED past the fail-closed defers
    that let a held box sit (the one-reset-per-episode latch, an unreadable holder scan counted as a
    tenant, the failed-reset cooldown). A held device is refused to tenants, so there is no running job
    to protect; the ladder still climbs only when each rung FAILS — it never jumps straight to the cold
    rung. The only guards kept are a reset already cycling (never double-reset), a real readable tenant,
    and the host rungs' own boot-loop rate limiter. Fired once per ceiling window until recovery."""

    def log(msg: str) -> None:
        if logger:
            logger.error(f"HOLD-DEADLINE-ESCALATE {msg}")

    async with _device_op("hold-deadline-escalate"):
        if await asyncio.to_thread(recovery_mechanism.scope_active):
            return
        if not _recovery_degraded():
            return  # the hold cleared between the sampler spawning us and the lock
        indices = _present_chip_indices()
        # No device nodes at all is the sysfs-blackout / all-off-bus case — the catastrophic drop that
        # most needs the cold rung, never a reason to bail. Bailing let the deadline watchdog keep
        # advancing the escalate bucket while nothing ran — the exact indefinite hold this exists to end.
        # expected falls back to the host's baseline, so off_bus reads full-mesh and the off-bus ladder
        # below climbs straight to the reboot/power-cycle rung. Only a host that has NEVER shown a chip
        # (no override, no baseline) has no known-good count to restore — there, and only there, hold.
        expected = health_monitor.expected(len(indices))
        if expected <= 0:
            return
        beats = await asyncio.to_thread(read_heartbeats)
        off_bus = len(dead_chips(beats)) + max(0, expected - len(beats))
        health_event(
            "hold_deadline_forced_escalation",
            off_bus=off_bus,
            expected=expected,
            held_since=device_hold_episode_since,
            host_at_risk=True,
        )
        held_age = 0.0
        if device_hold_episode_since:
            try:
                held_age = (datetime.now() - datetime.fromisoformat(device_hold_episode_since)).total_seconds()
            except ValueError:
                held_age = 0.0
        trigger = device_hold_escalation_trigger or f"the {int(_stuck_hold_ceiling_sec())}s ceiling"
        log(
            f"device HELD {int(held_age)}s — past {trigger} and not lifted — forcing the gentlest-first "
            f"recovery ladder (off_bus={off_bus}); a tenant-free held box has no gate that may keep it "
            f"holding"
        )
        # Run the gate's OWN gentlest-first ladder, forced past the fail-closed defers — NOT a jump to
        # the cold rung. An off-bus drop takes the off-bus ladder (surgical per-chip bridge reset ->
        # per-tray UBB re-power -> warm reboot -> BMC power cycle); a present mesh takes the mesh
        # reset -> climb. Each rung fires only when the gentler one failed. The module-global Galaxy
        # instance, NOT select_recovery: the idle ladders live on GalaxyRecovery but apply on every
        # platform, exactly like the gate's rungs (see GalaxyRecovery's ladder comment) — the
        # Galaxy-only rungs decline themselves off-platform (UBB plans return None) and the reset
        # argv is re-derived per platform inside _reset_and_verify_device, so a per-target host fires
        # `tt-smi -r`, never -glx_reset. Routing through select_recovery hands a per-target host the
        # base escalate() that always reports WAITING — these teeth silently bite nothing, and the
        # hold outlives every ceiling with no rung ever attempted.
        outcome = await galaxy_recovery.escalate(
            "offbus-forced" if off_bus > 0 else "present-forced", indices, expected, log
        )
        fsm.on_outcome(outcome)


def _maybe_spawn_forced_escalation() -> bool:
    """Start one forced escalation as its own task when a hold has outlived the ceiling and none is in
    flight. Detached from the sampler (like the relift): the reset/climb it runs takes tens of seconds,
    and awaiting it inline would blind the sampler's dead-chip tripwire."""
    global _forced_escalation_task
    if not _force_escalate_enabled():
        return False
    if _forced_escalation_task is not None and not _forced_escalation_task.done():
        return False
    if device_op_active:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False  # no event loop (the sampler always has one; a sync unit test may not) — nothing to spawn
    _forced_escalation_task = loop.create_task(_force_escalate_stuck_hold())
    return True


def _check_hold_deadline(now: Optional[datetime] = None) -> None:
    """Flag a hold that has outlived the deadline to the durable timeline, so none sits silent.

    The backstop under every other hold-termination path. The idle escalation
    (``_escalate_stuck_hold`` / ``_escalate_offbus_stuck_hold``) is what converts a stuck hold into
    a recovery, but each of its guards fails closed to "keep holding": the operator kill-switch off,
    a foreign tenant or an unreadable holder scan (which counts as a tenant), this episode's one
    reset already spent, the failed-reset cooldown. None of those write to the timeline — so a hold
    they defer on leaves NOTHING after the opening ``device_held``, which is the 17h idle blackout
    the incident found. This writes a loud, durable, actionable event when the episode clock crosses
    the deadline and RE-writes it each further window, so a monitor keying on recency keeps alarming.
    It also gives the hold TEETH: past the escalation ceiling it drives ``_maybe_spawn_forced_escalation``,
    an UNCONDITIONAL run of the recovery ladder (a held box is tenant-free, so no gate may keep it
    holding) — the mechanical guarantee that a hold always terminates, not just gets logged. Keyed on
    the episode clock (``device_hold_episode_since``) the escalation and the held/released pair already
    use; a missing or unparseable stamp is not held, so it is not flagged."""
    global device_hold_deadline_bucket, device_hold_escalate_bucket, device_hold_offbus_escalated
    global device_hold_risky_escalated, device_hold_escalation_trigger
    if not device_hold_episode_since:
        return
    try:
        started = datetime.fromisoformat(device_hold_episode_since)
    except ValueError:
        return
    age = ((now or datetime.now()) - started).total_seconds()
    # TEETH: past the escalation ceiling, FORCE the full recovery ladder — once per ceiling window —
    # until the hold clears or reaches the loud terminal rung. A held box is refused to tenants, so
    # there is no running job to protect and no gate may keep it holding; this is the guarantee the
    # relift's and class-escalators' fail-closed guards (unconfigured eth reader, last-fabric verdict
    # that never re-runs idle, unreadable tenant scan, one-reset latch) do not give on their own.
    ceiling = _stuck_hold_ceiling_sec()
    # An off-bus hold gets its FIRST attempt early (see _offbus_hold_ceiling_sec) and only its
    # first: the rung that answers it is the cheap surgical one, but each further window forces
    # the ladder past the one-reset-per-episode latch, so repeating on the short clock would walk
    # a shared box to a warm reboot and a BMC power cycle within minutes. Claiming bucket 1 here
    # leaves every later window on the general ceiling, exactly as before.
    # Consume a latch ONLY when the escalation actually started. The spawn declines on a device op
    # already in flight, an escalation still running, or the kill-switch — all transient except the
    # last. Marking the window spent on a decline burns it: the general clock then waits a whole
    # further ceiling, and the off-bus one-shot never fires again at all, which is how a hold
    # outlives its ceiling with no rung ever attempted.
    if isolated_chips and not device_hold_offbus_escalated and age >= _offbus_hold_ceiling_sec():
        device_hold_escalation_trigger = f"the {int(_offbus_hold_ceiling_sec())}s off-bus one-shot"
        if _maybe_spawn_forced_escalation():
            device_hold_offbus_escalated = True
    else:
        # The old risky-reset floor window (a separate forced run partway to the ceiling) is gone
        # (ladder-v2): the below-floor mesh reset no longer waits, so the general ceiling window is
        # the one forced run past the early off-bus one-shot.
        ebucket = int(age // ceiling)
        if ebucket >= 1 and ebucket > device_hold_escalate_bucket:
            device_hold_escalation_trigger = f"the {int(ceiling)}s ceiling (window {ebucket})"
            if _maybe_spawn_forced_escalation():
                device_hold_escalate_bucket = ebucket
    deadline = _hold_deadline_sec()
    bucket = int(age // deadline)
    if bucket < 1 or bucket <= device_hold_deadline_bucket:
        return
    device_hold_deadline_bucket = bucket
    health_event(
        "hold_stuck_past_deadline",
        held_since=device_hold_episode_since,
        reason=device_hold_episode_reason,
        held_age_sec=int(age),
        escalated=fsm.latch("escalated"),
        host_at_risk=True,
    )
    if logger:
        logger.error(
            f"HOLD-WATCHDOG device HELD {int(age)}s — past the {deadline}s deadline — and still "
            f"refused to tenants: {device_hold_episode_reason!r}. The idle escalation has not "
            f"cleared it (opted out, a stuck tenant/unreadable holder scan, its one reset spent, or "
            f"a killed relift). Manual recovery is likely required — a hold must terminate."
        )


def _refresh_idle_hold_ledger() -> None:
    """Keep the durable held/released timeline honest while the queue is empty.

    ``_note_tenant_gate_verdict`` only runs at a job boundary, so a device that goes degraded
    with no job flowing opens an fsm episode but writes no ``device_held`` event — the window
    it sat unusable leaves no durable trace, the exact idle-blackout hole prod turned up.
    Driven every sample, this closes it. Visibility only: it fires no reset. It also drives
    ``_check_hold_deadline``, the backstop that flags a hold outliving the deadline — the same idle
    path that would otherwise leave a stuck hold silent for hours after its opening ``device_held``.

    Keyed off the sampler's own confirmed verdict — a broker op, or the fsm episode, which
    ``_check_for_dead_chips`` opens only after the two-sample debounce a real drop survives and a
    transient does not: an all-32 all-ones blackout, a per-chip off-bus drop, and an all-gone
    sysfs blackout (every node off the bus) each dirty on the second consecutive sample — NOT the
    single-sample liveness probe the on-demand gate uses. Recording a durable held/released pair
    for a one-sample blip the strike counters are built to swallow would pollute the very timeline
    this exists to keep honest. What still waits for the next status query or job boundary — which
    read the fuller predicate — is the sub-confirmation window before that second sample, and a
    present-but-frozen ARC, which only the two-sample heartbeat verdict the gate runs can prove.

    A broker op in flight is skipped, not released: its own RUNNING row is the recovery, and
    writing a released here would flap the ledger held->released->held across every reset
    attempt. The episode only closes once the device is genuinely fit again."""
    if device_op_active:
        return
    _note_tenant_gate_verdict(_device_unavailable_for_tenant())
    _check_hold_deadline()


# How many times the gate will reset+verify a device that keeps coming back dirty before it
# stops trying. Exhausting this does NOT dispatch: the gate returns the degradation reason,
# and the runner holds or refuses the job on it. This bounds the number of resets — each one
# is another chance to trip the root complex into the fatal path that reboots the host — and
# nothing else.
# A running job that emits NO output for this long is reaped as `hung`: a job wedged on the
# device holds it until its own timeout, which is sized for the slowest legitimate run, and
# every queued tenant waits behind it for that whole window.
#
# Silence is a proxy, not proof: a long compile and a wedged mesh look identical from outside
# the process, so this WILL eventually misfire on a slow-but-healthy job. That is why the
# threshold is per-host tunable and 0 disables the reaper outright, and why the kill goes
# through the same graceful SIGTERM -> grace -> SIGKILL the timeout path uses — a hard kill
# mid-CCL wedges the mesh, which is the exact failure this is meant to clear.
HUNG_SILENCE_SEC = int(os.environ.get("TT_DEVICE_MCP_HUNG_SILENCE_SEC", "300"))
HUNG_POLL_SEC = 15.0

# How often a silent job is noted in its own log. This is the only hang signal that does not
# depend on the workload: the runtime has no always-on heartbeat, and a framework's progress
# printing covers that framework alone, so an LLM or a CCL test wedges into a log that simply
# stops. The broker holds the clock on every job's output whatever it is written in. Reaping
# is a separate decision — a host that has disarmed the reaper still needs legible hangs. 0
# silences the notices.
HUNG_NOTICE_SEC = int(os.environ.get("TT_DEVICE_MCP_HUNG_NOTICE_SEC", "60"))

# A job the runtime has already given up on, reaped on its own clock rather than the silence one.
#
# Silence cannot see this failure at all. When metal hits a dispatch timeout it declares the device
# unrecoverable and then unwinds, waiting the *full* per-operation timeout on each device in turn —
# eight devices at a 180s timeout is 24 minutes, and every one of those waits prints. So the job
# talks steadily the whole way down and never looks silent, while holding the device and freezing
# the queue behind it for the entire unwind. Nothing is salvageable once this line appears: the
# runtime's own verdict is that the device is gone, and only a reset brings it back.
#
# The grace exists because the traceback that follows names the wedged cores, which is usually the
# only evidence of what hung — worth a few seconds, not worth 24 minutes.
DOOMED_GRACE_SEC = int(os.environ.get("TT_DEVICE_MCP_DOOMED_GRACE_SEC", "45"))

# Matched against the job's own output. Both spellings are the same event seen at different layers:
# the context reporting the timeout, and the throw that carries it up.
DOOMED_PATTERNS = (
    "device timeout, potential hang detected, the device is unrecoverable",
    "Timeout detected (metal_context.cpp",
)

TENANT_GATE_MAX_VERIFY = 2


async def _await_device_free_for_tenant(job_log_file: Optional[Path]) -> str:
    """Hold a tenant job at the door until the device is its own. Returns '' when the
    device is free for a tenant, or the reason it is not — the caller decides what to do
    with a non-empty reason (the job runner refuses to dispatch; see job_runner).

    The invariant, in one place: no tenant job starts while a broker device op holds the
    device, while the device is dirty and unverified, or while a chip has fallen off the
    PCIe bus. The job runner satisfied the first half only by accident — it is
    single-threaded and awaits its gates inline, so a job physically could not overlap
    one — which is not an invariant, it is a coincidence of the call graph that the next
    refactor silently spends.
    """
    for _ in range(TENANT_GATE_MAX_VERIFY):
        # A broker op owns the device. Taking its lock IS the wait: the tenant does not get
        # the device until the broker gives it back. Re-check after, because the op that
        # hands it over may have flagged it dirty on the way out.
        while device_op_active:
            async with get_device_op_lock():
                pass

        # Run the admission gate every iteration — including on a device NOTHING has flagged. The
        # pre-job dispatch probe lives inside it, and an unflagged device is the case it exists for:
        # enumeration, the ARC heartbeat, and the fabric pass all pass on a mesh that cannot dispatch
        # (job 142 was admitted onto one and died at exit 134 in 24s), so a clean device must still
        # be proven to RUN A KERNEL before admission. A failed probe flags the device, so the check
        # below then falls through to reset+verify instead of dispatching onto the wedge. The gate
        # takes the device lock itself, so it must be called while we hold nothing. Inert until a
        # host opts in (TT_DEVICE_MCP_PREJOB_DISPATCH); with the probe off a clean device returns
        # from the gate immediately, costing a submitter nothing.
        await _ensure_device_clean_for_next_job(job_log_file)

        # Not-dirty, not HEALTHY: a held-but-undirty device (eth-frozen, fabric-unverified, a
        # foreign holder) gives the gate nothing to reset, so looping it here only spends extra
        # device ops per admission poll — one pass, then let the verdict below refuse the job.
        if not fsm.record.dirty:
            break

    # The authoritative verdict, not just the fsm state: a live sysfs probe for a
    # chip that fell off the bus with no episode opened for it (a spontaneous drop, or a
    # restart that lost the in-memory record). That is why the clean case above is a
    # `break`, not a bare `return ""` — such a chip leaves fsm HEALTHY, and a read of the
    # 0xFFFFFFFF it now returns can stall the host CPU that issues it, so the job must
    # never be dispatched.
    return _device_degraded_for_tenant()


async def _refuse_job_on_degraded_device(job: "Job", job_log_file: Optional[Path], reason: str) -> None:
    """Fail a tenant job AT THE DOOR instead of running its command onto a degraded
    device. The gate already gave a dirty device up to TENANT_GATE_MAX_VERIFY reset+verify
    attempts; a reason survives that only when the device is genuinely not fit to run on —
    most sharply, a chip off the bus, whose 0xFFFFFFFF reads can hang the host CPU that
    issues them. "Dispatch and let it fail honestly" is not honest for that case: the job
    would be the one to trigger the host-killer. Failing fast keeps the queue moving and
    tells the submitter exactly why; the device stays degraded and every later job is
    refused the same way until the broker recovers it (that recovery is a separate lever).
    Never raises — a bug here must not wedge the runner."""
    if logger:
        logger.warning(
            f"CLEAN-GATE refusing job {job.id} onto a degraded device — {reason}. "
            f"Not dispatched; it fails at the gate rather than onto the wedge."
        )
    async with get_lock():
        job.status = JobStatus.FAILED
        job.finished_at = datetime.now().isoformat()
        job.error = f"device degraded — job not dispatched: {reason}"
        if stats:
            stats.record_job_completion(job.status, job.wait_sec, job.runtime_sec)
    # Make the refusal legible where the submitter looks — the job's own log, which recent
    # history reads back — so a job that never ran does not read as one that vanished.
    if job_log_file:
        try:
            with open(job_log_file, "a") as f:
                f.write(
                    f"\n[REFUSED at {job.finished_at}] device degraded — {reason}\n"
                    f"The broker did not dispatch this job: running it onto the device "
                    f"in this state risks hanging the host. Resubmit once recovered.\n"
                )
            write_job_log_footer(job_log_file, job)
        except OSError as e:
            if logger:
                logger.error(f"could not write refusal footer for job {job.id}: {e}")


async def _refuse_job_privsep_identity(job: "Job", job_log_file: Optional[Path], reason: str) -> None:
    """Fail a tenant job AT THE DOOR when privsep is active but we can't run it as its real,
    non-root submitter. The only alternative is the direct-exec fallthrough, which runs the
    command as the broker itself (root) — the exact privilege privsep exists to withhold. This
    is a security refusal, not a device-health one, so it is loud (a health_event) and terminal.
    Never raises — a bug here must not wedge the runner."""
    if logger:
        logger.error(
            f"PRIVSEP refusing job {job.id} — {reason}. Not dispatched; a job we cannot "
            f"run as its real submitter must not run as the broker (root)."
        )
    health_event("privsep_identity_refused", job_id=job.id, owner=job.owner, reason=reason)
    async with get_lock():
        job.status = JobStatus.FAILED
        job.finished_at = datetime.now().isoformat()
        job.error = f"privsep: job not dispatched: {reason}"
        if stats:
            stats.record_job_completion(job.status, job.wait_sec, job.runtime_sec)
    if job_log_file:
        try:
            with open(job_log_file, "a") as f:
                f.write(
                    f"\n[REFUSED at {job.finished_at}] {reason}\n"
                    f"The broker did not dispatch this job: with privsep active it must run "
                    f"as its real submitter, never as the broker. Resubmit over the unix "
                    f"socket as a user with a passwd entry.\n"
                )
            write_job_log_footer(job_log_file, job)
        except OSError as e:
            if logger:
                logger.error(f"could not write privsep refusal footer for job {job.id}: {e}")


def _tenant_hold_enabled() -> bool:
    """Whether a degraded device HOLDS a tenant job (default) or FAILS it fast. Default ON:
    a queue must hold jobs while the device recovers, not bounce them back to the submitter.
    The indefinite hold that requires is now bounded by the recovery ladder (forced escalation
    past the hold ceiling), so the earlier default-OFF caveat no longer applies. Opt OUT with
    TT_DEVICE_MCP_TENANT_HOLD=0 on a host that deliberately wants fail-fast instead."""
    return os.environ.get("TT_DEVICE_MCP_TENANT_HOLD", "1").strip() != "0"


# Cadence of the hold's recovery re-check. Each pass re-runs the tenant gate, so a recovered
# device lifts here. A genuinely-stuck device is bounded not by this cadence but by the gate's
# own 600s reset cooldown (RESET_COOLDOWN_SEC), so this sets only how soon a recovered device
# is retried and how often a transient false-positive is re-checked — it does not pace resets
# on a device that keeps failing to recover.
def _hold_poll_sec_from_env(raw: Optional[str]) -> float:
    """Parse the hold re-check cadence defensively. It doubles as a governor, so a value that
    would defeat that role is floored, not honoured: inf/nan would sleep forever and hold a
    recovered device indefinitely — the freeze this loop exists to remove — and a sub-second
    poll would spin the re-check. A bad value must never crash import while the feature is
    OFF."""
    default = 60.0
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    if not math.isfinite(val):
        return default
    return max(1.0, val)


TENANT_HOLD_POLL_SEC = _hold_poll_sec_from_env(os.environ.get("TT_DEVICE_MCP_TENANT_HOLD_POLL_SEC"))


async def _hold_job_until_device_fit(job: "Job", job_log_file: Optional[Path], reason: str) -> str:
    """Hold a tenant job at the door while the device is degraded, then let it dispatch the
    instant the device is fit. Returns '' when fit — the caller dispatches — or the still-
    degraded reason when the hold was released by a cancel instead of by recovery (the caller
    sees the job is now KILLED and terminalizes it).

    Each pass re-runs the tenant gate, so a device dirtied and then recovered self-heals here
    exactly as the next job's gate would. A passive predicate cannot: only the gate clears the
    dirty flag, and the parked runner never reaches its own gate, so the queue would freeze on
    hardware that is fine. The gate resets only a genuinely dirty device (8bab0a2 keeps it off
    a healthy enum+ARC mesh) and TENANT_HOLD_POLL_SEC governs the cadence, so a device that
    will not come clean is retried, not stormed. No timeout by design — the bound is the device
    becoming fit (or a human / the recovery ladder), never a clock, because dispatching a
    tenant command onto a wedge is the outcome G2 forbids. Never raises."""
    if logger:
        logger.warning(
            f"CLEAN-GATE holding job {job.id} — device degraded: {reason}. Queue "
            f"HELD until the device is fit (no timeout); kill the job to release it."
        )
    if job_log_file:
        try:
            with open(job_log_file, "a") as f:
                f.write(
                    f"[HELD] device degraded — {reason}\n"
                    f"The broker is holding this job until the device is fit; it dispatches "
                    f"automatically then. Kill it to give up the hold.\n"
                )
        except OSError as e:
            if logger:
                logger.error(f"could not write hold note for job {job.id}: {e}")
    while reason:
        if job.status == JobStatus.KILLED:
            return reason  # released by a cancel, not by recovery — the device stays held
        _note_tenant_gate_verdict(reason)  # durable device_held (latched, idempotent)
        await asyncio.sleep(TENANT_HOLD_POLL_SEC)
        await cleanup_finished_jobs()  # keep sweeping through a long hold
        # Re-run the gate, not just the passive predicate: it resets+verifies a dirty device,
        # so a recovered one lifts here instead of freezing on a flag the parked runner can
        # never clear on its own.
        reason = await _await_device_free_for_tenant(job_log_file)
    _note_tenant_gate_verdict("")  # fit again -> device_released, then dispatch
    return ""


async def _ensure_device_clean_for_next_job(job_log_file: Optional[Path]) -> None:
    """Pre-job gate — a cheap safety net only. The authoritative snapshot+fabric
    check runs at the END of every run (see the post-job gate), so a submitter never
    pays for it on their critical path. This gate covers the residual case: the
    device is still flagged dirty at start time — the post-job check errored, or a
    broker restart dropped the in-memory flag before it ran — so reset + verify here
    before the inheriting job starts. A clean device returns IMMEDIATELY, costing a
    submitter nothing. Never raises."""
    # Prove the mesh can still RUN A KERNEL before admitting a tenant — and do it BEFORE the
    # not-dirty early return below, because an unflagged device is exactly the case that hurts:
    # job 142 was admitted onto a mesh nothing had flagged, died at exit 134 in 24s, and only then
    # was the device marked dirty. Enumeration and the ARC heartbeat are reads and pass on a dead
    # mesh; the fabric pass drives ethernet without enqueuing a program. Only a kernel proves a
    # kernel can run. Opt-in per host until the probe is timed there (see _prejob_dispatch_enabled).
    # Pass the job log so the verdict is recorded where the run's history reads back, not only in
    # server.log — the per-job log is the tenant-visible proof the gate actually ran.
    await _dispatch_probe_ok(job_log_file)
    if fsm.state is ServerState.HEALTHY:
        return  # clean device -> zero cost before a run
    if (fsm.record and fsm.record.why == "fabric_unverified") or not fsm.record.dirty:
        return
    try:
        await _device_health_gate(job_log_file, phase="pre-job", run_fabric=False)
    except Exception as e:  # noqa: BLE001 - gate must never block the queue
        if logger:
            logger.error(f"pre-job health gate error: {e}")
        _clear_device_dirty_unverified(f"pre-job gate error: {e}")


async def _verify_device_after_job(job_log_file: Optional[Path], job_failed: bool = False) -> None:
    """Post-job gate — runs at the END of every run (the finished job's caller has
    already been released, so this never delays their terminal/result): the enum/ARC
    snapshot always runs, so a chip that left the bus is flagged here — as the sampler
    already flags it in real time — and holds the queue before the next tenant.

    The ~45s fabric traffic pass runs ONLY when there is something to explain: a failed
    job (``job_failed``) or an already-dirty device. A fabric wedge — chips present, eth
    links down — does not show in the enum snapshot, but a job running across it fails,
    so a failure is exactly when the pass is worth its cost; ``job_failed`` forces it
    regardless of how recently one ran. A clean exit is not worth it: a mesh whose chips
    all enumerate and whose last job exited 0 has nothing the traffic pass would find
    that a later failure would not surface. It forces a CHECK, not a reset — an exit 1
    from a pytest assertion is not evidence of broken silicon.

    Never raises."""
    try:
        await _device_health_gate(job_log_file, phase="post-job", run_fabric=False, force_fabric=job_failed)
    except Exception as e:  # noqa: BLE001 - must never crash the runner
        # The gate threw before it could clear the device, so its state is unknown. Leaving it
        # unflagged lets the next tenant run on a device nothing verified; mark it dirty so the
        # residual pre-job gate resets + verifies before that job starts (see the pre-job gate).
        _mark_device_dirty(f"post-job health gate error: {e}", why="gate_error")
        if logger:
            logger.error(f"post-job health gate error: {e}")


def _pty_read(fd: int) -> bytes:
    """Blocking read of a pty master; b'' when the child closed it (EIO)."""
    try:
        return os.read(fd, 65536)
    except OSError:
        return b""


# Read-only tt-smi flags allowed by `smi` (no reset/reconfigure). Bare (no flags)
# is the interactive dashboard; positional values (e.g. a filename) are fine.
SMI_SAFE_FLAGS = {
    "-ls",
    "--list",
    "-s",
    "--snapshot",
    "--snapshot_no_tty",
    "-v",
    "--version",
    "-l",
    "--local",
    "-f",
    "--filename",
    "-h",
    "--help",
}


def smi_args_ok(args) -> bool:
    return all((not a.startswith("-")) or a in SMI_SAFE_FLAGS for a in args)


def _explain_interruption(
    job_id: Optional[str], started_at: Optional[str], last_write: Optional[datetime]
) -> Optional[str]:
    """Why a footerless job died, from the durable journal, or None if nothing explains it.

    A job is "interrupted" when its log has no footer and no broker tracks it: the broker
    went away mid-job. That is a description, not a diagnosis, and the diagnosis was on disk
    the whole time — the journal is fsync'd per record precisely because the events worth
    keeping are written moments before the machine dies.

    Ordered most specific first, and silent when it does not know: a wrong cause is worse
    than none. Correlation is by job_id where the event carries one, else by the job's own
    life window, which the log's last write bounds.
    """
    if not started_at:
        return None
    try:
        t0 = datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        return None
    # A few minutes past the last write: the killing blow lands after it, and a host reboot
    # records its event once the machine is back.
    t1 = (last_write.timestamp() if last_write else time.time()) + 300

    try:
        events = read_health_events(
            since_ts=t0,
            kinds={
                "job_killed",
                "device_holders_killed",
                "chip_dead",
                "all_chips_blackout",
                "auto_recovery",
                "broker_start",
            },
        )
    except Exception:  # noqa: BLE001 - a cause is a nicety; the job list is not
        return None

    window = [e for e in events if float(e.get("ts") or 0) <= t1]
    at = lambda e: e.get("iso") or "?"  # noqa: E731

    for e in window:
        if e.get("kind") == "job_killed" and e.get("job_id") == job_id:
            return f"killed at {at(e)} by {e.get('requested_by') or 'a user'}"

    for e in window:
        if e.get("kind") != "device_holders_killed":
            continue
        if e.get("job_id") not in (None, job_id):
            continue  # names a different job: not ours to claim
        why = e.get("reason") or "a chip left the PCIe bus"
        return (
            f"SIGKILLed at {at(e)} by the dead-chip holder-kill ({why}) — its device "
            f"mappings outlive the endpoint, and a read through one stalls a CPU core "
            f"and reboots the host"
        )

    for e in window:
        if e.get("kind") == "auto_recovery":
            return f"host {e.get('kind_of') or 'recovery'} fired by auto-recovery at {at(e)}"

    for e in window:
        if e.get("kind") in ("chip_dead", "all_chips_blackout"):
            return f"the device went dead under it at {at(e)}: {e.get('detail') or e.get('kind')}"

    for e in window:
        if e.get("kind") == "broker_start":
            return f"the broker restarted at {at(e)}; nothing recorded a device fault"

    return None


def _get_queue_status() -> dict:
    """Get queue status (shared by REST API and MCP tool).

    Module level, like _recent_jobs, because a nested one is unreachable from a test. The two
    build the same in-flight row from the same state, and only this one was out of a test's
    reach: so when the row learned to name whoever asked for the op, this copy kept saying
    "[broker]" and the test that should have caught it passed against the other one.
    """
    running = [j for j in jobs.values() if j.status == JobStatus.RUNNING]
    queued = [j for j in jobs.values() if j.status == JobStatus.QUEUED]
    rows = [
        {
            "id": j.id,
            "owner": j.owner,
            "command": j.command[:COMMAND_DISPLAY_LENGTH],
            "workspace": Path(j.workspace).name,
            "started_at": j.started_at,
        }
        for j in running
    ]

    # The broker's own device work belongs in RUNNING. A reset or a fabric pass owns
    # the device for 45-60s during which nothing can start, and reporting an empty
    # queue through that window makes the broker look hung to whoever is waiting —
    # these ops were visible only in RECENT, after they had already finished.
    #
    # The row names the whole op AND its current stage, and its clock is the STAGE's:
    # a reader takes `started_at` as the runtime of whatever `command` says, so the two
    # must measure the same thing. `op_started_at` carries the whole-op hold for anyone
    # who wants the total — which is a real question, just not the one the label asks.
    if device_op_active:
        stage = f"{device_op_active}: {device_op_detail}" if device_op_detail else device_op_active
        rows.insert(
            0,
            {
                "id": "--",
                "owner": device_op_owner or "[broker]",
                "command": stage,
                "workspace": "-",
                "started_at": device_op_stage_started_at or device_op_started_at or None,
                "op": device_op_active,
                "op_started_at": device_op_started_at or None,
            },
        )

    # An external scheduler's step (Slurm pre-step/post-step) can hold the device reserved
    # against dispatch for as long as its own gate takes — for post-step, unbounded by the step's
    # own deadline once a recovery-ladder climb is under way (03 I29). Without this row a queue
    # that dispatches nothing during exactly that window looks hung, with no visible reason: this
    # is that reason, named the same way device_op_active's row names a broker op.
    if external_step_active:
        rows.insert(
            0,
            {
                "id": "--",
                "owner": "[external-step]",
                "command": f"external step reserved: {external_step_active} (job dispatch deferred)",
                "workspace": "-",
                "started_at": None,
                "op": "external-step",
            },
        )

    # A device held degraded with no op running belongs in RUNNING too — the same reason
    # a broker op does: nothing can start on it, and a queue that shows nothing running
    # while nothing can start reads as a broker that has silently stopped. This is the
    # visible HOLD job; it makes device_busy True, which is what "not available to a
    # tenant" means. Suppressed while an op runs (that op is already the RUNNING row).
    hold = _device_hold_state()
    if hold:
        reason, since = hold
        rows.insert(
            0,
            {
                "id": "--",
                "owner": "[broker]",
                "command": f"device HELD (degraded): {reason} — new tenant jobs refused until recovered",
                "workspace": "-",
                "started_at": since,
                "op": "hold",
                "op_started_at": since,
            },
        )

    # Whether the device is degraded, and why, independent of what is running on it:
    # a chip off the bus or a dirty-unverified mesh is a state a watcher needs to see
    # even when the queue is empty and device_busy is False. Live and root-free, so a
    # status poll pays only a handful of sysfs reads for it.
    degraded = _device_degraded_for_tenant()

    return {
        "running": rows,
        "queued": [
            {
                "id": j.id,
                "owner": j.owner,
                "position": i + 1,
                "workspace": Path(j.workspace).name,
                "command": j.command[:COMMAND_DISPLAY_LENGTH],
            }
            for i, j in enumerate(queued)
        ],
        "device_busy": bool(rows),
        "device_degraded": degraded,
        # "" when no external step holds a reservation; otherwise every current holder's phase
        # name (comma joined — two can overlap, see _reserve_external_step). Also carried in the
        # RUNNING row above for `status`/`watch`; surfaced here too as a plain field for a caller
        # that wants the fact without parsing a row's `command` string.
        "external_step_active": external_step_active,
    }


def _recent_jobs(limit: int = 20) -> list[dict]:
    """Parse the most recent job log files into history records (owner, command,
    runtime, status, ...). Durable across the in-memory job purge; reads only each
    file's head + tail so it's cheap even for multi-MB device logs.

    A finished job carries a footer (STATUS/RUNTIME) written at completion. A
    footerless log has NOT finished — but "no footer" alone does not mean "still
    running": a broker restart wipes the in-memory queue while leftover processes
    live on, so a footerless log may be an orphan the broker no longer manages.
    We therefore classify footerless logs against the broker's *authoritative*
    in-memory state: ``running`` only if this broker is actually running it (so at
    most one shows running — the broker serializes), ``queued`` if it's waiting,
    else ``interrupted`` (footerless + untracked = orphaned by a restart). For a
    live job, runtime counts up to now and wait is the time it spent queued."""
    import collections

    if not job_log_dir:
        return []
    # Over-fetch, then sort by EXECUTION time below. The file name carries the queue time,
    # and with a deep queue the order jobs were queued in is not the order they ran in — a
    # job queued long ago may have started moments ago, and the reset it provoked runs
    # between it and the next job. Taking the newest `limit` names would drop exactly those.
    files = sorted(
        (p for p in Path(job_log_dir).glob("*.log") if p.name != "server.log"),
        key=lambda p: p.name,
        reverse=True,
    )[: max(limit * 4, 60)]

    # Snapshot the in-memory queue (id -> Job) — the source of truth for what is
    # actually running/queued right now, independent of the durable log footer.
    inmem = dict(jobs)
    now = datetime.now()

    out = []
    for f in files:
        rec = {v: None for v in list(HEADER_FIELDS.values()) + list(FOOTER_FIELDS.values())}
        rec["log_file"] = str(f)
        started_at = None
        try:
            # Read enough head to clear the env-var block and reach the
            # "[Started at ...]" marker, plus the tail for the footer.
            with open(f, errors="replace") as fh:
                head = [next(fh, "") for _ in range(500)]
            with open(f, errors="replace") as fh:
                tail = list(collections.deque(fh, FOOTER_TAIL_LINES))
        except OSError:
            continue
        for line in head:
            s = line.strip()
            if s.startswith("[Started at ") and s.endswith("]"):
                started_at = s[len("[Started at ") : -1].strip()
                continue
            key, sep, val = line.partition(":")
            if sep and key.strip() in HEADER_FIELDS:
                rec[HEADER_FIELDS[key.strip()]] = val.strip()
        rec.update(_parse_job_log_footer(tail))
        for line in tail:
            if "[KILLED by device recovery]" in line:
                rec["recovery_killed"] = True
                msg = line.partition("[KILLED by device recovery]")[2].strip()
                if msg:
                    rec["cause"] = msg
                break

        rec["started_at"] = started_at

        # A file with no JOB ID header is not a job record: an empty stub left by a broker killed
        # between creating the log and writing its header (a reset/reboot/holder-kill can do that mid
        # write_action_log), or a truncated file. It carries no owner, command, or id, so classifying
        # it as "interrupted" fills the recent list with phantom `? ? interrupted` rows. A genuine
        # orphaned job always carries the header it wrote when it started — skip anything that does not.
        if not rec["job_id"]:
            continue

        # Footer present => finished; trust the recorded status/wait/runtime.
        if rec["status"] is not None:
            if rec.get("recovery_killed"):
                rec["status"] = "broker-kill"
                if not rec.get("cause"):
                    rec["cause"] = "killed by device recovery: a chip left PCIe bus"
                if not (rec.get("command") or "").startswith("[MCP killed"):
                    rec["command"] = f"[MCP killed: chips left PCIe] {rec.get('command') or ''}"
            elif rec["status"] == "started" and _hold_row is not None and rec["job_id"] == _hold_row["id"]:
                rec["runtime"] = _span_str(_hold_row["started_at"], now)
            elif rec.get("owner") == "[broker]hold" and rec["status"] == "completed":
                rec["status"] = "ended"
            out.append(rec)
            continue

        # No footer: classify against the live queue or active action.
        job = inmem.get(rec["job_id"])
        if job is not None and job.status == JobStatus.RUNNING:
            rec["status"] = "running"
            start = job.started_at or started_at
            rec["started_at"] = start
            rec["runtime"] = _span_str(start, now)  # elapsed up to now
            rec["wait"] = _span_str(job.queued_at or rec["queued_at"], start)
        elif job is not None and job.status == JobStatus.QUEUED:
            rec["status"] = "queued"
            rec["wait"] = _span_str(job.queued_at or rec["queued_at"], now)  # waiting up to now
        elif _action_row is not None and rec["job_id"] == _action_row["id"]:
            rec["status"] = "running"
            rec["started_at"] = _action_row["started_at"]
            rec["runtime"] = _span_str(_action_row["started_at"], now)
            rec["wait"] = "-"
        elif not started_at:
            # Queued, never dispatched, and no longer in the queue: held by the gate or cancelled
            # by its owner before it started. Nothing ran, so nothing was interrupted.
            rec["status"] = "abandoned"
            rec["wait"] = "-"
            rec["cause"] = "left the queue before it started (held by the gate or cancelled)"
        else:
            # Footerless log this broker doesn't track => orphaned by a restart. We never saw it
            # finish, so there is no recorded runtime — but the log's last write approximates when
            # it died, so report elapsed-to-there rather than a blank that reads as "ran for 0s".
            rec["status"] = "interrupted"
            rec["wait"] = _span_str(rec["queued_at"], started_at)
            last_write = None
            if started_at:
                try:
                    last_write = datetime.fromtimestamp(f.stat().st_mtime)
                    rec["runtime"] = _span_str(started_at, last_write.isoformat())
                except OSError:
                    pass
            # "Interrupted" alone tells its owner their job died and not one thing about
            # why. The broker recorded the why as it happened — say it.
            cause = _explain_interruption(rec["job_id"], started_at, last_write)
            if cause:
                rec["cause"] = cause
                tag = "interrupted"
                if "auto-recovery" in cause.lower() or "reboot" in cause.lower():
                    tag = "interrupted: reboot"
                elif "chips" in cause.lower() or "pcie" in cause.lower():
                    tag = "MCP killed: chips left PCIe"
                elif "holder-kill" in cause.lower():
                    tag = "MCP killed: dead-chip holder-kill"
                if not (rec.get("command") or "").startswith(f"[{tag}]"):
                    rec["command"] = f"[{tag}] {rec.get('command') or ''}"
        out.append(rec)

    # A broker sub-action (a reset, a fabric pass) writes its durable entry only when it finishes,
    # so for its whole 45-60s hold it is absent from the files above — present only in the live
    # RUNNING view. Surface it here so the history stays continuous while it runs. A sub-action
    # that reserved its identity shows that reserved row — same id, start, and name the durable
    # row will carry — so it does not re-sort or rename when it lands. A stage that reserved no
    # durable row (a pure read check that leaves no history) still shows its live `--` narration.
    if _action_row is not None and not any(r.get("job_id") == _action_row["id"] for r in out):
        r = _action_row
        out.append(
            {
                "job_id": r["id"],
                "owner": r["owner"],
                "command": r["command"],
                "queued_at": r["started_at"],
                "started_at": r["started_at"],
                "finished_at": None,
                "status": "running",
                "exit_code": None,
                "wait": None,
                "runtime": _span_str(r["started_at"], now.isoformat()),
                "log_file": None,
            }
        )
    elif device_op_active:
        _started = device_op_started_at or now.isoformat()
        out.append(
            {
                "job_id": "--",
                "owner": device_op_owner or "[broker]",
                "command": device_op_detail or device_op_active,
                "queued_at": _started,
                "started_at": _started,
                "finished_at": None,
                "status": "running",
                "exit_code": None,
                "wait": None,
                "runtime": _span_str(_started, now.isoformat()),
                "log_file": None,
            }
        )

    # A device held degraded with no op running is a blank in the audit trail otherwise: no
    # job ran, no reset ran, yet the device was refused to every tenant. Surface that hold as
    # its own RUNNING row so the window it sat refused is legible, not missing. Mutually
    # exclusive with the op row above — _device_hold_state yields nothing while an op runs.
    hold = _device_hold_state(now)
    if _hold_row is not None and not any(r.get("job_id") == _hold_row["id"] for r in out):
        r = _hold_row
        out.append(
            {
                "job_id": r["id"],
                "owner": r["owner"],
                "command": r["command"],
                "queued_at": r["started_at"],
                "started_at": r["started_at"],
                "finished_at": None,
                "status": "started",
                "exit_code": None,
                "wait": None,
                "runtime": _span_str(r["started_at"], now.isoformat()),
                "log_file": None,
            }
        )
    elif hold:
        reason, since = hold
        out.append(
            {
                "job_id": "--",
                "owner": "[broker]",
                "command": f"device HELD (degraded): {reason} — new tenant jobs refused until recovered",
                "queued_at": since,
                "started_at": since,
                "finished_at": None,
                "status": "running",
                "exit_code": None,
                "wait": None,
                "runtime": _span_str(since, now.isoformat()),
                "log_file": None,
            }
        )

    # Execution order, which is not queue order. A job can sit in a deep queue while the
    # resets and fabric checks provoked by earlier jobs run ahead of it, and ordering by
    # queue time shows those repairs bunched at the head of the queue rather than after the
    # run that caused them — which is where they actually happened, and the only reading
    # that lets anyone see what the device was doing between two jobs.
    #
    # A job that has not started yet has no execution time; it sorts by when it will run,
    # i.e. after everything that already has, which is what its place in the queue means.
    def _exec_key(r: dict) -> str:
        return r.get("started_at") or r.get("queued_at") or ""

    pending = [r for r in out if r["status"] == "queued"]
    ran = [r for r in out if r["status"] != "queued"]
    ran.sort(key=_exec_key, reverse=True)
    pending.sort(key=lambda r: r.get("queued_at") or "")
    return (pending + ran)[:limit]


async def cleanup_finished_jobs():
    """Remove finished jobs older than JOB_RETENTION_SEC from memory.

    Job output is already persisted to log files, so we don't need to keep
    completed jobs in memory indefinitely.

    Uses lock to prevent race conditions with concurrent job state access.
    """
    now = datetime.now()
    to_remove = []

    async with get_lock():
        for job_id, job in jobs.items():
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.KILLED):
                if job.finished_at:
                    finished = datetime.fromisoformat(job.finished_at)
                    age_sec = (now - finished).total_seconds()
                    if age_sec > JOB_RETENTION_SEC:
                        to_remove.append(job_id)

        for job_id in to_remove:
            del jobs[job_id]
            if logger:
                logger.info(f"CLEANUP removed job_id={job_id} from memory")


def save_stats():
    """Save stats to disk and update symlink."""
    if not stats or not stats_file or not stats_dir:
        return

    # Write stats file
    with open(stats_file, "w") as f:
        json.dump(stats.to_dict(), f, indent=2)

    # Update 'current' symlink
    current_link = stats_dir / "current"
    if current_link.is_symlink() or current_link.exists():
        current_link.unlink()
    current_link.symlink_to(stats_file.name)


async def stats_persistence_loop():
    """Background task to periodically save stats, and to publish the Prometheus textfile on the
    same cadence (see ``metrics.write_textfile`` — it never raises, so it never needs its own
    try/except here)."""
    if logger:
        logger.info("STATS persistence loop started")

    while True:
        await asyncio.sleep(STATS_UPDATE_SEC)
        try:
            save_stats()
            if logger:
                logger.debug(f"STATS saved to {stats_file}")
        except Exception as e:
            if logger:
                logger.error(f"STATS save failed: {e}")
        # A safety-net recompute alongside the mutation-point updates in _update_queue_depth_metric
        # — cheap, and self-correcting against any call site this task's wiring missed.
        _update_queue_depth_metric()
        _publish_telemetry_snapshot()
        metrics.write_textfile()


_JOB_SCOPE_PREFIX = "ttdev-job-"
_SCOPE_POLL_SEC = 5


def job_scope_unit(job_id: str) -> str:
    """Deterministic transient-scope name for a job — the handle that lets a
    restarted broker find the job still running and re-adopt it."""
    return f"{_JOB_SCOPE_PREFIX}{job_id}.scope"


def _scope_unit_job_id(unit: str) -> Optional[str]:
    if unit.startswith(_JOB_SCOPE_PREFIX) and unit.endswith(".scope"):
        return unit[len(_JOB_SCOPE_PREFIX) : -len(".scope")]
    return None


def list_active_job_scopes() -> dict:
    """job_id -> scope unit for every active ttdev-job-*.scope. systemd owns these
    units (not the daemon's cgroup), so this is ground truth across a restart."""
    try:
        out = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--type=scope",
                "--state=active",
                "--no-legend",
                "--plain",
                f"{_JOB_SCOPE_PREFIX}*.scope",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.splitlines():
        parts = line.split()
        jid = _scope_unit_job_id(parts[0]) if parts else None
        if jid:
            found[jid] = parts[0]
    return found


def _scope_active(scope: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", scope], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


async def _terminate_job(job_id: str, pid: Optional[int], grace_sec: float = GRACEFUL_KILL_GRACE_SEC) -> None:
    """Gracefully terminate a RUNNING job (hung / timed-out) so it RELEASES THE DEVICE.

    A privsep job runs in its own systemd --scope, and killpg on the broker-captured pid does
    not reliably reach it: systemd reparents the payload into the scope's cgroup, so the pid we
    hold is the systemd-run wrapper, not the job. killpg then leaves the real process — mid-CCL,
    mid-fabric — running until it is SIGKILLed some other way, which is exactly the eth-core wedge
    (a timed-out job reaped by killpg was observed exiting with code None on a wedged fabric).
    Signal the SCOPE instead so systemd hits the whole cgroup; a non-privsep job has no scope, so
    fall back to the process group. Both terminators lead with SIGINT -- the only signal a ttnn job
    unwinds on to close the mesh (see _terminate_process_group / _terminate_scope).
    """
    scope = job_scope_unit(job_id)
    if await asyncio.to_thread(_scope_active, scope):
        await _terminate_scope(scope, grace_sec=grace_sec)
    elif pid:
        await _terminate_process_group(pid, grace_sec=grace_sec)


def _readopted_deadline(job: "Job") -> float:
    """Monotonic instant at which a re-adopted job is overdue.

    Time already served counts against the limit — the job started under the previous
    broker, and re-adoption must not hand it a fresh one. A start time we cannot read is
    measured from now instead, which errs late but still terminates.
    """
    limit = max(1, min(job.timeout_sec or MAX_TIMEOUT_SEC, MAX_TIMEOUT_SEC))
    served = 0.0
    if job.started_at:
        try:
            served = max(0.0, (datetime.now() - datetime.fromisoformat(job.started_at)).total_seconds())
        except ValueError:
            served = 0.0
    return time.monotonic() + max(0.0, limit - served)


def _job_from_log(job_id: str) -> Optional["Job"]:
    """Reconstruct a minimal Job from its persisted log header, for re-adoption."""
    if not job_log_dir:
        return None
    matches = sorted(Path(job_log_dir).glob(f"*_{job_id}.log"))
    if not matches:
        return None
    path = matches[-1]
    hdr, started = {}, None
    try:
        with open(path, errors="replace") as fh:
            for _ in range(500):
                line = fh.readline()
                if not line:
                    break
                s = line.strip()
                if s.startswith("[Started at ") and s.endswith("]"):
                    started = s[len("[Started at ") : -1].strip()
                    continue
                k, sep, v = line.partition(":")
                if sep:
                    hdr[k.strip()] = v.strip()
    except OSError:
        return None
    # The header's TIMEOUT is the job's reservation and it must survive the restart:
    # the runner task that enforced it died with the previous broker, so this value is
    # all the re-adoption monitor has to bound the job with.
    try:
        readopted_timeout = int(str(hdr.get("TIMEOUT", "")).rstrip("s").strip())
    except (TypeError, ValueError):
        readopted_timeout = DEFAULT_TIMEOUT_SEC
    job = Job(
        id=job_id,
        owner=hdr.get("OWNER", "?"),
        workspace=hdr.get("WORKSPACE", "/"),
        command=hdr.get("COMMAND", ""),
        queued_at=hdr.get("QUEUED", ""),
        status=JobStatus.RUNNING,
        started_at=started,
        timeout_sec=readopted_timeout,
    )
    job.log_file = str(path)
    return job


async def _terminate_scope(scope: str, grace_sec: float = GRACEFUL_KILL_GRACE_SEC) -> None:
    """Stop a re-adopted job's systemd scope so it RELEASES THE DEVICE.

    Same ladder and same reason as _terminate_process_group: `systemctl stop` sends
    SIGTERM, and Python installs no SIGTERM handler -- the interpreter dies without
    unwinding, ttnn never closes the mesh, and the eth cores are left mid-transaction.
    SIGINT is the signal that unwinds it, so it goes first; `stop` is the reap."""

    async def _run(*argv: str) -> None:
        await asyncio.to_thread(subprocess.run, list(argv), capture_output=True)

    await _run("systemctl", "kill", "--signal=SIGINT", scope)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + grace_sec
    while loop.time() < deadline:
        if not await asyncio.to_thread(_scope_active, scope):
            return
        await asyncio.sleep(_SCOPE_POLL_SEC)
    if logger:
        logger.info(f"TERMINATE scope={scope} survived SIGINT for {grace_sec}s; escalating")
    await _run("systemctl", "stop", scope)


async def _monitor_readopted_scope(job_id: str, scope: str):
    """Wait for a re-adopted scope to end, then finalize the job and free the device gate.

    The exit status comes from the file the job itself wrote (see job_exit_file): the
    broker that spawned the scope is gone, and a scope reports its status only to its
    spawner. Without that file this recorded every re-adopted job as "completed", so a job
    that failed across an auto-update told its owner it had passed.
    """
    # Re-arm the reservation. The runner task that enforced timeout_sec died with the
    # previous broker, and nothing here replaced it: a job that outlived an auto-update
    # ran UNBOUNDED and held the device for as long as it liked (observed: a 600s job
    # held a shared galaxy for 1044s). The scope is stopped gracefully so it can still
    # release the chip on the way out.
    timed_out = False
    try:
        deadline = None
        job0 = jobs.get(job_id)
        if job0 is not None and job0.started_at and job0.timeout_sec:
            try:
                started = datetime.fromisoformat(job0.started_at)
                deadline = started.timestamp() + job0.timeout_sec
            except (TypeError, ValueError):
                deadline = None
        while await asyncio.to_thread(_scope_active, scope):
            if deadline is not None and datetime.now().timestamp() > deadline:
                timed_out = True
                if logger:
                    logger.warning(
                        f"JOB_RUNNER job_id={job_id} TIMEOUT after {job0.timeout_sec}s "
                        f"(re-adopted across a broker restart); stopping scope {scope}"
                    )
                await _terminate_scope(scope)
                break
            await asyncio.sleep(_SCOPE_POLL_SEC)
    finally:
        job = jobs.get(job_id)
        if job is not None:
            rc = await asyncio.to_thread(read_job_exit_code, job_id)
            async with get_lock():
                if job.status == JobStatus.RUNNING:
                    if timed_out:
                        job.status = JobStatus.TIMEOUT
                        job.error = (job.error or "") + (
                            f"\n[TIMEOUT after {job.timeout_sec}s] (re-adopted across a broker restart)"
                        )
                    elif rc is None:
                        # The job died without running its EXIT trap — killed by a signal,
                        # or the host went down under it. Either way it did not finish, and
                        # calling that "completed" is the lie this code used to tell.
                        job.status = JobStatus.FAILED
                        job.error = (job.error or "") + (
                            "\n[broker] the broker restarted while this job was running and the job "
                            "left no exit status — it was killed, or the host went down. Its output "
                            "up to that point is in the log."
                        )
                    else:
                        job.exit_code = rc
                        job.status = JobStatus.COMPLETED if rc == 0 else JobStatus.FAILED
                job.finished_at = job.finished_at or datetime.now().isoformat()
            # A re-adopted job can leave the mesh wedged just as a normally-run one can, and this
            # path has no post-job _verify_device — so unless it flags the device, the tenant queued
            # behind it is dispatched onto the wedge. Mirror the normal clean gate: the runtime
            # naming a fault is the strongest evidence and is independent of exit code (a wedge
            # rides out on exit 0), so scan the log first, then fall back to a wedge-risk exit.
            job_log = Path(job.log_file) if job.log_file else None
            fault = _scan_output_for_device_fault(job_log)
            if fault:
                _mark_device_reported_fault(f"re-adopted job {job_id} {fault}", job=job)
            elif _is_wedge_risk_exit(job.status, job.exit_code):
                _mark_device_dirty(
                    f"re-adopted job {job_id} ended {job.status.value}"
                    + (f" (exit {job.exit_code})" if job.exit_code is not None else ""),
                    job=job,
                )
            if job.log_file:
                try:
                    write_job_log_footer(Path(job.log_file), job)
                except OSError:
                    pass
        clear_job_exit_file(job_id)
        readopted_scopes.pop(job_id, None)
        if logger:
            logger.info(
                f"RE-ADOPT job_id={job_id} scope {scope} ended; finalized " f"exit={job.exit_code if job else '?'}"
            )


async def reconcile_running_scopes():
    """Re-adopt jobs still running in named scopes after a broker restart so a
    restart never orphans a running job: it stays visible as RUNNING and the
    queue waits for it instead of starting a second job onto a busy device."""
    for job_id, scope in list_active_job_scopes().items():
        if job_id in jobs:
            continue
        fault_reason = _device_fault_failed_reason(job_id)
        if fault_reason:
            # This job was SIGKILLed by device recovery for a wedge it caused; do not re-adopt the
            # scope that ran it — re-running it is how a boot loop starts. Drop the record now that
            # the one restart it guarded against has consulted it.
            _clear_device_fault_failed(job_id)
            if logger:
                logger.warning(f"RE-ADOPT skipping job_id={job_id} (killed by device recovery): {fault_reason}")
            health_event("scope_reconcile_skipped_device_fault", job_id=job_id, scope=scope, reason=fault_reason)
            continue
        job = _job_from_log(job_id) or Job(
            id=job_id, owner="?", workspace="/", command=f"(re-adopted {scope})", queued_at="", status=JobStatus.RUNNING
        )
        job.status = JobStatus.RUNNING
        jobs[job_id] = job
        readopted_scopes[job_id] = scope
        if logger:
            logger.info(f"RE-ADOPT job_id={job_id} from {scope} (owner={job.owner})")
        asyncio.create_task(_monitor_readopted_scope(job_id, scope))


def _job_cooldown_remaining_sec(last_end_monotonic: float, now_monotonic: float) -> float:
    """Seconds a just-dequeued job must let the mesh rest before its device work begins.

    Zero when the cooldown is disabled, when no prior job has ended this process (the 0.0
    sentinel — a fresh boot's first job never waits), or when the rest already elapsed while
    the job sat in the queue. Any wait is only the remainder, so a job that queued long after
    the previous one finished pays nothing."""
    if JOB_COOLDOWN_SEC <= 0 or last_end_monotonic == 0.0:
        return 0.0
    remaining = JOB_COOLDOWN_SEC - (now_monotonic - last_end_monotonic)
    return remaining if remaining > 0 else 0.0


def _job_burst_decision(recent_times: list[float], now_monotonic: float) -> tuple[bool, float, list[float]]:
    """Decide whether an owner's new submission fits under the per-owner burst cap.

    `recent_times` is that owner's earlier admitted-submission monotonic timestamps, oldest
    first. Returns (allowed, retry_after_sec, kept_times):
      * kept_times — recent_times pruned to the trailing window; the caller stores it back so
        the per-owner record never grows past one window of submissions.
      * allowed is True (retry_after 0) when the cap is disabled (JOB_BURST_MAX <= 0) or fewer
        than the cap have been admitted inside the window.
      * at/over the cap, allowed is False and retry_after_sec is how long until the oldest
        in-window submission ages out and frees the next slot.
    """
    if JOB_BURST_MAX <= 0:
        return True, 0.0, list(recent_times)
    cutoff = now_monotonic - JOB_BURST_WINDOW_SEC
    kept = [t for t in recent_times if t > cutoff]
    if len(kept) >= JOB_BURST_MAX:
        retry_after = kept[0] + JOB_BURST_WINDOW_SEC - now_monotonic
        return False, retry_after if retry_after > 0 else 0.0, kept
    return True, 0.0, kept


async def job_runner():
    """Main loop processing jobs from the queue."""
    global current_process, current_job_id, last_job_end_monotonic

    if logger:
        logger.info("JOB_RUNNER started, waiting for jobs...")

    while True:
        job_id = await get_job_queue().get()

        # Handle case where job was cleaned up while still in queue
        job = jobs.get(job_id)
        if job is None:
            if logger:
                logger.info(f"JOB_RUNNER skipping removed job_id={job_id}")
            _forget_queued_job(job_id)  # gone from memory => never runs; its spec must not survive
            get_job_queue().task_done()
            continue

        if logger:
            logger.info(f"JOB_RUNNER dequeued job_id={job_id}")

        # Skip jobs that were cancelled while queued
        if job.status == JobStatus.KILLED:
            if logger:
                logger.info(f"JOB_RUNNER skipping cancelled job_id={job_id}")
            _forget_queued_job(job_id)  # cancelled is terminal: a restart must not revive it
            get_job_queue().task_done()
            continue

        # Wait out any job re-adopted from a prior broker instance: it still owns
        # the device, so we must not start a second job on top of it.
        while readopted_scopes:
            await asyncio.sleep(1)

        # Use existing log file (created at queue time)
        job_log_file = Path(job.log_file) if job.log_file else None

        # Let the mesh rest between consecutive jobs. Back-to-back saturating runs with no gap
        # is what pushed a marginal tray off the bus; this is the only idle the silicon is
        # guaranteed between one job's device work and the next's. Off by default, so it changes
        # nothing until a host opts in. A shutdown cancels this sleep like any queue wait — the
        # job is undispatched and keeps its persisted spec for the next broker to re-adopt.
        cooldown = _job_cooldown_remaining_sec(last_job_end_monotonic, time.monotonic())
        if cooldown > 0:
            health_event("job_cooldown", job=job_id, owner=job.owner, wait_sec=round(cooldown, 1))
            if logger:
                logger.info(
                    f"JOB_RUNNER job_id={job_id} cooling down {cooldown:.1f}s "
                    f"before dispatch (mesh rest between jobs)"
                )
            await asyncio.sleep(cooldown)

        # An external scheduler's pre-step/post-step may hold the device reserved right now,
        # partway through its own gate (see _reserve_external_step / _run_step_gate): its
        # in-flight check is only a snapshot, taken before the reservation, and its own device
        # work happens in threads a deadline cannot cancel out from under it. Waiting HERE, before
        # this job ever calls _ensure_device_clean_for_next_job or spawns, is what keeps this
        # dispatch from landing underneath that gate's holder scan or its device-op lock. One
        # direction only — the step never waits on the runner — so there is no cycle to deadlock.
        # This can hold for as long as the step's own gate takes — for `post-step` that includes a
        # possible recovery-ladder climb, unbounded by the step's deadline (03 I29) — so it is
        # logged like the cooldown wait above: a queue that dispatches nothing during an incident
        # must have a line somewhere naming the holder, not just a flag nothing surfaces.
        _external_step_wait = get_external_step_free_event()
        if not _external_step_wait.is_set():
            health_event("job_deferred_for_external_step", job=job_id, owner=job.owner, holder=external_step_active)
            if logger:
                logger.info(
                    f"JOB_RUNNER job_id={job_id} deferred — external step reservation held "
                    f"({external_step_active or 'unknown'}); dispatch resumes once it releases"
                )
            await _external_step_wait.wait()

        # The gate. No job starts while a broker device op holds the device, or while the
        # device is dirty and unverified: a job that ends abnormally can leave the mesh
        # wedged, and a reset or fabric pass owns the silicon this job is about to use.
        # Reset + verify happens here, while idle, so no job inherits another tenant's
        # wedge and none starts on hardware being reset out from under it.
        try:
            blocked_reason = await _await_device_free_for_tenant(job_log_file)
        except Exception as e:  # a gate ERROR must never itself block a job
            if logger:
                logger.error(f"JOB_RUNNER clean-device gate error for job_id={job_id}: {e}")
            blocked_reason = ""  # only an affirmative degraded verdict blocks; a bug does not

        # One durable timeline entry when this device opens or lifts a tenant-refused hold,
        # keyed off the same verdict the dispatch decision uses so the two can never
        # disagree. Latched inside, so the outage that refuses a queue's worth of jobs is
        # one held event and its recovery is one released event.
        _note_tenant_gate_verdict(blocked_reason)

        # Env-gated (default OFF): HOLD a degraded device rather than failing the arriving
        # job. Default OFF keeps fail-fast until a recovery ladder can bound the hold and the
        # change is proven on live silicon. When on, a degraded device holds the queue and
        # this job dispatches the instant the device is fit — the G2 hold, not a bounded retry.
        if blocked_reason and _tenant_hold_enabled():
            try:
                blocked_reason = await _hold_job_until_device_fit(job, job_log_file, blocked_reason)
            except Exception as e:  # a hold bug must never wedge the runner -> fall back to fail-fast
                if logger:
                    logger.error(f"JOB_RUNNER hold-gate error for job_id={job_id}: {e}")
            if job.status == JobStatus.KILLED:
                # Cancelled while held: already terminal — just sweep it and take the next job.
                await cleanup_finished_jobs()
                get_job_queue().task_done()
                continue

        # The gate is BLOCKING, not advisory: a device the gate could not make fit for a
        # tenant does not get one dispatched onto it. Refuse the job here, before it runs,
        # rather than run its command onto a wedge and call the crash "failing honestly".
        if blocked_reason:
            try:
                await _refuse_job_on_degraded_device(job, job_log_file, blocked_reason)
            except Exception as e:  # the refusal must terminalize the job, never wedge the runner
                if logger:
                    logger.error(f"JOB_RUNNER refuse-gate error for job_id={job_id}: {e}")
                async with get_lock():
                    job.status = JobStatus.FAILED
                    job.finished_at = datetime.now().isoformat()
                    job.error = f"device degraded — job not dispatched: {blocked_reason}"
            # A degraded device refuses EVERY arriving job, so this is the high-volume path
            # during an outage — the one that must still sweep finished jobs, or the retained
            # set grows without bound exactly while an operator is reading it.
            await cleanup_finished_jobs()
            get_job_queue().task_done()
            continue

        # Fail closed on an unhonorable privsep identity, BEFORE the job flips to RUNNING. When
        # privsep is active every job must run as its real, non-root submitter; if we can't
        # establish that identity the direct-exec fallthrough below would run it as the broker
        # (root). Refuse here instead — never fabricate that privilege for a job we can't scope.
        privsep_refusal_reason = privsep_refusal(job.peer_uid)
        if privsep_refusal_reason:
            try:
                await _refuse_job_privsep_identity(job, job_log_file, privsep_refusal_reason)
            except Exception as e:  # the refusal must terminalize the job, never wedge the runner
                if logger:
                    logger.error(f"JOB_RUNNER privsep-refuse error for job_id={job_id}: {e}")
                async with get_lock():
                    job.status = JobStatus.FAILED
                    job.finished_at = datetime.now().isoformat()
                    job.error = f"privsep: job not dispatched: {privsep_refusal_reason}"
            await cleanup_finished_jobs()
            get_job_queue().task_done()
            continue

        # Update job state under lock
        async with get_lock():
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now().isoformat()
            _update_queue_depth_metric()
            # Track device became busy
            if stats:
                stats.update_device_state(now_busy=True)

        # The sampler runs continuously; this only drops the idle samples from before the
        # job, so an incident's trace is the run-up to THIS job and not the hour before it.
        sampler.mark_job_window()

        # Write "started" marker to log
        if job_log_file:
            with open(job_log_file, "a") as f:
                f.write(f"[Started at {job.started_at}]\n\n")

        if logger:
            logger.info(f"JOB_RUNNER starting job_id={job_id}, log={job_log_file}")

        # Get activation script using env_vars resolved at queue time (not re-reading env_file)
        activation_script, _ = get_activation_script(job.workspace, env_file=None, inherited_env=job.env_vars)

        exit_file = job_exit_file(job_id)
        full_command = f"""
{_exit_trap_preamble(str(exit_file))}set -e
{activation_script}
{job.command}
"""

        # Create process in new session (os.setsid) so all child processes share the same
        # process group ID. This enables reliable cleanup of nested processes via killpg()
        # in the finally block, preventing orphaned child processes from holding device locks.
        # Under privsep (root + opt-in) the command runs as the submitting user inside a
        # device-admitted systemd scope; otherwise it runs directly as today.
        privsep_prefix = privsep_prefix_for(job.peer_uid, unit=job_scope_unit(job_id))
        cancelled = False  # set if the broker is shutting down (don't kill the job)
        terminal_note = ""  # timeout/exception marker, appended to error at the end
        try:
            if privsep_prefix:
                if logger:
                    logger.info(f"  privsep: running job {job_id} as uid={job.peer_uid} via systemd-run")
                proc = await asyncio.create_subprocess_exec(
                    *privsep_prefix,
                    "/bin/bash",
                    "-c",
                    full_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=os.setsid,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    full_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=os.setsid,
                    executable="/bin/bash",
                )

            async with get_lock():
                current_process = proc
                current_job_id = job_id
                job.pid = proc.pid
                # A job that never emits a line at all is still measured for silence, so the
                # clock starts at spawn rather than at first output.
                job.last_output_monotonic = time.monotonic()
            # The process is live, so its scope is what a restart re-adopts from here on. Drop
            # the queued spec only now: forgetting it any earlier would lose the job if the
            # broker died between dispatch and spawn.
            _forget_queued_job(job_id)

            # One open handle for the job's lifetime (line-buffered so streaming
            # readers see each line) — opening per line is a syscall storm under a
            # chatty job. In-memory capture is bounded (out_buf/err_buf deques).
            log_fh = open(job_log_file, "a", buffering=1) if job_log_file else None

            async def read_stream(stream, name, sink):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    job.last_output_monotonic = time.monotonic()
                    if job.doomed_monotonic is None and any(p in text for p in DOOMED_PATTERNS):
                        job.doomed_monotonic = time.monotonic()
                        note(
                            f"runtime reports the device unrecoverable — reaping in "
                            f"{DOOMED_GRACE_SEC}s rather than waiting out its unwind"
                        )
                    sink.append(text)
                    if log_fh:
                        log_fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] [{name}] {text}")

            def note(text):
                """Say something in the job's own log, in the job's own voice."""
                if log_fh:
                    try:
                        log_fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] [broker] {text}\n")
                    except (OSError, ValueError):
                        pass

            async def hung_watchdog():
                # Says the silence out loud, and only then reaps it.
                #
                # A wedged job prints NOTHING, and the runtime has no always-on heartbeat of its
                # own: the only progress line that survives a hang is one somebody's framework
                # chose to emit, and most workloads have none. So a job stuck in a CCL and a job
                # doing thirty minutes of honest work are the same empty log, and the person
                # reading it cannot tell which they have. The broker can: it holds the clock on
                # every job's output regardless of what the job is written in, which makes this
                # the one place a hang can be made visible for all of them.
                #
                # Announcing is independent of reaping — a host that has turned the reaper off
                # still needs its hangs to be legible.
                #
                # Kills the process rather than raising: the streams then hit EOF and the gather
                # below returns on its own, so the normal completion path runs and the job keeps
                # whatever output it managed to produce before it went quiet.
                announced = 0.0
                while True:
                    silent = time.monotonic() - job.last_output_monotonic

                    # Checked before silence: this job is still talking, so the silence clock will
                    # never reach it, and every second spent waiting is a second the device is held
                    # for a job the runtime has already written off.
                    if (
                        job.doomed_monotonic is not None
                        and DOOMED_GRACE_SEC > 0
                        and time.monotonic() - job.doomed_monotonic >= DOOMED_GRACE_SEC
                    ):
                        async with get_lock():
                            if job.status != JobStatus.RUNNING:
                                return
                            job.status = JobStatus.HUNG
                        note(
                            "device declared unrecoverable by the runtime "
                            f"{DOOMED_GRACE_SEC}s ago — terminating so the device can be recovered"
                        )
                        if logger:
                            logger.warning(
                                f"JOB_RUNNER job_id={job_id} DOOMED: device reported unrecoverable "
                                f"(grace {DOOMED_GRACE_SEC}s) -> terminating"
                            )
                        await _terminate_job(job_id, job.pid)
                        return

                    if HUNG_NOTICE_SEC > 0 and silent - announced >= HUNG_NOTICE_SEC:
                        announced = silent
                        left = f"; reaping as hung at {HUNG_SILENCE_SEC}s" if HUNG_SILENCE_SEC > 0 else ""
                        note(f"no output for {int(silent)}s{left}")

                    if HUNG_SILENCE_SEC > 0 and silent >= HUNG_SILENCE_SEC:
                        async with get_lock():
                            # A user kill that already landed owns the verdict; do not relabel it.
                            if job.status != JobStatus.RUNNING:
                                return
                            job.status = JobStatus.HUNG
                        note(
                            f"no output for {int(silent)}s (limit {HUNG_SILENCE_SEC}s) — "
                            f"terminating this job as hung"
                        )
                        if logger:
                            logger.warning(
                                f"JOB_RUNNER job_id={job_id} HUNG: no output for {int(silent)}s "
                                f"(limit {HUNG_SILENCE_SEC}s) -> terminating"
                            )
                        # Kill the whole scope for a privsep job — killpg on the wrapper pid leaves the
                        # scoped payload running and wedges the eth. See _terminate_job.
                        await _terminate_job(job_id, job.pid)
                        return

                    await asyncio.sleep(HUNG_POLL_SEC)

            watchdog = asyncio.create_task(hung_watchdog())
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(proc.stdout, "stdout", job.out_buf),
                        read_stream(proc.stderr, "stderr", job.err_buf),
                    ),
                    timeout=job.timeout_sec,
                )
            finally:
                watchdog.cancel()
                if log_fh:
                    log_fh.close()
            await proc.wait()

            async with get_lock():
                job.exit_code = proc.returncode
                # Preserve a verdict already reached by the killer (user kill, hung reaper):
                # the process died by our signal, so its returncode would otherwise read FAILED.
                if job.status not in (JobStatus.KILLED, JobStatus.HUNG):
                    job.status = JobStatus.COMPLETED if job.exit_code == 0 else JobStatus.FAILED

            if logger:
                logger.info(
                    f"JOB_RUNNER job_id={job_id} finished: status={job.status.value}, exit_code={job.exit_code}"
                )

        except asyncio.CancelledError:
            # Broker is shutting down (e.g. restart for auto-update). Do NOT kill
            # the job — it runs in its own systemd scope, so leave it alive and let
            # the next broker's startup re-adopt it. Re-raise to end the task.
            cancelled = True
            if logger:
                logger.info(f"JOB_RUNNER shutdown: leaving job {job_id} alive in its scope for re-adoption")
            raise

        except asyncio.TimeoutError:
            # Graceful SIGTERM -> grace -> SIGKILL so the job can release the device cleanly
            # (a hard SIGKILL mid-CCL is the classic mesh wedge). For a privsep job this MUST
            # signal the systemd scope, not killpg the wrapper pid — see _terminate_job.
            await _terminate_job(job_id, job.pid)

            async with get_lock():
                job.status = JobStatus.TIMEOUT
            terminal_note = f"\n[TIMEOUT after {job.timeout_sec}s]"

            if logger:
                logger.warning(f"JOB_RUNNER job_id={job_id} TIMEOUT after {job.timeout_sec}s")

        except Exception as e:
            async with get_lock():
                job.status = JobStatus.FAILED
            terminal_note = f"\n[EXCEPTION: {e}]"

            if logger:
                logger.error(f"JOB_RUNNER job_id={job_id} EXCEPTION: {e}")

        finally:
            # On shutdown leave the running job alive (it survives in its scope for
            # re-adoption) — skip the kill, the footer, and the completion bookkeeping.
            if cancelled:
                pass
            else:
                # Materialize the bounded capture into the result fields (the log
                # file holds the complete output; these are a tail), then drop the
                # per-line buffers so a retained finished job doesn't hold them.
                if job.status == JobStatus.HUNG:
                    terminal_note = (
                        "\n[HUNG: runtime reported the device unrecoverable]"
                        if job.doomed_monotonic is not None
                        else f"\n[HUNG: no output for {HUNG_SILENCE_SEC}s]"
                    )
                job.output = "".join(job.out_buf)
                job.error = "".join(job.err_buf) + terminal_note
                job.out_buf.clear()
                job.err_buf.clear()

                # Ensure process group is killed (idempotent - safe even if already dead)
                if job.pid:
                    try:
                        # Use PID directly as PGID (os.setsid makes them equal)
                        os.killpg(job.pid, signal.SIGKILL)
                        if logger:
                            logger.debug(f"JOB_RUNNER killed process group for job_id={job_id}, pid={job.pid}")
                    except (ProcessLookupError, OSError):
                        # Process already dead - expected for normal completion due to async timing
                        # between process exit and finally block execution
                        pass
                    except Exception as e:
                        if logger:
                            logger.warning(f"JOB_RUNNER failed to kill process group for job_id={job_id}: {e}")

                # This broker saw the job exit itself, so its own returncode is authoritative
                # and the job's file has served no purpose. Only a re-adopted job reads it.
                clear_job_exit_file(job_id)

                async with get_lock():
                    job.finished_at = datetime.now().isoformat()
                    current_process = None
                    current_job_id = None
                    # Track device became idle and record job stats
                    if stats:
                        stats.update_device_state(now_busy=False)
                        stats.record_job_completion(job.status, job.wait_sec, job.runtime_sec)

                # Clean-device gate bookkeeping: if this job ended in a way that can
                # leave the mesh/driver wedged, flag the device dirty so the post-job
                # check below forces a reset even if the traffic probe happens to
                # pass. The check clears the flag once the device verifies clean.
                # The runtime naming a fault is the strongest evidence there is, and the
                # exit code has no bearing on whether it said so: a wedge can raise mid-run
                # and still let the harness exit 0, and it can equally ride out on a crash.
                # Scanning only the benign exits threw the specific evidence away on exactly
                # the jobs that died of it, leaving a generic "ended failed" the gate then
                # cleared on the word of checks that cannot see this fault at all.
                fault = _scan_output_for_device_fault(job_log_file)
                if fault:
                    _mark_device_reported_fault(f"job {job_id} {fault}", job=job)
                elif job_id in reset_killed_job_ids:
                    # This job's death is OURS — a reset/recovery SIGKILLed it to take the device
                    # (most often a chip off the bus whose mapping would hang the host). Treating it
                    # as evidence the mesh needs a reset makes the reset its own justification, and
                    # the operator's one reset becomes two. Every extra reset of 32 ASICs is another
                    # chance to drop a marginal chip.
                    reset_killed_job_ids.discard(job_id)
                    # Say so on the JOB: a bare signal exit reads to the submitter as their own code
                    # crashing. Name the real cause so they resubmit rather than debug a phantom.
                    async with get_lock():
                        job.status = JobStatus.KILLED
                        if not job.error:
                            job.error = (
                                "killed by device recovery: the broker SIGKILLed this job to "
                                "reset/reboot the device after a wedge (e.g. a chip left the "
                                "PCIe bus). It did not crash on its own — resubmit once the "
                                "device is healthy."
                            )
                    if job_log_file:
                        try:
                            with open(job_log_file, "a") as f:
                                f.write(f"\n[KILLED by device recovery] {job.error}\n")
                        except OSError:
                            pass
                    if logger:
                        logger.info(
                            f"CLEAN-GATE job {job_id} was killed by device recovery; not "
                            f"flagging the device over a death we caused"
                        )
                elif _is_wedge_risk_exit(job.status, job.exit_code):
                    _mark_device_dirty(
                        f"job {job_id} ended {job.status.value}"
                        + (f" (exit {job.exit_code})" if job.exit_code is not None else ""),
                        job=job,
                    )

                # Write job log footer (outside lock - file I/O)
                if job_log_file:
                    write_job_log_footer(job_log_file, job)

                # Post-job health check — snapshot + fabric traffic — after EVERY
                # run, queue empty or not. A fabric wedge need not trip a wedge-risk
                # exit or print a known fault, so the dirty flag can miss it; the
                # traffic probe is the only authoritative catch. The finished job's
                # caller was already released (status set before this finally), so
                # this never bills the finished job, and idle time is free. It still
                # gates every later arrival: the runner is single-threaded, so a job
                # that queues during the check waits for it — and any reset it
                # triggers — before it starts.
                try:
                    # Any non-success exit means we do not know what the job did to the
                    # mesh, so the fabric is proved before the next tenant sees it — the
                    # periodic interval does not get a vote on whether a failure is
                    # investigated.
                    await _verify_device_after_job(
                        job_log_file,
                        job_failed=(job.status != JobStatus.COMPLETED or job.exit_code != 0),
                    )
                except Exception as e:  # noqa: BLE001 - never crash the runner
                    if logger:
                        logger.error(f"JOB_RUNNER post-job health gate error for job_id={job_id}: {e}")

                # The mesh's rest window starts now — after this job's device work AND the
                # post-job fabric pass, the last traffic the silicon saw. The next job's
                # cooldown measures from here.
                last_job_end_monotonic = time.monotonic()

                # Clean up old finished jobs from memory
                await cleanup_finished_jobs()

                if logger:
                    logger.info(f"JOB_RUNNER job_id={job_id} cleanup complete")


# ============== Create MCP Server ==============

QUEUED_SPEC_DIR = "queued"


def _queued_spec_path(job_id: str) -> Optional[Path]:
    if not job_log_dir:
        return None
    return Path(job_log_dir) / QUEUED_SPEC_DIR / f"{job_id}.json"


def _persist_queued_job(job: "Job") -> None:
    """Write a queued job's spec beside the logs so a broker restart does not silently eat it.

    The queue is an in-memory asyncio.Queue: a restart re-adopts what is RUNNING (its scope
    outlives us) and drops everything merely WAITING, with no trace beyond the tenant noticing
    their job never ran. The device is shared and a restart is routine — an autoupdate deploys
    on any idle window — so the queue has to survive one.
    """
    path = _queued_spec_path(job.id)
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "id": job.id,
                    "owner": job.owner,
                    "workspace": job.workspace,
                    "command": job.command,
                    "queued_at": job.queued_at,
                    "peer_uid": job.peer_uid,
                    "env": job.env,
                    "env_vars": job.env_vars,
                    "timeout_sec": job.timeout_sec,
                    "log_file": job.log_file,
                }
            )
        )
    except (OSError, TypeError, ValueError) as e:  # a job that cannot be persisted still runs
        if logger:
            logger.warning(f"could not persist queued job_id={job.id}: {e}")


def _forget_queued_job(job_id: str) -> None:
    """Drop a job's queued spec — it has started or reached a terminal state, so a restart must
    not re-run it. Called once the job's scope is live: the scope, not the spec, is what a
    restart re-adopts from that point on."""
    path = _queued_spec_path(job_id)
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


DEVICE_FAULT_FAILED_DIR = "device_fault_failed"
# A device-fault-failed record only has to outlive the ONE restart/reboot that follows the kill:
# queue restore and scope reconciliation consult it once, at startup. Prune anything older so a
# wrapped job id (000..999) can never inherit a stale ancestor's "do not run" verdict.
DEVICE_FAULT_FAILED_TTL_SEC = 86400


def _device_fault_failed_path(job_id: str) -> Optional[Path]:
    if not job_log_dir:
        return None
    return Path(job_log_dir) / DEVICE_FAULT_FAILED_DIR / f"{job_id}.json"


def _mark_job_device_fault_failed(job_id: str, reason: str) -> None:
    """Record — durably, across the reboot the recovery is about to cause — that this job was
    SIGKILLed by device recovery, so a restart's queue restore and scope reconciliation drop it
    rather than re-run the exact job that just wedged the device. That re-run is how a boot loop
    starts (user decision: a job that failed or caused a power cycle falls off the queue).

    Also keeps the in-memory reset-killed marker the runner reads to finalize the job as
    KILLED-by-recovery rather than a fresh wedge. Never raises: a job that cannot be persisted here
    still gets the in-memory marker, which is the pre-existing behaviour."""
    if not job_id:
        return
    reset_killed_job_ids.add(job_id)
    path = _device_fault_failed_path(job_id)
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"id": job_id, "reason": reason, "at_epoch": time.time(), "at": datetime.now().isoformat()})
        )
    except (OSError, TypeError, ValueError) as e:
        if logger:
            logger.warning(f"could not persist device-fault-failed job_id={job_id}: {e}")


def _clear_device_fault_failed(job_id: str) -> None:
    path = _device_fault_failed_path(job_id)
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _device_fault_failed_reason(job_id: str) -> Optional[str]:
    """The reason a job was dropped for a device-fault kill, or None if it was not (or its record
    has aged past DEVICE_FAULT_FAILED_TTL_SEC). Prunes a stale/unreadable record as a side effect,
    so the store never blocks a wrapped job id."""
    path = _device_fault_failed_path(job_id)
    if not path:
        return None
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        age = time.time() - float(rec.get("at_epoch"))
    except (TypeError, ValueError):
        age = None
    if age is None or age > DEVICE_FAULT_FAILED_TTL_SEC:
        _clear_device_fault_failed(job_id)
        return None
    return rec.get("reason") or "killed by device recovery"


async def _restore_queued_jobs() -> None:
    """Put jobs that were still waiting when the last broker died back on the queue.

    Only specs we can actually read are restored: a spec that will not parse is moved aside,
    never guessed at and never silently dropped. Ordered by queue time so a restart preserves
    the order tenants queued in.
    """
    if not job_log_dir:
        return
    d = Path(job_log_dir) / QUEUED_SPEC_DIR
    if not d.is_dir():
        return
    restored, broken, dropped = 0, 0, 0
    for p in sorted(d.glob("*.json")):
        try:
            spec = json.loads(p.read_text())
            job = Job(
                id=spec["id"],
                owner=spec["owner"],
                workspace=spec["workspace"],
                command=spec["command"],
                queued_at=spec["queued_at"],
                peer_uid=spec.get("peer_uid"),
                env=spec.get("env"),
                env_vars=spec.get("env_vars") or {},
                timeout_sec=int(spec.get("timeout_sec", DEFAULT_TIMEOUT_SEC)),
                log_file=spec.get("log_file"),
            )
        except (OSError, ValueError, TypeError, KeyError) as e:
            broken += 1
            if logger:
                logger.error(f"QUEUE-RESTORE unreadable spec {p.name}, not requeued: {e}")
            try:
                p.rename(p.with_suffix(".invalid"))
            except OSError:
                pass
            continue
        if job.id in jobs:
            continue  # already known (a re-adopted run) — the scope owns it, not the spec
        fault_reason = _device_fault_failed_reason(job.id)
        if fault_reason:
            # This job was SIGKILLed by device recovery (a wedge it caused). Re-running the exact
            # job that just took the device down is how a boot loop starts — drop it: forget its
            # spec + record, do not re-enqueue.
            _forget_queued_job(job.id)
            _clear_device_fault_failed(job.id)
            dropped += 1
            if logger:
                logger.warning(f"QUEUE-RESTORE dropping job_id={job.id} (killed by device recovery): {fault_reason}")
            health_event("queue_restore_skipped_device_fault", job_id=job.id, reason=fault_reason)
            continue
        jobs[job.id] = job
        await get_job_queue().put(job.id)
        _update_queue_depth_metric()
        restored += 1
    if (restored or broken or dropped) and logger:
        logger.info(
            f"QUEUE-RESTORE requeued {restored} job(s) that outlived the last broker"
            + (f"; {broken} spec(s) unreadable and set aside" if broken else "")
            + (f"; {dropped} job(s) dropped (killed by device recovery)" if dropped else "")
        )
    if restored or broken or dropped:
        health_event("queue_restored", restored=restored, unreadable=broken, dropped=dropped)


async def _record_startup_health() -> None:
    """Record what the mesh looked like the moment this broker came up.

    A reboot row says the box went down and came back; it says nothing about WHAT came back. A
    host that returns short a tray, or with its ARC frozen, was indistinguishable in the jobs
    list from a clean one — the gap that let a 24/32 box run for hours calling itself healthy.
    The probe itself is a sysfs read that touches no device, so it is safe beside a job
    re-adopted from the previous instance — but it runs as a DEVICE OP, because what it must
    never run beside is a reset cycling the bus: a hold episode that outlived the restart can
    have the deadline watchdog force the ladder within the first sample, and a probe racing
    that reset reads live chips as fallen mid-probe — a false failed-startup row, a dirty mark
    with a garbage reason, and a post-reboot verify handed an off-bus count that could route a
    healthy boot to the cold rung. The op makes them mutually exclusive either way the race
    goes; a still-cycling reset scope from the PREVIOUS process is waited out the same way.
    """
    try:
        async with _device_op("startup-health"):
            # Resets use a restart-safe systemd scope or local lock, so the previous process may
            # still have one cycling the bus right now.
            await recovery_mechanism.await_foreign_scope(lambda m: logger and logger.info(f"STARTUP HEALTH: {m}"))
            beats = await asyncio.to_thread(read_heartbeats)
            expected = health_monitor.expected(len(beats))
            verdict, detail, _ev = await asyncio.to_thread(heartbeat_verdict, expected)
            ok = verdict is Verdict.HEALTHY
            health_event("startup_health", healthy=ok, present=len(beats), expected=expected, detail=detail)
            write_action_log(
                "[broker]startup",
                f"device on broker start: {detail}",
                0.0,
                "completed" if ok else "failed",
                0 if ok else 1,
            )
            # A degraded device must not be handed a clean slate by the restart that found it. The
            # durable fsm record survives a broker restart, but not a degradation that happened while
            # nothing was running to record it (the box sat degraded across the whole downtime). This
            # live sysfs read catches that case too, so the next tenant is gated on what is actually
            # there rather than what the FSM last happened to say.
            if not ok:
                _mark_device_dirty(f"broker start: {detail}", why="heartbeat")
                # If this boot is a warm reboot the broker fired and the mesh did not come back, the
                # reboot is spent (it cannot re-enumerate a whole-bus wedge) — climb to the cold rung
                # now rather than re-holding it dead, the failure that lost blx02 for ~18h.
                off_bus = len(dead_chips(beats)) + max(0, expected - len(beats))

                def _log(msg: str) -> None:
                    if logger:
                        logger.warning(f"POST-REBOOT {msg}")

                await galaxy_recovery._verify_post_reboot_recovery(off_bus, expected, _log)
            if logger:
                (logger.info if ok else logger.error)(f"STARTUP HEALTH: {'OK' if ok else 'DEGRADED'} — {detail}")
    except Exception as e:  # noqa: BLE001 - a report must never block the broker coming up
        if logger:
            logger.error(f"STARTUP HEALTH check failed: {e}")


def _close_orphaned_hold() -> None:
    """Give a row to a hold the previous broker died holding.

    The episode is in-memory, so a broker that restarts while the device is held forgets it
    and the hold never gets its row: the window the device sat refused to everyone vanishes
    from the ledger precisely when someone is asking what happened to their box. The journal
    is durable and fsync'd per record, so it still knows — an unmatched device_held is an
    episode nobody closed. Writing the released event back closes it, so this stays put after
    the row exists rather than re-reporting the same hold on every start.

    If the device is still degraded, the startup probe opens a fresh episode; this only
    settles the one that outlived its broker.
    """
    try:
        events = read_health_events(kinds={"device_held", "device_released"})
    except Exception:  # noqa: BLE001 - a missing journal must not block startup
        return
    if not events or events[-1].get("kind") != "device_held":
        return  # nothing open
    held = events[-1]
    try:
        held_for = max(0.0, time.time() - float(held.get("ts") or 0))
    except (TypeError, ValueError):
        held_for = 0.0
    reason = held.get("reason") or "reason not recorded"
    # Stamped at the END like the normal release row: write_action_log would reconstruct a start
    # from the runtime and file this row at the hold's beginning, where RECENT sorts it above the
    # whole recovery it closes.
    _write_action_log_file(
        next_job_id(),
        "[broker]hold",
        f"device HELD, refused to tenants: {reason} " f"(episode ended by a broker restart)",
        datetime.now(),
        "ended",
        0,
        held_for,
    )
    health_event("device_released", why="closed on broker start")
    if logger:
        logger.info(f"STARTUP closed an orphaned hold of {int(held_for)}s: {reason}")


async def _verify_fabric_on_start() -> None:
    """Prove the mesh moves data, not merely that every chip answers.

    The startup probe reads sysfs: it costs nothing and catches a chip that is gone or
    frozen, which is why it runs first and reports immediately. It cannot see a wedged
    ethernet link — sysfs stays perfectly healthy while the fabric is unusable — and a
    broker start is exactly when the mesh is least accounted for: whatever the previous
    broker was doing, it did not finish. So the pass is forced rather than left to the
    periodic interval, which exists to spare successful jobs and has no vote here.

    Queued behind any job re-adopted from the previous broker. The pass pushes traffic
    across every inter-chip link, so running it beside a tenant job corrupts their
    measurements and ours both — and the device is theirs until they are done.
    """
    try:
        while readopted_scopes:
            await asyncio.sleep(_SCOPE_POLL_SEC)
        await _device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    except Exception as e:  # noqa: BLE001 - startup must survive a check that cannot run
        # The gate is normally self-contained; if it raised, the startup fabric verdict is unknown and
        # the tenant door is still held pending it (see run_startup_tasks). Leaving it held on a checker
        # that could not run is the indefinite hold; reopening it drains a tenant onto unverified
        # fabric. Do neither — mark dirty, so the first waiting tenant's own gate resets + verifies
        # before it runs, exactly as the post-job gate does when it errors.
        if logger:
            logger.error(f"STARTUP FABRIC check failed: {e}")
        health_event("startup_fabric_verify_errored", detail=str(e), host_at_risk=True)
        _mark_device_dirty(f"startup fabric verify errored before it could verify: {e}", why="gate_error")


async def run_startup_tasks() -> None:
    """Everything that must happen once, before this broker serves its queue.

    There is exactly one of these because there used to be two. main() and the lifespan each
    carried a copy of the sequence, and main() starts the job runner before any client can
    connect — so the lifespan's copy sat behind an `if` that never fired, and anything added
    only there (a queue restore, a startup health report) was dead on arrival while looking
    live in the diff. Whoever reaches startup first runs it; the rest is a no-op.

    Re-adoption must precede the runner: it must see jobs already on the device, or it will
    double-book them. Only the privsep broker re-adopts — it is the sole creator of ttdev-job
    scopes, and a per-user daemon or test server claiming them would show another user's job
    as its own.
    """
    global _startup_tasks_done
    if _startup_tasks_done:
        return
    _startup_tasks_done = True
    if not should_privsep():
        # No re-adoption sequence for a per-user daemon or a test harness, and — matching that
        # same narrower scope — no startup-verify gate either: resolve BOOT straight to HEALTHY
        # so fsm.state is never left at BOOT, which no caller outside this function may observe.
        # The ordinary verified-healthy path, not boot_merge — there is no reboot/re-adoption
        # concern for this deployment shape for boot_merge to reconcile.
        #
        # The rung inventory still prints. It describes what THIS process can execute (spec 04
        # I17), which has nothing to do with re-adoption, and this is the shape most likely to be
        # missing rungs — so it is the shape that most needs them named.
        log_rung_inventory()
        fsm.on_readings(_healthy_reading())
        return
    try:
        await reconcile_running_scopes()
    except Exception as e:  # never let re-adoption block startup
        if logger:
            logger.error(f"RE-ADOPT reconcile failed: {e}")
    # Jobs still WAITING when the last broker died: re-adoption above covers what was
    # running, this covers what was not. Awaited, not fired off — the runner must not race
    # an empty queue and idle the device while restored work sits unqueued.
    try:
        await _restore_queued_jobs()
    except Exception as e:  # never let a restore bug block startup
        if logger:
            logger.error(f"QUEUE-RESTORE failed: {e}")
    # Time the passive eth read on THIS box and arm it, or state which rung is off. Runs before the
    # startup fabric pass below, so the process's very first gate already knows what it has.
    try:
        await selftest_eth_heartbeat()
    except Exception as e:  # noqa: BLE001 - a self-test must never block startup
        if logger:
            logger.error(f"RUNG SELFTEST eth-heartbeat failed: {e}")
    log_rung_inventory()
    # A hold the last broker died holding has no row yet; give it one before anything opens
    # a new episode over the top of it.
    try:
        await asyncio.to_thread(_close_orphaned_hold)
    except Exception as e:  # noqa: BLE001 - bookkeeping must never block startup
        if logger:
            logger.error(f"STARTUP orphaned-hold close failed: {e}")
    # Reconcile BOOT into a real verdict before the job runner is created, so no queued job can
    # race in ahead of it — the report/pass below are async and would lose that race. boot_merge
    # always closes (RECOVERING(startup_unverified)) absent an episode still open from before this
    # restart: only the forced startup gate below, with a real probe pass behind it, may open the
    # door — a live sysfs check on its own is never enough (it reads perfectly healthy across a
    # wedged ethernet link).
    open_episode = fsm.record if fsm.record.state in (ServerState.RECOVERING, ServerState.DOWN) else None
    fsm.boot_merge(
        open_episode=open_episode, attributed_boot=recovery_mechanism.boot_from_broker_escalation(_current_boot_id())
    )
    # The per-boot "door held pending the startup verify" row, durable in the health journal —
    # fsm.json records the episode but is not the timeline operators read. A carried-over episode
    # already has its own rows and keeps its own why, so only the fresh startup hold gets one.
    if fsm.state is ServerState.RECOVERING and fsm.record.why == "startup_unverified":
        health_event("startup_fabric_hold", why="broker start: awaiting the startup fabric verify")
    # Answers "did it come back?" next to the reboot row that says it went away.
    # Fire-and-forget: a health REPORT must never gate the queue coming up.
    asyncio.create_task(_record_startup_health())
    # And then answers it properly, once the device is ours to test — this lifts the hold above on a
    # healthy verdict, holds/escalates on a wedge.
    asyncio.create_task(_verify_fabric_on_start())


@asynccontextmanager
async def server_lifespan(app: MCPServer):
    """Lifespan context manager - starts job runner on startup.

    Runs once per ASGI app, not per session. ``run_transports`` has normally done the work
    already, so the singleton checks are what keep this a no-op.
    """
    global job_runner_task, stats_task

    # Initialize async primitives (requires running event loop)
    _ensure_async_primitives()

    # Only start job runner if not already running (singleton pattern)
    if job_runner_task is None or job_runner_task.done():
        await run_startup_tasks()
        job_runner_task = asyncio.create_task(job_runner())
        if logger:
            logger.info("Server lifespan started, job runner initialized")
    else:
        if logger:
            logger.info("Server lifespan started, job runner already running (reusing)")

    # Start stats persistence task if stats are configured
    if stats and (stats_task is None or stats_task.done()):
        stats_task = asyncio.create_task(stats_persistence_loop())
        if logger:
            logger.info("Stats persistence task started")

    try:
        yield {}
    finally:
        # Only cancel if this context started it AND no other sessions exist
        # For simplicity, we never cancel - the runner lives for the server lifetime
        if logger:
            logger.info("Server lifespan ended (job runner kept alive)")


def create_mcp_server() -> MCPServer:
    """Create and configure the MCP server.

    Endpoint path and statefulness are ``streamable_http_app()`` arguments, in
    build_asgi_app. The listen address is uvicorn's, in run_transports.
    """

    mcp = MCPServer(
        name="tt-device-mcp",
        instructions="MCP server for managing shared Tenstorrent device access across multiple AI agents.",
        lifespan=server_lifespan,
    )

    # ============== Health Check Route ==============

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(_request: Request) -> JSONResponse:
        """Health check endpoint."""
        return JSONResponse(_health_payload())

    # ============== REST API for CLI ==============

    @mcp.custom_route("/api/tt_device_job_run_bg", methods=["POST"])
    async def api_job_run_bg(request: Request) -> JSONResponse:
        """REST API: Queue a job (non-blocking)."""
        data = await request.json()
        result = await _queue_job(
            workspace=data.get("workspace", ""),
            command=data.get("command", ""),
            env=data.get("env"),
            inherited_env=data.get("inherited_env"),
            timeout_sec=data.get("timeout_sec", DEFAULT_TIMEOUT_SEC),
        )
        return JSONResponse(result)

    @mcp.custom_route("/api/tt_device_job_status", methods=["POST"])
    async def api_job_status(request: Request) -> JSONResponse:
        """REST API: Get job status."""
        data = await request.json()
        return JSONResponse(_get_job_status(data.get("job_id")))

    @mcp.custom_route("/api/tt_device_job_logs", methods=["POST"])
    async def api_job_logs(request: Request) -> JSONResponse:
        """REST API: Get job logs."""
        data = await request.json()
        return JSONResponse(_get_job_logs(data.get("job_id"), data.get("tail", 100)))

    @mcp.custom_route("/api/tt_device_job_kill", methods=["POST"])
    async def api_job_kill(request: Request) -> JSONResponse:
        """REST API: Kill/cancel a job."""
        data = await request.json()
        # authz_owner, not the body: over the socket the peer uid wins and the field is
        # ignored, so no caller can name a user other than themselves. Over HTTP there is no
        # peer identity, and the reported value is the documented legacy fallback (spec 05).
        result = await _kill_job(data.get("job_id"), authz_owner(data.get("owner"))[0])
        return JSONResponse(result)

    @mcp.custom_route("/api/tt_device_queue_status", methods=["POST"])
    async def api_queue_status(request: Request) -> JSONResponse:
        """REST API: Get queue status."""
        return JSONResponse(_get_queue_status())

    @mcp.custom_route("/api/tt_device_reset", methods=["POST"])
    async def api_reset(request: Request) -> JSONResponse:
        """REST API: Reset the device(s). Honors the foreign-uid reset gate."""
        data = await request.json()
        return JSONResponse(await _reset_device(bool(data.get("force", False))))

    @mcp.custom_route("/api/tt_device_pre_step", methods=["POST"])
    async def api_pre_step(_request: Request) -> JSONResponse:
        """REST API: the read-only health pass an external scheduler runs before a job."""
        return JSONResponse(await _pre_step_impl())

    @mcp.custom_route("/api/tt_device_post_step", methods=["POST"])
    async def api_post_step(request: Request) -> JSONResponse:
        """REST API: reclaim stragglers, then the recovering health pass, after a job."""
        try:
            data = await request.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        # phase is never read from the body: it lands in the durable journal and the metric
        # labels, and 03 I5 keeps that vocabulary closed.
        return JSONResponse(await _post_step_impl(data))

    async def _reset_stream(force: bool):
        """Yield reset progress line-by-line as it happens, including tt-smi output
        live, so the CLI can print each step as it arrives. Final line is
        '::status::<reset_complete|reset_failed|refused|no_devices>'."""
        global current_process

        caller_uid = current_peer_uid.get()
        scan = enumerate_device_holders()
        yield (
            f"scanned device holders: {len(scan.holders)} process(es) on /dev/tenstorrent"
            + ("" if scan.complete else " (scan incomplete)")
            + "\n"
        )

        # Over the socket the caller's real uid scopes the gate. Over HTTP there is no peer
        # identity: on a privsep host an anonymous caller owns no holder, so every tenant is
        # foreign and we fail closed rather than reset over another tenant's run. Off privsep,
        # HTTP keeps the legacy single-tenant skip.
        if caller_uid is not None or privsep_enabled():
            decision = evaluate_reset_gate(caller_uid, scan, force=force)
            if not decision.allowed:
                foreign = [f"{h.username}(pid {h.pid})" for h in decision.foreign_holders]
                yield f"gate REFUSED: {decision.reason}; held by {', '.join(foreign)}\n"
                yield "hint: ask the holder to stop, or pass --force\n"
                yield "::status::refused\n"
                return
            scope = "caller-scoped" if caller_uid is not None else "anonymous (no peer identity)"
            yield f"gate allowed [{scope}]: {decision.reason}\n"
        else:
            yield "gate skipped: no peer identity (HTTP, privsep off); not caller-scoped\n"

        pid_to_stop = None
        job_id_to_stop = None
        async with get_lock():
            if current_process:
                pid_to_stop = current_process.pid
                job_id_to_stop = current_job_id
        if pid_to_stop:
            # A holder that is SIGKILLed never releases the chip, which guarantees
            # the reset we are about to perform. Interrupting it can release the
            # mesh cleanly instead, and can never make the reset worse.
            yield f"stopping currently running job (pid={pid_to_stop}) before reset\n"
            # Mark the victim BEFORE we stop it: its imminent exit must read as reset-caused, not as
            # a wedge that flags the device for another reset (the loop this guard exists to break).
            _note_reset_killed_job()
            # Scope-route a privsep job: killpg on the broker-held pid hits the wrapper,
            # not the payload, and would reset over a job still holding the mesh.
            if job_id_to_stop:
                await _terminate_job(job_id_to_stop, pid_to_stop)
            else:
                await _terminate_process_group(pid_to_stop)
        async with get_lock():
            if current_process:
                await current_process.wait()
                current_process = None
            else:
                yield "no broker job running; nothing to kill\n"

        indices = _present_chip_indices()
        if not indices:
            yield "no /dev/tenstorrent devices found; nothing to reset\n"
            yield "::status::no_devices\n"
            return

        recovery = select_recovery()
        argv = recovery.reset_argv(indices)
        yield f"$ {' '.join(argv)}   ({len(indices)} device(s); ~30-60s)\n"
        reset_owner = _reset_action_owner()
        _plog = (lambda m: logger.info(m)) if logger else (lambda m: None)
        output_queue: asyncio.Queue[str] = asyncio.Queue()

        async def run_reset():
            async with _device_op("reset", owner=reset_owner):
                health_event("reset_begin", argv=argv, expected_chips=len(indices))
                return await recovery_mechanism.reset_with_quiesce(
                    argv,
                    _plog,
                    reset_owner,
                    output_queue.put_nowait,
                )

        reset_task = asyncio.create_task(run_reset())
        streamed = False
        while not reset_task.done():
            try:
                chunk = await asyncio.wait_for(output_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            streamed = True
            yield chunk
        rc, output = await reset_task
        while not output_queue.empty():
            streamed = True
            yield output_queue.get_nowait()
        if output and not streamed:
            yield output if output.endswith("\n") else output + "\n"
        yield f"exit code: {rc}\n"
        status = "reset_complete" if rc == 0 else "reset_failed"
        if rc == 0:
            # Exit 0 says tt-smi ran, not that the mesh came back, and an unverified clear
            # does not release the device — nothing proved it fit. Leaving that for someone
            # else to answer parked the operator's freshly-reset box behind a hold until the
            # next gate happened along. The chips are back or they are not, and it takes a
            # second to find out, so find out.
            ok, _ev = await fsm.observe(len(indices), lambda m: None, run_fabric=False, recovery=recovery)
            detail = (_ev.get("snapshot") or {}).get("detail") or (_ev.get("heartbeat") or {}).get("detail", "")
            yield f"health: {'OK' if ok else 'UNHEALTHY'} — {detail}\n"
            if ok:
                _clear_device_reported_fault("reset stream: verified healthy")
                _clear_device_dirty(verified=True, why="operator reset stream: verified healthy")
            else:
                status = "reset_unhealthy"  # the reset ran; the mesh is not back
        yield f"::status::{status}\n"

    async def _with_keepalive(lines):
        """Pass ``lines`` through, adding a keepalive line after every quiet
        RESET_STREAM_KEEPALIVE_SEC. The pending step is never cancelled on a quiet
        interval: we wait on the same task again, so the reset is not disturbed."""
        nxt = None
        try:
            while True:
                if nxt is None:
                    nxt = asyncio.ensure_future(lines.__anext__())
                done, _ = await asyncio.wait({nxt}, timeout=RESET_STREAM_KEEPALIVE_SEC)
                if not done:
                    yield RESET_STREAM_KEEPALIVE_LINE + "\n"
                    continue
                try:
                    line = nxt.result()
                except StopAsyncIteration:
                    return
                nxt = None
                yield line
        finally:
            if nxt is not None and not nxt.done():
                nxt.cancel()
                await asyncio.gather(nxt, return_exceptions=True)  # the step must stop before aclose
            await lines.aclose()

    @mcp.custom_route("/api/tt_device_reset_stream", methods=["POST"])
    async def api_reset_stream(request: Request) -> StreamingResponse:
        """REST API: streaming reset — emits progress lines as they happen. Keepalive
        lines are opt-in: an older CLI would print the sentinel verbatim."""
        data = await request.json()
        lines = _reset_stream(bool(data.get("force", False)))
        if data.get("keepalive"):
            lines = _with_keepalive(lines)
        return StreamingResponse(lines, media_type="text/plain")

    async def _smi_stream(args, cols, rows):
        """Run tt-smi live (read-only) over a pty and stream its bytes. Unlike a
        queued job or exec, this is NOT serialized and is allowed WHILE another
        user's job runs — telemetry is read-only and parallel-safe (the telemetry
        server reads it continuously too). Read-only is enforced by an allowlist;
        no keystrokes are forwarded, so an in-dashboard reset can't be triggered."""
        import fcntl
        import pty
        import struct
        import termios

        if not smi_args_ok(args):
            yield b"smi: only read-only tt-smi is allowed here (no reset/config flags).\r\n"
            return
        caller_uid = current_peer_uid.get()
        owner = username_for_uid(caller_uid) if caller_uid is not None else "unknown"
        argv = ["tt-smi", *args]  # run as the broker (root opens the locked node; read-only)
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows or 24, cols or 80, 0, 0))
        except OSError:
            pass
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={**os.environ, "TERM": os.environ.get("TERM", "xterm-256color"), "PYTHONUNBUFFERED": "1"},
        )
        os.close(slave)
        if logger:
            logger.info(f"SMI: {owner} running 'tt-smi {' '.join(args)}' (parallel, read-only, pid={proc.pid})")
        _t0 = datetime.now()
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, _pty_read, master)
                if not data:
                    break
                yield data
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                os.close(master)
            except OSError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
            write_action_log(owner, "tt-smi " + " ".join(args), (datetime.now() - _t0).total_seconds(), "completed", 0)

    @mcp.custom_route("/api/tt_device_smi_stream", methods=["POST"])
    async def api_smi_stream(request: Request) -> StreamingResponse:
        """REST API: stream a live, read-only tt-smi (runs in parallel with jobs)."""
        data = await request.json()
        return StreamingResponse(
            _smi_stream(list(data.get("args", [])), int(data.get("cols", 80)), int(data.get("rows", 24))),
            media_type="application/octet-stream",
        )

    @mcp.custom_route("/api/tt_device_recent_jobs", methods=["POST"])
    async def api_recent_jobs(request: Request) -> JSONResponse:
        """REST API: the last N jobs (owner, command, runtime, status, ...)."""
        data = await _json_body(request)
        return JSONResponse({"jobs": _recent_jobs(int(data.get("limit", 20)))})

    # ============== MCP Tools ==============

    # ============== Shared Helpers ==============

    def _get_job_status(job_id: str) -> dict:
        """Get job status (shared by REST API and MCP tool)."""
        if job_id not in jobs:
            for r in _recent_jobs(60):
                if r.get("job_id") == job_id:
                    res = {
                        "job_id": job_id,
                        "owner": r.get("owner", "?"),
                        "status": r.get("status", "?"),
                        "command": r.get("command", "?"),
                        "exit_code": r.get("exit_code"),
                        "log_file": r.get("log_file"),
                    }
                    if r.get("runtime"):
                        try:
                            res["runtime_sec"] = float(str(r["runtime"]).rstrip("s"))
                        except ValueError:
                            pass
                    if r.get("cause"):
                        res["cause"] = r["cause"]
                    return res
            return {"error": "Job not found"}

        job = jobs[job_id]
        result = {
            "job_id": job_id,
            "owner": job.owner,
            "status": job.status.value,
            "command": job.command,
            "exit_code": job.exit_code,
            "log_file": job.log_file,
        }
        if job.error:
            result["cause"] = job.error

        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            if job.runtime_sec is not None:
                result["runtime_sec"] = job.runtime_sec
            if job.wait_sec is not None:
                result["wait_sec"] = job.wait_sec
            if job.status == JobStatus.TIMEOUT:
                result["timeout_sec"] = job.timeout_sec
                result["hint"] = timeout_hint(job.timeout_sec)

        return result

    def _get_job_logs(job_id: str, tail: int = 100) -> dict:
        """Get job logs (shared by REST API and MCP tool)."""
        if job_id not in jobs:
            return {"error": "Job not found"}

        job = jobs[job_id]
        content = ""
        if job.log_file and os.path.exists(job.log_file):
            with open(job.log_file, "r") as f:
                lines = f.readlines()
                content = "".join(lines[-tail:])

        return {
            "job_id": job_id,
            "log_file": job.log_file,
            "content": content,
        }

    async def _kill_job(job_id: str, owner: str) -> dict:
        """Kill/cancel a job (shared by REST API and MCP tool).

        On the unix socket the caller is authenticated by peer uid and the
        reported ``owner`` is ignored for authz; over HTTP the reported owner is
        used (legacy).
        """
        global current_process

        if job_id not in jobs:
            return {"error": "Job not found"}

        job = jobs[job_id]

        caller_owner, _ = authz_owner(owner)
        if not owner_matches(job.owner, caller_owner):
            return {"error": f"Permission denied: job belongs to {job.owner}"}

        terminate = None  # (job_id, pid) to reap OUTSIDE the lock, or None
        async with get_lock():
            if job.status == JobStatus.RUNNING:
                # A live privsep job and a re-adopted one both run in a systemd scope,
                # and killpg on the broker-held pid hits the systemd-run wrapper, not the
                # reparented payload -- it wedges the eth mid-CCL. _terminate_job signals
                # the scope when one is live (SIGINT first, so ttnn closes the mesh) and
                # falls back to the process group only for an unscoped job.
                readopted_scopes.pop(job_id, None)
                terminate = (job_id, job.pid)
                job.status = JobStatus.KILLED
                job.finished_at = datetime.now().isoformat()
                action = "killed"
            elif job.status == JobStatus.QUEUED:
                job.status = JobStatus.KILLED
                job.finished_at = datetime.now().isoformat()
                action = "cancelled"
                # A cancelled queued job will never run: drop its on-disk spec now, or cleanup
                # evicts it from `jobs` while the spec lingers and a restart re-queues the corpse.
                _forget_queued_job(job_id)
                _update_queue_depth_metric()
            else:
                return {"error": f"Job not running or queued (status: {job.status.value})"}

        # Who asked, and for which job. A kill mid-CCL wedges the mesh and the recovery lands
        # on whoever queues next, so "the broker killed it" must never be the whole record:
        # without the requester this reads as the broker acting on its own.
        if logger:
            logger.info(f"KILL job_id={job_id} ({action}) requested by {caller_owner} " f"— job owner {job.owner}")
        health_event("job_killed", job_id=job_id, action=action, requested_by=caller_owner, job_owner=job.owner)

        if terminate is not None:
            # Outside the lock so the job can release the device before SIGKILL.
            await _terminate_job(*terminate)

        return {"status": action, "job_id": job_id}

    # ============== Helper for job queueing ==============
    async def _queue_job(
        workspace: str,
        command: str,
        env: str = None,
        inherited_env: dict[str, str] = None,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    ) -> dict:
        """Internal helper to queue a job."""
        global job_counter

        identity_error = privsep_identity_error()
        if identity_error:
            return identity_error

        # The hard ceiling is enforced HERE, not only on the MCP tool's schema: every path —
        # the tool, the REST route the CLI and tt-run use — funnels through this one function,
        # and the REST route passes timeout_sec through raw. Clamping only at the tool layer
        # left the ceiling unenforced for the paths people actually submit through.
        timeout_sec = max(1, min(int(timeout_sec), MAX_TIMEOUT_SEC))

        # Always derived, never taken from the caller. Over the socket the peer uid names the
        # submitter and the request surface decides the tag; off it there is no identity to read
        # and the label honestly says so.
        owner = submitting_owner()

        # Per-owner burst cap, enforced HERE — the single funnel every submit path shares — so a
        # burst is refused before it fills the queue, not throttled at dispatch once the run is
        # already committed to the mesh. Check-and-record is atomic: there is no await between the
        # decision and storing the timestamp back, so two concurrent submits from one owner cannot
        # both slip past a full window. The attempt is what counts (recorded even if the submission
        # later fails env validation) — the limit is submission pressure, and a flood of malformed
        # submits is pressure too. Default 0 (off): the outer guard keeps a disabled host from
        # accumulating any per-owner state.
        if JOB_BURST_MAX > 0:
            now_monotonic = time.monotonic()
            allowed, retry_after, kept = _job_burst_decision(owner_submit_times.get(owner, []), now_monotonic)
            if not allowed:
                owner_submit_times[owner] = kept  # keep the pruned window, never unbounded
                health_event(
                    "job_burst_refused",
                    owner=owner,
                    recent=len(kept),
                    cap=JOB_BURST_MAX,
                    window_sec=int(JOB_BURST_WINDOW_SEC),
                    retry_after_sec=round(retry_after, 1),
                )
                if logger:
                    logger.warning(
                        f"  -> BURST REFUSED: owner={owner} has {len(kept)} submission(s) in "
                        f"{int(JOB_BURST_WINDOW_SEC)}s (cap {JOB_BURST_MAX}); retry in ~{retry_after:.0f}s"
                    )
                return {
                    "error": (
                        f"burst cap reached: {owner} submitted {len(kept)} job(s) in the "
                        f"last {int(JOB_BURST_WINDOW_SEC)}s (cap {JOB_BURST_MAX}); this "
                        f"submission is refused"
                    ),
                    "hint": (
                        f"wait ~{int(retry_after) + 1}s for a slot to free, or pace "
                        f"submissions further apart to let the mesh rest between runs"
                    ),
                    "retry_after_sec": round(retry_after, 1),
                }
            owner_submit_times[owner] = kept + [now_monotonic]

        workspace = os.path.expanduser(workspace)

        # Resolve and validate env vars (priority: env_file > inherited_env > defaults).
        # Every documented load_env_file failure is client input (missing file, unparseable
        # YAML, non-dict content) and must come back as a structured refusal — any one
        # escaping gives the submitter a 500 traceback instead of the reason.
        try:
            _, resolved_env_vars = get_activation_script(workspace, env, inherited_env, validate=True)
        except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
            if logger:
                logger.warning(f"  -> ERROR: {e}")
            return {"error": str(e)}

        now = datetime.now()
        queued_at = now.isoformat()

        # Scopes are ground truth for jobs that outlived a broker restart, so they
        # must be excluded from the id pool. Scan off the event loop (systemctl).
        live_scopes = frozenset(await asyncio.to_thread(list_active_job_scopes)) if should_privsep() else frozenset()
        async with get_lock():
            job_id = next_job_id(live_scopes)

        # Create log file immediately so it can be tailed
        job_log_file = None
        if job_log_dir:
            timestamp = now.strftime("%Y-%m-%d_%H%M%S")
            job_log_file = str(job_log_dir / f"{timestamp}_{job_id}.log")
            # Write initial header with explicit env vars
            with open(job_log_file, "w") as f:
                f.write("=" * 70 + "\n")
                f.write(f"JOB ID:      {job_id}\n")
                f.write(f"OWNER:       {owner}\n")
                f.write(f"WORKSPACE:   {workspace}\n")
                f.write(f"COMMAND:     {command}\n")
                f.write(f"ENV FILE:    {env or '(none)'}\n")
                f.write(f"TIMEOUT:     {timeout_sec}s\n")
                f.write(f"QUEUED:      {queued_at}\n")
                f.write("-" * 70 + "\n")
                f.write("ENVIRONMENT VARIABLES:\n")
                for key, value in sorted(resolved_env_vars.items()):
                    f.write(f"  {key}={value}\n")
                f.write("=" * 70 + "\n")
                f.write("\n[Waiting to start...]\n\n")

        job = Job(
            id=job_id,
            owner=owner,
            workspace=workspace,
            command=command,
            queued_at=queued_at,
            peer_uid=current_peer_uid.get(),
            env=env,
            env_vars=resolved_env_vars,
            timeout_sec=timeout_sec,
            log_file=job_log_file,
        )
        jobs[job_id] = job
        _persist_queued_job(job)
        await get_job_queue().put(job_id)
        _update_queue_depth_metric()

        # Ensure job runner is started (may be first job via REST API before MCP session)
        global job_runner_task
        if job_runner_task is None or job_runner_task.done():
            job_runner_task = asyncio.create_task(job_runner())
            if logger:
                logger.info("JOB_RUNNER started (triggered by first job queue)")

        # Determine if job will run immediately or be queued
        running_jobs = [j for j in jobs.values() if j.status == JobStatus.RUNNING]
        queue_size = get_job_queue().qsize()
        # A broker op holding the device, or a device flagged dirty, blocks this job just
        # as surely as another tenant does — the gate makes it wait for the reset+verify.
        # Reporting "starting (device idle)" through that window told a submitter their job
        # was running while it sat behind a twelve-minute reset, and left a log line that
        # reads exactly like a job dispatched onto a device the broker had marked dirty.
        blocked = _device_unavailable_for_tenant()

        if running_jobs:
            # Device busy - job is queued
            queue_position = queue_size  # Position in queue (1 = next after current)
            status_msg = f"queued (position {queue_position} - waiting for {running_jobs[0].id})"
        elif blocked:
            queue_position = queue_size
            status_msg = f"queued (waiting for the device: {blocked})"
        else:
            # Device idle - job will start immediately
            queue_position = 0
            status_msg = "starting (device idle)"

        if logger:
            logger.info(f"  -> {status_msg.upper()}: job_id={job_id}, owner={owner}, log={job_log_file}")

        return {
            "job_id": job_id,
            "status": "queued" if (running_jobs or blocked) else "starting",
            "message": status_msg,
            "position": queue_position,
            "log_file": job_log_file,
            "owner": owner,
            "env_vars": resolved_env_vars,
        }

    @mcp.tool(
        name="tt_device_job_run_bg",
        annotations={
            "title": "Queue Job (Non-blocking)",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def job_run_bg(params: JobSubmitInput) -> dict:
        """Queue a job to run on the Tenstorrent device (non-blocking).

        Returns immediately after queueing. Use tt_device_job_status or tt_device_job_wait to track progress.

        IMPORTANT: Always note the log_file path - you can tail it to monitor progress.

        Returns:
            dict: Job submission result:
                - job_id (str): Unique identifier, 3 digits (e.g., '042')
                - status (str): 'starting' (device idle, will run now) or 'queued' (waiting)
                - message (str): Human-readable status (e.g., 'queued (position 2 - waiting for 143051-1)')
                - position (int): 0 if starting, 1+ if queued (position in queue)
                - log_file (str): Absolute path to log file - TAIL THIS TO MONITOR
                - owner (str): Job owner
                - env_vars (dict): Resolved environment variables

            On error: {"error": str, "hint": str}
        """
        if logger:
            logger.info(
                f"TOOL job_run_bg: owner={submitting_owner()}, workspace={params.workspace}, command={params.command[:80]!r}, env={params.env}, timeout={params.timeout_sec}"
            )

        return await _queue_job(
            params.workspace,
            params.command,
            params.env,
            params.inherited_env,
            params.timeout_sec,
        )

    @mcp.tool(
        name="tt_device_job_run",
        annotations={
            "title": "Run Job (Blocking)",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def job_run(params: JobRunInput, ctx: Context) -> dict:
        """Queue a job and wait for completion (blocking).

        Queues the job and waits, streaming logs in real-time via progress updates.
        This is the recommended tool for running tests - it handles the full lifecycle.

        Shows queue status on submission (starting immediately vs queued position).
        IMPORTANT: The log_file path is always provided - useful for debugging.

        Returns:
            dict: Final job result:
                - job_id (str): Job identifier
                - status (str): Final status - 'completed', 'failed', 'timeout', or 'killed'
                - exit_code (int): Process exit code (0 = success)
                - log_file (str): Path to full log file - ALWAYS CHECK THIS FOR DETAILS
                - runtime_sec (float): Total execution time
                - output_tail (list[str]): Last N lines of output

            On error: {"error": str, "hint": str}
        """
        if logger:
            logger.info(
                f"TOOL job_run: owner={submitting_owner()}, workspace={params.workspace}, command={params.command[:80]!r}, env={params.env}, timeout={params.timeout_sec}"
            )

        result = await _queue_job(
            params.workspace,
            params.command,
            params.env,
            params.inherited_env,
            params.timeout_sec,
        )
        if "error" in result:
            return result

        job_id = result["job_id"]
        log_file = result.get("log_file", "")

        # Report queue status prominently
        await ctx.info(f"Job {job_id}: {result.get('message', 'submitted')}")
        await ctx.info(f"Log file: {log_file}")

        # Wait for completion with log streaming
        return await _wait_for_job_impl(job_id, ctx, stream_logs=True, output_lines=params.output_lines)

    @mcp.tool(
        name="tt_device_job_status",
        annotations={
            "title": "Get Job Status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def job_status(params: JobIdInput) -> dict:
        """Get status and results of a job.

        Use this to check if a job submitted with tt_device_job_run_bg has completed.

        Returns:
            dict: Job status:
                - job_id (str): Job identifier
                - status (str): Current status - 'queued', 'running', 'completed', 'failed', 'timeout', 'killed'
                - owner (str): Job owner
                - command (str): Command being executed
                - exit_code (int|null): Process exit code (null if still running)
                - runtime_sec (float|null): Execution time (null if not finished)
                - log_file (str): Path to log file
                - output (str): Captured stdout (only for finished jobs)
                - error (str): Captured stderr (only for finished jobs)

            On error: {"error": "Job not found"}
        """
        if logger:
            logger.info(f"TOOL job_status: job_id={params.job_id}")

        result = _get_job_status(params.job_id)

        # MCP tool adds output/error fields for finished jobs
        if "error" not in result and params.job_id in jobs:
            job = jobs[params.job_id]
            if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                result["output"] = job.output
                result["error"] = job.error

            if logger:
                logger.info(f"  -> status={job.status.value}, exit_code={job.exit_code}")
        elif logger:
            logger.warning("  -> ERROR: Job not found")

        return result

    # ============== Helper for waiting on jobs ==============
    async def _wait_for_job_impl(
        job_id: str,
        ctx: Context,
        stream_logs: bool = True,
        output_lines: int = 20,
    ) -> dict:
        """Internal helper to wait for a job to complete.

        Args:
            job_id: Job ID to wait for
            ctx: MCP context for streaming
            stream_logs: If True, stream log output as it happens
            output_lines: Number of lines to include in final result (default: 20)
        """
        if job_id not in jobs:
            if logger:
                logger.warning("  -> ERROR: Job not found")
            return {"error": "Job not found"}

        job = jobs[job_id]
        poll_interval = 2.0  # seconds
        last_log_size = 0
        recent: deque = deque(maxlen=8)  # tail for the progress snippet only

        # Show log file location first
        if job.log_file:
            await ctx.info(f"Log file: {job.log_file}")

        await ctx.info(f"Waiting for job {job_id}...")

        while job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            # Read new log content off the event loop (a chatty job's log can be
            # large) and keep only a bounded tail in memory.
            if stream_logs and job.log_file:
                new_content, last_log_size = await asyncio.to_thread(_read_new, job.log_file, last_log_size)
                if new_content:
                    new_lines = new_content.rstrip().split("\n")
                    recent.extend(new_lines)
                    for line in new_lines[-5:]:
                        stripped = strip_log_prefix(line.rstrip())
                        if stripped:
                            await ctx.info(stripped)

            # Report progress based on job status
            if job.status == JobStatus.QUEUED:
                # Find queue position
                running_job = next((j for j in jobs.values() if j.status == JobStatus.RUNNING), None)
                queued_jobs = [j for j in jobs.values() if j.status == JobStatus.QUEUED]
                try:
                    position = next(i + 1 for i, j in enumerate(queued_jobs) if j.id == job_id)
                except StopIteration:
                    position = len(queued_jobs)

                if running_job:
                    msg = f"Queued (position {position}, waiting for {running_job.id})"
                else:
                    msg = f"Queued (position {position})"
                if job.log_file:
                    msg += f"\nLog: {job.log_file}"
                # Use 0 progress to indicate queued state
                await ctx.report_progress(0, job.timeout_sec, msg)

            elif job.status == JobStatus.RUNNING and job.started_at:
                elapsed = (datetime.now() - datetime.fromisoformat(job.started_at)).total_seconds()
                recent_lines = []
                for line in list(recent)[-5:]:
                    stripped = strip_log_prefix(line.strip())
                    if stripped:
                        recent_lines.append(stripped)
                log_snippet = "\n".join(recent_lines) if recent_lines else ""
                msg = f"Running ({int(elapsed)}s)"
                if job.log_file:
                    msg += f"\nLog: {job.log_file}"
                if log_snippet:
                    msg += f"\n{log_snippet}"
                await ctx.report_progress(elapsed, job.timeout_sec, msg)

            await asyncio.sleep(poll_interval)

        # Job completed - read the final tail off the event loop.
        final_tail: list[str] = []
        if output_lines > 0 and job.log_file:
            final_tail = await asyncio.to_thread(_tail_lines, job.log_file, output_lines)

        # Send final status with log path
        status_emoji = "✓" if job.status == JobStatus.COMPLETED else "✗"
        await ctx.info(f"{status_emoji} Job {job_id} finished: {job.status.value}")
        if job.log_file:
            await ctx.info(f"Log file: {job.log_file}")

        # Build result with last N lines of output
        result = {
            "job_id": job_id,
            "status": job.status.value,
            "exit_code": job.exit_code,
            "log_file": job.log_file,
        }

        # Include last N lines of output in result
        if final_tail:
            result["output_tail"] = final_tail

        if job.runtime_sec is not None:
            result["runtime_sec"] = job.runtime_sec

        if job.status == JobStatus.TIMEOUT:
            result["timeout_sec"] = job.timeout_sec
            result["hint"] = timeout_hint(job.timeout_sec)
            await ctx.info(result["hint"])

        if logger:
            logger.info(f"  -> Job completed: status={job.status.value}, exit_code={job.exit_code}")

        return result

    @mcp.tool(
        name="tt_device_job_wait",
        annotations={
            "title": "Wait for Job",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def job_wait(params: JobWaitInput, ctx: Context) -> dict:
        """Wait for a job to complete, streaming logs in real-time.

        Use this after tt_device_job_run_bg to wait for completion and get results.
        Streams log output via progress updates while waiting.

        Returns:
            dict: Final job result:
                - job_id (str): Job identifier
                - status (str): Final status - 'completed', 'failed', 'timeout', or 'killed'
                - exit_code (int): Process exit code (0 = success)
                - log_file (str): Path to full log file
                - runtime_sec (float): Total execution time
                - output_tail (list[str]): Last N lines of output

            On error: {"error": "Job not found"}
        """
        if logger:
            logger.info(
                f"TOOL job_wait: job_id={params.job_id}, stream_logs={params.stream_logs}, output_lines={params.output_lines}"
            )

        return await _wait_for_job_impl(params.job_id, ctx, params.stream_logs, params.output_lines)

    @mcp.tool(
        name="tt_device_job_logs",
        annotations={
            "title": "Get Job Logs",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def job_logs(params: JobLogsInput) -> dict:
        """Get logs for a job.

        Retrieves the last N lines from the job's log file. Useful for debugging
        failed jobs or checking output without waiting.

        Returns:
            dict: Log content:
                - job_id (str): Job identifier
                - log_file (str): Absolute path to log file
                - content (str): Last N lines of the log

            On error: {"error": "Job not found"} or {"error": "Log file not found"}
        """
        if logger:
            logger.info(f"TOOL job_logs: job_id={params.job_id}, tail={params.tail}")

        result = _get_job_logs(params.job_id, params.tail)

        if "error" in result and logger:
            logger.warning("  -> ERROR: Job not found")

        return result

    @mcp.tool(
        name="tt_device_job_kill",
        annotations={
            "title": "Kill/Cancel Job",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def job_kill(params: JobKillInput) -> dict:
        """Kill a running job or cancel a queued job.

        Only the job owner can kill/cancel their own jobs. A running job is stopped with
        SIGINT (so it unwinds and RELEASES THE DEVICE), escalating to SIGTERM and then
        SIGKILL only if it refuses to exit. Queued jobs are removed from the queue.

        Returns:
            dict: Kill result:
                - status (str): 'killed' (was running) or 'cancelled' (was queued)
                - job_id (str): Job identifier

            On error:
                - {"error": "Job not found"}
                - {"error": "Permission denied: job owned by <owner>"}
                - {"error": "Job already finished"}
        """
        if logger:
            logger.info(f"TOOL job_kill: job_id={params.job_id}, owner={submitting_owner()}")

        result = await _kill_job(params.job_id, submitting_owner())

        if "error" in result:
            if logger:
                logger.warning(f"  -> ERROR: {result['error']}")
        else:
            if logger:
                logger.info(f"  -> {result['status'].upper()} job_id={params.job_id}, owner={submitting_owner()}")

        return result

    async def _exec_impl(params: DeviceExecInput) -> dict:
        """Run a diagnostic directly on the device, bypassing the queue. Shared by the MCP
        tool and the REST route so both enforce the same gate, force path, and audit."""
        if logger:
            logger.info(f"TOOL device_exec: owner={submitting_owner()}, command={params.command[:80]!r}")

        identity_error = privsep_identity_error()
        if identity_error:
            return identity_error

        # Check if allowed (peer uid is authoritative over the socket)
        caller_owner, _ = authz_owner(None)
        running_jobs = [j for j in jobs.values() if j.status == JobStatus.RUNNING]

        if running_jobs:
            running_job = running_jobs[0]
            if not owner_matches(running_job.owner, caller_owner):
                if not params.force:
                    if logger:
                        logger.warning(f"  -> ERROR: Device busy (owner: {running_job.owner}, caller: {caller_owner})")
                    return {
                        "error": f"Device busy: job {running_job.id} running (owner: {running_job.owner})",
                        "hint": "Wait for the job to finish or ask the owner to kill it. To triage "
                        "their hung job now, re-run with force (the diagnostic runs "
                        "alongside theirs — read-only tools only).",
                    }
                # Forced past a foreign owner: name whose run this lands on, and what it runs.
                if logger:
                    logger.warning(
                        f"  -> FORCED: {caller_owner} exec alongside job {running_job.id} "
                        f"(owner {running_job.owner}): {params.command[:80]!r}"
                    )
                health_event(
                    "exec_forced",
                    caller=caller_owner,
                    over_job=running_job.id,
                    over_owner=running_job.owner,
                    command=params.command[:200],
                )
            elif logger:
                logger.info(f"  -> Allowed: caller owns running job {running_job.id}")
        else:
            # Device is idle - allowed
            if logger:
                logger.info("  -> Allowed: device is idle")

        # "No tenant job is running" is not "the device is free": exec runs its command on
        # the device directly, outside the queue, so without this it is the one path that can
        # land on a mesh mid-galaxy-reset, on one the broker has flagged dirty, or on one a
        # chip has just fallen off (0xFFFFFFFF reads that hang the host CPU issuing them). The
        # last sets no in-memory flag, so exec takes the same live-probe verdict the job runner
        # gates on rather than the op/dirty flags alone. Exec is a short synchronous call — a
        # reset+fabric pass can hold the device for twelve minutes — so it refuses and says why
        # rather than blocking the caller behind it.
        blocked = _device_degraded_for_tenant()
        if blocked:
            if logger:
                logger.warning(f"  -> ERROR: device not free: {blocked}")
            return {
                "error": f"Device busy: {blocked}",
                "hint": "The device is held degraded. Watch tt_device_queue_status "
                "(device_degraded) and retry once it clears.",
            }

        # Fail closed on an unhonorable privsep identity: identity_error above catches the
        # HTTP (no peer) case; this also catches a peer uid with no passwd entry, which
        # privsep_prefix_for cannot scope and would otherwise run directly as the broker (root).
        exec_refusal = privsep_refusal(current_peer_uid.get())
        if exec_refusal:
            if logger:
                logger.warning(f"  -> ERROR: {exec_refusal}")
            return {"error": exec_refusal}

        # Execute command directly (under privsep, as the caller in a device-admitted scope).
        # Name that scope deterministically so a timeout can reap the SCOPE (systemd hits the
        # whole cgroup): killpg on proc.pid would signal only the systemd-run wrapper.
        global exec_scope_seq
        exec_scope_seq += 1
        exec_scope = f"{EXEC_SCOPE_PREFIX}{os.getpid()}-{exec_scope_seq}.scope"
        privsep_prefix = privsep_prefix_for(current_peer_uid.get(), unit=exec_scope)
        _exec_start = datetime.now()
        try:
            # A non-privsep exec gets its own session so a timeout reaps the whole tree by
            # group. A privsep exec runs in its own systemd scope instead — systemd reparents
            # the payload out of our cgroup, so killpg would reach only the systemd-run wrapper;
            # that one is reaped by its scope in the timeout handler below.
            if privsep_prefix:
                proc = await asyncio.create_subprocess_exec(
                    *privsep_prefix,
                    "/bin/bash",
                    "-c",
                    params.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    params.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    executable="/bin/bash",
                    start_new_session=True,
                )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=params.timeout_sec)
            exit_code = proc.returncode

            result = {
                "exit_code": exit_code,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }

            write_action_log(
                caller_owner,
                params.command,
                (datetime.now() - _exec_start).total_seconds(),
                "completed" if exit_code == 0 else "failed",
                exit_code,
            )
            if logger:
                logger.info(f"  -> Completed: exit_code={exit_code}")

            return result

        except asyncio.TimeoutError:
            # Reap the payload, not the wrapper. A privsep exec lives in its own scope, so
            # signal the SCOPE (SIGINT-first, so a hung tt-triage can unwind before it is
            # stopped); killpg here would leave it running on the mesh it was inspecting. A
            # non-privsep exec has no scope — reap its process group.
            if privsep_prefix:
                await _terminate_scope(exec_scope)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            # Record the interruption under its own name — a timed-out triage that shows as
            # "failed" or as nothing in recent history reads as a bug in the command, not the
            # clock that killed it.
            write_action_log(
                caller_owner, params.command, (datetime.now() - _exec_start).total_seconds(), "timeout", None
            )
            if logger:
                logger.warning(f"  -> ERROR: Command timed out ({params.timeout_sec}s)")
            return {"error": f"Command timed out after {params.timeout_sec} seconds"}

        except Exception as e:
            # "error" (could not run — spawn/setup failed), distinct from "failed" (ran, exited
            # non-zero): the two want different follow-ups.
            write_action_log(
                caller_owner, params.command, (datetime.now() - _exec_start).total_seconds(), "error", None
            )
            if logger:
                logger.error(f"  -> ERROR: {e}")
            return {"error": str(e)}

    @mcp.tool(
        name="tt_device_exec",
        annotations={
            "title": "Execute Device Command",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def device_exec(params: DeviceExecInput) -> dict:
        """Execute a command directly on the device (not queued).

        For diagnostic tools like tt-smi and tt-triage on a hung job: runs alongside the
        holder instead of queueing. Access control: allowed when the device is idle or the
        caller owns the running job; a foreign-owned running job is refused unless force.

        Returns dict with exit_code / stdout / stderr, or {"error", "hint"} when refused.
        """
        return await _exec_impl(params)

    @mcp.custom_route("/api/tt_device_exec", methods=["POST"])
    async def api_exec(request: Request) -> JSONResponse:
        """REST API: run a diagnostic directly on the device (bypasses the queue)."""
        data = await request.json()
        params = DeviceExecInput(
            command=data.get("command", ""),
            timeout_sec=max(1, min(int(data.get("timeout_sec", 180)), EXEC_MAX_TIMEOUT_SEC)),
            force=bool(data.get("force", False)),
        )
        return JSONResponse(await _exec_impl(params))

    @mcp.tool(
        name="tt_device_recent_jobs",
        annotations={
            "title": "Recent Jobs",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def recent_jobs(limit: int = 20) -> dict:
        """The last N jobs run through the broker (default 20), newest first, with
        owner, command, runtime, status, and exit code. Reads persisted job logs,
        so it covers finished jobs even after they leave the in-memory queue."""
        return {"jobs": _recent_jobs(limit)}

    @mcp.tool(
        name="tt_device_queue_status",
        annotations={
            "title": "Get Queue Status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def queue_status() -> dict:
        """Get current queue status - running jobs, queued jobs, device busy state.

        Use this to check what's currently running and queued before submitting new jobs.

        Returns:
            dict: Queue status:
                - device_busy (bool): True if a job is currently running
                - running (list): Currently running jobs, each with:
                    - id (str): Job ID
                    - owner (str): Job owner
                    - command (str): Command being run
                    - started_at (str): ISO timestamp
                - queued (list): Waiting jobs in order, each with:
                    - id (str): Job ID
                    - owner (str): Job owner
                    - command (str): Command to run
                    - position (int): Queue position (1 = next)
        """
        if logger:
            logger.info("TOOL queue_status")

        result = _get_queue_status()

        if logger:
            logger.info(
                f"  -> running={len(result['running'])}, queued={len(result['queued'])}, total_jobs={len(jobs)}"
            )

        return result

    def _step_refusal(reason: str) -> dict:
        return {
            "status": "refused",
            "ok": False,
            "reason": reason,
            "fsm_state": fsm.state.value,
            "fsm_why": fsm.record.why,
        }

    def _step_caller_is_root() -> str:
        """'' if the peer may drive a step. Both steps can reset the device and post-step can
        SIGKILL another user's processes; a tenant reaches the device through the queue, `reset`
        and `exec`."""
        uid = current_peer_uid.get()
        if uid == 0:
            return ""
        who = "an unauthenticated caller" if uid is None else f"uid {uid}"
        return f"a job step must be driven by root ({who} is not); a tenant uses run/reset/exec"

    def _broker_work_in_flight() -> str:
        """'' if the device is idle of broker work. The gate runs while the device is IDLE around
        a job; an external pass with a queue job live would probe underneath it.

        Three sources of broker ownership, none of them optional:
          * `device_op_active` — the broker itself is mid-reset or mid-fabric-pass (`_device_op`),
            outside of any job.
          * `current_job_id` — the runner's own ownership window for the currently dispatched
            job. It is set at spawn and cleared only in the runner's finally block, AFTER the
            process is reaped and the log closed — a job that just went HUNG (`server.py` sets
            HUNG well before clearing this) still owns the device for that whole teardown, even
            though its `JobStatus` is no longer RUNNING. Reading `current_job_id` instead of only
            job status is what keeps this predicate from calling the device idle during exactly
            that window.
          * the QUEUED scan — a job not yet dispatched has no `current_job_id` to catch, so it is
            still read from `jobs` directly.

        This is still a snapshot: nothing stops a job from being queued and dispatched the
        instant after this returns clean. The caller re-checks it after any await (the reclaim's
        `to_thread` is the one that matters) and before the gate runs.
        """
        if device_op_active:
            return f"broker device op in flight: {device_op_detail or device_op_active}"
        if current_job_id is not None:
            j = jobs.get(current_job_id)
            if j is not None:
                return f"broker job {j.id} is {j.status.value} ({j.owner}); the device is not idle"
            return f"broker job {current_job_id} is active; the device is not idle"
        for j in jobs.values():
            if j.status in (JobStatus.RUNNING, JobStatus.QUEUED):
                return f"broker job {j.id} is {j.status.value} ({j.owner}); the device is not idle"
        return ""

    def _parse_post_step_reclaim(data: dict) -> tuple[bool, str]:
        """(reclaim, error). Only a genuine JSON boolean may turn the kill path off: reclaim
        SIGTERM/SIGKILLs another user's processes, so an ambiguous value (a string, a number)
        must refuse rather than silently take either default — least of all the direction that
        runs the kill (Python's `bool("false")` is True, which would do exactly that)."""
        raw = data.get("reclaim", True)
        if isinstance(raw, bool):
            return raw, ""
        return True, f"reclaim must be a JSON boolean, got {raw!r}"

    def _parse_post_step_exit_code(data: dict) -> int:
        """A garbage exit_code is not a safety question the way reclaim is — it only decides
        whether the fabric pass gets forced — so an unparseable value falls back to 0 (clean)
        rather than refusing the whole step."""
        try:
            return int(data.get("exit_code", 0) or 0)
        except (TypeError, ValueError):
            return 0

    async def _pre_step_impl() -> dict:
        """The read-only pass an external scheduler runs before a job: probe, record, report.
        Never the ladder (03 I29) and never the fabric traffic pass — a prologue is on the
        critical path of every node in an allocation."""
        if refusal := _step_caller_is_root():
            return _step_refusal(refusal)

        # One absolute deadline for the WHOLE route, the same shape post-step uses: the wait
        # below and the gate after it share it, so waiting out a preceding step cannot buy the
        # gate a fresh full allotment and push the reply past what a prologue promises to bound.
        deadline_at = time.monotonic() + _shared_step_deadline_sec(PRE_STEP_DEADLINE_ENV, PRE_STEP_DEADLINE_DEFAULT_SEC)
        if not await _await_external_steps_clear(deadline_at):
            return {
                "status": "inconclusive",
                "ok": False,
                "reason": f"another external step ({external_step_active}) still held the device at the deadline",
                "fsm_state": fsm.state.value,
                "fsm_why": fsm.record.why,
            }

        if refusal := _broker_work_in_flight():
            return _step_refusal(refusal)

        # No await between the in-flight check above and this: on a single-threaded event loop
        # that makes the check-and-reserve atomic with respect to job_runner, the only other
        # reader of this reservation, so a job queued the instant after the check above still
        # cannot dispatch until the gate task below actually finishes. Held through the whole
        # gate call, not just until this function returns — the `finally` below only releases it
        # itself for a path that never handed that job to the gate task (see _run_step_gate's
        # ``handoff``, which covers this coroutine being cancelled mid-await too).
        token = _reserve_external_step("pre-step")
        handoff = {"reserved_by_task": False}
        try:
            timed_out, exc = await _run_step_gate(
                "pre-step",
                device_health_gate(None, phase="pre-step", run_fabric=False, with_recover=False),
                deadline_sec=max(0.0, deadline_at - time.monotonic()),
                mark_dirty_on_error=False,
                handoff=handoff,
                token=token,
            )
            if timed_out:
                return {
                    "status": "inconclusive",
                    "ok": False,
                    "reason": "the health pass did not finish within its deadline",
                    "fsm_state": fsm.state.value,
                    "fsm_why": fsm.record.why,
                }
            if exc is not None:
                # Unlike its in-broker twin (_ensure_device_clean_for_next_job), this pass is
                # read-only and takes no action on a gate exception — it must not mark the device
                # dirty on the say-so of the prologue alone. It found nothing it can vouch for, so
                # the verdict says exactly that; the epilogue's recovering pass is what responds.
                return {
                    "status": "inconclusive",
                    "ok": False,
                    "reason": f"the health pass raised: {exc}",
                    "fsm_state": fsm.state.value,
                    "fsm_why": fsm.record.why,
                }

            verdict = _slurm_step_verdict(require_free=True)
            verdict["status"] = "ok" if verdict["ok"] else "unfit"
            return verdict
        finally:
            if not handoff["reserved_by_task"]:
                _release_external_step(token)

    async def _post_step_impl(data: dict) -> dict:
        """The recovering pass an external scheduler runs after a job: reclaim the allocation's
        stragglers, then the full gate — probe, ladder if the evidence justifies it, re-verify.

        The reclaim comes first because the gate refuses over a tenant (04 I6/I7) and a straggler
        is exactly that from the scan's side. ``exit_code`` is the finished step's, and forces the
        fabric traffic pass: a fabric wedge is invisible to the enum snapshot but fails any job
        running across it, so a failure is when the pass earns its cost. A clean step pays
        nothing.

        ``data`` is the raw (already-JSON-decoded) request body, parsed here — after the root and
        in-flight checks, never before — so a malformed body from a non-root caller is refused for
        being non-root, not for being malformed, and a malformed body never reaches Python's
        arg-parsing machinery as an uncaught exception (a step must always return a verdict)."""
        if refusal := _step_caller_is_root():
            return _step_refusal(refusal)

        # One absolute deadline for the WHOLE route — the wait below, then reclaim, then the gate
        # — not a fresh timeout for each: reclaim can run two rounds of SIGTERM/SIGKILL with a
        # real grace sleep between them, and starting the clock only when it returns (as this
        # route used to) let the phases together outlast what "inconclusive" promises to bound.
        # _run_step_reclaim and _run_step_gate each get whatever of this budget is left.
        deadline_at = time.monotonic() + _shared_step_deadline_sec(
            POST_STEP_DEADLINE_ENV, POST_STEP_DEADLINE_DEFAULT_SEC
        )
        # Serialized against any other in-flight step, same as pre-step: two reclaims interleaving
        # their SIGTERM/SIGKILL rounds over one device would each audit kills the other may have
        # made, and a gate underneath another step's reclaim probes a device whose holders are
        # still being signalled.
        if not await _await_external_steps_clear(deadline_at):
            return {
                "status": "inconclusive",
                "ok": False,
                "reason": f"another external step ({external_step_active}) still held the device at the deadline",
                "fsm_state": fsm.state.value,
                "fsm_why": fsm.record.why,
                "reclaimed": [],
                "survivors": [],
            }

        if refusal := _broker_work_in_flight():
            return _step_refusal(refusal)

        # No await between the in-flight check above and this: on a single-threaded event loop
        # that makes the check-and-reserve atomic with respect to job_runner, the only other
        # reader of this reservation. It has to be taken here, before the reclaim below, not just
        # before the gate call — the reclaim's own `to_thread` hand-off is a yield too, and a job
        # dispatched into THAT window is what a raw scan would misread as a stale straggler and
        # SIGTERM/SIGKILL as root. ``handoff`` tracks whether _run_step_reclaim or _run_step_gate
        # ever handed the release off to its own task's done-callback (set the moment that task
        # exists, so even this coroutine being cancelled mid-route cannot race it); every return
        # before that point releases here instead, in the `finally`.
        token = _reserve_external_step("post-step")
        handoff = {"reserved_by_task": False}
        try:
            reclaim, reclaim_error = _parse_post_step_reclaim(data)
            if reclaim_error:
                return _step_refusal(reclaim_error)
            exit_code = _parse_post_step_exit_code(data)

            reclaimed: list[dict] = []
            survivors: list[dict] = []
            if reclaim:
                timed_out, res = await _run_step_reclaim(deadline_at, handoff, token)
                if timed_out:
                    return {
                        "status": "inconclusive",
                        "ok": False,
                        "reason": "the recovery pass did not finish within its deadline",
                        "fsm_state": fsm.state.value,
                        "fsm_why": fsm.record.why,
                        "reclaimed": reclaimed,
                        "survivors": survivors,
                    }
                # Audited inside _run_step_reclaim, on both the in-budget and the late path, so a
                # reclaim that overran its reply still reaches the journal. These are the reply's
                # copies only.
                reclaimed, survivors = _holder_rows(res.signalled), _holder_rows(res.survivors)
                if survivors or not res.scan_complete:
                    who = ", ".join(f"pid {h['pid']} ({h['username']})" for h in survivors) or "an unreadable holder"
                    out = _step_refusal(f"device still held after reclaim: {who}; recovery would run over a tenant")
                    out["reclaimed"], out["survivors"] = reclaimed, survivors
                    return out

            # The reclaim's `to_thread` yields the event loop. job_runner cannot dispatch into
            # that window any more — it waits on the same reservation taken above — but the
            # reservation is scoped to job dispatch, not to every device-touching path: an
            # operator's own `tt_device_reset` takes `_device_op` directly and answers to nothing
            # here. Re-checking after the reclaim, not just at entry, is what keeps the gate from
            # running underneath a reset that started in that gap.
            if refusal := _broker_work_in_flight():
                out = _step_refusal(refusal)
                out["reclaimed"], out["survivors"] = reclaimed, survivors
                return out

            was_degraded = _recovery_degraded()
            # mark_dirty_on_error=True matches the in-broker twin (_verify_device_after_job): the
            # gate threw before it could clear the device, so its state is unknown, and leaving it
            # unflagged would hand the next tenant a device nothing verified. deadline_sec is
            # whatever remains of the ROUTE's one absolute deadline after reclaim, not a fresh
            # allotment — reclaim consuming most of the budget correctly leaves the gate almost
            # none, rather than each phase getting the full deadline for itself.
            timed_out, exc = await _run_step_gate(
                "post-step",
                device_health_gate(None, phase="post-step", run_fabric=False, force_fabric=exit_code != 0),
                deadline_sec=max(0.0, deadline_at - time.monotonic()),
                mark_dirty_on_error=True,
                handoff=handoff,
                token=token,
            )
            if timed_out:
                return {
                    "status": "inconclusive",
                    "ok": False,
                    "reason": "the recovery pass did not finish within its deadline",
                    "fsm_state": fsm.state.value,
                    "fsm_why": fsm.record.why,
                    "reclaimed": reclaimed,
                    "survivors": survivors,
                }
            if exc is not None:
                return {
                    "status": "inconclusive",
                    "ok": False,
                    "reason": f"the recovery pass raised: {exc}",
                    "fsm_state": fsm.state.value,
                    "fsm_why": fsm.record.why,
                    "reclaimed": reclaimed,
                    "survivors": survivors,
                }

            verdict = _slurm_step_verdict(require_free=False)
            verdict["status"] = "ok" if verdict["ok"] else "unfit"
            verdict["recovered"] = was_degraded and verdict["ok"]
            verdict["reclaimed"], verdict["survivors"] = reclaimed, survivors
            return verdict
        finally:
            if not handoff["reserved_by_task"]:
                _release_external_step(token)

    async def _reset_device(force: bool = False) -> dict:
        """Device-reset core shared by the MCP tool and the REST/CLI route.

        Honors the foreign-uid reset gate using the caller's peer uid (known over
        the socket; over HTTP it degrades to a best-effort scan with no scoping).
        """
        global current_process

        # `steps` is the human-readable play-by-play returned to the caller so the
        # CLI can show exactly what was done and what each step returned.
        steps: list[str] = []

        def step(msg: str, level: str = "info") -> None:
            steps.append(msg)
            if logger:
                getattr(logger, level, logger.info)(f"  reset: {msg}")

        if logger:
            logger.info(f"reset_device: force={force}")

        caller_uid = current_peer_uid.get()
        scan = enumerate_device_holders()
        step(
            f"scanned device holders: {len(scan.holders)} process(es) holding /dev/tenstorrent"
            + ("" if scan.complete else " (scan incomplete — limited visibility)")
        )
        # Over the socket the caller's real uid scopes the gate. Over HTTP there is no peer
        # identity: on a privsep host an anonymous caller owns no holder, so every tenant is
        # foreign and we fail closed rather than reset over another tenant's run. Off privsep,
        # HTTP keeps the legacy single-tenant skip.
        if caller_uid is not None or privsep_enabled():
            decision = evaluate_reset_gate(caller_uid, scan, force=force)
            foreign = [{"pid": h.pid, "uid": h.uid, "user": h.username} for h in decision.foreign_holders]
            if not decision.allowed:
                step(f"gate REFUSED: {decision.reason}; foreign holders={foreign}", "warning")
                return {
                    "status": "refused",
                    "reason": decision.reason,
                    "foreign_holders": foreign,
                    "steps": steps,
                    "hint": "Ask the holder to stop, or pass force=true to override.",
                }
            scope = "caller-scoped" if caller_uid is not None else "anonymous (no peer identity)"
            step(f"gate allowed [{scope}]: {decision.reason}")
        else:
            step("gate skipped: no peer identity (HTTP, privsep off); not caller-scoped")

        pid_to_stop = None
        job_id_to_stop = None
        async with get_lock():
            if current_process:
                pid_to_stop = current_process.pid
                job_id_to_stop = current_job_id
        if pid_to_stop:
            # A holder that is SIGKILLed never releases the chip, which guarantees
            # the reset we are about to perform. Interrupting it can release the
            # mesh cleanly instead, and can never make the reset worse.
            step(f"stopping currently running job (pid={pid_to_stop}) before reset")
            # Mark the victim BEFORE we stop it: its imminent exit must read as reset-caused, not as
            # a wedge that flags the device for another reset (the loop this guard exists to break).
            _note_reset_killed_job()
            # Scope-route a privsep job: killpg on the broker-held pid hits the wrapper,
            # not the payload, and would reset over a job still holding the mesh.
            if job_id_to_stop:
                await _terminate_job(job_id_to_stop, pid_to_stop)
            else:
                await _terminate_process_group(pid_to_stop)
        async with get_lock():
            if current_process:
                await current_process.wait()
                current_process = None
            else:
                step("no broker job running; nothing to kill")

        indices = _present_chip_indices()
        if not indices:
            step("no /dev/tenstorrent devices found; nothing to reset", "warning")
            return {"status": "no_devices", "steps": steps}

        recovery = select_recovery()
        argv = recovery.reset_argv(indices)
        command = " ".join(argv)
        step(f"exec: {command}  ({len(indices)} device(s); ~30-60s on a Galaxy)")

        # The same choke point the health gate uses. Previously this ran a blocking
        # subprocess.run straight on the event loop: it starved the watchdog ping for
        # the full 60s+ of a Galaxy reset, systemd killed the broker as unresponsive,
        # and KillMode=control-group took the running tt-smi down with it — a reset
        # stopped partway through 32 ASICs. It also raced the gate's own reset.
        health_ok: Optional[bool] = None
        health_detail = "not checked (reset did not succeed)"
        reset_owner = _reset_action_owner()
        async with _device_op("reset", owner=reset_owner):
            rc, reset_out = await recovery_mechanism.reset_with_quiesce(argv, lambda m: step(m), reset_owner)
            status = "reset_complete" if rc == 0 else "reset_failed"
            step(f"{command} exited {rc} -> {status}", "info" if rc == 0 else "error")

            # A reset exiting 0 only means the command ran. Verify the chips actually
            # came back so the caller learns the mesh is usable, not just that tt-smi
            # returned 0.
            if rc == 0:
                health_ok, _ev = await fsm.observe(len(indices), lambda m: step(m), run_fabric=False, recovery=recovery)
                health_detail = (_ev.get("snapshot") or {}).get("detail") or (_ev.get("heartbeat") or {}).get(
                    "detail", ""
                )
                step(
                    f"health: {'OK' if health_ok else 'UNHEALTHY'} — {health_detail}",
                    "info" if health_ok else "warning",
                )
                if health_ok:
                    _clear_device_dirty(verified=True, why="reset tool: verified healthy")
                else:
                    status = "reset_unhealthy"  # reset ran but the mesh is not back

        return {
            "status": status,
            "devices": indices,
            "command": command,
            "returncode": rc,
            "health_ok": health_ok,
            "health_detail": health_detail,
            # The scoped reset merges stderr into stdout (one ordered stream is what
            # you want when reading a reset's tail), so stderr has nothing to add.
            "stdout": reset_out[-4000:],
            "stderr": "",
            "steps": steps,
        }

    @mcp.tool(
        name="tt_device_reset",
        annotations={
            "title": "Reset Device",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def reset_device(params: DeviceResetInput = DeviceResetInput()) -> dict:
        """Reset all Tenstorrent devices after a hang or crash.

        Use this when the device becomes unresponsive. This will:
        1. Refuse if another user's process is holding the device (reset gate)
        2. Kill any currently running process
        3. Reset all detected Tenstorrent devices via tt-smi

        A reset is a board-level reset of ALL chips, so resetting while another
        tenant holds the device aborts their run mid-op and can wedge the mesh.
        The gate refuses unless every current device holder is the caller's own
        uid. Pass force=true to override (the foreign holders are logged first).

        Over the unix socket the caller's real uid (SO_PEERCRED) is used for the
        gate; over HTTP there is no peer identity so the gate degrades to a
        best-effort holder scan with no caller scoping.

        WARNING: This is destructive - any running job will be terminated.

        Returns:
            dict: Reset result:
                - status (str): 'reset_complete', 'reset_failed', 'no_devices',
                  or 'refused' (foreign holder + not forced)
                - devices (list[str]): Device indices that were reset
                - foreign_holders (list): On refusal, [{pid, uid, user}, ...]
        """
        return await _reset_device(params.force)

    return mcp


def build_asgi_app(mcp: MCPServer):
    """Build the Starlette ASGI app and wrap it with peer-cred middleware.

    The same app (MCP /mcp + /api/* + /health) is served over both TCP and the
    unix socket; the middleware publishes the socket peer's uid per request.
    """
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
        # Not a bind: the SDK reads this only to auto-enable DNS-rebinding protection for
        # a loopback host. Enabled, it 421s the shim's synthetic `tt-device-broker` Host
        # and the socket every tenant arrives on stops working.
        host="0.0.0.0",
    )
    return PeerCredMiddleware(app)


# One log per notification kind (READY/WATCHDOG/...): a broken $NOTIFY_SOCKET fails on
# every WATCHDOG=1 ping, so an unbounded log would flood at the ping interval.
_sd_notify_errors_logged: set = set()


def _sd_notify(state: str) -> None:
    """Send a notification to systemd via $NOTIFY_SOCKET. No-op when not run under
    systemd Type=notify. Used for readiness and the watchdog heartbeat."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):  # abstract namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError as e:
        # A dropped WATCHDOG=1 makes systemd count the broker as wedged and kill it, and a dropped
        # READY=1 stalls startup — so a failing notify is worth saying, not swallowing. Logged once
        # per kind so a persistently broken socket cannot flood at the ping interval.
        kind = state.split("=", 1)[0]
        if kind not in _sd_notify_errors_logged:
            _sd_notify_errors_logged.add(kind)
            if logger:
                logger.warning(
                    f"systemd notify {kind} failed ({e}); if this persists systemd may " f"count the broker as wedged"
                )


async def _watchdog_heartbeat() -> None:
    """Ping the systemd watchdog from the event loop. If the loop ever wedges, the
    pings stop and systemd restarts the broker — running jobs survive in their
    scopes and are re-adopted on startup, so the recovery is non-destructive.
    Interval is half of WatchdogSec (passed as WATCHDOG_USEC)."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        # No systemd watchdog: a wedged event loop will NOT be auto-restarted. That is a real gap
        # in the liveness story the pings exist to close, so say it once rather than running
        # silently without it. Fires once — this coroutine is started once per process.
        if logger:
            logger.warning(
                "systemd watchdog not armed (WATCHDOG_USEC unset) — a wedged event loop " "will not be auto-restarted"
            )
        return
    interval = max(1.0, int(usec) / 1_000_000 / 2)
    stall_logged = False
    while True:
        # Ping only while the sampler — which drives every relift/escalation — is alive. A stalled
        # sampler (hung on a wedged device read while the event loop lives) means a held device will
        # never escalate; withhold the ping so systemd restarts the broker (startup re-gates and
        # re-adopts jobs, so the restart is non-destructive) rather than let it sit held forever.
        if sampler.is_stalled():
            if not stall_logged:
                if logger:
                    logger.error(
                        "telemetry sampler stalled > %ds — the relift/escalation loop is "
                        "not running; withholding the watchdog ping so systemd restarts",
                        int(sampler.STALL_SEC),
                    )
                health_event("watchdog_sampler_stalled", stall_sec=int(sampler.STALL_SEC))
                stall_logged = True
        else:
            stall_logged = False
            _sd_notify("WATCHDOG=1")
        await asyncio.sleep(interval)


async def run_transports(mcp: MCPServer, port: int, socket_path: str | None, serve_http: bool = True):
    """Run the unix-socket and/or HTTP (TCP) transports over the same ASGI app.

    ``serve_http=False`` runs socket-only — useful for a shared host where binding
    the well-known TCP port would change behavior for every other tenant; only
    clients that connect to the socket are affected.
    """
    import uvicorn

    app = build_asgi_app(mcp)

    # Ahead of either transport binding, so nothing is served against state that has not
    # re-adopted its surviving job scopes. The app lifespan runs too late to order against
    # the reset-scope wait below.
    global job_runner_task
    _ensure_async_primitives()
    seed_job_counter()  # ids continue across a restart instead of running backwards
    ensure_job_exit_dir()  # jobs record their own exit status here; see _monitor_readopted_scope

    # Settle the heartbeat probe's availability HERE rather than on first use. The
    # answer is cached, and the first gate could easily run against an already-wedged
    # mesh — caching "unsupported" off that would silently disable the probe for the
    # life of the process. See health.heartbeat_supported().
    hb_ok = await asyncio.to_thread(heartbeat_supported, True)
    if logger:
        logger.info(
            f"HEALTH sysfs ARC heartbeat probe: {'available' if hb_ok else 'NOT AVAILABLE'} "
            f"(free per-chip liveness, no device touch)"
        )

    # Stamp the stack at every start. Without it no journal entry can be compared across
    # hosts or across time — "was this box on the same firmware as that one, in the week
    # it kept rebooting" is unanswerable after the fact, and the answer is free now.
    env = await asyncio.to_thread(_stack_versions)
    chips = await asyncio.to_thread(chip_snapshot_event, "broker_start", env=env)
    health_event(
        "broker_start",
        version=__version__,
        heartbeat_probe=hb_ok,
        pid=os.getpid(),
        env=env,
        chips=len(chips),
        aer=aer_totals(chips),
    )
    if logger:
        logger.info(
            f"HEALTH stack: kmd={env.get('kmd')} fw={env.get('fw_bundle')} "
            f"kernel={env.get('kernel')} chips={len(chips)}"
        )

    # Cache every chip's PCI address while they are all still answering. Once a chip is
    # removed from the kernel its sysfs entry is gone, and with it the path to the bridge
    # that is the only way to reset it.
    global device_pci_map
    device_pci_map = {i: chip_pci_bdf(i) for i in await asyncio.to_thread(read_heartbeats)}
    if logger:
        logger.info(
            f"HEALTH mapped {len(device_pci_map)} chips to PCI addresses "
            f"(needed to reset a chip that has left the bus)"
        )

    # What killed the machine last time, straight from the firmware. This is readable
    # only from the current boot's dmesg and the next reboot overwrites it, so a record
    # not taken here is a crash nobody will ever explain. These hosts reboot most days;
    # taken every start, that becomes a distribution of real causes instead of a theory.
    await asyncio.to_thread(_record_previous_boot_error)

    # A reset started by the broker we are replacing may still be going in its
    # restart-safe backend. Wait it out before anything else touches the device.
    if await asyncio.to_thread(recovery_mechanism.scope_active):
        async with _device_op("startup-adopt-reset"):
            await recovery_mechanism.await_foreign_scope(lambda m: logger.info(f"STARTUP {m}") if logger else None)

    await run_startup_tasks()
    if job_runner_task is None or job_runner_task.done():
        job_runner_task = asyncio.create_task(job_runner())

    # Always-on: the reboot we are chasing does not wait for a job to be running, and the
    # in-memory ring does not survive it. Each sample also lands on disk.
    sampler.start()

    socket_server = None
    if socket_path:
        try:
            socket_server = await serve_unix_socket(app, socket_path)
        except Exception as exc:  # noqa: BLE001 - socket is optional, never fatal
            if logger:
                logger.error(f"unix-socket transport failed to start: {exc}")

    # Signal readiness + start the watchdog heartbeat (both no-op outside systemd
    # Type=notify, e.g. a per-user daemon).
    _sd_notify("READY=1")
    asyncio.create_task(_watchdog_heartbeat())

    if serve_http:
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        await uvicorn.Server(config).serve()
    elif socket_server is not None:
        # Socket-only: block on the socket server's serve task to keep the process alive.
        await socket_server._tt_serve_task
    else:
        raise SystemExit("nothing to serve: HTTP disabled and no unix socket configured")


def _preflight_required_capabilities(socket_path: Optional[str]) -> tuple[list[str], list[str], bool]:
    """Assert every capability the broker needs to actually recover a wedge is present and
    runnable BEFORE it serves. Otherwise each is resolved lazily at first use and silently
    skips when absent, so a box boots "healthy" and only discovers a missing probe or reset
    tool when a wedge needs it — the gap that once left a device in an unliftable hold.

    Returns ``(fails, warns, serialize_only)``: fails are missing REQUIRED capabilities (the
    broker refuses to serve); warns are absent OPTIONAL capabilities (logged loud, never silent,
    but not fatal); ``serialize_only`` asks the caller to turn the gate off because this host
    cannot probe at all. The decision is returned rather than applied so this stays a reporter.

    Checks only STABLE conditions — a configured path exists and is executable, a binary is on
    PATH — never a tool's runtime success, so a busy tt-smi or a mid-load KMD cannot fail the
    boot. What is required scales to what the host can use: the deploy-staged validators are
    required of root on systemd, which is the shape whose installer puts them down, and warned
    about elsewhere; opt-in rungs (power-cycle, privsep) are required only where opted in; the
    eth-heartbeat probe is an optimization, not a requirement (the fabric check is the
    authoritative eth/fabric validator)."""
    fails: list[str] = []
    warns: list[str] = []
    serialize_only = False

    def need_bin(name: str, why: str) -> None:
        if shutil.which(name) is None:
            fails.append(f"{name} not on PATH — {why}")

    def need_cmd_target(env_key: str, why: str, *, or_else: Optional[Callable[[], bool]] = None) -> None:
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            # `or_else` names a capability that satisfies `why` without this env var — e.g. a
            # built-in path the caller already knows how to run unconfigured. Only the empty
            # case defers to it: once an operator DOES set the var, it is their override and
            # must resolve to a real, executable target on its own.
            if or_else is not None and or_else():
                return
            fails.append(f"{env_key} not set — {why}")
            return
        target = raw.split()[0]  # the value is a command line; argv[0] is the script/binary
        resolved = target if os.path.isabs(target) else shutil.which(target)
        if not resolved or not os.path.exists(resolved):
            fails.append(f"{env_key}={raw!r}: target not found — {why}")
        elif not os.access(resolved, os.X_OK):
            fails.append(f"{env_key}={raw!r}: target not executable — {why}")

    try:
        present = len([p for p in Path(TT_DEV_DIR).iterdir() if p.name.isdigit()])
    except OSError:
        present = 0
    if present == 0:
        # No device nodes: the broker has nothing to arbitrate. A misdeployment, but bricking
        # it hides that worse than an idle broker whose /health shows no devices. Skip device caps.
        return fails, warns, serialize_only

    declared = os.environ.get("TT_DEVICE_MCP_EXPECTED_CHIPS", "").strip()
    fabric_host = (
        present > 1
        or (declared.isdigit() and int(declared) > 1)
        or os.environ.get("TT_DEVICE_MCP_RESET_MODE", "").strip().lower() == "galaxy"
    )

    # An unprivileged daemon with no tt-smi can still serialize, which is the one thing that
    # shape exists for, so it degrades loudly rather than refusing to serve. Root keeps this
    # fatal: a shared host that cannot probe is a trap, and is expected to have the tooling.
    if shutil.which("tt-smi") is None and not privileges.is_root():
        warns.append(
            "tt-smi is not on PATH — health gating and device recovery are both off and this "
            "daemon serializes only. Install tt-smi to get the gate and the reset rung."
        )
        serialize_only = True
    else:
        if health_monitor._health_check_enabled():
            need_bin("tt-smi", "the health snapshot shells out to it (or set TT_DEVICE_MCP_HEALTH_CHECK=0)")
        # Required even with probing disabled: `tt_device_reset` is an operator tool, not part
        # of the gate. The binary, not the mode: both modes shell out to tt-smi, so this needs no
        # derivation (which would be a device init on the boot path).
        _reset_override = os.environ.get("TT_DEVICE_MCP_RESET_ARGS", "").strip().split()
        need_bin(_reset_override[0] if _reset_override else "tt-smi", "the recovery ladder's reset command")

    if _scoped_reset_backend():
        # The system broker keeps resets alive in PID-1 scopes and controls host poller units.
        need_bin("systemd-run", "the recovery ladder runs each reset in a transient systemd scope")
        need_bin("systemctl", "the ladder quiesces pollers, adopts reset scopes, and reboots through it")
    # setpci is required nowhere and warned about only where it would otherwise be usable: the
    # rung needs root as well (I17), and telling an unprivileged daemon to install a tool it
    # still could not use is noise. Its absence for root is loud, not fatal — the ladder has
    # gentler-first ordering precisely so a missing rung falls through.
    if privileges.is_root() and shutil.which("setpci") is None:
        warns.append(
            "setpci not on PATH — the per-chip bridge reset, the gentlest recovery rung, cannot "
            "fire; the ladder falls through to the tt-smi reset"
        )
    # Reads only what is DECLARED. Deriving means a tt-smi device init, and the boot of a
    # broker that restarted after a wedge is the worst moment to aim one at the silicon —
    # deriving happens on the reset path, which already budgets for it.
    # Undeclared is only a finding if the boot flow could not resolve it either: a committed
    # fsm.recovery means the platform IS known for this process, so warning that it will be
    # derived later would contradict the BOOT line just above it in the log.
    # Same predicate as the scope backend, and for the same reason: the system installer is what
    # stages the fabric validator and the eth pre-read, and it is the only shape that runs as root
    # under systemd. Routed through the seam the preflight tests patch.
    deploy_staged = _scoped_reset_backend()
    if not deploy_staged and fabric_host:
        # The fabric validator and the eth pre-read are root systemd-staged artifacts that
        # install-user.sh has no way to put down, so on a per-user daemon their absence is the
        # expected state, not a misdeployment. The ladder still has its reset rung; what it loses
        # is the post-reset fabric proof, and a hold it cannot verify away stays a hold.
        warns.append(
            "multi-chip host without the deploy-staged fabric validator — post-reset fabric "
            "proof and per-job fabric admission-gating are off. The reset rung still runs; a "
            "mesh it cannot prove healthy stays held."
        )
    if (
        deploy_staged
        and fabric_host
        and fsm.recovery is None
        and not (
            os.environ.get("TT_DEVICE_MCP_RESET_ARGS", "").strip()
            or _declared_reset_mode(health_monitor._journal_skip_once)
        )
    ):
        warns.append(
            "reset mode undeclared AND unresolved at boot (the mesh could not be "
            "read) — it will be derived from tt-smi at reset time; declare "
            f"TT_DEVICE_MCP_RESET_MODE ({RESET_MODE_GALAXY}|{RESET_MODE_TARGET}|loudbox) "
            "to pin it"
        )

    if deploy_staged and fabric_host:
        need_cmd_target(
            "TT_DEVICE_MCP_FABRIC_CHECK_CMD",
            "the authoritative check that proves the fabric actually moves data",
            or_else=lambda: fabric.build_command() is not None,
        )
        # The eth-heartbeat probe is a NON-PERTURBING optimization for the read-only relift, not a
        # required validator — the fabric traffic pass is the authoritative eth/fabric check. Absent,
        # the relift falls back to the fabric pass. Warn loud so its absence is never silent, but do
        # not refuse to serve over it.
        if not os.environ.get("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "").strip():
            warns.append(
                "TT_DEVICE_MCP_ETH_HEARTBEAT_CMD unset — the passive eth-core probe is off "
                "(the read-only relift falls back to the fabric traffic pass). Optional."
            )

    # Armed but with no ipmitool: WARN, never refuse. Refusing leaves the host with no broker
    # at all — no queue, no gating, every tenant back on bare metal — which is a worse outage
    # than a ladder that stops one rung short. The rung reads as OFF everywhere else (see
    # _auto_power_cycle_enabled), so the shortfall is stated, not assumed.
    if os.environ.get("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1").strip() != "0" and not privileges.can_ipmi():
        warns.append(
            f"TT_DEVICE_MCP_AUTO_POWER_CYCLE is armed but {_no_bmc_reason()} — the "
            "BMC power-cycle rung is OFF. The ladder terminates at the warm reboot; a chip "
            "that has LEFT the PCIe bus cannot be recovered in software on this host."
        )

    if privsep_enabled():
        if not privileges.is_root():
            fails.append(
                "TT_DEVICE_MCP_PRIVSEP=1 but the broker is not root — jobs would silently run "
                "as the broker's own uid instead of the caller's"
            )
        need_bin("systemd-run", "TT_DEVICE_MCP_PRIVSEP=1 wraps every job in it")
        if not socket_path:
            fails.append(
                "TT_DEVICE_MCP_PRIVSEP=1 but no --socket — SO_PEERCRED identity (the privsep "
                "target) is carried only by the unix socket"
            )

    # The chip baseline and incident capture are the readers that make an unwritable dir fatal, and
    # both are behind the health checks. The event journal is written either way, but its loss is
    # best-effort by design — so with the checks off this warns rather than refusing to serve, which
    # is what demanding /var/lib did to every non-root daemon.
    hd = health_dir()
    try:
        hd.mkdir(parents=True, exist_ok=True)
        probe = hd / ".preflight"
        probe.write_text("")
        probe.unlink()
    except OSError as e:
        msg = (
            f"health dir {hd} not writable ({e}) — incident capture and the chip baseline "
            "would be silently lost, and the event journal with them"
        )
        (warns if serialize_only or not health_monitor._health_check_enabled() else fails).append(msg)

    # Host software below what this broker's reset and probe semantics were matched against.
    # Always a warning: an old firmware bundle is a configuration gap, not a missing recovery
    # capability, and a broker that refused to start over one would deny service for something
    # nobody asked it to enforce. Its value is explanatory — below bundle 19.9 the eth
    # link-status telemetry is unpopulated, so the eth probe SKIPs, and without this line that
    # skip reads as a broken probe rather than a host that cannot answer the question.
    warns.extend(version_floor_warnings())

    return fails, warns, serialize_only


def main():
    global logger, job_log_dir, stats, stats_dir, stats_file

    parser = argparse.ArgumentParser(description="TT Device MCP Server (Streamable HTTP)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--log-dir", type=str, default=None, help="Directory for log file (default: current dir)")
    parser.add_argument(
        "--socket",
        type=str,
        default=None,
        help=(
            "Unix domain socket path for the daemon (e.g. /run/tt-device-broker.sock). "
            "Enables peer-uid (SO_PEERCRED) authz alongside the HTTP transport. "
            "Falls back to the TT_DEVICE_MCP_SOCKET env var."
        ),
    )
    parser.add_argument(
        "--no-http",
        action="store_true",
        help="Serve only the unix socket (no TCP). Requires --socket; confines a shared-host broker to socket clients.",
    )
    args = parser.parse_args()

    # Setup logging directory
    log_dir = Path(args.log_dir) if args.log_dir else Path.cwd()
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "server.log"
    job_log_dir = log_dir  # Job logs go in same folder with timestamps

    socket_path = resolve_socket_path(args.socket)

    # Setup stats directory and file
    stats_dir = log_dir / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stats_file = stats_dir / f"{timestamp}.json"

    # Initialize stats
    stats = Stats()

    logger = setup_logging(log_file)

    logger.info("=" * 60)
    logger.info("TT-DEVICE-MCP SERVER STARTING (Streamable HTTP)")
    logger.info(f"  PID: {os.getpid()}")
    logger.info(f"  Port: {args.port}")
    logger.info(f"  Machine: {stats.machine}")
    logger.info(f"  MCP Endpoint: http://localhost:{args.port}/mcp")
    logger.info(f"  Health Check: http://localhost:{args.port}/health")
    if socket_path:
        logger.info(f"  Unix socket: {socket_path} (peer-uid authz enabled)")
    logger.info(f"  Logs dir: {log_dir}/")
    logger.info(f"  Stats file: {stats_file}")
    logger.info("=" * 60)

    # Save initial stats
    save_stats()

    if args.no_http and not socket_path:
        parser.error("--no-http requires --socket (or TT_DEVICE_MCP_SOCKET)")

    # The boot flow, before anything that reads the subsystem it builds: construct, then resolve
    # which platform this host IS. Preflight below reports capabilities per rung, and the rungs a
    # host actually has depend on that verdict, so it has to be settled first.
    resolved_mode = boot_broker(probe_platform=True)
    if logger:
        logger.info(f"BOOT platform: {resolved_mode or 'unresolved (per-pass fallback)'}")

    # Fail fast and loud on a missing recovery capability rather than discovering it at wedge
    # time. Refuse to serve device work the broker could not recover — the whole point of the gate.
    preflight_fails, preflight_warns, serialize_only = _preflight_required_capabilities(socket_path)
    for w in preflight_warns:
        logger.warning(f"PREFLIGHT (optional capability absent): {w}")
    if serialize_only:
        # Applied here, not inside preflight: that function reports, and the gate's master switch
        # is startup wiring. Exported so a job's own environment shows the state it ran under.
        os.environ["TT_DEVICE_MCP_HEALTH_CHECK"] = "0"
    if preflight_fails:
        logger.error("=" * 60)
        logger.error("STARTUP PREFLIGHT FAILED — refusing to serve. Missing required capabilities:")
        for f in preflight_fails:
            logger.error(f"  - {f}")
        logger.error(
            "Provision the above (or its deploy) and restart; do not run device work the " "broker cannot recover."
        )
        logger.error("=" * 60)
        sys.exit(1)

    # Create the server and run the unix-socket and/or HTTP transports.
    mcp = create_mcp_server()
    try:
        asyncio.run(run_transports(mcp, args.port, socket_path, serve_http=not args.no_http))
    finally:
        # One last publish on a clean shutdown (SIGINT/SIGTERM unwinding asyncio.run, or a normal
        # return) so the textfile reflects the final state rather than going stale for up to
        # STATS_UPDATE_SEC until node_exporter notices the process is gone. Never raises.
        metrics.write_textfile()


if __name__ == "__main__":
    main()
