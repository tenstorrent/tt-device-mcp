# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""An eth-venv provisioned after the broker started arms the eth rung on a paced retry (spec 03 I28).

Seen on a box: the autoupdate installed the eth-venv minutes after a broker start whose self-test
found no python that imports ttexalens. That self-test marked the rung not retryable, so it stayed
off until the next broker restart hours later, although eth.resolve_python caches a miss for 60s only
so a venv provisioned later is picked up. A host with no reader is now retried at the idle pace
(ETH_CHECK_REARM_INTERVAL_SEC), inside a fault episode too, and never on every full pass.
"""

import os
import time

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.health.monitors import eth

from .conftest import fsm_dirty, fsm_healthy, patch_health_event

_STAND_IN_PYTHON = """#!/bin/sh
# Stand-in eth-venv python: imports ttexalens, and the probe reads every core advancing.
if [ "$1" = "-c" ]; then exit 0; fi
echo "all eth cores advancing"
exit 0
"""


class _Log:
    def __init__(self):
        self.lines = []

    def _add(self, level):
        return lambda msg, *a, **k: self.lines.append((level, msg))

    def __getattr__(self, level):
        return self._add(level)


@pytest.fixture
def late_venv(monkeypatch, tmp_path, clear_job_state):
    """A host whose eth-venv is not there yet. Returns a function that provisions it."""
    venv = tmp_path / "eth-venv"
    validator = tmp_path / "validator"
    (validator / "current").mkdir(parents=True)
    probe = tmp_path / "eth-heartbeat-probe.py"
    probe.write_text("# stand-in probe; the stand-in python never reads it\n")
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_PYTHON", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", raising=False)
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "")  # restored (unset) after the test arms it
    monkeypatch.delenv("TTDEV_ETH_CHECK_ARMED")
    monkeypatch.setenv("TTDEV_ETH_VENV", str(venv))
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(validator))
    monkeypatch.setenv("TTDEV_ETH_CHECK_PROBE", str(probe))
    monkeypatch.setenv("TTDEV_ETH_CHECK_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(eth, "DEFAULT_ETH_VENV_PYTHON", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(eth, "_python_cache", None)
    monkeypatch.setattr(srv, "eth_check_armed", False)
    monkeypatch.setattr(srv, "eth_check_disarm_reason", "startup self-test has not run yet")
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "readopted_scopes", {})
    log = _Log()
    monkeypatch.setattr(srv, "logger", log)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))

    def provision():
        python = venv / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(_STAND_IN_PYTHON)
        python.chmod(0o755)

    return provision, log, events


def _age_last_attempt():
    """Move the last self-test attempt, and resolve_python's cached miss, one full interval back."""
    srv._last_eth_rearm_monotonic -= srv.ETH_CHECK_REARM_INTERVAL_SEC + 1
    if eth._python_cache is not None:
        key, resolved, at = eth._python_cache
        eth._set_python_cache((key, resolved, at - srv.ETH_CHECK_REARM_INTERVAL_SEC - 1))


@pytest.mark.asyncio
async def test_a_venv_provisioned_after_start_arms_on_the_next_paced_idle_retry(late_venv):
    """Fails on base: the no-reader self-test left the rung not retryable, off until a restart."""
    provision, log, events = late_venv
    assert srv.ETH_CHECK_REARM_INTERVAL_SEC > eth.NEGATIVE_TTL_SEC, "a paced retry must re-resolve the python"
    await srv.selftest_eth_heartbeat()
    assert srv.eth_check_armed is False
    assert "no python that imports ttexalens" in srv.eth_check_disarm_reason
    assert srv.eth_rearm_retryable is True and srv.eth_rearm_no_reader is True
    assert srv._health_payload()["eth_heartbeat_rung"] == {
        "armed": False,
        "disarm_reason": srv.eth_check_disarm_reason,
        "armed_late": None,
    }
    fsm_healthy(srv)
    provision()
    srv._maybe_spawn_eth_rearm()
    assert srv._eth_rearm_task is None, "retried inside ETH_CHECK_REARM_INTERVAL_SEC"
    _age_last_attempt()
    srv._maybe_spawn_eth_rearm()
    assert srv._eth_rearm_task is not None
    await srv._eth_rearm_task
    assert srv.eth_check_armed is True
    assert os.environ.get("TTDEV_ETH_CHECK_ARMED") == "1"
    assert srv.eth_rearm_no_reader is False
    rung = srv._health_payload()["eth_heartbeat_rung"]
    assert rung["armed"] is True and rung["disarm_reason"] is None
    assert "no python that imports ttexalens" in rung["armed_late"]["was_off_for"]
    assert "eth_rung_armed_late" in events
    assert any(level == "info" and msg.startswith("RUNG ARMED eth-heartbeat (late)") for level, msg in log.lines)


