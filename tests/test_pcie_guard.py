# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The host-safety envelope around a per-tray re-power and the per-host reset gate (spec 04 I22-I24).

Every test runs against a fake sysfs tree: four Blackhole trays, one chip each, the kernel's /dev
order 0x0X, 0x4X, 0xCX, 0x8X (so trays 3 and 4 are where list position puts them the wrong way
round, issue #27). Nothing here reaches a real device or a real config space.
"""

import json
import os
import pathlib
import shutil
import sys
import time
import types

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.monitors import pci
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
        (rp_dir / f"pci_bus/0000:{bus:02x}").mkdir(parents=True)  # the bus below the port
        (ep_dir / "vendor").write_text("0x1e52\n")
        (ep_dir / "device").write_text("0xb140\n")
        (devs / rp).symlink_to(rp_dir)
        (devs / ep).symlink_to(ep_dir)
        cls = tmp_path / f"sys/class/tenstorrent/tenstorrent!{chip}"
        cls.mkdir()
        (cls / "device").symlink_to(ep_dir)
        (cls / "tt_heartbeat").write_text("100\n")
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


def _rescanned(root) -> list:
    """The root ports whose bus below was rescanned on its own."""
    return sorted(rp for rp in PORTS if any((root / "sys/bus/pci/devices" / rp / "pci_bus").glob("*/rescan")))


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
    assert _rescanned(sysfs) == ["0000:80:01.1"]
    assert all(_restored(sysfs, p) for p in PORTS), "a quiet port gets its exact old settings back"
    assert plan.kept_masked == []


def test_envelope_restores_and_rescans_even_when_the_pulse_fails(sysfs):
    def fire():
        raise RuntimeError("ipmitool exited 1")

    with pytest.raises(RuntimeError):
        pcie_guard.safe_tray_repower(0b1000, [3], fire, lambda m: None, sleep=lambda s: None)
    assert _rescanned(sysfs) == ["0000:80:01.1"]
    assert all(_restored(sysfs, p) for p in PORTS)


ALL_ONES = "4294967295\n"


def _beat(root, chip, value):
    (root / f"sys/class/tenstorrent/tenstorrent!{chip}/tt_heartbeat").write_text(value)


def test_the_envelope_rescans_only_the_re_powered_trays_root_ports(sysfs):
    """A global rescan walks every other tray's ports too; after each per-tray re-power on a Galaxy the
    slot-6 chip of every other tray then read all-ones. Only the re-powered tray's port is rescanned,
    the bare global rescan never, and a tray whose chip is off the bus still has its port found."""
    plan = pcie_guard.safe_tray_repower(0b0010, [1], lambda: None, lambda m: None, sleep=lambda s: None)
    assert plan.tray_ports == ["0000:40:01.1"]
    assert _rescanned(sysfs) == ["0000:40:01.1"]
    assert not (sysfs / "sys/bus/pci/rescan").exists(), "never the whole bus"
    # Chip 1 off the bus (its function gone): its port, remembered and still in sysfs, is the tray's.
    (sysfs / "sys/bus/pci/devices/0000:41:00.0").unlink()
    assert pcie_guard.tray_root_ports([2]) == ["0000:40:01.1"]
    assert pcie_guard.tray_root_ports([1, 3]) == ["0000:00:01.1", "0000:c0:01.1"]


def test_the_envelope_falls_back_to_the_global_rescan_when_no_tray_port_is_known(sysfs):
    """With no port of the tray rescannable on its own, a removed chip could never come back: then,
    and only then, the whole bus is rescanned."""
    for rp in PORTS:
        shutil.rmtree(sysfs / "sys/bus/pci/devices" / rp / "pci_bus")
    lines = []
    pcie_guard.safe_tray_repower(0b1000, [3], lambda: None, lines.append, sleep=lambda s: None)
    assert (sysfs / "sys/bus/pci/rescan").read_text() == "1"
    assert any("rescanning the whole bus" in m for m in lines)


def test_a_sibling_chip_newly_all_ones_gets_one_remove_and_scoped_rescan(sysfs, monkeypatch):
    """Tray 4's re-power leaves chip 0 (tray 1) reading all-ones: it gets one remove and a rescan of its
    own port, with AER still masked, and is reported. Back after that: still_off is empty; still
    all-ones: it is reported still off and left to the walk (the mesh reset)."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    real_write = pcie_guard._write
    writes = []

    def write(path, text):
        writes.append((str(path.relative_to(sysfs)), _masked(sysfs, "0000:00:01.1")))
        real_write(path, text)
        if path.name == "rescan" and "0000:00:01.1" in str(path) and recovers:
            _beat(sysfs, 0, "100\n")

    monkeypatch.setattr(pcie_guard, "_write", write)
    for recovers, still_off in ((True, []), (False, [0])):
        events.clear()
        writes.clear()
        _beat(sysfs, 0, "100\n")
        plan = pcie_guard.safe_tray_repower(
            0b1000, [3], lambda: _beat(sysfs, 0, ALL_ONES), lambda m: None, sleep=lambda s: None
        )
        assert plan.siblings_dropped == [0] and plan.siblings_still_off == still_off
        sib = [w for w in writes if "0000:01:00.0" in w[0] or "0000:00:01.1" in w[0]]
        assert [w[0].rsplit("/", 1)[-1] for w in sib] == ["remove", "rescan"], "one remove, one scoped rescan"
        assert all(masked for _p, masked in sib), "done while AER is still masked"
        assert dict(events)["tray_repower_sibling_offbus"]["chips"] == [0]
        assert dict(events)["tray_repower_sibling_offbus"]["still_off"] == still_off
        assert dict(events)["tray_repower_envelope"]["siblings_still_off"] == still_off
        assert all(_restored(sysfs, p) for p in PORTS)


def test_a_sibling_chip_already_all_ones_or_on_the_tray_is_not_touched(sysfs, monkeypatch):
    """Only a chip on ANOTHER tray that read live before the pulse counts: one already all-ones before
    it (chip 1) or one on the re-powered tray (chip 3) is never removed by the check."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    _beat(sysfs, 1, ALL_ONES)

    def fire():
        _beat(sysfs, 3, ALL_ONES)

    plan = pcie_guard.safe_tray_repower(0b1000, [3], fire, lambda m: None, sleep=lambda s: None)
    assert plan.siblings_dropped == []
    assert not (sysfs / "sys/bus/pci/devices/0000:41:00.0/remove").exists()
    assert "tray_repower_sibling_offbus" not in dict(events)
    assert _rescanned(sysfs) == ["0000:80:01.1"]


def test_a_disabled_tray_walk_leaves_the_envelope_armed(sysfs, monkeypatch):
    """Disabling (a tray re-power regressed) is a ladder decision: the envelope itself never refuses a
    tray fire on it, since a refused fire inside the sweep would hold the host rung forever (I22). The
    ubb_tray_rung_disabled event is left once, however often it is disabled."""
    events, fired = [], []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    assert pcie_guard.tray_rung_disabled() is None
    pcie_guard.disable_tray_rung(tray=3, new_off=["5"])
    pcie_guard.disable_tray_rung(tray=1, new_off=["13"])
    assert [n for n, _ in events] == ["ubb_tray_rung_disabled"]
    assert pcie_guard.tray_rung_disabled() == {"tray": 3, "new_off": ["5"]}
    pcie_guard.safe_tray_repower(0b1000, [3], lambda: fired.append(1), lambda m: None, sleep=lambda s: None)
    assert fired == [1]
    assert "tray_repower_refused" not in dict(events)


def test_a_disabled_tray_rung_survives_a_broker_restart_on_the_same_boot(sysfs, monkeypatch):
    """The record goes to the health dir with the boot id: a new broker process on the same boot
    (in-memory state gone) still declines the tray rung."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    _boot(sysfs, "boot-a")
    pcie_guard.disable_tray_rung(tray=[3], new_off=["5", "13"], signature="position5")
    rec = json.loads((pcie_guard.health_dir() / pcie_guard.TRAY_RUNG_FILE).read_text())
    assert rec["boot_id"] == "boot-a" and rec["why"]["tray"] == [3]
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)  # a broker restart
    assert pcie_guard.tray_rung_disabled() == {"tray": [3], "new_off": ["5", "13"], "signature": "position5"}
    assert "ubb_tray_rung_disabled_restored" in [n for n, _ in events]
    pcie_guard.disable_tray_rung(tray=[1], new_off=["21"])
    assert [n for n, _ in events].count("ubb_tray_rung_disabled") == 1, "restored: not latched again"


