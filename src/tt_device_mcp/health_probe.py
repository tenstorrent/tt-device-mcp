# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Idle-time device-health probe (spec 08 I17), run by ``tt-device-health-probe.timer``.

The broker's gate runs at job boundaries, so an idle box that drops a chip off PCIe or loses its
telemetry exporter sits degraded until a job lands on it. This probe closes that window with
non-destructive steps only: a PCI bus rescan when fewer chips are present than the host should
have, and a restart of a stopped telemetry exporter. A wedge that needs a reset is the broker's.

It stands back unless the broker says the device is idle: no job running or queued in its table,
no hold, FSM healthy, no device op in flight (its ``/health``, restart-inhibit file and flock), and no
tenant process holding a device node (a Slurm step, or a hung job's leftovers, that the job table
does not show). The broker stops the pollers (tt-telemetry) around a reset, and a probe that
restarted them would fight it, so it asks again right before each action, and after starting a
poller it asks once more and stops it again if the broker began a device op meanwhile. The rescan
write itself is made under the broker's device-op flock (spec 06 I8), taken non-blocking. That leaves
a window of the settle time in which a poller it started can run beside a reset the broker began:
the flock may not be held across a subprocess such as ``systemctl start``, so it is only tested
before the start, and the window can be narrowed, not closed. A
poller that is disabled is left alone: stopping and disabling it is how an operator keeps it off.
What it cannot fix goes to the broker's opt-in alert hook (``alert.py``), once per episode.

