# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I102: chips that came back after a mesh reset's driver wait gave up.

tt-smi's ``-glx_reset`` re-powers the trays, then waits 100 s for every chip. When one link trains
later, tt-smi gives up: no POST_RESET and no board re-init, and the chips it found stay ARC-not-ready.
After such a give-up, when the tray re-power ran, the sweep rescans the Tenstorrent root ports once (the
PCI writer's bound, no settle). Every expected chip back on the bus: the broker's readers stop and one
more mesh reset runs, then the caller's full check with fabric decides. A chip still missing: the full
check, then the power cycle, with no wait added. A reset that did not run (no re-power) gets none of it.
The step is bounded from its own start (LATE_CHIP_STEP_SEC until the wait for its reset ends). Under an
I97 deadline it is taken only when it, the settle and the full check end by that deadline; otherwise it
adds no time. Only a just-passed full check lifts the hold. No test reaches a real reset, rescan or
power cycle."""

import time

import pytest

from tests.conftest import patch_health_event
from tests.test_below_floor_offbus_ladder import ALL, GONE, NEEDS, _b1_sweep, _bound_ladder, _clock, _i97, _rig
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import OUTCOME_TERMINAL, galaxy, pcie_guard

LATE = "30"  # the second chip of the replayed two-chip drop
BOTH_OUT = {c: v for c, v in ALL.items() if c not in (GONE, LATE)}


def _late(
    monkeypatch,
    tmp_path,
    *,
    back: int,
    repower=True,
    withheld=False,
    verdicts=(),
    i97=False,
    gives_up_sec=0,
    second_gives_up=False,
):
    """The 10-min bound on a 32-chip mesh holding with chip 31 off the bus. The sweep's mesh reset is a
    glx_reset whose tt-smi gave up at its driver wait with chips 30 and 31 out (after the tray re-power
    when ``repower``); the late-chip rescan brings ``back`` of the two back. A second mesh reset ends
    clean, or gives up again (``second_gives_up``). ``i97``: the sweep's repeated mesh reset runs as the
    I97 one under its deadline (I100); otherwise as a plain sweep reset. ``gives_up_sec``: how far the
    first reset moves galaxy's clock. Returns (order, events, counters): events are (name, fields);
    ``counters["waits"]`` holds each mesh reset's ``wait_until`` (None when not given) and
    ``counters["rescanned_at"]`` the monotonic time of each rescan."""
    order, _names, counters = _rig(monkeypatch, tmp_path, verdicts=verdicts)
    if not i97:
        monkeypatch.setattr(srv.galaxy_recovery, "_repeat_runs_under_deadline", lambda expected: False)
    clock = _clock(monkeypatch) if gives_up_sec else None
    bus = {"beats": {c: v for c, v in ALL.items() if c != GONE}, "after_give_up": False}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(bus["beats"]))
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: expected - len(bus["beats"]))
    monkeypatch.setattr(
        pcie_guard, "tt_endpoints", lambda: {f"0000:{0x40 + int(c):02x}:00.0": object() for c in bus["beats"]}
    )
    mech = srv.recovery_mechanism
    meshes = []
    counters["waits"], counters["rescanned_at"] = [], []

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        meshes.append(argv)
        counters["waits"].append(k.get("wait_until"))
        sweep_first = len(meshes) == 1
        gave_up = sweep_first or second_gives_up
        mech.last_reset_rc = 1 if gave_up else 0
        mech.last_reset_driver_wait_failed = gave_up
        mech.last_reset_tray_repower = gave_up and repower
        mech.last_reset_rescan_withheld = sweep_first and withheld
        if sweep_first:
            bus["beats"], bus["after_give_up"] = dict(BOTH_OUT), True
            if clock is not None:
                clock.shift += gives_up_sec
        return mech.last_reset_rc, ""

    def rescan():
        order.append("rescan")
        counters["rescanned_at"].append(time.monotonic())
        if bus["after_give_up"]:
            bus["beats"] = {**BOTH_OUT, **{c: 100 for c in (LATE, GONE)[:back]}}

    async def stop_readers(reason, grace):
        order.append("readers stopped")
        return []

    monkeypatch.setattr(mech, "reset_with_quiesce", mesh)
    monkeypatch.setattr(galaxy, "_pci_rescan", rescan)
    monkeypatch.setattr(srv.galaxy_recovery.monitor, "stop_check_readers", stop_readers)
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append((name, k)))
    return order, events, counters


def _after_sweep_mesh(order) -> list:
    """The sweep's part of ``order``: from the first mesh reset the sweep issued (its glx_reset)."""
    return order[order.index("mesh reset", order.index("full check")) :]


def _step(events) -> list:
    return [k for n, k in events if n == "late_chips_mesh_reset"]


@pytest.mark.asyncio
async def test_both_chips_back_late_get_one_more_mesh_reset_then_the_full_check_lifts(
    monkeypatch, tmp_path, clear_job_state
):
    """The two-chip replay: both chips come back after tt-smi gave up. One rescan, the readers stop, one
    more mesh reset (its POST_RESET and re-init), then the full check with fabric passes and lifts the
    hold. No power cycle."""
    order, events, counters = _late(monkeypatch, tmp_path, back=2, verdicts=[False, True])
    await _bound_ladder()
    assert _after_sweep_mesh(order) == ["mesh reset", "rescan", "readers stopped", "mesh reset", "full check"]
    assert "power cycle" not in order
    step = _step(events)
    assert len(step) == 1 and step[0]["fired"] is True and step[0]["on_bus"] == 32
    assert ("late_chips_mesh_reset_done", {"rc": 0, "driver_wait_gave_up": False}) in events
    assert counters["dirty_cleared"] == 1, "lifted once, on the just-passed full check with fabric"


@pytest.mark.asyncio
async def test_a_failed_full_check_after_the_second_mesh_reset_keeps_the_hold_and_power_cycles(
    monkeypatch, tmp_path, clear_job_state
):
    """Goal: the full check decides. Both chips are back and the second mesh reset ran, but the check
    with fabric fails (a short fabric reads as a fail): nothing lifts and the power cycle follows."""
    order, events, counters = _late(monkeypatch, tmp_path, back=2, verdicts=[False, False])
    assert await _bound_ladder() == OUTCOME_TERMINAL
    tail = ["mesh reset", "rescan", "readers stopped", "mesh reset", "full check", "power cycle"]
    assert _after_sweep_mesh(order) == tail
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_one_chip_still_out_goes_to_the_check_and_the_power_cycle_with_no_wait(
    monkeypatch, tmp_path, clear_job_state
):
    """Only chip 30 is back after the rescan: no second mesh reset and no settle. The full check runs
    and its failure requests the power cycle in the same pass."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1)
    sleeps = []
    real_sleep = galaxy.asyncio.sleep

    async def sleep(sec, *a, **k):
        sleeps.append((len(order), sec))
        return await real_sleep(0)

    monkeypatch.setattr(galaxy.asyncio, "sleep", sleep)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert _after_sweep_mesh(order) == ["mesh reset", "rescan", "full check", "power cycle"]
    late_rescan = len(order) - len(_after_sweep_mesh(order)) + 2  # order's length once it ran
    after = [sec for at, sec in sleeps if at == late_rescan]
    assert after == [galaxy._settle_before_host_rung_sec()], "the sweep's own settle only: the step adds no wait"
    step = _step(events)
    assert len(step) == 1 and step[0]["fired"] is False and (step[0]["on_bus"], step[0]["expected"]) == (31, 32)
    assert counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_the_late_chip_step_alone_is_quick_when_a_chip_is_missing(monkeypatch, tmp_path, clear_job_state):
    """The step's whole cost with a chip still out is its one rescan write: no settle, no reset."""
    order, events, _counters = _late(monkeypatch, tmp_path, back=1)
    mech = srv.recovery_mechanism
    await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)
    start = time.monotonic()
    await srv.galaxy_recovery._late_chips_mesh_reset(["tt-smi", "-glx_reset"], 32, lambda m: None, None)
    assert time.monotonic() - start < 1.0
    assert order == ["mesh reset", "rescan"]
    assert galaxy.LATE_CHIP_RESCAN_SEC == 30, "the stated bound: the PCI writer's timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("repower, withheld", [(False, False), (True, True)], ids=["no-repower", "rescan-withheld"])
