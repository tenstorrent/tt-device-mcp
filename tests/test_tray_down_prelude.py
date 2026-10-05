# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The tray-down prelude (spec 04 I18, issue #26).

Four or more chips off ONE physical tray at the first sighting of an episode is a tray that lost
power. Before the ladder's first rung it gets one PCI rescan and a read-only capture of the
BMC/CPLD/PCIe state; then the full reset ladder runs exactly as before, and the power cycle fires
only from the ladder's own host rung, when every reset failed. Anything else keeps the ladder
with no prelude.
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


def test_no_map_or_an_unplaced_chip_never_classifies_as_tray_down(bh_map):
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


def test_replay_the_prelude_runs_only_on_onsets_that_never_recovered_without_a_power_cycle(bh_map):
    """Every recorded onset, anonymised to its per-tray counts and outcome. The prelude changes no
    ladder decision anyway; on top of that, no episode that came back without a power cycle
    (NO-BOOT) is even classified as tray-down, so none of them gets the extra rescan or capture."""
    tray_down = partial = no_boot = 0
    for per_tray, outcome in _replay_rows():
        if per_tray.startswith("count="):
            off = set()  # the log named no chip ids: nothing to place on a tray
        else:
            spec = {int(t[1:]): int(n) for t, n in (p.split(":") for p in per_tray.split(","))}
            off = _off(bh_map, spec)
        onset = galaxy.tray_down_onset(off, BH_CHIP_BUSES, "tt-galaxy-bh")
        if outcome == "NO-BOOT":
            no_boot += 1
            assert onset is None, f"a recoverable onset ({per_tray}) gets no prelude"
        if onset is None:
            partial += 1
        else:
            tray_down += 1
            assert outcome == "BOOT", f"a tray-down onset ({per_tray}) that recovered without a cycle"
    assert (tray_down, partial, no_boot) == (142, 80, 72)


# ---- the gate/idle/watchdog wiring --------------------------------------------------------------


@pytest.fixture
def rig(monkeypatch, galaxy_trays):
    """srv.galaxy_recovery with the rescan, the capture, every ladder primitive and the power cycle
    recorded in ``rig['order']``, and the bus read from ``rig['beats']``. The ladder's dispatch
    (_fire_gate_rung) is the real one."""
    g = srv.galaxy_recovery
    state = {
        "beats": {},
        "events": [],
        "order": [],
        "allowed": (True, ""),
        "map": galaxy_trays,
        "reset_ok": False,
        "last_chance_ok": False,
    }
    order = state["order"]
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
    monkeypatch.delenv("TT_DEVICE_MCP_TRAY_DOWN_CAPTURE", raising=False)

    def rescan():
        order.append("rescan")
        if "after_rescan" in state:
            state["beats"] = state.pop("after_rescan")

    monkeypatch.setattr(galaxy, "_pci_rescan", rescan)

    def capture(onset, expected):
        order.append("capture")
        return {"cpld": "read"}

    monkeypatch.setattr(g, "_tray_down_capture", capture)

    async def fake_pc(log, reason):
        order.append("power_cycle")

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)

    async def mesh_reset(indices, log):
        order.append("glx_reset")
        return state["reset_ok"]

    async def last_chance(*a, **k):
        order.append("last_chance")
        return state["last_chance_ok"]

    def recorded(name, result=False):
        async def fire(*a, **k):
            order.append(name)
            return result

        return fire

    monkeypatch.setattr(g, "_reset_and_verify_device", mesh_reset)
    monkeypatch.setattr(g, "_settle_and_verify_before_host_rung", last_chance)
    monkeypatch.setattr(g, "_attempt_ubb_tray_reset", recorded("ubb_tray"))
    monkeypatch.setattr(g, "_fire_tray_down_no_window", recorded("tray_down_no_window", galaxy.OUTCOME_WAITING))
    monkeypatch.setattr(g, "_escalate_offbus_stuck_hold", recorded("offbus_ladder"))
    monkeypatch.setattr(g, "_escalate_stuck_hold", recorded("present_ladder"))
    state["g"] = g
    return state


def _beats_without(off: set) -> dict:
    return {str(i): 100 for i in range(32) if i not in off}


def _kinds(rig):
    return [k for k, _ in rig["events"]]


async def _run_ladder(g, indices, stages=(galaxy.STAGE_SMI_RESET, galaxy.STAGE_POWER_CYCLE)):
    """The gate's climb for one episode, one escalate() per pass: the stages the router names."""
    return [await g.escalate("gate/post-job", indices, 32, lambda m: None, stage=s) for s in stages]


