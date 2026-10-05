# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The tray-down fast path (spec 04 I18, issue #26).

Four or more chips off ONE physical tray at the first sighting of an episode is a tray that lost
power: capture the BMC/CPLD state, rescan the bus once, and power-cycle through the usual guard —
no SBR, tray re-power, mesh reset or last-chance sweep. Anything else keeps the ladder.
"""

import json
import pathlib
import subprocess

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health import bmc_capture
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import galaxy

# The kernel's chip index -> PCI address on a Blackhole Galaxy (as tests/test_ubb_tray_map.py).
BH_CHIP_BUSES = {
    chip: f"0000:{group + n:02x}:00.0"
    for base, group in ((0, 0x00), (8, 0x40), (16, 0xC0), (24, 0x80))
    for n, chip in enumerate(range(base, base + 8), start=1)
}
REPLAY = pathlib.Path(__file__).parent / "fixtures" / "tray_down_replay.tsv"


def _off(tray_map, spec: dict) -> set:
    """The first ``n`` chips of each tray in ``{tray: n}``."""
    return {c for t, n in spec.items() for c in sorted(tray_map[t])[:n]}


@pytest.fixture
def bh_map():
    return galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-bh")


# ---- classification -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, fast",
    [
        ({3: 4}, True),
        ({3: 3}, False),
        ({1: 2, 2: 2}, False),
        ({1: 8, 2: 1}, True),
        ({4: 1}, False),
    ],
)
def test_a_tray_with_four_or_more_chips_off_is_a_tray_down_onset(bh_map, spec, fast):
    onset = galaxy.tray_down_onset(_off(bh_map, spec), BH_CHIP_BUSES, "tt-galaxy-bh")
    assert (onset is not None) is fast
    if fast:
        assert onset == dict(sorted(spec.items()))


def test_no_map_or_an_unplaced_chip_never_classifies_fast(bh_map):
    off = _off(bh_map, {3: 8})
    assert galaxy.tray_down_onset(off, {}, "tt-galaxy-bh") is None
    assert galaxy.tray_down_onset(off, BH_CHIP_BUSES, "tt-galaxy-wh-unknown") is None
    partial = {c: a for c, a in BH_CHIP_BUSES.items() if c != min(off)}
    assert galaxy.tray_down_onset(off, partial, "tt-galaxy-bh") is None, "a chip the map cannot place declines"
    assert galaxy.tray_down_onset(set(), BH_CHIP_BUSES, "tt-galaxy-bh") is None


def _replay_rows():
    rows = []
    for line in REPLAY.read_text().splitlines():
        if line.startswith("#") or line.startswith("off_per_tray"):
            continue
        per_tray, outcome = line.split("\t")
        rows.append((per_tray, outcome))
    return rows


def test_replay_every_recoverable_episode_keeps_the_ladder(bh_map):
    """Every recorded onset, anonymised to its per-tray counts and outcome. No episode that came back
    without a power cycle (NO-BOOT) may take the fast path, and every FAST onset needed the cycle."""
    fast = ladder = no_boot = 0
    for per_tray, outcome in _replay_rows():
        if per_tray.startswith("count="):
            off = set()  # the log named no chip ids: nothing to place on a tray
        else:
            spec = {int(t[1:]): int(n) for t, n in (p.split(":") for p in per_tray.split(","))}
            off = _off(bh_map, spec)
        onset = galaxy.tray_down_onset(off, BH_CHIP_BUSES, "tt-galaxy-bh")
        if outcome == "NO-BOOT":
            no_boot += 1
            assert onset is None, f"a recoverable onset ({per_tray}) must keep the ladder"
        if onset is None:
            ladder += 1
        else:
            fast += 1
            assert outcome == "BOOT", f"a FAST onset ({per_tray}) that recovered without a cycle"
    assert (fast, ladder, no_boot) == (142, 80, 72)


# ---- the gate/idle/watchdog wiring --------------------------------------------------------------


@pytest.fixture
def rig(monkeypatch, galaxy_trays):
    """srv.galaxy_recovery with every reset rung a tripwire, the capture and power cycle recorded,
    and the bus read from ``rig['beats']``."""
    g = srv.galaxy_recovery
    state = {
        "beats": {},
        "events": [],
        "cycles": 0,
        "captures": 0,
        "rescans": 0,
        "allowed": (True, ""),
        "map": galaxy_trays,
    }
    patch_health_event(monkeypatch, lambda kind, **k: state["events"].append((kind, k)))
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(state["beats"]))
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: False)
    monkeypatch.setattr(g.mechanism, "auto_recovery_allowed", lambda action, tenant_active=False, **k: state["allowed"])
    monkeypatch.setattr(g.mechanism, "_journal_auto_recovery_denied", lambda *a, **k: None)
    monkeypatch.setattr(galaxy, "TRAY_DOWN_RESCAN_SETTLE_SEC", 0)

    def rescan():
        state["rescans"] += 1
        if "after_rescan" in state:
            state["beats"] = state.pop("after_rescan")

    monkeypatch.setattr(galaxy, "_pci_rescan", rescan)

    def capture(onset, expected):
        state["captures"] += 1
        return {"cpld": "read"}

    monkeypatch.setattr(g, "_tray_down_capture", capture)

    async def fake_pc(log, reason):
        state["cycles"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)

    def tripwire(name):
        async def fire(*a, **k):
            raise AssertionError(f"a FAST episode reached the ladder rung {name}")

        return fire

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", tripwire("reset_with_quiesce"))
    monkeypatch.setattr(g, "_attempt_ubb_tray_reset", tripwire("_attempt_ubb_tray_reset"))
    monkeypatch.setattr(g, "_fire_tray_down_no_window", tripwire("_fire_tray_down_no_window"))
    monkeypatch.setattr(g, "_fire_gate_rung", tripwire("_fire_gate_rung"))
    monkeypatch.setattr(g, "_escalate_offbus_stuck_hold", tripwire("_escalate_offbus_stuck_hold"))
    monkeypatch.setattr(g, "_escalate_stuck_hold", tripwire("_escalate_stuck_hold"))
    monkeypatch.setattr(g, "_settle_and_verify_before_host_rung", tripwire("last-chance sweep"))
    state["g"] = g
    return state


def _beats_without(off: set) -> dict:
    return {str(i): 100 for i in range(32) if i not in off}


def _kinds(rig):
    return [k for k, _ in rig["events"]]


@pytest.mark.asyncio
async def test_a_tray_down_onset_captures_rescans_and_power_cycles_with_no_reset(rig):
    off = _off(rig["map"], {3: 8})
    rig["beats"] = _beats_without(off)
    out = await rig["g"].escalate(
        "gate/post-job", sorted(map(str, off)), 32, lambda m: None, stage=galaxy.STAGE_BRIDGE_RESET
    )
    assert out == galaxy.OUTCOME_WAITING
    assert (rig["captures"], rig["rescans"], rig["cycles"]) == (1, 1, 1)
    assert ("tray_down_latched", {"path": "FAST", "off_per_tray": {3: 8}, "expected": 32}) in rig["events"]
    assert "tray_down_fast_path" in _kinds(rig)


@pytest.mark.asyncio
async def test_a_denied_power_cycle_holds_with_zero_resets_and_the_watchdog_does_not_start_the_ladder(rig):
    off = _off(rig["map"], {2: 5})
    rig["beats"] = _beats_without(off)
    rig["allowed"] = (False, "interval not elapsed")
    g = rig["g"]
    assert await g.escalate("gate/post-job", [], 32, lambda m: None) == galaxy.OUTCOME_WAITING
    # The idle relift and the hold-deadline watchdog re-check the guard; neither reaches a rung.
    assert await g.escalate("offbus", [], 32, lambda m: None) == galaxy.OUTCOME_WAITING
    assert await g.escalate("offbus-forced", [], 32, lambda m: None) == galaxy.OUTCOME_WAITING
    assert rig["cycles"] == 0 and rig["captures"] == 1 and rig["rescans"] == 1, "capture and rescan once per episode"
    assert _kinds(rig).count("tray_down_held") == 1, "the hold is journalled once per episode"
    # The guard opens later: the next pass cycles.
    rig["allowed"] = (True, "")
    await g.escalate("offbus-forced", [], 32, lambda m: None)
    assert rig["cycles"] == 1


@pytest.mark.asyncio
async def test_every_chip_back_after_the_rescan_runs_the_full_verify(rig, monkeypatch):
    off = _off(rig["map"], {1: 8})
    rig["beats"] = _beats_without(off)
    rig["after_rescan"] = _beats_without(set())
    verified = {"n": 0}

    async def verify(expected, log, run_fabric=True, **k):
        verified["n"] += 1
        assert run_fabric, "the full verify, fabric included"
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)
    out = await rig["g"].escalate("gate/post-job", [], 32, lambda m: None)
    assert out == galaxy.OUTCOME_RECOVERED and verified["n"] == 1 and rig["cycles"] == 0
    assert rig["g"]._td is None, "a recovered episode closes the latch"


@pytest.mark.asyncio
async def test_every_tray_below_four_after_the_rescan_hands_the_episode_to_the_ladder(rig, monkeypatch):
    off = _off(rig["map"], {4: 8})
    rig["beats"] = _beats_without(off)
    rig["after_rescan"] = _beats_without(set(sorted(off)[:2]))
    g = rig["g"]
    assert await g.escalate("gate/post-job", [], 32, lambda m: None) == galaxy.OUTCOME_WAITING
    assert rig["cycles"] == 0 and g._td["path"] == "LADDER"
    ladder = {"n": 0}

    async def offbus_ladder(*a, **k):
        ladder["n"] += 1
        return False

    monkeypatch.setattr(g, "_escalate_offbus_stuck_hold", offbus_ladder)
    await g.escalate("offbus", [], 32, lambda m: None)
    assert ladder["n"] == 1


@pytest.mark.asyncio
async def test_the_latch_does_not_change_after_a_reset_and_is_fresh_after_the_episode(rig, monkeypatch):
    """A ladder reset can turn a 1-chip drop into a whole tray off: that is not an onset."""
    g = rig["g"]
    ladder = {"n": 0}

    async def offbus_ladder(*a, **k):
        ladder["n"] += 1
        rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))  # the reset knocked the tray out
        return False

    monkeypatch.setattr(g, "_escalate_offbus_stuck_hold", offbus_ladder)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    await g.escalate("offbus", [], 32, lambda m: None)
    await g.escalate("offbus", [], 32, lambda m: None)
    assert ladder["n"] == 2 and g._td["path"] == "LADDER" and rig["cycles"] == 0
    g.tray_down_episode_end()
    await g.escalate("offbus", [], 32, lambda m: None)
    assert g._td["path"] == "FAST" and rig["cycles"] == 1, "a new episode latches afresh"


@pytest.mark.asyncio
async def test_a_restarted_broker_latches_a_tray_still_missing_afresh(rig, monkeypatch):
    """The latch is not persisted: a restarted broker (a new GalaxyRecovery) has none, takes it again
    at its first sighting and captures again, but the power cycle still asks the same guard, so a
    restart buys no extra cycle and no reset."""
    g = rig["g"]
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    rig["allowed"] = (False, "interval not elapsed")
    await g.escalate("offbus", [], 32, lambda m: None)
    assert (rig["captures"], rig["cycles"]) == (1, 0) and g._td["path"] == "FAST"

    restarted = galaxy.GalaxyRecovery(g.monitor, g.mechanism, g.deps)
    assert restarted._td is None, "a restart starts with no latch"
    monkeypatch.setattr(restarted, "_tray_down_capture", g._tray_down_capture)
    for rung in (
        "_attempt_ubb_tray_reset",
        "_fire_tray_down_no_window",
        "_fire_gate_rung",
        "_escalate_offbus_stuck_hold",
    ):
        monkeypatch.setattr(restarted, rung, getattr(g, rung))
    await restarted.escalate("offbus", [], 32, lambda m: None)
    assert restarted._td["path"] == "FAST" and rig["captures"] == 2, "latched and captured afresh"
    assert rig["cycles"] == 0 and rig["rescans"] == 2, "the guard still holds the cycle; no reset fired"
    rig["allowed"] = (True, "")
    await restarted.escalate("offbus", [], 32, lambda m: None)
    assert rig["cycles"] == 1 and rig["captures"] == 2


@pytest.mark.asyncio
async def test_the_gate_takes_the_fast_path_for_a_tray_down_onset(rig, monkeypatch, tmp_path, clear_job_state):
    """End to end through server.device_health_gate: tray 3 off the bus after a job, the default
    action, no tenant. The gate's hook latches FAST, captures, rescans once and power-cycles; no reset
    rung runs (the rig's tripwires, plus the mesh-wide reset here)."""
    for i in range(24):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    srv.device_op_lock = None
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    srv.isolated_chips = set()
    srv.device_pci_map = {}
    srv.device_fault_reported = ""
    fsm_dirty(srv, "", why="gate_error")

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chips 24-31 off the bus"}}

    async def no_mesh_reset(indices, log):
        raise AssertionError("a FAST episode reached the mesh-wide reset")

    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    patch_recovery(monkeypatch, "_reset_and_verify_device", no_mesh_reset)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert (rig["captures"], rig["rescans"], rig["cycles"]) == (1, 1, 1)
    assert rig["g"]._td["path"] == "FAST"
    assert ("tray_down_latched", {"path": "FAST", "off_per_tray": {3: 8}, "expected": 32}) in rig["events"]


@pytest.mark.asyncio
async def test_a_tray_missing_at_the_first_idle_sighting_is_an_onset(rig):
    """Startup and the idle relift count as a first sighting: the bus is read when no beats are passed."""
    rig["beats"] = _beats_without(_off(rig["map"], {2: 8}))
    await rig["g"].escalate("offbus", [], 32, lambda m: None)
    assert rig["g"]._td["path"] == "FAST" and rig["cycles"] == 1


@pytest.mark.asyncio
async def test_legacy_sweep_keeps_todays_ladder(rig, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_DOWN_ACTION", "legacy_sweep")
    g = rig["g"]
    ladder = {"n": 0}

    async def offbus_ladder(*a, **k):
        ladder["n"] += 1
        return False

    monkeypatch.setattr(g, "_escalate_offbus_stuck_hold", offbus_ladder)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    await g.escalate("offbus", [], 32, lambda m: None)
    assert ladder["n"] == 1 and g._td is None and rig["cycles"] == 0


@pytest.mark.asyncio
async def test_the_whole_bus_off_keeps_its_own_route(rig, monkeypatch):
    g = rig["g"]
    ladder = {"n": 0}

    async def offbus_ladder(*a, **k):
        ladder["n"] += 1
        return False

    monkeypatch.setattr(g, "_escalate_offbus_stuck_hold", offbus_ladder)
    rig["beats"] = {}
    await g.escalate("offbus", [], 32, lambda m: None)
    assert ladder["n"] == 1 and g._td["path"] == "LADDER"


# ---- the capture --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["ipmitool", "raw", "0x30", "0x8b", "0x01"],  # the tray re-power: a write
        ["ipmitool", "raw", "0x06", "0x52", "0x03", "0x42", "0x00", "0x0C", "0x0D"],  # index write
        ["ipmitool", "raw", "0x06", "0x52", "0x03", "0x42", "0x01", "0x0A", "0x00"],
        ["ipmitool", "chassis", "power", "cycle"],
        ["ipmitool", "sel", "clear"],
        ["lspci", "-s", "0000:81:00.0; reboot", "-vv"],
        ["sh", "-c", "true"],
    ],
)
def test_the_allow_list_refuses_anything_but_the_read_shapes(argv):
    assert not bmc_capture.argv_allowed(argv)


