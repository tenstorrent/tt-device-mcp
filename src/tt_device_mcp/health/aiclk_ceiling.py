# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Opt-in per-host AICLK ceiling: the broker re-applies a firmware clock cap before it loads the
chips or releases a job.

The cap (``SET_ASIC_HOST_FMAX``) lives in chip firmware RAM, so a chip reset, a power cycle or a
boot clears it, and a job may put the default back when it exits. An operator who sets
``TT_DEVICE_MCP_AICLK_CEILING_MHZ`` asks for no load to run above that clock, the broker's own
fabric traffic pass included. So the broker owns re-applying it at the points where it may have
been lost:

  - right after a reset's PCI rescan, before the pollers come back and before the verify's
    traffic pass (``RecoveryMechanism.reset_with_quiesce``);
  - in every probe pass, after the snapshot proved the chips present and before the eth read and
    the fabric pass (``HealthMonitor.update``);
  - at broker start (a boot or power cycle cleared it), before the startup fabric verify;
  - at the job door, if something since the last verified apply may have cleared it.

Unset, nothing here runs: no helper, no added time. The helper sends only to a chip that reads
above the ceiling, so it never fights another cap holder that already holds the same or a lower
value. Fail-closed: a chip the helper cannot bring to the ceiling is an UNHEALTHY probe, so the
gate does not release onto it; a helper that cannot run at all on this host (wrong architecture,
a tt-umd without the telemetry tag) disarms the feature once, loudly, instead of holding a
healthy box on a configuration error.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp import metrics
from tt_device_mcp.health.evidence import health_event
from tt_device_mcp.health.monitors.subproc import Terminate, _killpg, run_probe

ENV_MHZ = "TT_DEVICE_MCP_AICLK_CEILING_MHZ"
ENV_CMD = "TT_DEVICE_MCP_AICLK_CEILING_CMD"
ENV_TIMEOUT = "TT_DEVICE_MCP_AICLK_CEILING_TIMEOUT_SEC"
ENV_PYTHON = "TT_DEVICE_MCP_AICLK_CEILING_PYTHON"
DEFAULT_TIMEOUT_SEC = 8.0
# Per-chip bound inside the helper. Measured at ~2 ms per chip on a healthy 32-chip Blackhole
# host; a chip that takes a second to open is not healthy.
CHIP_TIMEOUT_SEC = 2.0
# Helper exit codes: 0 every chip verified at or below the ceiling; 1 a chip is still above it or
# could not be read/sent; 77 the check cannot run on this host at all (disarm, never a reset).
UNSUPPORTED_RC = 77

_REPO_ROOT = Path(__file__).resolve().parents[3]