Run as root: ``python -m tt_device_mcp.health_probe``. Always exits 0 unless it crashes.
"""

import errno
import fcntl
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp import alert
from tt_device_mcp.constants import DEFAULT_SOCKET
from tt_device_mcp.device_holders import enumerate_device_holders
from tt_device_mcp.health.evidence import _default_health_dir
from tt_device_mcp.health.monitors.pci import _sum_aer
from tt_device_mcp.utils import _uds_api_call

BROKER_UNIT = "tt-device-broker.service"
# The broker's own poller list and default (server.DEVICE_POLLER_SERVICES): the units it quiesces
# around a reset are the ones this probe re-arms.
DEFAULT_POLLER_SERVICES = "tt-telemetry.service,tt-metrics-exporter.service"
# The broker's restart-inhibit file (server.device_op_inhibit): present while a device op runs.
DEFAULT_DEVICE_OP_LOCK = "/run/tt-device-broker/device-op.lock"
# The broker's advisory flock (spec 06 I8, server.device_op_flock), held for every device op; a
# sibling of the inhibit file unless set on its own.
DEVICE_OP_FLOCK_NAME = "device-op.flock"
TT_PCI_VENDOR = "0x1e52"
STATE_FILE = "health_probe_state.json"
# Read from the running broker's own environment when this unit does not set them, so the probe
# uses the same hook, chip count and health dir as the broker with no second place to configure.
INHERITED_ENV = (
    "TT_DEVICE_MCP_ALERT_CMD",
    "TT_DEVICE_MCP_ALERT_TIMEOUT_SEC",
    "TT_DEVICE_MCP_EXPECTED_CHIPS",
    "TT_DEVICE_MCP_HEALTH_DIR",
    "TT_DEVICE_MCP_POLLER_SERVICES",
    "TT_DEVICE_MCP_DEVICE_OP_LOCK",
    "TT_DEVICE_MCP_DEVICE_OP_FLOCK",
)
RESCAN_SETTLE_SEC = 3.0
TELEMETRY_SETTLE_SEC = 5.0
# A chip a rescan could not bring back has a link down: rescanning it every lap only adds noise.
RESCAN_MIN_GAP_SEC = 1800.0
# AER counters only grow until reboot; a storm across many laps is one page an hour, not one a lap.
AER_ALERT_MIN_GAP_SEC = 3600.0
# A poller episode closes only after this many laps in a row found it active, so a crash-looping
# unit that is up on some laps and down on others stays one episode and one page.
POLLER_OK_LAPS = 3
# 'activating' is systemd already (re)starting the unit, or a slow init over many chips: neither
# restarted nor paged, unless it is still not up after this many laps in a row.
POLLER_ACTIVATING_LAPS = 3
POLLER_RESTARTABLE = ("failed", "inactive")
POLLER_ENABLED = ("enabled", "enabled-runtime")


def log(msg: str) -> None:
    print(f"tt-health-probe: {msg}", flush=True)  # -> the journal via the unit


def device_verdict(health: Optional[dict]) -> Optional[str]:
    """Why the broker's device is not fit for a poller to run on (no answer, a hold, an FSM that
    is not healthy, a device op in flight), or None. Jobs do not count: pollers run beside them."""
    if not isinstance(health, dict) or "error" in health:
        err = health.get("error") if isinstance(health, dict) else "no answer"
        return f"broker /health did not answer ({err})"
    if health.get("held"):
        return f"broker holds the device ({health.get('held_reason') or health.get('fsm_why') or '?'})"
    if health.get("fsm_state") != "healthy":
        return f"broker FSM is {health.get('fsm_state')!r} ({health.get('fsm_why') or '?'})"
    if health.get("device_degraded"):
        return f"broker recovery in flight ({health.get('device_degraded')})"
    return None


def idle_verdict(health: Optional[dict]) -> Optional[str]:
    """Why the probe must stand back, or None when the broker reports the device idle. This sees
    the broker's job table only; Probe.idle_why() adds the inhibit file and the device holders."""
    why = device_verdict(health)
    if why:
        return why
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

    def rescan(self) -> bool:
        """One write to the bus rescan file under the broker's device-op flock (spec 06 I8
        external-tool contract). False, nothing written, while a broker device op holds it."""
        with device_op_flock() as free:
            if not free:
                return False
            self.rescan_path.write_text("1")
        return True

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

    def device_op_lock(self) -> Optional[str]:
        """The broker's in-flight device op from its inhibit file or its flock, or None. A file
        whose pid is gone was left by a killed broker and is ignored, as the auto-updater ignores
        it. The flock is only tested, never kept."""
        try:
            text = device_op_inhibit().read_text().strip()
        except FileNotFoundError:
            text = ""
        except OSError as e:
            return f"unreadable ({e})"
        pid, _, name = text.partition(" ")
        if text and not (pid.isdigit() and not (self.proc_dir / pid).exists()):
            return name or text
        with device_op_flock() as free:
            return None if free else "unnamed (device-op flock held)"

    def tenant_holders(self) -> Optional[str]:
        """Tenant processes holding a device node, or None. Root and system accounts (the
        pollers) are not tenants. A scan that could not see every holder fails closed."""
        try:
            scan = enumerate_device_holders()
        except OSError as e:
            return f"holder scan failed ({e})"
        tenants = scan.foreign_holders(0)
        if tenants:
            return ", ".join(f"pid {h.pid} uid {h.uid}" for h in tenants[:4])
        if not scan.complete:
            return "holder scan incomplete"
        return None

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

    def _alert_once(self, kind: str, msg: str, key: str = "", **fields) -> None:
        """Log ALERT and run the hook on the first lap of an episode; later laps stay quiet.
        ``key`` names the episode when one kind has several (one per poller unit)."""
        key = key or kind
        if key in self.state["open"]:
            log(f"still: {msg}")
            return
        self.state["open"].append(key)
        log(f"ALERT {msg}")
        self._send(kind, msg, **fields)

    def _clear(self, key: str) -> None:
        if key in self.state["open"]:
            self.state["open"].remove(key)

    def _send(self, kind: str, msg: str, **fields) -> None:
        event = {"kind": kind, "host": socket.gethostname(), "detail": msg, **fields}
        t = alert.send_alert(event)
        if t is not None:
            t.join(alert.alert_timeout_sec() + 5)  # a oneshot must not exit under its own hook

    def idle_why(self) -> Optional[str]:
        """Why the probe must stand back: the broker's own verdict, then a device op its inhibit
        file names, then a tenant holding the device outside the broker's job table."""
        why = idle_verdict(self.health())
        if why:
            return why
        op = self.device_op_lock()
        if op:
            return f"broker device op in flight ({op})"
        holders = self.tenant_holders()
        if holders:
            return f"device held outside the job table ({holders})"
        return None

    def _still_idle(self) -> bool:
        why = self.idle_why()
        if why:
            log(f"stopped: {why}")
        return why is None

    # --- the lap ----------------------------------------------------------------------------
    def run(self) -> int:
        for k, v in self.broker_env().items():
            os.environ.setdefault(k, v)  # first: the inhibit file's path is the broker's
        why = self.idle_why()
        if why:
            log(f"skip: {why}")
            return 0
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
        pollers = []
        for unit in poller_units():
            st = self._check_poller(unit)
            if st is None:
                return
            pollers.append(f"{unit.removesuffix('.service')}={st}")
        aer = self._check_aer()
        if not self.state["open"]:
            log(f"ok {self.present()}/{expected} chips {' '.join(pollers)} aer={aer}")

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
            if self.rescan() is False:
                log("rescan skipped: a broker device op holds the device-op flock")
                return False
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

    def _check_poller(self, unit: str) -> Optional[str]:
        """Restart a stopped, enabled poller. Its state, 'absent' when it is not installed, or
        None when the broker stopped being idle. The episode (spec 08 I17) opens when the probe
        first has to act and closes after POLLER_OK_LAPS active laps in a row; it pages once,
        when a start leaves the unit down, when it went down again inside the episode, or when
        it stays 'activating' for POLLER_ACTIVATING_LAPS laps."""
        key = f"health_probe_telemetry_down:{unit}"
        rec = self.state.setdefault("pollers", {})
        if self.systemctl("show", "-p", "LoadState", "--value", unit) in ("", "not-found"):
            rec.pop(unit, None)
            self._clear(key)
            return "absent"
        st = rec.get(unit)
        state = self.systemctl("is-active", unit) or "unknown"
        if state == "active":
            if st is not None:
                st["ok_laps"] = st.get("ok_laps", 0) + 1
                st["activating_laps"] = 0
                if st["ok_laps"] >= POLLER_OK_LAPS:
                    log(f"{unit} stayed up {st['ok_laps']} laps: episode closed")
                    rec.pop(unit, None)
                    self._clear(key)
            return state
        if state == "activating":
            st = rec.setdefault(unit, {"restarts": 0})
            st["ok_laps"] = 0
            st["activating_laps"] = st.get("activating_laps", 0) + 1
            if st["activating_laps"] >= POLLER_ACTIVATING_LAPS:
                self._alert_once(
                    "health_probe_telemetry_down",
                    f"{unit} still activating after {st['activating_laps']} laps",
                    key=key,
                    unit=unit,
                    state=state,
                )
            else:
                log(f"{unit} activating: left to systemd")
            return state
        if state not in POLLER_RESTARTABLE:
            log(f"{unit} {state}: left alone")
            return state
        enabled = self.systemctl("is-enabled", unit) or "unknown"
        if enabled not in POLLER_ENABLED:
            # Stopped and disabled (or masked) is an operator's or a co-tenant's choice to keep it off.
            if st is not None:
                rec.pop(unit, None)
                self._clear(key)
            log(f"{unit} {state} and {enabled}: left off")
            return state
        if not self._still_idle():
            return None
        st = rec.setdefault(unit, {"restarts": 0})
        log(f"{unit} {state}, clearing its start limit and starting it")
        self.systemctl("reset-failed", unit)
        self.systemctl("start", unit)
        self.sleep(TELEMETRY_SETTLE_SEC)
        # The broker may have begun a reset while the unit started: it quiesces the pollers first,
        # so one the probe just started would be polling a bus the broker is about to reset.
        why = device_verdict(self.health())
        op = None if why else self.device_op_lock()
        if why or op:
            why = why or f"broker device op in flight ({op})"
            log(f"stopped {unit} again: {why}")
            self.systemctl("stop", unit)
            return None
        st["restarts"] = st.get("restarts", 0) + 1
        st["ok_laps"] = 0
        st["activating_laps"] = 0
        after = self.systemctl("is-active", unit) or "unknown"
        if after == "active" and st["restarts"] == 1:
            log(f"{unit} recovered")
            st["ok_laps"] = 1
        elif after == "activating" and st["restarts"] == 1:
            log(f"{unit} starting")
        else:
            again = f", start {st['restarts']} this episode" if st["restarts"] > 1 else ""
            self._alert_once(
                "health_probe_telemetry_down",
                f"{unit} {after} after a restart{again}: it cannot open the chips or keeps exiting",
                key=key,
                unit=unit,
                state=after,
                restarts=st["restarts"],
            )
            if after == "active":
                st["ok_laps"] = 1
        return after

    def _check_aer(self) -> int:
        """Alert when the chips' fatal/non-fatal AER counters grew since the last alert."""
        total = self.aer_total()
        if "aer_seen" not in self.state:
            # First lap of this boot: what is already counted happened before the probe watched,
            # and paging it all at once on deploy would be noise. Only growth pages.
            self.state["aer_seen"] = total
            if total:
                log(f"AER count {total} at the first lap of this boot: baseline, not paged")
            return total
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


