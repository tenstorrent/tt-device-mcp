# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Shared constants for tt-device-mcp."""

import math
import os
from pathlib import Path

DEFAULT_PORT = 8333  # default --port for the legacy HTTP transport; the socket-based broker never binds it
SERVICE_NAME = "tt-device-mcp"

# Canonical host broker socket (root, multi-tenant). Present → clients use it
# automatically; override with TT_DEVICE_MCP_SOCKET.
DEFAULT_SOCKET = "/run/tt-device-broker/broker.sock"
JOB_RETENTION_SEC = 300  # 5 minutes - grace period before removing finished jobs from memory
STATS_UPDATE_SEC = 30  # How often to persist stats to disk

# Graceful termination: killing a device job without letting it release the chip
# is how the mesh gets wedged. The escalation ladder is SIGINT -> SIGTERM -> SIGKILL.
#
# SIGINT first, and this is the load-bearing part: a ttnn job only releases the
# device from its teardown path (pytest fixture teardown, ttnn's atexit), and that
# path runs only if the interpreter UNWINDS. SIGINT is the one signal that unwinds
# it -- Python raises KeyboardInterrupt. SIGTERM does not: Python installs no
# handler for it, so its default action terminates the process outright, atexit
# never runs, the mesh is never closed, and the eth cores are left mid-transaction.
# For a Python job SIGTERM is therefore no gentler than SIGKILL; only SIGINT is.
#
# The SIGINT window has to cover a real mesh teardown, not a process exit: closing
# a 32-chip 4x8 galaxy drains dispatch queues and shuts eth links down, and a job
# interrupted mid-CCL must first return from the device op it is blocked in. At 10s
# every timeout escalated to SIGKILL and wedged eth cores.
GRACEFUL_KILL_GRACE_SEC = 60

# Window after SIGINT before the SIGKILL of last resort. A job that ignored an
# interrupt for a full minute is not unwinding; SIGTERM is a formality on the way
# to the reap, and reaching SIGKILL means the device was NOT released.
SIGTERM_GRACE_SEC = 15

# After a job is finalized, any of its processes still alive (a child that left the process
# group, or one the ladder never reached) gets SIGTERM, then SIGKILL after this window. The
# job itself is already gone, so this is a sweep of leftovers, not a teardown: it is kept
# short because the next job waits on it.
SURVIVOR_TERM_GRACE_SEC = 5

# How long to wait after the survivors' SIGKILL before naming whoever is still alive. A
# process that outlives SIGKILL is stuck in the kernel (state D, most often inside the
# driver), and no signal will move it.
SURVIVOR_KILL_WAIT_SEC = 2

# When a device reset has run long enough to be worth saying so (a 6U Galaxy reset is
# ~30-60s). This is a WARNING line, not a verdict: the reset owns its own systemd scope and
# is deliberately never killed, because one stopped partway through 32 ASICs is far worse
# than one that overran. Our timer expiring therefore says nothing whatsoever about the
# reset — it says we got bored.
DEVICE_RESET_OVERRUN_SEC = 180

# When we give up and call a reset failed. Only a scope that never ends earns that: reaching
# this means the reset is genuinely stuck, not merely slow. Measured on a real box, resets
# routinely pass 180s and go on to succeed — reporting those as failures set the failed-reset
# cooldown, which suppresses the NEXT reset, and left the device held on a verdict about a
# reset that had worked.
DEVICE_RESET_TIMEOUT_SEC = 600

# Upper bound on the post-job fabric traffic check (TT_DEVICE_MCP_FABRIC_CHECK_CMD).
# The check runs a pinned, prebuilt validator — it never compiles — so a healthy pass is
# 45-75s, and anything far past that is a wedged fabric, not a slow one. This bound is what
# a wedge COSTS: the queue is dead until it expires, and only then does recovery start. A
# generous margin here is not caution, it is the wedge's dwell time.
FABRIC_CHECK_TIMEOUT_SEC = 180

# The fabric check exits with this when it could not run at all (validator or
# descriptor absent). It means "nothing was learned about the fabric" and must stay
# distinct from exit 0: a check that reports a mesh it never looked at as healthy is
# worse than no check, and one that reports it as broken resets the device for no
# reason. The broker skips on this code and warns.
FABRIC_CHECK_CANNOT_CHECK_RC = 77

# The recovery ladder's five named rungs, in severity order (0..4) — the one place that names
# them. Lives here, not in health/recovery/stages/ (the package these actually name), so
# metrics.py can import it directly: importing a package submodule always runs that package's
# __init__.py first, and health/recovery/__init__.py imports metrics — a cycle metrics.py's own
# docstring already avoids for fsm.FAULTS by not importing it. constants.py has no import of
# anything under tt_device_mcp, so it carries no such risk for any importer.
STAGE_BRIDGE_RESET = "bridge_reset"
STAGE_SMI_RESET = "smi_reset"
STAGE_UBB_TRAY = "ubb_tray"
STAGE_HOST_REBOOT = "host_reboot"
STAGE_POWER_CYCLE = "power_cycle"
STAGE_NAMES = (STAGE_BRIDGE_RESET, STAGE_SMI_RESET, STAGE_UBB_TRAY, STAGE_HOST_REBOOT, STAGE_POWER_CYCLE)

