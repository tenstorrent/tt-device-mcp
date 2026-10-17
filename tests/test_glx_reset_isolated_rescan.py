# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I81: a mesh reset brings back the chips the broker isolated, in the same reset.

`tt-smi -glx_reset` re-powers every tray, counts a 30 s settle, then waits up to 100 s for every chip to
show up in /dev/tenstorrent, and exits 1 with no re-init when one never does. A chip the broker isolated
(its endpoint `remove`d) can only come back through a PCI rescan, so before I81 every mesh reset over an
isolated chip failed after ~263 s even when the re-power had healed it, and the spec 04 I39 progress
retry's second reset finished the job: a one-chip drop (class B1) took ~8.5 min on a 6 min budget.
"""

import asyncio
import time
from pathlib import Path

import pytest

from tests.conftest import patch_recovery
from tests.test_no_repeat_failed_rung import (
    EXPECTED,
    ONE_SHOT,
    RESET_SEC,
    _beats_with_off,
    _drive_progress_retry_watchdog,
    _episode,
    _false,
    _window,
)
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base

RESCAN = Path("/sys/bus/pci/rescan")
GLX_OK_SEC = 62  # a mesh reset whose driver wait finds every chip
PROGRESS_RETRY_SEC = 60  # spec 04 I39: the retry comes due 60 s after the failed reset
B1_BUDGET_SEC = 6 * 60
SETTLE = "\x1b[93mWaiting for 30 seconds: 0\x1b[0m" + "".join(
    f"\r\x1b[93mWaiting for 30 seconds: {i}\x1b[0m" for i in range(1, 31)
)


def _fake_glx(hw, clock, real_sleep, *, chunks=None):
    """`tt-smi -glx_reset` as the broker's backend runs it: the re-power heals the dropped chip's tray,
    the settle line goes out, and the driver wait finds every chip only if a rescan re-added the
    isolated endpoint while it ran. ``chunks`` splits the settle line the way a file backend's reads may."""

    async def run(argv, log, owner="[broker]health-gate", on_output=None):
        emit = on_output or (lambda s: None)
        hw["resets"].append(list(argv))
        emit("Issuing USER_RESET to all chips\n")
        hw["repowered"] = True
        for part in chunks or [SETTLE + "\n"]:
            emit(part)
        for _ in range(5):
            await real_sleep(0)  # the 100 s driver wait: the event loop runs meanwhile
        if not hw["off"]:
            clock[0] += GLX_OK_SEC
            emit(f'Waiting for all {EXPECTED} chips to show up in "/dev/tenstorrent", found {EXPECTED}\n')
            return 0, "reset done"
        clock[0] += RESET_SEC
        emit(f'Waiting for all {EXPECTED} chips to show up in "/dev/tenstorrent", found {EXPECTED - 1}\n')
        return 1, "thread '<unnamed>' panicked: Timeout waiting for chips"

    return run


def _real_reset_path(monkeypatch, hw, clock, *, chunks=None):
    """Run the real reset_with_quiesce over a fake tt-smi. A rescan re-adds the isolated endpoint only
    after the re-power; the 3 s post-reset settle and pollers are stubbed."""
    real_sleep = asyncio.sleep

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__self__", None) == RESCAN:  # the real sysfs write never runs here
            hw["rescans"].append("during" if hw["in_reset"] else "after")
            if hw["repowered"]:
                hw["off"].clear()
            return None
        return fn(*a, **k)

    async def no_sleep(_):
        return None

    async def pollers(active, log):
        return []

    async def no_ceiling(*a, **k):
        return True, "ok", {}

    glx = _fake_glx(hw, clock, real_sleep, chunks=chunks)

    async def run_scoped(argv, log, owner="[broker]health-gate", on_output=None):
        hw["in_reset"] = True
        try:
            return await glx(argv, log, owner, on_output)
        finally:
            hw["in_reset"] = False

    monkeypatch.setattr(base, "returned_chips", lambda iso: sorted(c for c in iso if c not in hw["off"]))
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run_scoped)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(base.CEILING, "apply", no_ceiling)


def _hw():
    return {"off": {"31"}, "repowered": False, "in_reset": False, "resets": [], "rescans": []}