def test_a_new_boot_keeps_the_tray_rung_latch(sysfs, monkeypatch):
    """A power cycle does not repair the UBB whose re-power regressed: a record from an earlier boot
    still declines the rung (measured: re-armed on each new boot, the same tray re-power took 20 more
    chips off three times in an hour)."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    _boot(sysfs, "boot-a")
    pcie_guard.disable_tray_rung(tray=[1], new_off=[str(c) for c in range(8, 28)])
    path = pcie_guard.health_dir() / pcie_guard.TRAY_RUNG_FILE
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)
    _boot(sysfs, "boot-b")
    assert pcie_guard.tray_rung_disabled() == {"tray": [1], "new_off": [str(c) for c in range(8, 28)]}
    assert path.exists()
    restored = dict(events)["ubb_tray_rung_disabled_restored"]
    assert restored["latched_boot_id"] == "boot-a" and restored["boot_id"] == "boot-b"
    assert "ubb_tray_rung_rearmed" not in dict(events)


def test_the_tray_rung_latch_expires_after_its_window(sysfs, monkeypatch):
    """Bounded: a record older than TT_DEVICE_MCP_TRAY_RUNG_LATCH_DAYS is removed and the rung re-arms,
    from disk and in memory alike."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_RUNG_LATCH_DAYS", "2")
    _boot(sysfs, "boot-a")
    now = [1_000_000.0]
    monkeypatch.setattr(pcie_guard.time, "time", lambda: now[0])
    pcie_guard.disable_tray_rung(tray=[3], new_off=["5"])
    path = pcie_guard.health_dir() / pcie_guard.TRAY_RUNG_FILE
    now[0] += 2 * 86400 - 60
    assert pcie_guard.tray_rung_disabled() == {"tray": [3], "new_off": ["5"]}
    now[0] += 120
    assert pcie_guard.tray_rung_disabled() is None, "in memory: expired"
    assert not path.exists() and "ubb_tray_rung_rearmed" in [n for n, _ in events]
    pcie_guard.disable_tray_rung(tray=[3], new_off=["5"])
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)
    now[0] += 2 * 86400
    assert pcie_guard.tray_rung_disabled() is None, "from disk: expired"
    assert not path.exists()


