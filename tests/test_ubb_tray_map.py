# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A Galaxy's UBB tray is identified by its chips' PCI bus, never by their index (spec 04 I16).

The two are not the same ordering, twice over. tt-smi maps a chip to a tray by masking its bus id
to the tray group and looking the group up in a per-architecture table, and on a Blackhole Galaxy
that table puts bus 0xc0 on tray 3 and 0x80 on tray 4. And the kernel's chip index is not PCI
order either: on a Blackhole Galaxy /dev 16-23 sit on 0xc1-0xc8 and 24-31 on 0x81-0x88, while
tt-smi's snapshot lists chips in PCI order (0x0X, 0x4X, 0x8X, 0xCX). Read the snapshot's list by
position and chips 16-31 land on the other tray: the walk re-powers tray 3 for an off-bus chip 24
(tracker issue #27). So the map is keyed by the kernel's own chip index, read from sysfs.
"""

import json
import subprocess

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import galaxy

# Bus ids in the order tt-smi's snapshot lists them: PCI order, four groups of eight, one per tray,
# the low nibble counting the chips within the group.
BH_BUS_IDS = [f"0000:{group + n:02x}:00.0" for group in (0x00, 0x40, 0x80, 0xC0) for n in range(1, 9)]

# The kernel's chip index -> PCI address on a Blackhole Galaxy: /dev 16-23 are 0xC1-0xC8 and
# 24-31 are 0x81-0x88, so the kernel's order is NOT tt-smi's for the last two trays.
BH_CHIP_BUSES = {
    chip: f"0000:{group + n:02x}:00.0"
    for base, group in ((0, 0x00), (8, 0x40), (16, 0xC0), (24, 0x80))
    for n, chip in enumerate(range(base, base + 8), start=1)
}

# The same Galaxy after tt-kmd handed chips 16 and 24 each other's index (it falls back to a free
# index when a chip's own is still taken, e.g. across a drop/rescan): chip 16 is now on 0x81 (tray 4)
# and chip 24 on 0xC1 (tray 3). Every bus is where it was; only the indexes moved.
RENUMBERED_CHIP_BUSES = {**BH_CHIP_BUSES, 16: BH_CHIP_BUSES[24], 24: BH_CHIP_BUSES[16]}


def test_a_blackhole_tray_comes_from_the_bus_group_not_the_chip_index():
    """tt-smi's Blackhole table numbers bus group 0xc0 tray 3 and 0x80 tray 4. The kernel puts
    chips 16-23 on 0xc0 and 24-31 on 0x80, so each chip's own bus places it."""
    trays = galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-bh")

    assert trays[1] == list(range(0, 8))
    assert trays[2] == list(range(8, 16))
    assert trays[3] == list(range(16, 24))
    assert trays[4] == list(range(24, 32))


@pytest.mark.parametrize(
    "chip, bus, tray, mask",
    [
        (3, 0x04, 1, 0x01),
        (12, 0x45, 2, 0x02),
        (22, 0xC7, 3, 0x04),  # issue #27: a 0xCX chip is tray 3, mask 0x04
        (24, 0x81, 4, 0x08),  # issue #27: chip 24 on 0000:81 is tray 4, mask 0x08
        (25, 0x82, 4, 0x08),  # issue #27: chip 25 on 0000:82:00.0
    ],
)
def test_each_blackhole_bus_range_maps_to_its_tray_and_bmc_bit(chip, bus, tray, mask):
    """All four bus ranges, pinned: 0x0X tray 1, 0x4X tray 2, 0xCX tray 3, 0x8X tray 4. The BMC
    bit is tray - 1. Fails on base for the 0x8X/0xCX rows: read by position, 0x8X was tray 3."""
    assert BH_CHIP_BUSES[chip] == f"0000:{bus:02x}:00.0"
    trays = galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-bh")

    assert chip in trays[tray]
    assert galaxy._affected_trays({str(chip)}, 32, trays) == [tray]
    assert galaxy._ubb_tray_walk_plan({str(chip)}, 32, trays)[0] == tray
    assert 1 << (tray - 1) == mask


def test_the_issue_27_walk_leads_with_the_tray_the_chip_is_on():
    """The reported walk for an off-bus chip 25 (0000:82:00.0) was [3, 1, 2, 4]: tray 3 first, a
    healthy tray. It must lead with tray 4."""
    trays = galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-bh")

    assert galaxy._ubb_tray_walk_plan({"25"}, 32, trays) == [4, 1, 2, 3]
    assert galaxy._ubb_tray_walk_plan({"24", "30"}, 32, trays) == [4, 1, 2, 3]


def test_a_positional_bus_list_is_refused_not_read_by_position():
    """A list carries no chip index, and its position is not one: tt-smi's PCI order swaps trays 3
    and 4 against the kernel's ids. The map refuses it rather than guess."""
    assert galaxy._tray_map(BH_BUS_IDS, "tt-galaxy-bh") is None
    assert galaxy._tray_map([], "tt-galaxy-bh") is None
    assert galaxy._tray_map({}, "tt-galaxy-bh") is None


def test_a_missing_chip_does_not_shift_any_other_chips_tray():
    """Keyed by chip index, a hole is just a hole: with chips 3 and 20 absent every other chip keeps
    its tray, where a positional read would slide every later chip down by one."""
    holes = {c: b for c, b in BH_CHIP_BUSES.items() if c not in (3, 20)}
    trays = galaxy._tray_map(holes, "tt-galaxy-bh")

    assert trays[1] == [0, 1, 2, 4, 5, 6, 7]
    assert trays[2] == list(range(8, 16))
    assert trays[3] == [16, 17, 18, 19, 21, 22, 23]
    assert trays[4] == list(range(24, 32))
    # ...and a drop on a chip the map does not place still declines (I16).
    assert galaxy._affected_trays({"20"}, 32, trays) is None


def test_a_wormhole_tray_uses_the_wormhole_table_for_the_same_buses():
    """The bus groups are the same silicon layout on both architectures; the tray NUMBERING is not.
    Wormhole numbers 0xc0 tray 1, 0x80 tray 2, 0x00 tray 3 and 0x40 tray 4 — so the identical map
    must resolve differently from the Blackhole case above, which is the whole reason the board
    type selects the table instead of one being hardcoded."""
    trays = galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-wh")

    assert trays[1] == list(range(16, 24))
    assert trays[2] == list(range(24, 32))
    assert trays[3] == list(range(0, 8))
    assert trays[4] == list(range(8, 16))


def test_an_unreadable_bus_id_yields_no_map_rather_than_a_partial_one():
    """A chip whose bus id the snapshot did not carry cannot be placed on a tray, and a map missing
    one chip would silently drop it from its tray's reset. Refuse the whole map instead: the rung's
    decline path is the safe one, a half-map is not."""
    holes = dict(BH_CHIP_BUSES)
    holes[20] = ""

    assert galaxy._tray_map(holes, "tt-galaxy-bh") is None


def test_a_bus_group_outside_the_table_yields_no_map():
    """Every chip must land in one of the four known tray groups. A bus outside them means this is
    not the topology the table describes, and guessing a tray for it is exactly what I16 forbids."""
    strays = dict(BH_CHIP_BUSES)
    strays[0] = "0000:21:00.0"  # group 0x20 — no tray

    assert galaxy._tray_map(strays, "tt-galaxy-bh") is None


def test_a_non_galaxy_board_type_yields_no_map():
    """The UBB tables describe a Galaxy. Anything else has no trays to map, and the rung declines
    on that rather than borrowing a Galaxy's numbering."""
    assert galaxy._tray_map({n: f"0000:{n + 1:02x}:00.0" for n in range(4)}, "n300") is None


@pytest.mark.parametrize("board_type", ["", None])
def test_an_unknown_board_type_yields_no_map(board_type):
    """No identified board type means no table can be chosen. Unknown is not Wormhole-by-default:
    picking either table here would be the guess I16 exists to prevent."""
    assert galaxy._tray_map(BH_CHIP_BUSES, board_type) is None


def test_a_glx_board_type_with_no_matching_arch_suffix_journals_once(monkeypatch):
    """If tt-smi ever gains a new architecture in GLX_BOARD_TYPES, this build would silently drop
    it from the tables and the rung would decline on that hardware with no operator signal. The
    spec's ownership stance is that a tt-smi change must reach us as a loud "rung stopped firing",
    not as a release note (I16 scope)."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **fields: events.append((kind, fields)))
    monkeypatch.setattr(galaxy, "_UBB_TABLES_CACHE", None)
    monkeypatch.setattr(galaxy, "_UBB_TABLES_UNKNOWN_JOURNALED", set())
    monkeypatch.setattr(
        "tt_smi.constants.GLX_BOARD_TYPES",
        ["tt-galaxy-bh", "tt-galaxy-wh", "tt-galaxy-next"],
    )

    galaxy._ubb_bus_id_tables()
    galaxy._ubb_bus_id_tables()

    missing = [e for e in events if e[0] == "ubb_tray_table_missing"]
    assert missing == [("ubb_tray_table_missing", {"board": "tt-galaxy-next"})]


# --- the map is what the rung actually plans from ---------------------------------------------


@pytest.fixture
def bh_trays():
    return galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-bh")


def test_a_whole_tray_drop_sets_the_bit_for_the_tray_that_is_down(bh_trays):
    """The bitmap the BMC receives is what decides which silicon is re-powered, so it has to key on
    the real tray. Chips 16-23 (0xCX) are tray 3 (bit 2) and 24-31 (0x8X) are tray 4 (bit 3). Read
    by position from tt-smi's PCI-ordered list, these two drops pulse each other's tray."""
    assert galaxy._ubb_reset_plan({str(i) for i in range(16, 24)}, 32, bh_trays) == ([3], 0x04)
    assert galaxy._ubb_reset_plan({str(i) for i in range(24, 32)}, 32, bh_trays) == ([4], 0x08)


def test_affected_trays_names_the_tray_an_operator_would_read_from_tt_smi(bh_trays):
    """The tray ids ride out in the ubb_reset_required event and in the operator line beside the
    BMC command. A lone off-bus chip 20 (0xc5) is on tray 3, which is what
    `tt-smi -glx_list_tray_to_device` prints for it."""
    assert galaxy._affected_trays({"20"}, 32, bh_trays) == [3]
    assert galaxy._affected_trays({"0", "31"}, 32, bh_trays) == [1, 4]


def test_the_walk_leads_with_the_affected_tray_then_sweeps_the_rest(bh_trays):
    """Walk order is affected-first, then the remaining trays ascending — over real tray numbers,
    which are 1-based, not the 0-based ordinals the index arithmetic produced."""
    assert galaxy._ubb_tray_walk_plan({"20"}, 32, bh_trays) == [3, 1, 2, 4]
    assert galaxy._ubb_tray_walk_plan({"0", "31"}, 32, bh_trays) == [1, 4, 2, 3]


def test_without_a_map_every_tray_decision_declines(bh_trays):
    """A broker that restarted after the drop never saw a healthy snapshot, so it has no bus map.
    I16 makes that a decline, not a fall-back to arithmetic: the rung does not fire on a guess."""
    assert galaxy._ubb_reset_plan({str(i) for i in range(8)}, 32, None) is None
    assert galaxy._affected_trays({"20"}, 32, None) is None
    assert galaxy._ubb_tray_walk_plan({"20"}, 32, None) is None


def test_a_chip_the_map_does_not_place_declines_rather_than_resetting_the_rest(bh_trays):
    """A drop containing a chip absent from the map cannot be planned: re-powering only the trays
    that could be resolved would leave the unplaced chip down and report the walk as done."""
    partial = {tray: [c for c in chips if c != 20] for tray, chips in bh_trays.items()}

    assert galaxy._affected_trays({"20"}, 32, partial) is None
    assert galaxy._ubb_tray_walk_plan({"19", "20"}, 32, partial) is None


# --- the map reaches the rung that fires -------------------------------------------------------


def _galaxy_snapshot():
    """A 32-chip Blackhole Galaxy snapshot carrying the real bus ids."""
    return {
        "device_info": [
            {
                "board_info": {"board_id": f"0100{i:04d}", "board_type": "tt-galaxy-bh", "bus_id": bus},
                "telemetry": {"asic_temperature": 45},
            }
            for i, bus in enumerate(BH_BUS_IDS)
        ]
    }


def _fake_smi(snapshot):
    def run(argv, capture_output=True, text=True, timeout=None, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(snapshot), "")

    return run


def _seed_sysfs(tmp_path, chip_buses):
    """Give the (sealed, per-test) sysfs class dir one ``tenstorrent!N`` node per chip, its
    ``device`` link pointing at a PCI device dir named for the chip's address — the shape the
    KMD exposes. Any nodes already there are replaced."""
    for node in list(pci.SYSFS_CLASS_DIR.iterdir()):
        (node / "device").unlink(missing_ok=True)
        node.rmdir()
    devices = tmp_path / "pci-devices"
    devices.mkdir(exist_ok=True)
    for chip, address in chip_buses.items():
        target = devices / address
        target.mkdir(exist_ok=True)
        node = pci.SYSFS_CLASS_DIR / f"tenstorrent!{chip}"
        node.mkdir()
        (node / "device").symlink_to(target)


@pytest.fixture
def galaxy_seen(monkeypatch, tmp_path):
    """Put the broker in the state it is in after one healthy snapshot of a Blackhole Galaxy: the
    board types, the snapshot's bus ids and the kernel's chip -> bus map cached, which is the only
    place the tray map can come from once chips start leaving the bus."""
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    monkeypatch.setattr(srv.health_monitor, "_chip_buses", None, raising=False)
    _seed_sysfs(tmp_path, BH_CHIP_BUSES)
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(_galaxy_snapshot()))
    ok, _detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert ok


def test_the_snapshot_caches_every_chips_bus_id(galaxy_seen):
    """The bus id is only readable while the chip is ON the bus, and the tray rung only ever runs
    once chips have left it. So the gate's own snapshot has to bank the map on the way past."""
    assert srv.health_monitor._bus_ids == BH_BUS_IDS


def test_the_tray_map_keys_on_the_kernels_chip_ids_not_the_snapshots_list_order(galaxy_seen):
    """tt-smi's snapshot is in PCI order; the kernel's ids put 0xCX before 0x8X. The banked map is
    the kernel's, so chip 24 (0x81) is on tray 4 — the case issue #27 found re-powering tray 3.
    Fails on base: the map was read by position from the PCI-ordered list."""
    assert srv.health_monitor._chip_buses == BH_CHIP_BUSES
    trays = srv.galaxy_recovery._tray_map_now()

    assert trays[3] == list(range(16, 24))
    assert trays[4] == list(range(24, 32))


def _recording_events(monkeypatch):
    from tt_device_mcp.health import monitor as monitor_mod

    monkeypatch.setattr(srv.health_monitor, "_skip_events_journaled", set())
    events = []

    def record(kind, **fields):
        events.append((kind, fields))

    patch_health_event(monkeypatch, record)
    monkeypatch.setattr(monitor_mod, "health_event", record)
    return events


@pytest.mark.parametrize(
    "sysfs",
    [
        {c: b for c, b in BH_CHIP_BUSES.items() if c != 20},  # a chip whose node vanished mid-read
        {**BH_CHIP_BUSES, 20: "0000:21:00.0"},  # a bus tt-smi did not report
    ],
    ids=["sysfs-short", "sysfs-other-bus"],
)
def test_sysfs_and_tt_smi_disagreeing_banks_no_map_and_logs_both(monkeypatch, tmp_path, sysfs):
    """The self-check: the kernel's map and tt-smi's snapshot must name the same buses. When they do
    not, one read raced the bus, so nothing is banked, both are journaled, and the next full pass
    that agrees banks the map."""
    events = _recording_events(monkeypatch)
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    monkeypatch.setattr(srv.health_monitor, "_chip_buses", None, raising=False)
    _seed_sysfs(tmp_path, sysfs)
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(_galaxy_snapshot()))

    srv.health_monitor.verify_device_health(32, timeout_sec=5)

    assert srv.health_monitor._chip_buses is None
    assert srv.galaxy_recovery._tray_map_now() is None
    mismatch = [f for k, f in events if k == "chip_bus_map_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["tt_smi"] == BH_BUS_IDS
    assert mismatch[0]["sysfs"] == {str(c): b for c, b in sorted(sysfs.items())}

    _seed_sysfs(tmp_path, BH_CHIP_BUSES)
    srv.health_monitor.verify_device_health(32, timeout_sec=5)

    assert srv.health_monitor._chip_buses == BH_CHIP_BUSES


def _event_map(chip_buses):
    return {str(c): b for c, b in sorted(chip_buses.items())}


def test_a_renumbered_full_read_re_banks_the_map_and_journals_old_and_new(monkeypatch, tmp_path, galaxy_seen):
    """tt-kmd can give a chip another index after a drop, a rescan or a tray reset, without tt-smi's
    PCI-ordered list changing at all. A full read that sysfs and tt-smi agree on is the kernel's
    numbering now, so it replaces the banked map in the same pass and the journal carries both maps.
    Fails on 2bb8128: the drift was journaled and the stale map kept."""
    events = _recording_events(monkeypatch)
    _seed_sysfs(tmp_path, RENUMBERED_CHIP_BUSES)

    srv.health_monitor.verify_device_health(32, timeout_sec=5)
    srv.health_monitor.verify_device_health(32, timeout_sec=5)

    assert srv.health_monitor._chip_buses == RENUMBERED_CHIP_BUSES
    drift = [f for k, f in events if k == "chip_bus_map_drift"]
    assert len(drift) == 1, "one change, one event: the second pass matches the re-banked map"
    assert drift[0]["old"] == _event_map(BH_CHIP_BUSES)
    assert drift[0]["new"] == _event_map(RENUMBERED_CHIP_BUSES)
    trays = srv.galaxy_recovery._tray_map_now()
    assert trays[3] == list(range(17, 25))
    assert trays[4] == [16, *range(25, 32)]


@pytest.mark.asyncio
async def test_after_a_renumbering_the_walk_re_powers_the_tray_of_the_chips_new_bus(
    monkeypatch, tmp_path, galaxy_seen, clear_job_state
):
    """What the re-bank is for: chip 24 now sits on 0xC1, so its drop is a tray-3 drop. The first
    mask is 0x04 and the handshake quiesces tray 3's chips as the kernel numbers them now. Fails on
    2bb8128: the stale map still put chip 24 on 0x81 and fired 0x08 at tray 4's healthy chips."""
    _recording_events(monkeypatch)
    _seed_sysfs(tmp_path, RENUMBERED_CHIP_BUSES)
    srv.health_monitor.verify_device_health(32, timeout_sec=5)
    fired, lines = [], []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append((bitmap, ids)), raising=False)

    beats = {str(i): 100 for i in range(32) if i != 24}
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 1, 32, lines.append)

    assert out is True
    assert fired[0] == (0x04, list(range(17, 25)))
    assert any("re-powering tray 3 (BMC mask 0x04, chips 17-24)" in line for line in lines), lines