def device_op_inhibit() -> Path:
    path = os.environ.get("TT_DEVICE_MCP_DEVICE_OP_LOCK", "").strip()
    return Path(path or DEFAULT_DEVICE_OP_LOCK)


class device_op_flock:
    """``with device_op_flock() as free:`` holds the broker's device-op flock for one short step,
    taken non-blocking (spec 06 I8 external-tool contract): opened fresh, never created, released
    on close. ``free`` is False while a broker device op holds it, True once taken, and True when
    no lock is on offer (no file yet, an older broker), as the contract says."""

    def __enter__(self) -> bool:
        explicit = os.environ.get("TT_DEVICE_MCP_DEVICE_OP_FLOCK", "").strip()
        path = Path(explicit) if explicit else device_op_inhibit().with_name(DEVICE_OP_FLOCK_NAME)
        self.fd = -1
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
        return True

    def __exit__(self, *exc) -> None:
        if self.fd >= 0:
            os.close(self.fd)


def poller_units() -> list[str]:
    """The pollers to keep running: the broker's TT_DEVICE_MCP_POLLER_SERVICES, same default."""
    raw = os.environ.get("TT_DEVICE_MCP_POLLER_SERVICES", DEFAULT_POLLER_SERVICES)
    return [u.strip() for u in raw.split(",") if u.strip()]


def main() -> int:
    return Probe().run()


if __name__ == "__main__":
    raise SystemExit(main())
