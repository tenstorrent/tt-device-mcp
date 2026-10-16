# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A health probe the host shutdown or the broker's own stop killed is not a verdict (03 I58).

The field case: an operator rebooted the host while a hold-deadline escalation ran its post-reset
verify. systemd SIGTERMed the fabric validator with the broker, the check "exited -15", and the
escalation asked for a host reboot over it. Only the stand-in probes here are spawned (``sh``);
systemd is never asked: ``shutdown_in_progress`` is stubbed.
"""

import asyncio

import pytest

from tests.test_dispatch_failure_reset import _gate, mesh  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import evidence, shutdown
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import fabric, subproc
from tt_device_mcp.health.shutdown import CheckInterruptedByShutdown

STOPPING = "host shutting down (reboot.target job start running)"
SUICIDE = ["sh", "-c", "kill -TERM $$"]


@pytest.fixture(autouse=True)
def _not_stopping(monkeypatch):
    monkeypatch.setattr(shutdown, "_broker_stopping", "")


def _stopping(monkeypatch, why=STOPPING):
    asked = []

    def probe():
        asked.append(True)
        return why

    monkeypatch.setattr(shutdown, "shutdown_in_progress", probe)
    return asked


async def _run(argv, timeout=10.0):
    return await subproc.run_probe(argv, timeout_sec=timeout, track=lambda p: None, stream=True, env={}, name="fabric")


# ---- the probe runner ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_probe_killed_during_shutdown_raises_and_is_journaled(monkeypatch):
    _stopping(monkeypatch)
    with pytest.raises(CheckInterruptedByShutdown) as e:
        await _run(SUICIDE)
    assert (e.value.probe, e.value.rc, e.value.why) == ("fabric", -15, STOPPING)
    assert isinstance(e.value, asyncio.CancelledError), "callers unwind it as a cancellation"
    evs = [x for x in evidence.read_health_events() if x["kind"] == "check_interrupted_by_shutdown"]
    assert len(evs) == 1 and evs[0]["probe"] == "fabric" and evs[0]["rc"] == -15


@pytest.mark.asyncio
async def test_a_signal_outside_shutdown_keeps_the_old_failure(monkeypatch):
    asked = _stopping(monkeypatch, why="")
    rc, _ = await _run(SUICIDE)
    assert rc == -15 and asked == [True]


@pytest.mark.asyncio
async def test_a_timeout_kill_keeps_the_old_failure_even_during_shutdown(monkeypatch):
    asked = _stopping(monkeypatch)
    rc, _ = await _run(["sh", "-c", "sleep 5"], timeout=0.2)
    assert rc is None, "the broker's own timeout still reports a timeout"
    assert asked == [], "a timeout never asks whether the host is stopping"


@pytest.mark.asyncio
async def test_a_clean_or_failing_exit_never_asks(monkeypatch):
    asked = _stopping(monkeypatch)
    assert (await _run(["sh", "-c", "exit 1"]))[0] == 1
    assert (await _run(["sh", "-c", "exit 0"]))[0] == 0
    assert asked == []


# ---- the fabric verdict ----------------------------------------------------------------------


@pytest.fixture
def fab(monkeypatch, health_deps):
    """The field host's shape: an operator wrapper whose exit code is the verdict, so a -15 reads
    as UNHEALTHY (the built-in path already reads an unrecognised -15 as could-not-run)."""
    m = HealthMonitor(health_deps)
    state = {"rc": -15}

    async def check(argv, env, **kw):
        if state.get("raise"):
            raise state["raise"]
        return state["rc"], "fabric-check: running run_cluster_validation --send-traffic"

    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "fabric-wrapper")
    monkeypatch.setattr(m, "_begin_action_row", lambda *a, **k: None)
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(fabric, "build_command", lambda descriptor=True: (["fabric-wrapper"], {}))
    monkeypatch.setattr(fabric, "check", check)
    return m, state


@pytest.mark.asyncio
async def test_fabric_signal_and_timeout_outside_shutdown_still_fail(fab):
    m, state = fab
    ok, detail = await m.verify_fabric_health(60)
    assert ok is False and "exited -15" in detail
    state["rc"] = None
    ok, detail = await m.verify_fabric_health(60)
    assert ok is False and "timed out" in detail


@pytest.mark.asyncio
async def test_fabric_interrupted_by_shutdown_is_journaled_and_not_a_verdict(fab):
    m, state = fab
    state["raise"] = CheckInterruptedByShutdown("fabric", -15, STOPPING)
    with pytest.raises(CheckInterruptedByShutdown):
        await m.verify_fabric_health(60)
    evs = [x for x in evidence.read_health_events() if x["kind"] == "fabric_check_interrupted"]
    assert evs and evs[-1]["how"] == "shutdown"
    assert m.last_fabric_ok is not False, "no failed fabric verdict was recorded"


def test_tt_smi_killed_during_shutdown_raises_else_fails(monkeypatch, health_deps):
    m = HealthMonitor(health_deps)

    class _Done:
        returncode, stdout, stderr = -15, "", ""

    monkeypatch.setattr(monitor_mod.subprocess, "run", lambda *a, **k: _Done())
    _stopping(monkeypatch, why="")
    ok, detail = m._verify_device_health_body(32)
    assert ok is False and "exited -15" in detail
    _stopping(monkeypatch)
    with pytest.raises(CheckInterruptedByShutdown):
        m._verify_device_health_body(32)


# ---- the escalation: no reboot, no climb, no release -----------------------------------------


@pytest.mark.asyncio
async def test_post_reset_verify_killed_by_shutdown_requests_no_reboot_and_releases_nothing(mesh):  # noqa: F811
    """The field sequence: a prior cycle failed, the reset fires, its post-reset verify is killed by
    the reboot. Without I58 this climbs to the host rung (see test_dispatch_failure_reset's
    test_a_failed_post_reset_check_stays_held_and_climbs, the same setup, which reboots once)."""
    mesh["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    real = srv.galaxy_recovery._verify_device
    calls = {"n": 0}

    async def verify(expected, log, run_fabric=True, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CheckInterruptedByShutdown("fabric", -15, STOPPING)
        return await real(expected, log, run_fabric=run_fabric, **kw)

    for r in (srv.galaxy_recovery, srv.per_target_recovery):
        r._verify_device = verify
    before = srv.fsm.state
    with pytest.raises(asyncio.CancelledError):
        await _gate()
    assert mesh["reboots"] == 0, "no host reboot is requested over a check the shutdown killed"
    assert "stuck_hold_host_escalation" not in mesh["events"]
    assert "last_chance_reset_sweep" not in mesh["events"], "no further rung"
    assert "device_released" not in mesh["events"]
    assert srv.fsm.state is not ServerState.HEALTHY and srv.fsm.state is before, "the device stays as it was"


# ---- shutdown detection ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "answers,expected",
    [
        ({"is-system-running": "stopping\n"}, "host shutting down (system state stopping)"),
        ({"list-jobs": "1234 reboot.target start waiting\n"}, "host shutting down (reboot.target job start waiting)"),
        ({"show": "deactivating\n"}, "broker stopping (tt-device-broker.service deactivating)"),
        ({"is-system-running": "running\n", "list-jobs": "77 foo.service start running\n", "show": "active\n"}, ""),
        ({}, ""),
    ],
)
def test_shutdown_in_progress_reads_systemd(monkeypatch, answers, expected):
    monkeypatch.setattr(shutdown.shutil, "which", lambda name: "/bin/systemctl")
    monkeypatch.setattr(shutdown, "_systemctl", lambda *args: answers.get(args[0], ""))
    monkeypatch.setattr(shutdown, "_own_unit", lambda: "tt-device-broker.service")
    assert shutdown.shutdown_in_progress() == expected


def test_the_broker_stop_signal_counts_without_systemd(monkeypatch):
    monkeypatch.setattr(shutdown.shutil, "which", lambda name: None)
    assert shutdown.shutdown_in_progress() == ""
    shutdown.note_broker_stopping(15)
    assert shutdown.shutdown_in_progress() == "broker received SIGTERM"