@pytest.mark.parametrize(
    "smi_off_bus, sysfs, mismatch",
    [
        # tt-smi saw the whole mesh, but one chip's node was gone by the time sysfs was read.
        (None, {c: b for c, b in RENUMBERED_CHIP_BUSES.items() if c != 20}, True),
        # chip 24 off the bus: tt-smi's snapshot is short and sysfs has no node for it either.
        (RENUMBERED_CHIP_BUSES[24], {c: b for c, b in RENUMBERED_CHIP_BUSES.items() if c != 24}, False),
        # every node there, one on a bus tt-smi did not report: one of the two reads raced the bus.
        (None, {**RENUMBERED_CHIP_BUSES, 20: "0000:21:00.0"}, True),
    ],
    ids=["sysfs-short", "chip-off-the-bus", "sysfs-other-bus"],
)
def test_a_short_or_disagreeing_read_never_overwrites_the_banked_map(
    monkeypatch, tmp_path, galaxy_seen, smi_off_bus, sysfs, mismatch
):
    """Only a trusted full read moves the map. A chip off the bus has no node, so a short read
    cannot place it, and a read tt-smi disagrees with raced the bus: the last trusted map stands
    even when the chips that are readable look renumbered."""
    events = _recording_events(monkeypatch)
    snapshot = _galaxy_snapshot()
    snapshot["device_info"] = [d for d in snapshot["device_info"] if d["board_info"]["bus_id"] != smi_off_bus]
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(snapshot))
    _seed_sysfs(tmp_path, sysfs)

    srv.health_monitor.verify_device_health(32, timeout_sec=5)

    assert srv.health_monitor._chip_buses == BH_CHIP_BUSES
    assert srv.galaxy_recovery._tray_map_now()[4] == list(range(24, 32))
    kinds = [k for k, _ in events]
    assert "chip_bus_map_drift" not in kinds
    assert ("chip_bus_map_mismatch" in kinds) is mismatch


