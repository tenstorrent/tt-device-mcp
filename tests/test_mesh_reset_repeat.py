# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I93: one capped, spaced, guarded repeat of a failed Galaxy mesh reset per hold episode.

The repeat runs at most once, no sooner than MESH_RESET_REPEAT_MIN_GAP_SEC after the first reset
ended, only when it and its full check can end before the 10-min hold bound, only with every chip on
the bus, only behind the BMC op_mode guard and preflight (spec 04 I91), never under a tenant, and
only after non-tenant readers of the device are stopped. The full check runs after it; only a pass
lifts the hold, and a failed one climbs (the last-chance sweep, then the power cycle)."""

import inspect
import signal

import pytest

from tests.test_dispatch_failure_reset import CHIPS, _gate, mesh  # noqa: F401 - the shared fixture
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.recovery import galaxy, mesh_repeat, pcie_guard

INDICES = list(range(CHIPS))


def _age_first_reset(seconds: float) -> None:
    run = srv.recovery_mechanism.last_mesh_reset_run
    assert run is not None, "the first mesh reset was recorded"
    run["started"] -= seconds
    run["ended"] -= seconds


async def _repeat():
    return await srv.galaxy_recovery._mesh_reset_repeat(INDICES, CHIPS, srv.device_hold_episode_since, srv.logger.info)


async def _first_reset_failed(mesh):  # noqa: F811
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]]
    assert srv.fsm.state is ServerState.RECOVERING


# --- the clock plan --------------------------------------------------------------------------------


def test_the_plan_allows_one_repeat_and_never_a_second():
    assert mesh_repeat.MESH_RESET_REPEAT_CAP == 1
    due = mesh_repeat.plan(repeats_used=0, hold_age_sec=250, since_first_end_sec=210, first_reset_sec=60)
    assert due[0] == "due"
    assert mesh_repeat.plan(repeats_used=1, hold_age_sec=250, since_first_end_sec=210, first_reset_sec=60)[:2] == (
        "skip",
        "cap_reached",
    )


def test_the_plan_waits_out_the_min_gap_after_the_first_reset_ends():
    verdict, why, left = mesh_repeat.plan(repeats_used=0, hold_age_sec=60, since_first_end_sec=50, first_reset_sec=60)
    assert (verdict, why) == ("wait", "min_gap") and left == pytest.approx(150)


@pytest.mark.parametrize("age", [0, 100, 200, 359, 360, 420, 599, 600, 900])
@pytest.mark.parametrize("first", [None, 30, 60, 120, 263, 400])
def test_a_planned_repeat_always_ends_before_the_hold_bound(age, first):
    """Spec 04 I76 is never pushed later: a repeat that is due or waiting ends, with its full check,
    before the 10-min bound (its reset leg at least the first reset's measured time)."""
    for since in (0, 100, 199, 200, 300):
        verdict, _, left = mesh_repeat.plan(
            repeats_used=0, hold_age_sec=age, since_first_end_sec=since, first_reset_sec=first
        )
        if verdict != "skip":
            assert age + left + mesh_repeat.repeat_cost_sec(first) <= mesh_repeat.HOLD_BOUND_SEC


def test_an_unknown_first_reset_time_sizes_the_repeat_at_the_worst_case():
    assert mesh_repeat.repeat_cost_sec(None) == mesh_repeat.MESH_RESET_WORST_SEC + mesh_repeat.FULL_CHECK_WORST_SEC
    assert mesh_repeat.repeat_cost_sec(10) == mesh_repeat.MESH_RESET_FLOOR_SEC + mesh_repeat.FULL_CHECK_WORST_SEC
    assert mesh_repeat.repeat_cost_sec(9999) == mesh_repeat.MESH_RESET_WORST_SEC + mesh_repeat.FULL_CHECK_WORST_SEC


def test_no_env_or_config_knob_turns_the_repeat_off_or_changes_it():
    src = inspect.getsource(mesh_repeat) + inspect.getsource(galaxy.GalaxyRecovery._mesh_reset_repeat)
    assert "environ" not in src and "getenv" not in src and "TT_DEVICE_MCP_" not in src


# --- the broker path -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_first_reset_waits_then_repeats_once_and_never_a_third(mesh):  # noqa: F811
    await _first_reset_failed(mesh)
    assert "last_chance_reset_sweep" not in mesh["events"]
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    verdict, recovered = await _repeat()
    assert (verdict, recovered) == ("ran", False)
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]] * 2
    assert mesh["verifies"][-1] is True, "the full check (fabric included) ran after the repeat"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed check after the repeat keeps the hold"
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 2, "never a third mesh reset in the episode"