@pytest.mark.asyncio
async def test_a_tray_down_onset_rescans_captures_then_runs_the_full_ladder_and_power_cycles_only_when_it_fails(rig):
    off = _off(rig["map"], {3: 8})
    rig["beats"] = _beats_without(off)
    outs = await _run_ladder(rig["g"], sorted(map(str, off)))
    assert rig["order"] == ["rescan", "capture", "glx_reset", "last_chance", "power_cycle"]
    assert outs == [galaxy.OUTCOME_WAITING, galaxy.OUTCOME_WAITING]
    assert (
        "tray_down_latched",
        {"path": "TRAY_DOWN", "off_per_tray": {3: 8}, "expected": 32, "after_reset": False},
    ) in rig["events"]
    assert "tray_down_prelude" in _kinds(rig)


@pytest.mark.asyncio
async def test_a_ladder_that_recovers_the_tray_fires_no_power_cycle(rig):
    rig["beats"] = _beats_without(_off(rig["map"], {2: 8}))
    rig["reset_ok"] = True
    assert (await _run_ladder(rig["g"], [], stages=(galaxy.STAGE_SMI_RESET,))) == [galaxy.OUTCOME_RECOVERED]
    assert rig["order"] == ["rescan", "capture", "glx_reset"]


@pytest.mark.asyncio
async def test_the_last_chance_sweep_still_gates_the_power_cycle_of_a_tray_down(rig):
    rig["beats"] = _beats_without(_off(rig["map"], {2: 8}))
    rig["last_chance_ok"] = True
    outs = await _run_ladder(rig["g"], [])
    assert outs == [galaxy.OUTCOME_WAITING, galaxy.OUTCOME_RECOVERED]
    assert rig["order"] == ["rescan", "capture", "glx_reset", "last_chance"], "no power cycle"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage, rung",
    [
        (galaxy.STAGE_SMI_RESET, "glx_reset"),
        (galaxy.STAGE_POWER_CYCLE, "last_chance"),
    ],
)
async def test_the_prelude_changes_no_ladder_input(rig, monkeypatch, stage, rung):
    """The ladder sees the caller's own stage, indices, evidence and beats, with or without the
    prelude: the same call with TT_DEVICE_MCP_TRAY_DOWN_CAPTURE=0 (1.0.0's path) records the same
    arguments and the same rungs."""
    g = rig["g"]
    seen = []
    real = galaxy.GalaxyRecovery._fire_gate_rung

    async def spy(self, *a, **k):
        seen.append((a[:2], {key: k[key] for key in ("ev", "beats")}))
        return await real(self, *a, **k)

    monkeypatch.setattr(galaxy.GalaxyRecovery, "_fire_gate_rung", spy)
    beats = _beats_without(_off(rig["map"], {4: 8}))
    rig["beats"] = dict(beats)
    await g.escalate("gate/post-job", ["24"], 32, lambda m: None, stage=stage, ev=None, beats=dict(beats))
    with_prelude = [r for r in rig["order"] if r not in ("rescan", "capture")]
    assert rig["order"][:2] == ["rescan", "capture"]
    g.tray_down_episode_end()
    rig["order"].clear()
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_DOWN_CAPTURE", "0")
    await g.escalate("gate/post-job", ["24"], 32, lambda m: None, stage=stage, ev=None, beats=dict(beats))
    assert rig["order"] == with_prelude and rung in with_prelude
    assert seen[0] == seen[1]


@pytest.mark.asyncio
async def test_the_prelude_runs_once_per_episode_from_any_caller(rig):
    """The gate, the idle relift and the hold-deadline watchdog all pass through escalate(): the
    rescan and the capture happen once, at the first, and each caller then runs its own ladder."""
    g = rig["g"]
    rig["beats"] = _beats_without(_off(rig["map"], {2: 5}))
    await g.escalate("gate/post-job", [], 32, lambda m: None, stage=galaxy.STAGE_SMI_RESET)
    await g.escalate("offbus", [], 32, lambda m: None)
    await g.escalate("offbus-forced", [], 32, lambda m: None)
    assert rig["order"] == ["rescan", "capture", "glx_reset", "offbus_ladder", "offbus_ladder"]
    assert _kinds(rig).count("tray_down_prelude") == 1


