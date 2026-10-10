# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The host-safety envelope around a per-tray re-power and the per-host reset gate (spec 04 I18-I20).

Every test runs against a fake sysfs tree: four Blackhole trays, one chip each, the kernel's /dev
order 0x0X, 0x4X, 0xCX, 0x8X (so trays 3 and 4 are where list position puts them the wrong way
round, issue #27). Nothing here reaches a real device or a real config space.
"""

import json
import os
import pathlib
import shutil
import sys
import types

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import galaxy, pcie_guard

# (chip id, endpoint bus, root port) in the kernel's /dev order
CHIPS = [(0, 0x01, "0000:00:01.1"), (1, 0x41, "0000:40:01.1"), (2, 0xC1, "0000:c0:01.1"), (3, 0x81, "0000:80:01.1")]
PORTS = [rp for _c, _b, rp in CHIPS]
AER, DPC, PCIE = 0x100, 0x200, 0x40


def _port_config() -> bytearray:
    cfg = bytearray(4096)
    cfg[0x06] = 0x10  # capability list present
    cfg[0x34] = PCIE
    cfg[PCIE] = 0x10  # PCIe capability, last in the list
    cfg[PCIE + 8 : PCIE + 10] = (0x000F).to_bytes(2, "little")  # all error reporting enabled
    cfg[AER : AER + 4] = (0x0001 | 1 << 16 | DPC << 20).to_bytes(4, "little")
    cfg[AER + 0x14 : AER + 0x18] = (0x2000).to_bytes(4, "little")  # CE mask: Advisory Non-Fatal only
    cfg[AER + 0x2C : AER + 0x30] = (0x7).to_bytes(4, "little")  # root error interrupts on
    cfg[DPC : DPC + 4] = (0x001D | 1 << 16).to_bytes(4, "little")
    cfg[DPC + 6 : DPC + 8] = (0x000B).to_bytes(2, "little")  # DPC triggers + interrupt on
    return cfg


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    devs = tmp_path / "sys/bus/pci/devices"
    devs.mkdir(parents=True)
    (tmp_path / "sys/class/tenstorrent").mkdir(parents=True)
    for chip, bus, rp in CHIPS:
        seg = rp.split(":")[1]
        rp_dir = tmp_path / f"sys/devices/pci0000:{seg}/{rp}"
        ep = f"0000:{bus:02x}:00.0"
        ep_dir = rp_dir / ep
        ep_dir.mkdir(parents=True)
        (rp_dir / "vendor").write_text("0x1022\n")
        (rp_dir / "config").write_bytes(bytes(_port_config()))
        (rp_dir / "aer_rootport_total_err_cor").write_text("0\n")
        (ep_dir / "vendor").write_text("0x1e52\n")
        (ep_dir / "device").write_text("0xb140\n")
        (devs / rp).symlink_to(rp_dir)
        (devs / ep).symlink_to(ep_dir)
        cls = tmp_path / f"sys/class/tenstorrent/tenstorrent!{chip}"
        cls.mkdir()
        (cls / "device").symlink_to(ep_dir)
    constants = types.ModuleType("tt_smi.constants")
    constants.BH_UBB_BUS_IDS = {1: 0x00, 2: 0x40, 3: 0xC0, 4: 0x80}
    constants.WH_UBB_BUS_IDS = {}
    pkg = types.ModuleType("tt_smi")
    pkg.constants = constants
    monkeypatch.setitem(sys.modules, "tt_smi", pkg)
    monkeypatch.setitem(sys.modules, "tt_smi.constants", constants)
    monkeypatch.setattr(pcie_guard, "SYS_ROOT", tmp_path)
    monkeypatch.setattr(pcie_guard, "_FLOOD_UNTIL", 0.0)
    monkeypatch.setattr(pcie_guard, "_LAST_AER_SAMPLE", None)
    monkeypatch.setenv("TT_DEVICE_MCP_AER_QUIET_CHECK_SEC", "0.01")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(pcie_guard, "health_event", lambda *a, **k: None)
    return tmp_path


def _cfg(root, bdf, off, size):
    data = (root / "sys/bus/pci/devices" / bdf / "config").read_bytes()
    return int.from_bytes(data[off : off + size], "little")


def _set_cfg(root, bdf, off, size, value):
    p = root / "sys/bus/pci/devices" / bdf / "config"
    data = bytearray(p.read_bytes())
    data[off : off + size] = value.to_bytes(size, "little")
    p.write_bytes(bytes(data))


def _masked(root, bdf) -> bool:
    return (
        _cfg(root, bdf, PCIE + 8, 2) & 0xF == 0
        and _cfg(root, bdf, AER + 0x2C, 4) & 0x7 == 0
        and _cfg(root, bdf, AER + 0x08, 4) & 0x07FFF030 == 0x07FFF030
        and _cfg(root, bdf, AER + 0x14, 4) & 0xF1C1 == 0xF1C1
        and _cfg(root, bdf, DPC + 6, 2) & 0xB == 0
    )


def _restored(root, bdf) -> bool:
    return (
        _cfg(root, bdf, PCIE + 8, 2) == 0xF
        and _cfg(root, bdf, AER + 0x2C, 4) == 0x7
        and _cfg(root, bdf, AER + 0x08, 4) == 0
        and _cfg(root, bdf, AER + 0x14, 4) == 0x2000
        and _cfg(root, bdf, DPC + 6, 2) == 0xB
    )


# ---------------------------------------------------------------- tray map cross-check (issue #27)


def test_sysfs_names_each_chip_its_bus_and_root_port(sysfs):
    eps = pcie_guard.tt_endpoints()
    assert {e.chip: (e.bus, e.root_port) for e in eps.values()} == {c: (b, rp) for c, b, rp in CHIPS}
    assert pcie_guard.tt_root_ports(eps) == sorted(PORTS)


def test_plan_refuses_a_tray_map_that_points_at_another_tray(sysfs):
    """Tray 4 is bus 0x80, chip 3 in the kernel's order. Chip 2 (list position 3) sits on tray 3:
    a map built by position names it for tray 4, and firing would cut a healthy tray."""
    bad = pcie_guard.plan_tray_repower(0b1000, [2])
    assert bad.mismatch and "[2]" in bad.mismatch and "[3]" in bad.mismatch
    good = pcie_guard.plan_tray_repower(0b1000, [3])
    assert not good.mismatch
    assert good.functions == ["0000:81:00.0"] and good.chips == [3]
    assert good.root_ports == sorted(PORTS), "every Tenstorrent root port is masked, the siblings too"


def test_plan_refuses_without_a_tray_table(sysfs, monkeypatch):
    monkeypatch.delitem(sys.modules, "tt_smi.constants")
    monkeypatch.setitem(sys.modules, "tt_smi", types.ModuleType("tt_smi"))
    assert pcie_guard.plan_tray_repower(0b1, [0]).mismatch


# ---------------------------------------------------------------- the envelope


def test_envelope_masks_every_port_removes_the_tray_fires_rescans_and_restores(sysfs):
    steps = []

    def fire():
        assert all(_masked(sysfs, p) for p in PORTS), "every root port is masked while the tray is unpowered"
        assert (sysfs / "sys/bus/pci/devices/0000:81:00.0/remove").read_text() == "1"
        assert not (sysfs / "sys/bus/pci/devices/0000:01:00.0/remove").exists(), "other trays stay"
        steps.append("fire")

    plan = pcie_guard.safe_tray_repower(
        0b1000,
        [3],
        fire,
        lambda m: None,
        quiesce=lambda chips: steps.append(("quiesce", chips)),
        reinit=lambda chips: steps.append(("reinit", chips)),
        sleep=lambda s: None,
    )
    assert steps == [("quiesce", [3]), "fire", ("reinit", [3])]
    assert (sysfs / "sys/bus/pci/rescan").read_text() == "1"
    assert all(_restored(sysfs, p) for p in PORTS), "a quiet port gets its exact old settings back"
    assert plan.kept_masked == []


def test_envelope_restores_and_rescans_even_when_the_pulse_fails(sysfs):
    def fire():
        raise RuntimeError("ipmitool exited 1")

    with pytest.raises(RuntimeError):
        pcie_guard.safe_tray_repower(0b1000, [3], fire, lambda m: None, sleep=lambda s: None)
    assert (sysfs / "sys/bus/pci/rescan").read_text() == "1"
    assert all(_restored(sysfs, p) for p in PORTS)


def test_a_port_that_keeps_erroring_stays_masked_and_marks_a_flood(sysfs, monkeypatch):
    """The 0x40 sibling port flooding while tray 4 is re-powered: it stays masked, the others come
    back, and the flood window starts so the gate refuses the next automatic reset."""
    sibling = "0000:40:01.1"

    def fire():
        _set_cfg(sysfs, sibling, AER + 0x10, 4, 0x2000)  # Advisory Non-Fatal, latched again and again

    plan = pcie_guard.safe_tray_repower(0b1000, [3], fire, lambda m: None, sleep=lambda s: None)
    assert plan.kept_masked == [sibling]
    assert _masked(sysfs, sibling)
    assert all(_restored(sysfs, p) for p in PORTS if p != sibling)
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    allowed, why = pcie_guard.host_reset_gate(0)
    assert not allowed and "flooding" in why


def test_envelope_waits_for_holders_then_refuses_without_touching_anything(sysfs, monkeypatch):
    pids = sysfs / "proc/driver/tenstorrent/3"
    pids.mkdir(parents=True)
    (pids / "pids").write_text("4242\n")
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_REPOWER_HOLDER_WAIT_SEC", "1")
    waited = []
    with pytest.raises(pcie_guard.TrayRepowerRefused, match="4242"):
        pcie_guard.safe_tray_repower(
            0b1000, [3], lambda: pytest.fail("fired over a holder"), lambda m: None, sleep=waited.append
        )
    assert waited, "it waits for the holder before refusing"
    assert all(_restored(sysfs, p) for p in PORTS)
    assert not (sysfs / "sys/bus/pci/devices/0000:81:00.0/remove").exists()


def test_envelope_proceeds_once_the_holder_lets_go(sysfs, monkeypatch):
    pids = sysfs / "proc/driver/tenstorrent/3"
    pids.mkdir(parents=True)
    (pids / "pids").write_text("4242\n")
    fired = []
    pcie_guard.safe_tray_repower(
        0b1000, [3], lambda: fired.append(1), lambda m: None, sleep=lambda s: (pids / "pids").write_text("")
    )
    assert fired == [1]


def test_envelope_refuses_a_mismatched_tray_map(sysfs):
    with pytest.raises(pcie_guard.TrayRepowerRefused):
        pcie_guard.safe_tray_repower(0b1000, [2], lambda: pytest.fail("fired the wrong tray"), lambda m: None)
    assert all(_restored(sysfs, p) for p in PORTS)


def test_dry_run_logs_the_plan_and_touches_nothing(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_REPOWER_DRY_RUN", "1")
    lines = []
    with pytest.raises(pcie_guard.TrayRepowerDryRun):
        pcie_guard.safe_tray_repower(0b1000, [3], lambda: pytest.fail("dry run fired"), lines.append)
    text = "\n".join(lines)
    assert "DRY RUN" in text and "0000:81:00.0" in text and "0x08" in text
    assert all(_restored(sysfs, p) for p in PORTS)
    assert not (sysfs / "sys/bus/pci/devices/0000:81:00.0/remove").exists()
    assert not (sysfs / "sys/bus/pci/rescan").exists()


def test_cli_prints_the_plan(sysfs, capsys):
    assert pcie_guard.main(["0x8", "3"]) == 0
    assert "0000:81:00.0" in capsys.readouterr().out
    assert pcie_guard.main(["0x8", "2"]) == 1


# ---------------------------------------------------------------- the per-host gate


def test_gate_is_off_by_default(sysfs, monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_HOST_RESET_GATE", raising=False)
    assert pcie_guard.host_reset_gate(5) == (True, "")


def test_hold_refuses_a_reset_over_chips_already_off_the_bus(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    assert pcie_guard.host_reset_gate(0)[0]
    allowed, why = pcie_guard.host_reset_gate(1)
    assert not allowed and "off the bus" in why
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    assert pcie_guard.host_reset_gate(1)[0], "guard alone does not hold on an off-bus chip"


def test_chips_off_bus_counts_missing_functions(sysfs):
    assert pcie_guard.chips_off_bus(4) == 0
    os.unlink(sysfs / "sys/bus/pci/devices/0000:c1:00.0")
    assert pcie_guard.chips_off_bus(4) == 1


def test_guard_refuses_during_an_aer_flood(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    counter = sysfs / "sys/bus/pci/devices/0000:40:01.1/aer_rootport_total_err_cor"
    assert pcie_guard.host_reset_gate(0)[0]
    counter.write_text("500\n")
    allowed, why = pcie_guard.host_reset_gate(0)
    assert not allowed and "flooding" in why
    assert not pcie_guard.host_reset_gate(0)[0], "the flood window holds after the counters stop moving"


def test_first_look_after_a_recent_boot_counts_errors_since_boot(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    (sysfs / "proc").mkdir()
    (sysfs / "proc/uptime").write_text("585.0 1000.0\n")
    (sysfs / "sys/bus/pci/devices/0000:40:01.1/aer_rootport_total_err_cor").write_text("500\n")
    assert not pcie_guard.host_reset_gate(0)[0]


# ---------------------------------------------------------------- wired into the ladder


@pytest.mark.asyncio
async def test_a_gated_sweep_never_reaches_the_power_cycle(sysfs, monkeypatch):
    """The ladder rule: no power cycle without the full reset ladder. A sweep the gate refused is not
    the full ladder, so the host rung holds."""
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    os.unlink(sysfs / "sys/bus/pci/devices/0000:c1:00.0")
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: pytest.fail("gated sweep fired a tray"))
    cycles = []

    async def fake_pc(log, reason):
        cycles.append(reason)

    async def bad(expected, log, run_fabric=True, **k):
        return False, {}

    async def no_reset(*a, **k):
        pytest.fail("gated sweep fired a mesh reset")

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", no_reset)
    patch_recovery(monkeypatch, "_verify_device", bad)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 4, lambda m: None, ev=None, beats={})
    assert out == galaxy.OUTCOME_WAITING and cycles == []
    out = await g._fire_tray_down_no_window({"2"}, 4, lambda m: None, gate_phase="x", ev=None)
    assert out == galaxy.OUTCOME_WAITING and cycles == []


@pytest.mark.asyncio
async def test_a_refused_tray_fire_makes_the_sweep_incomplete(sysfs, monkeypatch):
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)

    def refuse(*a, **k):
        raise pcie_guard.TrayRepowerRefused("tray map disagrees")

    async def ok_reset(*a, **k):
        return 0, ""

    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", refuse)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", ok_reset)
    assert await g._issue_all_resets_back_to_back(0b1000, [3], [4], 4, lambda m: None, do_sbr=False) is False
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: None)
    assert await g._issue_all_resets_back_to_back(0b1000, [3], [4], 4, lambda m: None, do_sbr=False) is True


@pytest.mark.asyncio
async def test_pollers_are_stopped_across_the_tray_fire(sysfs, monkeypatch):
    g = srv.galaxy_recovery
    seen = []

    async def pollers(active, log):
        seen.append(("start" if active else "stop"))
        return ["tt-fmax-cap.service"]

    monkeypatch.setattr(g.mechanism, "_set_device_pollers", pollers)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: seen.append("fire"))
    await g._fire_tray_repower(0b1000, [3], lambda m: None)
    assert seen == ["stop", "fire", "start"]


@pytest.mark.asyncio
async def test_hold_gate_stops_an_automatic_mesh_reset_over_an_off_bus_chip(sysfs, monkeypatch):
    """The idle relift's glx_reset that dropped a chip must not be re-issued on the next pass."""
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    os.unlink(sysfs / "sys/bus/pci/devices/0000:41:00.0")
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", lambda log: _false())
    monkeypatch.setattr(g.monitor, "expected", lambda n: 4)

    async def no_reset(*a, **k):
        pytest.fail("gated mesh reset fired")

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", no_reset)
    assert await g._reset_and_verify_device(["0", "1", "2", "3"], lambda m: None) is False


@pytest.mark.asyncio
async def test_guard_masks_the_root_ports_around_an_automatic_mesh_reset(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", lambda log: _false())
    monkeypatch.setattr(g.monitor, "expected", lambda n: 4)
    masked = []

    async def reset(*a, **k):
        masked.append(all(_masked(sysfs, p) for p in PORTS))
        return 1, "reset failed"

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    await g._reset_and_verify_device(["0", "1", "2", "3"], lambda m: None)
    assert masked == [True]
    assert all(_restored(sysfs, p) for p in PORTS)


async def _false():
    return False


# ---------------------------------------------------------------- host-hang latch (spec 04 I21)


def _boot(root, boot_id):
    p = root / "proc/sys/kernel/random/boot_id"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(boot_id + "\n")


def test_an_off_bus_reset_writes_an_intent_and_removes_it_when_done(sysfs):
    _boot(sysfs, "boot-a")
    intent = pcie_guard.health_dir() / pcie_guard.INTENT_FILE
    pcie_guard.begin_offbus_reset("tray re-power", 0)
    assert not intent.exists(), "no chip off the bus -> no intent"
    pcie_guard.begin_offbus_reset("tray re-power", 2)
    assert json.loads(intent.read_text())["boot_id"] == "boot-a"
    pcie_guard.end_offbus_reset()
    assert not intent.exists()


def test_an_intent_from_a_boot_that_died_latches_hold_until_cleared(sysfs, monkeypatch):
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
    _boot(sysfs, "boot-a")
    pcie_guard.begin_offbus_reset("mesh reset", 1)  # the host dies before end_offbus_reset
    monkeypatch.setattr(pcie_guard, "_INTENT_OPEN", False)
    _boot(sysfs, "boot-b")
    latch = pcie_guard.check_offbus_reset_latch(lambda m: None)
    assert latch["boot_id"] == "boot-a" and latch["kind"] == "mesh reset"
    assert events[0][0] == "offbus_reset_hold_latched" and events[0][1]["host_at_risk"] is True
    assert not (pcie_guard.health_dir() / pcie_guard.INTENT_FILE).exists()
    assert pcie_guard.gate_mode() == pcie_guard.GATE_HOLD, "the latch overrides a configured gate of off"
    allowed, why = pcie_guard.host_reset_gate(1)
    assert not allowed and "--clear-hang-latch" in why
    assert pcie_guard.host_reset_gate(0)[0] is True, "a reset with every chip present still runs"
    assert pcie_guard.check_offbus_reset_latch(lambda m: None) is not None, "the latch survives a restart"
    assert pcie_guard.main(["--clear-hang-latch"]) == 0
    assert pcie_guard.offbus_reset_latched() is None
    assert pcie_guard.gate_mode() == pcie_guard.GATE_OFF


def test_an_intent_from_this_boot_does_not_latch(sysfs):
    _boot(sysfs, "boot-a")
    pcie_guard.begin_offbus_reset("tray re-power", 1)
    pcie_guard.check_offbus_reset_latch(lambda m: None)  # broker restarted, host did not
    assert pcie_guard.offbus_reset_latched() is None


def test_the_latch_can_be_switched_off(sysfs, monkeypatch):
    _boot(sysfs, "boot-a")
    pcie_guard.begin_offbus_reset("tray re-power", 1)
    _boot(sysfs, "boot-b")
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HANG_LATCH", "0")
    assert pcie_guard.check_offbus_reset_latch(lambda m: None) is None
    assert pcie_guard.offbus_reset_latched() is None


@pytest.mark.asyncio
async def test_a_tray_re_power_carries_an_intent_and_ends_it(monkeypatch, clear_job_state, galaxy_trays):
    """The walk writes the intent before it fires and removes it once its verify is over."""
    monkeypatch.setattr(pcie_guard, "_boot_id", lambda: "boot-a")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    intent = pcie_guard.health_dir() / pcie_guard.INTENT_FILE
    seen = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: seen.append(intent.exists()), raising=False)
    beats = {str(i): 100 for i in range(31)}  # chip 31 off
    assert await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 1, 32, lambda m: None) is True
    assert seen == [True], "the intent is on disk while the re-power runs"
    assert not intent.exists()


