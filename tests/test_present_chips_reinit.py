# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I104: POST_RESET and board re-init on the chips present after a driver-wait give-up.

When a chip never comes back after a mesh reset's tray re-power, tt-smi's driver wait gives up and the
chips that did come back get no POST_RESET and no board re-init. The sweep then runs those two steps on
the chips present (the same ioctl and UMD discovery tt-smi runs), through the broker's reset path, whose
one bounded rescan follows. The full check with fabric decides: a pass lifts the hold, anything else
power-cycles with no added wait. Under an I97 deadline the step runs only when the settle and the full
check still end by it. No test reaches a real reset, ioctl, rescan or power cycle."""

import pytest

from tests.test_below_floor_offbus_ladder import _bound_ladder
from tests.test_late_chips_after_driver_wait import _after_sweep_mesh, _late
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import OUTCOME_TERMINAL, galaxy, present_reinit

GLX = ["tt-smi", "-glx_reset"]


def _reinit(events) -> list:
    return [k for n, k in events if n == "present_chips_reinit"]


def _done(events) -> list:
    return [k for n, k in events if n == "present_chips_reinit_done"]


@pytest.mark.asyncio
async def test_one_chip_stays_out_the_rest_are_reinited_the_check_fails_and_the_broker_cycles(
    monkeypatch, tmp_path, clear_job_state
):
    """The replay: chip 31 never comes back. The 31 chips present get POST_RESET and re-init, the full
    check with fabric fails, nothing lifts and the power cycle follows in the same pass."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    tail = ["mesh reset", "rescan", "readers stopped", "present reinit", "full check", "power cycle"]
    assert _after_sweep_mesh(order) == tail
    step = _reinit(events)
    assert len(step) == 1 and step[0]["fired"] is True and (step[0]["present"], step[0]["expected"]) == (31, 32)
    assert _done(events) == [{"rc": 0, "on_bus": 31, "expected": 32}]
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_the_chip_back_after_the_reinit_passes_the_check_and_no_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """The missing chip is back on the rescan after the re-init: the full check with fabric passes and
    lifts the hold once. No power cycle."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1, reinit_back=True, verdicts=[False, True])
    await _bound_ladder()
    assert _after_sweep_mesh(order) == ["mesh reset", "rescan", "readers stopped", "present reinit", "full check"]
    assert "power cycle" not in order
    assert _done(events) == [{"rc": 0, "on_bus": 32, "expected": 32}]
    assert counters["dirty_cleared"] == 1, "lifted once, on the just-passed full check with fabric"


@pytest.mark.asyncio
@pytest.mark.parametrize("reinit_rc", [0, 1], ids=["reinit-ok", "reinit-failed"])
async def test_a_failed_check_after_the_reinit_keeps_the_hold_and_cycles(
    monkeypatch, tmp_path, clear_job_state, reinit_rc
):
    """Every chip is back after the re-init, but the full check fails (a fabric short of the cabled
    links reads as a fail), or the re-init itself failed: the check still decides, nothing lifts, and
    the power cycle follows."""
    order, events, counters = _late(
        monkeypatch, tmp_path, back=1, reinit_back=True, reinit_rc=reinit_rc, verdicts=[False, False]
    )
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert _after_sweep_mesh(order)[-3:] == ["present reinit", "full check", "power cycle"]
    assert _done(events)[0]["rc"] == reinit_rc
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_the_step_itself_lifts_nothing(monkeypatch, tmp_path, clear_job_state):
    """Called alone, with every chip back after it, the step clears no hold and no fault."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1, reinit_back=True)
    mech, log = srv.recovery_mechanism, lambda m: None
    await mech.reset_with_quiesce(GLX, log)
    await srv.galaxy_recovery._present_chips_reinit(32, log, None)
    assert order == ["mesh reset", "readers stopped", "present reinit"]
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repower, withheld", [(False, False), (True, True)], ids=["no-repower", "rescan-withheld"])
async def test_no_reinit_without_the_repower_or_when_the_rescan_was_withheld(
    monkeypatch, tmp_path, clear_job_state, repower, withheld
):
    """A reset whose tray re-power did not run did not run at all, and a withheld rescan keeps its chips
    out: neither gets the re-init. The full check, then the power cycle, as before."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1, repower=repower, withheld=withheld)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert _after_sweep_mesh(order) == ["mesh reset", "full check", "power cycle"]
    withheld_skip = [{"fired": False, "reason": "the rescan after the reset was withheld (I69)"}]
    assert _reinit(events) == ([] if not repower else withheld_skip)
    assert counters["dirty_cleared"] == 0


def _reinit_ends(monkeypatch, order, *, raises=False, scope="tt-device-reset-3.scope"):
    """The I104 step's reset path ends badly: its wait ends at its bound (rc None) with its scope still
    cycling, as base.py leaves it, or (``raises``) the call raises. Any other reset runs as the rig's."""
    mech = srv.recovery_mechanism
    state = {"cycling": False}
    first = mech.reset_with_quiesce

    async def reset(argv, log, *a, **k):
        rc, out = await first(argv, log, *a, **k)
        if argv == galaxy._present_reinit_argv():
            if raises:
                raise RuntimeError("the re-init blew up")
            state["cycling"] = True
            return None, ""
        return rc, out

    monkeypatch.setattr(mech, "reset_with_quiesce", reset)
    monkeypatch.setattr(mech, "scope_active", lambda: scope if state["cycling"] else None)
    return state


