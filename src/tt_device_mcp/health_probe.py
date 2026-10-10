# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Idle-time device-health probe (spec 08 I17), run by ``tt-device-health-probe.timer``.

The broker's gate runs at job boundaries, so an idle box that drops a chip off PCIe or loses its
telemetry exporter sits degraded until a job lands on it. This probe closes that window with
non-destructive steps only: a PCI bus rescan when fewer chips are present than the host should
have, and a restart of a stopped telemetry exporter. A wedge that needs a reset is the broker's.

It stands back unless the broker says the device is idle: no job running or queued, no hold, FSM
healthy, no recovery op in flight. The broker stops the pollers (tt-telemetry) around a reset, and
a probe that restarted them would fight it, so it asks again right before each action. What it
cannot fix goes to the broker's opt-in alert hook (``alert.py``), once per episode, never per run.

Run as root: ``python -m tt_device_mcp.health_probe``. Always exits 0 unless it crashes.
"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp import alert
from tt_device_mcp.constants import DEFAULT_SOCKET
from tt_device_mcp.health.evidence import _default_health_dir
from tt_device_mcp.health.monitors.pci import _sum_aer
from tt_device_mcp.utils import _uds_api_call

BROKER_UNIT = "tt-device-broker.service"
TELEMETRY_UNIT = "tt-telemetry.service"
TT_PCI_VENDOR = "0x1e52"
STATE_FILE = "health_probe_state.json"
# Read from the running broker's own environment when this unit does not set them, so the probe
# uses the same hook, chip count and health dir as the broker with no second place to configure.
INHERITED_ENV = (
    "TT_DEVICE_MCP_ALERT_CMD",
    "TT_DEVICE_MCP_ALERT_TIMEOUT_SEC",
    "TT_DEVICE_MCP_EXPECTED_CHIPS",
    "TT_DEVICE_MCP_HEALTH_DIR",
)
RESCAN_SETTLE_SEC = 3.0
TELEMETRY_SETTLE_SEC = 5.0
# A chip a rescan could not bring back has a link down: rescanning it every lap only adds noise.
RESCAN_MIN_GAP_SEC = 1800.0
# AER counters only grow until reboot; a storm across many laps is one page an hour, not one a lap.
AER_ALERT_MIN_GAP_SEC = 3600.0


def log(msg: str) -> None:
    print(f"tt-health-probe: {msg}", flush=True)  # -> the journal via the unit


def idle_verdict(health: Optional[dict]) -> Optional[str]:
    """Why the probe must stand back, or None when the broker reports the device idle."""
    if not isinstance(health, dict) or "error" in health:
        err = health.get("error") if isinstance(health, dict) else "no answer"
        return f"broker /health did not answer ({err})"
    if health.get("held"):
        return f"broker holds the device ({health.get('held_reason') or health.get('fsm_why') or '?'})"
    if health.get("fsm_state") != "healthy":
        return f"broker FSM is {health.get('fsm_state')!r} ({health.get('fsm_why') or '?'})"
    if health.get("device_degraded"):
        return f"broker recovery in flight ({health.get('device_degraded')})"
    running, queued = health.get("running"), health.get("queued")
    if not isinstance(running, int) or not isinstance(queued, int):
        return "broker /health has no job counts"
    if running or queued:
        return f"broker busy ({running} running, {queued} queued)"
    return None


