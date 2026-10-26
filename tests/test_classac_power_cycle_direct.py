# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Whole-tray and all-off drops go to the power cycle with no reset first (spec 04 I37).

Class A is one or more whole trays off the bus with a chip still on it; class C is every chip still
off after a mesh reset already ran and failed. No whole-tray drop healed without a power cycle, and
on all-off drops whose first mesh reset failed each repeat ran 130-262 s and exited 1; one mesh reset
has healed an all-off bus, at startup (after a broker restart) as at run time, so that one still
runs. These pin the short path: the cycle is requested in the first window, behind the host rung's
own guards, and partial drops keep the full ladder. No test reaches a real reset, sweep or power cycle."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_arc_not_ready import _fake_sysfs
from tests.test_startup_fabric_hugepages import _shortfalls, _startup, _systemctl  # noqa: F401 - the fixtures
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.evidence import health_dir
from tt_device_mcp.health.monitors import heartbeat
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.base import RESET_RECORD_FILE

EXPECTED = 32
RESET_SEC = 262  # the longest measured failed mesh reset (rc 1) on an all-off drop
SWEEP_SEC = 323  # the last-chance sweep before a host rung (I22)
SKIPPED = "last_chance_sweep_skipped"


def _off(tray_map, spec: dict) -> set:
    """The first ``n`` chips of each tray in ``{tray: n}``."""
    return {c for t, n in spec.items() for c in sorted(tray_map[t])[:n]}


def _beats_without(off: set) -> dict:
    return {str(i): 100 for i in range(EXPECTED) if i not in off}


@pytest.fixture
def rig(monkeypatch, tmp_path, galaxy_trays):
    """srv.galaxy_recovery with the rescan, the capture, the mesh reset, the sweep's resets and the
    power cycle recorded in ``rig['order']`` and timed on ``rig['clock']``; the bus is read from
    ``rig['beats']``. The gate's dispatch, the I37 hook and the last-chance sweep are the real ones."""
    g = srv.galaxy_recovery
    state = {
        "beats": {},
        "events": [],
        "order": [],
        "clock": [0],
        "cycled_at": [],
        "allowed": (True, ""),
        "scope": None,
        "scan": HolderScan(holders=[], complete=True),
        "map": galaxy_trays,
        "g": g,
    }
    order = state["order"]
    patch_health_event(monkeypatch, lambda kind, **k: state["events"].append((kind, k)))
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: order.append("released"))
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: state["scope"])
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: state["scan"])
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(state["beats"]))
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(g.mechanism, "auto_recovery_allowed", lambda action, tenant_active=False, **k: state["allowed"])
    monkeypatch.setattr(g.mechanism, "_journal_auto_recovery_denied", lambda *a, **k: order.append("denied"))
    monkeypatch.setattr(g.mechanism, "mesh_reset_failed_since_release", False)
    monkeypatch.setattr(g.mechanism, "last_reset_rc", None)
    monkeypatch.setattr(g.mechanism, "_reset_record_cache", None)
    monkeypatch.setattr(g.mechanism, "whole_tray_off", None)  # spec 04 I73 latch: the mechanism is shared
    monkeypatch.setattr(g.mechanism, "_ac_power_paged", 0.0)
    monkeypatch.setattr(g.mechanism, "_ac_power_logged", set())
    monkeypatch.setattr(galaxy, "TRAY_DOWN_RESCAN_SETTLE_SEC", 0)
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    # No operator check installed or configured, and no stand-back marker: nothing objects.
    monkeypatch.setenv("TTDEV_ROOT", str(tmp_path))
    for name in (
        "TT_DEVICE_MCP_MANUAL_RECOVERY_CMD",
        "TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD",
        "TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE",
        "TT_DEVICE_MCP_TRAY_DOWN_CAPTURE",
        "TT_DEVICE_MCP_OFFBUS_HOLD_ESCALATE",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(galaxy, "_pci_rescan", lambda: order.append("rescan"))
    monkeypatch.setattr(g, "_tray_down_capture", lambda onset, expected: order.append("capture") or {})

    async def power_cycle(log, reason):
        order.append("power_cycle")
        state["cycled_at"].append(state["clock"][0])

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)

    async def mesh_reset(indices, log):
        # As the real one on a drop no reset heals: it runs, exits 1 and records the failure.
        order.append("glx_reset")
        state["clock"][0] += RESET_SEC
        g.mechanism.mesh_reset_failed_since_release = True
        g.mechanism.last_reset_rc = 1
        return False

    async def sweep(trays, expected, log, do_sbr=True, **_kw):
        order.append("sweep")
        state["clock"][0] += SWEEP_SEC
        return True

    async def verify(expected, log, run_fabric=True, **k):
        return False, {}

    monkeypatch.setattr(g, "_reset_and_verify_device", mesh_reset)
    monkeypatch.setattr(g, "_issue_all_resets_back_to_back", sweep)
    monkeypatch.setattr(g, "_verify_device", verify)
    g.tray_down_episode_end()
    yield state
    g.tray_down_episode_end()


async def _record(order, name):
    order.append(name)
    return False


async def _gate(rig, stage, phase="post-job"):
    off = sorted(set(map(str, range(EXPECTED))) - set(rig["beats"]), key=int)
    return await rig["g"].escalate(f"gate/{phase}", off, EXPECTED, lambda m: None, stage=stage)


def _kinds(rig):
    return [k for k, _ in rig["events"]]


# ---- the fault class ----------------------------------------------------------------------------


