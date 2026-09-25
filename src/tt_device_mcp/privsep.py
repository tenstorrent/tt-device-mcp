# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Privilege separation: run each job as its invoking user in a device-admitted
systemd slice.

Phase A authenticates the caller (SO_PEERCRED) but still execs every job as the
daemon's own uid. Phase B closes that: when the daemon runs as root it launches
each job via ``systemd-run --scope --uid=<peer> --slice=<ttdev>`` so the job runs
as the real user, in their workspace, inside a cgroup slice that is granted
``/dev/tenstorrent``. Combined with the udev rule + user-slice DeviceAllow drop-in
shipped under ``deploy/``, a process that is *not* admitted through this path
cannot open the device — bare-metal use is blocked at the kernel rather than
punished after the fact.

Everything here is gated and degrades to today's behavior: privsep is OFF unless
``TT_DEVICE_MCP_PRIVSEP`` is set, the daemon is root, and ``systemd-run`` exists.
The command-construction helpers are pure so they unit-test without root/systemd.
"""

import logging
import os
import pwd
import shutil
from typing import List, Optional

logger = logging.getLogger("tt-device-mcp")

# Unix group that owns /dev/tenstorrent (0660 root:<group>) after the udev rule.
# In lockdown mode an admitted job runs with this as its primary gid so DAC lets
# it open the node; interactive shells (other gid) are denied — that's the
# bare-metal block. Set TT_DEVICE_MCP_DEVICE_GROUP to enable.
DEFAULT_DEVICE_GROUP = "ttdev"

_TRUE = {"1", "true", "yes", "on"}


def privsep_enabled(env=None) -> bool:
    """True if the operator opted into privilege separation via env."""
    env = os.environ if env is None else env
    return env.get("TT_DEVICE_MCP_PRIVSEP", "").strip().lower() in _TRUE


def should_privsep(env=None) -> bool:
    """Whether to wrap job exec in ``systemd-run``.

    Requires all of: opt-in flag, the daemon running as root (``--uid`` needs it),
    and ``systemd-run`` on PATH. Any missing → return False and run jobs as today.
    """
    if not privsep_enabled(env):
        return False
    if os.geteuid() != 0:
        logger.warning("TT_DEVICE_MCP_PRIVSEP set but daemon is not root; privsep disabled")
        return False
    if shutil.which("systemd-run") is None:
        logger.warning("TT_DEVICE_MCP_PRIVSEP set but systemd-run not found; privsep disabled")
        return False
    return True


def _gid_for_uid(uid: int) -> Optional[int]:
    try:
        return pwd.getpwuid(uid).pw_gid
    except KeyError:
        return None


def systemd_run_prefix(
    *,
    uid: int,
    gid: int,
    device_group: Optional[str] = None,
    unit: Optional[str] = None,
) -> List[str]:
    """Build the ``systemd-run`` argv that runs a job as ``uid``.

    ``--scope`` keeps the job a child of the daemon so the existing PIPE capture
    still works, but systemd reparents the payload into the scope's own cgroup:
    killpg on the broker-held pid reaches only the systemd-run wrapper. Terminating
    a scoped job must signal the scope itself (see ``server._terminate_job``).

    Cooperative mode (``device_group`` unset, device left world-rw): the job keeps
    the user's primary ``gid``. Lockdown mode (device locked to ``0660 root:<group>``
    by the udev rule): the job runs with ``device_group`` as its **primary gid** so
    DAC lets it open the node, while interactive shells (other gid) are denied.

    We use ``--gid`` rather than ``-p SupplementaryGroups``/``DevicePolicy``: older
    systemd-run rejects transient ``-p`` properties on a scope, and plain DAC via
    the gid is sufficient for the lock.
    """
    job_gid = device_group if device_group else gid
    argv = [
        "systemd-run",
        "--scope",
        "--quiet",
        "--collect",  # GC the transient unit even if the job exits non-zero
        f"--uid={uid}",
        f"--gid={job_gid}",
    ]
    if unit is not None:
        argv.append(f"--unit={unit}")
    return argv


def privsep_prefix_for(peer_uid: Optional[int], *, env=None, unit: Optional[str] = None) -> Optional[List[str]]:
    """The ``systemd-run`` prefix to run a job for ``peer_uid``, or None to run as today.

    Returns None (no wrapping) when privsep is off, the caller's uid is unknown
    (HTTP transport), the caller is root (nothing to drop to), or the uid has no
    passwd entry. The caller then falls back to the legacy direct-exec path.

    ``unit`` names the transient scope so it survives — and is rediscoverable
    after — a broker restart (the scope is its own systemd unit, not in the
    daemon's cgroup), which is what lets startup re-adopt a still-running job.

    Device admission is opt-in via ``TT_DEVICE_MCP_DEVICE_GROUP`` — set it only
    once the device node is locked to ``0660 root:<group>``; unset (cooperative
    mode) yields a bare per-user scope.
    """
    if not should_privsep(env):
        return None
    if peer_uid is None or peer_uid == 0:
        # No peer identity (HTTP) or already root — don't fabricate a target user.
        return None
    gid = _gid_for_uid(peer_uid)
    if gid is None:
        logger.warning("privsep: uid=%s has no passwd entry — no scope built", peer_uid)
        return None
    env = os.environ if env is None else env
    device_group = env.get("TT_DEVICE_MCP_DEVICE_GROUP") or None
    return systemd_run_prefix(uid=peer_uid, gid=gid, device_group=device_group, unit=unit)


def privsep_refusal(peer_uid: Optional[int], *, env=None) -> Optional[str]:
    """Why a job for ``peer_uid`` must be REFUSED under active privsep, or None to proceed.

    When privsep is active every job must run as its real, non-root submitter via
    ``systemd-run --uid``. If that identity can't be established, refusing is the only safe
    move: the sole fallthrough is direct exec as the broker itself — root — so a
    ``privsep_prefix_for`` that returns None must never be read as "run it anyway". A caller
    who genuinely IS root is not a refusal (it already runs as itself); privsep inactive is
    not a refusal (direct exec is the intended legacy behavior, and the daemon is not root).
    """
    if not should_privsep(env):
        return None
    if peer_uid is None:
        return (
            "privsep is active but this job carries no peer identity (SO_PEERCRED) — "
            "refusing rather than running it as the broker (root)"
        )
    if peer_uid == 0:
        return None
    if _gid_for_uid(peer_uid) is None:
        return (
            f"privsep is active but uid={peer_uid} has no passwd entry to run as — "
            f"refusing rather than running it as the broker (root)"
        )
    return None
