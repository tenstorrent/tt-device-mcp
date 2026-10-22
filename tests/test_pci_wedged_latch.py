# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I76 / spec 03 I83: the one in-flight PCI sysfs writer and its PCI-wedged latch.

A PCI remove, rescan or bridge reset can block in the kernel for good behind a hung endpoint. One
such write in flight per broker; the first that times out (or is refused behind it) latches
PCI-wedged: the pass stops, later PCI rungs and the dead-chip valve's removes are skipped at once,
the power cycle is requested at once (only the post-boot/post-cycle cooldown may delay it), and the
latch clears only with no write pending and a full check with fabric just passed.

The writer is reached through the server module (never imported here at module level), so each test
fails on a tree without it.
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.recovery import base as recovery_base


def _writer(monkeypatch, required=True):
    writer = getattr(srv, "PCI_WRITER", None)
    assert writer is not None or not required, "no one in-flight PCI writer"
    if writer is not None:
        monkeypatch.setattr(writer, "on_latch", None)  # these tests drive the hold by hand
    return writer


@pytest.mark.asyncio
async def test_a_blocked_bridge_reset_stops_the_pass_after_one_call(monkeypatch):
    """(a) 8 isolated, enumerated chips and a bridge reset blocked in the kernel: one call, one
    timeout, the rung fails and every chip stays lost or withheld, instead of 8 timeouts in a row."""
    writer = _writer(monkeypatch, required=False)
    chips = [str(i) for i in range(8)]
    kernel_lock = threading.Event()
    calls = []
    monkeypatch.setattr(srv, "isolated_chips", set(chips))
    monkeypatch.setattr(srv, "device_pci_map", {c: f"0000:{0x61 + int(c):02x}:00.0" for c in chips})
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "PCI_SYSFS_WRITE_TIMEOUT_SEC", 0.25)
    monkeypatch.setattr(recovery_pkg, "bridge_reset_call_timeout", lambda: 0.25)

    def bridge(idx, bdf):
        calls.append(idx)
        kernel_lock.wait(30)
        return None

    class NoPath:
        def __init__(self, p):
            self.p = str(p)

        def write_text(self, v):
            calls.append("rescan")

        def exists(self):
            return False

    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", bridge)
    monkeypatch.setattr(recovery_pkg, "Path", NoPath)
    t0 = time.monotonic()
    try:
        ok = await asyncio.wait_for(srv.galaxy_recovery._recover_isolated_chips(lambda *a, **k: None), 10)
    finally:
        kernel_lock.set()
    assert len(calls) <= 1, f"one PCI write behind a blocked lock, not {calls}"
    assert time.monotonic() - t0 < 0.6
    assert not ok, "the rung failed"
    assert writer is not None and writer.latched
    assert srv.isolated_chips == set(chips), "every chip stays isolated"
    reasons = srv.galaxy_recovery.last_bridge_reset_reasons
    assert all(reasons.get(c, {}).get("reason") == "pci_wedged" for c in chips[1:]), reasons


@pytest.mark.asyncio
async def test_b_a_later_rescan_behind_a_blocked_write_fails_fast_and_starts_no_thread(monkeypatch):
    """(b) While the first write's thread is still blocked, a later rescan is refused at once."""
    writer = _writer(monkeypatch, required=False)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "PCI_SYSFS_WRITE_TIMEOUT_SEC", 0.25)
    kernel_lock = threading.Event()
    later = []
    try:
        with pytest.raises(asyncio.TimeoutError):
            await recovery_base.pci_rescan("first", write=lambda: kernel_lock.wait(30))
        threads = threading.active_count()
        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await recovery_base.pci_rescan("later", write=lambda: later.append(1))
        assert time.monotonic() - t0 < 0.1, "refused at once"
        assert later == [] and threading.active_count() == threads, "no second write, no new thread"
        assert writer is not None and writer.latched and writer.pending()
    finally:
        kernel_lock.set()


@pytest.mark.asyncio
async def test_c_the_valve_skips_a_dead_chip_while_pci_wedged(monkeypatch):
    """(c) While latched the dead-chip valve skips the chip: no remove (it would queue on the blocked
    lock), and the device is held."""
    writer = _writer(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    calls = []
    monkeypatch.setattr(srv, "isolate_chip", lambda idx: calls.append(idx) or True)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: "0000:46:00.0")

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    writer._latch("test: a remove blocked in the kernel")

    await srv.sampler.isolate_dead_chips(["13"])

    assert calls == [], "no isolate_chip while PCI-wedged"
    assert srv.fsm.state is not ServerState.HEALTHY, "the device is held"


def _wedged_hold(monkeypatch, writer, now):
    since = (now - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", since)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_pci_wedged_asked", None, raising=False)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "_recovery_degraded", lambda: True)
    monkeypatch.setattr(srv, "_host_action_blocks_escalation", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    rungs = []

    async def host_rung(age, log):
        rungs.append(age)

    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)
    fsm_dirty(srv, "PCI-wedged", why="heartbeat")
    writer._latch("test: a bridge reset blocked in the kernel")
    return since, rungs


async def _tick(now):
    srv._check_hold_deadline(now)
    task = srv._forced_escalation_task
    if task is not None:
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_d_the_latch_asks_for_the_power_cycle_at_once_outside_the_cooldown(monkeypatch):
    """(d) Outside the cooldown the host rung is asked on the first tick, seconds into the hold, not
    at the 10-min mark, and only once per latch."""
    writer = _writer(monkeypatch)
    now = datetime.now()
    _, rungs = _wedged_hold(monkeypatch, writer, now)
    await _tick(now)
    assert len(rungs) == 1 and rungs[0] < 60, rungs
    await _tick(now + timedelta(seconds=1))
    assert len(rungs) == 1, "asked once per latch"


@pytest.mark.asyncio
async def test_d_inside_the_cooldown_the_latch_asks_when_it_ends(monkeypatch):
    """(d) A cooldown denial arms the retry: nothing is asked inside it, the host rung the moment it ends."""
    writer = _writer(monkeypatch)
    now = datetime.now()
    since, rungs = _wedged_hold(monkeypatch, writer, now)
    until = now.timestamp() + 120
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", (until, since))
    await _tick(now)
    assert rungs == [], "inside the cooldown nothing is asked"
    await _tick(now + timedelta(seconds=121))
    assert len(rungs) == 1, "asked as soon as the cooldown ended"


@pytest.mark.asyncio
async def test_e_no_hold_lifts_while_pci_wedged_without_a_just_passed_full_check(monkeypatch):
    """(e) While latched: an unverified clear never lifts; a verified lift is refused while a write is
    still blocked; once none is and the full check with fabric just passed, it lifts and clears."""
    writer = _writer(monkeypatch)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")
    monkeypatch.setattr(srv, "_hold_lift_unproven", lambda: "")  # a full check with fabric just passed
    fsm_dirty(srv, "PCI-wedged", why="heartbeat")
    kernel_lock = threading.Event()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await writer.write("blocked remove", kernel_lock.wait, 30, timeout=0.2)
        srv._clear_device_dirty_unverified("test", holds=False)
        assert srv.fsm.state is not ServerState.HEALTHY, "an unverified clear lifted a PCI-wedged hold"
        assert srv._clear_device_dirty(verified=True, why="test") is False, "lifted with a write still blocked"
        assert writer.latched
    finally:
        kernel_lock.set()
    for _ in range(100):
        if not writer.pending():
            break
        await asyncio.sleep(0.01)
    assert srv._clear_device_dirty(verified=True, why="test") is True
    assert not writer.latched, "the latch clears on the verified lift"
