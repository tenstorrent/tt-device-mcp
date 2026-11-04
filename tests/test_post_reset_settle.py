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


def _gate(monkeypatch, tmp_path, *, chips=32, mode="galaxy", fabric=(True, "links healthy"), slice_sec=None):
    """A clean post-job gate (every probe stubbed) on a ``chips``-chip host; records each settle wait and what held
    while it ran. The settle sleeps in one slice unless ``slice_sec`` is given."""
    monkeypatch.setattr(srv, "POST_RESET_SETTLE_SLICE_SEC", slice_sec or 10 * POST_RESET_SETTLE_SEC)
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


# ---- a chip off the bus never waits for the settle (04 I37 and the off-bus paths keep their time) ----


def _observed(monkeypatch, calls):
    """Record, in ``calls["order"]``, each settle slice and each observe, and every post_reset_settle* event."""
    calls["order"], calls["events"] = [], []
    real_observe, real_event = srv.fsm.observe, srv.health_event

    async def observe(*a, **k):
        calls["order"].append("observe")
        return await real_observe(*a, **k)

    def event(name, **k):
        if name.startswith("post_reset_settle"):
            calls["events"].append((name, k))
        return real_event(name, **k)

    monkeypatch.setattr(srv.fsm, "observe", observe)
    monkeypatch.setattr(srv, "health_event", event)


@pytest.mark.asyncio
async def test_a_tray_off_the_bus_skips_the_settle_and_observe_runs_at_once(monkeypatch, tmp_path):
    calls = _gate(monkeypatch, tmp_path, slice_sec=5.0)
    _observed(monkeypatch, calls)
    for i in range(24, 32):  # one tray did not come back from the reset
        (tmp_path / str(i)).unlink()
    FABRIC_DEBT.note_reset("tt-smi -glx_reset", at=srv.time.monotonic() - 5)
    assert srv._post_reset_settle_left(32, srv.galaxy_recovery) > 0, "a young debt would otherwise wait"
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert calls["waits"] == [] and calls["order"][:1] == ["observe"]
    assert calls["events"] == [("post_reset_settle", calls["events"][0][1])]
    assert calls["events"][0][1]["wait_sec"] == 0.0 and calls["events"][0][1]["off_bus"] == 8
    assert not FABRIC_DEBT.settle_served, "a skipped settle is not one served"


@pytest.mark.asyncio
async def test_a_chip_leaving_mid_settle_ends_the_wait(monkeypatch, tmp_path):
    calls = _gate(monkeypatch, tmp_path, slice_sec=5.0)
    _observed(monkeypatch, calls)
    gate_sleep = srv.asyncio.sleep

    async def sleep(sec, *a, **k):
        if sec >= 1:
            calls["order"].append("slice")
            if len(calls["waits"]) == 1:  # chip 7 drops during the second slice
                (tmp_path / "7").unlink()
        return await gate_sleep(sec, *a, **k)

    monkeypatch.setattr(srv.asyncio, "sleep", sleep)
    FABRIC_DEBT.note_reset("tenant job j5", at=srv.time.monotonic() - 1)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    assert [w["sec"] for w in calls["waits"]] == [5.0, 5.0], "the settle ends after the slice the chip left in"
    assert calls["order"][:3] == ["slice", "slice", "observe"]
    assert [e[0] for e in calls["events"]] == ["post_reset_settle", "post_reset_settle_ended"]
    assert calls["events"][1][1]["off_bus"] == 1 and calls["events"][1][1]["waited_sec"] == 10.0


@pytest.mark.asyncio
async def test_the_sliced_settle_still_waits_the_whole_remainder(monkeypatch, tmp_path):
    calls = _gate(monkeypatch, tmp_path, slice_sec=5.0)
    FABRIC_DEBT.note_reset("tenant job j6", at=srv.time.monotonic() - 2)
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)
    total = sum(w["sec"] for w in calls["waits"])
    assert POST_RESET_SETTLE_SEC - 4 <= total <= POST_RESET_SETTLE_SEC - 1
    assert all(w["sec"] <= 5.0 and w["fabric_ran"] == 0 for w in calls["waits"])
    assert calls["fabric"] == 1 and srv.fsm.state is ServerState.HEALTHY
