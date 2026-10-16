# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A mesh reset that failed with the off-bus set unchanged is not repeated in its hold (spec 04 I36).

One Galaxy host ran six identical mesh resets in one hold, each exiting 1 after a 263 s overrun, one
per forced hold-deadline window. These pin the bound: the next window climbs to the next stronger rung
(the last-chance sweep, then the host rung, rate limits kept) or, with no host rung, hands the hold to
an operator once, and fires no further reset on that set. The replay at the end times the off-bus
episode on a fake clock against the old path. No test reaches a real reset, reboot or power cycle."""

import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold, _arm_stuck_hold
from tt_device_mcp import alert, privileges
from tt_device_mcp import server as srv
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.evidence import health_dir
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery import offbus_power_cycle as opc
from tt_device_mcp.health.recovery.base import POWER_CYCLE_MIN_INTERVAL_SEC, RESET_RECORD_FILE

EXPECTED = 32
OFF_BUS = 4  # chips 28-31 off the bus, below the galaxy-reset floor
RESET_SEC = 263  # the measured overrun of each failed mesh reset (rc 1)
SWEEP_SEC = 323  # the last-chance sweep before a host rung (I22)
SKIP = "reset_rung_skipped_failed_unchanged"


def _episode(monkeypatch, *, power_cycle=False, clock=None, off_bus=OFF_BUS):
    """An idle off-bus hold past the ceiling whose mesh reset runs and exits non-zero, leaving the same
    chips off the bus. ``clock`` (a one-item list of hold-age seconds) drives the hold's age and is
    advanced by each reset and sweep. Returns (counters, events, alerts)."""
    counters = _arm_offbus_stuck_hold(monkeypatch, off_bus=off_bus, isolated=set(), reset_result=False)
    events, alerts = [], []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    monkeypatch.setattr(opc, "health_event", lambda kind, **f: events.append(kind))
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: alerts.append(event))
    counters["sweeps"] = counters["trays"] = 0
    _fresh_reset_record(monkeypatch)

    async def reset(indices, log):
        # The reset ran (it stamps the reset clock, as the real one does before it fires) and exited 1.
        counters["resets"] += 1
        srv.recovery_mechanism.last_reset_monotonic = time.monotonic()
        srv.recovery_mechanism.last_reset_rc = 1
        srv.recovery_mechanism.last_reset_exit_nonzero = True
        srv.recovery_mechanism.last_reset_failed = True
        if clock is not None:
            clock[0] += RESET_SEC
        return False

    async def tray(*a, **k):
        counters["trays"] += 1
        return None

    async def sweep(*a, **k):
        counters["sweeps"] += 1
        if clock is not None:
            clock[0] += SWEEP_SEC
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    monkeypatch.setattr(srv.galaxy_recovery, "_attempt_ubb_tray_reset", tray)
    monkeypatch.setattr(srv.galaxy_recovery, "_settle_and_verify_before_host_rung", sweep)
    if power_cycle:
        monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
        monkeypatch.setattr(privileges, "can_ipmi", lambda: True)
    if clock is not None:
        monkeypatch.setattr(galaxy, "_hold_age_sec", lambda since: clock[0])
    return counters, events, alerts


async def _window(force=True):
    return await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=force)


def _fresh_reset_record(monkeypatch):
    """Each episode starts on a boot with no failed reset recorded (spec 04 I58)."""
    (health_dir() / RESET_RECORD_FILE).unlink(missing_ok=True)
    monkeypatch.setattr(srv.recovery_mechanism, "_reset_record_cache", None)


def _ids():
    return {str(c) for c in range(EXPECTED - OFF_BUS, EXPECTED)}


# --- the failed reset is not repeated on the same set ---------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_unchanged_reset_is_not_repeated_and_with_no_host_rung_is_handed_off_once(
    monkeypatch, clear_job_state
):
    """No power cycle or reboot: the first window's reset fails, the hold goes to an operator once,
    latched, and no later window fires another reset. Fails on base, which resets every window."""
    counters, events, alerts = _episode(monkeypatch)
    hold_since = srv.device_hold_episode_since
    assert await _window() is True
    assert counters["resets"] == 1
    assert srv.recovery_mechanism.rung_failed_unchanged(hold_since, "mesh_reset", _ids())
    for _ in range(4):
        assert await _window() is True
    assert counters["resets"] == 1, "a reset that failed on this same set must not fire again in the hold"
    assert events.count(SKIP) == 4
    pages = [a for a in alerts if a["kind"] == "offbus_gated_hold_page"]
    assert len(pages) == 1, "one operator page per episode"
    assert "no power cycle or reboot to climb to" in pages[0]["message"]
    handoff = opc._pending["handoff"]
    assert handoff["deadline"] is None and handoff["latched"], "latched: no deadline lifts it into a reset"
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0 and counters["sweeps"] == 0
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


@pytest.mark.asyncio
async def test_with_a_power_cycle_the_failed_reset_climbs_at_once(monkeypatch, clear_job_state):
    """The early window's reset fails before the ceiling with the same chips off: every rung below the
    host pair has run once and the mesh reset may not run again, so the power cycle fires in that same
    pass, with no ceiling wait and no last-chance sweep (whose last step is that mesh reset) (I38)."""
    clock = [0.0]
    counters, events, _ = _episode(monkeypatch, power_cycle=True, clock=clock)
    assert await _window() is True
    assert counters["resets"] == 1 and "host_rung_deferred_until_ladder_end" not in events
    assert "host_rung_not_deferred" in events and SKIP in events
    assert counters["sweeps"] == 0, "the sweep would repeat the failed mesh reset"
    assert counters["power_cycles"] == 1 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0, "the power cycle releases nothing; the startup check gates release"


@pytest.mark.asyncio
async def test_a_rate_limited_power_cycle_fires_no_reset(monkeypatch, clear_job_state):
    """A power cycle the rate limit denies holds: no reset, no sweep, no cycle, window after window."""
    counters, events, _ = _episode(monkeypatch, power_cycle=True)
    monkeypatch.setattr(
        srv.recovery_mechanism,
        "read_auto_recovery_ledger",
        lambda: [{"action": "power-cycle", "at_epoch": time.time()}],
    )
    for _ in range(3):
        assert await _window() is True
    assert counters["resets"] == 1
    assert events.count(SKIP) == 2
    assert counters["sweeps"] == 0 and counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_a_changed_off_bus_set_lets_the_reset_run_again(monkeypatch, clear_job_state):
    """A chip came back (4 -> 3 off): progress another reset may finish, so it runs."""
    counters, events, _ = _episode(monkeypatch)
    assert await _window() is True and counters["resets"] == 1
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(EXPECTED - OFF_BUS + 1)})
    assert await _window() is True
    assert counters["resets"] == 2 and SKIP not in events


@pytest.mark.asyncio
async def test_a_new_hold_episode_on_the_same_set_does_not_reset_again(monkeypatch, clear_job_state):
    """The record is keyed by the boot, not the episode (spec 04 I58): the same off-bus set in a new
    hold of the same boot does not get the reset that already failed on it."""
    counters, events, _ = _episode(monkeypatch)
    assert await _window() is True and counters["resets"] == 1
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=20)).isoformat()
    assert await _window() is True
    assert counters["resets"] == 1 and SKIP in events


@pytest.mark.asyncio
async def test_a_reset_that_never_launched_is_not_recorded(monkeypatch, clear_job_state):
    """The reset clock is stamped before launch, so it cannot tell a launched reset from one that never
    exited (rc None, still cycling or not started): only a launched one that failed is recorded."""
    counters, events, _ = _episode(monkeypatch)

    async def unlaunched(indices, log):
        counters["resets"] += 1
        srv.recovery_mechanism.last_reset_monotonic = time.monotonic()
        srv.recovery_mechanism.last_reset_rc = None
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", unlaunched)
    hold_since = srv.device_hold_episode_since
    assert await _window() is True and counters["resets"] == 1
    assert not srv.recovery_mechanism.rung_failed_unchanged(hold_since, "mesh_reset", _ids())


@pytest.mark.asyncio
async def test_a_refused_reset_is_not_recorded(monkeypatch, clear_job_state):
    """A reset an outage hold refused never ran: it is not a failed rung, so the next window tries it
    again rather than climbing past it. The real reset path runs here; the command itself must not."""
    counters, events, _ = _episode(monkeypatch)
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    refusals = []
    monkeypatch.setattr(recovery_pkg, "refuse_mesh_reset", lambda *a, **k: refusals.append(a) or True)

    async def no_reset(*a, **k):
        raise AssertionError("a refused reset must not run")

    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", no_reset)
    hold_since = srv.device_hold_episode_since
    assert await _window() is True and len(refusals) == 1
    assert not srv.recovery_mechanism.rung_failed_unchanged(hold_since, "mesh_reset", _ids())
    assert await _window() is True
    assert len(refusals) == 2 and SKIP not in events, "the refused reset is offered again, not skipped"
    assert counters["dirty_cleared"] == 0


# --- the present mesh (fault class D) -------------------------------------------------------------


def _present_mesh(monkeypatch):
    counters = _arm_stuck_hold(monkeypatch, reset_result=False)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))

    async def reset(indices, log):
        counters["resets"] += 1
        srv.recovery_mechanism.last_reset_monotonic = time.monotonic()
        srv.recovery_mechanism.last_reset_rc = 1
        srv.recovery_mechanism.last_reset_failed = True
        return False

    async def sweep(*a, **k):
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    monkeypatch.setattr(srv.galaxy_recovery, "_settle_and_verify_before_host_rung", sweep)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(privileges, "can_ipmi", lambda: True)
    return counters, events


async def _present_window(force):
    return await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=force)


@pytest.mark.asyncio
async def test_a_forced_present_mesh_window_skips_the_failed_reset(monkeypatch, clear_job_state):
    counters, events = _present_mesh(monkeypatch)
    assert await _present_window(True) is True and counters["resets"] == 1
    assert await _present_window(True) is True
    assert counters["resets"] == 1 and SKIP in events
    assert counters["power_cycles"] == 2, "each window climbs; the rate limit is stubbed open here"


@pytest.mark.asyncio
async def test_a_non_forced_grace_retry_still_resets_a_present_mesh(monkeypatch, clear_job_state):
    """The grace-cadence retries before the ceiling stay: a present mesh's retry is cheap."""
    counters, events = _present_mesh(monkeypatch)
    assert await _present_window(True) is True and counters["resets"] == 1
    srv.recovery_mechanism.last_reset_monotonic = time.monotonic() - galaxy._present_mesh_reset_grace_sec() - 1
    assert await _present_window(False) is True
    assert counters["resets"] == 2 and SKIP not in events