def test_the_fault_class_of_an_off_bus_set(galaxy_trays):
    m = galaxy_trays
    every = set(range(EXPECTED))

    def cls(off, *, failed=False, expected=EXPECTED, tray_map=m):
        return galaxy._power_cycle_direct_class(off, expected, tray_map, mesh_reset_failed=failed)

    assert cls(_off(m, {3: 8})) == galaxy.FAULT_CLASS_WHOLE_TRAY
    assert cls(_off(m, {1: 8, 2: 8, 3: 8})) == galaxy.FAULT_CLASS_WHOLE_TRAY, "three trays, one still on"
    assert cls(every, failed=True) == galaxy.FAULT_CLASS_ALL_OFF
    assert cls(every) is None, "an all-off drop gets its one mesh reset first, at startup as at run time"
    assert cls(_off(m, {3: 1})) is None, "one chip: B1"
    assert cls(_off(m, {3: 4})) is None, "part of one tray: the ladder"
    assert cls(_off(m, {3: 8, 2: 1})) is None, "a whole tray plus a stray chip is not exactly whole trays"
    assert cls(_off(m, {3: 8}), tray_map=None) is None, "no map, no class"
    assert cls({0, 1, 2, 3, 4, 5, 6, 7}, expected=8, failed=True) is None, "a single-tray host"
    assert cls(set(), failed=True) is None


# ---- class A ------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [{3: 8}, {1: 8, 4: 8}])
async def test_a_whole_tray_goes_to_the_power_cycle_with_no_reset(rig, spec):
    rig["beats"] = _beats_without(_off(rig["map"], spec))
    assert await _gate(rig, galaxy.STAGE_SMI_RESET) == galaxy.OUTCOME_WAITING
    assert rig["order"] == ["rescan", "capture", "power_cycle"], "no mesh reset, tray re-power or sweep"
    ((_, fired),) = [(k, f) for k, f in rig["events"] if k == "power_cycle_direct"]
    assert fired["fault_class"] == galaxy.FAULT_CLASS_WHOLE_TRAY and fired["phase"] == "firing"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["offbus", "offbus-forced"])
