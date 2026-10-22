# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I78: the I81 rescan at tt-smi's driver wait repeats until every isolated chip is back.

tt-smi -glx_reset re-powers every tray, settles 30 s and then waits up to 100 s for all 32 chips in
/dev/tenstorrent. A chip removed for dead-chip isolation only comes back on a PCI rescan, and one
whose link trains late in that wait misses I81's single rescan. I78 repeats the rescan, bounded,
from the same trigger: the settle's last count, which both reset backends deliver only once the
settle is over. Nothing here touches a device: fake backends, a fake sysfs, and a fake tt-smi child.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from tt_device_mcp import privileges
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import pcie_guard

_real_sleep = asyncio.sleep
SETTLE_START = "\033[93mWaiting for 30 seconds: 0\033[0m"
SETTLE = SETTLE_START + "".join(f"\r\033[93mWaiting for 30 seconds: {i}\033[0m" for i in range(1, 31)) + "\n"
MESH_RESET = ["tt-smi", "-glx_reset"]


class FakeSysfs:
    """Counts PCI rescans and decides which removed chips each one brings back."""

    def __init__(self, back_after: int = 1):
        self.rescans = 0
        self.at: list[float] = []
        self.back_after = back_after  # rescans needed before the removed chips reappear

    def rescan(self):
        self.rescans += 1
        self.at.append(time.time())
        return ["0000:00:01.0"]

    def returned(self, isolated):
        return sorted(isolated, key=int) if self.rescans >= self.back_after else []


def _mechanism(monkeypatch, isolated: set, sysfs: FakeSysfs, events: list, *, every=0, scoped=False, tmp=None):
    monkeypatch.setitem(privileges._LATCHED, "systemd", False)
    monkeypatch.setattr(pcie_guard, "rescan_tt_root_ports", sysfs.rescan)
    monkeypatch.setattr(recovery_base, "returned_chips", sysfs.returned)
    monkeypatch.setattr(recovery_base, "health_event", lambda name, **kw: events.append((name, kw)))
    monkeypatch.setattr(recovery_base, "DRIVER_WAIT_RESCAN_EVERY_SEC", every)

    async def fast_sleep(sec, *a, **kw):
        await _real_sleep(min(sec, 0.05))  # the 3 s post-reset settle; the file backend's 50 ms poll

    monkeypatch.setattr(recovery_base.asyncio, "sleep", fast_sleep)

    real_to_thread = asyncio.to_thread

    async def to_thread(fn, *a, **kw):
        # The fake rescan runs inline: on a loaded host a worker thread can outlast the few loop turns
        # a test waits after each output chunk, and the rescan count it reads would then lag.
        if fn is recovery_base._scoped_rescan:
            return fn(*a, **kw)
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(recovery_base.asyncio, "to_thread", to_thread)

    async def pollers(_active, _log):
        return []

    mechanism = recovery_base.RecoveryMechanism(
        current_boot_id=lambda: "boot",
        boot_btime_id=lambda: "0",
        scoped_reset_backend=lambda: scoped,
        set_device_pollers=pollers,
        set_device_op_detail=lambda _detail: None,
        begin_action_row=lambda *_a: None,
        write_action_log=lambda *_a: None,
        device_hold_episode_since=lambda: "",
        local_reset_dir=(lambda: tmp) if tmp is not None else None,
        isolated_chips=lambda: isolated,
    )

    async def no_ceiling(_log):
        return None

    mechanism._reapply_aiclk_ceiling = no_ceiling
    return mechanism


async def _settle(n: int = 10):
    for _ in range(n):
        await _real_sleep(0)


def _fake_glx_reset(mechanism, sysfs: FakeSysfs, isolated: set, chunks: list[str], seen: dict, wait_sec: float = 2.0):
    """A reset backend that streams ``chunks`` and, like tt-smi, succeeds only if every removed chip
    is back by the end of its driver wait (``wait_sec`` of real time here)."""

    async def run_scoped(argv, log, owner, on_output=None):
        for chunk in chunks:
            if on_output is not None:
                on_output(chunk)
            await _settle()
            seen.setdefault("rescans_after_chunk", []).append(sysfs.rescans)
        deadline = time.monotonic() + wait_sec
        while isolated and time.monotonic() < deadline:
            await _real_sleep(0.01)
        await _settle()
        seen["isolated_at_wait_end"] = set(isolated)
        seen["rescans_at_wait_end"] = sysfs.rescans
        return (0, "ok") if not isolated else (1, "Driver not loaded with 32 devices after 100 seconds")

    mechanism.run_scoped = run_scoped


@pytest.mark.asyncio
async def test_the_rescan_repeats_until_the_chip_is_back_and_then_stops(monkeypatch):
    """Two chips whose links train only by the third rescan: the repeat brings both back inside the
    wait, the reset exits 0, and the repeat stops there."""
    isolated, sysfs, events, seen = {"6", "13"}, FakeSysfs(back_after=3), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, [SETTLE], seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert rc == 0
    assert seen["isolated_at_wait_end"] == set() and seen["rescans_at_wait_end"] == 3
    assert sysfs.rescans == 4  # three in the wait, then the post-reset rescan
    returned = [kw for n, kw in events if n == "isolated_chips_returned"]
    assert returned and returned[0]["context"] == "mesh reset driver wait"
    assert [n for n, _ in events].count("reset_driver_wait_rescan") == 1
    assert mechanism.reset_in_flight is False