# ---------------------------------------------------------------- per-chip bridge SBR under the gate


@pytest.mark.asyncio
@pytest.mark.parametrize("latched", [False, True])
async def test_the_per_chip_bridge_reset_obeys_the_hold_gate_and_the_latch(sysfs, monkeypatch, latched):
    from tt_device_mcp.health import recovery as recovery_pkg

    if latched:
        monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
        (pcie_guard.health_dir() / pcie_guard.LATCH_FILE).write_text('{"boot_id": "boot-a"}')
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    srv.isolated_chips = {"1"}
    srv.device_pci_map = {"1": "0000:41:00.0"}
    monkeypatch.setattr(recovery_pkg, "bridge_reset_enabled", lambda: True)
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda *a: pytest.fail("gated SBR fired"))
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    g = srv.galaxy_recovery
    await g._recover_isolated_chips(lambda m: None)
    assert g.last_bridge_reset_reasons == {"1": {"reason": "gated"}}
    srv.isolated_chips = set()


# ---------------------------------------------------------------- last-chance sweep: affected trays only


@pytest.mark.asyncio
@pytest.mark.parametrize("off, trays", [(set(range(24, 32)), [3]), (set(), [])])
async def test_the_last_chance_sweep_re_powers_only_the_affected_trays(
    monkeypatch, clear_job_state, galaxy_trays, off, trays
):
    """Spec 04 I21: a caller that names no off-bus chips gets them read from the heartbeats; only trays
    holding one are re-powered. A present mesh gets SBR and the mesh reset but no tray re-power."""
    g = srv.galaxy_recovery
    swept = []

    async def sweep(bitmap, ids, trays_, expected, log, do_sbr):
        swept.append(trays_)
        return True

    async def bad(expected, log, run_fabric=True, **k):
        return False, {}

    monkeypatch.setattr(g, "_issue_all_resets_back_to_back", sweep)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(g.deps, "read_heartbeats", lambda: {str(i): 100 for i in range(32) if i not in off})
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    patch_recovery(monkeypatch, "_verify_device", bad)
    assert await g._settle_and_verify_before_host_rung(32, lambda m: None, "x") is False
    assert swept == [trays]


