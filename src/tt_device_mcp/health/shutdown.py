# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A health probe killed by the host going down, or by the broker's own stop, is not a verdict
(spec 03 I63).

systemd stops a unit by signalling its whole cgroup, so a host shutdown or a broker stop
SIGTERMs a running probe in the same instant it SIGTERMs the broker. The probe then exits -15,
and read as a failure that reached the post-reset verify of a hold escalation: the broker asked
for a host reboot over a check the shutdown itself had killed. The check measured nothing about
the device.

So a probe that ends by SIGTERM/SIGKILL while a shutdown is under way raises
:class:`CheckInterruptedByShutdown`. It is a ``CancelledError``: every caller already unwinds a
cancelled pass without counting it, climbing a rung or releasing the device, because that is
how a broker stop reaches it. The hold and the device stay as they are; after the boot the
hugepage-gated startup pass decides. A probe killed outside a shutdown (the broker's own
timeout, a dead-chip kill, any other signal) is not touched here and fails as before.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
import subprocess
from typing import Optional

from tt_device_mcp.health.evidence import health_event

# How a probe reports a SIGTERM/SIGKILL death: asyncio/subprocess give the negative signal, a
# shell wrapper that ran the probe in the foreground gives 128 + the signal.
SIGNAL_RCS = frozenset({-signal.SIGTERM, -signal.SIGKILL, 128 + signal.SIGTERM, 128 + signal.SIGKILL})

# A queued or running job for one of these means the host is going down.
SHUTDOWN_TARGETS = frozenset(
    {"shutdown.target", "reboot.target", "poweroff.target", "halt.target", "kexec.target", "final.target"}
)

SYSTEMCTL_TIMEOUT_SEC = 3.0

_broker_stopping = ""


class CheckInterruptedByShutdown(asyncio.CancelledError):
    """A probe the shutdown killed. Unwinds the pass like a cancellation; never a verdict."""

    def __init__(self, probe: str, rc: int, why: str):
        super().__init__(f"{probe} interrupted by shutdown (exit {rc}): {why}")
        self.probe, self.rc, self.why = probe, rc, why


def note_broker_stopping(sig: int) -> None:
    """The broker's own stop signal arrived (called from its signal handler)."""
    global _broker_stopping
    try:
        name = signal.Signals(sig).name
    except ValueError:
        name = str(sig)
    _broker_stopping = f"broker received {name}"


def _systemctl(*args: str) -> str:
    try:
        proc = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _own_unit() -> str:
    try:
        with open("/proc/self/cgroup") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    for line in lines:
        unit = line.rsplit("/", 1)[-1]
        if unit.endswith(".service"):
            return unit
    return ""


def shutdown_in_progress() -> str:
    """Why the host or the broker is going down, or ``""`` when neither is. Blocking (systemctl,
    bounded by ``SYSTEMCTL_TIMEOUT_SEC`` per call); any read that fails counts as not stopping,
    so a doubt keeps the old failure path."""
    if _broker_stopping:
        return _broker_stopping
    if not shutil.which("systemctl"):
        return ""
    state = _systemctl("is-system-running").strip()
    if state == "stopping":
        return "host shutting down (system state stopping)"
    for line in _systemctl("list-jobs", "--no-legend", "--plain").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in SHUTDOWN_TARGETS:
            return f"host shutting down ({parts[1]} job {' '.join(parts[2:4])})".rstrip()
    unit = _own_unit()
    if unit and _systemctl("show", "-p", "ActiveState", "--value", unit).strip() == "deactivating":
        return f"broker stopping ({unit} deactivating)"
    return ""


def interrupted_by_shutdown(probe: str, rc: Optional[int]) -> Optional[CheckInterruptedByShutdown]:
    """The exception to raise when ``probe`` died by SIGTERM/SIGKILL during a shutdown, journaled
    as ``check_interrupted_by_shutdown``; ``None`` for every other exit, a broker timeout
    (``rc is None``) included. Blocking: call it off the event loop."""
    if rc is None or rc not in SIGNAL_RCS:
        return None
    why = shutdown_in_progress()
    if not why:
        return None
    health_event("check_interrupted_by_shutdown", probe=probe, rc=rc, why=why)
    return CheckInterruptedByShutdown(probe, rc, why)