def test_a_tray_rung_record_from_another_host_never_counts(sysfs, monkeypatch):
    """Per host: a record a different host wrote (a copied or shared health dir) is removed."""
    monkeypatch.setattr(pcie_guard, "health_event", lambda *a, **k: None)
    _boot(sysfs, "boot-a")
    pcie_guard.disable_tray_rung(tray=[3], new_off=["5"])
    path = pcie_guard.health_dir() / pcie_guard.TRAY_RUNG_FILE
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "host": rec["host"] + "-other"}))
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)
    assert pcie_guard.tray_rung_disabled() is None
    assert not path.exists()


def test_a_tray_rung_record_without_a_time_is_aged_by_its_file(sysfs, monkeypatch):
    """A record written before the time was kept (no at_epoch, no host) is aged by the file's mtime, so
    it is bounded too: it counts inside the window and is removed past it."""
    monkeypatch.setattr(pcie_guard, "health_event", lambda *a, **k: None)
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_RUNG_LATCH_DAYS", "1")
    _boot(sysfs, "boot-a")
    path = pcie_guard.health_dir() / pcie_guard.TRAY_RUNG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"why": {"tray": [3]}, "boot_id": "boot-old"}))
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)
    assert pcie_guard.tray_rung_disabled() == {"tray": [3]}
    old = path.stat().st_mtime - 2 * 86400
    os.utime(path, (old, old))
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)
    assert pcie_guard.tray_rung_disabled() is None and not path.exists()


def test_an_unreadable_boot_id_keeps_the_tray_rung_record(sysfs, monkeypatch):
    """With this boot's id unreadable a record cannot be shown stale, so it counts (as the host-hang
    latch does); the hold it routes to is the bounded I27 hand-off."""
    monkeypatch.setattr(pcie_guard, "health_event", lambda *a, **k: None)
    _boot(sysfs, "boot-a")
    pcie_guard.disable_tray_rung(tray=[3], new_off=["5"])
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)
    (sysfs / "proc/sys/kernel/random/boot_id").unlink()
    assert pcie_guard.tray_rung_disabled() == {"tray": [3], "new_off": ["5"]}


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


def _all_ones(root, monkeypatch, chip):
    """Chip ``chip`` keeps its PCI function and class node, but its ARC heartbeat reads all-ones: the
    chip has left the bus and its BAR reads return nothing."""
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", root / "sys/class/tenstorrent")
    _beat(root, chip, ALL_ONES)


def test_chips_off_bus_counts_an_all_ones_chip_still_in_sysfs(sysfs, monkeypatch):
    """Spec 04 I24: a chip still listed in sysfs whose heartbeat reads all-ones is off the bus."""
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", sysfs / "sys/class/tenstorrent")
    assert pcie_guard.chips_off_bus(4) == 0, "ticking heartbeats are on the bus"
    _all_ones(sysfs, monkeypatch, 2)
    assert pcie_guard.chips_off_bus(4) == 1
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    assert pcie_guard.host_reset_gate(pcie_guard.chips_off_bus(4))[0] is False


