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
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery import offbus_power_cycle as opc
from tt_device_mcp.health.recovery.base import RESET_RECORD_FILE

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
    """Each episode starts on a boot with no failed reset recorded (spec 04 I38)."""
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
async def test_with_a_power_cycle_the_next_window_sweeps_then_power_cycles(monkeypatch, clear_job_state):
    """An early window's reset fails before the ceiling (the host rung defers); the next window skips
    the reset and takes the next stronger rung: one last-chance sweep, then the power cycle."""
    clock = [0.0]
    counters, events, _ = _episode(monkeypatch, power_cycle=True, clock=clock)
    assert await _window() is True
    assert counters["resets"] == 1 and "host_rung_deferred_until_ladder_end" in events
    assert counters["power_cycles"] == 0
    clock[0] = 600.0  # the next hold-deadline window, at the ceiling
    assert await _window() is True
    assert SKIP in events
    assert counters["resets"] == 1, "the failed reset is not run again before the stronger rung"
    assert counters["sweeps"] == 1 and counters["power_cycles"] == 1 and counters["reboots"] == 0
    assert counters["dirty_cleared"] == 0


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
    """The record is keyed by the boot, not the episode (spec 04 I38): the same off-bus set in a new
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
    clock[0] = 600.0
    assert await _window() is True
    assert len(fired) == 1 and SKIP in events, "the failed reset is not repeated"
    assert counters["sweeps"] == 1 and counters["power_cycles"] == 1, "it went on to the next rung"
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
async def test_replay_the_power_cycle_fires_one_reset_sooner(monkeypatch, clear_job_state):
    """Partial off-bus drop, power cycle armed. Old: 0 reset, 263 defer; 600 reset again, 863 sweep,
    1186 cycle. New: 600 skip, sweep, 923 cycle: 263 s sooner per avoided repeat."""
    old_at, old_resets = await _replay(monkeypatch, old=True, power_cycle=True, windows=4)
    new_at, new_resets = await _replay(monkeypatch, old=False, power_cycle=True, windows=4)
    assert (old_at, old_resets) == (1186.0, 2)
    assert (new_at, new_resets) == (923.0, 1)
    assert new_at < old_at and old_at - new_at == RESET_SEC


@pytest.mark.asyncio
async def test_replay_with_no_host_rung_the_reset_loop_ends_in_one_hand_off(monkeypatch, clear_job_state):
    """No host rung: old fires the same reset every window (six in the incident); new fires one, then
    hands the hold to an operator."""
    old_at, old_resets = await _replay(monkeypatch, old=True, power_cycle=False, windows=6)
    new_at, new_resets = await _replay(monkeypatch, old=False, power_cycle=False, windows=6)
    assert old_at is None and new_at is None
    assert old_resets == 6 and new_resets == 1


# --- the record across a broker restart and a new boot (spec 04 I38) ------------------------------


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