def test_cpld_reads_come_only_from_config(monkeypatch):
    for k in ("BUSES", "ADDR", "REGS"):
        monkeypatch.delenv(f"TT_DEVICE_MCP_TRAY_CPLD_{k}", raising=False)
    argvs, cpld = bmc_capture.capture_argvs([3], ["0000:c1:00.0"], 1000.0)
    assert not cpld and not any(a[:2] == ["ipmitool", "raw"] for a in argvs)
    assert ["ipmitool", "sel", "elist", "last", "40"] in argvs, "the SEL is still read without the config"
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_CPLD_BUSES", "3:0x07")
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_CPLD_ADDR", "0x42")
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_CPLD_REGS", "0x0A,0x0B")
    argvs, cpld = bmc_capture.capture_argvs([3, 4], ["0000:c1:00.0"], 1000.0)
    raw = [a for a in argvs if a[:2] == ["ipmitool", "raw"]]
    assert cpld and raw == [
        ["ipmitool", "raw", "0x06", "0x52", "0x07", "0x42", "0x01", "0x0A"],
        ["ipmitool", "raw", "0x06", "0x52", "0x07", "0x42", "0x01", "0x0B"],
    ], "only the configured tray's registers, as single-byte reads"
    assert all(bmc_capture.argv_allowed(a) for a in argvs)