@pytest.mark.asyncio
async def test_a_hold_gate_refuses_the_mesh_reset_and_the_sweep_over_an_all_ones_chip(sysfs, monkeypatch):
    """Spec 04 I24: in hold mode an all-ones chip with its function still listed refuses the mesh reset
    and the back-to-back sweep alike. No glx_reset argv is ever issued."""
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "hold")
    _all_ones(sysfs, monkeypatch, 1)
    events, argvs = [], []
    patch_health_event(monkeypatch, lambda name, **k: events.append(name))
    g = srv.galaxy_recovery

    async def reset(argv, log, *a, **k):
        argvs.append(argv)
        return 0, ""

    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", lambda log: _false())
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(g.monitor, "expected", lambda n: 4)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: pytest.fail("gated sweep fired a tray"))
    assert await g._reset_and_verify_device_once(["0", "1", "2", "3"], lambda m: None) is False
    assert await g._issue_all_resets_back_to_back(0b1000, [3], [4], 4, lambda m: None, do_sbr=False) is False
    assert argvs == [], "no mesh reset over a chip whose heartbeat reads all-ones"
    assert events.count("host_reset_gated") == 2 and "reset_begin" not in events


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


# ---------------------------------------------------------------- host-hang latch (spec 04 I25)


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


def _watch_dir_fsyncs(monkeypatch) -> list:
    """Record each fsync of the health dir, with whether the intent was still there at that moment."""
    hd = pcie_guard.health_dir()
    hd.mkdir(parents=True, exist_ok=True)
    ino, real, seen = hd.stat().st_ino, os.fsync, []

    def fsync(fd):
        if os.fstat(fd).st_ino == ino:
            seen.append((hd / pcie_guard.INTENT_FILE).exists())
        return real(fd)

    monkeypatch.setattr(pcie_guard.os, "fsync", fsync)
    return seen


@pytest.mark.asyncio
async def test_a_sweep_that_fails_its_verify_leaves_no_intent_for_the_power_cycle(
    sysfs, monkeypatch, tmp_path, clear_job_state
):
    """The last-chance sweep, its failed verify, then the broker's own power cycle: when the cycle's
    command runs, the intent is gone AND that unlink reached the disk (the directory was fsynced after
    it). An unlink still in the page cache is undone by the power loss, and the next boot latched the
    broker's own cycle as a host hang (spec 04 I25)."""
    _boot(sysfs, "boot-a")
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 8)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    intent = pcie_guard.health_dir() / pcie_guard.INTENT_FILE
    during = []

    async def mesh(argv, log, *a, **k):
        during.append(intent.exists())
        return 1, "glx_reset failed"

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)

    async def verify(expected, log, run_fabric=True, **k):
        return False, {"snapshot": {"ok": False}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    fsyncs = _watch_dir_fsyncs(monkeypatch)
    fired = []
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: fired.append((intent.exists(), list(fsyncs))))

    assert await g._settle_and_verify_before_host_rung(32, lambda m: None, "idle", offbus_chips=set()) is False
    await srv._auto_power_cycle_host(lambda m: None, "idle escalation: 8/32 chip(s) off the bus")

    assert during == [True], "the sweep ran under its intent"
    assert len(fired) == 1
    present, synced = fired[0]
    assert not present, "the intent is gone before the power-cycle command runs"
    assert False in synced, "and its removal was made durable (directory fsynced after the unlink)"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["power-cycle", "reboot"])
async def test_a_broker_host_action_closes_an_open_intent_durably_before_it_fires(sysfs, monkeypatch, tmp_path, action):
    """Whoever left the intent open, the broker's own reboot or power cycle removes it, durably, after
    its ledger row and before the fire: the host going down next is the broker's, not a hang."""
    _boot(sysfs, "boot-a")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    pcie_guard.begin_offbus_reset("reset sweep", 8)
    intent = pcie_guard.health_dir() / pcie_guard.INTENT_FILE
    fsyncs = _watch_dir_fsyncs(monkeypatch)
    fired = []
    m = srv.recovery_mechanism
    await m._fire_recovery_escalation(
        action,
        row_owner=f"[broker]{action}-request",
        label=f"auto {action}",
        detail="test",
        fire=lambda: fired.append((intent.exists(), list(fsyncs), m.read_auto_recovery_ledger()[-1]["action"])),
        log=lambda msg: None,
        reason="test",
    )
    assert fired == [(False, [False], action)], "intent removed and synced after the ledger row, before the fire"
    assert pcie_guard._INTENT_OPEN is False