@pytest.mark.asyncio
async def test_every_chip_back_after_the_rescan_runs_the_full_verify_and_stops(rig, monkeypatch):
    off = _off(rig["map"], {1: 8})
    rig["beats"] = _beats_without(off)
    rig["after_rescan"] = _beats_without(set())
    verified = {"n": 0}

    async def verify(expected, log, run_fabric=True, **k):
        verified["n"] += 1
        assert run_fabric, "the full verify, fabric included"
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)
    out = await rig["g"].escalate("gate/post-job", [], 32, lambda m: None, stage=galaxy.STAGE_SMI_RESET)
    assert out == galaxy.OUTCOME_RECOVERED and verified["n"] == 1
    assert rig["order"] == ["rescan", "capture"], "no rung, no power cycle"
    assert rig["g"]._td is None, "a recovered episode closes the latch"


@pytest.mark.asyncio
async def test_chips_back_on_a_mesh_that_fails_verify_still_get_the_full_ladder(rig, monkeypatch):
    rig["beats"] = _beats_without(_off(rig["map"], {1: 8}))
    rig["after_rescan"] = _beats_without(set())

    async def verify(expected, log, run_fabric=True, **k):
        return False, {}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await _run_ladder(rig["g"], [])
    assert rig["order"] == ["rescan", "capture", "glx_reset", "last_chance", "power_cycle"]


@pytest.mark.asyncio
async def test_a_tenant_defers_the_rescan_but_not_the_capture(rig, monkeypatch):
    """The capture only reads, so it runs at once; the rescan writes to the PCI subsystem and waits
    for a pass with no tenant, still before that pass's first rung."""
    g = rig["g"]
    scans = [HolderScan(holders=[], complete=False), HolderScan(holders=[], complete=True)]
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: scans.pop(0) if scans else scans_done)
    scans_done = HolderScan(holders=[], complete=True)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    await g.escalate("offbus", [], 32, lambda m: None)
    assert rig["order"] == ["capture", "offbus_ladder"]
    await g.escalate("offbus", [], 32, lambda m: None)
    assert rig["order"] == ["capture", "offbus_ladder", "rescan", "offbus_ladder"]
    await g.escalate("offbus", [], 32, lambda m: None)
    assert rig["order"].count("rescan") == 1 and rig["order"].count("capture") == 1


@pytest.mark.asyncio
async def test_one_to_three_chips_off_get_no_prelude(rig):
    rig["beats"] = _beats_without(_off(rig["map"], {3: 3, 1: 1}))
    await _run_ladder(rig["g"], [])
    assert rig["order"] == ["glx_reset", "last_chance", "power_cycle"]
    assert rig["g"]._td["path"] == "PARTIAL"


@pytest.mark.asyncio
async def test_the_latch_does_not_change_after_a_reset_and_is_fresh_after_the_episode(rig, monkeypatch):
    """A ladder reset can turn a 1-chip drop into a whole tray off: that is not an onset."""
    g = rig["g"]

    async def offbus_ladder(*a, **k):
        rig["order"].append("offbus_ladder")
        rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))  # the reset knocked the tray out
        return False

    monkeypatch.setattr(g, "_escalate_offbus_stuck_hold", offbus_ladder)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    await g.escalate("offbus", [], 32, lambda m: None)
    await g.escalate("offbus", [], 32, lambda m: None)
    assert rig["order"] == ["offbus_ladder", "offbus_ladder"] and g._td["path"] == "PARTIAL"
    g.tray_down_episode_end()
    await g.escalate("offbus", [], 32, lambda m: None)
    assert g._td["path"] == "TRAY_DOWN" and rig["order"][2:] == ["rescan", "capture", "offbus_ladder"]


@pytest.mark.asyncio
async def test_a_manual_reset_before_the_first_sighting_is_not_an_onset(rig, monkeypatch):
    """Review finding: a reset that did not come through escalate() (the reset tool, the HTTP reset)
    goes through reset_with_quiesce, which marks the mechanism; a whole tray off seen after it is
    the reset's doing, so it gets no prelude, and the ladder runs as before."""
    g = rig["g"]
    mech = g.mechanism
    monkeypatch.setattr(mech, "run_scoped", lambda argv, log, owner: _done())
    monkeypatch.setattr(mech, "_set_device_pollers", lambda on, log: _done(True))
    monkeypatch.setattr(mech, "_set_device_op_detail", lambda d: None)
    monkeypatch.setattr(mech, "reset_in_flight", False)
    monkeypatch.setattr(mech, "reset_since_release", False)
    await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None, owner="manual")
    assert mech.reset_since_release
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    await _run_ladder(g, [])
    assert rig["order"] == ["glx_reset", "last_chance", "power_cycle"], "no rescan, no capture"
    assert g._td["path"] == "PARTIAL"
    assert (
        "tray_down_latched",
        {"path": "PARTIAL", "off_per_tray": {3: 8}, "expected": 32, "after_reset": True},
    ) in rig["events"]
    g.tray_down_episode_end()
    assert not mech.reset_since_release, "the release clears it"