def expected_chips(present: int, health_dir: Path) -> int:
    """How many chips this host should have, read the way the broker reads it (spec 03 I11):
    TT_DEVICE_MCP_EXPECTED_CHIPS, else the broker's high-water baseline, else what is present.
    Read only: the baseline is the broker's to ratchet. A baseline that exists but cannot be read
    still proves the host has shown chips, so it fails closed to at least one."""
    env = os.environ.get("TT_DEVICE_MCP_EXPECTED_CHIPS", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    try:
        baseline = int(json.loads((health_dir / "chip_baseline.json").read_text()).get("chips", 0))
    except FileNotFoundError:
        return present
    except (OSError, ValueError, TypeError, AttributeError):
        return max(present, 1)
    return max(baseline, present)


class Probe:
    """One lap. Every host touch is a method or a path attribute, so a test can stub it."""

    def __init__(self, root: Path = Path("/")):
        self.dev_dir = root / "dev/tenstorrent"
        self.pci_dir = root / "sys/bus/pci/devices"
        self.rescan_path = root / "sys/bus/pci/rescan"
        self.boot_id_path = root / "proc/sys/kernel/random/boot_id"
        self.proc_dir = root / "proc"
        self.sleep: Callable[[float], None] = time.sleep
        self.now: Callable[[], float] = time.time
        self.state: dict = {}

    # --- host seams -------------------------------------------------------------------------
    def health(self) -> Optional[dict]:
        return _uds_api_call(DEFAULT_SOCKET, "/health", "GET", None, 5)

    def systemctl(self, *args: str) -> str:
        try:
            r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout.strip()

    def rescan(self) -> None:
        self.rescan_path.write_text("1")

    def present(self) -> int:
        try:
            return sum(1 for p in self.dev_dir.iterdir() if p.name.isdigit())
        except OSError:
            return 0

    def broker_env(self) -> dict:
        """The running broker's environment, limited to INHERITED_ENV. Empty when it is not up."""
        pid = self.systemctl("show", "-p", "MainPID", "--value", BROKER_UNIT)
        if not pid.isdigit() or pid == "0":
            return {}
        try:
            raw = (self.proc_dir / pid / "environ").read_bytes()
        except OSError:
            return {}
        env = {}
        for item in raw.split(b"\0"):
            k, sep, v = item.decode("utf-8", "replace").partition("=")
            if sep and k in INHERITED_ENV:
                env[k] = v
        return env

    def aer_total(self) -> int:
        total = 0
        try:
            devices = list(self.pci_dir.iterdir())
        except OSError:
            return 0
        for d in devices:
            try:
                if (d / "vendor").read_text().strip().lower() != TT_PCI_VENDOR:
                    continue
            except OSError:
                continue
            for f in ("aer_dev_fatal", "aer_dev_nonfatal"):
                total += _sum_aer(d / f) or 0
        return total

    def boot_id(self) -> str:
        try:
            return self.boot_id_path.read_text().strip()
        except OSError:
            return ""

    # --- state ------------------------------------------------------------------------------
    def _load_state(self, health_dir: Path) -> None:
        try:
            self.state = json.loads((health_dir / STATE_FILE).read_text())
        except (OSError, ValueError):
            self.state = {}
        if not isinstance(self.state, dict) or self.state.get("boot_id") != self.boot_id():
            self.state = {"boot_id": self.boot_id()}  # a reboot clears every episode and counter
        self.state.setdefault("open", [])

    def _save_state(self, health_dir: Path) -> None:
        try:
            health_dir.mkdir(parents=True, exist_ok=True)
            tmp = health_dir / (STATE_FILE + ".tmp")
            tmp.write_text(json.dumps(self.state))
            tmp.replace(health_dir / STATE_FILE)
        except OSError as e:
            log(f"could not save {STATE_FILE}: {e}")

    def _alert_once(self, kind: str, msg: str, **fields) -> None:
        """Log ALERT and run the hook on the first lap of an episode; later laps stay quiet."""
        if kind in self.state["open"]:
            log(f"still: {msg}")
            return
        self.state["open"].append(kind)
        log(f"ALERT {msg}")
        self._send(kind, msg, **fields)

    def _clear(self, kind: str) -> None:
        if kind in self.state["open"]:
            self.state["open"].remove(kind)

    def _send(self, kind: str, msg: str, **fields) -> None:
        event = {"kind": kind, "host": socket.gethostname(), "detail": msg, **fields}
        t = alert.send_alert(event)
        if t is not None:
            t.join(alert.alert_timeout_sec() + 5)  # a oneshot must not exit under its own hook

    def _still_idle(self) -> bool:
        why = idle_verdict(self.health())
        if why:
            log(f"stopped: {why}")
        return why is None

    # --- the lap ----------------------------------------------------------------------------
    def run(self) -> int:
        why = idle_verdict(self.health())
        if why:
            log(f"skip: {why}")
            return 0
        for k, v in self.broker_env().items():
            os.environ.setdefault(k, v)
        hdir = _default_health_dir()
        self._load_state(hdir)
        try:
            self._lap(hdir)
        finally:
            self._save_state(hdir)
        return 0

    def _lap(self, hdir: Path) -> None:
        present = self.present()
        expected = expected_chips(present, hdir)
        if not self._check_pci(present, expected):
            return
        tele = self._check_telemetry()
        if tele is None:
            return
        aer = self._check_aer()
        if not self.state["open"]:
            log(f"ok {self.present()}/{expected} chips telemetry={tele} aer={aer}")

    def _check_pci(self, present: int, expected: int) -> bool:
        """Rescan the bus when chips are missing. False when the broker stopped being idle."""
        if present >= expected:
            self._clear("health_probe_chips_missing")
            self.state.pop("last_rescan", None)
            return True
        last = float(self.state.get("last_rescan", 0) or 0)
        if self.now() - last < RESCAN_MIN_GAP_SEC:
            log(f"still: {present}/{expected} chips; next rescan in {int(RESCAN_MIN_GAP_SEC - (self.now() - last))}s")
            return True
        if not self._still_idle():
            return False
        log(f"{present}/{expected} chips present, rescanning the PCI bus")
        try:
            self.rescan()
        except OSError as e:
            log(f"rescan failed: {e}")
        self.state["last_rescan"] = self.now()
        self.sleep(RESCAN_SETTLE_SEC)
        now = self.present()
        if now >= expected:
            log(f"rescan recovered {now}/{expected} chips")
            self._clear("health_probe_chips_missing")
            self.state.pop("last_rescan", None)
        else:
            self._alert_once(
                "health_probe_chips_missing",
                f"rescan left {now}/{expected} chips: a link is down, needs a reset",
                present=now,
                expected=expected,
            )
        return True

    def _check_telemetry(self) -> Optional[str]:
        """Restart a stopped telemetry exporter. Its state, 'absent' when it is not installed, or
        None when the broker stopped being idle."""
        if self.systemctl("show", "-p", "LoadState", "--value", TELEMETRY_UNIT) in ("", "not-found"):
            return "absent"
        state = self.systemctl("is-active", TELEMETRY_UNIT) or "unknown"
        if state == "active":
            self._clear("health_probe_telemetry_down")
            return state
        if not self._still_idle():
            return None
        log(f"tt-telemetry {state}, clearing its start limit and starting it")
        self.systemctl("reset-failed", TELEMETRY_UNIT)
        self.systemctl("start", TELEMETRY_UNIT)
        self.sleep(TELEMETRY_SETTLE_SEC)
        after = self.systemctl("is-active", TELEMETRY_UNIT) or "unknown"
        if after == "active":
            log("tt-telemetry recovered")
            self._clear("health_probe_telemetry_down")
        else:
            self._alert_once(
                "health_probe_telemetry_down",
                f"tt-telemetry {after} after a restart: it cannot open the chips",
                state=after,
            )
        return after

    def _check_aer(self) -> int:
        """Alert when the chips' fatal/non-fatal AER counters grew since the last alert."""
        total = self.aer_total()
        seen = int(self.state.get("aer_seen", 0) or 0)
        if total <= seen:
            return total
        last = float(self.state.get("aer_alert_at", 0) or 0)
        if self.now() - last < AER_ALERT_MIN_GAP_SEC:
            log(f"AER count {total} (was {seen}); alerted {int(self.now() - last)}s ago")
            return total
        self.state["aer_seen"] = total
        self.state["aer_alert_at"] = self.now()
        msg = f"AER count {total} across the chips (+{total - seen} since the last alert)"
        log(f"ALERT {msg}")
        self._send("health_probe_aer", msg, aer_total=total, aer_new=total - seen)
        return total


def main() -> int:
    return Probe().run()


if __name__ == "__main__":
    raise SystemExit(main())