def _intent_then_cycle(sysfs, *, row_action, row_boot="boot-a", row_dt=60.0):
    """An intent left on boot-a, a ledger row ``row_dt`` s after it, and the host now on boot-b."""
    _boot(sysfs, "boot-a")
    pcie_guard.begin_offbus_reset("reset sweep", 8)
    at = json.loads((pcie_guard.health_dir() / pcie_guard.INTENT_FILE).read_text())["at_epoch"]
    if row_action:
        row = {"action": row_action, "reason": "idle escalation", "boot_id": row_boot, "at_epoch": at + row_dt}
        (pcie_guard.health_dir() / "auto_recovery.jsonl").write_text(json.dumps(row) + "\n")
    _boot(sysfs, "boot-b")


@pytest.mark.parametrize("action", ["power-cycle", "reboot"])
def test_an_intent_older_than_the_brokers_own_cycle_does_not_latch(sysfs, monkeypatch, action):
    """The boot after the broker's own power cycle or reboot: an intent left from before it (an unlink
    the power loss undid) is not a host hang. No latch, no event, and the mesh reset is not gated, so
    the off-bus hold's ladder runs at once (spec 04 I25)."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
    _intent_then_cycle(sysfs, row_action=action)

    assert srv.pcie_guard_at_start(lambda m: None) is None
    assert "offbus_reset_hold_latched" not in [n for n, _ in events]
    assert ("offbus_reset_intent_closed", True) in [(n, k.get("at_boot")) for n, k in events]
    assert not (pcie_guard.health_dir() / pcie_guard.INTENT_FILE).exists()
    assert pcie_guard.offbus_reset_latched() is None
    assert pcie_guard.gate_mode() == pcie_guard.GATE_OFF
    assert pcie_guard.host_reset_gate(8) == (True, ""), "glx_reset over the off-bus chips is not gated"


@pytest.mark.parametrize(
    "row_action, row_boot, row_dt",
    [
        (None, "boot-a", 60.0),  # no host action recorded: the host went down on its own
        ("power-cycle", "boot-a", -60.0),  # the only cycle predates the reset
        ("power-cycle", "boot-z", 60.0),  # a cycle on another boot says nothing about this one
        ("reset", "boot-a", 60.0),  # not a host action
    ],
)
def test_an_intent_with_no_later_host_action_still_latches(sysfs, monkeypatch, row_action, row_boot, row_dt):
    """A real host crash inside an off-bus reset keeps its latch: only a reboot or power cycle the
    broker recorded at or after the intent, on the intent's boot, excuses it."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    _intent_then_cycle(sysfs, row_action=row_action, row_boot=row_boot, row_dt=row_dt)

    latch = srv.pcie_guard_at_start(lambda m: None)
    assert latch is not None and latch["kind"] == "reset sweep"
    assert "offbus_reset_hold_latched" in [n for n, _ in events]
    assert pcie_guard.host_reset_gate(8)[0] is False


def test_a_latch_holds_only_on_the_boot_it_was_written_in(sysfs, monkeypatch):
    """The host-hang record is scoped to its boot: once the host boots again it never holds a rung,
    and the broker drops it at start with an event; a latch that names no boot is no exception."""
    events = []
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
    _boot(sysfs, "boot-a")
    pcie_guard.begin_offbus_reset("mesh reset", 1)
    monkeypatch.setattr(pcie_guard, "_INTENT_OPEN", False)
    _boot(sysfs, "boot-b")
    assert pcie_guard.check_offbus_reset_latch(lambda m: None)["latched_on_boot"] == "boot-b"
    assert pcie_guard.host_reset_gate(1)[0] is False, "held on the boot it was latched on"
    _boot(sysfs, "boot-c")  # e.g. the bounded power cycle (I27) or an operator's
    assert pcie_guard.offbus_reset_latched() is None
    assert pcie_guard.gate_mode() == pcie_guard.GATE_OFF and pcie_guard.host_reset_gate(1) == (True, "")
    logs = []
    assert pcie_guard.check_offbus_reset_latch(logs.append) is None
    assert not (pcie_guard.health_dir() / pcie_guard.LATCH_FILE).exists()
    assert events[-1][0] == "offbus_reset_hold_expired" and events[-1][1]["latch"]["latched_on_boot"] == "boot-b"
    assert any("expired" in m for m in logs)
    (pcie_guard.health_dir() / pcie_guard.LATCH_FILE).write_text('{"boot_id": "boot-a"}')
    assert pcie_guard.offbus_reset_latched() is None, "a latch that names no boot is not this boot's"
    _boot(sysfs, "")
    assert pcie_guard.offbus_reset_latched() is not None, "with the boot id unreadable it cannot be shown stale"


