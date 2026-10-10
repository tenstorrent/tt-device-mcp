# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A Galaxy's UBB tray is identified by its chips' PCI bus, never by their index (spec 04 I16).

The two are not the same ordering. tt-smi maps a chip to a tray by masking its bus id to the tray
group and looking the group up in a per-architecture table, and on a Blackhole Galaxy that table
puts bus 0x80 (chips 16-23) on tray 4 and bus 0xc0 (chips 24-31) on tray 3 — the reverse of what
chip-index arithmetic yields. A bitmap built from the index therefore pulses a tray the drop never
touched, which on this rung means re-powering eight healthy chips and leaving the dead ones dead.

The bus ids below are the real ones from a 32-chip Blackhole Galaxy snapshot.
"""

import json
import subprocess

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import galaxy

# Bus ids as a Blackhole Galaxy reports them: four groups of eight, one per tray, the low nibble
# counting the chips within the group.
BH_BUS_IDS = [f"0000:{group + n:02x}:00.0" for group in (0x00, 0x40, 0x80, 0xC0) for n in range(1, 9)]


def test_a_blackhole_tray_comes_from_the_bus_group_not_the_chip_index():
    """Chips 16-23 are on bus group 0x80, which tt-smi's Blackhole table numbers tray 4, and chips
    24-31 on 0xc0, which is tray 3. Index arithmetic yields 2 and 3 for those two groups, so it
    names the wrong tray for both. Fails on base: the derivation does not exist."""
    trays = galaxy._tray_map(BH_BUS_IDS, "tt-galaxy-bh")

    assert trays[1] == list(range(0, 8))
    assert trays[2] == list(range(8, 16))
    assert trays[3] == list(range(24, 32))
    assert trays[4] == list(range(16, 24))


def test_a_wormhole_tray_uses_the_wormhole_table_for_the_same_buses():
    """The bus groups are the same silicon layout on both architectures; the tray NUMBERING is not.
    Wormhole numbers 0xc0 tray 1 and 0x00 tray 3 — so the identical bus ids must resolve to a
    different tray map than the Blackhole case above, which is the whole reason the board type
    selects the table instead of one being hardcoded."""
    trays = galaxy._tray_map(BH_BUS_IDS, "tt-galaxy-wh")

    assert trays[1] == list(range(24, 32))
    assert trays[2] == list(range(16, 24))
    assert trays[3] == list(range(0, 8))
    assert trays[4] == list(range(8, 16))


def test_an_unreadable_bus_id_yields_no_map_rather_than_a_partial_one():
    """A chip whose bus id the snapshot did not carry cannot be placed on a tray, and a map missing
    one chip would silently drop it from its tray's reset. Refuse the whole map instead: the rung's
    decline path is the safe one, a half-map is not."""
    holes = list(BH_BUS_IDS)
    holes[20] = ""

    assert galaxy._tray_map(holes, "tt-galaxy-bh") is None


def test_a_bus_group_outside_the_table_yields_no_map():
    """Every chip must land in one of the four known tray groups. A bus outside them means this is
    not the topology the table describes, and guessing a tray for it is exactly what I16 forbids."""
    strays = list(BH_BUS_IDS)
    strays[0] = "0000:21:00.0"  # group 0x20 — no tray

    assert galaxy._tray_map(strays, "tt-galaxy-bh") is None


def test_a_non_galaxy_board_type_yields_no_map():
    """The UBB tables describe a Galaxy. Anything else has no trays to map, and the rung declines
    on that rather than borrowing a Galaxy's numbering."""
    assert galaxy._tray_map([f"0000:{n:02x}:00.0" for n in range(1, 5)], "n300") is None


@pytest.mark.parametrize("board_type", ["", None])
def test_an_unknown_board_type_yields_no_map(board_type):
    """No identified board type means no table can be chosen. Unknown is not Wormhole-by-default:
    picking either table here would be the guess I16 exists to prevent."""
    assert galaxy._tray_map(BH_BUS_IDS, board_type) is None


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
    return galaxy._tray_map(BH_BUS_IDS, "tt-galaxy-bh")


def test_a_whole_tray_drop_sets_the_bit_for_the_tray_that_is_down(bh_trays):
    """The bitmap the BMC receives is what decides which silicon is re-powered, so it has to key on
    the real tray. Chips 16-23 are tray 4 (bit 3) and 24-31 are tray 3 (bit 2). Index arithmetic
    gives 0x04 and 0x08 for these two drops — each pulsing the other's tray."""
    assert galaxy._ubb_reset_plan({str(i) for i in range(16, 24)}, 32, bh_trays) == ([4], 0x08)
    assert galaxy._ubb_reset_plan({str(i) for i in range(24, 32)}, 32, bh_trays) == ([3], 0x04)