async def test_no_late_chip_step_without_the_repower_or_when_the_rescan_was_withheld(
    monkeypatch, tmp_path, clear_job_state, repower, withheld
):
    """A glx_reset that gave up with no tray re-power did not run (reset_did_not_run, unchanged), and a
    reset whose rescan I69 withheld keeps its chips out: neither gets the rescan or a second mesh reset.
    The full check, then the power cycle, as before."""
    order, events, counters = _late(monkeypatch, tmp_path, back=2, repower=repower, withheld=withheld)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert _after_sweep_mesh(order) == ["mesh reset", "full check", "power cycle"]
    step = _step(events)
    assert step == (
        [] if not repower else [{"fired": False, "reason": "the rescan after the reset was withheld (I69)"}]
    )
    assert counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_the_second_mesh_reset_runs_once_per_hold(monkeypatch, tmp_path, clear_job_state):
    """A second give-up in the same hold gets no third mesh reset."""
    order, events, _counters = _late(monkeypatch, tmp_path, back=2)
    gr, mech = srv.galaxy_recovery, srv.recovery_mechanism
    log = lambda m: None  # noqa: E731
    for _ in range(2):
        await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], log)
        mech.last_reset_driver_wait_failed = mech.last_reset_tray_repower = True
        await gr._late_chips_mesh_reset(["tt-smi", "-glx_reset"], 32, log, None)
    assert [k["fired"] for k in _step(events)] == [True, False]
    assert _step(events)[1]["reason"] == "already ran in this hold"


