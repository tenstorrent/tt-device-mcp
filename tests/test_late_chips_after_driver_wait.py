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
Only a just-passed full check lifts the hold. No test reaches a real reset, rescan or power cycle."""

import time

import pytest

from tests.conftest import patch_health_event
from tests.test_below_floor_offbus_ladder import ALL, GONE, _bound_ladder, _rig
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import OUTCOME_TERMINAL, galaxy, pcie_guard

LATE = "30"  # the second chip of the replayed two-chip drop
BOTH_OUT = {c: v for c, v in ALL.items() if c not in (GONE, LATE)}


def _late(monkeypatch, tmp_path, *, back: int, repower=True, withheld=False, verdicts=()):
    """The 10-min bound on a 32-chip mesh holding with chip 31 off the bus. The sweep's mesh reset is a
    glx_reset whose tt-smi gave up at its driver wait with chips 30 and 31 out (after the tray re-power
    when ``repower``); the late-chip rescan brings ``back`` of the two back. A second mesh reset ends
    clean. Returns (order, events, counters): events are (name, fields)."""
    order, _names, counters = _rig(monkeypatch, tmp_path, verdicts=verdicts)
    bus = {"beats": {c: v for c, v in ALL.items() if c != GONE}, "after_give_up": False}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(bus["beats"]))
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: expected - len(bus["beats"]))
    monkeypatch.setattr(
        pcie_guard, "tt_endpoints", lambda: {f"0000:{0x40 + int(c):02x}:00.0": object() for c in bus["beats"]}
    )
    mech = srv.recovery_mechanism
    meshes = []

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        meshes.append(argv)
        sweep_first = len(meshes) == 1
        mech.last_reset_rc = 1 if sweep_first else 0
        mech.last_reset_driver_wait_failed = sweep_first
        mech.last_reset_tray_repower = sweep_first and repower
        mech.last_reset_rescan_withheld = sweep_first and withheld
        if sweep_first:
            bus["beats"], bus["after_give_up"] = dict(BOTH_OUT), True
        return mech.last_reset_rc, ""

    def rescan():
        order.append("rescan")
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
    assert step == ([] if not repower else [{"fired": False, "reason": "the rescan after the reset was withheld (I69)"}])
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
async def test_a_second_mesh_reset_that_would_end_past_the_deadline_is_skipped(monkeypatch, tmp_path, clear_job_state):
    """Under the I97 deadline the fit check is passed in: one that reads late stops the step after the
    rescan, before the readers stop or a reset starts."""
    order, events, _counters = _late(monkeypatch, tmp_path, back=2)
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)
    await srv.galaxy_recovery._late_chips_mesh_reset(
        ["tt-smi", "-glx_reset"], 32, lambda m: None, lambda: "would end past the deadline"
    )
    assert order == ["mesh reset", "rescan"]
    assert _step(events) == [{"fired": False, "reason": "would end past the deadline", "on_bus": 32, "expected": 32}]


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