@pytest.mark.asyncio
async def test_the_repeat_gives_up_after_its_tries(monkeypatch):
    """A chip that never comes back: at most DRIVER_WAIT_RESCAN_TRIES rescans in the wait, journaled
    as spent; the reset fails as before and keeps its I39/I36 path."""
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(back_after=10_000), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, [SETTLE], seen, wait_sec=0.5)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert rc == 1
    assert seen["rescans_at_wait_end"] == recovery_base.DRIVER_WAIT_RESCAN_TRIES
    assert sysfs.rescans == recovery_base.DRIVER_WAIT_RESCAN_TRIES + 1
    spent = [kw for n, kw in events if n == "reset_driver_wait_rescan_spent"]
    assert spent == [{"tries": recovery_base.DRIVER_WAIT_RESCAN_TRIES, "still_isolated": ["6"]}]
    assert isolated == {"6"}


@pytest.mark.asyncio
async def test_the_repeat_ends_with_the_reset_and_never_delays_its_cleanup(monkeypatch):
    """tt-smi exits while the repeat waits out its period: the reset's cleanup runs at once (the wait
    is cut short, no further rescan), then the one post-reset rescan."""
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(back_after=10_000), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events, every=3600)
    _fake_glx_reset(mechanism, sysfs, isolated, [SETTLE], seen, wait_sec=0.05)

    t0 = time.monotonic()
    rc, _ = await asyncio.wait_for(mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None), timeout=10)

    assert time.monotonic() - t0 < 5
    assert rc == 1 and sysfs.rescans == 2  # the driver-wait rescan, then the post-reset one
    assert mechanism.reset_in_flight is False
    assert all(t.done() for t in asyncio.all_tasks() if t is not asyncio.current_task())


@pytest.mark.asyncio
async def test_a_cancelled_reset_stops_the_repeats(monkeypatch):
    isolated, sysfs, events = {"6"}, FakeSysfs(back_after=10_000), []
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events, every=0.01)
    hang = asyncio.Event()

    async def run_scoped(argv, log, owner, on_output=None):
        on_output(SETTLE)
        await hang.wait()

    mechanism.run_scoped = run_scoped
    task = asyncio.create_task(mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None))
    # Each repeat hops through the thread pool twice, so a loaded runner can take longer than a
    # fixed few tens of ms to reach the second rescan: wait for it, bounded.
    deadline = time.monotonic() + 5
    while sysfs.rescans < 2 and time.monotonic() < deadline:
        await _real_sleep(0.01)
    assert sysfs.rescans >= 2, "the repeat ran while the reset did"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = sysfs.rescans
    await _real_sleep(0.1)
    assert sysfs.rescans == after
    assert mechanism.reset_in_flight is False


@pytest.mark.asyncio
async def test_no_rescan_while_the_settle_counts(monkeypatch):
    """The settle's first count ('seconds: 0', printed as the re-power returns) and its running
    counts start nothing; only its last count does. A rescan before the settle ends can enumerate a
    chip whose firmware has not come up, which the kernel driver then never re-initialises."""
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    counts = [SETTLE_START] + [f"\r\033[93mWaiting for 30 seconds: {i}\033[0m" for i in range(1, 31)] + ["\n"]
    _fake_glx_reset(mechanism, sysfs, isolated, ["Issuing reset to all trays\n", *counts], seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert seen["rescans_after_chunk"][:31] == [0] * 31, "no rescan before the settle's last count"
    assert seen["rescans_after_chunk"][31] == 1
    assert rc == 0 and isolated == set()


# tt-smi's timed_wait as it really prints: the first count with end='', each later count after a \r
# with end='', and one newline once the settle is over. Scaled to a 3 s settle at 0.3 s a count.
_FAKE_TT_SMI = r"""
import pathlib, sys, time
d = pathlib.Path(sys.argv[2])
print("Issuing reset to all trays", flush=True)
sys.stdout.write("\033[93mWaiting for 3 seconds: 0\033[0m"); sys.stdout.flush()
for i in range(1, 4):
    time.sleep(0.3)
    if i == 3:
        (d / "settle_end").write_text(repr(time.time()))
    sys.stdout.write("\r\033[93mWaiting for 3 seconds: %d\033[0m" % i); sys.stdout.flush()
print(flush=True)
print("Waiting for driver load ... 0 seconds (found 31 devices)", flush=True)
time.sleep(0.6)
(d / "exit").write_text(repr(time.time()))
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [True, False], ids=["scoped-line-reader", "local-file-reader"])
async def test_both_backends_rescan_only_once_the_settle_is_over(monkeypatch, tmp_path, scoped):
    """Review finding: the trigger must be the same moment on both reset backends. The scoped backend
    reads stdout by line, the local one polls a file; with tt-smi's real \\r countdown both deliver the
    settle's last count only at its end, so on both the rescan runs after the settle and inside the
    driver wait, never at the re-power."""
    isolated, sysfs, events = {"6"}, FakeSysfs(), []
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events, scoped=scoped, tmp=tmp_path)
    if scoped:
        mechanism._reset_scope_argv = lambda argv: ("ttdev-reset-test", list(argv))
    # The fake carries -glx_reset, so it is a mesh reset (spec 04 I91); this test times the rescan,
    # not the ipmitool guard, whose unshare a sandboxed suite may not be allowed to run.
    mechanism._launch_argv = list
    argv = [sys.executable, "-c", _FAKE_TT_SMI, "-glx_reset", str(tmp_path)]

    rc, text = await asyncio.wait_for(mechanism.reset_with_quiesce(argv, lambda _m: None), timeout=30)

    assert rc == 0, text
    settle_end = float((tmp_path / "settle_end").read_text())
    child_exit = float((tmp_path / "exit").read_text())
    assert sysfs.at, "a rescan ran"
    assert sysfs.at[0] >= settle_end, f"rescan {settle_end - sysfs.at[0]:.2f}s before the settle ended"
    assert sysfs.at[0] < child_exit, "the rescan ran inside tt-smi's driver wait, not after it"
    assert isolated == set()