@pytest.mark.asyncio
async def test_b1_replay_one_mesh_reset_heals_an_isolated_chip_inside_the_budget(monkeypatch, clear_job_state):
    """Replay of a B1 episode: chip 31 fell off and was isolated; the 120 s one-shot's mesh reset
    re-powers its tray. The rescan at tt-smi's driver wait re-adds it, so that ONE reset exits 0 and
    the full check, fabric included, passes and releases. Before I81 the reset exited 1 at ~263 s, the
    post-reset rescan brought the chip back, and the I39 retry's second reset healed it 60 s later:
    ONE_SHOT + 263 + 60 + 62 = 505 s against a 360 s budget."""
    clock = [ONE_SHOT]
    counters, events, _ = _episode(monkeypatch, power_cycle=True, clock=clock, off_bus=1)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry", None)
    monkeypatch.setattr(srv.recovery_mechanism, "progress_retry_spent_since", "")
    hw = _hw()
    monkeypatch.setattr(srv, "read_heartbeats", lambda: _beats_with_off(hw["off"]))
    srv.isolated_chips = {"31"}
    monkeypatch.delattr(srv.galaxy_recovery, "_reset_and_verify_device")
    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", lambda log: _false())
    verifies = []

    async def sbr_no_help(log):
        counters["sbr"] += 1
        return False

    async def verify(expected, log, run_fabric=True, **_):
        verifies.append((run_fabric, len(hw["resets"])))
        assert counters["dirty_cleared"] == 0, "nothing released before the verify"
        ok = not hw["off"]
        return ok, {"snapshot": {"ok": ok}}

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr_no_help)
    patch_recovery(monkeypatch, "_verify_device", verify)
    _real_reset_path(monkeypatch, hw, clock)

    assert await _window() is True
    if srv.recovery_mechanism.progress_retry is not None:  # the old path: the first reset failed
        assert _drive_progress_retry_watchdog(monkeypatch) == ["progress_retry"]
        clock[0] += PROGRESS_RETRY_SEC
        assert await _window() is True

    assert clock[0] <= B1_BUDGET_SEC, f"B1 time to recover {clock[0]:.0f}s is over its {B1_BUDGET_SEC}s budget"
    assert [r for r in hw["resets"] if "-glx_reset" in r] == hw["resets"]
    assert len(hw["resets"]) == 1, f"one mesh reset heals it, not {len(hw['resets'])}"
    assert hw["rescans"][0] == "during", "the isolated endpoint is re-added inside tt-smi's driver wait"
    assert "reset_driver_wait_rescan" in events
    assert verifies and all(rf is True for rf, _ in verifies), "the post-reset check runs the fabric pass"
    assert counters["dirty_cleared"] == 1 and counters["power_cycles"] == 0
    assert srv.isolated_chips == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [None, [SETTLE[:500], SETTLE[500:] + "\n"], [SETTLE[: SETTLE.rindex("30") + 1], SETTLE[SETTLE.rindex("30") + 1 :]]],
    ids=["one-line", "split-read", "split-in-the-count"],
)
async def test_the_rescan_fires_once_at_the_driver_wait_and_the_caller_still_sees_the_output(
    monkeypatch, clear_job_state, chunks
):
    """The marker is found however the backend splits the output; one rescan, after the settle, and the
    operator's own output callback still gets every chunk."""
    hw, clock = _hw(), [0.0]
    srv.isolated_chips = {"31"}
    _real_reset_path(monkeypatch, hw, clock, chunks=chunks)
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    seen = []

    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(
        ["tt-smi", "-glx_reset"], lambda m: None, on_output=seen.append
    )

    assert rc == 0 and hw["rescans"] == ["during", "after"]
    assert "".join(seen).count("Waiting for 30 seconds: 30") == 1
    assert srv.isolated_chips == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "isolated"),
    [(["tt-smi", "-glx_reset"], set()), (["tt-smi", "-r", "0,1"], {"31"})],
    ids=["nothing-isolated", "not-a-mesh-reset"],
)
async def test_no_early_rescan_without_an_isolated_chip_or_off_a_mesh_reset(
    monkeypatch, clear_job_state, argv, isolated
):
    """The early rescan is only for a mesh reset over isolated chips; anything else runs as before, with
    the one rescan after the reset."""
    hw, clock = _hw(), [0.0]
    srv.isolated_chips = set(isolated)
    _real_reset_path(monkeypatch, hw, clock)
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)

    await srv.recovery_mechanism.reset_with_quiesce(argv, lambda m: None)

    assert hw["rescans"] == ["after"]