# --- goal 8: a full health check after every reset ------------------------------------------------


@pytest.mark.asyncio
async def test_every_reset_is_verified_with_the_fabric_and_a_failed_verify_keeps_the_hold_and_climbs(
    monkeypatch, clear_job_state
):
    """The real reset path: the reset exits 0 and the full verify, fabric included, fails. The hold
    stands (nothing clears it), the failure is recorded, and the next window takes the next rung."""
    clock = [0.0]
    counters, events, _ = _episode(monkeypatch, power_cycle=True, clock=clock)
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    fired, verifies = [], []

    async def reset_cmd(argv, log, *a, **k):
        fired.append(argv)
        return 0, ""

    async def verify(expected, log, run_fabric=None, **_):
        verifies.append(run_fabric)
        return False, {"snapshot": {"ok": False}}

    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", reset_cmd)
    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", lambda log: _false())
    patch_recovery(monkeypatch, "_verify_device", verify)
    assert await _window() is True
    assert len(fired) == 1
    assert verifies and all(v in (None, True) for v in verifies), "the post-reset verify runs the fabric pass"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed verify keeps the hold"
    assert counters["sweeps"] == 0 and counters["power_cycles"] == 1, "it went on to the next rung at once"
    clock[0] = 600.0
    assert await _window() is True
    assert len(fired) == 1, "the failed reset is not repeated"
    assert counters["dirty_cleared"] == 0


