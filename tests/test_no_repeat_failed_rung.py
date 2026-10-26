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
SPENT = "reset_rung_skipped_spent"


def _skipped(events):
    """Windows that declined the failed mesh reset: on the unchanged set (I36, I58) or because the
    episode's one mesh reset is spent (I87)."""
    return events.count(SKIP) + events.count(SPENT)


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

    async def sweep(*a, **k):
        counters["sweeps"] += 1
        if clock is not None:
            clock[0] += SWEEP_SEC
        return False

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
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
    assert _skipped(events) == 4
    pages = [a for a in alerts if a["kind"] == "offbus_gated_hold_page"]
    assert len(pages) == 1, "one operator page per episode"
    assert "no power cycle to climb to" in pages[0]["message"]
    handoff = opc._pending["handoff"]
    assert handoff["deadline"] is None and handoff["latched"], "latched: no deadline lifts it into a reset"
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0 and counters["sweeps"] == 0
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


@pytest.mark.asyncio
async def test_with_a_power_cycle_the_failed_reset_climbs_at_once(monkeypatch, clear_job_state):
    """The early window's reset fails before the ceiling with the same chips off: no ceiling wait, but
    the last-chance sweep (the full ladder) runs right before the power cycle in that same pass (I38)."""
    clock = [0.0]
    counters, events, _ = _episode(monkeypatch, power_cycle=True, clock=clock)
    assert await _window() is True
    assert counters["resets"] == 1 and "host_rung_deferred_until_ladder_end" not in events
    assert "host_rung_not_deferred" in events and SKIP not in events
    assert counters["sweeps"] == 1, "the full ladder runs right before the power cycle"
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
    assert _skipped(events) == 2
    assert counters["sweeps"] == 0 and counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_a_changed_off_bus_set_gets_no_second_reset_in_the_episode(monkeypatch, clear_job_state):
    """A chip came back (4 -> 3 off) after the failed reset: the episode's one mesh reset is spent, so no
    second one runs on the new set (spec 04 I87); the next rung is the power cycle or a hand-off."""
    counters, events, _ = _episode(monkeypatch)
    assert await _window() is True and counters["resets"] == 1
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(EXPECTED - OFF_BUS + 1)})
    assert await _window() is True
    assert counters["resets"] == 1 and SPENT in events


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
    assert counters["resets"] == 1 and _skipped(events)
    assert counters["power_cycles"] == 2, "each window climbs; the rate limit is stubbed open here"


@pytest.mark.asyncio
async def test_a_non_forced_grace_pass_does_not_reset_a_present_mesh_again(monkeypatch, clear_job_state):
    """The grace-cadence pass after a failed reset no longer retries it: the episode's one mesh reset is
    spent (spec 04 I87), so a later idle pass runs no reset."""
    counters, events = _present_mesh(monkeypatch)
    assert await _present_window(True) is True and counters["resets"] == 1
    srv.recovery_mechanism.last_reset_monotonic = time.monotonic() - galaxy._present_mesh_reset_grace_sec() - 1
    await _present_window(False)
    assert counters["resets"] == 1


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
    assert counters["sweeps"] == 1 and counters["power_cycles"] == 1, "the sweep, then the power cycle at once"
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
        monkeypatch.setattr(srv.recovery_mechanism, "mesh_reset_spent", lambda *a: False)
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
    1186 cycle. With I36 alone: 600 skip, sweep, 923 cycle. New (I36 + I38): the sweep runs right
    after the one failed reset and the cycle fires at 586, at the sweep's end."""
    old_at, old_resets = await _replay(monkeypatch, old=True, power_cycle=True, windows=4)
    new_at, new_resets = await _replay(monkeypatch, old=False, power_cycle=True, windows=4)
    assert (old_at, old_resets) == (1186.0, 2)
    assert (new_at, new_resets) == (float(RESET_SEC + SWEEP_SEC), 1)


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
            mp.setattr(srv.recovery_mechanism, "mesh_reset_spent", lambda *a: False)
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
        # Old: 120 reset, 252 deferred; 600 reset again, 732 sweep, 924 cycle. New: 252 sweep, 444 cycle.
        (1, 132, 924.0, 444.0),
        # B2, four chips off; each mesh reset overran to 263 s (exit 1, the same four still off).
        # Old: 120 reset, 383 deferred; 600 reset again, 863 sweep, 1186 cycle. New: 383 sweep, 706 cycle.
        (4, 263, 1186.0, 706.0),
    ],
    ids=["b1-one-chip", "b2-four-chips"],
)
async def test_partial_offbus_power_cycles_right_after_the_failed_reset(
    monkeypatch, clear_job_state, off_bus, reset_sec, old_cycle_at, new_cycle_at
):
    """Spec 04 I38: a partial off-bus drop whose one mesh reset failed on an unchanged set gets the
    last-chance sweep at once and reaches the power cycle at its end, not at the 600 s ceiling window."""
    old_at, old_resets = await _replay_from_onset(monkeypatch, old=True, off_bus=off_bus, reset_sec=reset_sec)
    new_at, new_resets = await _replay_from_onset(monkeypatch, old=False, off_bus=off_bus, reset_sec=reset_sec)
    assert (old_at, old_resets) == (old_cycle_at, 2)
    assert (new_at, new_resets) == (new_cycle_at, 1)
    assert new_at - ONE_SHOT - reset_sec - (reset_sec + 60) < 5, "no wait but the sweep before the power cycle"


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
    assert counters["resets"] == 1 and counters["power_cycles"] == 1 and counters["sweeps"] == 1