@pytest.mark.asyncio
async def test_a_reinit_still_cycling_at_its_bound_gets_no_check_and_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """The re-init's wait ends at its bound (rc None) with its scope still cycling: no settle and no full
    check over it. The verify counts as failed, nothing lifts, and the power cycle follows in the same
    pass. The step's end is journalled with rc None and the chips on the bus."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1, reinit_back=True, verdicts=[False, True])
    state = _reinit_ends(monkeypatch, order)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert state["cycling"]
    assert _after_sweep_mesh(order)[-2:] == ["present reinit", "power cycle"]
    skipped = [k for n, k in events if n == "sweep_verify_skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "reset_scope_active"
    assert _done(events) == [{"rc": None, "on_bus": 32, "expected": 32}]
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "nothing lifts"


@pytest.mark.asyncio
async def test_a_reinit_that_raises_still_gets_the_full_check_and_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """The re-init raises: the step journals the error with the chips on the bus and returns; the full
    check with fabric still runs and decides. It fails, nothing lifts, and the power cycle follows."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1)
    _reinit_ends(monkeypatch, order, raises=True)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert _after_sweep_mesh(order)[-3:] == ["present reinit", "full check", "power cycle"]
    done = _done(events)
    assert len(done) == 1 and done[0]["rc"] is None and "blew up" in done[0]["error"]
    assert (done[0]["on_bus"], done[0]["expected"]) == (31, 32)
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "nothing lifts"