@pytest.mark.asyncio
async def test_a_failed_repeat_keeps_the_hold_then_climbs_to_the_power_cycle(mesh, armed_power_cycle):  # noqa: F811
    """The idle re-drive runs the repeat once the gap is out; its failed full check keeps the hold and the
    climb goes on (the kept last-chance sweep, then the power cycle) in the same pass."""
    await _first_reset_failed(mesh)
    assert armed_power_cycle == [] and "last_chance_reset_sweep" not in mesh["events"], "the repeat comes first"
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    assert await srv.galaxy_recovery._escalate_stuck_hold(INDICES, CHIPS, srv.logger.info) is True
    assert mesh["events"].count("mesh_reset_repeat") == 1
    assert mesh["verifies"].count(True) >= 2, "the full check ran after the repeat"
    assert mesh["events"].count("last_chance_reset_sweep") == 1
    assert armed_power_cycle == ["power-cycle"], "spent repeat: the climb goes on to the power cycle"
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_a_repeat_whose_full_check_passes_lifts_the_hold(mesh):  # noqa: F811
    await _first_reset_failed(mesh)
    mesh["reset_heals"] = True
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    assert await _repeat() == ("ran", True)
    assert mesh["verifies"][-1] is True and mesh["bad"] is False
    assert "mesh_reset_repeat" in mesh["events"]


@pytest.mark.asyncio
async def test_no_repeat_that_cannot_end_before_the_hold_bound(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(galaxy, "_hold_age_sec", lambda since: 400.0)
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1, "the 10-min ladder's mesh reset is the episode's second"
    assert "mesh_reset_repeat_skipped" in mesh["events"]


@pytest.mark.asyncio
async def test_no_repeat_without_the_bmc_op_mode_guard(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)

    async def refused():
        return "BMC reports op_mode 0x0 could be reached"

    monkeypatch.setattr(srv.recovery_mechanism, "bmc_preflight", refused)
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1


@pytest.mark.asyncio
async def test_no_repeat_after_a_bmc_guard_finding(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(srv.recovery_mechanism, "bmc_guard_state", {"kind": "fallback_seen"})
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["sudo", "tt-smi", "-glx_reset"],
        ["env", "X=1", "tt-smi", "-glx_reset"],
        ["python3", "-m", "tt_smi", "-glx_reset"],
    ],
)
async def test_a_repeat_under_an_override_reset_argv_is_still_guarded(mesh, monkeypatch, argv):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(srv.galaxy_recovery, "reset_argv", lambda indices: list(argv))
    preflights = []

    async def _preflight():
        preflights.append(True)
        return ""

    monkeypatch.setattr(srv.recovery_mechanism, "bmc_preflight", _preflight)
    assert (await _repeat())[0] == "ran"
    assert preflights, "the BMC preflight ran before the repeat"
    assert mesh["resets"][-1] == list(argv), "the repeat launched through the same reset backend"
    launched = srv.recovery_mechanism._launch_argv(list(argv))
    assert launched[0] == "unshare" and "--wrap" in launched, "and that backend runs it under the ipmitool guard"


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [["sudo", "tt-smi", "-glx_reset"], ["python3", "-m", "tt_smi", "-glx_reset"]])
async def test_a_repeat_under_an_override_reset_argv_is_refused_when_the_preflight_refuses(
    mesh, monkeypatch, argv  # noqa: F811
):
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(srv.galaxy_recovery, "reset_argv", lambda indices: list(argv))

    async def _refuse():
        return "op_mode 0x0 fallback possible"

    monkeypatch.setattr(srv.recovery_mechanism, "bmc_preflight", _refuse)
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1, "a reset the guard cannot vouch for never repeats"


@pytest.mark.asyncio
async def test_a_non_tenant_reader_is_stopped_then_the_repeat_runs(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    reader = DeviceHolder(pid=424242, uid=0)
    sent, alive = [], {424242}
    scans = iter([HolderScan(holders=[reader], complete=True)])
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: next(scans, HolderScan(holders=[], complete=True)))
    monkeypatch.setattr(galaxy, "_signal_pid", lambda pid, sig: sent.append((pid, sig)) or alive.discard(pid))
    monkeypatch.setattr(galaxy, "_pid_alive", lambda pid: pid in alive)
    assert (await _repeat())[0] == "ran"
    assert sent == [(424242, signal.SIGTERM)], "SIGTERM first; SIGKILL only if it outlives the grace"
    assert len(mesh["resets"]) == 2


@pytest.mark.asyncio
async def test_a_live_tenant_blocks_the_repeat(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    tenant = DeviceHolder(pid=434343, uid=5000)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[tenant], complete=True))
    monkeypatch.setattr(galaxy, "_signal_pid", lambda pid, sig: pytest.fail("a tenant is never signalled"))
    assert (await _repeat())[0] == "wait"
    assert "hold_bound_tenant_blocked" in mesh["events"]
    assert len(mesh["resets"]) == 1


@pytest.mark.asyncio
async def test_no_repeat_with_a_chip_off_the_bus(mesh, monkeypatch):  # noqa: F811
    """Whole-tray (A) and partial (B/C) drops never repeat: they keep their own rungs."""
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 8)
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1


@pytest.mark.asyncio
async def test_no_repeat_that_would_rescan_a_removed_chip(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(galaxy, "removed_chip_ids", lambda: {"0000:c1:00.0"})
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1