def _latched_past_the_ceiling_with_a_chip_off(sysfs, monkeypatch, gate: str) -> list:
    """Boot-a began an off-bus reset, boot-b latched it, one chip is still off the bus and the latch
    is older than its ceiling. Returns the pages sent."""
    pages = []
    monkeypatch.setattr(pcie_guard.alert, "send_alert", lambda event, log=None: pages.append(event))
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", gate)
    _boot(sysfs, "boot-a")
    pcie_guard.begin_offbus_reset("mesh reset", 1)
    monkeypatch.setattr(pcie_guard, "_INTENT_OPEN", False)
    _boot(sysfs, "boot-b")
    assert pcie_guard.check_offbus_reset_latch(lambda m: None)["latched_on_boot"] == "boot-b"
    _take_off_bus(sysfs, 0)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + pcie_guard.LATCH_MAX_SEC + 1)
    return pages


@pytest.mark.parametrize("gate", ["off", "hold"])
def test_with_no_power_cycle_a_latch_with_a_chip_off_lifts_at_its_ceiling(sysfs, monkeypatch, gate):
    """With TT_DEVICE_MCP_POWER_CYCLE_CMD unset no I27 cycle would end an off-bus hold, so the latch
    lifts at its ceiling even with a chip off, pages once, and the gate follows its configured mode."""
    pages = _latched_past_the_ceiling_with_a_chip_off(sysfs, monkeypatch, gate)
    logs = []
    reason = pcie_guard.expire_offbus_reset_latch(len(CHIPS), True, logs.append)
    assert reason and "TT_DEVICE_MCP_POWER_CYCLE_CMD" in reason and "ceiling" in reason
    assert pcie_guard.offbus_reset_latched() is None
    assert any("lifted" in m for m in logs)
    assert len(pages) == 1 and pages[0]["kind"] == "offbus_reset_hold_expired_off_bus"
    assert "boot boot-a" in pages[0]["message"] and "boot boot-b" in pages[0]["message"]
    assert pcie_guard.expire_offbus_reset_latch(len(CHIPS), True, logs.append) is None
    assert len(pages) == 1, "paged once"
    if gate == "off":
        assert pcie_guard.host_reset_gate(1) == (True, "")
    else:
        assert pcie_guard.host_reset_gate(1) == (False, "1 chip(s) off the bus and TT_DEVICE_MCP_HOST_RESET_GATE=hold")


def test_with_no_power_cycle_the_gate_itself_lifts_a_latch_past_its_ceiling(sysfs, monkeypatch):
    """No health pass needed: the gate, asked with a chip off, ends the hold at its ceiling."""
    pages = _latched_past_the_ceiling_with_a_chip_off(sysfs, monkeypatch, "off")
    assert pcie_guard.host_reset_gate(1) == (True, "")
    assert pcie_guard.offbus_reset_latched() is None and len(pages) == 1


def test_with_a_power_cycle_a_latch_with_a_chip_off_still_holds_past_its_ceiling(sysfs, monkeypatch):
    pages = _latched_past_the_ceiling_with_a_chip_off(sysfs, monkeypatch, "off")
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/usr/local/bin/cycle-host")
    assert pcie_guard.expire_offbus_reset_latch(len(CHIPS), True, lambda m: None) is None
    allowed, why = pcie_guard.host_reset_gate(1)
    assert not allowed and pcie_guard.offbus_reset_latched() is not None and not pages
    assert "ceiling" not in why, "the power cycle, not the ceiling, ends this hold"


def test_the_hold_message_names_both_boots(sysfs, monkeypatch, capsys):
    """The reset ran in boot-a; the hold is on boot-b. Both the gate and the CLI say so."""
    _latched_past_the_ceiling_with_a_chip_off(sysfs, monkeypatch, "off")
    monkeypatch.setattr(time, "time", lambda: 0.0)  # inside the ceiling: it holds
    allowed, why = pcie_guard.host_reset_gate(1)
    assert not allowed
    assert "the mesh reset on boot boot-a did not finish before the host went down; held on boot boot-b until" in why
    assert "ceiling" in why, "with no power cycle the ceiling is one of the ends"
    pcie_guard.main(["0x1"])
    out = capsys.readouterr().out
    assert "the mesh reset on boot boot-a did not finish before the host went down; held on boot boot-b" in out


def test_a_latch_from_an_ended_boot_never_gates_even_if_the_broker_never_restarted(sysfs, monkeypatch):
    """Field record: the reset sweep ran on boot A, the hold latched on boot B, the host is now on
    boot C and the broker has not dropped the file. Its boot has ended, so no rung is gated by it."""
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
    rec = {"kind": "reset sweep", "off_bus": 8, "boot_id": "boot-a", "at_epoch": time.time() - 90000}
    rec.update(latched_on_boot="boot-b", latched_at_epoch=time.time() - 89500)
    pcie_guard.health_dir().mkdir(parents=True, exist_ok=True)
    (pcie_guard.health_dir() / pcie_guard.LATCH_FILE).write_text(json.dumps(rec))
    _boot(sysfs, "boot-c")
    _take_off_bus(sysfs, 0)
    assert pcie_guard.offbus_reset_latched() is None
    assert pcie_guard.gate_mode() == pcie_guard.GATE_OFF and pcie_guard.host_reset_gate(1) == (True, "")