async def _false():
    return False


# --- time to recover: the old path against the new on a fake clock --------------------------------


async def _replay(monkeypatch, *, old: bool, power_cycle: bool, windows: int):
    """Drive one off-bus episode through hold-deadline windows at hold age 0 (the early one-shot) and
    then every 600 s ceiling window. Returns (hold age when the power cycle fired or None, resets).
    Its patches are scoped to the replay, so conftest's own pins stay."""
    with monkeypatch.context() as mp:
        return await _replay_in(mp, old=old, power_cycle=power_cycle, windows=windows)


async def _replay_in(monkeypatch, *, old: bool, power_cycle: bool, windows: int):
    clock = [0.0]
    counters, _, _ = _episode(monkeypatch, power_cycle=power_cycle, clock=clock)
    if old:
        monkeypatch.setattr(srv.recovery_mechanism, "rung_failed_unchanged", lambda *a: False)
    fired_at = None

    async def power_cycle_host(log, reason):
        nonlocal fired_at
        counters["power_cycles"] += 1
        fired_at = clock[0]

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle_host)
    for w in range(windows):
        clock[0] = max(clock[0], 600.0 * w)
        await _window()
        if fired_at is not None:
            break
    return fired_at, counters["resets"]


@pytest.mark.asyncio
async def test_replay_the_power_cycle_fires_right_after_the_failed_reset(monkeypatch, clear_job_state):
    """Partial off-bus drop, power cycle armed. Old: 0 reset, 263 defer; 600 reset again, 863 sweep,
    1186 cycle. With I36 alone: 600 skip, sweep, 923 cycle. New (I36 + I38): the cycle fires at 263,
    right after the one failed reset."""
    old_at, old_resets = await _replay(monkeypatch, old=True, power_cycle=True, windows=4)
    new_at, new_resets = await _replay(monkeypatch, old=False, power_cycle=True, windows=4)
    assert (old_at, old_resets) == (1186.0, 2)
    assert (new_at, new_resets) == (float(RESET_SEC), 1)


