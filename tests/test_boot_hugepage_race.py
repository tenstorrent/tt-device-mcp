# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The boot hugepage race, replayed on a fake clock: a host reboots with a fabric-unverified DOWN
episode on record, the broker starts before the 1G hugepage pool is filled, and its startup fabric
check exits 77 in under a second. The broker must end that on its own, with no reset and no human:
wait for the pool before the startup pass (spec 03 I33), or else reopen the carried hold (I49) and
re-check a hugepage 77 every 30 s (I48) until the pool fills. A real fabric fault (a missing cable,
I47) found once the pool is there must still keep the box held.

Field timeline (times relative to the startup check): the broker starts at -5 s, the startup fabric
check exits 77 at 0 s ("Failed to pin pages for hugepage"), and the pool is filled at +21 s.
"""

import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_recovery
from tests.test_boot_fabric_reverify import _FABRIC_OK, _broker, _carried
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import fabric

# The real opt-in reader (_broker stubs it out): it reads the pool only with the expected chip count set.
_REAL_SHORTFALL = srv._hugepages_shortfall
_POOL_READY = 21.0
_TICK = 5.0
_HUGEPAGE_77 = "  what():  Failed to pin pages for hugepage\nfabric-check: validator did not complete a measurement"
_CABLE = f"{fabric.CABLE_FAULT}: 1 port/cable connection(s) of the cabling descriptor missing and none extra"


def _clock(monkeypatch):
    """The broker's monotonic and wall clocks, moved by hand: ``clock["t"]`` seconds since the check."""
    clock = {"t": 0.0}
    mono0 = time.monotonic()

    class _Time:
        def __getattr__(self, name):
            return getattr(time, name)

        def monotonic(self):
            return mono0 + clock["t"]

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=clock["t"])

    monkeypatch.setattr(srv, "time", _Time())
    monkeypatch.setattr(srv, "datetime", _Datetime)
    return clock


def _race(monkeypatch, tmp_path, *, cable=False):
    """A carried DOWN fabric hold on a 32-chip broker with the default relift, whose fabric pass
    77s on the hugepage pool until ``_POOL_READY``, then passes (or, with ``cable``, finds a missing
    cable)."""
    _events, seen = _broker(monkeypatch, tmp_path, _carried(tmp_path))
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_RELIFT", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_BACKOFF_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", raising=False)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", False)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", "")
    clock = _clock(monkeypatch)
    known = {"fault": ""}
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: known["fault"])
    passes = []

    async def verify(expected, log, run_fabric=True, **_):
        passes.append(clock["t"])
        if clock["t"] < _POOL_READY or cable:
            output = _HUGEPAGE_77 if clock["t"] < _POOL_READY else _CABLE
            if clock["t"] >= _POOL_READY:
                known["fault"] = _CABLE
            monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", True)
            monkeypatch.setattr(srv.health_monitor, "last_fabric_output", output)
            return True, {
                "snapshot": {"ok": True},
                "eth_heartbeat": {"ok": True, "detail": "advancing"},
                "fabric": {"ok": None, "detail": output},
            }
        monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", False)
        return _FABRIC_OK

    patch_recovery(monkeypatch, "_verify_device", verify)

    async def eth_read(*a, **k):
        return True, "all active-eth cores advancing"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_read)
    return clock, passes, seen


async def _idle_ticks(clock, until):
    """Run the idle relift every ``_TICK`` s until the hold lifts or ``until``; the lift time or None."""
    while clock["t"] < until:
        clock["t"] += _TICK
        await srv._attempt_idle_relift()
        if srv.fsm.state is ServerState.HEALTHY:
            return clock["t"]
    return None


@pytest.mark.asyncio
async def test_the_startup_pass_that_77s_on_the_pool_is_lifted_once_it_fills(monkeypatch, tmp_path, clear_job_state):
    """The field race as it happened: the startup pass ran at 0 s and 77'd on the pool. The carried
    DOWN reopens, and the hugepage re-check lifts the hold within 30 s and one tick of the pool."""
    clock, passes, seen = _race(monkeypatch, tmp_path)

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    assert srv.fsm.state is ServerState.RECOVERING, "the carried DOWN must not stand on a pool that is filling"

    lifted = await _idle_ticks(clock, 600)

    assert lifted is not None, "no human: the broker lifts the hold itself"
    assert lifted - _POOL_READY <= srv.FABRIC_77_HUGEPAGES_RECHECK_SEC + _TICK
    assert all(t < _POOL_READY for t in passes[:-1]) and passes[-1] >= _POOL_READY
    assert seen["resets"] == 0 and seen["rungs"] == [], "a pool still filling is never reset for"


@pytest.mark.asyncio
async def test_a_broker_that_sees_the_pool_waits_for_it_before_the_startup_pass(monkeypatch, tmp_path, clear_job_state):
    """With TT_DEVICE_MCP_EXPECTED_CHIPS set the startup pass waits for the pool and runs once, after
    it fills: no 77 at all."""
    clock, passes, seen = _race(monkeypatch, tmp_path)
    clock["t"] = -5.0
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    monkeypatch.setattr(srv, "HUGEPAGES_POLL_SEC", 0)

    def shortfall():
        clock["t"] += 2.0  # the broker re-reads the pool every 2 s
        return (0, 32) if clock["t"] < _POOL_READY else None

    monkeypatch.setattr(srv, "_hugepages_shortfall", _REAL_SHORTFALL)
    monkeypatch.setattr(srv, "health_hugepages_shortfall", shortfall)

    await srv._verify_fabric_on_start()

    assert passes and passes[0] >= _POOL_READY, "no pass before the pool fills"
    assert len(passes) == 1
    assert srv.fsm.state is ServerState.HEALTHY
    assert clock["t"] - _POOL_READY <= 2.0
    assert seen["resets"] == 0 and seen["rungs"] == []


@pytest.mark.asyncio
async def test_a_missing_cable_found_once_the_pool_fills_keeps_the_box_held(monkeypatch, tmp_path, clear_job_state):
    """The same race on a box with a cable gone: once the pool fills the pass names the missing cable,
    and the hold stands past the 77 bounds, with no release, reset or ladder rung."""
    clock, passes, seen = _race(monkeypatch, tmp_path, cable=True)

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    lifted = await _idle_ticks(clock, 2 * 3600)

    assert lifted is None, "a missing cable is never lifted"
    assert any(t >= _POOL_READY for t in passes), "the pass did run once the pool filled"
    assert srv.fsm.state is not ServerState.HEALTHY
    assert seen["resets"] == 0 and seen["rungs"] == []
