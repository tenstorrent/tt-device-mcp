# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The idle-time device-health probe (spec 08 I17). Every host touch is stubbed: a fake root
tree for /dev, /sys and /proc, and a recorded systemctl."""

import json
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
        self.units = {"tt-telemetry.service": "active"}
        self.load_state = "loaded"
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
            return self.load_state
        if args[:2] == ("show", "-p") and args[2] == "MainPID":
            return self.main_pid
        if args[0] == "is-active":
            return self.units.get(args[1], "inactive")
        return ""

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


# --- AER --------------------------------------------------------------------------------------


def _pci(p: FakeProbe, name: str, vendor: str, fatal: int) -> None:
    d = p.pci_dir / name
    d.mkdir(exist_ok=True)
    (d / "vendor").write_text(vendor + "\n")
    (d / "aer_dev_fatal").write_text(f"Undefined 0\nTOTAL_ERR_FATAL {fatal}\n")
    (d / "aer_dev_nonfatal").write_text("TOTAL_ERR_NONFATAL 0\n")


def test_aer_growth_alerts_once_an_hour(tmp_path, hdir, sent):
    p = FakeProbe(tmp_path, chips=8)
    _pci(p, "0000:01:00.0", "0x1e52", 2)
    _pci(p, "0000:02:00.0", "0x8086", 50)  # not a Tenstorrent device: ignored
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
    install = lines.index(_one(APPLY, "install", "tt-device-health-probe.timer", "/etc/systemd/system/"))
    reload_ = next(i for i, s in enumerate(lines) if s.strip() == "systemctl daemon-reload")
    retire = lines.index(_one(APPLY, "disable --now tt-health-probe.timer"))
    arm = _one(APPLY, "enable --now tt-device-health-probe.timer")
    assert install < reload_ and retire < reload_ and lines.index(arm) > reload_
    assert "is-active --quiet tt-device-health-probe.timer" in arm
    then = lines[lines.index(arm) + 1]
    assert "warn:" in then and "tt-device-health-probe.timer did not arm" in then
    assert "exit" not in arm + then  # optional: a timer that will not arm never aborts the apply
    assert "TTDEV_HEALTH_PROBE:-1" in _one(APPLY, "if [", "TTDEV_HEALTH_PROBE")
    assert _one(APPLY, "disable --now tt-device-health-probe.timer")
    assert "rm -f /etc/systemd/system/tt-health-probe.timer" in _one(APPLY, "rm -f", "tt-health-probe.timer")


def test_the_probe_unit_runs_the_module_as_a_oneshot():
    deploy = APPLY.parent
    svc = (deploy / "tt-device-health-probe.service").read_text()
    assert "Type=oneshot" in svc and "-m tt_device_mcp.health_probe" in svc
    timer = (deploy / "tt-device-health-probe.timer").read_text()
    assert "OnUnitActiveSec=2min" in timer and "WantedBy=timers.target" in timer