async def _done(value=(0, "")):
    return value


@pytest.mark.asyncio
async def test_a_reset_in_flight_or_a_live_scope_is_not_an_onset(rig, monkeypatch):
    g = rig["g"]
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: "ttdev-reset-1.scope")
    await g.escalate("offbus", [], 32, lambda m: None)
    assert g._td["path"] == "PARTIAL" and "rescan" not in rig["order"]


@pytest.mark.asyncio
async def test_a_restarted_broker_latches_a_tray_still_missing_afresh(rig):
    """The latch is not persisted: a restarted broker (a new GalaxyRecovery) has none, takes it again
    at its first sighting and rescans and captures again; the ladder and the power-cycle guard are
    the same, so a restart buys no extra power cycle."""
    g = rig["g"]
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    await g.escalate("offbus", [], 32, lambda m: None)
    assert g._td["path"] == "TRAY_DOWN"

    restarted = galaxy.GalaxyRecovery(g.monitor, g.mechanism, g.deps)
    assert restarted._td is None, "a restart starts with no latch"
    restarted._tray_down_capture = g._tray_down_capture
    restarted._escalate_offbus_stuck_hold = g._escalate_offbus_stuck_hold
    await restarted.escalate("offbus", [], 32, lambda m: None)
    assert restarted._td["path"] == "TRAY_DOWN"
    assert rig["order"] == ["rescan", "capture", "offbus_ladder", "rescan", "capture", "offbus_ladder"]
    assert "power_cycle" not in rig["order"]


@pytest.mark.asyncio
async def test_the_gate_runs_the_prelude_then_its_ladder_end_to_end(rig, monkeypatch, tmp_path, clear_job_state):
    """End to end through server.device_health_gate: tray 3 off the bus after a job, no tenant. The
    hook in escalate() latches TRAY_DOWN, rescans and captures before the gate's first rung, and the
    gate's ladder then runs its rung; no power cycle on a first pass."""
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

    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    rungs = []
    real = galaxy.GalaxyRecovery._fire_gate_rung

    async def spy(self, gate_phase, stage, *a, **k):
        rig["order"].append(f"rung:{stage}")
        rungs.append(stage)
        return await real(self, gate_phase, stage, *a, **k)

    monkeypatch.setattr(galaxy.GalaxyRecovery, "_fire_gate_rung", spy)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert rig["order"][:3] == ["rescan", "capture", f"rung:{rungs[0]}"]
    assert "power_cycle" not in rig["order"]
    assert rig["g"]._td["path"] == "TRAY_DOWN"


@pytest.mark.asyncio
async def test_capture_off_runs_the_ladder_alone(rig, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_DOWN_CAPTURE", "0")
    g = rig["g"]
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    await _run_ladder(g, [])
    assert rig["order"] == ["glx_reset", "last_chance", "power_cycle"] and g._td is None


@pytest.mark.asyncio
async def test_the_whole_bus_off_keeps_its_own_route(rig):
    g = rig["g"]
    rig["beats"] = {}
    await g.escalate("offbus", [], 32, lambda m: None)
    assert rig["order"] == ["offbus_ladder"] and g._td["path"] == "PARTIAL"


# ---- the capture --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["ipmitool", "raw", "0x30", "0x8b", "0x01"],  # the tray re-power: a write
        ["ipmitool", "raw", "0x06", "0x52", "0x03", "0x42", "0x00", "0x0C", "0x0D"],  # index write
        ["ipmitool", "raw", "0x06", "0x52", "0x03", "0x42", "0x01", "0x0A", "0x00"],
        ["ipmitool", "chassis", "power", "cycle"],
        ["ipmitool", "sel", "clear"],
        ["ipmitool", "sdr", "elist", "full"],
        ["ipmitool", "mc", "reset", "cold"],
        ["lspci", "-s", "0000:81:00.0; reboot", "-vv"],
        ["sh", "-c", "true"],
    ],
)
def test_the_allow_list_refuses_anything_but_the_read_shapes(argv):
    assert not bmc_capture.argv_allowed(argv)


