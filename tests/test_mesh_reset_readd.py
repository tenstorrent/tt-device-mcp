# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I78: a mesh reset re-adds a chip the broker removed from the kernel once its pulse fires.

tt-smi -glx_reset pulses every tray, settles 30 s and then waits for all 32 chips in /dev/tenstorrent.
A chip removed for dead-chip isolation is reset by the pulse but only a PCI rescan brings its node
back, so without a rescan inside that wait the first reset fails and a second one is needed. These
tests drive reset_with_quiesce with a fake reset backend and a fake sysfs; nothing touches a device.
"""

from __future__ import annotations

import asyncio

import pytest

from tt_device_mcp import privileges
from tt_device_mcp.health.recovery import base as recovery_base

_real_sleep = asyncio.sleep
PULSE_LINE = "\033[93mWaiting for 30 seconds: 0\033[0m"
MESH_RESET = ["tt-smi", "-glx_reset"]


class FakeSysfs:
    """Counts PCI rescans and decides which removed chips each one brings back."""

    def __init__(self, back_after: int = 1):
        self.rescans = 0
        self.back_after = back_after  # rescans needed before the removed chips reappear

    def path(self, p):
        sysfs = self

        class _Path:
            def __init__(self, raw):
                self.raw = str(raw)

            def write_text(self, _text):
                assert self.raw == "/sys/bus/pci/rescan", self.raw
                sysfs.rescans += 1

        return _Path(p)

    def returned(self, isolated):
        return sorted(isolated, key=int) if self.rescans >= self.back_after else []


def _mechanism(monkeypatch, isolated: set, sysfs: FakeSysfs, events: list):
    monkeypatch.setitem(privileges._LATCHED, "systemd", False)
    monkeypatch.setattr(recovery_base, "Path", sysfs.path)
    monkeypatch.setattr(recovery_base, "returned_chips", sysfs.returned)
    monkeypatch.setattr(recovery_base, "health_event", lambda name, **kw: events.append((name, kw)))
    monkeypatch.setattr(recovery_base, "MESH_PULSE_RESCAN_EVERY_SEC", 0)

    async def fast_sleep(_sec, *a, **kw):
        await _real_sleep(0)

    monkeypatch.setattr(recovery_base.asyncio, "sleep", fast_sleep)

    async def pollers(_active, _log):
        return []

    mechanism = recovery_base.RecoveryMechanism(
        current_boot_id=lambda: "boot",
        boot_btime_id=lambda: "0",
        scoped_reset_backend=lambda: False,
        set_device_pollers=pollers,
        set_device_op_detail=lambda _detail: None,
        begin_action_row=lambda *_a: None,
        write_action_log=lambda *_a: None,
        device_hold_episode_since=lambda: "",
        isolated_chips=lambda: isolated,
    )

    async def no_ceiling(_log):
        return None

    mechanism._reapply_aiclk_ceiling = no_ceiling
    return mechanism


async def _settle(n: int = 10):
    for _ in range(n):
        await asyncio.sleep(0)


def _fake_glx_reset(mechanism, sysfs: FakeSysfs, isolated: set, chunks: list[str], seen: dict):
    """A reset backend that streams ``chunks`` and, like pyluwen, succeeds only if every removed
    chip is back by the end of its wait."""

    async def run_scoped(argv, log, owner, on_output=None):
        seen["on_output"] = on_output is not None
        for i, chunk in enumerate(chunks):
            if on_output is not None:
                on_output(chunk)
            await _settle()
            seen.setdefault("rescans_after_chunk", []).append(sysfs.rescans)
        # pyluwen's wait for the full chip count: up to 2 s of real time here, ending early once the
        # removed chips are back or the re-add loop has spent its tries (rescans run in threads).
        for _ in range(200):
            if not isolated or sysfs.rescans >= recovery_base.MESH_PULSE_RESCAN_TRIES:
                break
            await _real_sleep(0.01)
        await _settle()
        seen["isolated_at_wait_end"] = set(isolated)
        return (0, "ok") if not isolated else (1, "Didn't find all 32 chips in /dev/tenstorrent after 100 seconds")

    mechanism.run_scoped = run_scoped


@pytest.mark.asyncio
async def test_the_removed_chip_is_re_added_after_the_pulse_and_the_reset_heals(monkeypatch):
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, ["Resetting 6U trays\n", PULSE_LINE], seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert rc == 0
    assert seen["isolated_at_wait_end"] == set()
    assert isolated == set()
    names = [n for n, _ in events]
    assert "mesh_reset_pulse_seen" in names
    returned = [kw for n, kw in events if n == "isolated_chips_returned"]
    assert returned and returned[0]["context"] == "mesh reset pulse"
    assert mechanism.reset_in_flight is False


@pytest.mark.asyncio
async def test_no_rescan_runs_before_the_pulse_line(monkeypatch):
    """Spec 04 I69: a removed chip may be hung, so its node never comes back before the pulse."""
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    chunks = ["Starting reset on 32 devices\n", "Resetting 6U trays\n", "IPMI ok\n", PULSE_LINE]
    _fake_glx_reset(mechanism, sysfs, isolated, chunks, seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert seen["rescans_after_chunk"][:3] == [0, 0, 0]
    assert rc == 0
    assert "mesh_reset_pulse_seen" in [n for n, _ in events]


@pytest.mark.asyncio
async def test_no_rescan_when_the_pulse_line_never_comes(monkeypatch):
    """A pulse that fails exits tt-smi before its settle line: only the post-reset rescan runs."""
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, ["Resetting 6U trays\n", "IPMI failed\n"], seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert rc == 1
    assert seen["isolated_at_wait_end"] == {"6"}
    assert "mesh_reset_pulse_seen" not in [n for n, _ in events]
    assert sysfs.rescans == 1  # the existing post-reset rescan, after tt-smi exited


@pytest.mark.asyncio
async def test_the_rescan_repeats_until_the_chip_is_back_and_then_stops(monkeypatch):
    isolated, sysfs, events, seen = {"6", "13"}, FakeSysfs(back_after=3), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, [PULSE_LINE], seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert rc == 0
    assert seen["isolated_at_wait_end"] == set()
    # Three rescans during the wait brought both chips back; the post-reset rescan adds one more.
    assert sysfs.rescans == 4


@pytest.mark.asyncio
async def test_the_rescan_gives_up_after_its_tries(monkeypatch):
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(back_after=10_000), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, [PULSE_LINE], seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert rc == 1
    assert sysfs.rescans == recovery_base.MESH_PULSE_RESCAN_TRIES + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("isolated", "argv"),
    [(set(), MESH_RESET), ({"6"}, ["tt-smi", "-r"])],
    ids=["no-removed-chip", "not-a-mesh-reset"],
)
async def test_no_extra_rescan_without_a_removed_chip_or_for_a_non_mesh_reset(monkeypatch, isolated, argv):
    sysfs, events, seen = FakeSysfs(back_after=10_000), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, [PULSE_LINE], seen)

    await mechanism.reset_with_quiesce(argv, lambda _m: None)

    assert seen["on_output"] is False  # no callback attached: the backend keeps its plain path
    assert "mesh_reset_pulse_seen" not in [n for n, _ in events]
    assert sysfs.rescans == 1  # only the post-reset rescan


@pytest.mark.asyncio
async def test_a_pulse_line_split_across_chunks_still_counts(monkeypatch):
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    _fake_glx_reset(mechanism, sysfs, isolated, ["Resetting 6U trays\n\033[93mWaiting for", " 30 sec", "onds: 0"], seen)

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None)

    assert seen["rescans_after_chunk"][:2] == [0, 0]
    assert rc == 0
    assert isolated == set()


@pytest.mark.asyncio
async def test_the_callers_output_callback_still_gets_every_chunk(monkeypatch):
    isolated, sysfs, events, seen = {"6"}, FakeSysfs(), [], {}
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    chunks = ["Resetting 6U trays\n", PULSE_LINE, "\rWaiting for 30 seconds: 1", "\nAll chips back\n"]
    _fake_glx_reset(mechanism, sysfs, isolated, chunks, seen)
    got: list[str] = []

    rc, _ = await mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None, on_output=got.append)

    assert got == chunks
    assert rc == 0


@pytest.mark.asyncio
async def test_a_cancelled_reset_stops_the_rescans(monkeypatch):
    isolated, sysfs, events = {"6"}, FakeSysfs(back_after=10_000), []
    mechanism = _mechanism(monkeypatch, isolated, sysfs, events)
    hang = asyncio.Event()

    async def run_scoped(argv, log, owner, on_output=None):
        on_output(PULSE_LINE)
        await hang.wait()

    mechanism.run_scoped = run_scoped
    task = asyncio.create_task(mechanism.reset_with_quiesce(MESH_RESET, lambda _m: None))
    await _settle(5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = sysfs.rescans
    await _settle(20)
    assert sysfs.rescans == after
    assert mechanism.reset_in_flight is False