@pytest.mark.asyncio
async def test_under_an_i97_deadline_the_step_runs_only_when_it_and_the_check_end_by_it(
    monkeypatch, tmp_path, clear_job_state
):
    """The step's bound: the rescan, the readers' grace, the BMC preflight, the wait for a dying holder
    and a reset that gives up (380 s), then the settle and the full check's allowance (570 s in all).
    A deadline short of that stops the step before its rescan, with no time added; one with room runs
    it, its reset's wait ending by the step's start plus 380 s."""
    assert galaxy.LATE_CHIP_STEP_SEC == 30 + 10 + 10 + 30 + 300 == 380
    order, events, counters = _late(monkeypatch, tmp_path, back=2)
    needs = galaxy._late_chips_needs_sec()  # the rig sets the settle to 0
    assert needs == 380 + galaxy.MESH_RESET_BEFORE_HOST_RUNG_SETTLE_SEC + 180
    gr, mech, log = srv.galaxy_recovery, srv.recovery_mechanism, lambda m: None
    await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], log)
    age = galaxy._hold_age_sec(srv.device_hold_episode_since)
    await gr._late_chips_mesh_reset(["tt-smi", "-glx_reset"], 32, log, age + needs - 5)
    assert order == ["mesh reset"], "no rescan, no reader stop, no reset"
    deadline = round(age + needs - 5)
    assert _step(events) == [
        {
            "fired": False,
            "reason": f"it and the full check after it could not end by the {deadline}s deadline",
            "deadline_sec": pytest.approx(age + needs - 5, abs=0.1),
        }
    ]
    await gr._late_chips_mesh_reset(["tt-smi", "-glx_reset"], 32, log, age + needs + 60)
    assert order == ["mesh reset", "rescan", "readers stopped", "mesh reset"]
    assert _step(events)[1]["fired"] is True
    started, wait_until = counters["rescanned_at"][0], counters["waits"][1]
    assert started < wait_until <= started + galaxy.LATE_CHIP_STEP_SEC