@pytest.mark.asyncio
async def test_a_failed_driver_wait_rescan_is_journaled_and_the_reset_runs_on(monkeypatch, clear_job_state):
    """A rescan write that fails is logged and journaled; the reset still runs to its end and its own
    rescan after it still runs."""
    hw, clock = _hw(), [0.0]
    srv.isolated_chips = {"31"}
    _real_reset_path(monkeypatch, hw, clock)
    events = []
    monkeypatch.setattr(base, "health_event", lambda kind, **f: events.append((kind, f)))
    fake = srv.asyncio.to_thread
    calls = []

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__self__", None) == RESCAN and not calls:
            calls.append(fn)
            raise PermissionError("read-only sysfs")
        return await fake(fn, *a, **k)

    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)

    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)

    assert rc == 1, "the chip did not come back in time, as before I81"
    assert ("reset_driver_wait_rescan", {"chips": ["31"], "ok": False, "error": "read-only sysfs"}) in events
    assert hw["rescans"] == ["after"] and srv.isolated_chips == set()


@pytest.mark.asyncio
async def test_a_cancelled_reset_cancels_a_pending_driver_wait_rescan(monkeypatch, clear_job_state):
    """An abandoned reset leaves no orphaned rescan task behind."""
    srv.isolated_chips = {"31"}
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    gate = asyncio.Event()
    started = []

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__self__", None) == RESCAN:
            started.append(time.monotonic())
            await gate.wait()
        return None

    async def run_scoped(argv, log, owner="[broker]health-gate", on_output=None):
        on_output(SETTLE + "\n")
        await asyncio.sleep(3600)

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run_scoped)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    task = asyncio.ensure_future(srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None))
    for _ in range(10):
        await asyncio.sleep(0)
    assert started, "the rescan started at the driver wait"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    for _ in range(5):
        await asyncio.sleep(0)
    assert all(t.done() for t in pending), "no rescan task left running"


@pytest.mark.asyncio
async def test_a_cancel_while_the_driver_wait_rescan_finishes_still_runs_the_reset_cleanup(
    monkeypatch, clear_job_state
):
    """tt-smi has exited but the driver-wait sysfs write is still running when the caller is cancelled
    (an operator's reset call dropped). The cancel still reaches the caller, but only after the cleanup
    every reset owes: the in-flight flag clear (or the dead-chip sampler stays blind), the rescan after
    the reset, the returned chip dropped from the isolated set, and the pollers back."""
    srv.isolated_chips = {"31"}
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(base, "returned_chips", lambda iso: sorted(iso))
    gate = asyncio.Event()
    rescans, pollers_calls = [], []
    real_sleep = asyncio.sleep

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__self__", None) == RESCAN:
            rescans.append("driver wait" if not rescans else "after")
            if len(rescans) == 1:
                await gate.wait()  # a slow sysfs write, still running when tt-smi exits
            return None
        return fn(*a, **k)

    async def run_scoped(argv, log, owner="[broker]health-gate", on_output=None):
        on_output(SETTLE + "\n")
        await real_sleep(0)
        return 1, "tt-smi exited"

    async def pollers(active, log):
        pollers_calls.append(active)
        return ["tt-telemetry"]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run_scoped)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    task = asyncio.ensure_future(srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None))
    for _ in range(10):
        await real_sleep(0)
    assert rescans == ["driver wait"] and srv.recovery_mechanism.reset_in_flight is True
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert srv.recovery_mechanism.reset_in_flight is False, "the dead-chip sampler is not left blind"
    assert rescans == ["driver wait", "after"], "the rescan after the reset still ran"
    assert srv.isolated_chips == set(), "the returned chip left the isolated set"
    assert pollers_calls == [False, True], "the pollers are back"
    gate.set()
    for _ in range(5):
        await real_sleep(0)


@pytest.mark.asyncio
async def test_output_after_a_cancelled_reset_starts_no_driver_wait_rescan(monkeypatch, clear_job_state):
    """After a mid-reset cancel the backend's shielded output reader keeps feeding the callback. A driver
    wait marker that arrives then starts no rescan (it would be nobody's to cancel); the caller's own
    callback still sees the output."""
    srv.isolated_chips = {"31"}
    monkeypatch.setattr(base, "health_event", lambda *a, **k: None)
    rescans, seen, feed = [], [], []

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__self__", None) == RESCAN:
            rescans.append(fn)
        return None

    async def run_scoped(argv, log, owner="[broker]health-gate", on_output=None):
        feed.append(on_output)
        on_output("Issuing USER_RESET to all chips\n")
        await asyncio.sleep(3600)

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run_scoped)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    task = asyncio.ensure_future(
        srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None, on_output=seen.append)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    feed[0](SETTLE + "\n")  # the shielded reader, still running after the cancel
    for _ in range(5):
        await asyncio.sleep(0)
    assert rescans == [], "no rescan starts after the reset was cancelled"
    assert seen[-1] == SETTLE + "\n"
