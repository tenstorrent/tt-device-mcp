# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The startup gate counts an operator reset's passed full check as its own pass (spec 03 I56).

A broker restart arms the forced startup pass; an operator reset run during the startup hold
verified the mesh with the fabric pass and released it, and the startup gate, queued behind that
reset on the device lock, then ran a second full pass (~30 s on a galaxy) over a mesh nothing had
touched since. Only a verify that started after the gate was armed, with the 1G pool ready, and
released a device still clean with no fabric pass owed counts; anything else runs the pass.
"""

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_device_safety import _clean_post_job_gate, _restart_broker
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import post_reset
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.post_reset import FABRIC_DEBT

FOK = {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "fabric links healthy"}}
FBAD = {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": "2 links failed"}}
F77 = {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}


@pytest.fixture
def started(monkeypatch, tmp_path, clear_job_state):
    """A 32-chip galaxy with a fabric check and its pool ready. Returns the gate's call counts and
    the journal rows; ``_start`` then starts the broker."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    dev = tmp_path / "dev"
    dev.mkdir()
    calls = _clean_post_job_gate(monkeypatch, dev, eth=(True, "eth ok"), chips=32)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(post_reset, "health_event", lambda kind, **f: events.append((kind, f)))
    return calls, events


async def _start(monkeypatch, tmp_path, name="fsm.json"):
    """One broker start: the startup hold is up and the startup gate armed and owed."""
    await _restart_broker(monkeypatch, tmp_path / name)
    srv.last_fabric_check_monotonic = 0.0
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == "startup_unverified"
    assert FABRIC_DEBT.owed == "broker start"


async def _operator_reset(monkeypatch, evidence):
    """The operator reset's verify returns ``evidence``; the gate after it runs its own probes."""

    async def verify(expected, log, run_fabric=True, **_):
        return evidence["fabric"]["ok"] is not False, dict(evidence)

    with monkeypatch.context() as m:
        patch_recovery(m, "_verify_device", verify)
        verdict, _ = await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")
    return verdict


async def _startup_gate():
    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)


def _counted(events):
    return [f for k, f in events if k == "startup_gate_counted_reset_verify"]


@pytest.mark.asyncio
async def test_a_passed_reset_verify_after_the_start_is_the_startup_pass(monkeypatch, started, tmp_path):
    """cs04: the verify passed with fabric and released the startup hold; the startup gate, 78 s
    behind it on the lock, then ran a second 30 s fabric pass. Now it counts the verify."""
    calls, events = started
    await _start(monkeypatch, tmp_path)
    assert await _operator_reset(monkeypatch, FOK) is True
    assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""

    await _startup_gate()

    assert calls["fabric"] == 0 and calls["eth"] == 0 and calls["resets"] == 0, "no second pass"
    assert len(_counted(events)) == 1
    assert srv.fsm.state is ServerState.HEALTHY
    assert srv.last_fabric_check_monotonic == srv._reset_verify_fabric_ok_monotonic > 0, (
        "the verify's pass is the newest fabric pass: the first run_fabric gate must not pay it again"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence, verdict", [(FBAD, False), (F77, None)], ids=["fabric-failed", "fabric-77"])
async def test_a_failed_or_unverified_reset_verify_keeps_the_hold_and_the_startup_gate_runs(
    monkeypatch, started, tmp_path, evidence, verdict
):
    calls, events = started
    await _start(monkeypatch, tmp_path)
    assert await _operator_reset(monkeypatch, evidence) is verdict
    assert srv.fsm.state is ServerState.RECOVERING, "a failed verify never releases"

    await _startup_gate()

    assert _counted(events) == []
    assert calls["fabric"] + calls["resets"] >= 1, "the startup gate ran its own check"


@pytest.mark.asyncio
async def test_a_reset_verify_started_before_the_pool_was_ready_does_not_count(monkeypatch, started, tmp_path):
    """The startup check never runs before the 1G pool is ready, so neither may its stand-in."""
    calls, events = started
    await _start(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: (5, 32))
    assert await _operator_reset(monkeypatch, FOK) is True
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)

    await _startup_gate()

    assert _counted(events) == []
    assert calls["fabric"] == 1, "the startup gate ran its own fabric pass"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_reset_verify_from_before_the_start_does_not_count(monkeypatch, started, tmp_path):
    calls, events = started
    await _start(monkeypatch, tmp_path)
    assert await _operator_reset(monkeypatch, FOK) is True
    # A second broker start arms the gate again: the earlier verify predates it.
    await _start(monkeypatch, tmp_path, "fsm2.json")

    await _startup_gate()

    assert _counted(events) == []
    assert calls["fabric"] == 1
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_reset_after_the_verify_runs_the_startup_gate(monkeypatch, started, tmp_path):
    """A later reset owes a fabric pass again: the verify no longer speaks for the mesh."""
    calls, events = started
    await _start(monkeypatch, tmp_path)
    assert await _operator_reset(monkeypatch, FOK) is True
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")

    await _startup_gate()

    assert _counted(events) == []
    assert calls["fabric"] == 1
    assert FABRIC_DEBT.owed == "" and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_device_dirtied_after_the_verify_runs_the_startup_gate(monkeypatch, started, tmp_path):
    calls, events = started
    await _start(monkeypatch, tmp_path)
    assert await _operator_reset(monkeypatch, FOK) is True
    srv._mark_device_dirty("job 7 crashed", why="job_killed")

    await _startup_gate()

    assert _counted(events) == []
    assert calls["fabric"] + calls["resets"] >= 1
