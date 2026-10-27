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
    traffic pass (``RecoveryMechanism.reset_with_quiesce``). Freshly reset firmware can still read
    the old limit for a few seconds after the cap was sent, so this apply re-runs the helper within
    a short settle window before it calls the ceiling unverified;
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

import asyncio
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
ENV_SETTLE = "TT_DEVICE_MCP_AICLK_CEILING_SETTLE_SEC"
DEFAULT_TIMEOUT_SEC = 8.0
# Post-reset settle window: a chip reset seconds ago may still read its old limit right after the
# cap was sent (seen ~4 s after a Galaxy reset, all 32 chips, then verified 7 s later). The helper
# is re-run every SETTLE_POLL_SEC while the window lasts; only a chip still above the ceiling at
# its end is unverified. A re-run's timeout is capped to the time left in the window plus
# SETTLE_RETRY_MARGIN_SEC, so the whole apply ends within max(timeout, window + margin): 8 s at the
# defaults, under the ~10 s a post-reset step may add. The margin is well above a healthy run
# (~2 ms per chip), so the cap never cuts one short.
DEFAULT_SETTLE_SEC = 3.0
MAX_SETTLE_SEC = 10.0
SETTLE_POLL_SEC = 0.5
SETTLE_RETRY_MARGIN_SEC = 4.0
# Per-chip bound inside the helper. Measured at ~2 ms per chip on a healthy 32-chip Blackhole
# host; a chip that takes a second to open is not healthy.
CHIP_TIMEOUT_SEC = 2.0
# Helper exit codes: 0 every chip verified at or below the ceiling; 1 a chip is still above it or
# could not be read/sent; 77 the check cannot run on this host at all (disarm, never a reset).
UNSUPPORTED_RC = 77

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Clock and sleep seams for the settle window (tests drive them without real waiting).
_now = time.monotonic
_sleep = asyncio.sleep


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


def settle_sec() -> float:
    """The post-reset settle window in seconds, 0 to report the first readback as is."""
    try:
        v = float(os.environ.get(ENV_SETTLE, "") or DEFAULT_SETTLE_SEC)
    except ValueError:
        return DEFAULT_SETTLE_SEC
    return min(max(0.0, v), MAX_SETTLE_SEC)


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

    async def _run_helper(
        self, argv: list[str], env: dict, terminate: Terminate, timeout: float
    ) -> tuple[Optional[int], str]:
        try:
            rc, out = await run_probe(
                argv,
                timeout_sec=timeout,
                track=self._track,
                stream=True,
                env=env,
                terminate=terminate,
                name="aiclk-ceiling",
            )
        except Exception as e:  # noqa: BLE001 - a helper that cannot start is a host config fault
            rc, out = UNSUPPORTED_RC, f"could not start the helper: {e}".encode()
        return rc, out.decode(errors="replace") if isinstance(out, (bytes, bytearray)) else str(out)

    async def apply(
        self,
        where: str,
        *,
        log: Optional[Callable[[str], None]] = None,
        terminate: Terminate = _killpg,
        settle: float = 0.0,
    ) -> tuple[Optional[bool], str, dict]:
        """Run the helper (once unless ``settle``). Returns ``(ok, detail, evidence)``: True verified (clears
        ``owed``), False not verified (``owed`` stays set), None not armed or disarmed now.
        Bounded by ``timeout_sec()``; the helper's process group is killed on timeout. Never
        raises.

        ``settle`` > 0 (the post-reset apply) re-runs a helper that reported a chip above the
        ceiling or unreadable every ``SETTLE_POLL_SEC`` until ``settle`` seconds after the first
        run started, and reports only the last result: no retry starts past the window, and a
        retry's timeout is capped to the time left in the window plus ``SETTLE_RETRY_MARGIN_SEC``,
        so the added time is at most ``max(timeout_sec(), settle + SETTLE_RETRY_MARGIN_SEC)``. A
        timeout or a cannot-run exit is never retried."""
        mhz = ceiling_mhz()
        if mhz is None or self.disarmed:
            return None, "not armed", {}
        from tt_device_mcp.health import chip_state

        # Spec 03 I66: the helper opens every chip; a chip lost and still attached keeps it from
        # running (outside this broker's own reset step). Not verified, so ``owed`` stays set.
        lost = chip_state.unreadable_lost()
        if lost:
            return False, f"not run: chip(s) {','.join(lost)} lost and still attached", {"lost_chips": lost}
        argv = build_argv(mhz)
        env = {**os.environ, ENV_MHZ: str(mhz)}
        t0 = _now()
        run_timeout = timeout_sec()
        rc, text = await self._run_helper(argv, env, terminate, run_timeout)
        attempts = 1
        # Verified, timed out (already spent the full bound) or cannot run here: never re-run.
        final = (0, None, UNSUPPORTED_RC, 126, 127)
        while rc not in final and _now() + SETTLE_POLL_SEC <= t0 + settle:
            if log and attempts == 1:
                log(f"aiclk-ceiling: readback not yet at {mhz} MHz (rc={rc}); re-reading for up to {settle:g}s")
            await _sleep(SETTLE_POLL_SEC)
            left = max(0.0, t0 + settle - _now())
            run_timeout = min(timeout_sec(), left + SETTLE_RETRY_MARGIN_SEC)
            rc, text = await self._run_helper(argv, env, terminate, run_timeout)
            attempts += 1
        dt = _now() - t0
        s = _summary(text)
        chips, at_or_below, sent = s.get("chips"), s.get("ok"), s.get("sent", 0)
        evidence = {"mhz": mhz, "rc": rc, "seconds": round(dt, 3), "where": where}
        if attempts > 1:
            evidence["attempts"] = attempts
        # The helper's "ok" count is renamed: an evidence dict's "ok" is the probe's own verdict.
        for k, ek in (
            ("chips", "chips"),
            ("ok", "chips_ok"),
            ("sent", "sent"),
            ("over", "over"),
            ("unreadable", "unreadable"),
        ):
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
            tries = f", {attempts} reads" if attempts > 1 else ""
            detail = f"ceiling {mhz} MHz applied to {count} (sent {sent}, {dt:.2f}s{tries})"
            health_event("aiclk_ceiling_applied", **evidence)
            metrics.probe_observed("aiclk_ceiling", "healthy", dt)
            ok: Optional[bool] = True
        else:
            self.owed = True
            why = f"timed out after {round(run_timeout, 1):g}s" if rc is None else f"rc={rc}"
            if attempts > 1:
                why += f" after {attempts} reads in {dt:.1f}s"
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