@pytest.mark.asyncio
async def test_on_the_i100_repeat_the_step_is_not_taken_and_the_power_cycle_keeps_its_bound(
    monkeypatch, tmp_path, clear_job_state
):
    """The sweep's repeated mesh reset runs as the I97 one (I100) under a deadline of its start plus
    300 s. Its tt-smi gives up at the driver wait after 263 s with both chips back: about 37 s are left,
    short of the step and its check, so the step stops before its rescan. No check (it could not end by
    the deadline): the hold stays and the power cycle is requested within the repeat's bound."""
    order, events, counters = _late(monkeypatch, tmp_path, back=2, i97=True, gives_up_sec=263)
    cycled_at = []

    async def power_cycle(log, reason):
        order.append("power cycle")
        cycled_at.append(galaxy._hold_age_sec(srv.device_hold_episode_since))

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert _after_sweep_mesh(order) == ["mesh reset", "power cycle"]
    fired = _i97(events)
    assert [(k["fired"], k["repeat"]) for k in fired] == [(True, True)]
    step = _step(events)
    assert len(step) == 1 and step[0]["fired"] is False and "deadline" in step[0]["reason"]
    assert step[0]["deadline_sec"] == fired[0]["deadline_sec"], "the I97 deadline, unchanged"
    unverified = [k for n, k in events if n == "whole_mesh_reset_before_host_rung_unverified"]
    assert len(unverified) == 1 and "gave up" in unverified[0]["reason"] and "deadline" in unverified[0]["reason"]
    bound = NEEDS + galaxy.MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC + galaxy.MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC
    assert len(cycled_at) == 1 and cycled_at[0] <= fired[0]["hold_age_sec"] + bound
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_on_the_i97_path_the_step_is_not_taken_and_the_power_cycle_comes_by_the_bound(
    monkeypatch, tmp_path, clear_job_state
):
    """Class B1: the SBR has every chip back, the check fails on ARC-not-ready chips, and the I97 mesh
    reset starts at the latest hold age its deadline allows. Its tt-smi gives up at the driver wait
    after 263 s with the trays re-powered: the step would end past the deadline, so it stops before
    its rescan. No check, the hold stays, and the power cycle is requested by the 10-min bound."""
    deadline = galaxy._mesh_reset_before_host_rung_deadline_sec()
    order, events, counters, _reasons = _b1_sweep(
        monkeypatch, tmp_path, verdicts=[False, False], held_sec=deadline - NEEDS - 10
    )
    clock, cycled_at, mech = _clock(monkeypatch), [], srv.recovery_mechanism

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        clock.shift += 263
        for name, value in (
            ("last_reset_driver_wait_failed", True),
            ("last_reset_tray_repower", True),
            ("last_reset_rescan_withheld", False),
            ("last_reset_rc", 1),
        ):
            monkeypatch.setattr(mech, name, value)
        return 1, ""

    async def power_cycle(log, reason):
        order.append("power cycle")
        cycled_at.append(galaxy._hold_age_sec(srv.device_hold_episode_since))

    monkeypatch.setattr(mech, "reset_with_quiesce", mesh)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order[order.index("mesh reset") :] == ["mesh reset", "power cycle"], "no rescan, no check"
    assert [k["fired"] for k in _i97(events)] == [True]
    step = _step(events)
    assert len(step) == 1 and step[0]["fired"] is False and step[0]["deadline_sec"] == round(deadline, 1)
    assert len(cycled_at) == 1 and cycled_at[0] <= galaxy._stuck_hold_ceiling_sec()
    assert counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_the_class_a_cooldown_sweep_takes_no_late_chip_step(monkeypatch, tmp_path, clear_job_state):
    """A whole tray off with the power cycle in cooldown: the 10-min sweep's mesh reset gives up at its
    driver wait after the re-power, yet no rescan and no second mesh reset follow (late_chips=False).
    Its one full check fails and the cycle follows at the cooldown's end."""
    order, events, counters = _late(monkeypatch, tmp_path, back=2)
    gr = srv.galaxy_recovery
    monkeypatch.setattr(gr.mechanism, "class_a_ladder_due", lambda *a, **k: True)
    monkeypatch.setattr(gr.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    out = await gr._class_a_cooldown_sweep({str(i) for i in range(24, 32)}, 32, lambda m: None)
    assert out == OUTCOME_TERMINAL
    assert order.count("mesh reset") == 1 and "rescan" not in order and "readers stopped" not in order
    assert order[-1] == "full check" and "power cycle" not in order, "the cycle waits for the cooldown"
    assert _step(events) == [] and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_a_second_give_up_on_the_plain_sweep_is_checked_and_power_cycles_within_the_step_bound(
    monkeypatch, tmp_path, clear_job_state
):
    """Both chips are back, the second mesh reset runs and gives up at its driver wait too: the full
    check still runs (it decides), fails, nothing lifts and the power cycle is requested. The wait for
    that reset ended by the step's start plus LATE_CHIP_STEP_SEC; the sweep's own reset had no such
    bound."""
    order, events, counters = _late(monkeypatch, tmp_path, back=2, second_gives_up=True)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    tail = ["mesh reset", "rescan", "readers stopped", "mesh reset", "full check", "power cycle"]
    assert _after_sweep_mesh(order) == tail
    assert ("late_chips_mesh_reset_done", {"rc": 1, "driver_wait_gave_up": True}) in events
    waits, started = counters["waits"], counters["rescanned_at"][-1]  # the step's rescan, not the ladder's
    assert waits[0] is None and started < waits[1] <= started + galaxy.LATE_CHIP_STEP_SEC
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_a_failed_rescan_ends_the_step(monkeypatch, tmp_path, clear_job_state):
    """A rescan the PCI writer refuses or times out on: no second mesh reset; the power cycle decides."""
    order, events, _counters = _late(monkeypatch, tmp_path, back=2)

    def wedged():
        order.append("rescan")
        raise OSError("write failed")

    monkeypatch.setattr(galaxy, "_pci_rescan", wedged)
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)
    await srv.galaxy_recovery._late_chips_mesh_reset(["tt-smi", "-glx_reset"], 32, lambda m: None, None)
    assert order == ["mesh reset", "rescan"]
    assert _step(events)[0]["fired"] is False and "did not complete" in _step(events)[0]["reason"]
