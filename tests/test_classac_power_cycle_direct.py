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

from types import SimpleNamespace

import pytest

from tests.conftest import patch_health_event
from tests.test_startup_fabric_hugepages import _shortfalls, _startup  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import galaxy

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
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: False)
    monkeypatch.setattr(g.mechanism, "auto_recovery_allowed", lambda action, tenant_active=False, **k: state["allowed"])
    monkeypatch.setattr(g.mechanism, "_journal_auto_recovery_denied", lambda *a, **k: order.append("denied"))
    monkeypatch.setattr(g.mechanism, "mesh_reset_failed_since_release", False)
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
        return False

    async def sweep(bitmap, ids, trays, expected, log, do_sbr=True):
        order.append("sweep")
        state["clock"][0] += SWEEP_SEC
        return True

    async def verify(expected, log, run_fabric=True, **k):
        return False, {}

    monkeypatch.setattr(g, "_reset_and_verify_device", mesh_reset)
    monkeypatch.setattr(g, "_issue_all_resets_back_to_back", sweep)
    monkeypatch.setattr(g, "_verify_device", verify)
    monkeypatch.setattr(g, "_attempt_ubb_tray_reset", lambda *a, **k: _record(order, "ubb_tray"))
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
    out = await rig["g"].escalate(phase, [], EXPECTED, lambda m: None)
    assert rig["order"] == (["power_cycle"] if cycled else [])
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
async def test_a_partial_drop_keeps_the_ladder(rig, spec, prelude):
    rig["beats"] = _beats_without(_off(rig["map"], spec))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    await _gate(rig, galaxy.STAGE_POWER_CYCLE)
    assert rig["order"] == [*prelude, "glx_reset", "sweep", "power_cycle"]
    assert "power_cycle_direct" not in _kinds(rig) and SKIPPED not in _kinds(rig)


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
    Class A: at once. Class C, at startup as at run time: after its one reset. The
    partial-tray ladder (B2, unchanged) for comparison: after the reset and the sweep."""

    async def episode(beats, stages, phase="post-job"):
        g = rig["g"]
        g.tray_down_episode_end()
        g.mechanism.mesh_reset_failed_since_release = False
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
    assert (whole_tray, all_off_startup, all_off_run_time, part_tray) == (
        0,
        RESET_SEC,
        RESET_SEC,
        RESET_SEC + SWEEP_SEC,
    )
