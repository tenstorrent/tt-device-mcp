# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A multi-tray Galaxy settles after a reset before the full check its fabric debt owes (spec 04 I32).

The debt keeps the time of its newest reset; the gate waits out what is left of POST_RESET_SETTLE_SEC
since then, with the device held, then runs the full check unchanged. Nothing else waits.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty
from tests.test_device_safety import _clean_post_job_gate
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import post_reset
from tt_device_mcp.health.post_reset import FABRIC_DEBT, POST_RESET_SETTLE_SEC, FabricDebt

_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _fresh_settle():
    FABRIC_DEBT.reset_monotonic = FABRIC_DEBT.settle_served = 0.0
    yield
    FABRIC_DEBT.reset_monotonic = FABRIC_DEBT.settle_served = 0.0


# ---- the debt keeps its reset's time ----


def test_the_debt_stores_the_reset_time_and_keeps_the_latest():
    d = FabricDebt()
    d.note_reset("tenant job a", lambda *a, **k: None, at=100.0)
    assert d.reset_monotonic == 100.0
    d.note_reset("tenant job b", at=150.0)
    assert (d.owed, d.reset_monotonic) == ("tenant job b", 150.0)
    d.note_reset("adopted scope", at=120.0)
    assert d.reset_monotonic == 150.0, "an older reset must not move the settle's start back"
    d.note_fabric(True)
    d.note_reset("PCI rescan", lambda *a, **k: None, at=10.0)
    assert d.reset_monotonic == 10.0, "a fresh debt starts from its own reset"


def test_a_reset_with_no_time_given_is_now_and_a_killed_pass_is_no_reset(monkeypatch):
    d = FabricDebt()
    monkeypatch.setattr(post_reset.time, "monotonic", lambda: 500.0)
    d.note_reset("tt-smi -glx_reset", lambda *a, **k: None)
    assert d.reset_monotonic == 500.0
    k = FabricDebt()
    k.note_fabric_killed("fabric check killed", lambda *a, **k: None)
    assert k.owed and k.settle_left(POST_RESET_SETTLE_SEC) == 0.0


@pytest.mark.parametrize(
    "reset_at, now, served, want",
    [
        (1000.0, 1030.0, False, POST_RESET_SETTLE_SEC - 30),
        (1000.0, 1000.0 + POST_RESET_SETTLE_SEC + 5, False, 0.0),
        (1000.0, 1030.0, True, 0.0),
        (0.0, 1030.0, False, 0.0),
        (2000.0, 1030.0, False, POST_RESET_SETTLE_SEC),
    ],
    ids=["young", "old", "already-waited", "time-unknown", "clock-skew-bounded"],
)
def test_settle_left(reset_at, now, served, want):
    d = FabricDebt()
    d.note_reset("x", lambda *a, **k: None, at=reset_at)
    if served:
        d.note_settle_served()
    assert d.settle_left(POST_RESET_SETTLE_SEC, now=now) == pytest.approx(want)
    d.note_fabric(True)
    assert d.settle_left(POST_RESET_SETTLE_SEC, now=now) == 0.0, "nothing owed waits for nothing"


@pytest.mark.parametrize("uptime, recent", [("40.0 100.0\n", True), ("86400.0 1.0\n", False)])
def test_a_broker_start_owes_from_the_boot(monkeypatch, tmp_path, uptime, recent):
    f = tmp_path / "uptime"
    f.write_text(uptime)
    monkeypatch.setattr(post_reset, "_UPTIME", str(f))
    d = FabricDebt()
    d.note_reset("broker start", lambda *a, **k: None, at=post_reset.boot_monotonic())
    left = d.settle_left(POST_RESET_SETTLE_SEC)
    assert left == (pytest.approx(POST_RESET_SETTLE_SEC - 40.0, abs=1.0) if recent else 0.0)


def test_an_unreadable_uptime_reads_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(post_reset, "_UPTIME", str(tmp_path / "missing"))
    assert post_reset.boot_monotonic() == 0.0


# ---- the gate ----