def test_cpld_reads_come_only_from_config_and_cover_every_configured_tray(monkeypatch):
    for k in ("BUSES", "ADDR", "REGS"):
        monkeypatch.delenv(f"TT_DEVICE_MCP_TRAY_CPLD_{k}", raising=False)
    for k in ("BUS", "ADDR", "REGS"):
        monkeypatch.delenv(f"TT_DEVICE_MCP_PDB_CPLD_{k}", raising=False)
    argvs, cpld = bmc_capture.capture_argvs(["0000:c1:00.0"], 1000.0)
    assert cpld == {"tray": False, "pdb": False} and not any(a[:2] == ["ipmitool", "raw"] for a in argvs)
    for fixed in (["ipmitool", "sel", "elist", "last", "40"], ["ipmitool", "sdr", "elist"], ["ipmitool", "mc", "info"]):
        assert fixed in argvs, "the SEL, sensors and BMC state are read without any config"
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_CPLD_BUSES", "3:0x07,1:0x05")
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_CPLD_ADDR", "0x42")
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_CPLD_REGS", "0x0A,0x0B")
    monkeypatch.setenv("TT_DEVICE_MCP_PDB_CPLD_BUS", "0x02")
    monkeypatch.setenv("TT_DEVICE_MCP_PDB_CPLD_ADDR", "0x44")
    monkeypatch.setenv("TT_DEVICE_MCP_PDB_CPLD_REGS", "0x10")
    argvs, cpld = bmc_capture.capture_argvs(["0000:c1:00.0"], 1000.0)
    raw = [a for a in argvs if a[:2] == ["ipmitool", "raw"]]
    assert cpld == {"tray": True, "pdb": True} and raw == [
        ["ipmitool", "raw", "0x06", "0x52", "0x05", "0x42", "0x01", "0x0A"],
        ["ipmitool", "raw", "0x06", "0x52", "0x05", "0x42", "0x01", "0x0B"],
        ["ipmitool", "raw", "0x06", "0x52", "0x07", "0x42", "0x01", "0x0A"],
        ["ipmitool", "raw", "0x06", "0x52", "0x07", "0x42", "0x01", "0x0B"],
        ["ipmitool", "raw", "0x06", "0x52", "0x02", "0x44", "0x01", "0x10"],
    ], "every configured tray (the healthy ones are the baseline) and the PDB, as single-byte reads"
    assert all(bmc_capture.argv_allowed(a) for a in argvs)
    monkeypatch.setenv("TT_DEVICE_MCP_PDB_CPLD_REGS", "0x10,nope")
    assert bmc_capture.capture_argvs([], 1000.0)[1]["pdb"] is False, "a malformed PDB config reads nothing"


def test_bmc_reads_get_the_long_timeout_inside_the_deadline():
    seen = {}

    def run(argv, timeout, **k):
        seen[argv[0]] = timeout
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    for argv in (["ipmitool", "mc", "info"], ["lspci", "-s", "0000:c1:00.0", "-vv"], ["journalctl", "-k"]):
        bmc_capture._run_one(argv, run)
    assert seen == {"ipmitool": 8.0, "lspci": 2.0, "journalctl": 3.0}
    assert bmc_capture.BMC_CALL_TIMEOUT_SEC < bmc_capture.CAPTURE_DEADLINE_SEC


def test_a_missing_incident_bundle_still_gets_the_bmc_reads_written(galaxy_trays, monkeypatch):
    """Review finding: with no incident bundle the capture wrote nothing. It now writes into a
    tray_down_bmc directory of its own under the incidents root."""
    from tt_device_mcp.health import evidence

    g = srv.galaxy_recovery
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    off = sorted(galaxy_trays[3])
    summary = g._tray_down_capture({"off_per_tray": {3: len(off)}, "off": off, "expected": 32, "epoch": 1000.0}, 32)
    bundle = pathlib.Path(summary["bundle"])
    assert bundle.parent == evidence.HEALTH_DIR / evidence.INCIDENTS_DIR and bundle.name.endswith("_tray_down_bmc")
    assert "not run in tests" in (bundle / "bmc.txt").read_text()


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
    assert len(synced) >= 3, "bmc.txt, onset.json and the directory are fsync'd before the ladder"


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
    argvs, _ = bmc_capture.capture_argvs(targets, 1000.0)
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
    assert seen["pci_targets"] == ["b"] and seen["trays"] == [3] and seen["all_trays"] == [1, 2, 3, 4]