def test_a_full_read_that_matches_the_banked_map_changes_nothing(monkeypatch, galaxy_seen):
    """The steady state: every full gate pass re-reads sysfs, and a map that has not moved stays
    the banked one and journals nothing."""
    events = _recording_events(monkeypatch)
    banked = srv.health_monitor._chip_buses

    ok, _detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)

    assert ok
    assert srv.health_monitor._chip_buses is banked
    assert [k for k, _ in events if k.startswith("chip_bus_map")] == []


def test_a_drifted_snapshot_journals_but_leaves_the_cached_map_standing(monkeypatch):
    """A warm reset (this rung's own, the per-chip bridge, or a power cycle) can re-order PCI
    enumeration. tt-smi's bus list is banked once beside the chip map, and a later full snapshot
    that disagrees is worth naming (spec 04 I16). No tray is read from this list (the chip map,
    which re-banks, is what the trays come from), so the list stands."""
    from tt_device_mcp.health import monitor as monitor_mod

    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    monkeypatch.setattr(srv.health_monitor, "_skip_events_journaled", set())
    events = []

    def record(kind, **fields):
        events.append((kind, fields))

    patch_health_event(monkeypatch, record)
    monkeypatch.setattr(monitor_mod, "health_event", record)
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(_galaxy_snapshot()))
    srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert srv.health_monitor._bus_ids == BH_BUS_IDS

    drifted_bus = list(BH_BUS_IDS)
    drifted_bus[0], drifted_bus[8] = drifted_bus[8], drifted_bus[0]  # tray-1 and tray-2 head chips swap
    drifted = {
        "device_info": [
            {
                "board_info": {"board_id": f"0100{i:04d}", "board_type": "tt-galaxy-bh", "bus_id": bus},
                "telemetry": {"asic_temperature": 45},
            }
            for i, bus in enumerate(drifted_bus)
        ]
    }
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(drifted))
    srv.health_monitor.verify_device_health(32, timeout_sec=5)

    assert srv.health_monitor._bus_ids == BH_BUS_IDS, "the cached map must not move on drift"
    drift_events = [e for e in events if e[0] == "bus_map_drift"]
    assert len(drift_events) == 1, drift_events

    srv.health_monitor.verify_device_health(32, timeout_sec=5)
    drift_events = [e for e in events if e[0] == "bus_map_drift"]
    assert len(drift_events) == 1, "the same drift must not re-fire the event"