def test_the_latch_can_be_switched_off(sysfs, monkeypatch):
    _boot(sysfs, "boot-a")
    pcie_guard.begin_offbus_reset("tray re-power", 1)
    _boot(sysfs, "boot-b")
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HANG_LATCH", "0")
    assert pcie_guard.check_offbus_reset_latch(lambda m: None) is None
    assert pcie_guard.offbus_reset_latched() is None


def _latched(monkeypatch, root, events) -> dict:
    """A host-hang hold latched on boot-b: an off-bus reset on boot-a never finished."""
    monkeypatch.setattr(pcie_guard, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
    _boot(root, "boot-a")
    pcie_guard.begin_offbus_reset("mesh reset", 1)
    monkeypatch.setattr(pcie_guard, "_INTENT_OPEN", False)
    _boot(root, "boot-b")
    return pcie_guard.check_offbus_reset_latch(lambda m: None)


def _age_latch(seconds: float) -> None:
    path = pcie_guard.health_dir() / pcie_guard.LATCH_FILE
    rec = json.loads(path.read_text())
    rec["latched_at_epoch"] = time.time() - seconds
    path.write_text(json.dumps(rec))


def test_the_latch_lifts_once_every_chip_is_back_and_host_pci_is_ok(sysfs, monkeypatch):
    """Goal: a bounded hold (I25). Every chip on the bus with host-pci OK lifts it on its own boot,
    with an event naming why; host-pci not OK, or not judged, keeps it until the ceiling."""
    events, logs = [], []
    _latched(monkeypatch, sysfs, events)
    n = len(CHIPS)
    assert pcie_guard.expire_offbus_reset_latch(n, False, logs.append) is None
    assert pcie_guard.expire_offbus_reset_latch(n, None, logs.append) is None
    assert pcie_guard.expire_offbus_reset_latch(n + 1, True, logs.append) is None, "a chip short of expected"
    assert pcie_guard.expire_offbus_reset_latch(0, True, logs.append) is None, "no expected count proves nothing"
    assert pcie_guard.offbus_reset_latched() is not None
    reason = pcie_guard.expire_offbus_reset_latch(n, True, logs.append)
    assert reason and "host-pci is OK" in reason
    assert pcie_guard.offbus_reset_latched() is None
    assert not (pcie_guard.health_dir() / pcie_guard.LATCH_FILE).exists()
    assert pcie_guard.gate_mode() == pcie_guard.GATE_OFF
    assert events[-1][0] == "offbus_reset_hold_expired" and events[-1][1]["reason"] == reason
    assert any("lifted" in m for m in logs)
    assert pcie_guard.expire_offbus_reset_latch(n, True, logs.append) is None, "nothing left to lift"


@pytest.mark.parametrize("why", ["host-pci OK", "past the ceiling"])
def test_the_latch_never_lifts_while_a_chip_is_off_the_bus(sysfs, monkeypatch, why):
    """An off-bus hold is the I27 power cycle's to end, never the latch's own expiry: neither a clean
    host-pci verdict nor the ceiling lifts it with a chip off, or with sysfs unreadable."""
    events = []
    _latched(monkeypatch, sysfs, events)
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/usr/local/bin/cycle-host")
    if why == "past the ceiling":
        _age_latch(pcie_guard.LATCH_MAX_SEC + 1)
    _take_off_bus(sysfs, 0)
    assert pcie_guard.expire_offbus_reset_latch(len(CHIPS), True, lambda m: None) is None
    allowed, why_held = pcie_guard.host_reset_gate(1)
    assert not allowed and "--clear-hang-latch" in why_held
    monkeypatch.setattr(pcie_guard, "tt_endpoints", lambda: None)
    assert pcie_guard.expire_offbus_reset_latch(len(CHIPS) - 1, True, lambda m: None) is None
    assert pcie_guard.offbus_reset_latched() is not None
    assert not [n for n, _k in events if n == "offbus_reset_hold_expired"]


def test_the_latch_lifts_at_its_ceiling_with_every_chip_on_the_bus(sysfs, monkeypatch):
    """Without a clean host-pci look (health checks off, or host-pci not OK) the hold still ends: the
    gate, asked with no chip off the bus, lifts a latch older than its ceiling."""
    events = []
    _latched(monkeypatch, sysfs, events)
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/usr/local/bin/cycle-host")
    _age_latch(pcie_guard.LATCH_MAX_SEC - 60)
    assert pcie_guard.host_reset_gate(0) == (True, "")
    assert pcie_guard.offbus_reset_latched() is not None, "inside the ceiling it holds"
    _age_latch(pcie_guard.LATCH_MAX_SEC + 1)
    assert pcie_guard.host_reset_gate(1)[0] is False, "a gate asked with a chip off never lifts it"
    assert pcie_guard.offbus_reset_latched() is not None
    assert pcie_guard.host_reset_gate(0) == (True, "")
    assert pcie_guard.offbus_reset_latched() is None and pcie_guard.gate_mode() == pcie_guard.GATE_OFF
    assert "ceiling" in events[-1][1]["reason"]
    # A latch that names no time ages from its file; a ceiling set lower is honoured.
    _latched(monkeypatch, sysfs, events)
    path = pcie_guard.health_dir() / pcie_guard.LATCH_FILE
    path.write_text('{"latched_on_boot": "boot-b"}')
    old = time.time() - 120
    os.utime(path, (old, old))
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HANG_LATCH_MAX_SEC", "60")
    assert "ceiling" in pcie_guard.expire_offbus_reset_latch(len(CHIPS), False, lambda m: None)


@pytest.mark.parametrize("raw", ["", "0", "-5", "nan", "inf", "six hours"])
def test_the_latch_ceiling_defaults_and_rejects_nonsense(monkeypatch, raw):
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_HANG_LATCH_MAX_SEC", raw)
    assert pcie_guard.latch_max_sec() == pcie_guard.LATCH_MAX_SEC == 21600


def test_a_latch_that_cannot_be_removed_keeps_holding(sysfs, monkeypatch):
    events = []
    _latched(monkeypatch, sysfs, events)
    _deny(monkeypatch, "unlink", pcie_guard.health_dir() / pcie_guard.LATCH_FILE)
    assert pcie_guard.expire_offbus_reset_latch(len(CHIPS), True, lambda m: None) is None
    assert pcie_guard.offbus_reset_latched() is not None
    assert events[-1][0] == "offbus_reset_hold_unremovable" and events[-1][1]["host_at_risk"] is True


@pytest.mark.asyncio
async def test_the_between_jobs_check_lifts_the_latch_once_the_mesh_is_back(sysfs, monkeypatch, health_deps):
    """The between-jobs and idle passes are where a clean bus is seen: the latch lifts there with no
    operator, and a pass with a chip off leaves it."""
    from tt_device_mcp.health.monitor import HealthMonitor
    from tt_device_mcp.health.monitors import hostpci

    _latched(monkeypatch, sysfs, [])
    monkeypatch.setattr(hostpci, "host_pci_verdict", lambda: (True, "ok", {}))
    m = HealthMonitor(health_deps)

    async def healthy(*a, **k):
        return True, "ok"

    monkeypatch.setattr(m, "_verify_device", healthy)
    await m.update("post-job", run_fabric=False, expected=len(CHIPS) + 1)
    assert pcie_guard.offbus_reset_latched() is not None, "a chip is still missing"
    logs = []
    await m.update("post-job", run_fabric=False, expected=len(CHIPS), log=logs.append)
    assert pcie_guard.offbus_reset_latched() is None
    assert any("host-hang hold" in line and "lifted" in line for line in logs)


@pytest.mark.asyncio
async def test_a_tray_re_power_carries_an_intent_and_ends_it(monkeypatch, clear_job_state, galaxy_trays):
    """The walk writes the intent before it fires and removes it once its verify is over."""
    monkeypatch.setenv("TT_DEVICE_MCP_UBB_SINGLE_TRAY_WALK", "1")  # the single-tray walk under test is opt-in
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
# Part of a tray: a whole one skips the sweep (spec 04 I37 class A).
@pytest.mark.parametrize("off, trays", [(set(range(24, 30)), [3]), (set(), [])])
async def test_the_last_chance_sweep_re_powers_only_the_affected_trays(
    monkeypatch, clear_job_state, galaxy_trays, off, trays
):
    """Spec 04 I25: a caller that names no off-bus chips gets them read from the heartbeats; only trays
    holding one are re-powered. A present mesh gets SBR and the mesh reset but no tray re-power."""
    g = srv.galaxy_recovery
    swept = []

    async def sweep(bitmap, ids, trays_, expected, log, do_sbr, mesh_reset=True):
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
    shutil.rmtree(root / f"sys/class/tenstorrent/tenstorrent!{chip}")


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
    cycle above it is not held (I22) — the old path's recovery."""
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
    """The documented limit (spec 04 I23): a port never seen with its chip present is not masked."""
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
    the envelope refuses before it touches anything and says why (spec 04 I23)."""
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