@pytest.mark.asyncio
async def test_partial_offbus_after_the_failed_reset_waits_only_for_the_power_cycle_cooldown(
    monkeypatch, clear_job_state
):
    """I38 with I65: the host booted 3 min ago and one chip stays off after the failed mesh reset. The
    climb still skips the ceiling wait (I38); the sweep runs only once the cycle may fire, and the power cycle waits out the cooldown
    counted from that boot, with its deadline set for the watchdog; no reset fires meanwhile (I36,
    I58). /health shows the deadline while it waits, the watchdog asks again at that deadline, and the
    cycle fires once the cooldown has run out: bounded, never open-ended. Nothing releases the hold."""
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
    assert "host_rung_not_deferred" in events and counters["sweeps"] == 0, "I38: no ceiling wait"
    assert counters["resets"] == 1 and counters["power_cycles"] == 0, "I65: the cycle waits for the cooldown"
    until = int(boot[0]) + POWER_CYCLE_MIN_INTERVAL_SEC
    assert mech.power_cycle_retry[0] == until, "the watchdog asks again at the cooldown's end"
    assert mech.power_cycle_cooldown_state["cause"] == "host boot"
    # /health shows the bounded wait: the rung it waits for and the moment it is asked again.
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("chip off", srv.device_hold_episode_since))
    shown = srv._health_payload()["power_cycle_cooldown"]
    assert shown["escalation_stage"] == "power-cycle" and shown["cause"] == "host boot"
    assert shown["next_deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")
    clock[0] = 600.0
    assert await _window() is True
    assert counters["resets"] == 1 and counters["power_cycles"] == 0 and counters["sweeps"] == 0
    assert srv._health_payload()["power_cycle_cooldown"]["next_deadline"] == shown["next_deadline"]
    # The watchdog asks again the moment the cooldown ends, not at the next ceiling window.
    assert not mech.power_cycle_retry_due(until - 1)
    spawned = _drive_watchdog_at(monkeypatch, datetime.fromtimestamp(until))
    assert spawned[:1] == ["power_cycle_cooldown"], "the cooldown's end is asked first"
    assert "power_cycle_cooldown" not in srv._health_payload(now=datetime.fromtimestamp(until))
    boot[0] -= POWER_CYCLE_MIN_INTERVAL_SEC  # the cooldown has run out
    assert await _window() is True  # the spawned escalation
    assert counters["resets"] == 1 and counters["power_cycles"] == 1 and counters["sweeps"] == 1
    assert mech.power_cycle_cooldown_state is None and "power_cycle_cooldown" not in srv._health_payload()
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "nothing released the hold"


