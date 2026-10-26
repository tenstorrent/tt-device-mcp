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
import threading
import time

import pytest

from tt_device_mcp import privileges
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import pcie_guard
from tt_device_mcp.health.recovery.stages import bridge_reset

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
    monkeypatch.setattr(bridge_reset, "bridge_reset_enabled", lambda: False)  # no SBR unless a test arms it

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


# --- spec 04 I97: the SBR in tt-smi's driver wait, and a driver wait that gave up ----------------

GAVE_UP = "Didn't find all 32 chips in /dev/tenstorrent after 100 seconds, giving up\n"


def _arm_sbr(monkeypatch, sysfs: FakeSysfs, *, heals: bool, started=None, hold_sec: float = 0.0) -> list:
    """Give the process the SBR capability. Each SBR is recorded as (chip, rescans so far); ``heals``:
    the next rescan brings the chip back. ``started`` (a threading.Event) is set as the write begins,
    which then takes ``hold_sec``. The fabric and AICLK debts it notes are recorded, not kept."""
    sbrs: list = []

    def reset_removed_chip(idx):
        if started is not None:
            started.set()
        time.sleep(hold_sec)
        sbrs.append((idx, sysfs.rescans))
        if heals:
            sysfs.back_after = sysfs.rescans + 1
        return True

    monkeypatch.setattr(bridge_reset, "bridge_reset_enabled", lambda: True)
    monkeypatch.setattr(bridge_reset, "reset_removed_chip", reset_removed_chip)
    monkeypatch.setattr(recovery_base.FABRIC_DEBT, "note_reset", lambda why: None)
    monkeypatch.setattr(recovery_base.CEILING, "mark_owed", lambda why: None)
    return sbrs


@pytest.mark.asyncio
@pytest.mark.parametrize("heals", [True, False], ids=["sbr-heals", "chip-stays-out"])
async def test_an_sbr_follows_the_rescans_that_left_a_chip_out_in_the_driver_wait(monkeypatch, heals):
    """Four rescans in tt-smi's driver wait leave chip 6 out: one SBR through its recorded bridge, then
    the rescans go on (they add it back, I69). A chip the SBR brought back is in by the next rescan and
    the reset succeeds; one it did not keeps the rescans to their tries and the reset fails as before."""
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(back_after=10_000), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    sbrs = _arm_sbr(monkeypatch, sysfs, heals=heals)
    _fake_glx_reset(mechanism, sysfs, isolated, [SETTLE], seen, wait_sec=0.5)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert sbrs == [("6", recovery_base.DRIVER_WAIT_SBR_AFTER_TRIES)], "one SBR, after the fourth rescan"
    assert [kw for n, kw in events if n == "reset_driver_wait_sbr"] == [{"chip": "6", "ok": True}]
    if heals:
        assert rc == 0 and isolated == set()
        assert seen["rescans_at_wait_end"] == recovery_base.DRIVER_WAIT_SBR_AFTER_TRIES + 1
    else:
        assert rc == 1 and isolated == {"6"}
        assert seen["rescans_at_wait_end"] == recovery_base.DRIVER_WAIT_RESCAN_TRIES


@pytest.mark.asyncio
async def test_a_reset_that_ends_during_the_driver_wait_sbr_takes_no_more_rescans_in_it(monkeypatch):
    """tt-smi exits while the SBR's write is in flight: the write finishes (the cleanup waits for it),
    the loop rescans no more, and the one rescan after the reset follows."""
    isolated, sysfs, events = {"6"}, FakeSysfs(back_after=10_000), []
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    started = threading.Event()
    sbrs = _arm_sbr(monkeypatch, sysfs, heals=False, started=started, hold_sec=0.3)

    async def run_scoped(argv, log, owner, on_output=None):
        on_output(SETTLE)
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            await _real_sleep(0.01)
        return 1, "Driver not loaded with 32 devices after 100 seconds"

    mechanism.run_scoped = run_scoped
    rc, _ = await asyncio.wait_for(mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None), timeout=10)

    assert rc == 1 and sbrs == [("6", recovery_base.DRIVER_WAIT_SBR_AFTER_TRIES)]
    assert sysfs.rescans == recovery_base.DRIVER_WAIT_SBR_AFTER_TRIES + 1  # none after the SBR in the wait
    assert mechanism.reset_in_flight is False