# ---------------------------------------------------------------- review #52 fixes


def _take_off_bus(root, chip):
    """Chip ``chip`` leaves the bus: its PCI function and its /dev class node are gone; its root
    port (a host bridge) stays."""
    _c, bus, _rp = CHIPS[chip]
    os.unlink(root / f"sys/bus/pci/devices/0000:{bus:02x}:00.0")
    (root / f"sys/class/tenstorrent/tenstorrent!{chip}/device").unlink()
    (root / f"sys/class/tenstorrent/tenstorrent!{chip}").rmdir()


def _forget_in_process(monkeypatch):
    """A new broker process: only the persisted topology is left."""
    monkeypatch.setattr(pcie_guard, "_SEEN", None)


def test_an_off_bus_chips_root_port_is_masked_for_its_trays_re_power(sysfs, monkeypatch):
    """Tray-down: the re-powered tray's chip is off the bus, so no function names its root port. The
    port an earlier look saw (in this process, or persisted by an earlier one) is still masked."""
    pcie_guard.tt_endpoints()  # the broker saw the mesh whole once
    _take_off_bus(sysfs, 0)
    for fresh in (False, True):
        if fresh:
            _forget_in_process(monkeypatch)
        plan = pcie_guard.plan_tray_repower(0b1, [0])
        assert not plan.mismatch
        assert plan.root_ports == sorted(PORTS), "tray 1's own port 0000:00:01.1 is masked too"
        assert pcie_guard.tt_root_ports() == sorted(PORTS), "and the mesh reset masks it too"
    seen = []
    pcie_guard.safe_tray_repower(
        0b1, [0], lambda: seen.append(_masked(sysfs, "0000:00:01.1")), lambda m: None, sleep=lambda s: None
    )
    assert seen == [True]
    assert _restored(sysfs, "0000:00:01.1")


