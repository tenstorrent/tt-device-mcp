# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The idle-time device-health probe (spec 08 I17). Every host touch is stubbed: a fake root
tree for /dev, /sys and /proc, and a recorded systemctl."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.deploy_helpers import _one, _statements
from tt_device_mcp import alert
from tt_device_mcp import health_probe as hp

APPLY = Path(__file__).resolve().parent.parent / "deploy" / "apply-host-config.sh"
IDLE = {
    "status": "ok",
    "running": 0,
    "queued": 0,
    "held": False,
    "fsm_state": "healthy",
    "fsm_why": "",
    "device_degraded": None,
}


class FakeProbe(hp.Probe):
    """A Probe on a temp root with scripted /health answers and a recorded systemctl."""

    def __init__(self, root: Path, chips: int, health=None):
        super().__init__(root)
        self.dev_dir.mkdir(parents=True)
        self.pci_dir.mkdir(parents=True)
        self.boot_id_path.parent.mkdir(parents=True)
        self.boot_id_path.write_text("boot-a\n")
        self.set_chips(chips)
        self.answers = list(health or [IDLE])
        self.calls: list[tuple] = []
        self.units = {"tt-telemetry.service": "active"}  # installed units; any other is not-found
        self.enabled: dict = {}  # is-enabled per unit, "enabled" when unlisted
        self.load_state = "loaded"
        self.op = None  # what the broker's inhibit file names
        self.op_after_start = None  # ...once a poller start has run
        self.tenants = None  # tenant holders outside the job table
        self.main_pid = "0"
        self.rescans = 0
        self.after_rescan = None
        self.slept: list[float] = []
        self.sleep = self.slept.append
        self.clock = 10_000.0
        self.now = lambda: self.clock

    def set_chips(self, n: int) -> None:
        for p in self.dev_dir.iterdir():
            p.unlink()
        for i in range(n):
            (self.dev_dir / str(i)).touch()

    def health(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def systemctl(self, *args):
        self.calls.append(args)
        if args[:2] == ("show", "-p") and args[2] == "LoadState":
            return self.load_state if args[4] in self.units else "not-found"
        if args[:2] == ("show", "-p") and args[2] == "MainPID":
            return self.main_pid
        if args[0] == "is-active":
            return self.units.get(args[1], "inactive")
        if args[0] == "is-enabled":
            return self.enabled.get(args[1], "enabled")
        if args[0] == "start" and self.op_after_start:
            self.op = self.op_after_start
        return ""

    def device_op_lock(self):
        return self.op

    def tenant_holders(self):
        return self.tenants

    def rescan(self):
        self.rescans += 1
        if self.after_rescan is not None:
            self.set_chips(self.after_rescan)

    def mutations(self):
        return [c for c in self.calls if c[0] in ("start", "reset-failed", "restart", "stop")]


@pytest.fixture
def hdir(tmp_path, monkeypatch):
    d = tmp_path / "health"
    for k in hp.INHERITED_ENV:  # set then drop, so whatever run() inherits is undone after the test
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_DIR", str(d))
    return d


@pytest.fixture
def sent(monkeypatch):
    events = []
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: events.append(event))
    return events


def _baseline(hdir: Path, chips: int) -> None:
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / "chip_baseline.json").write_text(json.dumps({"chips": chips}))


# --- skip / idle ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "health, needle",
    [
        (None, "did not answer"),
        ({"error": "connection refused"}, "connection refused"),
        ({**IDLE, "held": True, "held_reason": "eth fault"}, "holds the device (eth fault)"),
        ({**IDLE, "fsm_state": "recovering", "fsm_why": "reset"}, "'recovering'"),
        ({**IDLE, "fsm_state": "boot"}, "'boot'"),
        ({**IDLE, "device_degraded": "pcie_reset"}, "recovery in flight"),
        ({**IDLE, "running": 1}, "1 running"),
        ({**IDLE, "queued": 2}, "2 queued"),
        ({k: v for k, v in IDLE.items() if k != "running"}, "no job counts"),
    ],
)
def test_idle_verdict_requires_an_idle_healthy_broker(health, needle):
    why = hp.idle_verdict(health)
    assert why is not None and needle in why
    assert hp.idle_verdict(dict(IDLE)) is None


@pytest.mark.parametrize("busy", [{**IDLE, "running": 1}, {**IDLE, "held": True}, None])
def test_a_busy_broker_is_left_alone(tmp_path, hdir, sent, busy):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=4, health=[busy])
    p.units["tt-telemetry.service"] = "failed"
    assert p.run() == 0
    assert p.rescans == 0 and p.mutations() == [] and sent == []
    assert not (hdir / hp.STATE_FILE).exists()  # it did not even open its state


