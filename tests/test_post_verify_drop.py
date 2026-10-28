# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A chip lost after a reset's own check (spec 04 I31, I84).

Dead-chip isolation can kill the post-reset fabric check: that pass reaches no verdict, but not
because eth links are still training, so it fails the verify at once with no eth-training wait. And a
chip that drops after the reset's own check read every chip present is a new drop, not a regression of
that reset: the gate and the idle climb must not latch it, or the next mesh reset is skipped for the
rest of the hold."""

import time

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold, _reach_reboot_rung_with_regressing_reset
from tests.test_post_reset_gate import NO_TRAINED_LINK_77, _replay_post_reset_verify, mesh  # noqa: F401
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitor import FABRIC_KILLED_BY_ISOLATION

KILLED = {
    "snapshot": {"ok": True},
    "fabric": {"ok": None, "detail": f"{FABRIC_KILLED_BY_ISOLATION}: chip 8 lost (rc -9)"},
}
ALL_PRESENT = {"snapshot": {"ok": True}}
CHIP_MISSING = {"snapshot": {"ok": False, "detail": "31/32 chips enumerated"}}


# -- killed_check_no_sleep ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("mesh")
async def test_killed_check_no_sleep(monkeypatch):
    """A post-reset fabric check that dead-chip isolation killed is a failed verify at once: no
    eth-training wait, no second pass. The same pass still read every chip present, so it is recorded."""
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    srv.recovery_mechanism.last_reset_monotonic = 1234.0
    healthy, spent, sleeps, fabric_asked = await _replay_post_reset_verify(monkeypatch, [(70.0, KILLED)])
    assert healthy is False, "a check a lost chip ended is a failed verify, never a pass or a 77"
    assert sleeps == [] and spent == 70.0, "no eth-training wait"
    assert fabric_asked == [True], "one full pass with fabric (goal 8), no retry"
    assert "post_reset_verify_chip_lost" in events
    assert srv.recovery_mechanism.last_reset_verified_present()


@pytest.mark.asyncio
@pytest.mark.usefixtures("mesh")
async def test_killed_check_no_sleep_when_a_chip_is_isolated_during_the_check(monkeypatch):
    """The same when the 77 detail does not name the kill but a chip went off the bus under the check."""
    srv.isolated_chips = set()

    async def verify(expected, log, run_fabric=True, **_):
        srv.isolated_chips = {"8"}
        return True, dict(NO_TRAINED_LINK_77)

    sleeps = []

    async def sleep(sec, *_a, **_k):
        sleeps.append(sec)

    from tt_device_mcp.health import recovery as recovery_pkg

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(recovery_pkg.asyncio, "sleep", sleep)
    healthy, _, _ = await srv.galaxy_recovery._verify_device_after_reset(32, lambda m: None)
    assert healthy is False and sleeps == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("mesh")
async def test_a_plain_no_trained_link_77_still_waits(monkeypatch):
    """Nothing lost: the 77 is eth training, and the wait stays (spec 04 I84)."""
    srv.isolated_chips = set()
    healthy, _, sleeps, _ = await _replay_post_reset_verify(monkeypatch, [(40.0, NO_TRAINED_LINK_77)] * 2)
    assert healthy is None and sleeps == [60.0], "a persistent 77 is UNKNOWN, held (goal 11)"


# -- gate path ------------------------------------------------------------------------------------


def _gate_reset_whose_verify_reads(monkeypatch, tmp_path, evidence):
    """The gate's reset (20 -> 21 of 32 off the bus) through the real post-reset verify, whose one pass
    reads ``evidence``. Returns (events, reset count)."""
    _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before=20, off_after=21)
    events, resets = [], {"n": 0}
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    stub = srv.galaxy_recovery._reset_and_verify_device
    rec = srv.galaxy_recovery

    async def verify(expected, log, run_fabric=True, **_):
        return bool(evidence["snapshot"]["ok"]), dict(evidence)

    async def reset(indices, log):
        resets["n"] += 1
        srv.recovery_mechanism.last_reset_monotonic = time.monotonic()
        await stub(indices, log)  # the chip drops
        monkeypatch.setattr(rec, "_verify_device", verify)
        try:
            healthy, _, _ = await rec._verify_device_after_reset(32, log)
        finally:
            monkeypatch.delattr(rec, "_verify_device")
        return healthy

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    return events, resets


@pytest.mark.asyncio
async def test_post_verify_drop_not_regressed(monkeypatch, tmp_path, clear_job_state):
    """The reset's own check read all 32 chips; a chip dropped under its fabric pass (the check was
    killed). That is a new drop, not a regression: no latch and no reset_regressed_offbus. The failed
    verify still arms the failed-reset cooldown, so the next gate runs no second reset inside it."""
    events, resets = _gate_reset_whose_verify_reads(monkeypatch, tmp_path, KILLED)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert resets["n"] == 1
    assert "reset_regressed_offbus" not in events
    assert srv.recovery_mechanism.regressed_reset_since is None
    events.clear()
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert "galaxy_reset_skipped_regressed" not in events, "a later drop is not latched as a regression"
    assert "reset_suppressed_cooldown" in events, "the failed verify's cooldown still applies"
    assert resets["n"] == 1, "no second reset inside the failed-reset cooldown"


@pytest.mark.asyncio
async def test_real_regression_still_flagged(monkeypatch, tmp_path, clear_job_state):
    """The reset's own check found a chip missing: more chips off the bus is still a regression."""
    events, _ = _gate_reset_whose_verify_reads(monkeypatch, tmp_path, CHIP_MISSING)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert "reset_regressed_offbus" in events
    assert not srv.recovery_mechanism.last_reset_verified_present()


@pytest.mark.asyncio
async def test_real_regression_still_flagged_after_an_older_verified_reset(monkeypatch, tmp_path, clear_job_state):
    """An earlier reset whose check read every chip present does not excuse a later regressing one."""
    _reach_reboot_rung_with_regressing_reset(monkeypatch, tmp_path, off_before=20, off_after=28)
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    srv.recovery_mechanism.verified_present_reset_at = 5.0
    stub = srv.galaxy_recovery._reset_and_verify_device

    async def reset(indices, log):
        srv.recovery_mechanism.last_reset_monotonic = time.monotonic()
        return await stub(indices, log)

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert "reset_regressed_offbus" in events


# -- idle path ------------------------------------------------------------------------------------


def _idle_reset(monkeypatch, verified_present):
    """The idle off-bus climb's galaxy reset (16 -> 17 off the bus); its own check read every chip present
    when ``verified_present``."""
    events = []
    _arm_offbus_stuck_hold(
        monkeypatch, off_bus=16, isolated=set(), reset_result=False, beats_after_reset={str(i): 100 for i in range(15)}
    )
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    stub = srv.galaxy_recovery._reset_and_verify_device

    async def reset(indices, log):
        mech = srv.recovery_mechanism
        mech.last_reset_monotonic = time.monotonic()
        if verified_present:
            mech.verified_present_reset_at = mech.last_reset_monotonic
        return await stub(indices, log)

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    return events


@pytest.mark.asyncio
async def test_post_verify_drop_not_regressed_on_the_idle_path(monkeypatch, clear_job_state):
    events = _idle_reset(monkeypatch, verified_present=True)
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert "reset_regressed_offbus" not in events
    assert srv.recovery_mechanism.regressed_reset_since is None


@pytest.mark.asyncio
async def test_real_regression_still_flagged_on_the_idle_path(monkeypatch, clear_job_state):
    events = _idle_reset(monkeypatch, verified_present=False)
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert "reset_regressed_offbus" in events