def test_guard_masks_an_off_bus_chips_port_around_a_mesh_reset(sysfs, monkeypatch):
    pcie_guard.tt_endpoints()
    _take_off_bus(sysfs, 1)
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    mask = pcie_guard.mask_for_mesh_reset(lambda m: None)
    assert "0000:40:01.1" in mask.ports and _masked(sysfs, "0000:40:01.1")
    mask.restore(quiet_sec=0)


def test_a_remembered_port_gone_from_sysfs_is_not_masked(sysfs, monkeypatch):
    pcie_guard.tt_endpoints()
    _forget_in_process(monkeypatch)
    _take_off_bus(sysfs, 0)
    os.unlink(sysfs / "sys/bus/pci/devices/0000:00:01.1")
    assert "0000:00:01.1" not in pcie_guard.tt_root_ports()


@pytest.mark.parametrize("ever_seen", [True, False])
def test_an_all_off_bus_mesh_still_gets_its_trays_re_powered(sysfs, monkeypatch, ever_seen):
    """Every chip off the bus: there is no healthy tray to cut and nothing to cross-check, so the
    re-power goes ahead (the old path re-powered every tray here before the power cycle)."""
    if ever_seen:
        pcie_guard.tt_endpoints()
    else:
        _forget_in_process(monkeypatch)
        (pcie_guard.health_dir() / pcie_guard.TOPOLOGY_FILE).unlink(missing_ok=True)
    for chip in range(len(CHIPS)):
        _take_off_bus(sysfs, chip)
    if not ever_seen:
        _forget_in_process(monkeypatch)
    plan = pcie_guard.plan_tray_repower(0b1111, [0, 1, 2, 3])
    assert not plan.mismatch
    assert plan.root_ports == (sorted(PORTS) if ever_seen else [])
    fired = []
    pcie_guard.safe_tray_repower(0b1111, [0, 1, 2, 3], lambda: fired.append(1), lambda m: None, sleep=lambda s: None)
    assert fired == [1]
    assert pcie_guard.tray_bus_groups() == ({1: 0x00, 2: 0x40, 3: 0xC0, 4: 0x80} if ever_seen else None)


