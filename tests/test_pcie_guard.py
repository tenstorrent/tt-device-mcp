# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The host-safety envelope around a per-tray re-power and the per-host reset gate (spec 04 I18-I20).

Every test runs against a fake sysfs tree: four Blackhole trays, one chip each, the kernel's /dev
order 0x0X, 0x4X, 0xCX, 0x8X (so trays 3 and 4 are where list position puts them the wrong way
round, issue #27). Nothing here reaches a real device or a real config space.
"""

import os
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
