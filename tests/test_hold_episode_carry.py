# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The hold episode carries across a quick release (spec 04 I82).

The 10-min clock of the hold bound (I76) counts from the first moment the device stopped being healthy
in the episode. A release that a new hold follows within _HOLD_CARRY_SEC, or around the first job started
in that window, does not restart it, nor does a latch lift, a failed first job, a class change or a broker
restart. A bound ladder whose check released a device that is held again did not heal it, so the next
bound step is the host rung, not another ladder. No test reaches a real reset or power cycle."""

import time
from datetime import datetime, timedelta

import pytest

from tests.test_device_safety import _arm_fabric_only_deadline_hold
from tests.test_hold_bound import BOUND, MISMATCH, _drain, _watchdog
from tt_device_mcp import server as srv
from tt_device_mcp.health import evidence as health
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED

# Hold classes whose bound ladder can end in a release on a device that still has the fault:
# (fsm why, hold detail, fabric output, known-fault latch).
_CLASSES = {
    "missing port (topology mismatch)": (srv.FABRIC_RELIFT_WHY, "post-job: fabric unverified", MISMATCH, ""),
    "latched I42 topology hardware fault": (
        srv.FABRIC_RELIFT_WHY,
        "job 7: fabric topology hardware fault after a topology-mismatch lift",
        MISMATCH,
        "",
    ),
    "missing cable (known fabric fault)": (
        srv.FABRIC_RELIFT_WHY,
        "fabric HARDWARE FAULT: a cable is missing",
        "",
        "known fabric fault: a cable is missing",
    ),
    "77-only": (srv.FABRIC_RELIFT_WHY, "post-job: fabric check COULD NOT RUN (77)", "", ""),
    "generic": ("eth_frozen", "gate/post-job: eth/fabric fault on a present mesh", "", ""),
}


@pytest.fixture(autouse=True)
def _restore_broker_state(monkeypatch):
    """The broker keeps its hold state in module globals: put every one back after each test."""
    for name in list(vars(srv)):
        if name.startswith(("device_hold_", "_hold_")) and not callable(getattr(srv, name)):
            monkeypatch.setattr(srv, name, getattr(srv, name))


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-a")


def _age() -> float:
    return srv._hold_age_now()


def _open_hold(monkeypatch, hold, age_sec):
    """A hold of class ``hold`` opened through the gate verdict, its episode ``age_sec`` old."""
    why, detail, output, known = _CLASSES[hold]
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output
    )
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: known)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    srv._note_tenant_gate_verdict(detail)
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=age_sec)).isoformat()
    srv._persist_hold_episode(srv.device_hold_episode_since)
    return escalations, events, detail


async def _bound_ladder_releases(monkeypatch, escalations, events):
    """The bound's ladder ran and its check passed (on the discovered links), so the device was released."""
    srv._note_hold_bound_ladder(_age(), OUTCOME_RECOVERED)
    assert "host_rung" not in events
    srv._note_tenant_gate_verdict("")  # its check passed: released
    assert "device_released" in events and not srv.device_hold_episode_since


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", list(_CLASSES))
async def test_replay_a_release_after_the_bound_ladder_then_a_failed_first_job_goes_to_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state, hold
):
    """A Galaxy host, 2026-10-17: a missing-port hold reached the bound; its ladder's check passed on the
    discovered links and released the device; the first job died on the same missing link about 40 s later.
    The new hold started a new clock, so the host rung after the ladder was never due and the loop ran
    every ~13 min for an hour. Fails on base: the re-hold is seconds old and the next bound step is
    another ladder 10 min later."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, hold, BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)

    srv._note_hold_carry_job("start")  # the first job after the release...
    srv._note_hold_carry_job("end")  # ...dies on the fault the ladder did not heal
    srv._note_tenant_gate_verdict(f"job 8: {detail}")

    assert srv.device_hold_episode_since == since, "the re-hold continues the episode: its clock did not restart"
    assert _age() >= BOUND
    assert "hold_bound_carried" in events
    assert srv._hold_bound_cycle_due(_age()), "the ladder already ran this episode: the host rung is due now"

    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [], "no second ladder: the bound goes straight to the host rung"
    assert events.count("host_rung") == 1, "exactly one host-rung request"


@pytest.mark.asyncio
async def test_a_class_change_on_the_rehold_keeps_the_clock(monkeypatch, tmp_path, clear_job_state):
    """The episode is the device's, not the cause's: a re-hold naming another cause continues it."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    _esc, _events, _detail = _open_hold(monkeypatch, "missing port (topology mismatch)", 200)
    since = srv.device_hold_episode_since
    srv._note_tenant_gate_verdict("")
    srv._note_tenant_gate_verdict("gate/post-job: eth/fabric fault on a present mesh")
    assert srv.device_hold_episode_since == since
    assert 200 <= _age() < BOUND, "below the bound nothing else changes: the class paths keep their speed"
    assert not srv._hold_bound_cycle_due(_age())


