# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I86: the settle before the one verify that decides a power cycle polls the chips, it does not sleep.

The sweep's reset ends, the broker waits for the chips to re-enumerate, then runs one full check (fabric included).
The wait used to be a flat 60 s sleep, so a chip that stays off cost the whole minute before the cycle was requested,
and a chip that came back at 4 s cost it too. Now the wait polls each expected chip's answer (enumerated and a live
ARC heartbeat), ends at once when all answer and at the settle's own bound (60 s, env override unchanged) when one
does not. The full check with fabric that follows still decides: only a pass releases, a failure goes on to the cycle.
"""

from __future__ import annotations

import time as real_time
from types import SimpleNamespace

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import galaxy, pcie_guard

EXPECTED = 32
BOUND = 60
LATE_CHIP = "31"


class Rig:
    """A fake clock the settle poll's sleep advances, the chips that answer at each fake second, and every step."""

    def __init__(self):
        self.now = 1000.0
        self.t0 = self.now
        self.answer_at: dict = {}  # chip -> seconds after the reset at which it answers (absent: never)
        self.order: list = []
        self.events: list = []
        self.verifies: list = []
        self.verify_ok = False
        self.cleared: list = []

    def elapsed(self):
        return self.now - self.t0

    def answering(self):
        return [c for c, t in self.answer_at.items() if self.elapsed() >= t]


@pytest.fixture
def rig(monkeypatch):
    g = srv.galaxy_recovery
    r = Rig()
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: BOUND)
    monkeypatch.setattr(
        galaxy, "time", SimpleNamespace(monotonic=lambda: r.now, time=real_time.time, sleep=real_time.sleep)
    )

    async def fake_sleep(seconds):
        r.order.append("sleep")
        r.now += seconds

    monkeypatch.setattr(galaxy.asyncio, "sleep", fake_sleep)

    def present():
        r.order.append("poll")
        return [str(i) for i in range(EXPECTED) if str(i) != LATE_CHIP or LATE_CHIP in r.answering()]

    monkeypatch.setattr(srv, "_present_chip_indices", present)
    monkeypatch.setattr(galaxy, "arc_live_chips", lambda chips: chips)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda kind, **f: r.events.append((kind, f)))

    async def sbr(log):
        r.order.append("sbr")
        return False

    async def mesh(argv, log, *a, **k):
        r.order.append("mesh")
        r.t0 = r.now  # the settle is measured from the reset's end
        return 0, ""

    async def verify(expected, log, run_fabric=True, **k):
        r.order.append("verify")
        r.verifies.append(run_fabric)
        return r.verify_ok, {}

    def clear_dirty(*a, **k):
        r.order.append("clear_dirty")
        r.cleared.append(k)
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)
    monkeypatch.setattr(g.deps, "clear_device_dirty", clear_dirty)
    monkeypatch.setattr(g.deps, "clear_device_reported_fault", lambda why: None)
    return r


async def _last_chance(rig):
    return await srv.galaxy_recovery._settle_and_verify_before_host_rung(
        EXPECTED, lambda m: None, "idle", offbus_chips={"30"}, context="idle escalation"
    )


def _poll_event(rig):
    return next(f for k, f in rig.events if k == "settle_poll")


@pytest.mark.asyncio
async def test_settle_early_heal_ends_when_every_chip_answers(rig):
    rig.answer_at = {LATE_CHIP: 4}
    rig.verify_ok = True
    healthy = await _last_chance(rig)
    assert healthy is True
    ev = _poll_event(rig)
    assert ev["not_answering"] == 0
    assert 4 <= ev["waited_sec"] < 10, "the settle ended when the chip answered, not at the bound"
    assert rig.elapsed() < BOUND / 2
    assert rig.verifies == [True]


@pytest.mark.asyncio
async def test_settle_bound_chip_off_waits_the_bound_then_fails_the_check(rig):
    healthy = await _last_chance(rig)  # no chip ever answers LATE_CHIP
    assert healthy is False, "a chip that has not answered counts as failed: the caller climbs to the cycle"
    ev = _poll_event(rig)
    assert ev["not_answering"] == 1
    assert BOUND <= ev["waited_sec"] <= BOUND + galaxy.SETTLE_POLL_SEC
    assert rig.elapsed() <= BOUND + galaxy.SETTLE_POLL_SEC, "the bound is the settle's own, no second one added"
    assert rig.verifies == [True], "the full check with fabric still runs after the bound"
    assert rig.cleared == []