@pytest.mark.asyncio
async def test_replay_with_no_host_rung_the_reset_loop_ends_in_one_hand_off(monkeypatch, clear_job_state):
    """No host rung: old fires the same reset every window (six in the incident); new fires one, then
    hands the hold to an operator."""
    old_at, old_resets = await _replay(monkeypatch, old=True, power_cycle=False, windows=6)
    new_at, new_resets = await _replay(monkeypatch, old=False, power_cycle=False, windows=6)
    assert old_at is None and new_at is None
    assert old_resets == 6 and new_resets == 1


# --- spec 04 I38: the host rung fires right after the failed reset, timed from the drop's onset -----

ONE_SHOT = 120.0  # the off-bus one-shot: the first forced window after the drop


async def _replay_from_onset(monkeypatch, *, old: bool, off_bus: int, reset_sec: int):
    """One partial off-bus drop timed from its onset: the one-shot window at 120 s, then a window every
    600 s ceiling. The reset runs ``reset_sec`` and exits 1 with the same chips off; the sweep costs one
    more reset plus its 60 s settle. Returns (onset age when the power cycle fired, resets)."""
    with monkeypatch.context() as mp:
        mp.setattr(__import__(__name__, fromlist=["_"]), "RESET_SEC", reset_sec)
        mp.setattr(__import__(__name__, fromlist=["_"]), "SWEEP_SEC", reset_sec + 60)
        clock = [ONE_SHOT]
        counters, events, _ = _episode(mp, power_cycle=True, clock=clock, off_bus=off_bus)
        if old:
            mp.setattr(srv.recovery_mechanism, "rung_failed_unchanged", lambda *a: False)
        fired_at = None

        async def power_cycle_host(log, reason):
            nonlocal fired_at
            fired_at = clock[0]

        mp.setattr(srv, "_auto_power_cycle_host", power_cycle_host)
        for w in range(4):
            clock[0] = max(clock[0], 600.0 * w, ONE_SHOT)
            assert await _window() is True
            if fired_at is not None:
                break
        assert counters["dirty_cleared"] == 0, "nothing releases before the post-cycle startup check"
        return fired_at, counters["resets"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "off_bus, reset_sec, old_cycle_at, new_cycle_at",
    [
        # B1, one chip off below the floor; the mesh reset gave up after 132 s (exit 1, chip still off).
        # Old: 120 reset, 252 deferred; 600 reset again, 732 sweep, 924 cycle. New: 252 cycle.
        (1, 132, 924.0, 252.0),
        # B2, four chips off; each mesh reset overran to 263 s (exit 1, the same four still off).
        # Old: 120 reset, 383 deferred; 600 reset again, 863 sweep, 1186 cycle. New: 383 cycle.
        (4, 263, 1186.0, 383.0),
    ],
    ids=["b1-one-chip", "b2-four-chips"],
)
async def test_partial_offbus_power_cycles_right_after_the_failed_reset(
    monkeypatch, clear_job_state, off_bus, reset_sec, old_cycle_at, new_cycle_at
):
    """Spec 04 I38: a partial off-bus drop whose one mesh reset failed on an unchanged set reaches the
    power cycle within 300 s of the one-shot plus that reset, not at the 600 s ceiling window."""
    old_at, old_resets = await _replay_from_onset(monkeypatch, old=True, off_bus=off_bus, reset_sec=reset_sec)
    new_at, new_resets = await _replay_from_onset(monkeypatch, old=False, off_bus=off_bus, reset_sec=reset_sec)
    assert (old_at, old_resets) == (old_cycle_at, 2)
    assert (new_at, new_resets) == (new_cycle_at, 1)
    assert new_at - ONE_SHOT - reset_sec < 5, "no wait between the failed reset and the power cycle"
    if reset_sec <= 180:
        assert new_at < 300