def test_a_partial_boot_snapshot_never_freezes_the_map(monkeypatch):
    """The boot platform probe calls verify_device_health(0) to identify the machine on a mesh that
    may already have chips down. The count guard "past the count check" is a no-op at
    expected_count == 0, so a boot-after-drop snapshot must not bank a partial bus map — a shorter
    list keyed positionally by enumerate() would attribute one chip's bus to another's id and the
    tray rung would fire on the wrong tray (I16). First-write-wins pins whatever lands first, so
    the guard must live in _bank_bus_ids_once itself, not in the caller's count check."""
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)

    partial = {
        "device_info": [
            {
                "board_info": {"board_id": f"0100{i:04d}", "board_type": "tt-galaxy-bh", "bus_id": bus},
                "telemetry": {"asic_temperature": 45},
            }
            for i, bus in enumerate(BH_BUS_IDS[:24])
        ]
    }
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(partial))
    srv.health_monitor.verify_device_health(0, timeout_sec=5)
    assert srv.health_monitor._bus_ids is None, "a boot probe with expected_count=0 must not bank a bus map"

    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(_galaxy_snapshot()))
    ok, _detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert ok
    assert srv.health_monitor._bus_ids == BH_BUS_IDS, "the first full-count snapshot must fill the cache"


def test_a_degraded_first_normal_snapshot_cannot_freeze_the_map(monkeypatch):
    """A first normal gate pass on a fresh broker can derive expected_count from the chips it sees.
    That survivor count is enough to verify "nothing got worse", but not enough to prove a Galaxy's
    tray map is complete: a later full snapshot still has to get to fill the cache."""
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)

    partial = {
        "device_info": [
            {
                "board_info": {"board_id": f"0100{i:04d}", "board_type": "tt-galaxy-bh", "bus_id": bus},
                "telemetry": {"asic_temperature": 45},
            }
            for i, bus in enumerate(BH_BUS_IDS[:24])
        ]
    }
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(partial))
    expected = srv.health_monitor.expected(24)
    assert expected == 24

    ok, _detail = srv.health_monitor.verify_device_health(expected, timeout_sec=5)

    assert ok
    assert srv.health_monitor._bus_ids is None, "the baseline's first survivor count is not a full tray map"

    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(_galaxy_snapshot()))
    expected = srv.health_monitor.expected(32)
    ok, _detail = srv.health_monitor.verify_device_health(expected, timeout_sec=5)
    assert ok
    assert srv.health_monitor._bus_ids == BH_BUS_IDS, "a later full snapshot must still earn the cache"