async def test_an_idle_whole_tray_hold_cycles_with_no_reset(rig, monkeypatch, phase):
    ladders = []
    monkeypatch.setattr(rig["g"], "_escalate_offbus_stuck_hold", lambda *a, **k: _record(ladders, "offbus"))
    rig["beats"] = _beats_without(_off(rig["map"], {2: 8}))
    assert await rig["g"].escalate(phase, [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["rescan", "capture", "power_cycle"] and ladders == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase, blocker, cycled",
    [
        ("offbus-forced", None, True),  # the deadline climb waives a scan it cannot read
        ("offbus", None, False),  # before the deadline an unreadable scan still holds
        ("offbus-forced", "job_running", False),
        ("offbus-forced", "tenant", False),
        ("offbus-forced", "startup_verify", False),  # spec 03 I75: the caller's stand-down, re-read here
    ],
)
async def test_the_deadline_climb_cycles_past_an_unreadable_scan_but_never_a_tenant(
    rig, monkeypatch, phase, blocker, cycled
):
    """The hold-deadline climb waives an unreadable holder scan, as the tray rung does, so a class A
    hold at its deadline is not held forever by a scan that cannot see; a running job or a readable
    tenant still holds it. Nothing is released here: the boot after the cycle runs the startup gate's
    full check, fabric included (03 I4)."""
    rig["scan"] = HolderScan(holders=[], complete=False)
    if blocker == "job_running":
        monkeypatch.setattr(rig["g"].deps, "job_running", lambda: True)
    elif blocker == "tenant":
        monkeypatch.setattr(galaxy, "is_tenant", lambda h: True)
        monkeypatch.setattr(galaxy, "holder_comm", lambda pid: "python")
        rig["scan"] = HolderScan(holders=[SimpleNamespace(pid=4242, uid=1000)], complete=True)
    rig["beats"] = _beats_without(_off(rig["map"], {2: 8}))
    rig["g"]._td_reset_seen = True  # no prelude: only the guard is under test
    stand_down = (lambda: True) if blocker == "startup_verify" else None
    out = await rig["g"].escalate(phase, [], EXPECTED, lambda m: None, stand_down=stand_down)
    assert rig["order"] == (["power_cycle"] if cycled else [])
    assert rig["g"].startup_verify_deferred is (blocker == "startup_verify")
    assert out == (galaxy.OUTCOME_TERMINAL if cycled else galaxy.OUTCOME_WAITING)
    assert "released" not in rig["order"]


# ---- class C ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_off_at_startup_gets_one_mesh_reset_first(rig):
    """The startup gate also runs after a broker restart, and one mesh reset has healed an all-off
    bus there: with no failed reset this episode it runs exactly once, then the next pass cycles."""
    rig["beats"] = {}
    await _gate(rig, galaxy.STAGE_SMI_RESET, phase="startup")
    assert rig["order"] == ["glx_reset"], "one mesh reset, no power cycle yet"
    assert "power_cycle_direct" not in _kinds(rig)
    assert await _gate(rig, galaxy.STAGE_SMI_RESET, phase="startup") == galaxy.OUTCOME_WAITING
    assert rig["order"] == ["glx_reset", "power_cycle"], "no second reset, no sweep, no tray re-power"


@pytest.mark.asyncio
async def test_all_off_at_startup_after_a_failed_reset_cycles_with_no_reset(rig):
    rig["beats"] = {}
    rig["g"].mechanism.mesh_reset_failed_since_release = True
    assert await _gate(rig, galaxy.STAGE_SMI_RESET, phase="startup") == galaxy.OUTCOME_WAITING
    assert rig["order"] == ["power_cycle"], "no glx_reset, no sweep, no tray re-power"


@pytest.mark.asyncio
async def test_all_off_after_a_failed_mesh_reset_cycles_with_no_second_reset(rig):
    """A run-time all-off drop gets one mesh reset; once it failed, the next pass cycles, whichever
    stage the router names: the reset stage requests it directly, and the host rung's sweep skips."""
    rig["beats"] = {}
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert rig["order"] == ["glx_reset"]
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert rig["order"] == ["glx_reset", "power_cycle"], "no second reset"
    await _gate(rig, galaxy.STAGE_POWER_CYCLE)
    assert rig["order"] == ["glx_reset", "power_cycle", "power_cycle"], "no sweep before the host rung"
    assert SKIPPED in _kinds(rig)


@pytest.mark.asyncio
async def test_the_failed_reset_mark_is_set_by_a_failed_reset_and_cleared_at_the_episode_end(monkeypatch):
    """The real mesh reset sets the mark on a nonzero exit and on an unhealthy verify, not on a healthy
    one; the end of the episode clears it."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "mesh_reset_failed_since_release", False)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(galaxy.pcie_guard, "chips_off_bus", lambda expected: 0)
    monkeypatch.setattr(galaxy.pcie_guard, "mask_for_mesh_reset", lambda log: None)
    result = {"rc": 1, "healthy": False}

    async def reset(argv, log):
        return result["rc"], ""

    async def verify(expected, log, run_fabric=True, **k):
        return result["healthy"], {}

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    monkeypatch.setattr(g, "_verify_device", verify)
    monkeypatch.setattr(g.mechanism, "auto_reset_cooldown_remaining", lambda: 0, raising=False)
    for rc, healthy, marked in [(1, False, True), (0, False, True), (0, True, False)]:
        g.mechanism.mesh_reset_failed_since_release = False
        result.update(rc=rc, healthy=healthy)
        await g._reset_and_verify_device(["0"], lambda m: None)
        assert g.mechanism.mesh_reset_failed_since_release is marked, (rc, healthy)
    g.mechanism.mesh_reset_failed_since_release = True
    g.tray_down_episode_end()
    assert g.mechanism.mesh_reset_failed_since_release is False


# ---- B1/B2 partial drops ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("spec, prelude", [({3: 1}, []), ({3: 4}, ["rescan", "capture"])])
async def test_a_partial_drop_keeps_the_ladder(rig, monkeypatch, spec, prelude):
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=2)).isoformat())
    rig["beats"] = _beats_without(_off(rig["map"], spec))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    await _gate(rig, galaxy.STAGE_POWER_CYCLE)
    # The mesh reset failed on this same set. The last-chance sweep still runs right before the host
    # rung (spec 04 I58): its mesh reset repeats once under the I97 deadline, then the power cycle.
    assert rig["order"] == [*prelude, "glx_reset", "sweep", "power_cycle"]
    assert "power_cycle_direct" not in _kinds(rig)
    assert [f.get("reason") for k, f in rig["events"] if k == SKIPPED] == []
    assert [f["fired"] for k, f in rig["events"] if k == "whole_mesh_reset_before_host_rung"] == [True]


# ---- the guards ---------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["scope", "tenant", "unreadable_scan", "standback_file", "rate_limit"])
async def test_a_guard_holds_the_power_cycle_and_fires_nothing(rig, monkeypatch, tmp_path, guard):
    if guard == "scope":
        rig["scope"] = "tt-reset-1.scope"
    elif guard == "tenant":
        rig["scan"] = HolderScan(holders=[object()], complete=True)
        monkeypatch.setattr(
            rig["g"], "_tenant_active", lambda scan, force=False: bool(scan.holders) or not (force or scan.complete)
        )
    elif guard == "unreadable_scan":
        rig["scan"] = HolderScan(holders=[], complete=False)
    elif guard == "standback_file":
        marker = tmp_path / "cycle-in-progress"
        marker.write_text("")
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE", str(marker))
    else:
        rig["allowed"] = (False, "power cycle rate limit: 1 in the last 6 h")
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    rig["g"]._td_reset_seen = True  # no prelude: only the hold is under test
    assert await _gate(rig, galaxy.STAGE_SMI_RESET) == galaxy.OUTCOME_WAITING
    expect = ["denied"] if guard == "rate_limit" else []
    assert rig["order"] == expect, "held: no reset, no sweep, no cycle"


@pytest.mark.asyncio
async def test_with_no_power_cycle_opted_in_the_ladder_runs(rig, monkeypatch):
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: False)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert rig["order"] == ["rescan", "capture", "glx_reset"]
    assert ("power_cycle_direct", "not_opted_in") in [(k, f.get("phase")) for k, f in rig["events"]]


# ---- the host rung's last-chance sweep ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, swept",
    [("whole_tray", False), ("all_off_after_failed_reset", False), ("all_off_run_time", True), ("part_tray", True)],
)
async def test_the_last_chance_sweep_skips_itself_for_a_and_c(rig, case, swept):
    m = rig["map"]
    off = {
        "whole_tray": _off(m, {3: 8}),
        "part_tray": _off(m, {3: 5}),
    }.get(case, set(range(EXPECTED)))
    rig["g"].mechanism.mesh_reset_failed_since_release = case == "all_off_after_failed_reset"
    out = await rig["g"]._settle_and_verify_before_host_rung(
        EXPECTED, lambda m: None, "post-job", offbus_chips={str(c) for c in off}
    )
    assert out is False, "never recovered: the caller climbs"
    assert ("sweep" in rig["order"]) is swept
    assert (SKIPPED in _kinds(rig)) is not swept


# ---- goal: the full check after the cycle ---------------------------------------------------------


@pytest.mark.asyncio
async def test_after_the_cycle_the_startup_gate_awaits_hugepages_and_the_fabric(monkeypatch, _startup):  # noqa: F811
    """The host the cycle brings back runs the startup pass only once the hugepage pool is full, with
    the fabric pass; a mesh still all off there gets its one mesh reset, then requests the cycle again
    behind its rate limit, holds, and never releases the device."""
    events, rows, gate_calls = _startup
    runs = []

    async def gate(job_log_file, *, phase, run_fabric, force_fabric=False):
        runs.append((phase, run_fabric))

    monkeypatch.setattr(srv, "_device_health_gate", gate)
    _shortfalls(monkeypatch, [(0, 32), (16, 32), None])
    await srv._verify_fabric_on_start()
    assert runs == [("startup", True)], "after the pool fills, the startup pass runs with the fabric"


@pytest.mark.asyncio
async def test_a_failed_startup_check_after_the_cycle_keeps_the_hold(rig):
    """The boot after the cycle starts with no failed reset, so an all-off mesh gets its one mesh
    reset; when that fails the cycle is asked again, the rate limit denies it, and the hold stays."""
    rig["beats"] = {}
    rig["allowed"] = (False, "power cycle rate limit: 1 in the last 6 h")
    await _gate(rig, galaxy.STAGE_SMI_RESET, phase="startup")
    assert await _gate(rig, galaxy.STAGE_SMI_RESET, phase="startup") == galaxy.OUTCOME_WAITING
    assert rig["order"] == ["glx_reset", "denied"], "one reset, no second one, no sweep, no release"


# ---- time to recover ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_to_recover_on_a_fake_clock(rig):
    """When the cycle is requested, on the clock each failed reset (262 s) and sweep (323 s) advance.
    Class A: at once. Class C, at startup as at run time: after its one reset. The partial-tray
    ladder (B2): after its one reset; the sweep would repeat it (I58), so it is skipped."""

    async def episode(beats, stages, phase="post-job"):
        g = rig["g"]
        g.tray_down_episode_end()
        g.mechanism.mesh_reset_failed_since_release = False
        g.mechanism._reset_record_cache = (health_dir() / RESET_RECORD_FILE, {"boot_id": "", "failed": []})
        rig["clock"][0] = 0
        rig["cycled_at"].clear()
        rig["beats"] = beats
        for stage in stages:
            if rig["cycled_at"]:
                break
            await _gate(rig, stage, phase=phase)
        return rig["cycled_at"][0]

    climb = (galaxy.STAGE_SMI_RESET, galaxy.STAGE_POWER_CYCLE)
    whole_tray = await episode(_beats_without(_off(rig["map"], {3: 8})), climb)
    all_off_startup = await episode({}, climb, phase="startup")
    all_off_run_time = await episode({}, climb)
    part_tray = await episode(_beats_without(_off(rig["map"], {3: 5})), climb)
    assert (whole_tray, all_off_startup, all_off_run_time, part_tray) == (0, RESET_SEC, RESET_SEC, RESET_SEC)


# ---- across a broker restart (spec 04 I58) ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["startup", "post-job"])
async def test_replay_all_off_after_a_restart_cycles_with_no_second_reset(rig, monkeypatch, phase):
    """G6/G8: a mesh reset failed on the all-off mesh, then the broker restarted. Neither the startup
    gate nor a later gate runs that reset again: the power cycle follows at once."""
    g = rig["g"]
    rig["beats"] = {}
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert rig["order"][:1] == ["glx_reset"], "a first-seen all-off mesh at run time gets its one reset (I37)"
    rig["order"].clear()
    rig["cycled_at"].clear()
    # The restart: memory goes, the record on disk stays.
    g.mechanism.mesh_reset_failed_since_release = False
    g.mechanism._reset_record_cache = None
    g.tray_down_episode_end()
    await _gate(rig, galaxy.STAGE_SMI_RESET, phase=phase)
    assert rig["order"] == ["power_cycle"], "no second reset on the same all-off mesh"


# ---- class A with no tray map (a broker that never read a full mesh) -----------------------------

# tt-smi's UBB bus groups, one per tray (BH: tray 1 0x00, 2 0x40, 3 0xC0, 4 0x80).
UBB_TABLES = {
    "tt-galaxy-bh": {1: 0x00, 2: 0x40, 3: 0xC0, 4: 0x80},
    "tt-galaxy-wh": {1: 0xC0, 2: 0x80, 3: 0x00, 4: 0x40},
}
GROUPS = {0x00, 0x40, 0x80, 0xC0}


def _present(groups: dict) -> dict:
    """``{chip id: bus}`` for ``{group: n present}``, chips numbered in order as a short boot does."""
    buses = [g + 1 + k for g, n in sorted(groups.items()) for k in range(n)]
    return dict(enumerate(buses))


def test_whole_trays_absent_reads_class_a_from_bus_presence_alone():
    def absent(groups, off, expected=EXPECTED, tray_groups=GROUPS):
        present = _present(groups)
        return galaxy._whole_trays_absent(present, expected, set(range(len(present), len(present) + off)), tray_groups)

    assert absent({0x00: 8, 0x40: 8, 0x80: 8}, 8), "one tray (0xC0) with no chip on the bus"
    assert absent({0x40: 8}, 24), "three trays off, one still on"
    assert not absent({0x00: 8, 0x40: 8, 0x80: 7}, 9), "a tray with a chip missing is not whole"
    assert not absent({0x00: 8, 0x40: 8, 0x80: 8}, 9), "a stray off-bus chip the empty tray does not explain"
    assert not absent({0x00: 8, 0x40: 8, 0x80: 8, 0xC0: 8}, 8), "every tray full: off-bus chips unexplained"
    assert not absent({0x00: 8, 0x40: 8, 0x80: 8}, 8, tray_groups=()), "no tt-smi tables, no class"
    assert not absent({0x00: 8, 0x40: 8, 0x80: 8}, 8, expected=24), "not a four-tray host"
    assert not galaxy._whole_trays_absent({0: 0x01, 1: 0x21}, EXPECTED, set(range(2, 32)), GROUPS), "unknown group"
    assert not galaxy._whole_trays_absent({}, EXPECTED, set(range(32)), GROUPS), "no chip on the bus is class C's"
    assert not galaxy._whole_trays_absent({0: None}, EXPECTED, set(range(1, 32)), GROUPS)


def test_whole_trays_absent_needs_the_off_bus_ids_to_be_the_missing_ones():
    """Spec 04 I37 id-set match: matching counts are not enough. A heartbeat read and a sysfs read that raced
    can name different chips; the presence read then says nothing and the ladder decides."""
    present = _present({0x00: 8, 0x40: 8, 0x80: 8})  # chips 0-23 on the bus
    assert galaxy._whole_trays_absent(present, EXPECTED, {str(i) for i in range(24, 32)}, GROUPS)
    raced = {str(i) for i in range(23, 31)}  # 8 ids, but 23 is present and 31 is not named
    assert not galaxy._whole_trays_absent(present, EXPECTED, raced, GROUPS)
    assert galaxy._absent_tray_groups(present, EXPECTED, set(range(24, 32)), GROUPS) == [0xC0]


def test_the_class_reads_presence_only_with_no_map():
    off24 = set(range(24, 32))
    present = _present({0x00: 8, 0x40: 8, 0x80: 8})
    cls = galaxy._power_cycle_direct_class
    assert cls(off24, EXPECTED, None, mesh_reset_failed=False, present_buses=present, tray_groups=GROUPS) == "A"
    assert cls(off24, EXPECTED, None, mesh_reset_failed=False) is None, "no map and no presence read"
    partial = _present({0x00: 8, 0x40: 8, 0x80: 7, 0xC0: 1})
    assert cls(off24, EXPECTED, None, mesh_reset_failed=False, present_buses=partial, tray_groups=GROUPS) is None


@pytest.fixture
def short_boot(rig, monkeypatch):
    """The broker came up on 24 of 32 chips: no full read, so no tray map is banked (I16), and the
    24 chips on the bus fill trays 1, 2 and 4."""
    monkeypatch.setattr(rig["g"], "_tray_map_now", lambda: None)
    monkeypatch.setattr(galaxy, "_ubb_bus_id_tables", lambda: UBB_TABLES)
    rig["present"] = _present({0x00: 8, 0x40: 8, 0x80: 8})
    monkeypatch.setattr(galaxy, "chip_pci_bdfs", lambda: {i: f"0000:{b:02x}:00.0" for i, b in rig["present"].items()})
    rig["link_down"] = set()
    monkeypatch.setattr(galaxy, "link_down", lambda bdf: bdf in rig["link_down"])
    rig["beats"] = _beats_without(set(range(24, 32)))
    return rig


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["startup", "post-job"])
async def test_a_whole_tray_drop_with_no_map_cycles_at_the_first_gate_pass(short_boot, phase):
    """Spec 04 I37: below the reset floor the router names the tray rung; with no banked map the
    class is read from the buses on the bus, so the cycle is requested now, not after the 600 s
    ceiling and a mesh reset that has never healed a whole tray."""
    rig = short_boot
    assert await _gate(rig, galaxy.BELOW_FLOOR, phase=phase) == galaxy.OUTCOME_WAITING
    assert "glx_reset" not in rig["order"] and "ubb_tray" not in rig["order"] and "sweep" not in rig["order"]
    assert rig["order"][-1] == "power_cycle"
    ((_, fired),) = [(k, f) for k, f in rig["events"] if k == "power_cycle_direct"]
    assert fired["fault_class"] == galaxy.FAULT_CLASS_WHOLE_TRAY and fired["phase"] == "firing"
    assert "released" not in rig["order"], "the boot after the cycle runs the full startup check"


@pytest.mark.asyncio
async def test_the_hold_deadline_climb_with_no_map_cycles_with_no_reset(short_boot):
    rig = short_boot
    assert await rig["g"].escalate("offbus-forced", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert "glx_reset" not in rig["order"] and rig["order"][-1] == "power_cycle"


@pytest.mark.asyncio
async def test_a_partial_drop_with_no_map_keeps_the_ladder(short_boot):
    rig = short_boot
    rig["present"] = _present({0x00: 8, 0x40: 8, 0x80: 7, 0xC0: 1})
    await _gate(rig, galaxy.BELOW_FLOOR, phase="startup")
    assert "power_cycle" not in rig["order"] and "power_cycle_direct" not in _kinds(rig)


@pytest.mark.asyncio
async def test_inside_the_cooldown_the_deadline_shows_at_once_and_no_reset_runs(short_boot, monkeypatch):
    """Spec 04 I65 unchanged: the cooldown refuses the cycle for a bounded wait; /health shows the
    deadline from the first gate pass, the watchdog re-asks then, and nothing resets meanwhile."""
    import time
    from datetime import datetime

    rig, g = short_boot, short_boot["g"]
    until = time.time() + 1800
    monkeypatch.setattr(g.mechanism, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(g.mechanism, "power_cycle_retry", None)
    cooldown = {"until": until, "last": until - 3600, "cause": "host boot", "interval_sec": 3600, "why": "cooldown"}
    monkeypatch.setattr(
        g.mechanism, "auto_recovery_allowed", lambda action, **k: g.mechanism._deny_power_cycle(dict(cooldown))
    )
    assert await _gate(rig, galaxy.BELOW_FLOOR, phase="startup") == galaxy.OUTCOME_WAITING
    assert rig["order"][-1] == "denied"
    assert not {"glx_reset", "ubb_tray", "sweep", "power_cycle"} & set(rig["order"]), "no reset meanwhile"
    assert g.mechanism.power_cycle_retry[0] == until
    monkeypatch.setattr(srv, "recovery_mechanism", g.mechanism)
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    shown = srv._health_payload()["power_cycle_cooldown"]["next_deadline"]
    assert shown == datetime.fromtimestamp(until).isoformat(timespec="seconds")
    note = srv._hold_escalation_note(8)
    assert datetime.fromtimestamp(until).strftime("%H:%M:%S") in note and "~2 min" not in note


# ---- class A with unreadable survivors (spec 04 I37) --------------------------------------------


def _unreadable(monkeypatch, chips):
    """These chips are enumerated and bound but their heartbeat read fails (EIO)."""
    chips = {str(c) for c in chips}
    monkeypatch.setattr(galaxy, "unreadable_on_bus_chips", lambda ids: chips & {str(i) for i in ids})


@pytest.mark.asyncio
async def test_a_whole_tray_with_eio_survivors_cycles_with_the_map(rig, monkeypatch):
    """A survivor whose read fails with EIO counts off the bus by the heartbeat; the drop then reads
    as a whole tray plus a stray chip (the ladder). Taken as on the bus, it is class A."""
    survivor = sorted(rig["map"][1])[0]
    rig["beats"] = _beats_without(_off(rig["map"], {3: 8}) | {survivor})
    _unreadable(monkeypatch, {survivor})
    assert await _gate(rig, galaxy.STAGE_SMI_RESET) == galaxy.OUTCOME_WAITING
    assert "glx_reset" not in rig["order"] and rig["order"][-1] == "power_cycle"
    ((_, ev),) = [(k, f) for k, f in rig["events"] if k == "class_a_unreadable_survivors"]
    assert ev["unreadable"] == [str(survivor)] and len(ev["off_bus"]) == 8


@pytest.mark.asyncio
async def test_a_whole_tray_with_eio_survivors_cycles_with_no_map(short_boot, monkeypatch):
    rig = short_boot
    rig["beats"] = _beats_without(set(range(24, 32)) | {0})  # chip 0's node is there, its read fails
    _unreadable(monkeypatch, {0})
    assert await _gate(rig, galaxy.BELOW_FLOOR, phase="startup") == galaxy.OUTCOME_WAITING
    assert not {"glx_reset", "ubb_tray", "sweep"} & set(rig["order"]) and rig["order"][-1] == "power_cycle"


@pytest.mark.asyncio
async def test_a_no_map_tray_of_lingering_nodes_is_class_a(short_boot):
    """Chips the heartbeat calls off the bus whose nodes the kernel still lists (all-ones endpoints)
    cannot fill the tray they left."""
    rig = short_boot
    rig["present"] = _present({0x00: 8, 0x40: 8, 0x80: 8, 0xC0: 8})
    assert await _gate(rig, galaxy.BELOW_FLOOR, phase="startup") == galaxy.OUTCOME_WAITING
    assert "glx_reset" not in rig["order"] and rig["order"][-1] == "power_cycle"


@pytest.mark.asyncio
async def test_all_chips_unreadable_is_not_class_a(rig, monkeypatch):
    """Every chip bound but unreadable leaves nothing off the bus to be a tray: the all-off path
    (one mesh reset, then class C) is unchanged."""
    rig["beats"] = {}
    _unreadable(monkeypatch, range(EXPECTED))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert "glx_reset" in rig["order"] and "class_a_unreadable_survivors" not in _kinds(rig)


@pytest.mark.asyncio
async def test_a_partial_drop_with_eio_survivors_keeps_the_ladder(rig, monkeypatch):
    survivor = sorted(rig["map"][1])[0]
    rig["beats"] = _beats_without(_off(rig["map"], {3: 4}) | {survivor})
    _unreadable(monkeypatch, {survivor})
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert "power_cycle_direct" not in _kinds(rig) and "class_a_unreadable_survivors" not in _kinds(rig)


def _lingering_link_down(monkeypatch, tmp_path, *, gone=(), eio=(), down=()):
    """Real sysfs reads: ``gone`` chips have no node, ``eio`` chips are bound with an EIO heartbeat
    read, and ``down`` of them still listed with their link down (half by speed, half by width)."""
    down = sorted(down)
    _fake_sysfs(
        monkeypatch, tmp_path, missing=set(gone), eio=set(eio), speed_unknown=set(down[::2]), width_63=set(down[1::2])
    )
    monkeypatch.setattr(galaxy, "unreadable_on_bus_chips", heartbeat.unreadable_on_bus_chips)


@pytest.mark.asyncio
async def test_lingering_link_down_nodes_leave_an_all_off_bus_its_mesh_reset(rig, monkeypatch, tmp_path):
    """Spec 03 I9 and 04 I37: one tray's nodes are gone and the other trays' linger, bound, with an EIO
    read and the link down. Every chip is off the bus, so the one mesh reset still runs; the lingering
    nodes are not survivors that make the drop look like class A."""
    gone = set(rig["map"][max(rig["map"])])
    lingering = set(range(EXPECTED)) - gone
    _lingering_link_down(monkeypatch, tmp_path, gone=gone, eio=lingering, down=lingering)
    rig["beats"] = {}
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert "glx_reset" in rig["order"]
    assert "class_a_unreadable_survivors" not in _kinds(rig) and "power_cycle_direct" not in _kinds(rig)


@pytest.mark.asyncio
async def test_a_tray_of_link_down_nodes_with_one_eio_survivor_is_class_a(rig, monkeypatch, tmp_path):
    """A whole tray lingers with its links down and one chip elsewhere reads EIO with its link up: only
    that chip is on the bus, the tray is off, and the drop is class A (straight to the power cycle)."""
    tray = set(rig["map"][3])
    survivor = sorted(rig["map"][1])[0]
    _lingering_link_down(monkeypatch, tmp_path, eio=tray | {survivor}, down=tray)
    rig["beats"] = _beats_without(tray | {survivor})
    assert await _gate(rig, galaxy.STAGE_SMI_RESET) == galaxy.OUTCOME_WAITING
    assert "glx_reset" not in rig["order"] and rig["order"][-1] == "power_cycle"
    ((_, ev),) = [(k, f) for k, f in rig["events"] if k == "class_a_unreadable_survivors"]
    assert ev["unreadable"] == [str(survivor)] and ev["off_bus"] == sorted(map(str, tray), key=int)


@pytest.mark.asyncio
async def test_an_enodata_tray_reading_ffff_is_class_a(rig, monkeypatch, tmp_path):
    """Spec 04 I24: a tray that lost power right after a reset reads ENODATA on bound endpoints with
    their links up. Its config space reads 0xffff, so it is off the bus, not ARC-not-ready: class A,
    straight to the power cycle with no mesh reset."""
    tray = set(rig["map"][3])
    _fake_sysfs(monkeypatch, tmp_path, enodata=tray, vendor_ffff=tray)
    monkeypatch.setattr(galaxy, "unreadable_on_bus_chips", heartbeat.unreadable_on_bus_chips)
    monkeypatch.setattr(heartbeat.time, "sleep", lambda s: None)
    rig["beats"] = heartbeat.read_heartbeats()
    assert heartbeat.arc_not_ready_chips(rig["beats"]) == set()
    assert await _gate(rig, galaxy.STAGE_SMI_RESET) == galaxy.OUTCOME_WAITING
    assert "glx_reset" not in rig["order"] and rig["order"][-1] == "power_cycle"
    assert "released" not in rig["order"]


@pytest.mark.asyncio
async def test_an_enodata_tray_with_unreadable_config_never_lifts(rig, monkeypatch, tmp_path):
    """The same tray with config space that cannot be read: no evidence it is gone, none that it is
    there. It is never ARC-not-ready, every rung counts it off the bus, and the hold never lifts."""
    tray = set(rig["map"][3])
    _fake_sysfs(monkeypatch, tmp_path, enodata=tray, config_unreadable=tray)
    monkeypatch.setattr(galaxy, "unreadable_on_bus_chips", heartbeat.unreadable_on_bus_chips)
    monkeypatch.setattr(heartbeat.time, "sleep", lambda s: None)
    rig["beats"] = heartbeat.read_heartbeats()
    assert heartbeat.arc_not_ready_chips(rig["beats"]) == set()
    assert galaxy._offbus_chip_ids(rig["beats"], EXPECTED) == {str(c) for c in tray}
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert "released" not in rig["order"]
    assert galaxy.offbus_count(rig["beats"], EXPECTED) == len(tray)


# ---- the whole-tray check with no map (Part 2 of spec 04 I37) ------------------------------------------


@pytest.fixture
def real_tray_rung(short_boot, monkeypatch):
    from tt_device_mcp.health.recovery import offbus_power_cycle

    g = short_boot["g"]
    monkeypatch.setattr(offbus_power_cycle, "_pending", {})
    monkeypatch.setattr(g.deps, "device_hold_episode_since", lambda: "2026-10-16T12:39:00")

    def rung(beats, off_bus):
        return type(g)._note_whole_tray_drop(g, beats, off_bus, EXPECTED, lambda m: None)

    return short_boot, rung, offbus_power_cycle


@pytest.mark.asyncio
async def test_the_whole_tray_check_with_no_map_hands_a_whole_tray_to_the_power_cycle(real_tray_rung):
    rig, rung, opc = real_tray_rung
    assert rung(rig["beats"], 8) is True
    assert opc.whole_tray_down_age_sec("2026-10-16T12:39:00") is not None
    ((_, ev),) = [(k, f) for k, f in rig["events"] if k == "whole_tray_down_power_cycle"]
    assert ev["trays"] == ["bus group 0xc0"]


@pytest.mark.asyncio
async def test_the_whole_tray_check_with_no_map_and_eio_survivors_hands_off_too(real_tray_rung, monkeypatch):
    rig, rung, opc = real_tray_rung
    rig["beats"] = _beats_without(set(range(24, 32)) | {0})
    _unreadable(monkeypatch, {0})
    assert rung(rig["beats"], 9) is True
    assert opc.whole_tray_down_age_sec("2026-10-16T12:39:00") is not None


@pytest.mark.asyncio
async def test_the_whole_tray_check_with_no_map_keeps_a_partial_drop_on_the_ladder(real_tray_rung):
    rig, rung, opc = real_tray_rung
    rig["present"] = _present({0x00: 8, 0x40: 8, 0x80: 7, 0xC0: 1})
    assert rung(rig["beats"], 8) is False
    assert opc.whole_tray_down_age_sec("2026-10-16T12:39:00") is None
    assert "whole_tray_down_power_cycle" not in _kinds(rig)


@pytest.mark.asyncio
async def test_a_listed_node_whose_link_is_down_does_not_fill_a_tray(short_boot):
    """Spec 03 I9 link-down: a node the kernel still lists with its link down (speed Unknown, width 63) is not on
    the bus. Chips 0-7's heartbeats read, but their tray shows one link down: not whole trays."""
    rig = short_boot
    rig["link_down"] = {f"0000:{rig['present'][0]:02x}:00.0"}
    present_buses, _ = rig["g"]._presence_read(set(range(24, 32)), EXPECTED)
    assert 0 not in present_buses and 1 in present_buses
    await _gate(rig, galaxy.BELOW_FLOOR, phase="startup")
    assert "power_cycle" not in rig["order"], "the read does not add up to whole trays: the ladder decides"


# ---- a failed reset's cooldown never delays the class A/C power cycle (spec 04 I3) ---------------
#
# The cooldown WAIT returned before the off-bus class was read, so a whole tray off the bus after a
# failed operator reset waited for the forced ladder at the hold bound instead of its power cycle.


async def _gate_in_cooldown(rig, monkeypatch, tmp_path, off: set, argv=("tt-smi", "-glx_reset")):
    """A failed operator reset arms the cooldown, then a post-job gate runs end to end on ``off``.
    An all-off set keeps its device nodes (lingering): with none at all the gate holds and leaves the
    climb to the idle off-bus escalation."""
    (tmp_path / "dev").mkdir()
    for i in range(EXPECTED):
        if i not in off or len(off) == EXPECTED:
            (tmp_path / "dev" / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path / "dev"))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    srv.device_op_lock = None
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: EXPECTED)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "device_pci_map", {})
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(rig["g"].mechanism, "last_reset_failed", False)
    monkeypatch.setattr(rig["g"].mechanism, "last_reset_monotonic", 0.0)
    fsm_dirty(srv, "", why="gate_error")

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": "chips off the bus"}}

    patch_recovery(monkeypatch, "_verify_device", unhealthy)
    rungs = []
    monkeypatch.setattr(
        galaxy.GalaxyRecovery, "_fire_gate_rung", lambda self, gate_phase, stage, *a, **k: _record(rungs, stage)
    )
    rig["beats"] = _beats_without(off)
    rig["g"]._td_reset_seen = True  # the off set follows a reset: no tray-down onset
    srv._note_operator_reset_outcome(1, None, srv.time.monotonic(), list(argv))
    assert rig["g"].mechanism.cooling(), "the failed operator reset armed the cooldown"

    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    return rungs