@pytest.mark.asyncio
async def test_a_raise_with_the_bus_unreadable_journals_no_chip_count(monkeypatch, tmp_path, clear_job_state):
    """The re-init raises and the bus cannot be read after it: the step still ends without raising, with
    on_bus None (not a count it did not read), and lifts nothing."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1)
    _reinit_ends(monkeypatch, order, raises=True)
    mech, log = srv.recovery_mechanism, lambda m: None
    await mech.reset_with_quiesce(GLX, log)

    def unreadable():
        raise OSError("sysfs gone")

    monkeypatch.setattr(galaxy.pcie_guard, "tt_endpoints", unreadable)
    await srv.galaxy_recovery._present_chips_reinit(32, log, None)
    assert order == ["mesh reset", "readers stopped", "present reinit"]
    assert _done(events)[0]["on_bus"] is None and _done(events)[0]["rc"] is None
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


def _no_listing():
    raise OSError("no /dev/tenstorrent")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "present, age_known, reason",
    [
        (_no_listing, True, "could not list the chips present"),
        (lambda: [], True, "no chip is present"),
        (None, False, "the hold age is unknown"),
    ],
    ids=["listing-failed", "no-chip", "age-unknown"],
)
async def test_the_skip_branches_stop_nothing_and_run_nothing(
    monkeypatch, tmp_path, clear_job_state, present, age_known, reason
):
    """A failed listing, no chip present, or an unknown hold age under a deadline: the step is journalled
    as not fired, stops no reader and runs no re-init; the full check decides as before."""
    order, events, counters = _late(monkeypatch, tmp_path, back=1)
    mech, log = srv.recovery_mechanism, lambda m: None
    await mech.reset_with_quiesce(GLX, log)
    if present is not None:
        monkeypatch.setattr(galaxy.present_reinit, "present_ids", present)
    if not age_known:
        monkeypatch.setattr(galaxy, "_hold_age_sec", lambda since: None)
    await srv.galaxy_recovery._present_chips_reinit(32, log, None if age_known else 585.0)
    assert order == ["mesh reset"], "no reader stop, no re-init"
    step = _reinit(events)
    assert len(step) == 1 and step[0]["fired"] is False and step[0]["reason"].startswith(reason)
    assert _done(events) == []
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_no_reinit_after_a_mesh_reset_that_ended_clean(monkeypatch, tmp_path, clear_job_state):
    """Both chips back late: the I102 second mesh reset ends clean, so its own POST_RESET and re-init
    ran and the I104 step does not."""
    order, events, _counters = _late(monkeypatch, tmp_path, back=2, verdicts=[False, True])
    await _bound_ladder()
    assert "present reinit" not in order and _reinit(events) == []


@pytest.mark.asyncio
async def test_under_an_i97_deadline_the_reinit_runs_only_when_the_check_still_fits(
    monkeypatch, tmp_path, clear_job_state
):
    """The wait for the step is PRESENT_REINIT_SEC, cut so the step's tail, the settle and the full
    check end by the deadline. Less than PRESENT_REINIT_MIN_SEC left: no reader stop and no re-init.
    The measured case: the sweep's reset gave up at a hold age of 374 s against the 585 s deadline,
    which leaves the step 16 s and the check its full allowance."""
    assert (galaxy.PRESENT_REINIT_SEC, galaxy.PRESENT_REINIT_MIN_SEC, galaxy.PRESENT_REINIT_TAIL_SEC) == (30, 10, 5)
    order, events, counters = _late(monkeypatch, tmp_path, back=1)
    gr, mech, log = srv.galaxy_recovery, srv.recovery_mechanism, lambda m: None
    await mech.reset_with_quiesce(GLX, log)
    age = galaxy._hold_age_sec(srv.device_hold_episode_since)
    settle_and_check = galaxy.MESH_RESET_BEFORE_HOST_RUNG_SETTLE_SEC + galaxy.MESH_RESET_BEFORE_HOST_RUNG_CHECK_SEC
    reserve = settle_and_check + galaxy.PRESENT_REINIT_TAIL_SEC  # the rig may shorten the settle
    await gr._present_chips_reinit(32, log, age + reserve + galaxy.PRESENT_REINIT_MIN_SEC - 1)
    assert order == ["mesh reset"], "no reader stop, no re-init"
    assert _reinit(events)[0]["fired"] is False and "deadline" in _reinit(events)[0]["reason"]
    t0 = galaxy.time.monotonic()
    left = 585 - 374 - (10 - galaxy.MESH_RESET_BEFORE_HOST_RUNG_SETTLE_SEC)  # the measured case, as the rig runs it
    await gr._present_chips_reinit(32, log, age + left)
    assert order == ["mesh reset", "readers stopped", "present reinit"]
    step = _reinit(events)[1]
    assert step["fired"] is True and step["wait_sec"] == pytest.approx(left - reserve, abs=0.5)
    assert left - reserve == pytest.approx(16, abs=0.5)
    assert counters["reinit_waits"][0] <= t0 + 16 + 0.5


def test_the_reinit_argv_is_the_module_under_the_broker_interpreter():
    argv = galaxy._present_reinit_argv()
    assert argv[0] == galaxy.sys.executable and argv[1:] == ["-I", "-m", "tt_device_mcp.health.recovery.present_reinit"]
    assert not galaxy.ipmi_guard.is_mesh_reset(argv), "not a mesh reset: the give-up flags of the last one stay"


# The module itself: tt-smi's POST_RESET ioctl and UMD re-init are stand-ins here.


def _devs(tmp_path, ids):
    d = tmp_path / "tenstorrent"
    d.mkdir()
    for i in ids:
        (d / str(i)).touch()
    (d / "by-id").mkdir()
    return str(d)


def test_present_ids_lists_only_the_numeric_device_nodes(tmp_path):
    assert present_reinit.present_ids(_devs(tmp_path, [10, 0, 2])) == [0, 2, 10]
    assert present_reinit.present_ids(str(tmp_path / "missing")) == []


def test_post_reset_on_every_present_chip_then_the_reinit(tmp_path, capsys):
    calls, reinits = [], []
    dev = _devs(tmp_path, range(1, 32))

    def ioctl(i, flag):
        calls.append((i, flag))
        return True

    assert present_reinit.main(dev, ioctl, 6, lambda: reinits.append(1) or 31) == 0
    assert sorted(calls) == [(i, 6) for i in range(1, 32)] and reinits == [1]
    assert "Re-initialized 31 boards" in capsys.readouterr().out


@pytest.mark.parametrize("fail", ["false", "oserror"])
def test_a_failed_post_reset_fails_the_step_and_skips_the_reinit(tmp_path, capsys, fail):
    reinits = []

    def ioctl(i, flag):
        if i == 3:
            if fail == "oserror":
                raise OSError(5, "Input/output error")
            return False
        return True

    assert present_reinit.main(_devs(tmp_path, range(4)), ioctl, 6, lambda: reinits.append(1) or 4) == 1
    assert reinits == []
    assert "POST_RESET failed for device" in capsys.readouterr().out


def test_a_failed_reinit_or_no_chip_fails_the_step(tmp_path):
    def boom():
        raise RuntimeError("discovery failed")

    assert present_reinit.main(_devs(tmp_path, [0]), lambda i, f: True, 6, boom) == 1
    assert present_reinit.main(str(tmp_path / "none"), lambda i, f: True, 6, lambda: 0) == 1


def test_the_ioctl_is_tt_smis_post_reset():
    ioctl, flag = present_reinit._tt_smi_ioctl()
    assert int(flag) == 6 and callable(ioctl)