@pytest.mark.asyncio
async def test_the_startup_arm_is_not_late(late_venv):
    provision, log, events = late_venv
    provision()
    await srv.selftest_eth_heartbeat()
    assert srv.eth_check_armed is True
    assert srv._health_payload()["eth_heartbeat_rung"]["armed_late"] is None
    assert "eth_rung_armed_late" not in events
    assert any(level == "info" and msg.startswith("RUNG ARMED eth-heartbeat: ") for level, msg in log.lines)


@pytest.mark.asyncio
async def test_a_still_missing_reader_warns_once_not_every_interval(late_venv):
    _provision, log, _events = late_venv
    fsm_healthy(srv)
    await srv.selftest_eth_heartbeat()
    for _ in range(3):
        _age_last_attempt()
        srv._maybe_spawn_eth_rearm()
        await srv._eth_rearm_task
    assert srv._eth_selftest_runs == 4
    warnings = [msg for level, msg in log.lines if level == "warning" and msg.startswith("RUNG OFF eth-heartbeat")]
    assert len(warnings) == 1, warnings
    assert "retried every 600s" in warnings[0]


@pytest.mark.asyncio
async def test_the_no_reader_retry_keeps_its_pace_inside_a_fault_episode(late_venv, monkeypatch):
    """A box with no ttexalens adds at most one resolve per interval, never one per post-reset check."""
    await srv.selftest_eth_heartbeat()
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    builds = []
    real_build = eth.build

    def _counted():
        builds.append(1)
        return real_build()

    monkeypatch.setattr(eth, "build", _counted)
    for _ in range(3):
        assert await srv._rearm_eth_before_check() == ""
    assert builds == [], "a no-reader retry ran unpaced inside a fault episode"
    _age_last_attempt()
    await srv._rearm_eth_before_check()
    await srv._rearm_eth_before_check()
    assert builds == [1], "the paced retry must run once per interval"


@pytest.mark.asyncio
async def test_a_hung_read_is_still_retried_unpaced_inside_a_fault_episode(late_venv, monkeypatch):
    """Only the no-reader cause is paced in an episode: a hung read stays a goal-8 retry at every reset."""
    monkeypatch.setattr(srv, "eth_rearm_retryable", True)
    monkeypatch.setattr(srv, "eth_rearm_no_reader", False)
    monkeypatch.setattr(srv, "_last_eth_rearm_monotonic", time.monotonic())
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    attempts = []

    async def _selftest():
        attempts.append(1)
        return ""

    monkeypatch.setattr(srv, "selftest_eth_heartbeat", _selftest)
    await srv._rearm_eth_before_check()
    await srv._rearm_eth_before_check()
    assert attempts == [1, 1]


def _stub_fabric(monkeypatch, ok, detail):
    mon = srv.health_monitor
    calls = []

    async def _snapshot_ok(expected):
        return True, "ok"

    async def _fabric():
        calls.append(1)
        return ok, detail

    monkeypatch.setattr(mon, "_verify_device", _snapshot_ok)
    monkeypatch.setattr(mon, "verify_fabric_health", _fabric)
    return calls


@pytest.mark.asyncio
async def test_a_missing_reader_never_delays_a_post_reset_release_and_fabric_still_decides(late_venv, monkeypatch):
    """Goal 8/11: with no reader at all the post-reset verify still runs the full check, fabric
    included, and only its verdict decides; the eth rung adds no retry and no wait."""
    await srv.selftest_eth_heartbeat()
    runs = srv._eth_selftest_runs
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    fabric_calls = _stub_fabric(monkeypatch, True, "all links healthy")
    t0 = time.monotonic()
    healthy, evidence, _retries = await srv.galaxy_recovery._verify_device_after_reset(2, lambda m: None)
    assert time.monotonic() - t0 < 5
    assert healthy
    assert fabric_calls == [1]
    assert evidence["eth_heartbeat"]["ok"] is None
    assert srv._eth_selftest_runs == runs, "a post-reset check inside the interval re-ran the self-test"


@pytest.mark.asyncio
async def test_a_missing_reader_does_not_pass_a_fabric_check_that_found_fewer_links(late_venv, monkeypatch):
    await srv.selftest_eth_heartbeat()
    fsm_dirty(srv, "gate/post-job: fabric unhealthy", why="job_killed")
    fabric_calls = _stub_fabric(monkeypatch, False, "fabric: 3 of 4 port connections up vs the cabling descriptor")
    healthy, _evidence, _retries = await srv.galaxy_recovery._verify_device_after_reset(2, lambda m: None)
    assert not healthy, "a post-reset check that found fewer links than the cabling expects passed"
    assert fabric_calls == [1]


def test_the_autoupdate_checks_the_new_eth_venv_imports_before_the_restart():
    """A fresh eth-venv is import-checked (and warmed) once before the broker restart, loudly on failure."""
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh").read_text()
    pip = script.index('eth-venv/bin/pip" install')
    check = script.index("""eth-venv/bin/python" -c 'import ttexalens'""", pip)
    assert check < script.index("systemctl restart tt-device-broker")
    assert "import ttexalens' FAILS" in script[check : check + 400]