def ceiling_mhz() -> Optional[int]:
    """The configured ceiling in MHz, or None when the feature is off (unset, empty, not a
    positive integer)."""
    raw = os.environ.get(ENV_MHZ, "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return None
    return int(raw)


def timeout_sec() -> float:
    try:
        return max(1.0, float(os.environ.get(ENV_TIMEOUT, "") or DEFAULT_TIMEOUT_SEC))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def helper_path() -> Path:
    """Where the installer stages the helper, else this checkout's own copy (dev installs)."""
    installed = Path(os.environ.get("TTDEV_ROOT", "/opt/tt-device-broker")) / "aiclk-ceiling.py"
    if installed.is_file():
        return installed
    return _REPO_ROOT / "deploy" / "tt-device-aiclk-ceiling.py"


def build_argv(mhz: int) -> list[str]:
    override = os.environ.get(ENV_CMD, "").strip()
    if override:
        # Judged on its exit code alone, the same contract as the eth-heartbeat override.
        return ["/bin/sh", "-c", override]
    python = os.environ.get(ENV_PYTHON, "").strip() or sys.executable
    return [python, str(helper_path()), "--mhz", str(mhz), "--chip-timeout", str(CHIP_TIMEOUT_SEC)]


def _summary(text: str) -> dict:
    """The helper's last ``{"summary": ...}`` JSON line, or {} (an override need not print one)."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("summary"):
                return d
    return {}


class AiclkCeiling:
    """Process-wide ceiling state. ``owed`` means something may have cleared the cap since the
    last verified apply; it starts True because a broker start follows a boot, a power cycle or
    at least an unknown stretch the broker did not watch."""

    def __init__(self) -> None:
        self.owed = True
        self.disarmed = ""
        self.last: dict = {}
        self.proc = None  # the live helper, for the dead-chip kill path

    def armed(self) -> bool:
        return ceiling_mhz() is not None and not self.disarmed

    def mark_owed(self, reason: str) -> None:
        """Record that the cap may be gone. Journaled on the transition only, so a busy queue does
        not write one row per job."""
        if not self.armed() or self.owed:
            return
        self.owed = True
        health_event("aiclk_ceiling_owed", reason=reason)

    def _track(self, proc) -> None:
        self.proc = proc

    async def apply(
        self,
        where: str,
        *,
        log: Optional[Callable[[str], None]] = None,
        terminate: Terminate = _killpg,
    ) -> tuple[Optional[bool], str, dict]:
        """Run the helper once. Returns ``(ok, detail, evidence)``: True verified (clears
        ``owed``), False not verified (``owed`` stays set), None not armed or disarmed now.
        Bounded by ``timeout_sec()``; the helper's process group is killed on timeout. Never
        raises."""
        mhz = ceiling_mhz()
        if mhz is None or self.disarmed:
            return None, "not armed", {}
        argv = build_argv(mhz)
        env = {**os.environ, ENV_MHZ: str(mhz)}
        t0 = time.monotonic()
        try:
            rc, out = await run_probe(
                argv, timeout_sec=timeout_sec(), track=self._track, stream=True, env=env, terminate=terminate
            )
        except Exception as e:  # noqa: BLE001 - a helper that cannot start is a host config fault
            rc, out = UNSUPPORTED_RC, f"could not start the helper: {e}".encode()
        dt = time.monotonic() - t0
        text = out.decode(errors="replace") if isinstance(out, (bytes, bytearray)) else str(out)
        s = _summary(text)
        chips, at_or_below, sent = s.get("chips"), s.get("ok"), s.get("sent", 0)
        evidence = {"mhz": mhz, "rc": rc, "seconds": round(dt, 3), "where": where}
        # The helper's "ok" count is renamed: an evidence dict's "ok" is the probe's own verdict.
        for k, ek in (("chips", "chips"), ("ok", "chips_ok"), ("sent", "sent"), ("over", "over"), ("unreadable", "unreadable")):
            if k in s:
                evidence[ek] = s[k]
        last_line = text.strip().splitlines()[-1][:200] if text.strip() else "(no output)"

        if rc == UNSUPPORTED_RC or rc in (126, 127):
            # Not a chip fault: the host cannot run the check (wrong architecture, no tt-umd tag,
            # helper missing). Holding the box on it would reset healthy silicon over a config
            # error, so disarm for this process and say so once, loudly.
            self.disarmed = last_line
            health_event("aiclk_ceiling_disarmed", mhz=mhz, rc=rc, detail=last_line, where=where)
            metrics.probe_observed("aiclk_ceiling", "skipped", dt)
            detail = f"DISARMED — the ceiling check cannot run on this host (rc={rc}): {last_line}"
            if log:
                log(f"aiclk-ceiling: {detail}")
            return None, detail, evidence

        count = f"{at_or_below}/{chips}" if chips is not None else "all"
        if rc == 0:
            self.owed = False
            self.last = {**evidence, "verified_at": time.time()}
            detail = f"ceiling {mhz} MHz applied to {count} (sent {sent}, {dt:.2f}s)"
            health_event("aiclk_ceiling_applied", **evidence)
            metrics.probe_observed("aiclk_ceiling", "healthy", dt)
            ok: Optional[bool] = True
        else:
            self.owed = True
            why = f"timed out after {timeout_sec():.0f}s" if rc is None else f"rc={rc}"
            detail = f"ceiling {mhz} MHz NOT verified ({why}; {count} at or below): {last_line}"
            health_event("aiclk_ceiling_unverified", **evidence, detail=last_line)
            metrics.probe_observed("aiclk_ceiling", "unhealthy", dt)
            ok = False
        if log:
            log(f"aiclk-ceiling: {'OK' if ok else 'UNHEALTHY'} — {detail}")
        return ok, detail, evidence

    def status(self) -> dict:
        return {
            "armed": self.armed(),
            "mhz": ceiling_mhz(),
            "owed": self.owed,
            "disarmed": self.disarmed or None,
            "last_verified_at": self.last.get("verified_at"),
        }


CEILING = AiclkCeiling()