@pytest.mark.asyncio
async def test_an_all_off_bus_last_chance_sweep_is_complete(sysfs, monkeypatch):
    """With the gate off, an all-off mesh's sweep re-powers every tray and is COMPLETE, so the power
    cycle above it is not held (I18) — the old path's recovery."""
    pcie_guard.tt_endpoints()
    for chip in range(len(CHIPS)):
        _take_off_bus(sysfs, chip)
    monkeypatch.delenv("TT_DEVICE_MCP_HOST_RESET_GATE", raising=False)
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "_set_device_pollers", None, raising=False)
    fired = []

    def fire(bitmap, ids, log=None):
        return pcie_guard.safe_tray_repower(
            bitmap, ids, lambda: fired.append(bitmap), lambda m: None, sleep=lambda s: None
        )

    async def reset(*a, **k):
        return 0, ""

    monkeypatch.setattr(galaxy, "_fire_ubb_reset", fire)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    monkeypatch.setattr(galaxy, "_ubb_reset_enabled", lambda: True)
    complete = await g._issue_all_resets_back_to_back(
        0b1111, [0, 1, 2, 3], [1, 2, 3, 4], 4, lambda m: None, do_sbr=False
    )
    assert fired == [0b1111] and complete is True


def test_the_clear_command_finds_the_system_brokers_latch(sysfs, monkeypatch, capsys):
    """An operator shell resolves another health dir than the root broker's: the clear still finds
    and lifts the system broker's latch, and the logged command names the broker's own dir."""
    system = sysfs / "var/lib/tt-device-broker/health"
    system.mkdir(parents=True)
    (system / pcie_guard.LATCH_FILE).write_text(json.dumps({"boot_id": "boot-a"}))
    assert pcie_guard.health_dir() != system
    assert pcie_guard.main(["--clear-hang-latch"]) == 0
    assert "cleared" in capsys.readouterr().out
    assert not (system / pcie_guard.LATCH_FILE).exists()
    other = sysfs / "elsewhere"
    other.mkdir()
    (other / pcie_guard.LATCH_FILE).write_text("{}")
    assert pcie_guard.main(["--clear-hang-latch", "--health-dir", str(other)]) == 0
    assert not (other / pcie_guard.LATCH_FILE).exists()
    assert pcie_guard.main(["--clear-hang-latch"]) == 0
    assert "no host-hang hold was latched in" in capsys.readouterr().out
    assert pcie_guard.main(["--clear-hang-latch", "--bogus"]) == 2
    cmd = pcie_guard.clear_latch_cmd()
    assert cmd.startswith("sudo ") and sys.executable in cmd and f"--health-dir {pcie_guard.health_dir()}" in cmd


def test_a_latch_that_cannot_be_removed_is_reported_not_called_absent(sysfs, monkeypatch, capsys):
    (pcie_guard.health_dir() / pcie_guard.LATCH_FILE).write_text("{}")

    def denied(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pcie_guard.Path, "unlink", denied)
    assert pcie_guard.main(["--clear-hang-latch"]) == 1
    out = capsys.readouterr().out
    assert "could not be cleared" in out and "no host-hang hold" not in out