@pytest.mark.asyncio
async def test_a_denied_power_cycle_after_the_failed_reset_is_retried_next_window(monkeypatch, clear_job_state):
    """A power cycle the rate limit denies right after the failed reset is not lost: the next window
    asks again, and no reset fires in between (I36, I38)."""
    counters, events, _ = _episode(monkeypatch, power_cycle=True)
    allowed = iter([(False, "rate limited"), (True, "")])
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda *a, **k: next(allowed))
    assert await _window() is True
    assert counters["resets"] == 1 and counters["power_cycles"] == 0
    assert await _window() is True
    assert counters["resets"] == 1 and counters["power_cycles"] == 1 and counters["sweeps"] == 0


@pytest.mark.asyncio
async def test_partial_offbus_after_the_failed_reset_waits_only_for_the_power_cycle_cooldown(
    monkeypatch, clear_job_state
):
    """I38 with I65: the host booted 3 min ago and one chip stays off after the failed mesh reset. The
    climb still skips the ceiling wait and the sweep (I38), but the power cycle waits out the cooldown
    counted from that boot, with its deadline set for the watchdog; no reset fires meanwhile (I36,
    I58), and the cycle fires once the cooldown has run out. Nothing releases the hold."""
    clock = [ONE_SHOT]
    counters, events, _ = _episode(monkeypatch, power_cycle=True, clock=clock, off_bus=1)
    mech = srv.recovery_mechanism
    boot = [time.time() - 180]
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(boot[0])))
    monkeypatch.setattr(mech, "last_verified_release", lambda: None)
    monkeypatch.setattr(mech, "power_cycle_retry", None)
    monkeypatch.setattr(mech, "power_cycle_cooldown_state", None)
    monkeypatch.delenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, raising=False)
    assert await _window() is True
    assert "host_rung_not_deferred" in events and counters["sweeps"] == 0, "I38: no ceiling wait, no sweep"
    assert counters["resets"] == 1 and counters["power_cycles"] == 0, "I65: the cycle waits for the cooldown"
    until = int(boot[0]) + POWER_CYCLE_MIN_INTERVAL_SEC
    assert mech.power_cycle_retry[0] == until, "the watchdog asks again at the cooldown's end"
    assert mech.power_cycle_cooldown_state["cause"] == "host boot"
    clock[0] = 600.0
    assert await _window() is True
    assert counters["resets"] == 1 and counters["power_cycles"] == 0 and counters["sweeps"] == 0
    boot[0] -= POWER_CYCLE_MIN_INTERVAL_SEC  # the cooldown has run out
    assert await _window() is True
    assert counters["resets"] == 1 and counters["power_cycles"] == 1 and counters["sweeps"] == 0
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "nothing released the hold"


# --- E3: one failed reset per unchanged set, then the next rung, for every reset class ----------------

WINDOWS = 6  # the forced hold-deadline windows the incident ran one mesh reset in each


def _record_cycle_order(monkeypatch, counters):
    """Each power cycle notes how many resets had run when it fired: partial off-bus gets its mesh
    reset first (the host rung never jumps the queue), and only one."""
    order = []

    async def power_cycle(log, reason):
        counters["power_cycles"] += 1
        order.append(counters["resets"])

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    return order


@pytest.mark.asyncio
@pytest.mark.parametrize("power_cycle", [True, False], ids=["power-cycle", "no-host-rung"])
@pytest.mark.parametrize("off_bus", [1, 2, 4], ids=["b1-one-chip", "b1-two-chips", "b2-four-chips"])
async def test_e3_offbus_classes_reset_once_then_take_the_next_rung(
    monkeypatch, clear_job_state, off_bus, power_cycle
):
    """B1 (one or two chips off) and B2 (four), with and without a power cycle, over six forced
    windows: the mesh reset runs first and once on the unchanged set, the next rung follows in that
    same window (the power cycle, or one operator hand-off), and no later window repeats the reset.
    Nothing releases the hold: release waits for the full check after a reset that heals."""
    clock = [ONE_SHOT]
    counters, events, alerts = _episode(monkeypatch, power_cycle=power_cycle, clock=clock, off_bus=off_bus)
    order = _record_cycle_order(monkeypatch, counters)
    assert await _window() is True
    assert counters["resets"] == 1
    if power_cycle:
        assert order == [1], "the mesh reset ran first, then the power cycle in the same window"
        assert counters["sweeps"] == 0, "no last-chance sweep: its last step is the failed mesh reset"
    else:
        pages = [a for a in alerts if a["kind"] == "offbus_gated_hold_page"]
        assert len(pages) == 1 and opc._pending["handoff"]["latched"], "handed to an operator at once"
    for w in range(1, WINDOWS):
        clock[0] = max(clock[0], 600.0 * w)
        assert await _window() is True
    assert counters["resets"] == 1, "the failed reset is never repeated on the unchanged set"
    assert events.count(SKIP) >= WINDOWS - 1, "every later window skips it"
    if not power_cycle:
        assert len([a for a in alerts if a["kind"] == "offbus_gated_hold_page"]) == 1, "one page per episode"
        assert counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


