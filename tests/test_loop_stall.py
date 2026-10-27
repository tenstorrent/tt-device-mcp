# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A chip slow to answer never stalls the broker's event loop (spec 03 I87).

/health and the queue status read every chip's heartbeat from sysfs. Each read is a PCIe read; a
chip with AER errors or leaving the bus can take seconds to answer. Those reads used to run on the
event loop, several passes per call, so every broker surface stopped answering for as long as they
took. They now run once per call in a worker thread; past STATUS_CHIP_READ_WAIT_SEC the call
answers from the last completed read and says how old it is. A watchdog logs where the loop is
blocked when it stalls anyway."""

import ast
import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import fsm_healthy
from tests.test_arc_not_ready import _fake_sysfs, _live_health
from tests.test_verification_jobs import _finished, _job, broker_root, enum_arc, events  # noqa: F401
from tt_device_mcp import loop_watch
from tt_device_mcp import server as srv


@pytest.fixture
def status_rig(monkeypatch, tmp_path, clear_job_state):
    """A healthy 32-chip fake sysfs whose heartbeat read can be held up by a test."""
    _fake_sysfs(monkeypatch, tmp_path)
    _live_health(monkeypatch)
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_status_read", None)
    monkeypatch.setattr(srv, "_status_read_last", None)
    monkeypatch.setattr(srv, "_decision_read", None)
    real = srv.read_heartbeats
    rig = {"gate": threading.Event(), "reads": 0, "threads": set()}
    rig["gate"].set()

    def read():
        rig["reads"] += 1
        rig["threads"].add(threading.get_ident())
        assert rig["gate"].wait(10), "test never released the read"
        return real()

    monkeypatch.setattr(srv, "read_heartbeats", read)
    yield rig
    rig["gate"].set()


async def _ticks_while(coro, every: float = 0.05):
    """``(result, ticks)``: run ``coro`` while another coroutine counts its loop turns."""
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(every)
            ticks += 1

    ticker = asyncio.ensure_future(tick())
    try:
        return await coro, ticks
    finally:
        ticker.cancel()


@pytest.mark.asyncio
async def test_health_answers_within_a_second_while_the_chip_read_hangs(status_rig):
    """The loop stays free: /health answers from the last read, with its age, while a fresh read
    hangs in its thread."""
    first = await srv._status_view(srv._health_payload)
    assert "chip_read_stale_sec" not in first
    assert first["off_bus"] == 0

    status_rig["gate"].clear()
    started = time.monotonic()
    payload, ticks = await _ticks_while(asyncio.wait_for(srv._status_view(srv._health_payload), 1.0))
    assert time.monotonic() - started < 1.0
    assert payload["chip_read_stale_sec"] >= 0
    assert payload["off_bus"] == 0
    assert payload["expected_chips"] == 32
    assert ticks >= 5, "the loop did not run while the chips were read"
    assert threading.get_ident() not in status_rig["threads"], "a status read ran on the loop thread"
    status_rig["gate"].set()


@pytest.mark.asyncio
async def test_the_first_read_waits_in_its_thread_not_on_the_loop(status_rig):
    """With no read yet, /health waits for the one in flight, and the loop keeps running meanwhile."""
    status_rig["gate"].clear()
    asyncio.get_running_loop().call_later(0.8, status_rig["gate"].set)
    payload, ticks = await _ticks_while(srv._status_view(srv._health_payload))
    assert "chip_read_stale_sec" not in payload
    assert payload["expected_chips"] == 32
    assert ticks >= 10
    assert threading.get_ident() not in status_rig["threads"]


@pytest.mark.asyncio
async def test_concurrent_status_calls_share_one_read(status_rig):
    """A slow chip never piles up one read thread per poll."""
    await srv._status_view(srv._health_payload)
    status_rig["gate"].clear()
    reads = status_rig["reads"]
    views = [srv._status_view(srv._health_payload) for _ in range(5)] + [srv._status_view(srv._get_queue_status)]
    payloads = await asyncio.wait_for(asyncio.gather(*views), 1.5)
    assert all("chip_read_stale_sec" in p for p in payloads)
    assert status_rig["reads"] == reads + 1
    status_rig["gate"].set()


@pytest.mark.asyncio
async def test_no_status_read_while_a_reset_step_owns_the_chips(status_rig, monkeypatch):
    """Spec 03 I66 still holds: nothing reads the chips while a reset step owns them."""
    monkeypatch.setattr(srv.chip_state, "recovering", lambda: True)
    await srv._status_view(srv._health_payload)
    assert status_rig["reads"] == 0


def test_the_status_routes_build_their_payload_off_the_loop_read():
    """/health and the REST and MCP queue status go through _status_view, never a bare build."""
    tree = ast.parse(Path(srv.__file__).read_text())
    bare = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ("health_check", "api_queue_status", "queue_status"):
            for call in ast.walk(node):
                f = getattr(call, "func", None)
                if isinstance(call, ast.Call) and isinstance(f, ast.Name):
                    if f.id in ("_health_payload", "_get_queue_status"):
                        bare.append(f"{node.name}:{call.lineno}")
    assert bare == [], f"status built with a read on the loop: {bare}"


# ---------------------------------------------------------------- decision reads


def _fabric_only_hold(monkeypatch):
    """A fabric_unverified hold with nothing dirty and no fault: liveness alone decides the lift."""
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="gate/post-job: fabric unverified", dirty=False)
    assert srv._fabric_hold_shape("")


@pytest.mark.asyncio
async def test_admission_reads_the_chips_off_the_loop(status_rig, monkeypatch):
    """A tenant's admission reads the chips fresh, in a worker thread: the loop keeps running while
    a slow chip answers, and the verdict is the fresh one."""

    async def clean(_log):
        return None

    monkeypatch.setattr(srv, "_ensure_device_clean_for_next_job", clean)
    status_rig["gate"].clear()
    asyncio.get_running_loop().call_later(0.8, status_rig["gate"].set)
    reason, ticks = await _ticks_while(srv._await_device_free_for_tenant(None))
    assert reason == ""
    assert status_rig["reads"] == 1
    assert ticks >= 10, "the loop did not run while admission read the chips"
    assert threading.get_ident() not in status_rig["threads"], "a decision read ran on the loop thread"


@pytest.mark.asyncio
async def test_a_decision_read_that_times_out_refuses(status_rig, monkeypatch):
    """A read not back by the deadline counts as no chip readable: a refusal, never a pass."""
    assert await srv._device_degraded_for_decision_live() == ""
    monkeypatch.setattr(srv, "DECISION_CHIP_READ_TIMEOUT_SEC", 0.3)
    status_rig["gate"].clear()
    reason, ticks = await _ticks_while(srv._device_degraded_for_decision_live())
    assert "heartbeat unreadable" in reason
    assert ticks >= 3
    status_rig["gate"].set()


@pytest.mark.asyncio
async def test_a_hung_decision_read_never_piles_up_threads(status_rig, monkeypatch):
    """Later decisions wait on the read in flight rather than start one thread each."""
    monkeypatch.setattr(srv, "DECISION_CHIP_READ_TIMEOUT_SEC", 0.2)
    status_rig["gate"].clear()
    first = await srv._device_degraded_for_decision_live()
    second = await srv._device_degraded_for_decision_live()
    assert first and second
    assert status_rig["reads"] == 1
    status_rig["gate"].set()


@pytest.mark.asyncio
async def test_a_timed_out_read_never_counts_the_hold_fabric_only(status_rig, monkeypatch):
    """The fabric-only test behind every lift reads liveness off the loop; a read that times out
    leaves the hold as it is."""
    _fabric_only_hold(monkeypatch)
    assert await srv._fabric_hold_only_live()
    monkeypatch.setattr(srv, "DECISION_CHIP_READ_TIMEOUT_SEC", 0.3)
    status_rig["gate"].clear()
    assert not await srv._fabric_hold_only_live()
    assert srv.fsm.state is not srv.ServerState.HEALTHY
    status_rig["gate"].set()


@pytest.mark.asyncio
async def test_a_timed_out_read_never_lifts_a_hold(
    status_rig, monkeypatch, broker_root, enum_arc, events  # noqa: F811
):
    """An operator fabric check passed, but the chip read behind the lift does not come back in
    time: the hold stays (spec 03 I4, I87)."""
    _fabric_only_hold(monkeypatch)
    monkeypatch.setattr(srv, "DECISION_CHIP_READ_TIMEOUT_SEC", 0.3)
    status_rig["gate"].clear()
    job = _finished(_job("601", f"{broker_root}/fabric-check.sh", peer_uid=0))
    assert not await srv._settle_verification_job(job)
    assert srv.fsm.state is not srv.ServerState.HEALTHY
    assert not [k for k, _ in events if k == "verification_lifted_hold"]
    status_rig["gate"].set()


@pytest.mark.asyncio
async def test_a_readable_device_still_lifts_through_the_offloop_read(
    status_rig, monkeypatch, broker_root, enum_arc, events  # noqa: F811
):
    """The same lift with the chips answering: the off-loop read changes where it runs, not what
    it decides."""
    _fabric_only_hold(monkeypatch)
    job = _finished(_job("602", f"{broker_root}/fabric-check.sh", peer_uid=0))
    assert await srv._settle_verification_job(job)
    assert srv.fsm.state is srv.ServerState.HEALTHY
    assert threading.get_ident() not in status_rig["threads"]


def test_no_async_path_reads_the_chips_on_the_loop_for_a_decision():
    """Coroutines take their decision through the live helpers, and recent_jobs builds under the
    status read, never with a bare read on the loop."""
    tree = ast.parse(Path(srv.__file__).read_text())
    allowed = {"_device_degraded_for_decision_live", "_fabric_hold_only_live"}
    bare = []

    def calls(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                yield child
            yield from calls(child)

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name in allowed:
            continue
        for call in calls(node):
            if call.func.id in ("_device_degraded_for_decision", "_fabric_hold_only", "_recent_jobs"):
                bare.append(f"{node.name}:{call.lineno}:{call.func.id}")
    assert bare == [], f"a coroutine reads the chips on the loop: {bare}"


# ---------------------------------------------------------------- the stall watchdog


def _blocking_call_on_the_loop():
    time.sleep(0.8)


@pytest.mark.asyncio
async def test_a_blocked_loop_is_logged_with_the_call_that_blocks_it(caplog):
    log = logging.getLogger("test_loop_stall")
    caplog.set_level(logging.WARNING, logger="test_loop_stall")
    loop_watch.start(log, warn_sec=0.3, tick_sec=0.05)
    try:
        await asyncio.sleep(0.1)
        _blocking_call_on_the_loop()
        await asyncio.sleep(0.2)
    finally:
        loop_watch.stop()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "event loop stalled" in text
    assert "_blocking_call_on_the_loop" in text
    assert "event loop was blocked for" in text


@pytest.mark.asyncio
async def test_a_free_loop_logs_nothing(caplog):
    log = logging.getLogger("test_loop_stall")
    caplog.set_level(logging.WARNING, logger="test_loop_stall")
    loop_watch.start(log, warn_sec=0.3, tick_sec=0.05)
    try:
        for _ in range(10):
            await asyncio.sleep(0.05)
    finally:
        loop_watch.stop()
    assert not caplog.records