def test_the_flood_window_holds_automatic_resets_even_with_the_gate_off(sysfs, monkeypatch):
    """A port the envelope had to leave masked opens the flood window, and the next automatic reset
    is refused whatever the gate's mode — that reset is the incident's re-power-then-glx_reset."""
    monkeypatch.delenv("TT_DEVICE_MCP_HOST_RESET_GATE", raising=False)

    def fire():
        _set_cfg(sysfs, "0000:40:01.1", AER + 0x10, 4, 0x2000)

    plan = pcie_guard.safe_tray_repower(0b1000, [3], fire, lambda m: None, sleep=lambda s: None)
    assert plan.kept_masked
    allowed, why = pcie_guard.host_reset_gate(0)
    assert not allowed and "flood window" in why
    monkeypatch.setattr(pcie_guard, "_FLOOD_UNTIL", 0.0)
    assert pcie_guard.host_reset_gate(5) == (True, ""), "off, with no window open, is the old behaviour"


@pytest.mark.asyncio
async def test_after_a_kept_masked_port_the_stuck_hold_mesh_reset_does_not_fire(sysfs, monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_HOST_RESET_GATE", raising=False)
    pcie_guard._note_flood()
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", lambda log: _false())
    monkeypatch.setattr(g.monitor, "expected", lambda n: 4)

    async def no_reset(*a, **k):
        pytest.fail("mesh reset fired inside the flood window")

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", no_reset)
    assert await g._reset_and_verify_device(["0", "1", "2", "3"], lambda m: None) is False


def test_a_steady_trickle_between_distant_looks_is_not_a_flood(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    counter = sysfs / "sys/bus/pci/devices/0000:40:01.1/aer_rootport_total_err_cor"
    clock = [10_000.0]
    monkeypatch.setattr(pcie_guard.time, "monotonic", lambda: clock[0])
    assert pcie_guard.host_reset_gate(0)[0]
    clock[0] += 6 * 3600  # six hours later, 300 more correctable errors: under one a minute
    counter.write_text("300\n")
    assert pcie_guard.host_reset_gate(0)[0], "a trickle hours apart is not a flood"
    clock[0] += 10  # 100 more in ten seconds is
    counter.write_text("400\n")
    allowed, why = pcie_guard.host_reset_gate(0)
    assert not allowed and "flooding" in why


def test_a_trickle_since_an_old_boot_is_not_a_flood(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    (sysfs / "proc").mkdir()
    (sysfs / "proc/uptime").write_text("1500.0 1000.0\n")  # 25 min up, 60 errors since: a trickle
    (sysfs / "sys/bus/pci/devices/0000:40:01.1/aer_rootport_total_err_cor").write_text("60\n")
    assert pcie_guard.host_reset_gate(0)[0]


@pytest.mark.asyncio
async def test_a_gated_mesh_reset_records_no_reset_and_arms_no_cooldown(sysfs, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    os.unlink(sysfs / "sys/bus/pci/devices/0000:41:00.0")
    events = []
    patch_health_event(monkeypatch, lambda name, **k: events.append(name))
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", lambda log: _false())
    monkeypatch.setattr(g.monitor, "expected", lambda n: 4)
    monkeypatch.setattr(g.mechanism, "last_reset_monotonic", 123.0)
    assert await g._reset_and_verify_device(["0", "1", "2", "3"], lambda m: None) is False
    assert "host_reset_gated" in events and "reset_begin" not in events
    assert g.mechanism.last_reset_monotonic == 123.0


def test_the_envelope_masks_before_it_quiesces_and_refuses_when_it_cannot_mask(sysfs, monkeypatch):
    seen = []
    pcie_guard.safe_tray_repower(
        0b1000,
        [3],
        lambda: None,
        lambda m: None,
        quiesce=lambda chips: seen.append(all(_masked(sysfs, p) for p in PORTS)),
        sleep=lambda s: None,
    )
    assert seen == [True], "the ports are masked before any chip is touched"

    real_apply = pcie_guard.AerMask.apply

    def failing_apply(self):
        self.ports = self.ports + ["0000:ff:00.0"]  # no such function: its config read fails
        return real_apply(self)

    monkeypatch.setattr(pcie_guard.AerMask, "apply", failing_apply)
    with pytest.raises(pcie_guard.TrayRepowerRefused, match="could not mask"):
        pcie_guard.safe_tray_repower(
            0b1000,
            [3],
            lambda: pytest.fail("fired unmasked"),
            lambda m: None,
            quiesce=lambda chips: pytest.fail("quiesced with no mask"),
            sleep=lambda s: None,
        )
    assert all(_restored(sysfs, p) for p in PORTS), "a half-applied mask is put back"


def test_a_quiesce_that_raises_still_re_inits_and_restores(sysfs):
    steps = []

    def quiesce(chips):
        raise RuntimeError("USER_RESET ioctl failed")

    with pytest.raises(RuntimeError):
        pcie_guard.safe_tray_repower(
            0b1000,
            [3],
            lambda: pytest.fail("fired after a failed quiesce"),
            lambda m: None,
            quiesce=quiesce,
            reinit=lambda chips: steps.append(("reinit", chips)),
            sleep=lambda s: None,
        )
    assert steps == [("reinit", [3])]
    assert all(_restored(sysfs, p) for p in PORTS)


# ---------------------------------------------------------------- re-review #52 fixes


def _no_topology(monkeypatch):
    """A first start on this host: no look yet, in this process or persisted."""
    _forget_in_process(monkeypatch)
    (pcie_guard.health_dir() / pcie_guard.TOPOLOGY_FILE).unlink(missing_ok=True)


def _tray_1_plan(sysfs, monkeypatch):
    """Tray 1's chip leaves the bus and a new broker process plans its re-power."""
    _take_off_bus(sysfs, 0)
    _forget_in_process(monkeypatch)
    return pcie_guard.plan_tray_repower(0b1, [0])


def test_without_a_healthy_look_an_off_bus_chips_port_is_not_known(sysfs, monkeypatch):
    """The documented limit (spec 04 I19): a port never seen with its chip present is not masked."""
    _no_topology(monkeypatch)
    plan = _tray_1_plan(sysfs, monkeypatch)
    assert "0000:00:01.1" not in plan.root_ports


def test_broker_start_records_the_topology_for_the_first_tray_drop(sysfs, monkeypatch):
    _no_topology(monkeypatch)
    srv.pcie_guard_at_start(lambda m: None)  # what the broker runs at start
    assert (pcie_guard.health_dir() / pcie_guard.TOPOLOGY_FILE).exists()
    assert not [f for f in os.listdir(pcie_guard.health_dir()) if f.endswith(".tmp")], "no temp file left"
    plan = _tray_1_plan(sysfs, monkeypatch)
    assert not plan.mismatch
    assert plan.root_ports == sorted(PORTS), "tray 1's own port 0000:00:01.1 is masked"


@pytest.mark.asyncio
async def test_the_between_jobs_check_records_the_topology_for_the_first_tray_drop(sysfs, monkeypatch, health_deps):
    from tt_device_mcp.health.monitor import HealthMonitor
    from tt_device_mcp.health.monitors import hostpci

    _no_topology(monkeypatch)
    monkeypatch.setattr(hostpci, "host_pci_verdict", lambda: (True, "ok", {}))
    m = HealthMonitor(health_deps)

    async def healthy(*a, **k):
        return True, "ok"

    monkeypatch.setattr(m, "_verify_device", healthy)
    await m.update("post-job", run_fabric=False, expected=len(CHIPS))
    plan = _tray_1_plan(sysfs, monkeypatch)
    assert not plan.mismatch
    assert plan.root_ports == sorted(PORTS), "tray 1's own port 0000:00:01.1 is masked"


def test_an_unchanged_topology_is_not_rewritten(sysfs, monkeypatch):
    _no_topology(monkeypatch)
    writes = []
    real_replace = os.replace
    monkeypatch.setattr(pcie_guard.os, "replace", lambda a, b: (writes.append(b), real_replace(a, b)))
    for _ in range(3):
        pcie_guard.record_topology()
    assert len(writes) == 1


# ---------------------------------------------------------------- unreadable sysfs fails closed


def _deny(monkeypatch, method, target):
    """``Path.<method>`` on ``target`` fails as for a non-root reader (chmod does not stop root)."""
    real = getattr(pathlib.Path, method)

    def guarded(self, *a, **k):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, method, guarded)


def _break_sysfs(root, monkeypatch, how):
    devs = root / "sys/bus/pci/devices"
    if how == "devices dir missing":
        shutil.rmtree(devs)
    elif how == "devices dir unlistable":
        _deny(monkeypatch, "iterdir", devs)
    elif how == "vendor unreadable":
        _deny(monkeypatch, "read_text", devs / "0000:01:00.0" / "vendor")
    elif how == "vendor unparseable":
        (devs / "0000:01:00.0" / "vendor").write_text("garbage\n")
    elif how == "root port vendor unreadable":
        _deny(monkeypatch, "read_text", devs / "0000:00:01.1" / "vendor")
    elif how == "chip with no PCI address":
        (devs / "bogus").symlink_to(devs / "0000:01:00.0")
    elif how == "chip class dir unlistable":
        _deny(monkeypatch, "iterdir", root / "sys/class/tenstorrent")
    elif how == "device id unreadable":
        _deny(monkeypatch, "read_text", devs / "0000:01:00.0" / "device")
    else:
        raise AssertionError(how)


UNREADABLE = [
    "devices dir missing",
    "devices dir unlistable",
    "vendor unreadable",
    "vendor unparseable",
    "root port vendor unreadable",
    "chip with no PCI address",
    "chip class dir unlistable",
    "device id unreadable",
]


@pytest.mark.parametrize("all_off", [False, True])
@pytest.mark.parametrize("how", UNREADABLE)
def test_a_tray_re_power_refuses_when_sysfs_cannot_be_read(sysfs, monkeypatch, how, all_off):
    """Sysfs that cannot be read is not "every chip off the bus": the #27 cross-check cannot run, so
    the envelope refuses before it touches anything and says why (spec 04 I19)."""
    pcie_guard.record_topology()  # a healthy look first: the remembered ports must not make it safe
    if all_off:
        for chip in range(1, len(CHIPS)):
            _take_off_bus(sysfs, chip)
    _break_sysfs(sysfs, monkeypatch, how)
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    plan = pcie_guard.plan_tray_repower(0b1, [0])
    assert plan.mismatch, f"{how}: an unreadable sysfs must refuse the re-power"
    fired, logs = [], []
    with pytest.raises(pcie_guard.TrayRepowerRefused) as exc:
        pcie_guard.safe_tray_repower(0b1, [0], lambda: fired.append(1), logs.append, sleep=lambda s: None)
    assert fired == []
    assert not any(_masked(sysfs, p) for p in PORTS if (sysfs / "sys/bus/pci/devices" / p).exists())
    refused = [k for n, k in events if n == "tray_repower_refused"]
    assert refused and refused[0]["reason"] == str(exc.value)
    if how != "device id unreadable":  # that one refuses on the missing tray table, as before
        assert "cannot read" in str(exc.value)


def test_an_all_off_mesh_behind_an_unreadable_sysfs_is_not_re_powered(sysfs, monkeypatch):
    """The reported case: no function listed because /sys/bus/pci/devices cannot be read."""
    pcie_guard.record_topology()
    _break_sysfs(sysfs, monkeypatch, "devices dir unlistable")
    assert pcie_guard.tt_endpoints() is None
    assert "cannot read" in pcie_guard.plan_tray_repower(0b1111, [0, 1, 2, 3]).mismatch
    assert pcie_guard.main(["0b1111", "0", "1", "2", "3"]) == 1


@pytest.mark.parametrize("how", UNREADABLE[:-1])
def test_an_unreadable_sysfs_counts_every_chip_off_so_a_hold_gate_holds(sysfs, monkeypatch, how):
    _break_sysfs(sysfs, monkeypatch, how)
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    off_bus = pcie_guard.chips_off_bus(len(CHIPS))
    if how == "devices dir missing":
        assert off_bus == 0, "no PCI sysfs at all: nothing to count, the AER evidence decides"
    else:
        assert off_bus == len(CHIPS)
        assert pcie_guard.host_reset_gate(off_bus)[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("how", UNREADABLE[:-1])
async def test_an_unreadable_look_keeps_the_recorded_topology(sysfs, monkeypatch, health_deps, how):
    """Broker start and the between-jobs check look at sysfs (#292): a look that cannot read it
    never raises, never shrinks the recorded topology and leaves a health event naming why."""
    from tt_device_mcp.health.monitor import HealthMonitor
    from tt_device_mcp.health.monitors import hostpci

    _no_topology(monkeypatch)
    pcie_guard.record_topology()
    path = pcie_guard.health_dir() / pcie_guard.TOPOLOGY_FILE
    before = path.read_text()
    _break_sysfs(sysfs, monkeypatch, how)
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    srv.pcie_guard_at_start(lambda m: None)
    monkeypatch.setattr(hostpci, "host_pci_verdict", lambda: (True, "ok", {}))
    m = HealthMonitor(health_deps)

    async def healthy(*a, **k):
        return True, "ok"

    monkeypatch.setattr(m, "_verify_device", healthy)
    await m.update("post-job", run_fabric=False, expected=len(CHIPS))
    assert path.read_text() == before
    unreadable = [k for n, k in events if n == "pci_topology_unreadable"]
    assert len(unreadable) == 2 and all("cannot read" in k["reason"] for k in unreadable)
    _forget_in_process(monkeypatch)
    assert "cannot read" in pcie_guard.plan_tray_repower(0b1, [0]).mismatch


# ---------------------------------------------------------------- entries the guard does not need


def _vmd_entry(root, bdf="10000:e0:01.0", vendor="0x8086\n"):
    """A function in a 5-digit PCI domain behind an Intel VMD controller, which is not a chip."""
    d = root / f"sys/devices/pci0000:00/0000:00:0e.0/pci{bdf.split(':')[0]}:{bdf.split(':')[1]}/{bdf}"
    d.mkdir(parents=True)
    if vendor is not None:
        (d / "vendor").write_text(vendor)
    (root / "sys/bus/pci/devices" / bdf).symlink_to(d)
    return d


def test_a_5_digit_domain_beside_the_mesh_does_not_make_sysfs_unreadable(sysfs, monkeypatch):
    """A VMD host lists domains >= 0x10000 with 5+ digits. Such an entry is a PCI address, and an
    entry that is no Tenstorrent function never makes the whole of sysfs unreadable."""
    _vmd_entry(sysfs)
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    pcie_guard.record_topology()
    assert events == []
    rec = json.loads((pcie_guard.health_dir() / pcie_guard.TOPOLOGY_FILE).read_text())
    assert rec["root_ports"] == sorted(PORTS)
    plan = pcie_guard.plan_tray_repower(0b1, [0])
    assert not plan.mismatch and plan.functions == ["0000:01:00.0"] and plan.root_ports == sorted(PORTS)
    assert pcie_guard.chips_off_bus(len(CHIPS)) == 0


def test_a_chip_in_a_5_digit_domain_is_parsed_and_counted(sysfs):
    """Tray 1's chip behind a VMD controller: its own domain's port is its root port, not the VMD
    controller in domain 0000 above it."""
    _take_off_bus(sysfs, 0)
    rp = _vmd_entry(sysfs, "10000:00:01.1")
    (rp / "config").write_bytes(bytes(_port_config()))
    ep = rp / "10000:01:00.0"
    ep.mkdir()
    (ep / "vendor").write_text("0x1e52\n")
    (ep / "device").write_text("0xb140\n")
    (sysfs / "sys/bus/pci/devices/10000:01:00.0").symlink_to(ep)
    cls = sysfs / "sys/class/tenstorrent/tenstorrent!0"
    cls.mkdir()
    (cls / "device").symlink_to(ep)
    eps = pcie_guard.tt_endpoints()
    assert eps["10000:01:00.0"] == pcie_guard.Endpoint("10000:01:00.0", 0x01, "10000:00:01.1", 0, 0xB140)
    assert pcie_guard.chips_off_bus(len(CHIPS)) == 0
    plan = pcie_guard.plan_tray_repower(0b1, [0])
    assert not plan.mismatch and plan.functions == ["10000:01:00.0"] and plan.chips == [0]
    assert "10000:00:01.1" in plan.root_ports and "0000:00:0e.0" not in plan.root_ports


@pytest.mark.parametrize("how", ["entry name unparseable", "vendor unreadable", "vendor unparseable"])
def test_an_unrelated_entry_the_scan_cannot_read_is_skipped(sysfs, monkeypatch, how):
    devs = sysfs / "sys/bus/pci/devices"
    if how == "entry name unparseable":
        (devs / "bogus").symlink_to(sysfs / "sys/devices")
    else:
        d = _vmd_entry(sysfs, "0000:20:00.0", vendor="garbage\n")
        if how == "vendor unreadable":
            _deny(monkeypatch, "read_text", devs / "0000:20:00.0" / "vendor")
        assert d.is_dir()
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    assert sorted(pcie_guard.tt_endpoints()) == sorted(f"0000:{b:02x}:00.0" for _c, b, _r in CHIPS)
    pcie_guard.record_topology()
    assert not pcie_guard.plan_tray_repower(0b1, [0]).mismatch
    assert pcie_guard.chips_off_bus(len(CHIPS)) == 0
    assert events == []


def test_an_unreadable_sysfs_under_guard_logs_a_mask_failure(sysfs, monkeypatch):
    """The mesh-reset mask cannot list the ports when sysfs is unreadable: it says so in the journal
    and masks the ports an earlier look recorded."""
    pcie_guard.record_topology()
    _break_sysfs(sysfs, monkeypatch, "devices dir unlistable")
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "guard")
    events, logs = [], []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    mask = pcie_guard.mask_for_mesh_reset(logs.append)
    failed = [k for n, k in events if n == "aer_mask_failed"]
    assert len(failed) == 1 and "cannot read" in failed[0]["error"] and failed[0]["host_at_risk"]
    assert logs and "cannot read" in logs[0]
    assert sorted(mask.ports) == sorted(PORTS) and all(_masked(sysfs, p) for p in PORTS)
    mask.restore(quiet_sec=0)