def test_affected_trays_names_the_tray_an_operator_would_read_from_tt_smi(bh_trays):
    """The tray ids ride out in the ubb_reset_required event and in the operator line beside the
    BMC command. A lone off-bus chip 20 is on tray 4, which is what
    `tt-smi -glx_list_tray_to_device` prints for it."""
    assert galaxy._affected_trays({"20"}, 32, bh_trays) == [4]
    assert galaxy._affected_trays({"0", "31"}, 32, bh_trays) == [1, 3]


def test_the_walk_covers_only_the_affected_trays(bh_trays):
    """The walk is the affected trays only, ascending — over real tray numbers, which are 1-based, not
    the 0-based ordinals the index arithmetic produced (spec 04 I25: never a healthy tray)."""
    assert galaxy._ubb_tray_walk_plan({"20"}, 32, bh_trays) == [4]
    assert galaxy._ubb_tray_walk_plan({"0", "31"}, 32, bh_trays) == [1, 3]


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


@pytest.fixture
def galaxy_seen(monkeypatch):
    """Put the broker in the state it is in after one healthy snapshot of a Blackhole Galaxy: the
    board types and the per-chip bus ids cached, which is the only place the tray map can come
    from once chips start leaving the bus."""
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi(_galaxy_snapshot()))
    ok, _detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert ok


def test_the_snapshot_caches_every_chips_bus_id(galaxy_seen):
    """The bus id is only readable while the chip is ON the bus, and the tray rung only ever runs
    once chips have left it. So the gate's own snapshot has to bank the map on the way past."""
    assert srv.health_monitor._bus_ids == BH_BUS_IDS


def test_a_drifted_snapshot_journals_but_leaves_the_cached_map_standing(monkeypatch):
    """A warm reset (this rung's own, the per-chip bridge, or a power cycle) can re-order PCI
    enumeration. The cache is one-shot, but a later full snapshot that disagrees is worth naming
    so a wrong tray decision is not silent (spec 04 I16). Overwriting on drift would open a race
    where a bad snapshot replaces a known-good one, so the cache stands."""
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
    """Chips 16-23 dropping is a tray-4 drop on Blackhole, so the BMC bitmap must be 0x08 and the
    ioctl handshake must quiesce chips 16-23. Chip-index arithmetic makes this 0x04 — bit 2 — which
    re-powers tray 3's eight healthy chips and leaves the dropped tray down."""
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

    beats = {str(i): 100 for i in list(range(16)) + list(range(24, 32))}
    out = await srv.galaxy_recovery._attempt_ubb_tray_reset(beats, 8, 32, lambda m: None)

    assert out is True
    assert fired == [(0x08, list(range(16, 24)))]


# --- the Wormhole snapshot suffix must not disable the rung ------------------------------------


def test_a_wormhole_snapshot_with_l_r_suffixes_still_produces_a_tray_map(monkeypatch):
    """tt-smi appends " L"/" R" to every wormhole board type in its snapshot copy (backend.py's
    get_logs_json). A WH Galaxy therefore always caches two board strings — the unanimity check on
    the raw set has size 2 and would decline the rung on every WH host. I16's "no map, no fire" is
    for missing facts, not for a snapshot that carries the machine type in the shape tt-smi ships."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", BH_BUS_IDS, raising=False)
    monkeypatch.setattr(
        srv.health_monitor,
        "_board_types",
        ["tt-galaxy-wh L"] * 16 + ["tt-galaxy-wh R"] * 16,
    )

    trays = srv.galaxy_recovery._tray_map_now()

    assert trays is not None
    # Wormhole numbers the same bus groups differently from Blackhole (see the WH table test above).
    assert trays[1] == list(range(24, 32))
    assert trays[2] == list(range(16, 24))
    assert trays[3] == list(range(0, 8))
    assert trays[4] == list(range(8, 16))


def test_a_wormhole_lookup_survives_the_snapshot_suffix_inside_tray_map():
    """Direct callers of _tray_map should not have to strip the suffix — the normalization belongs
    inside the map so a future call site cannot forget it. Fails on base: the lookup key still
    carries " L", tables are keyed by unsuffixed values, so it returns None."""
    assert galaxy._tray_map(BH_BUS_IDS, "tt-galaxy-wh L") is not None
    assert galaxy._tray_map(BH_BUS_IDS, "tt-galaxy-wh R") is not None


@pytest.mark.asyncio
async def test_a_broker_with_no_cached_bus_map_declines_the_walk(monkeypatch, clear_job_state):
    """A broker restarted after the drop never saw a healthy snapshot, so it cannot know which tray
    holds the dropped chips. I16 declines rather than guess — and declining must not spend the
    episode's one walk, so a later snapshot can still earn a real one."""
    fired = []
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
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