def test_the_capture_meets_its_deadline_survives_a_missing_tool_and_fsyncs_the_bundle(tmp_path, monkeypatch):
    import threading

    release = threading.Event()
    spawned = []

    def run(argv, **k):
        spawned.append(argv)
        if argv[0] == "ipmitool":
            raise FileNotFoundError("ipmitool")
        if argv[0] == "journalctl":
            release.wait(5)  # hangs past the deadline
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    synced = []
    real_fsync = bmc_capture.os.fsync
    monkeypatch.setattr(bmc_capture.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    onset = {"off_per_tray": {3: 8}, "off": list(range(24, 32)), "expected": 32, "epoch": 1000.0}
    try:
        summary = bmc_capture.capture_tray_down(
            tmp_path, onset, trays=[3], pci_targets=["0000:c1:00.0"], deadline_sec=0.3, run=run
        )
    finally:
        release.set()
    assert summary["elapsed_sec"] < 2
    assert all(bmc_capture.argv_allowed(a) for a in spawned)
    text = (tmp_path / "bmc.txt").read_text()
    assert "not installed" in text and "deadline" in text and "ok" in text
    assert json.loads((tmp_path / "onset.json").read_text())["off_per_tray"] == {"3": 8}
    assert len(synced) >= 3, "bmc.txt, onset.json and the directory are fsync'd before the cycle"


def _sysfs_tree(tmp_path):
    """A /sys/bus/pci/devices look-alike: links into a device tree where each chip sits under its
    own bridge. Chip 01 is still enumerated; chip 02 has left the bus (its bridge stays, pointing
    at the empty bus 2); bus 03 has no bridge at all."""
    tree = tmp_path / "tree" / "pci0000:00"
    devices = tmp_path / "devices"
    devices.mkdir()
    for bridge_addr, sec in (("0000:00:01.1", 1), ("0000:00:01.2", 2)):
        (tree / bridge_addr).mkdir(parents=True)
        (tree / bridge_addr / "secondary_bus_number").write_text(f"{sec}\n")
        (devices / bridge_addr).symlink_to(tree / bridge_addr)
    (tree / "0000:00:01.1" / "0000:01:00.0").mkdir()
    (devices / "0000:01:00.0").symlink_to(tree / "0000:00:01.1" / "0000:01:00.0")
    return devices


def test_lspci_targets_the_bridge_above_each_off_chip(tmp_path, monkeypatch):
    """The bridge keeps the link status when the endpoint drops: it is found by the sysfs parent
    while the endpoint is listed, and by its secondary bus once it is gone. The endpoint is added
    only while present; an address with no bridge or a malformed one adds nothing."""
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", _sysfs_tree(tmp_path))
    assert bmc_capture.upstream_bridge("0000:01:00.0") == "0000:00:01.1"
    assert bmc_capture.upstream_bridge("0000:02:00.0") == "0000:00:01.2", "found with the endpoint gone"
    assert bmc_capture.upstream_bridge("0000:03:00.0") is None
    targets = bmc_capture.lspci_targets(["0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "x; reboot", "0000:01:00.0"])
    assert targets == ["0000:00:01.1", "0000:01:00.0", "0000:00:01.2"]
    argvs, _ = bmc_capture.capture_argvs([1], targets, 1000.0)
    assert [a[2] for a in argvs if a[0] == "lspci"] == targets


def test_the_capture_reads_the_bridge_of_every_off_chip(galaxy_trays, monkeypatch):
    """GalaxyRecovery hands every off chip's banked PCI address (I16) to lspci_targets, not only the
    first chip of each tray, and passes its targets to the capture."""
    g = srv.galaxy_recovery
    off = sorted(galaxy_trays[3])
    seen = {}
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(bmc_capture, "lspci_targets", lambda addrs: seen.setdefault("addrs", list(addrs)) and ["b"])
    monkeypatch.setattr(bmc_capture, "capture_tray_down", lambda bundle, onset, **k: seen.update(k) or {})
    g._tray_down_capture({"off_per_tray": {3: len(off)}, "off": off, "expected": 32, "epoch": 1000.0}, 32)
    buses = srv.health_monitor._chip_buses
    assert seen["addrs"] == [buses[c] for c in off]
    assert seen["pci_targets"] == ["b"] and seen["trays"] == [3]
