# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I93: the episode's second Galaxy mesh reset is spaced from the first.

After a first ``-glx_reset`` on a present mesh fails, the climb's last-chance sweep runs the episode's
second mesh reset no sooner than MESH_RESET_MIN_GAP_SEC after the first ended, and never later than the
10-min hold bound (I76): at the bound it runs at once. No third mesh reset: the sweep runs once per hold
phase (I87). The full check (fabric included) runs after it; only a pass lifts the hold, and a failed one
asks for the power cycle. Link-short holds, chips off the bus and BMC guard findings never wait.

The end-to-end tests drive the real gate: the gate opens the hold, then resets; one fake clock moves
monotonic time and the wall clock (the hold's age) together as the resets, checks and the wait run."""

import inspect
import time
from datetime import datetime, timedelta

import pytest

from tests.test_dispatch_failure_reset import CHIPS, _gate, mesh  # noqa: F401 - the shared fixture
from tt_device_mcp import privileges
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.recovery import galaxy, mesh_repeat, pcie_guard

GAP = mesh_repeat.MESH_RESET_MIN_GAP_SEC
BOUND = mesh_repeat.HOLD_BOUND_SEC
RESET_SEC = 120.0  # one mesh reset, wall time
CHECK_SEC = 60.0  # one full check (fabric included), wall time


def _arm_power_cycle(monkeypatch):
    fired = []
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(privileges, "can_ipmi", lambda: True)
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: fired.append(("power-cycle", _hold_age())))
    return fired


def _hold_age():
    return galaxy._hold_age_sec(srv.device_hold_episode_since)


@pytest.fixture
def clock(mesh, monkeypatch):  # noqa: F811
    """One fake clock for monotonic time and the wall clock. A mesh reset takes RESET_SEC, a full check
    CHECK_SEC and the gap wait its seconds; each reset records its start (monotonic) and the hold's age."""
    st = {"off": 0.0, "runs": [], "waits": []}
    real_mono, real_now = time.monotonic, datetime.now

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now(tz) + timedelta(seconds=st["off"])

    monkeypatch.setattr(time, "monotonic", lambda: real_mono() + st["off"])
    for mod in (srv, galaxy):
        monkeypatch.setattr(mod, "datetime", _Now)
    g = srv.galaxy_recovery
    reset, verify = g.mechanism.reset_with_quiesce, g._verify_device

    async def slow_reset(argv, log, *a, **k):
        st["runs"].append({"start": time.monotonic(), "age": _hold_age()})
        if st.get("before_reset"):
            st["before_reset"]()
        st["off"] += RESET_SEC
        out = await reset(argv, log, *a, **k)
        st["runs"][-1]["end"] = time.monotonic()
        return out

    async def slow_verify(*a, **k):
        st["off"] += CHECK_SEC
        return await verify(*a, **k)

    async def gap_sleep(seconds):
        st["waits"].append(seconds)
        st["off"] += seconds

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", slow_reset)
    for r in (g, srv.per_target_recovery):
        monkeypatch.setattr(r, "_verify_device", slow_verify)
    monkeypatch.setattr(galaxy, "_mesh_reset_gap_sleep", gap_sleep)
    return st


# --- the gap -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "since_end, age, wait",
    [(0, 100, GAP), (60, 180, GAP - 60), (199, 400, 1), (GAP, 300, 0), (500, 550, 0), (10, 550, 50), (0, BOUND, 0)],
)
def test_the_gap_wait_ends_by_the_hold_bound(since_end, age, wait):
    got = mesh_repeat.gap_wait(since_first_end_sec=since_end, hold_age_sec=age, bound_sec=BOUND)
    assert got == wait
    assert age + got <= BOUND, "the wait never runs past the 10-min bound"


def test_the_gap_wait_needs_the_first_reset_and_the_hold_clock():
    assert mesh_repeat.gap_wait(since_first_end_sec=None, hold_age_sec=100, bound_sec=BOUND) == 0
    assert mesh_repeat.gap_wait(since_first_end_sec=0, hold_age_sec=None, bound_sec=BOUND) == 0
    assert mesh_repeat.gap_wait(since_first_end_sec=0, hold_age_sec=500, bound_sec=450) == 0, "a shorter bound"


def test_no_env_or_config_knob_changes_the_gap():
    assert "environ" not in inspect.getsource(mesh_repeat)
    assert "environ" not in inspect.getsource(galaxy.GalaxyRecovery._space_second_mesh_reset)


# --- the real gate path --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_first_reset_spaces_the_sweeps_mesh_reset_then_asks_for_the_power_cycle(
    mesh, clock, monkeypatch  # noqa: F811
):
    """The gate opens the hold; its mesh reset (120 s) and full check (60 s) fail. The climb's sweep
    waits until 200 s after the first reset ended, runs the second mesh reset well before the bound,
    its full check fails, and the power cycle is asked. Two mesh resets, never a third."""
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True  # a prior cycle failed: the gate climbs this pass
    fired = _arm_power_cycle(monkeypatch)
    await _gate()
    assert mesh["resets"] == [["tt-smi", "-glx_reset"]] * 2, "the gate's reset, then the sweep's"
    first, second = clock["runs"]
    assert first["age"] is not None and first["age"] >= 0, "the gate opened the hold before its reset"
    assert clock["waits"] == [pytest.approx(GAP - CHECK_SEC, abs=1)], "the sweep waited out the gap"
    assert second["start"] - first["end"] >= GAP - 1, "the second reset starts the gap after the first ended"
    assert second["age"] < BOUND, "and before the 10-min bound"
    assert "mesh_reset_spaced" in mesh["events"]
    assert mesh["events"].count("last_chance_reset_sweep") == 1
    assert mesh["verifies"][-1] is True, "the full check (fabric included) ran after the second reset"
    assert srv.fsm.state is ServerState.RECOVERING, "a failed full check keeps the hold"
    assert [f[0] for f in fired] == ["power-cycle"], "and asks for the power cycle"
    assert fired[0][1] < BOUND, "before a ladder at the bound would have"