@pytest.mark.asyncio
@pytest.mark.parametrize("host_rung", ["power-cycle", "reboot", "none"])
async def test_e3_present_mesh_resets_once_then_takes_the_next_rung(monkeypatch, clear_job_state, host_rung):
    """D (every chip present, the mesh reset fails its verify) over six forced windows: one reset,
    then the host rung in that same window (the power cycle, else the warm reboot; both are on by
    default), and with both opted out no further reset at all: the hold stays for an operator."""
    counters, events = _present_mesh(monkeypatch)
    if host_rung != "power-cycle":
        monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1" if host_rung == "reboot" else "0")
    monkeypatch.setattr(privileges, "is_root", lambda: True)
    monkeypatch.setattr(privileges, "has_systemd", lambda: True)
    order = _record_cycle_order(monkeypatch, counters)
    reboots_at = []

    async def reboot(log, reason):
        counters["reboots"] += 1
        reboots_at.append(counters["resets"])

    monkeypatch.setattr(srv.galaxy_recovery, "_auto_reboot_host", reboot)
    assert await _present_window(True) is True
    assert counters["resets"] == 1
    fired = {"power-cycle": order, "reboot": reboots_at, "none": []}[host_rung]
    assert fired == ([] if host_rung == "none" else [1]), "the host rung fired right after the failed reset"
    for _ in range(1, WINDOWS):
        assert await _present_window(True) is True
    assert counters["resets"] == 1, "the failed reset is never repeated on the unchanged mesh"
    assert events.count(SKIP) == WINDOWS - 1
    if host_rung == "none":
        assert counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


# --- spec 04 I39: one early retry after a failed mesh reset whose off-bus set shrank -------------------


def test_progress_retry_is_armed_once_per_episode_and_comes_due(monkeypatch):
    mech = srv.recovery_mechanism
    monkeypatch.setattr(mech, "progress_retry", None)
    monkeypatch.setattr(mech, "progress_retry_spent_since", "")
    since = "2026-10-15T18:43:41"
    assert mech.note_progress_retry(since, 60.0)
    t0 = time.monotonic()
    assert not mech.progress_retry_due(since, now=t0)
    assert mech.progress_retry_due(since, now=t0 + 61.0)
    assert not mech.progress_retry_due("2026-10-15T19:11:00", now=t0 + 61.0), "another episode never sees it"
    mech.spend_progress_retry()
    assert not mech.progress_retry_due(since, now=t0 + 61.0)
    assert not mech.note_progress_retry(since, 60.0), "a spent retry is never re-armed in its episode"
    assert mech.note_progress_retry("2026-10-15T19:11:00", 60.0), "a new episode gets its own"


def test_the_watchdog_spawns_the_due_progress_retry_before_the_ceiling(monkeypatch):
    fired = []
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: None)
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)  # the 120 s one-shot already ran
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry", None)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry_spent_since", "")
    srv.fsm.set_latch("escalated", False)
    monkeypatch.setattr(srv, "isolated_chips", {13})
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chip(s) 13 fell off the PCIe bus")
    since = datetime(2026, 10, 15, 18, 43, 41)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    age = since + timedelta(seconds=400)  # well inside the first ceiling window

    srv._check_hold_deadline(now=age)
    assert fired == [], "nothing is armed: the watchdog waits as before"

    assert srv.recovery_mechanism.note_progress_retry(since.isoformat(), 0.0)
    srv._check_hold_deadline(now=age)
    assert fired == [True], "the due retry runs at once, not at the ceiling window"
    assert srv.recovery_mechanism.progress_retry is None
    srv._check_hold_deadline(now=age + timedelta(seconds=30))
    assert fired == [True], "it is spent: one retry per episode"


# --- spec 04 I39 end to end: arm, spawn, one more reset on the changed set, then I38 ---------------


def _beats_with_off(off):
    return {str(c): 100 for c in range(EXPECTED) if str(c) not in off}