@pytest.mark.asyncio
async def test_the_walk_re_powers_the_tray_the_dropped_chips_actually_sit_on(monkeypatch, galaxy_seen, clear_job_state):
    """Chips 24-31 (0x8X) dropping is a tray-4 drop on Blackhole, so the BMC bitmap must be 0x08
    and the ioctl handshake must quiesce chips 24-31. Read by position from tt-smi's PCI-ordered
    list this was 0x04 — bit 2 — which re-powers tray 3's eight healthy chips and leaves the
    dropped tray down."""
    fired = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append((bitmap, ids)), raising=False)

    beats = {str(i): 100 for i in range(24)}
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)

    assert out is True
    assert fired == [(0x08, list(range(24, 32)))]


@pytest.mark.asyncio
async def test_a_lone_off_bus_chip_24_re_powers_tray_4_first(monkeypatch, galaxy_seen, clear_job_state):
    """Issue #27's case: chip 24 (0000:81:00.0) off the bus. The first mask must be 0x08 (tray 4),
    not 0x04, and the log line names the tray, its BMC mask and its chips."""
    fired, lines = [], []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append((bitmap, ids)), raising=False)

    beats = {str(i): 100 for i in range(32) if i != 24}
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 1, 32, lines.append)

    assert out is True
    assert fired[0] == (0x08, list(range(24, 32)))
    assert any("re-powering tray 4 (BMC mask 0x08, chips 24-31)" in line for line in lines), lines


