# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I93: one capped, spaced, guarded repeat of a failed Galaxy mesh reset per hold episode.

The repeat runs at most once, no sooner than MESH_RESET_REPEAT_MIN_GAP_SEC after the first reset
ended, only when its worst case (the worst mesh reset plus a full check) still ends by the 10-min hold
bound, never for a fabric link-short hold, only with every chip on the bus, only behind the BMC op_mode guard and preflight (spec 04 I91), never under a tenant, and
only after non-tenant readers of the device are stopped. The full check runs after it; only a pass
lifts the hold, and a failed one climbs (the last-chance sweep, then the power cycle)."""

import inspect
import signal
import time
from datetime import datetime, timedelta

import pytest

from tests.test_dispatch_failure_reset import CHIPS, _gate, mesh  # noqa: F401 - the shared fixture
from tt_device_mcp import privileges
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import recovery as recovery_mod
from tt_device_mcp.health.recovery import galaxy, mesh_repeat, pcie_guard

INDICES = list(range(CHIPS))


def _age_first_reset(seconds: float) -> None:
    run = srv.recovery_mechanism.last_mesh_reset_run
    assert run is not None, "the first mesh reset was recorded"
    run["started"] -= seconds
    run["ended"] -= seconds


async def _repeat():
    return await srv.galaxy_recovery._mesh_reset_repeat(INDICES, CHIPS, srv.device_hold_episode_since, srv.logger.info)


def _arm_power_cycle(monkeypatch):
    """conftest's armed_power_cycle, armed after the first reset: armed from the start, the gate's own
    climb (no room for a repeat right after a reset inside the hold) would fire it at once."""
    fired = []
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(privileges, "can_ipmi", lambda: True)
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: fired.append("power-cycle"))
    return fired


async def _bound_tick(monkeypatch, mesh, age=mesh_repeat.HOLD_BOUND_SEC + 1):  # noqa: F811
    """The watchdog's tick with the wall clock moved ``age`` seconds past the hold's opening."""

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=age)

    monkeypatch.setattr(srv, "datetime", _Later)
    monkeypatch.setattr(galaxy, "datetime", _Later)
    for name, value in (
        ("device_hold_deadline_bucket", 0),
        ("device_hold_escalate_bucket", 0),
        ("device_hold_offbus_escalated", False),
        ("_forced_escalation_task", None),
    ):
        monkeypatch.setattr(srv, name, value)
    mesh["events"].clear()
    srv._check_hold_deadline()
    if srv._forced_escalation_task is not None:
        await srv._forced_escalation_task


async def _first_reset_failed(mesh):  # noqa: F811
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]]
    assert srv.fsm.state is ServerState.RECOVERING


# --- the clock plan --------------------------------------------------------------------------------


def test_the_plan_allows_one_repeat_and_never_a_second():
    assert mesh_repeat.MESH_RESET_REPEAT_CAP == 1
    due = mesh_repeat.plan(repeats_used=0, hold_age_sec=100, since_first_end_sec=210)
    assert due[0] == "due"
    assert mesh_repeat.plan(repeats_used=1, hold_age_sec=100, since_first_end_sec=210)[:2] == ("skip", "cap_reached")


def test_the_plan_waits_out_the_min_gap_after_the_first_reset_ends():
    verdict, why, left = mesh_repeat.plan(repeats_used=0, hold_age_sec=0, since_first_end_sec=50)
    assert (verdict, why) == ("wait", "min_gap") and left == pytest.approx(150)


@pytest.mark.parametrize("age", [0, 100, 156, 157, 158, 200, 359, 360, 420, 599, 600, 900])
def test_a_planned_repeat_ends_by_the_hold_bound_at_its_worst_case(age):
    """Spec 04 I76 is never pushed later: a repeat that is due or waiting ends by the 10-min bound even
    when its mesh reset and its full check both take their worst case, whatever the first reset took."""
    assert mesh_repeat.REPEAT_WORST_SEC == mesh_repeat.MESH_RESET_WORST_SEC + mesh_repeat.FULL_CHECK_WORST_SEC
    for since in (0, 50, 100, 199, 200, 300):
        verdict, why, left = mesh_repeat.plan(repeats_used=0, hold_age_sec=age, since_first_end_sec=since)
        if verdict == "skip":
            assert why == "cannot_end_before_bound"
        else:
            assert age + left + mesh_repeat.REPEAT_WORST_SEC <= mesh_repeat.HOLD_BOUND_SEC


def test_a_fast_first_reset_does_not_shrink_the_repeats_admission():
    """A 10 s first reset once sized the repeat at a 60 s reset leg: admitted at hold age 360 s, a slow
    repeat then ran past 600 s. The worst case decides now."""
    assert mesh_repeat.plan(repeats_used=0, hold_age_sec=360, since_first_end_sec=210)[:2] == (
        "skip",
        "cannot_end_before_bound",
    )
    assert mesh_repeat.plan(repeats_used=0, hold_age_sec=157, since_first_end_sec=210)[0] == "due"
    assert mesh_repeat.plan(repeats_used=0, hold_age_sec=158, since_first_end_sec=210)[0] == "skip"


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
async def test_a_failed_repeat_keeps_the_hold_then_climbs_to_the_power_cycle(mesh, monkeypatch):  # noqa: F811
    """The idle re-drive runs the repeat once the gap is out; its failed full check keeps the hold and the
    climb goes on (the kept last-chance sweep, then the power cycle) in the same pass."""
    await _first_reset_failed(mesh)
    fired = _arm_power_cycle(monkeypatch)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    assert await srv.galaxy_recovery._escalate_stuck_hold(INDICES, CHIPS, srv.logger.info) is True
    assert mesh["events"].count("mesh_reset_repeat") == 1
    assert mesh["verifies"].count(True) >= 2, "the full check ran after the repeat"
    assert mesh["events"].count("last_chance_reset_sweep") == 1
    assert fired == ["power-cycle"], "spent repeat: the climb goes on to the power cycle"
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_a_failed_first_reset_inside_the_hold_climbs_at_once(mesh, armed_power_cycle):  # noqa: F811
    """A reset that ran inside the hold leaves no room: its gap plus a worst-case repeat ends past 600 s,
    so the gate climbs at once (the last-chance sweep, then the power cycle) and journals why."""
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    await _gate()
    assert "mesh_reset_repeat" not in mesh["events"]
    assert "mesh_reset_repeat_skipped" in mesh["events"]
    assert "cannot_end_before_bound" in mesh["log"].text()
    assert mesh["events"].count("last_chance_reset_sweep") == 1
    assert armed_power_cycle == ["power-cycle"]


@pytest.mark.asyncio
async def test_a_repeat_whose_full_check_passes_lifts_the_hold(mesh):  # noqa: F811
    await _first_reset_failed(mesh)
    mesh["reset_heals"] = True
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    assert await _repeat() == ("ran", True)
    assert mesh["verifies"][-1] is True and mesh["bad"] is False
    assert "mesh_reset_repeat" in mesh["events"]


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [158.0, 360.0, 400.0])
async def test_no_repeat_that_cannot_end_before_the_hold_bound(mesh, monkeypatch, age):  # noqa: F811
    """Whatever the first reset took (here a fast one), a repeat whose worst case ends past 600 s never
    starts: a mesh reset is never killed mid-flight, so it could not be stopped at the bound."""
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    run = srv.recovery_mechanism.last_mesh_reset_run
    run["started"] = run["ended"] - 10
    monkeypatch.setattr(galaxy, "_hold_age_sec", lambda since: age)
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1, "the 10-min ladder's mesh reset is the episode's second"
    assert "mesh_reset_repeat_skipped" in mesh["events"]


@pytest.mark.asyncio
async def test_the_bound_is_rechecked_at_launch(mesh, monkeypatch):  # noqa: F811
    """The preflight, holder scans and reader eviction take time: a repeat admitted at 150 s whose launch
    comes at 170 s no longer fits and does not start."""
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    ages = iter([150.0, 170.0])
    monkeypatch.setattr(galaxy, "_hold_age_sec", lambda since: next(ages))
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1
    assert "cannot_end_before_bound" in mesh["log"].text()


@pytest.mark.asyncio
async def test_no_repeat_for_a_fabric_link_short_hold(mesh, monkeypatch):  # noqa: F811
    """Spec 04 I79/I83: a lost link, a latched topology mismatch or missing ports gets one ladder pass
    and then the power cycle at once; a mesh reset does not heal it, so no repeat delays the cycle."""
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(srv, "_fabric_link_short_hold", lambda: "topology mismatch")
    assert (await _repeat())[0] == "skip"
    assert len(mesh["resets"]) == 1
    assert "mesh_reset_repeat_skipped" in mesh["events"]
    assert "fabric_link_short (topology mismatch)" in mesh["log"].text()


# --- the 10-min bound (spec 04 I76) whatever the repeat's state --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", ["pending", "failed"])
async def test_the_bound_runs_the_ladder_and_asks_for_the_power_cycle_at_600s(mesh, monkeypatch, repeat):  # noqa: F811
    await _first_reset_failed(mesh)
    if repeat == "pending":
        _age_first_reset(100)
        assert await _repeat() == ("wait", None), "the repeat waits out its gap"
    else:
        _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
        assert await _repeat() == ("ran", False)
    fired = _arm_power_cycle(monkeypatch)
    resets = len(mesh["resets"])
    await _bound_tick(monkeypatch, mesh)
    assert "hold_deadline_forced_escalation" in mesh["events"], "the bound's ladder runs at 600 s"
    assert "mesh_reset_repeat" not in mesh["events"], "no repeat at or past the bound"
    assert mesh["events"].count("last_chance_reset_sweep") == 1
    assert len(mesh["resets"]) == resets + 1, "the sweep's mesh reset only"
    assert fired == ["power-cycle"], "the full check still fails: the power cycle is asked"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_slow_running_repeat_takes_no_77_retry_past_the_bound(mesh, monkeypatch):  # noqa: F811
    """Admitted at hold age 157 s, the latest a repeat fits: its mesh reset takes its worst case (263 s)
    and its full check 100 s and reads a 77. A re-check would end past 600 s, so none runs: the fabric
    stays unverified, the hold stays, and the bound's ladder and the power cycle come on time."""
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    monkeypatch.setattr(galaxy, "_hold_age_sec", lambda since: 157.0)
    skew = [0.0]

    class _Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        def monotonic(self):
            return time.monotonic() + skew[0]

    monkeypatch.setattr(recovery_mod, "time", _Clock())
    monkeypatch.setattr(recovery_mod, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0.0)
    monkeypatch.setattr(recovery_mod.fabric, "validator_not_built", lambda: None)
    plain_reset = srv.recovery_mechanism.reset_with_quiesce

    async def slow_reset(argv, log, *a, **k):
        skew[0] += mesh_repeat.MESH_RESET_WORST_SEC
        return await plain_reset(argv, log, *a, **k)

    checks = []

    async def verify_77(expected, log, run_fabric=True, **_):
        checks.append(run_fabric)
        skew[0] += 100.0
        return True, {"snapshot": {"ok": True, "detail": "32/32"}, "fabric": {"ok": None, "detail": "no trained link"}}

    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", slow_reset)
    monkeypatch.setattr(srv.galaxy_recovery, "_verify_device", verify_77)
    assert await _repeat() == ("ran", None), "a 77 is UNKNOWN: neither a recovery nor a failed reset"
    assert checks == [True], "one full check with fabric, no re-check past the bound"
    assert srv.recovery_mechanism.post_reset_verify_deadline is None
    assert srv.fsm.state is ServerState.RECOVERING, "an unverified fabric keeps the hold"
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", plain_reset)
    monkeypatch.setattr(recovery_mod, "time", time)
    fired = _arm_power_cycle(monkeypatch)
    await _bound_tick(monkeypatch, mesh)
    assert "hold_deadline_forced_escalation" in mesh["events"]
    assert fired == ["power-cycle"]


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
async def test_a_root_reader_that_outlives_the_grace_is_killed(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    reader = DeviceHolder(pid=424243, uid=0)
    sent, alive = [], {424243}
    scans = iter([HolderScan(holders=[reader], complete=True)])
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: next(scans, HolderScan(holders=[], complete=True)))
    monkeypatch.setattr(galaxy, "PREEMPT_KILL_GRACE_SEC", 0.01)
    monkeypatch.setattr(
        galaxy,
        "_signal_pid",
        lambda pid, sig: sent.append((pid, sig)) or (alive.discard(pid) if sig == signal.SIGKILL else None),
    )
    monkeypatch.setattr(galaxy, "_pid_alive", lambda pid: pid in alive)
    assert (await _repeat())[0] == "ran"
    assert sent == [(424243, signal.SIGTERM), (424243, signal.SIGKILL)], "SIGKILL once the grace has passed"
    assert len(mesh["resets"]) == 2


@pytest.mark.asyncio
async def test_a_live_tenant_blocks_the_repeat(mesh, monkeypatch):  # noqa: F811
    await _first_reset_failed(mesh)
    _age_first_reset(mesh_repeat.MESH_RESET_REPEAT_MIN_GAP_SEC + 5)
    tenant = DeviceHolder(pid=434343, uid=5000)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[tenant], complete=True))
    monkeypatch.setattr(galaxy, "_signal_pid", lambda pid, sig: pytest.fail("a tenant is never signalled"))
    assert (await _repeat())[0] == "wait"
    assert (await _repeat())[0] == "wait"
    assert mesh["events"].count("hold_bound_tenant_blocked") == 1, "journalled once per episode"
    assert mesh["log"].text().count("mesh reset repeat held off") == 2, "the log says it every tick"
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