@pytest.mark.asyncio
async def test_a_rehold_after_the_carry_window_with_no_job_is_a_new_episode(monkeypatch, tmp_path, clear_job_state):
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._hold_carry["released"] = time.time() - srv._HOLD_CARRY_SEC - 1
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert _age() < 60, "a hold long after a release with no job in between is a new episode"
    assert not srv._hold_bound_cycle_due(_age())


@pytest.mark.asyncio
async def test_a_job_started_in_the_window_keeps_the_carry_until_it_ends(monkeypatch, tmp_path, clear_job_state):
    """The first job after a release shows whether the release was right, however long it runs."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    since = srv.device_hold_episode_since
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._note_hold_carry_job("start")
    srv._hold_carry["released"] = time.time() - 3600  # the job ran an hour
    srv._note_hold_carry_job("end")
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert srv.device_hold_episode_since == since
    assert srv._hold_bound_cycle_due(_age())


@pytest.mark.asyncio
async def test_a_job_started_after_the_window_does_not_extend_it(monkeypatch, tmp_path, clear_job_state):
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    srv._hold_carry["released"] = time.time() - srv._HOLD_CARRY_SEC - 1
    srv._note_hold_carry_job("start")
    srv._note_hold_carry_job("end")
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert _age() < 60


@pytest.mark.asyncio
async def test_the_carry_does_not_cross_a_reboot(monkeypatch, tmp_path, clear_job_state):
    """A reboot or power cycle is the biggest reset there is: the next hold is a new episode."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    await _bound_ladder_releases(monkeypatch, escalations, events)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-b")
    monkeypatch.setattr(srv, "_hold_carry", None)
    monkeypatch.setattr(srv, "_hold_carry_loaded", False)  # a new process reads the carry from disk
    srv._note_tenant_gate_verdict(f"job 8: {detail}")
    assert _age() < 60


def _restart_broker(monkeypatch):
    """A new broker process: nothing in memory, only what the old one left on disk."""
    for name, value in (
        ("device_hold_logged", False),
        ("device_hold_episode_since", ""),
        ("_hold_episode_restorable", True),
        ("_hold_carry", None),
        ("_hold_carry_loaded", False),
        ("_hold_bound_episode", ""),
        ("_hold_bound_ladder_age", None),
        ("_hold_bound_ladder_recovered", False),
        ("_hold_bound_cycle_age", None),
        ("_hold_bound_check_failed_age", None),
    ):
        monkeypatch.setattr(srv, name, value)


@pytest.mark.asyncio
async def test_replay_a_restart_whose_startup_check_releases_then_a_failed_first_job_goes_to_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """The same host, 09:02: an autoupdate restarted the broker mid-episode; its startup check released the
    device on the discovered links and the first job re-held it with a new clock. Fails on base: the
    restart's release wiped the episode start, so the re-hold is seconds old."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 5)
    since = srv.device_hold_episode_since
    srv._note_hold_bound_ladder(BOUND + 5, OUTCOME_RECOVERED)  # the bound ladder ran before the restart
    _restart_broker(monkeypatch)

    srv._note_tenant_gate_verdict("")  # the startup check released it
    srv._note_tenant_gate_verdict(f"job 8: {detail}")  # the first job died on the same link

    assert srv.device_hold_episode_since == since, "the restart did not restart the episode clock"
    assert srv._hold_bound_cycle_due(_age()), "the ladder before the restart counts: the host rung is due"
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and events.count("host_rung") == 1


@pytest.mark.asyncio
async def test_a_hold_open_across_a_restart_escalates_at_once_when_past_the_bound(
    monkeypatch, tmp_path, clear_job_state
):
    """A hold that began before the restart escalates no later than 10 min after it began: here at once."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 120)
    since = srv.device_hold_episode_since
    _restart_broker(monkeypatch)

    srv._note_tenant_gate_verdict(detail)  # the startup check still fails: held from the start
    assert srv.device_hold_episode_since == since
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == ["present-forced"], "the full ladder runs on the first tick, not 10 min after the restart"


@pytest.mark.asyncio
async def test_a_hold_open_across_a_restart_after_its_bound_ladder_goes_to_the_host_rung(
    monkeypatch, tmp_path, clear_job_state
):
    """A restart after the bound's ladder ran keeps that ladder: the host rung follows, not a new ladder."""
    _isolate(monkeypatch, tmp_path)
    _watchdog(monkeypatch)
    escalations, events, detail = _open_hold(monkeypatch, "missing port (topology mismatch)", BOUND + 120)
    since = srv.device_hold_episode_since
    srv._note_hold_bound_ladder(BOUND + 5, OUTCOME_RECOVERED)
    _restart_broker(monkeypatch)

    srv._note_tenant_gate_verdict(detail)
    assert srv.device_hold_episode_since == since
    srv._check_hold_deadline(now=datetime.now())
    await _drain()
    assert escalations == [] and events.count("host_rung") == 1