def _no_release(rig):
    """Goal 11: nothing here passed a full check, fabric included, so nothing is released."""
    assert "released" not in rig["order"]
    assert srv.fsm.state is not srv.ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [{3: 8}, {1: 8, 4: 8}])
async def test_a_whole_tray_in_a_failed_resets_cooldown_cycles_on_the_next_gate_pass(
    rig, monkeypatch, tmp_path, clear_job_state, spec
):
    rungs = await _gate_in_cooldown(rig, monkeypatch, tmp_path, _off(rig["map"], spec))
    assert rig["order"] == ["power_cycle"], "no reset, no sweep, no wait for the forced ladder"
    assert rungs == []
    ((_, fired),) = [(k, f) for k, f in rig["events"] if k == "power_cycle_direct"]
    assert fired["fault_class"] == galaxy.FAULT_CLASS_WHOLE_TRAY
    assert "reset_suppressed_cooldown" not in _kinds(rig)
    _no_release(rig)


@pytest.mark.asyncio
async def test_all_off_after_a_failed_operator_mesh_reset_cycles_in_its_cooldown(
    rig, monkeypatch, tmp_path, clear_job_state
):
    """The failed operator -glx_reset was the episode's one mesh reset: class C, no second one."""
    rungs = await _gate_in_cooldown(rig, monkeypatch, tmp_path, set(range(EXPECTED)))
    assert rig["order"] == ["power_cycle"] and rungs == []
    ((_, fired),) = [(k, f) for k, f in rig["events"] if k == "power_cycle_direct"]
    assert fired["fault_class"] == galaxy.FAULT_CLASS_ALL_OFF
    _no_release(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "off, argv",
    [
        (lambda m: _off(m, {3: 4}), ("tt-smi", "-glx_reset")),  # part of a tray: the ladder
        (lambda m: set(), ("tt-smi", "-glx_reset")),  # a present mesh: the 120 s grace
        (lambda m: set(range(EXPECTED)), ("tt-smi", "-r", "0")),  # all off, no mesh reset spent yet
    ],
    ids=["partial", "present", "all-off-no-mesh-reset"],
)
async def test_any_other_set_still_waits_out_the_cooldown(rig, monkeypatch, tmp_path, clear_job_state, off, argv):
    rungs = await _gate_in_cooldown(rig, monkeypatch, tmp_path, off(rig["map"]), argv=argv)
    assert rig["order"] == [] and rungs == [], "no reset, no power cycle inside the cooldown"
    assert "reset_suppressed_cooldown" in _kinds(rig)
    assert "power_cycle_direct" not in _kinds(rig)
    _no_release(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [{2: 8}, None], ids=["whole-tray", "all-off"])
async def test_an_idle_off_bus_hold_in_a_failed_resets_cooldown_cycles(rig, monkeypatch, spec):
    """The idle off-bus escalation reads the class before its own cooldown check: a whole tray is
    class A, and every chip off after the failed operator mesh reset is class C."""
    ladders = []
    monkeypatch.setattr(rig["g"], "_escalate_offbus_stuck_hold", lambda *a, **k: _record(ladders, "offbus"))
    monkeypatch.setattr(rig["g"].mechanism, "last_reset_monotonic", 0.0)
    srv._note_operator_reset_outcome(1, None, srv.time.monotonic(), ["tt-smi", "-glx_reset"])
    assert rig["g"].mechanism.cooling()
    rig["beats"] = _beats_without(_off(rig["map"], spec)) if spec else {}
    rig["g"]._td_reset_seen = True
    assert await rig["g"].escalate("offbus", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["power_cycle"] and ladders == []
    assert "released" not in rig["order"]