@pytest.mark.asyncio
async def test_no_sbr_in_the_driver_wait_without_the_capability(monkeypatch):
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(back_after=10_000), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    monkeypatch.setattr(bridge_reset, "reset_removed_chip", lambda idx: pytest.fail("SBR without setpci"))
    _fake_glx_reset(mechanism, sysfs, isolated, [SETTLE], seen, wait_sec=0.5)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert rc == 1 and seen["rescans_at_wait_end"] == recovery_base.DRIVER_WAIT_RESCAN_TRIES
    assert "reset_driver_wait_sbr" not in [n for n, _ in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [True, False], ids=["gave-up", "completed"])
async def test_a_reset_whose_tt_smi_gave_up_at_its_driver_wait_is_logged_as_failed(monkeypatch, gave_up):
    """tt-smi gives up when a chip never shows up at its driver wait: no POST_RESET and no board
    re-init ran, so the reset's one line (spec 03 I83) says FAILED rather than "no POST_RESET
    failure", and the mechanism remembers it for the ladder."""
    sysfs, events, logs = FakeSysfs(), [], []
    mechanism = _mechanism(monkeypatch, set(), sysfs, events)

    async def run_scoped(argv, log, owner, on_output=None):
        on_output(SETTLE)
        if gave_up:
            on_output(GAVE_UP)
            return 1, SETTLE + GAVE_UP
        return 0, "ok"

    mechanism.run_scoped = run_scoped
    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, logs.append)

    failed = [m for m in logs if m.startswith("mesh reset FAILED")]
    if gave_up:
        assert rc == 1 and failed and failed[0].startswith("mesh reset FAILED (rc 1)")
        assert ("reset_driver_wait_gave_up", {"rc": 1, "tray_repower": True}) in events
        assert failed[0].startswith("mesh reset FAILED (rc 1): the tray re-power ran")
        assert not any("no POST_RESET failure" in m for m in logs)
    else:
        assert rc == 0 and not failed and any("no POST_RESET failure" in m for m in logs)
        assert "reset_driver_wait_gave_up" not in [n for n, _ in events]
    assert mechanism.last_reset_driver_wait_failed is gave_up


# What tt-smi 6.7's -glx_reset prints, in order: the PCIe link and user resets, the tray re-power (an
# IPMI UBB reset), the 30 s settle, then the driver wait.
REPOWER = "Executing command: sudo ipmitool raw 0x30 0x8B 0xF 0xFF 0x3 0xF\n"
# When that IPMI call fails, tt-smi retries it in its legacy mode; when that fails too it raises and exits
# before the settle, so no tray was re-powered.
IPMI_FAILED = "WARNING! Galaxy no-retimer IPMI reset (op_mode 0x3) failed:\nUnable to send RAW command\n"
REPOWER_LEGACY = "Executing command: sudo ipmitool raw 0x30 0x8B 0xF 0xFF 0x0 0xF\n"
IPMI_GAVE_UP = "RuntimeError: IPMI command failed: Unable to send RAW command\n"
# tt-smi 6.7's last line when chips never showed up in its driver wait.
PANIC_GAVE_UP = (
    "pyo3_runtime.PanicException: Didn't find all 32 chips in /dev/tenstorrent after 100 seconds... giving up\n"
)


def test_the_tray_repower_is_read_from_tt_smi_output():
    assert recovery_base.glx_tray_repower_ran(REPOWER)
    assert recovery_base.glx_tray_repower_ran(SETTLE)
    assert recovery_base.glx_tray_repower_ran("x\nExecuting command: ipmitool raw 0x30 0x8b 0xF 0xFF 0x3 0xF")
    assert recovery_base.glx_tray_repower_ran(REPOWER + IPMI_FAILED + REPOWER_LEGACY + SETTLE), "the fallback ran"
    assert recovery_base.glx_tray_repower_ran(REPOWER), "tt-smi cut in its IPMI call: the BMC may have re-powered"
    assert not recovery_base.glx_tray_repower_ran(REPOWER + IPMI_FAILED + REPOWER_LEGACY + IPMI_GAVE_UP)
    assert not recovery_base.glx_tray_repower_ran("Issuing RESET_PCIE_LINK on 31 devices\n")
    assert not recovery_base.glx_tray_repower_ran("")
    assert not recovery_base.glx_tray_repower_ran(None)