@pytest.mark.asyncio
async def test_a_spaced_sweep_whose_full_check_passes_lifts_the_hold(mesh, clock, monkeypatch):  # noqa: F811
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    fired = _arm_power_cycle(monkeypatch)

    def second_heals():
        if len(clock["runs"]) > 1:
            mesh["reset_heals"] = True

    clock["before_reset"] = second_heals
    await _gate()
    first, second = clock["runs"]
    assert second["start"] - first["end"] >= GAP - 1 and second["age"] < BOUND
    assert mesh["verifies"][-1] is True, "the full check with the fabric pass passed just before the lift"
    assert srv.fsm.state is ServerState.HEALTHY, "only that pass lifts the hold"
    assert fired == [], "no power cycle"


@pytest.mark.asyncio
async def test_a_first_reset_ending_late_waits_only_to_the_bound(mesh, clock, monkeypatch):  # noqa: F811
    """A first reset whose check ends 20 s before the bound: the sweep waits 20 s, not 200 s, and its
    mesh reset runs at the bound."""
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    fired = _arm_power_cycle(monkeypatch)

    def first_runs_late():
        if len(clock["runs"]) == 1:
            clock["off"] += BOUND - 20 - CHECK_SEC - RESET_SEC - clock["runs"][0]["age"]

    clock["before_reset"] = first_runs_late
    await _gate()
    assert clock["waits"] == [pytest.approx(20, abs=1)]
    assert clock["runs"][1]["age"] == pytest.approx(BOUND, abs=1), "the second reset runs at the bound"
    assert [f[0] for f in fired] == ["power-cycle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["", "link_short", "chip_off_bus", "bmc_finding", "other_episode", "sweep_spent"])
async def test_the_sweep_waits_only_on_a_present_mesh_after_this_episodes_reset(
    mesh, clock, monkeypatch, why  # noqa: F811
):
    """A link-short hold (I79/I83: the power cycle at once), a chip off the bus, a BMC guard finding
    (I91), a first reset from another episode or a spent sweep: no wait, the sweep runs (or skips) at
    once, as before."""
    mesh["reset_heals"] = False
    await _gate()  # the first reset fails; no prior failure, so the gate holds without climbing
    assert len(clock["runs"]) == 1 and srv.fsm.state is ServerState.RECOVERING
    g = srv.galaxy_recovery
    if why == "link_short":
        monkeypatch.setattr(g.deps, "fabric_link_short_hold", lambda: "lost link")
    elif why == "chip_off_bus":
        monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 1)
    elif why == "bmc_finding":
        monkeypatch.setattr(g.mechanism, "bmc_guard_state", {"kind": "fallback_0x0"})
    elif why == "other_episode":
        g.mechanism.last_mesh_reset_run["episode"] = "2000-01-01T00:00:00"
        g.mechanism.last_mesh_reset_run["ended"] -= 100_000
    elif why == "sweep_spent":
        g.mechanism.sweep_spent = g._sweep_phase()
    waited = await g._space_second_mesh_reset(CHIPS, srv.logger.info, "")
    assert waited is (why == "")
    assert clock["waits"] == ([pytest.approx(GAP - CHECK_SEC, abs=1)] if not why else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["gap_pending", "gap_over"])
async def test_the_bound_runs_the_ladder_and_asks_for_the_power_cycle_at_600s(
    mesh, clock, monkeypatch, first  # noqa: F811
):
    """At the 10-min mark the forced ladder runs at once, never waiting for the gap, whatever happened
    below the bound; a failing full check asks for the power cycle (I76)."""
    mesh["reset_heals"] = False
    await _gate()  # the first reset fails and the hold stands: nothing climbed below the bound
    resets = len(mesh["resets"])
    assert resets == 1 and srv.fsm.state is ServerState.RECOVERING
    clock["off"] += BOUND - _hold_age() + 1
    if first == "gap_pending":
        # The first reset ended 10 s before the bound: its gap is still open when the bound comes.
        srv.recovery_mechanism.last_mesh_reset_run["ended"] = time.monotonic() - 10
    fired = _arm_power_cycle(monkeypatch)
    clock["waits"].clear()
    mesh["events"].clear()
    for name, value in (
        ("device_hold_deadline_bucket", 0),
        ("device_hold_escalate_bucket", 0),
        ("device_hold_offbus_escalated", False),
        ("_forced_escalation_task", None),
    ):
        monkeypatch.setattr(srv, name, value)
    srv._check_hold_deadline()
    if srv._forced_escalation_task is not None:
        await srv._forced_escalation_task
    assert "hold_deadline_forced_escalation" in mesh["events"], "the bound's ladder runs at 600 s"
    assert clock["waits"] == [], "the bound never waits for the gap"
    assert mesh["events"].count("last_chance_reset_sweep") == 1
    assert len(mesh["resets"]) == resets + 1, "the bound's sweep's mesh reset"
    assert [f[0] for f in fired] == ["power-cycle"], "the full check still fails: the power cycle is asked"
    assert srv.fsm.state is not ServerState.HEALTHY