def _drive_progress_retry_watchdog(monkeypatch):
    """Make the armed retry due now and run the hold watchdog once; returns the spawn kinds it chose."""
    spawned = []
    monkeypatch.setattr(
        srv, "_maybe_spawn_forced_escalation", lambda: spawned.append(srv.device_hold_escalation_kind) or True
    )
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)  # the one-shot already ran
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)
    srv.fsm.set_latch("escalated", False)
    armed = srv.recovery_mechanism.progress_retry
    if armed is not None:
        srv.recovery_mechanism.progress_retry = (armed[0], time.monotonic() - 1)
    since = datetime.fromisoformat(srv.device_hold_episode_since)
    srv._check_hold_deadline(now=since + timedelta(seconds=200))
    return spawned


def _progress_episode(monkeypatch, after_each_reset, power_cycle=True):
    """An off-bus hold at the 120 s one-shot whose mesh resets exit 1; ``after_each_reset`` is the list
    of off-bus id sets the heartbeats show after the 1st, 2nd, ... reset."""
    clock = [ONE_SHOT]
    counters, events, alerts = _episode(monkeypatch, power_cycle=power_cycle, clock=clock, off_bus=2)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry", None)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry_spent_since", "")
    state = {"off": {"30", "31"}}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: _beats_with_off(state["off"]))
    srv.isolated_chips = set()
    seen = []

    async def reset(indices, log):
        seen.append(set(state["off"]))
        counters["resets"] += 1
        srv.recovery_mechanism.last_reset_monotonic = time.monotonic()
        srv.recovery_mechanism.last_reset_exit_nonzero = True
        srv.recovery_mechanism.last_reset_failed = True
        srv.recovery_mechanism.last_reset_rc = 1
        clock[0] += 132
        state["off"] = set(after_each_reset[min(counters["resets"], len(after_each_reset)) - 1])
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    return counters, events, alerts, seen, clock


@pytest.mark.asyncio
async def test_a_failed_reset_that_shrank_the_set_retries_once_on_the_new_set_then_climbs(monkeypatch, clear_job_state):
    """I39 end to end: the one-shot's mesh reset exits 1 with 2 -> 1 chips off. The retry is armed and
    nothing climbs yet (the set changed: not I38). The watchdog spawns it as `progress_retry`; it runs
    one more mesh reset on the 1-chip set, which exits 1 leaving that same chip off, so the climb goes to
    I38 and power-cycles in the same pass. Nothing releases the device at any step."""
    counters, events, _, seen, clock = _progress_episode(monkeypatch, [{"31"}, {"31"}])
    hold_since = srv.device_hold_episode_since
    assert await _window() is True
    assert events.count("mesh_reset_progress_retry_armed") == 1
    assert srv.recovery_mechanism.progress_retry is not None
    assert not srv.recovery_mechanism.rung_failed_unchanged(hold_since, "mesh_reset", {"31"})
    assert "host_rung_deferred_until_ladder_end" in events and "host_rung_not_deferred" not in events
    assert counters["power_cycles"] == 0 and counters["resets"] == 1

    assert _drive_progress_retry_watchdog(monkeypatch) == ["progress_retry"]
    assert srv.recovery_mechanism.progress_retry is None, "spent on the spawn"
    assert await _window() is True  # the spawned forced escalation
    assert counters["resets"] == 2 and seen == [{"30", "31"}, {"31"}], "one more reset, on the changed set"
    assert events.count("mesh_reset_progress_retry_armed") == 1, "never re-armed in the episode"
    assert "host_rung_not_deferred" in events and counters["power_cycles"] == 1, "unchanged after it: I38"
    assert counters["sweeps"] == 0 and clock[0] < 600, "inside the first ceiling window"
    assert _drive_progress_retry_watchdog(monkeypatch) == [], "no second retry"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "nothing released the hold"