# A Slurm prologue/epilogue must always return a verdict rather than hang until the site's
# PrologEpilogTimeout kills the script — a kill drains the node with nothing said about what was
# slow. Deadlines are ours, and shorter than any site's, so a step times out into an
# "inconclusive" answer first. Lives here, not server.py, so the CLI can derive its client
# timeout from the same env vars and defaults without importing server.py's heavier dependency
# chain (the mcp package) just to read two numbers.
PRE_STEP_DEADLINE_ENV = "TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC"
POST_STEP_DEADLINE_ENV = "TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC"
PRE_STEP_DEADLINE_DEFAULT_SEC = 120
POST_STEP_DEADLINE_DEFAULT_SEC = 600

# Margin the CLIENT timeout carries over the server's own step deadline. A fixed client timeout
# independent of the server's would report a transport failure for a step the broker is still
# resolving the instant a site raises its deadline env var past the client's hardcoded number —
# exactly the failure mode this margin exists to prevent. Tying the two together means raising
# the server deadline raises the client timeout with it, automatically.
STEP_CLIENT_TIMEOUT_MARGIN_SEC = 60


def step_deadline_sec(var: str, default: int) -> float:
    """The step deadline honored by the server side of a pre-step/post-step route: how long the
    gate call is allowed before the route reports "inconclusive" instead of hanging. Any value
    that cannot be read as a positive, finite float falls back to ``default`` -- "inf" would
    disable the deadline outright, defeating the guarantee that a step always returns a verdict."""
    raw = os.environ.get(var, "").strip()
    try:
        val = float(raw) if raw else float(default)
    except ValueError:
        val = float(default)
    return val if math.isfinite(val) and val > 0 else float(default)


def step_client_timeout_sec(var: str, default: int) -> float:
    """The CLI's HTTP client timeout for a step route: the server's own deadline (read from the
    same env var, same default) plus STEP_CLIENT_TIMEOUT_MARGIN_SEC, so it always outlasts the
    pass it is waiting on even when a site has raised the server-side deadline."""
    return step_deadline_sec(var, default) + STEP_CLIENT_TIMEOUT_MARGIN_SEC


def user_install_dir() -> Path:
    """The per-user base: venv, CLI symlink, and — under `state/` — everything the
    daemon writes while it runs. One base, so there is one thing to find and one to delete.

    Machine-local, never $HOME: $HOME is a network share on many cluster hosts, and a venv of
    compiled wheels is machine- and arch-specific, so sharing it collides across hosts.

    Boot-cleared on purpose: **nothing here is meant to outlive a reboot.** This shape has no
    boot recovery to protect — it is not a systemd unit, and the container it usually runs in has
    no cron — so a base that survived would only hand the next install a stale venv to write over.
    Re-run the installer after a reboot; that is the supported path, not a fallback.
    """
    override = os.environ.get("TT_DEVICE_MCP_INSTALL_DIR", "").strip()
    return Path(override) if override else Path(f"/tmp/tt-device-mcp-{os.getuid()}")


def user_state_dir() -> Path:
    """Every per-user daemon's runtime state: socket, pid, logs, stats, health journal, job exit
    status. A subdirectory of the install base (above), so the whole per-user footprint is one
    tree — and it inherits that base's guarantee that nothing survives a boot: a job cannot, a
    socket must not, and the one durable reader is the reboot-rung rate limiter that only the
    system broker has a ladder to drive.

    Per-uid because /tmp is only private inside a container; on a reserved bare-metal box two users
    can each run a daemon, and a shared base would hand the second an EACCES it cannot explain.

    Overridable via TT_DEVICE_MCP_STATE_DIR *independently* of the install base, so a test can
    repoint state alone and never touch the live daemon's.
    """
    override = os.environ.get("TT_DEVICE_MCP_STATE_DIR", "").strip()
    return Path(override) if override else user_install_dir() / "state"


def user_socket_path() -> str:
    """Socket for a per-user standalone daemon (no broker, no root). Machine-local, so a detached
    daemon stays reachable for the life of the boot. Everything is socket-based; no TCP port."""
    return str(user_state_dir() / "daemon.sock")


def resolve_socket(explicit=None) -> str:
    """The socket to reach a server, in priority order: explicit/env override,
    the host broker, then this user's standalone daemon. None if nothing's up."""
    cand = explicit or os.environ.get("TT_DEVICE_MCP_SOCKET")
    if cand:
        return cand
    for p in (DEFAULT_SOCKET, user_socket_path()):
        if os.path.exists(p):
            return p
    return None
