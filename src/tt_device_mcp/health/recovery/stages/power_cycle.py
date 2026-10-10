# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cold BMC chassis power-cycle recovery rung — the final one on the ladder."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile
import time
from typing import Callable

# Spec 04 I22. The pre-power-cycle hook gets this long by default to drain whatever else on the host
# would be cut by the cycle; past it the hook's process group is killed and the cycle goes ahead.
PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC = 300.0
# Ceiling on a configured timeout: the hook delays recovery of a box that is already down, so a
# typo (3000000) must never read as "wait forever".
PRE_POWER_CYCLE_HOOK_TIMEOUT_MAX_SEC = 3600.0
# Only the tail of the hook's output reaches the log line; the rest stays in the temp file.
HOOK_OUTPUT_TAIL_BYTES = 2048


def _fire_power_cycle() -> None:
    """Shell out to the BMC chassis power cycle — the one action that recovers a whole-bus wedge
    a warm reboot leaves wedged (measured on blx04). Isolated to one line so every test replaces
    it and no test path can ever power-cycle the box running the suite. Raises on a non-zero exit /
    missing binary so a fire that did NOT take the box down is detectable."""
    r = subprocess.run(["ipmitool", "chassis", "power", "cycle"], timeout=30, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ipmitool power cycle exited {r.returncode}: {r.stderr.strip()[:200]}")


def pre_power_cycle_hook_timeout_sec() -> float:
    """The hook's timeout. A non-positive or malformed value reads as the default; a huge one is
    capped, so no setting makes the hook block recovery for good."""
    raw = os.environ.get("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK_TIMEOUT_SEC", "").strip()
    try:
        v = float(raw)
    except ValueError:
        return PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC
    if not 0 < v < float("inf"):
        return PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC
    return min(v, PRE_POWER_CYCLE_HOOK_TIMEOUT_MAX_SEC)


def run_pre_power_cycle_hook(log: Callable[[str], None], reason: str) -> dict:
    """Run ``TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK`` (spec 04 I22) and wait for it, bounded.

    The command is split like shell words and run without a shell, in its own session, with the
    cycle's reason in ``TT_DEVICE_MCP_POWER_CYCLE_REASON``. Its output goes to a temp file, not a
    pipe, so a background child it leaves holding stdout cannot stretch the wait past its own exit.
    On timeout its whole process group is killed. Every outcome — unset, unparseable, not
    startable, non-zero, timed out — is logged and returned, and the caller fires the cycle anyway:
    the hook is a courtesy to co-tenants, never a gate on recovery. Never raises.

    Returns {"ran": bool, "rc": int|None, "timed_out": bool, "seconds": float, "detail": str}."""
    out = {"ran": False, "rc": None, "timed_out": False, "seconds": 0.0, "detail": ""}
    raw = os.environ.get("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", "").strip()
    if not raw:
        return out
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        out["detail"] = f"does not parse ({e})"
        log(f"PRE-POWER-CYCLE HOOK skipped: TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK {out['detail']}")
        return out
    if not argv:
        return out
    timeout = pre_power_cycle_hook_timeout_sec()
    env = {**os.environ, "TT_DEVICE_MCP_POWER_CYCLE_REASON": reason}
    log(f"PRE-POWER-CYCLE HOOK: running {argv[0]!r} (up to {timeout:g}s) before the power cycle")
    t0 = time.monotonic()
    try:
        with tempfile.TemporaryFile() as buf:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=buf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                )
            except OSError as e:
                out["detail"] = f"could not start: {e}"
                log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} {out['detail']}; power-cycling anyway")
                return out
            out["ran"] = True
            try:
                out["rc"] = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                out["timed_out"] = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)  # reap it, so it never lingers as a zombie
                except subprocess.TimeoutExpired:
                    pass
            out["seconds"] = round(time.monotonic() - t0, 1)
            buf.seek(max(0, buf.seek(0, os.SEEK_END) - HOOK_OUTPUT_TAIL_BYTES))
            out["detail"] = buf.read().decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001 - a broken hook must never stop the cycle
        out["detail"] = f"failed: {e!r}"
        log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} {out['detail']}; power-cycling anyway")
        return out
    tail = f": {out['detail'][-500:]}" if out["detail"] else ""
    if out["timed_out"]:
        log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} killed after {timeout:g}s; power-cycling anyway{tail}")
    else:
        log(f"PRE-POWER-CYCLE HOOK {argv[0]!r} exited {out['rc']} after {out['seconds']:g}s{tail}")
    return out