@pytest.mark.asyncio
async def test_a_failed_reset_on_an_unchanged_set_arms_no_retry_and_climbs_at_once(monkeypatch, clear_job_state):
    """The control: the same two chips stay off after the exit 1. No retry is armed and the watchdog
    spawns none; the climb goes straight to I38's power cycle."""
    counters, events, _, _, _ = _progress_episode(monkeypatch, [{"30", "31"}])
    assert await _window() is True
    assert "mesh_reset_progress_retry_armed" not in events
    assert srv.recovery_mechanism.progress_retry is None
    assert "host_rung_not_deferred" in events and counters["power_cycles"] == 1 and counters["resets"] == 1
    assert _drive_progress_retry_watchdog(monkeypatch) == []
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_ok", [False, True], ids=["verify-fails-keeps-hold", "verify-passes-releases"])
async def test_the_progress_retry_releases_only_after_the_full_fabric_verify(monkeypatch, clear_job_state, verify_ok):
    """Goal of spec 04 I39 + the post-reset check (I32): on the real reset path the retry's reset exits 0
    and the full verify runs with the fabric pass. A failed verify keeps the hold and climbs (the set is
    unchanged: I38); only a passing verify clears it."""
    counters, events, _, _, _ = _progress_episode(monkeypatch, [{"31"}])
    state_off = [{"30", "31"}]
    monkeypatch.setattr(srv, "read_heartbeats", lambda: _beats_with_off(state_off[0]))
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    rcs, verifies = iter([1, 0]), []

    async def reset_cmd(argv, log, *a, **k):
        rc = next(rcs)
        state_off[0] = {"31"}  # the first reset brought chip 30 back; 31 stays off through both
        return rc, ""

    async def verify(expected, log, run_fabric=None, **_):
        verifies.append(run_fabric)
        assert counters["dirty_cleared"] == 0, "nothing cleared before the verify"
        return verify_ok, {"snapshot": {"ok": verify_ok}}

    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", reset_cmd)
    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", lambda log: _false())
    patch_recovery(monkeypatch, "_verify_device", verify)
    assert await _window() is True
    assert events.count("mesh_reset_progress_retry_armed") == 1 and verifies == []
    assert _drive_progress_retry_watchdog(monkeypatch) == ["progress_retry"]
    assert await _window() is True
    assert verifies and all(v in (None, True) for v in verifies), "the retry's verify runs the fabric pass"
    if verify_ok:
        assert counters["dirty_cleared"] == 1 and counters["power_cycles"] == 0
    else:
        assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed verify keeps the hold"
        assert counters["power_cycles"] == 1, "and goes on to the host rung (I38)"


@pytest.mark.asyncio
@pytest.mark.parametrize("power_cycle", [True, False], ids=["power-cycle", "no-host-rung"])
async def test_e3_shrunk_then_unchanged_resets_at_most_twice_then_climbs(monkeypatch, clear_job_state, power_cycle):
    """E3 with I39 over six forced windows: the one-shot's reset shrinks 2 -> 1 chips off, the one
    progress retry runs on the new set and leaves it unchanged, and from then on the hold climbs (the
    power cycle in that same window, or one operator hand-off) and never resets again."""
    counters, events, alerts, seen, clock = _progress_episode(monkeypatch, [{"31"}, {"31"}], power_cycle=power_cycle)
    order = _record_cycle_order(monkeypatch, counters)
    assert await _window() is True and counters["resets"] == 1 and order == []
    assert _drive_progress_retry_watchdog(monkeypatch) == ["progress_retry"]
    assert await _window() is True
    assert counters["resets"] == 2 and seen == [{"30", "31"}, {"31"}]
    if power_cycle:
        assert order == [2], "the power cycle fired right after the retry failed on the unchanged set"
    else:
        pages = [a for a in alerts if a["kind"] == "offbus_gated_hold_page"]
        assert len(pages) == 1 and opc._pending["handoff"]["latched"], "handed to an operator at once"
    for w in range(1, WINDOWS):
        clock[0] = max(clock[0], 600.0 * w)
        assert _drive_progress_retry_watchdog(monkeypatch) == [], "one retry per episode"
        assert await _window() is True
    assert counters["resets"] == 2, "at most two resets: the one-shot and the progress retry"
    assert counters["sweeps"] == 0
    if not power_cycle:
        assert counters["power_cycles"] == 0 and counters["reboots"] == 0
        assert len([a for a in alerts if a["kind"] == "offbus_gated_hold_page"]) == 1
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


# --- the record across a broker restart and a new boot (spec 04 I58) ------------------------------


def _restart(monkeypatch):
    """A broker restart: the in-memory state goes, the health dir stays."""
    mech = srv.recovery_mechanism
    monkeypatch.setattr(mech, "_reset_record_cache", None)
    monkeypatch.setattr(mech, "mesh_reset_failed_since_release", False)
    monkeypatch.setattr(mech, "last_reset_monotonic", 0.0)


@pytest.mark.asyncio
async def test_the_failed_reset_survives_a_broker_restart_but_not_a_new_boot(monkeypatch, clear_job_state):
    counters, events, _ = _episode(monkeypatch)
    assert await _window() is True and counters["resets"] == 1
    _restart(monkeypatch)
    assert await _window() is True
    assert counters["resets"] == 1 and SKIP in events, "after the restart the failed reset is not run again"
    _restart(monkeypatch)
    monkeypatch.setattr(srv.recovery_mechanism, "_current_boot_id", lambda: "another-boot")
    assert not srv.recovery_mechanism.rung_failed_unchanged("", "mesh_reset", _ids()), "a new boot starts fresh"
    assert await _window() is True and counters["resets"] == 2