def _drive_watchdog_at(monkeypatch, now):
    """Run the hold watchdog once at ``now`` (one-shot already spent, windows claimed); returns the
    spawn kinds it chose."""
    spawned = []
    monkeypatch.setattr(
        srv, "_maybe_spawn_forced_escalation", lambda: spawned.append(srv.device_hold_escalation_kind) or True
    )
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)
    srv.fsm.set_latch("escalated", False)
    srv._check_hold_deadline(now=now)
    return spawned


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
async def test_e3_offbus_classes_reset_once_then_take_the_next_rung(monkeypatch, clear_job_state, off_bus, power_cycle):
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
        assert counters["sweeps"] == 1, "the last-chance sweep runs right before the power cycle"
    else:
        pages = [a for a in alerts if a["kind"] == "offbus_gated_hold_page"]
        assert len(pages) == 1 and opc._pending["handoff"]["latched"], "handed to an operator at once"
    for w in range(1, WINDOWS):
        clock[0] = max(clock[0], 600.0 * w)
        assert await _window() is True
    assert counters["resets"] == 1, "the failed reset is never repeated on the unchanged set"
    assert _skipped(events) >= WINDOWS - 1, "every later window skips it"
    if not power_cycle:
        assert len([a for a in alerts if a["kind"] == "offbus_gated_hold_page"]) == 1, "one page per episode"
        assert counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


@pytest.mark.asyncio
@pytest.mark.parametrize("host_rung", ["power-cycle", "none"])
async def test_e3_present_mesh_resets_once_then_takes_the_next_rung(monkeypatch, clear_job_state, host_rung):
    """D (every chip present, the mesh reset fails its verify) over six forced windows: one reset,
    then the host rung in that same window (the power cycle, on by default; there is no reboot rung),
    and with it opted out no further reset at all: the hold stays for an operator."""
    counters, events = _present_mesh(monkeypatch)
    if host_rung != "power-cycle":
        monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setattr(privileges, "is_root", lambda: True)
    monkeypatch.setattr(privileges, "has_systemd", lambda: True)
    order = _record_cycle_order(monkeypatch, counters)
    assert await _present_window(True) is True
    assert counters["resets"] == 1
    fired = {"power-cycle": order, "none": []}[host_rung]
    assert fired == ([] if host_rung == "none" else [1]), "the host rung fired right after the failed reset"
    for _ in range(1, WINDOWS):
        assert await _present_window(True) is True
    assert counters["resets"] == 1, "the failed reset is never repeated on the unchanged mesh"
    assert _skipped(events) == WINDOWS - 1
    if host_rung == "none":
        assert counters["power_cycles"] == 0 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


# --- spec 04 I87: one mesh reset per hold episode, a changed set included -------------------------


def _beats_with_off(off):
    return {str(c): 100 for c in range(EXPECTED) if str(c) not in off}


@pytest.mark.asyncio
@pytest.mark.parametrize("power_cycle", [True, False], ids=["power-cycle", "no-host-rung"])
async def test_a_failed_reset_that_shrank_the_set_is_not_repeated_and_climbs(monkeypatch, clear_job_state, power_cycle):
    """The one mesh reset fails but brings a chip back (2 -> 1 off). No path runs a second mesh reset in
    the episode, on the new set or any other (spec 04 I87): the power cycle fires in that window, or with
    no host rung the hold goes to an operator once. Nothing releases the hold."""
    clock = [ONE_SHOT]
    counters, events, alerts = _episode(monkeypatch, power_cycle=power_cycle, clock=clock, off_bus=2)
    state = {"off": {"30", "31"}}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: _beats_with_off(state["off"]))
    srv.isolated_chips = set()
    order = _record_cycle_order(monkeypatch, counters)
    reset = srv.galaxy_recovery._reset_and_verify_device

    async def shrinking_reset(indices, log):
        out = await reset(indices, log)
        state["off"] = {"31"}
        return out

    patch_recovery(monkeypatch, "_reset_and_verify_device", shrinking_reset)
    for w in range(WINDOWS):
        clock[0] = max(clock[0], 600.0 * w, ONE_SHOT)
        assert await _window() is True
    assert counters["resets"] == 1, "a shrunk set gets no second mesh reset in the episode"
    assert _skipped(events) >= WINDOWS - 1
    if power_cycle:
        assert order[:1] == [1], "the power cycle follows the one failed reset"
    else:
        assert len([a for a in alerts if a["kind"] == "offbus_gated_hold_page"]) == 1, "one page per episode"
        assert counters["power_cycles"] == 0
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
    assert counters["resets"] == 1 and _skipped(events), "after the restart the failed reset is not run again"
    _restart(monkeypatch)
    monkeypatch.setattr(srv.recovery_mechanism, "_current_boot_id", lambda: "another-boot")
    assert not srv.recovery_mechanism.rung_failed_unchanged("", "mesh_reset", _ids()), "a new boot starts fresh"
    assert await _window() is True and counters["resets"] == 2