def _gate(monkeypatch, tmp_path, *, chips=32, mode="galaxy", fabric=(True, "links healthy")):
    """A clean post-job gate (every probe stubbed) on a ``chips``-chip host; records each settle wait and what held
    while it ran."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", mode)
    calls = _clean_post_job_gate(
        monkeypatch, tmp_path, eth=(True, "all active eth cores advancing"), chips=chips, fabric=fabric
    )
    calls["waits"] = []

    async def sleep(sec, *a, **k):
        if sec >= 1:
            calls["waits"].append(
                {
                    "sec": sec,
                    "fabric_ran": calls["fabric"],
                    "lock": srv.device_op_active,
                    "debt": srv._post_reset_debt(),
                    "bound": srv._hold_bound_sec(),
                }
            )
            return None
        return await _REAL_SLEEP(sec, *a, **k)

    monkeypatch.setattr(srv.asyncio, "sleep", sleep)
    return calls


@pytest.mark.asyncio
async def test_a_young_tenant_reset_waits_only_the_remainder_then_runs_the_full_check(monkeypatch, tmp_path):
    calls = _gate(monkeypatch, tmp_path)
    FABRIC_DEBT.note_reset("tenant job j1", at=srv.time.monotonic() - 30)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert len(calls["waits"]) == 1
    w = calls["waits"][0]
    assert POST_RESET_SETTLE_SEC - 32 <= w["sec"] <= POST_RESET_SETTLE_SEC - 29
    # Held throughout: inside the gate's device lock, the debt still refusing release, no check run yet.
    assert w["fabric_ran"] == 0 and w["lock"].startswith("health-gate/") and w["debt"] == "tenant job j1"
    assert calls["fabric"] == 1, "the full check runs unchanged after the settle"
    assert FABRIC_DEBT.owed == "" and srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, chips, mode, age",
    [
        ("old-reset", 32, "galaxy", POST_RESET_SETTLE_SEC + 10),
        ("nothing-owed", 32, "galaxy", None),
        ("single-tray", 8, "galaxy", 5),
        ("not-a-galaxy", 32, "target", 5),
    ],
)
async def test_no_wait(monkeypatch, tmp_path, case, chips, mode, age):
    calls = _gate(monkeypatch, tmp_path, chips=chips, mode=mode)
    if age is not None:
        FABRIC_DEBT.note_reset("tt-smi -glx_reset", at=srv.time.monotonic() - age)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert calls["waits"] == []
    assert calls["fabric"] == (0 if age is None else 1), "between jobs with no reset adds nothing (goal 5)"


@pytest.mark.asyncio
async def test_one_reset_is_waited_for_once(monkeypatch, tmp_path):
    """A gate whose full check could not run leaves the debt owed; the next gate on the same reset runs the check again
    with no second wait."""
    calls = _gate(monkeypatch, tmp_path, fabric=(None, "fabric check could not run (77)"))
    FABRIC_DEBT.note_reset("tenant job j0", at=srv.time.monotonic() - 10)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert FABRIC_DEBT.owed == "tenant job j0"
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert len(calls["waits"]) == 1 and calls["fabric"] == 2


@pytest.mark.asyncio
async def test_a_ladder_reset_inside_the_gate_adds_no_second_wait(monkeypatch, tmp_path):
    """The ladder's resets run inside the gate's observe, after its one settle; its own post-reset pass keeps its own
    77 retry wait and gets no settle on top."""
    calls = _gate(monkeypatch, tmp_path)
    real_observe = srv.fsm.observe

    async def observe(*a, **k):
        FABRIC_DEBT.note_reset("tt-smi -glx_reset")  # a ladder rung's reset, this very moment
        return await real_observe(*a, **k)

    monkeypatch.setattr(srv.fsm, "observe", observe)
    FABRIC_DEBT.note_reset("tenant job j2", at=srv.time.monotonic() - 5)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert len(calls["waits"]) == 1


# ---- goal 10: the settle never delays the 10-min bound ----


def _open_hold(monkeypatch, age):
    fsm_dirty(srv, "chip 3 off the bus", why="heartbeat")
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=age)).isoformat())


@pytest.mark.parametrize("hold_age, want", [(100, POST_RESET_SETTLE_SEC - 5), (580, 20), (600, 0.0), (900, 0.0)])
def test_the_settle_ends_by_the_hold_bound(monkeypatch, tmp_path, hold_age, want):
    calls = _gate(monkeypatch, tmp_path)
    del calls
    _open_hold(monkeypatch, hold_age)
    FABRIC_DEBT.note_reset("tenant job j3", at=srv.time.monotonic() - 5)
    left = srv._post_reset_settle_left(32, srv.galaxy_recovery)
    assert left == pytest.approx(want, abs=1.0)
    assert srv._hold_age_now() + left <= max(srv._hold_bound_sec(), srv._hold_age_now()) + 1.0


@pytest.mark.asyncio
async def test_the_bound_and_the_hold_clock_are_untouched_while_settling(monkeypatch, tmp_path):
    calls = _gate(monkeypatch, tmp_path)
    _open_hold(monkeypatch, 120)
    since = srv.device_hold_episode_since
    FABRIC_DEBT.note_reset("tenant job j4", at=srv.time.monotonic() - 1)
    waited = await srv._post_reset_settle(32, srv.galaxy_recovery, lambda m: None)
    assert waited == pytest.approx(POST_RESET_SETTLE_SEC - 1, abs=1.0)
    assert calls["waits"][0]["bound"] == srv._hold_bound_sec() == 600
    assert srv.device_hold_episode_since == since, "the wait counts in the hold's own clock"
    assert srv.fsm.state is not ServerState.HEALTHY