def test_a_broker_turning_busy_stops_the_rescan(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=4, health=[IDLE, {**IDLE, "running": 1}])
    p.units["tt-telemetry.service"] = "failed"
    p.run()
    assert p.rescans == 0 and p.mutations() == [] and sent == []


def test_a_broker_turning_busy_stops_the_telemetry_restart(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8, health=[IDLE, {**IDLE, "held": True}])
    p.units["tt-telemetry.service"] = "failed"
    p.run()
    assert p.mutations() == [] and sent == []


# --- chip count -------------------------------------------------------------------------------


def test_expected_chips_reads_the_broker_count(tmp_path, hdir, monkeypatch):
    assert hp.expected_chips(4, tmp_path) == 4  # no baseline yet: what is present
    _baseline(tmp_path, 32)
    assert hp.expected_chips(30, tmp_path) == 32  # the broker's high-water mark
    assert hp.expected_chips(33, tmp_path) == 33
    (tmp_path / "chip_baseline.json").write_text("{not json")
    assert hp.expected_chips(0, tmp_path) == 1  # unreadable baseline fails closed
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "8")
    assert hp.expected_chips(30, tmp_path) == 8  # the explicit count wins
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "zero")
    assert hp.expected_chips(30, tmp_path) == 30


def test_the_probe_never_writes_the_baseline(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    before = (hdir / "chip_baseline.json").read_text()
    p = FakeProbe(tmp_path, chips=32)
    p.run()
    assert (hdir / "chip_baseline.json").read_text() == before


def test_present_counts_numeric_device_nodes_only(tmp_path):
    p = FakeProbe(tmp_path, chips=3)
    (p.dev_dir / "by-id").mkdir()
    assert p.present() == 3


# --- PCI rescan -------------------------------------------------------------------------------


def test_rescan_recovers_without_alert(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=6)
    p.after_rescan = 8
    p.run()
    assert p.rescans == 1 and hp.RESCAN_SETTLE_SEC in p.slept
    assert sent == []
    state = json.loads((hdir / hp.STATE_FILE).read_text())
    assert state["open"] == [] and "last_rescan" not in state


def test_missing_chips_rescan_then_alert_once(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=6)
    p.run()
    assert p.rescans == 1
    assert [e["kind"] for e in sent] == ["health_probe_chips_missing"]
    assert sent[0]["present"] == 6 and sent[0]["expected"] == 8 and sent[0]["host"]

    # Next laps inside the gap: no rescan, no second page.
    p.clock += 120
    p.run()
    assert p.rescans == 1 and len(sent) == 1
    # Past the gap: one more rescan, still the same episode, still no second page.
    p.clock += hp.RESCAN_MIN_GAP_SEC
    p.run()
    assert p.rescans == 2 and len(sent) == 1
    # The chips come back: the episode closes, and a new drop pages again.
    p.set_chips(8)
    p.run()
    assert json.loads((hdir / hp.STATE_FILE).read_text())["open"] == []
    p.set_chips(7)
    p.clock += 10
    p.run()
    assert p.rescans == 3 and len(sent) == 2


# --- telemetry --------------------------------------------------------------------------------


def test_telemetry_restart_and_alert(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    p.units["tt-telemetry.service"] = "failed"
    p.run()
    assert ("reset-failed", "tt-telemetry.service") in p.calls
    assert ("start", "tt-telemetry.service") in p.calls
    assert [e["kind"] for e in sent] == ["health_probe_telemetry_down"]
    assert sent[0]["state"] == "failed"
    p.run()  # still down: restarted again, not paged again
    assert p.calls.count(("start", "tt-telemetry.service")) == 2 and len(sent) == 1


def test_telemetry_restart_that_works_does_not_page(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    p.units["tt-telemetry.service"] = "inactive"
    orig = p.systemctl

    def systemctl(*args):
        if args[0] == "start":
            p.units["tt-telemetry.service"] = "active"
        return orig(*args)

    p.systemctl = systemctl
    p.run()
    assert ("start", "tt-telemetry.service") in p.calls and sent == []


def test_a_host_without_telemetry_is_skipped(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    p.load_state = "not-found"
    p.run()
    assert p.mutations() == [] and sent == []


def _starts_to(p: FakeProbe, unit: str, after: str) -> None:
    """A start of ``unit`` leaves it in state ``after``."""
    orig = p.systemctl

    def systemctl(*args):
        if args[:2] == ("start", unit):
            p.units[unit] = after
        return orig(*args)

    p.systemctl = systemctl


@pytest.mark.parametrize("enabled", ["disabled", "masked"])
def test_a_poller_stopped_and_disabled_on_purpose_is_left_off(tmp_path, hdir, sent, enabled):
    p = FakeProbe(tmp_path, chips=8)
    for state in ("inactive", "failed"):
        p.units["tt-telemetry.service"] = state
        p.enabled["tt-telemetry.service"] = enabled
        p.run()
    assert p.mutations() == [] and sent == []


def test_an_activating_poller_is_left_to_systemd_then_paged_once(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    p.units["tt-telemetry.service"] = "activating"  # auto-restart, or a slow init over many chips
    for _ in range(hp.POLLER_ACTIVATING_LAPS - 1):
        p.run()
    assert p.mutations() == [] and sent == []
    for _ in range(5):
        p.run()
    assert p.mutations() == []  # never restarted: systemd is already on it
    assert [(e["kind"], e["state"]) for e in sent] == [("health_probe_telemetry_down", "activating")]


def test_a_start_that_is_still_activating_does_not_page(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    p.units["tt-telemetry.service"] = "failed"
    _starts_to(p, "tt-telemetry.service", "activating")
    p.run()
    p.units["tt-telemetry.service"] = "active"
    for _ in range(hp.POLLER_OK_LAPS):
        p.run()
    assert sent == [] and p.calls.count(("start", "tt-telemetry.service")) == 1
    assert json.loads((hdir / hp.STATE_FILE).read_text())["pollers"] == {}


def test_a_flapping_poller_pages_once_per_episode(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    _starts_to(p, "tt-telemetry.service", "active")
    for _ in range(10):  # crash loop: down on one lap, up (after the probe's start) the next
        p.units["tt-telemetry.service"] = "failed"
        p.run()
        p.run()
    assert p.calls.count(("start", "tt-telemetry.service")) == 10
    assert [(e["kind"], e["restarts"]) for e in sent] == [("health_probe_telemetry_down", 2)]
    # Up for POLLER_OK_LAPS laps in a row closes the episode; the next fall is a new one.
    for _ in range(hp.POLLER_OK_LAPS):
        p.run()
    assert json.loads((hdir / hp.STATE_FILE).read_text())["open"] == []
    for _ in range(2):
        p.units["tt-telemetry.service"] = "failed"
        p.run()
    assert len(sent) == 2


def test_a_poller_that_stays_up_between_rare_falls_is_restarted_quietly(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    _starts_to(p, "tt-telemetry.service", "active")
    for _ in range(3):
        p.units["tt-telemetry.service"] = "failed"
        p.run()
        for _ in range(hp.POLLER_OK_LAPS):
            p.run()
    assert p.calls.count(("start", "tt-telemetry.service")) == 3 and sent == []


def test_the_pollers_are_the_broker_list(tmp_path, hdir, sent, monkeypatch):
    p = FakeProbe(tmp_path, chips=8)
    p.units = {"tt-telemetry.service": "active", "tt-metrics-exporter.service": "failed"}
    p.run()  # the broker's default list has both
    assert ("start", "tt-metrics-exporter.service") in p.calls
    monkeypatch.setenv("TT_DEVICE_MCP_POLLER_SERVICES", "my-poller.service")
    p.units = {"my-poller.service": "inactive", "tt-telemetry.service": "failed"}
    p.calls.clear()
    p.run()
    assert p.mutations() == [("reset-failed", "my-poller.service"), ("start", "my-poller.service")]


def test_the_poller_list_comes_from_the_broker_env(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    p.units = {"my-poller.service": "failed", "tt-telemetry.service": "failed"}
    p.main_pid = "7"
    (p.proc_dir / "7").mkdir()
    (p.proc_dir / "7" / "environ").write_bytes(b"TT_DEVICE_MCP_POLLER_SERVICES=my-poller.service\0")
    p.run()
    assert p.mutations() == [("reset-failed", "my-poller.service"), ("start", "my-poller.service")]


# --- standing back: the inhibit file, holders outside the job table, the start/settle gap ----


def test_a_device_op_in_flight_stops_the_lap(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=4)
    p.units["tt-telemetry.service"] = "failed"
    p.op = "galaxy_reset"
    p.run()
    assert p.rescans == 0 and p.mutations() == [] and sent == []


def test_a_tenant_holding_the_device_outside_the_job_table_stops_the_lap(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=4)
    p.units["tt-telemetry.service"] = "failed"
    p.tenants = "pid 4321 uid 1001"  # a Slurm step, or a hung job's leftovers
    p.run()
    assert p.rescans == 0 and p.mutations() == [] and sent == []


@pytest.mark.parametrize(
    "turn",
    [
        {"op_after_start": "galaxy_reset"},
        {"health": [IDLE, IDLE, {**IDLE, "device_degraded": "pcie_reset"}]},
        {"health": [IDLE, IDLE, {**IDLE, "fsm_state": "recovering"}]},
    ],
)
def test_a_device_op_that_starts_during_the_settle_stops_the_poller_again(tmp_path, hdir, sent, turn):
    p = FakeProbe(tmp_path, chips=8, health=turn.get("health"))
    p.op_after_start = turn.get("op_after_start")
    p.units["tt-telemetry.service"] = "failed"
    _starts_to(p, "tt-telemetry.service", "active")
    p.run()
    assert p.mutations() == [
        ("reset-failed", "tt-telemetry.service"),
        ("start", "tt-telemetry.service"),
        ("stop", "tt-telemetry.service"),
    ]
    assert sent == []


def test_a_job_starting_during_the_settle_keeps_the_poller(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8, health=[IDLE, IDLE, {**IDLE, "running": 1}])
    p.units["tt-telemetry.service"] = "failed"
    _starts_to(p, "tt-telemetry.service", "active")
    p.run()
    assert ("stop", "tt-telemetry.service") not in p.calls  # pollers run beside jobs


def test_device_op_lock_reads_the_broker_inhibit_file(tmp_path, monkeypatch):
    p = hp.Probe(tmp_path)
    lock = tmp_path / "device-op.lock"
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(lock))
    assert p.device_op_lock() is None  # no file: no op
    (tmp_path / "proc" / "77").mkdir(parents=True)
    lock.write_text("77 galaxy_reset\n")
    assert p.device_op_lock() == "galaxy_reset"
    lock.write_text("78 galaxy_reset\n")
    assert p.device_op_lock() is None  # a killed broker's leftover


def test_the_broker_device_op_flock_is_tested_and_the_rescan_written_under_it(tmp_path, monkeypatch):
    import fcntl

    p = hp.Probe(tmp_path)
    p.rescan_path.parent.mkdir(parents=True)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(tmp_path / "device-op.lock"))
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", raising=False)
    flock = tmp_path / "device-op.flock"  # the broker's default: a sibling of the inhibit file
    assert p.device_op_lock() is None and p.rescan() is True  # no lock on offer (older broker)
    assert p.rescan_path.read_text() == "1"
    p.rescan_path.unlink()
    flock.touch()
    held = os.open(flock, os.O_RDONLY)
    fcntl.flock(held, fcntl.LOCK_EX)  # a broker device op in flight
    assert p.device_op_lock() == "unnamed (device-op flock held)"
    assert p.rescan() is False and not p.rescan_path.exists()
    (tmp_path / "device-op.lock").write_text("999999 galaxy_reset\n")  # a killed broker's file
    assert p.device_op_lock() == "unnamed (device-op flock held)"  # the flock still counts
    os.close(held)
    assert p.device_op_lock() is None and p.rescan() is True
    assert not hp.Probe(tmp_path).device_op_lock()  # testing it never keeps it


def test_a_rescan_the_flock_refused_is_retried_next_lap(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=4)
    p.rescan = lambda: False
    p.run()
    assert "last_rescan" not in json.loads((hdir / hp.STATE_FILE).read_text()) and sent == []


def test_tenant_holders_fail_closed(monkeypatch):
    from tt_device_mcp.device_holders import DeviceHolder, HolderScan

    p = hp.Probe()
    scans = [
        HolderScan(holders=[DeviceHolder(pid=10, uid=0), DeviceHolder(pid=11, uid=998)]),
        HolderScan(holders=[DeviceHolder(pid=12, uid=1001)]),
        HolderScan(complete=False),
    ]
    monkeypatch.setattr(hp, "enumerate_device_holders", lambda: scans.pop(0))
    assert p.tenant_holders() is None  # root and system accounts (the pollers) are not tenants
    assert "pid 12 uid 1001" in p.tenant_holders()
    assert p.tenant_holders() == "holder scan incomplete"


# --- AER --------------------------------------------------------------------------------------


def _pci(p: FakeProbe, name: str, vendor: str, fatal: int) -> None:
    d = p.pci_dir / name
    d.mkdir(exist_ok=True)
    (d / "vendor").write_text(vendor + "\n")
    (d / "aer_dev_fatal").write_text(f"Undefined 0\nTOTAL_ERR_FATAL {fatal}\n")
    (d / "aer_dev_nonfatal").write_text("TOTAL_ERR_NONFATAL 0\n")


def test_aer_growth_alerts_once_an_hour(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    _pci(p, "0000:01:00.0", "0x1e52", 0)
    _pci(p, "0000:02:00.0", "0x8086", 50)  # not a Tenstorrent device: ignored
    p.run()  # the first lap of the boot sets the baseline
    _pci(p, "0000:01:00.0", "0x1e52", 2)
    p.run()
    assert [(e["kind"], e["aer_total"]) for e in sent] == [("health_probe_aer", 2)]
    p.run()  # no growth
    assert len(sent) == 1
    _pci(p, "0000:01:00.0", "0x1e52", 5)
    p.clock += 600
    p.run()  # grew, but inside the hour
    assert len(sent) == 1
    p.clock += hp.AER_ALERT_MIN_GAP_SEC
    p.run()
    assert [e["aer_new"] for e in sent] == [2, 3]


def test_aer_already_counted_at_the_first_lap_is_a_baseline_not_a_page(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    _pci(p, "0000:01:00.0", "0x1e52", 40)  # errors from before the probe watched (e.g. the deploy)
    p.run()
    assert sent == []
    assert json.loads((hdir / hp.STATE_FILE).read_text())["aer_seen"] == 40
    _pci(p, "0000:01:00.0", "0x1e52", 41)
    p.run()
    assert [(e["aer_total"], e["aer_new"]) for e in sent] == [(41, 1)]
    p.boot_id_path.write_text("boot-b\n")  # a new boot sets a new baseline
    _pci(p, "0000:01:00.0", "0x1e52", 3)
    p.run()
    assert len(sent) == 1


def test_a_reboot_resets_episodes_and_counters(tmp_path, hdir, sent):
    _baseline(hdir, 8)
    p = FakeProbe(tmp_path, chips=6)
    p.run()
    assert len(sent) == 1
    p.boot_id_path.write_text("boot-b\n")
    p.run()
    assert len(sent) == 2  # a new boot is a new episode


# --- the hook and the broker's environment ----------------------------------------------------


def test_hook_and_chip_count_come_from_the_broker_env(tmp_path, hdir, monkeypatch):
    out = tmp_path / "alert.json"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ncat > {out}\n")
    hook.chmod(0o755)
    p = FakeProbe(tmp_path, chips=6)
    p.main_pid = "4242"
    (p.proc_dir / "4242").mkdir()
    (p.proc_dir / "4242" / "environ").write_bytes(
        b"PATH=/usr/bin\0TT_DEVICE_MCP_ALERT_CMD=" + str(hook).encode() + b"\0TT_DEVICE_MCP_EXPECTED_CHIPS=8\0"
    )
    p.run()
    event = json.loads(out.read_text())  # the real hook ran, and the probe waited for it
    assert event["kind"] == "health_probe_chips_missing"
    assert event["expected"] == 8 and event["present"] == 6


def test_broker_env_keeps_only_the_inherited_keys(tmp_path, hdir):
    p = FakeProbe(tmp_path, chips=1)
    assert p.broker_env() == {}  # broker not running
    p.main_pid = "7"
    (p.proc_dir / "7").mkdir()
    (p.proc_dir / "7" / "environ").write_bytes(b"SECRET=x\0TT_DEVICE_MCP_HEALTH_DIR=/h\0TT_DEVICE_MCP_ALERT_CMD=a b\0")
    assert p.broker_env() == {"TT_DEVICE_MCP_HEALTH_DIR": "/h", "TT_DEVICE_MCP_ALERT_CMD": "a b"}


def test_the_unit_setting_beats_the_broker_env(tmp_path, hdir, sent, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "6")
    p = FakeProbe(tmp_path, chips=6)
    p.main_pid = "7"
    (p.proc_dir / "7").mkdir()
    (p.proc_dir / "7" / "environ").write_bytes(b"TT_DEVICE_MCP_EXPECTED_CHIPS=8\0")
    p.run()
    assert p.rescans == 0 and sent == []


# --- deploy -----------------------------------------------------------------------------------


def test_apply_installs_and_arms_the_probe_optionally():
    lines = _statements(APPLY)
    install = lines.index(_one(APPLY, "install_health_probe_units /etc/systemd/system"))
    reload_ = next(i for i, s in enumerate(lines) if i > install and s.strip() == "systemctl daemon-reload")
    arm = lines.index(_one(APPLY, "arm_health_probe /etc/systemd/system"))
    fabric = lines.index(_one(APPLY, "is-active --quiet tt-device-fabric-validator.timer"))
    assert install < reload_ < fabric < arm  # arming never comes before the required validator
    assert "rm -f" not in _shell_fn("arm_health_probe")  # the old probe is moved aside, never deleted


def _shell_fn(name: str) -> str:
    """One shell function from apply-host-config.sh, to run alone against a sandbox."""
    m = re.search(rf"^{name}\(\) {{\n.*?^}}\n", APPLY.read_text(), re.S | re.M)
    assert m, f"{name}() not found in {APPLY.name}"
    return m.group(0)


def _arm(tmp_path, *, opt_out: bool = False, arms: bool = True, old: bool = True):
    """Run arm_health_probe on a sandbox unit dir with a recorded systemctl."""
    units = tmp_path / "units"
    units.mkdir()
    if old:
        (units / "tt-health-probe.timer").write_text("old timer")
        (units / "tt-health-probe.service").write_text("old service")
    log = tmp_path / "systemctl.log"
    log.touch()
    script = f"""set -euo pipefail
systemctl() {{
    echo "$*" >> {log}
    [ "$*" = "is-active --quiet tt-device-health-probe.timer" ] && return {0 if arms else 3}
    return 0
}}
{_shell_fn("arm_health_probe")}
arm_health_probe {units}
"""
    env = {"PATH": os.environ["PATH"], **({"TTDEV_HEALTH_PROBE": "0"} if opt_out else {})}
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return units, log.read_text().splitlines(), r.stderr


def test_an_armed_probe_moves_the_old_timer_aside(tmp_path):
    units, calls, err = _arm(tmp_path)
    assert calls.index("enable --now tt-device-health-probe.timer") < calls.index("disable --now tt-health-probe.timer")
    assert calls[-1] == "daemon-reload"
    assert sorted(p.name for p in units.iterdir()) == [
        "tt-health-probe.service.retired-by-tt-device-mcp",
        "tt-health-probe.timer.retired-by-tt-device-mcp",
    ]
    assert (units / "tt-health-probe.timer.retired-by-tt-device-mcp").read_text() == "old timer"
    assert "retiring" in err


def test_an_opted_out_host_keeps_the_old_timer(tmp_path):
    units, calls, err = _arm(tmp_path, opt_out=True)
    assert calls == ["disable --now tt-device-health-probe.timer"]
    assert sorted(p.name for p in units.iterdir()) == ["tt-health-probe.service", "tt-health-probe.timer"]


def test_a_probe_that_will_not_arm_keeps_the_old_timer(tmp_path):
    units, calls, err = _arm(tmp_path, arms=False)
    assert "disable --now tt-health-probe.timer" not in calls
    assert sorted(p.name for p in units.iterdir()) == ["tt-health-probe.service", "tt-health-probe.timer"]
    assert "warn:" in err and "did not arm" in err


def test_a_host_without_the_old_timer_only_arms(tmp_path):
    units, calls, err = _arm(tmp_path, old=False)
    assert calls == ["enable --now tt-device-health-probe.timer", "is-active --quiet tt-device-health-probe.timer"]
    assert list(units.iterdir()) == []


def test_the_probe_unit_runs_the_broker_venv(tmp_path):
    script = f"""set -euo pipefail
VENV=/srv/broker/venv
DEPLOY={APPLY.parent}
{_shell_fn("install_health_probe_units")}
install_health_probe_units {tmp_path}
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    svc = (tmp_path / "tt-device-health-probe.service").read_text()
    assert "ExecStart=/srv/broker/venv/bin/python -m tt_device_mcp.health_probe" in svc
    assert "@VENV@" not in svc.replace("# @VENV@", "")
    assert (tmp_path / "tt-device-health-probe.timer").exists()


def test_the_probe_unit_runs_the_module_as_a_oneshot():
    deploy = APPLY.parent
    svc = (deploy / "tt-device-health-probe.service").read_text()
    assert "Type=oneshot" in svc and "-m tt_device_mcp.health_probe" in svc
    timer = (deploy / "tt-device-health-probe.timer").read_text()
    assert "OnUnitActiveSec=2min" in timer and "WantedBy=timers.target" in timer