@pytest.mark.asyncio
async def test_a_driver_wait_give_up_after_the_tray_repower_says_the_repower_ran(monkeypatch):
    """The partial-drop case seen on a Galaxy: the re-power ran, the isolated chip never came back in
    tt-smi's driver wait. The line says the re-power ran, so a reader does not take the failed reset for
    one that never reset anything; the ladder treats it as the failed reset it is."""
    sysfs, events, logs = FakeSysfs(), [], []
    mechanism = _mechanism(monkeypatch, set(), sysfs, events)

    async def run_scoped(argv, log, owner, on_output=None):
        out = "Issuing RESET_PCIE_LINK on 31 devices\n" + REPOWER + SETTLE + GAVE_UP
        on_output(out)
        return 1, out

    mechanism.run_scoped = run_scoped
    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, logs.append)

    assert rc == 1
    assert any(m.startswith("mesh reset FAILED (rc 1): the tray re-power ran, but tt-smi gave up") for m in logs)
    assert ("reset_driver_wait_gave_up", {"rc": 1, "tray_repower": True}) in events
    assert "reset_did_not_run" not in [n for n, _ in events]
    assert mechanism.last_reset_driver_wait_failed is True


@pytest.mark.asyncio
async def test_a_two_chip_partial_drop_that_stays_out_through_the_repower_is_a_failed_reset_that_ran(monkeypatch):
    """A partial drop of two chips, as replayed from a Galaxy: both stay off the bus through the tray
    re-power, the driver-wait rescans and one SBR of each chip's bridge. tt-smi gives up, so the reset
    ran and FAILED: the line says the re-power ran, both chips stay isolated for the ladder, and it is
    never logged as a reset that did not run or as a clean end."""
    isolated, sysfs, events, seen, logs = {"7", "24"}, FakeSysfs(back_after=10_000), [], {}, []
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    sbrs = _arm_sbr(monkeypatch, sysfs, heals=False)
    _fake_glx_reset(mechanism, sysfs, isolated, [REPOWER, SETTLE], seen, wait_sec=0.5)
    streamed = mechanism.run_scoped

    async def run_scoped(argv, log, owner, on_output=None):
        rc, _ = await streamed(argv, log, owner, on_output)
        on_output(PANIC_GAVE_UP)
        return rc, "Issuing RESET_PCIE_LINK on 30 devices before IPMI reset...\n" + REPOWER + SETTLE + PANIC_GAVE_UP

    mechanism.run_scoped = run_scoped
    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, logs.append)

    assert rc == 1 and isolated == {"7", "24"}
    assert sbrs == [(c, recovery_base.DRIVER_WAIT_SBR_AFTER_TRIES) for c in ("7", "24")], "one SBR per chip"
    assert seen["rescans_at_wait_end"] == recovery_base.DRIVER_WAIT_RESCAN_TRIES
    assert any(m.startswith("chip(s) ['7', '24'] still missing after") for m in logs)
    assert any(m.startswith("mesh reset FAILED (rc 1): the tray re-power ran, but tt-smi gave up") for m in logs)
    assert ("reset_driver_wait_gave_up", {"rc": 1, "tray_repower": True}) in events
    assert "reset_did_not_run" not in [n for n, _ in events]
    assert not any("no POST_RESET failure" in m for m in logs)
    assert mechanism.last_reset_driver_wait_failed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "out,why,ipmi_failed",
    [
        ("Error: could not open /dev/tenstorrent/0\n", "tt-smi exited before the tray re-power", False),
        (REPOWER + IPMI_FAILED + REPOWER_LEGACY + IPMI_GAVE_UP, "tt-smi's IPMI tray re-power failed", True),
    ],
    ids=["exited-before", "ipmi-failed"],
)
async def test_a_mesh_reset_that_exited_before_the_tray_repower_is_logged_as_not_run(
    monkeypatch, out, why, ipmi_failed
):
    """tt-smi exits non-zero before it re-powers the trays, or its IPMI call to re-power them failed in
    both modes: nothing was reset. The line says the reset did not run and why (never "no POST_RESET
    failure"), journaled reset_did_not_run; it is no driver-wait give-up."""
    sysfs, events, logs = FakeSysfs(), [], []
    mechanism = _mechanism(monkeypatch, set(), sysfs, events)

    async def run_scoped(argv, log, owner, on_output=None):
        on_output(out)
        return 1, out

    mechanism.run_scoped = run_scoped
    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, logs.append)

    assert rc == 1
    assert f"mesh reset FAILED (rc 1): reset did not run — {why}" in logs
    assert ("reset_did_not_run", {"rc": 1, **({"ipmi_failed": True} if ipmi_failed else {})}) in events
    assert not any("no POST_RESET failure" in m for m in logs)
    assert mechanism.last_reset_driver_wait_failed is False
