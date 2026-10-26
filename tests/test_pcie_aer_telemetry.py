# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I91: per-chip PCIe AER and link telemetry at job start, job end, and just before and
after the post-job check's probe pass. Read-only sysfs; never a lost, all-ones or resetting chip's
own link; the gate decides exactly as without it.

Every test runs against a fake sysfs tree: four Blackhole trays, one chip each."""

import sys
import types

import pytest

from tests.conftest import fsm_dirty
from tests.test_eth_timeout_reset import ADVANCING, _gate, mesh  # noqa: F401 - the gate fixture
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import chip_state
from tt_device_mcp.health.monitors import pcie_aer
from tt_device_mcp.health.recovery import pcie_guard

# (chip id, endpoint bus, root port) in the kernel's /dev order; the last is a tray's last chip
CHIPS = [(0, 0x01, "0000:00:01.1"), (1, 0x41, "0000:40:01.1"), (2, 0xC1, "0000:c0:01.1"), (3, 0x81, "0000:80:02.1")]
AER_FILE = "RxErr {n}\nBadTLP 0\nTOTAL_ERR_COR {n}\n"


def _aer(path, n):
    for attr in ("aer_dev_correctable", "aer_dev_nonfatal", "aer_dev_fatal"):
        (path / attr).write_text(AER_FILE.format(n=n if attr == "aer_dev_correctable" else 0))


class Bus:
    def __init__(self, root):
        self.root = root

    def ep(self, chip):
        _c, bus, rp = CHIPS[chip]
        return self.rp(chip) / f"0000:{bus:02x}:00.0"

    def rp(self, chip):
        rp = CHIPS[chip][2]
        return self.root / f"sys/devices/pci0000:{rp.split(':')[1]}/{rp}"

    def errors(self, chip, n, port=False):
        _aer(self.rp(chip) if port else self.ep(chip), n)

    def link(self, chip, speed, width):
        (self.ep(chip) / "current_link_speed").write_text(speed + "\n")
        (self.ep(chip) / "current_link_width").write_text(width + "\n")

    def drop(self, chip):
        """The chip left the bus: the kernel removed its function and its class node."""
        _c, bus, _rp = CHIPS[chip]
        (self.root / f"sys/bus/pci/devices/0000:{bus:02x}:00.0").unlink()
        (self.root / f"sys/class/tenstorrent/tenstorrent!{chip}/device").unlink()


@pytest.fixture
def bus(tmp_path, monkeypatch):
    devs = tmp_path / "sys/bus/pci/devices"
    devs.mkdir(parents=True)
    (tmp_path / "sys/class/tenstorrent").mkdir(parents=True)
    b = Bus(tmp_path)
    for chip, bus_no, rp in CHIPS:
        rp_dir, ep_dir = b.rp(chip), b.ep(chip)
        ep_dir.mkdir(parents=True)
        (rp_dir / "vendor").write_text("0x1022\n")
        (rp_dir / "current_link_speed").write_text("32.0 GT/s PCIe\n")
        (rp_dir / "current_link_width").write_text("1\n")
        _aer(rp_dir, 0)
        (ep_dir / "vendor").write_text("0x1e52\n")
        (ep_dir / "device").write_text("0xb140\n")
        _aer(ep_dir, 5)
        b.link(chip, "32.0 GT/s PCIe", "1")
        (devs / rp).symlink_to(rp_dir)
        (devs / ep_dir.name).symlink_to(ep_dir)
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
    monkeypatch.setattr(pcie_guard, "health_event", lambda *a, **k: None)
    pcie_aer.new_cycle()
    reads = []
    real = pcie_aer._read
    monkeypatch.setattr(pcie_aer, "_read", lambda p: (reads.append(str(p)), real(p))[1])
    b.reads = reads
    yield b
    pcie_aer.new_cycle()


def _link_reads(b, chip):
    ep = b.ep(chip).name
    return [r for r in b.reads if f"/{ep}/current_link" in r]


def test_snapshot_reads_both_ends_and_names_bdf_root_port_and_tray(bus):
    snap = pcie_aer.snapshot()
    assert set(snap) == {"0", "1", "2", "3"}
    rec = snap["3"]
    assert rec["bdf"] == "0000:81:00.0" and rec["root_port"] == "0000:80:02.1" and rec["tray"] == 4
    assert rec["aer"] == {"cor": 5, "nonfatal": 0, "fatal": 0}
    assert rec["rp_aer"] == {"cor": 0, "nonfatal": 0, "fatal": 0}
    assert rec["link"] == {"speed": "32.0 GT/s PCIe", "width": "1"}
    assert rec["rp_link"] == {"speed": "32.0 GT/s PCIe", "width": "1"}


def test_delta_lists_only_chips_with_errors_or_a_link_change(bus):
    before = pcie_aer.snapshot()
    bus.errors(3, 436, port=True)
    bus.errors(1, 9)
    bus.link(2, "2.5 GT/s PCIe", "1")
    rows = pcie_aer.delta(before, pcie_aer.snapshot())
    assert [r["chip"] for r in rows] == [1, 2, 3]
    assert rows[0]["aer"] == {"cor": 4} and "rp_aer" not in rows[0]
    assert rows[1]["link"]["after"]["speed"] == "2.5 GT/s PCIe" and "aer" not in rows[1]
    assert rows[2] == {
        "chip": 3,
        "bdf": "0000:81:00.0",
        "root_port": "0000:80:02.1",
        "tray": 4,
        "rp_aer": {"cor": 436},
    }
    assert pcie_aer.totals(rows) == {"chips": 3, "aer": {"cor": 4}, "rp_aer": {"cor": 436}}
    assert pcie_aer.delta(before, before) == []


def test_delta_names_a_chip_that_left_the_bus_and_reads_nothing_of_it(bus):
    before = pcie_aer.snapshot()
    bus.drop(2)
    bus.reads.clear()
    after = pcie_aer.snapshot()
    assert "2" not in after and not _link_reads(bus, 2)
    rows = pcie_aer.delta(before, after)
    assert rows == [{"chip": 2, "bdf": "0000:c1:00.0", "root_port": "0000:c0:01.1", "tray": 3, "gone": True}]
    assert pcie_aer.delta(after, before)[0]["joined"] is True


def test_a_counter_that_went_down_counts_its_new_value_and_unreadable_is_left_out():
    assert pcie_aer._aer_delta({"cor": 400}, {"cor": 7}) == {"cor": 7}
    assert pcie_aer._aer_delta({"cor": None}, {"cor": 7}) == {}
    assert pcie_aer._aer_delta({"cor": 3}, {"cor": None}) == {}
    assert pcie_aer._aer_delta({}, {"cor": 2}) == {"cor": 2}


def test_a_lost_chip_s_own_link_is_never_read(bus, monkeypatch):
    monkeypatch.setattr(chip_state, "_lost", {"1": {"since": "", "reason": "t", "node": None, "next": 1e18}})
    snap = pcie_aer.snapshot()
    assert snap["1"]["link_skipped"] == "lost" and "link" not in snap["1"]
    assert not _link_reads(bus, 1)
    assert snap["1"]["aer"]["cor"] == 5, "the kernel's own counters reach no chip"
    assert "link" in snap["0"] and _link_reads(bus, 0)


def test_a_chip_the_sampler_read_all_ones_is_not_read(bus):
    snap = pcie_aer.snapshot(suspect={"2"})
    assert snap["2"]["link_skipped"] == "all_ones" and not _link_reads(bus, 2)


def test_no_chip_link_is_read_while_a_reset_step_owns_the_chips(bus, monkeypatch):
    monkeypatch.setattr(chip_state, "op_running", lambda: True)
    snap = pcie_aer.snapshot()
    assert all(rec["link_skipped"] == "reset" for rec in snap.values())
    assert not any(_link_reads(bus, c) for c in range(4))


@pytest.mark.parametrize("speed,width", [("Unknown", "1"), ("32.0 GT/s PCIe", "63")])
def test_an_all_ones_link_read_is_not_repeated_in_the_same_job_cycle(bus, speed, width):
    bus.link(0, speed, width)
    first = pcie_aer.snapshot()
    assert first["0"]["link"]["down"] is True
    if speed == "Unknown":
        assert not [r for r in _link_reads(bus, 0) if r.endswith("current_link_width")]
    bus.reads.clear()
    second = pcie_aer.snapshot()
    assert second["0"]["link_skipped"] == "all_ones" and not _link_reads(bus, 0)
    pcie_aer.new_cycle()
    bus.link(0, "32.0 GT/s PCIe", "1")
    assert pcie_aer.snapshot()["0"]["link"] == {"speed": "32.0 GT/s PCIe", "width": "1"}


def test_no_write_and_no_config_space_read(bus, monkeypatch):
    import builtins
    from pathlib import Path

    opened = []
    for name in ("read_text", "read_bytes", "write_text", "write_bytes", "open"):
        real = getattr(Path, name)

        def spy(self, *a, _real=real, _name=name, **k):
            mode = a[0] if _name == "open" and a else k.get("mode", "r")
            opened.append((str(self), "w" if _name.startswith("write") else mode))
            return _real(self, *a, **k)

        monkeypatch.setattr(Path, name, spy)
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open", lambda f, mode="r", *a, **k: (opened.append((str(f), mode)), real_open(f, mode, *a, **k))[1]
    )
    pcie_aer.snapshot()
    sysfs = [(p, m) for p, m in opened if p.startswith(str(bus.root / "sys"))]
    assert sysfs, "the snapshot read sysfs"
    assert all(set(m) <= {"r", "b", "t"} for _p, m in sysfs), "read-only"
    assert not [p for p, _m in sysfs if p.endswith("/config")]


def test_a_broken_sysfs_gives_an_empty_snapshot(monkeypatch):
    monkeypatch.setattr(pcie_guard, "tt_endpoints", lambda: (_ for _ in ()).throw(OSError("gone")))
    assert pcie_aer.snapshot() == {}


# --- the job cycle and the post-job check -------------------------------------------------------


def _capture(monkeypatch, mesh):  # noqa: F811
    events = []

    def he(name, *a, **k):
        mesh["events"].append(name)
        events.append((name, k))

    monkeypatch.setattr(srv, "health_event", he)
    return events


@pytest.mark.asyncio
async def test_job_start_end_and_the_check_each_write_one_event_into_the_gate_record(
    mesh, bus, monkeypatch  # noqa: F811
):
    events = _capture(monkeypatch, mesh)
    mesh["eth"] = ADVANCING
    await srv._pcie_job_start("042")
    bus.errors(1, 20)  # during the job
    await srv._pcie_job_end("042")
    fsm_dirty(srv, "job 042 failed", job={"id": "042"})

    async def fabric_pass():
        bus.errors(3, 436, port=True)  # during the pass, on the chip's own root port
        mesh["fabric"].append(0)
        srv.health_monitor.last_fabric_ok = True
        return True, "all links healthy"

    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", fabric_pass)
    await _gate()
    deltas = {k["phase"]: k for n, k in events if n == "pcie_aer_delta"}
    assert set(deltas) == {"job", "job_end_to_check", "check"}
    assert [r["chip"] for r in deltas["job"]["chips"]] == [1]
    assert deltas["job_end_to_check"]["chips"] == []
    check = deltas["check"]
    assert check["fabric_ran"] is True and check["fabric_ok"] is True and check["job"] == "042"
    assert check["chips"] == [
        {"chip": 3, "bdf": "0000:81:00.0", "root_port": "0000:80:02.1", "tray": 4, "rp_aer": {"cor": 436}}
    ]
    gate = [k for n, k in events if n == "gate"][-1]
    assert gate["pcie"]["check"] == check["chips"] and gate["pcie"]["job"] == deltas["job"]["chips"]
    assert gate["dirty_job"]["id"] == "042" and gate["dirty_job"]["pcie"] == gate["pcie"]
    assert srv._pcie_cycle == {}


@pytest.mark.asyncio
async def test_a_clean_exit_check_writes_its_event_with_no_dirty_job(mesh, bus, monkeypatch):  # noqa: F811
    events = _capture(monkeypatch, mesh)
    mesh["eth"] = ADVANCING
    await srv._pcie_job_start("7")
    await srv._pcie_job_end("7")
    await _gate()
    assert srv.fsm.state is ServerState.HEALTHY
    check = [k for n, k in events if n == "pcie_aer_delta" and k["phase"] == "check"]
    assert len(check) == 1 and check[0]["chips"] == [] and check[0]["fabric_ran"] is False
    gate = [k for n, k in events if n == "gate"][-1]
    assert gate["pcie"]["check"] == [] and not gate["dirty_job"]


async def _outcome(mesh, monkeypatch, telemetry, during=lambda: None):  # noqa: F811
    if telemetry == "broken":
        monkeypatch.setattr(pcie_aer, "snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr(pcie_aer, "delta", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    elif telemetry == "off":
        monkeypatch.setattr(pcie_aer, "snapshot", lambda *a, **k: {})
    await srv._pcie_job_start("9")
    during()
    await srv._pcie_job_end("9")
    fsm_dirty(srv, "job 9 failed: exit 1")
    await _gate()
    names = [n for n in mesh["events"] if n != "pcie_aer_delta"]
    return srv.fsm.state.name, list(mesh["fabric"]), len(mesh["resets"]), names


# What the post-job gate does after a failed job on this mesh, telemetry aside: a green fabric pass
# releases; a red one holds (one chip short of a mass reset) with no reset.
GATE_OUTCOME = {
    False: ("HEALTHY", [0], 0, ["device_op_begin", "gate", "device_dirty_cleared", "device_op_end"]),
    True: (
        "RECOVERING",
        [0],
        0,
        [
            "device_op_begin",
            "gate",
            "device_held",
            "galaxy_reset_suppressed_below_mass_threshold",
            "device_dirty_dropped_held",
            "device_op_end",
        ],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("fabric_bad", [False, True])
@pytest.mark.parametrize("telemetry", ["off", "live", "broken"])
async def test_the_gate_decides_the_same_with_or_without_the_telemetry(
    mesh, bus, monkeypatch, fabric_bad, telemetry  # noqa: F811
):
    """After a failed job, the post-job gate reaches the same state, runs the same fabric passes and
    resets and writes the same events (but the telemetry's own) whether the telemetry reads nothing,
    reads errors on every chip, or raises."""
    mesh["eth"] = ADVANCING
    mesh["fabric_bad"] = fabric_bad
    mesh["reset_heals"] = False

    def errors_everywhere():
        for chip in range(len(CHIPS)):
            bus.errors(chip, 99, port=True)
            bus.errors(chip, 99)

    assert await _outcome(mesh, monkeypatch, telemetry, errors_everywhere) == GATE_OUTCOME[fabric_bad]