# --- the back-to-back sweep journals the mask it fired -----------------------------------------


def _sweep_stubs(monkeypatch, fire):
    """Stub every device-touching step of the back-to-back sweep and record its events and lines."""
    g = srv.galaxy_recovery
    events, lines = [], []
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda kind, **fields: events.append((kind, fields)))

    async def sbr(log):
        return False

    async def mesh(argv, log, *a, **k):
        return 0, ""

    async def healthy(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    patch_recovery(monkeypatch, "_verify_device", healthy)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", fire)
    return g, events, lines


@pytest.mark.asyncio
async def test_the_sweep_journals_the_tray_4_mask_it_fired_for_chips_24_to_31(monkeypatch, galaxy_seen):
    """Chips 24-31 off a Blackhole Galaxy: the sweep's BMC re-power must leave exactly one
    ubb_reset_fired naming mask 0x08 and chips 24-31, and a log line naming tray 4 beside the exact
    ipmitool command. Fails on base: a successful fire emitted no event and no line, so nobody could
    prove afterwards which mask went out."""
    fired = []
    g, events, lines = _sweep_stubs(monkeypatch, lambda bitmap, ids: fired.append((bitmap, ids)))

    offbus = {str(i) for i in range(24, 32)}
    out = await g._fire_tray_down_no_window(offbus, 32, lines.append, gate_phase="post-job", ev=None)

    assert out == galaxy.OUTCOME_RECOVERED
    assert fired == [(0x08, list(range(24, 32)))]
    done = [f for k, f in events if k == "ubb_reset_fired"]
    assert len(done) == 1, events
    assert done[0]["ubb_bitmap"] == 0x08
    assert done[0]["chips"] == list(range(24, 32))
    assert done[0]["trays"] == [4]
    assert done[0]["rc"] == 0
    command = " ".join(galaxy._ubb_reset_argv(0x08))
    assert done[0]["command"] == command
    assert any(f"tray 4 (BMC mask 0x08, chips 24-31): `{command}`" in line for line in lines), lines


@pytest.mark.asyncio
async def test_a_failed_sweep_fire_journals_its_rc_and_no_fired_event(monkeypatch, galaxy_seen):
    """A BMC command that exits non-zero is journalled as ubb_reset_failed with its exit code and
    mask, never as ubb_reset_fired, and the sweep still goes on to the mesh reset."""
    from tt_device_mcp.health.recovery.stages.ubb_tray import UbbResetError

    def fail(bitmap, ids):
        raise UbbResetError(1, "Unable to send RAW command\n")

    g, events, lines = _sweep_stubs(monkeypatch, fail)

    offbus = {str(i) for i in range(24, 32)}
    await g._fire_tray_down_no_window(offbus, 32, lines.append, gate_phase="post-job", ev=None)

    kinds = [k for k, _ in events]
    assert "ubb_reset_fired" not in kinds
    failed = [f for k, f in events if k == "ubb_reset_failed"]
    assert len(failed) == 1
    assert failed[0]["rc"] == 1
    assert failed[0]["ubb_bitmap"] == 0x08
    assert failed[0]["chips"] == list(range(24, 32))
    assert any("failed to launch (rc 1)" in line for line in lines), lines


@pytest.mark.asyncio
async def test_the_last_chance_sweep_labels_each_tray_from_the_map(monkeypatch, galaxy_seen):
    """The other caller of the sweep (the last-chance gate before a host rung) also hands the map
    down, so its per-tray line names the tray's chips and not an empty list."""
    g, events, lines = _sweep_stubs(monkeypatch, lambda bitmap, ids: None)

    ok = await g._settle_and_verify_before_host_rung(
        32, lines.append, "post-job", offbus_chips={str(i) for i in range(24, 32)}
    )

    assert ok is True
    assert any("tray 4 (BMC mask 0x08, chips 24-31)" in line for line in lines), lines
    assert [f["ubb_bitmap"] for k, f in events if k == "ubb_reset_fired"] == [0x08]


# --- the Wormhole snapshot suffix must not disable the rung ------------------------------------


def test_a_wormhole_snapshot_with_l_r_suffixes_still_produces_a_tray_map(monkeypatch):
    """tt-smi appends " L"/" R" to every wormhole board type in its snapshot copy (backend.py's
    get_logs_json). A WH Galaxy therefore always caches two board strings — the unanimity check on
    the raw set has size 2 and would decline the rung on every WH host. I16's "no map, no fire" is
    for missing facts, not for a snapshot that carries the machine type in the shape tt-smi ships."""
    monkeypatch.setattr(srv.health_monitor, "_chip_buses", BH_CHIP_BUSES, raising=False)
    monkeypatch.setattr(
        srv.health_monitor,
        "_board_types",
        ["tt-galaxy-wh L"] * 16 + ["tt-galaxy-wh R"] * 16,
    )

    trays = srv.galaxy_recovery._tray_map_now()

    assert trays is not None
    # Wormhole numbers the same bus groups differently from Blackhole (see the WH table test above).
    assert trays[1] == list(range(16, 24))
    assert trays[2] == list(range(24, 32))
    assert trays[3] == list(range(0, 8))
    assert trays[4] == list(range(8, 16))


def test_a_wormhole_lookup_survives_the_snapshot_suffix_inside_tray_map():
    """Direct callers of _tray_map should not have to strip the suffix — the normalization belongs
    inside the map so a future call site cannot forget it. Fails on base: the lookup key still
    carries " L", tables are keyed by unsuffixed values, so it returns None."""
    assert galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-wh L") is not None
    assert galaxy._tray_map(BH_CHIP_BUSES, "tt-galaxy-wh R") is not None


@pytest.mark.asyncio
async def test_a_broker_with_no_cached_bus_map_declines_the_walk(monkeypatch, clear_job_state):
    """A broker restarted after the drop never saw a healthy snapshot, so it cannot know which tray
    holds the dropped chips. I16 declines rather than guess — and declining must not spend the
    episode's one walk, so a later snapshot can still earn a real one."""
    fired = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.health_monitor, "_chip_buses", None, raising=False)
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["tt-galaxy-bh"] * 32)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append(bitmap), raising=False)

    beats = {str(i): 100 for i in range(24)}
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)

    assert out is None
    assert fired == []
    assert srv.fsm.latch("ubb_reset_fired") is False
