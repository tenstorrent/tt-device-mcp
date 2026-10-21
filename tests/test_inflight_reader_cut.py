# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 03 I83: during a mesh reset's driver wait, the readers of a chip still all-ones are cut, and the
chip is detached (removed from the kernel) BEFORE any of them is killed.

The driver's fd-close path sends a chip a power-state message unless the device is marked detached, and
only the PCI remove marks it. Killing a reader of an off-bus chip that is still attached makes the kernel
read that chip, the very access the cut exists to stop. So: detach first, then kill; a chip that cannot
be detached leaves every reader alone, and the hold with its bounded recovery decides.
"""

import asyncio
import os
import signal

import pytest

from tests.test_glx_reset_isolated_rescan import SETTLE
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders
from tt_device_mcp.health import ALL_ONES

OK = 0x1234


class _World:
    """The sampler's deps as the in-flight watch sees them, recording every call in order."""

    def __init__(self, monkeypatch, *, readers=(4242,), complete=True, detach_ok=True, isolate=True):
        self.order: list = []
        self.events: list = []
        self.reads: list = []
        self.skips: list = []
        self.isolated: set = set()
        self.kill_gate: asyncio.Event | None = None
        self.readers = list(readers)
        self.complete = complete
        self.detach_ok = detach_ok
        deps = srv.sampler._deps
        monkeypatch.setattr(srv.recovery_mechanism, "reset_driver_wait", True)
        monkeypatch.setattr(deps, "read_inflight_heartbeats", self.read)
        monkeypatch.setattr(deps, "find_inflight_readers", lambda: (list(self.readers), self.complete))
        monkeypatch.setattr(deps, "isolate_chip", self.detach if isolate else None)
        monkeypatch.setattr(deps, "isolated_chips", lambda: self.isolated)
        monkeypatch.setattr(deps, "set_device_pollers", self.pollers)
        monkeypatch.setattr(deps, "owe_device_pollers", lambda why: self.order.append(("owe", why)))
        monkeypatch.setattr(deps, "cut_inflight_readers", self.kill)
        monkeypatch.setattr(deps, "health_event", lambda kind, **f: self.events.append((kind, f)))
        monkeypatch.setattr(srv.sampler, "inflight_strikes", {})
        monkeypatch.setattr(srv.sampler, "inflight_cut", frozenset())
        monkeypatch.setattr(srv.sampler, "_inflight_pending", frozenset())
        monkeypatch.setattr(srv.sampler, "_inflight_task", None)

    def read(self, skip):
        self.skips.append(set(skip))
        return {i: v for i, v in self.reads.pop(0).items() if i not in skip}

    def detach(self, idx):
        self.order.append(("detach", idx))
        return self.detach_ok if not callable(self.detach_ok) else self.detach_ok(idx)

    async def pollers(self, on, log=None):
        self.order.append(("pollers", on))

    async def kill(self, reason):
        if self.kill_gate is not None:
            await self.kill_gate.wait()
        self.order.append(("kill", reason))
        return []

    def kinds(self):
        return [k for k, _ in self.events]

    async def samples(self, *beats):
        for b in beats:
            self.reads.append(b)
            await srv.sampler.watch_reset_in_flight()
        task = srv.sampler._inflight_task
        if task is not None and self.kill_gate is None:
            await task


def _steps(order):
    return [o[0] if o[0] != "detach" else f"detach{o[1]}" for o in order if o[0] != "owe"]


@pytest.mark.asyncio
async def test_two_all_ones_reads_detach_the_chip_before_its_readers_are_killed(monkeypatch):
    w = _World(monkeypatch)
    await w.samples({"0": OK, "1": ALL_ONES}, {"0": OK, "1": ALL_ONES})
    assert _steps(w.order) == ["detach1", "pollers", "kill"], w.order
    assert w.order[1] == ("pollers", False)
    assert w.isolated == {"1"}, "the post-reset path must see the chip as isolated (rescan, full check)"
    assert "reset_inflight_off_bus" in w.kinds()


@pytest.mark.asyncio
async def test_one_all_ones_read_does_nothing(monkeypatch):
    w = _World(monkeypatch)
    await w.samples({"0": OK, "1": ALL_ONES}, {"0": OK, "1": OK})
    assert w.order == [] and w.events == []


@pytest.mark.asyncio
async def test_a_healthy_reset_reads_once_per_sample_and_does_nothing(monkeypatch):
    w = _World(monkeypatch)
    await w.samples({"0": OK, "1": OK}, {"0": OK, "1": OK}, {"0": OK, "1": OK})
    assert w.order == [] and w.events == []


@pytest.mark.asyncio
async def test_a_chip_that_cannot_be_detached_keeps_every_reader_alive(monkeypatch):
    w = _World(monkeypatch, detach_ok=False)
    await w.samples({"1": ALL_ONES}, {"1": ALL_ONES})
    assert _steps(w.order) == ["detach1"], f"a reader was killed without a detach: {w.order}"
    refused = [f for k, f in w.events if k == "reset_inflight_cut_refused"]
    assert refused and refused[0]["host_at_risk"] and refused[0]["chips"] == ["1"]
    assert w.isolated == set()


@pytest.mark.asyncio
async def test_one_failed_detach_of_two_kills_no_reader(monkeypatch):
    """A reader may hold both chips: the one detach that worked does not make its kill safe."""
    w = _World(monkeypatch, detach_ok=lambda idx: idx == "1")
    await w.samples({"1": ALL_ONES, "2": ALL_ONES}, {"1": ALL_ONES, "2": ALL_ONES})
    assert _steps(w.order) == ["detach1", "detach2"], w.order
    assert "reset_inflight_cut_refused" in w.kinds()


@pytest.mark.asyncio
async def test_with_no_detach_wired_nothing_is_killed(monkeypatch):
    w = _World(monkeypatch, isolate=False)
    await w.samples({"1": ALL_ONES}, {"1": ALL_ONES})
    assert w.order == []
    assert "reset_inflight_cut_refused" in w.kinds()


@pytest.mark.asyncio
async def test_no_reader_means_no_detach_and_no_kill(monkeypatch):
    """The reset keeps its chips: with nobody reading, the endpoint stays for the reset to finish."""
    w = _World(monkeypatch, readers=())
    await w.samples({"1": ALL_ONES}, {"1": ALL_ONES})
    assert w.order == []
    assert "reset_inflight_readers_cut" in w.kinds()


@pytest.mark.asyncio
async def test_an_incomplete_holder_scan_counts_as_a_reader(monkeypatch):
    w = _World(monkeypatch, readers=(), complete=False)
    await w.samples({"1": ALL_ONES}, {"1": ALL_ONES})
    assert _steps(w.order) == ["detach1", "pollers", "kill"], w.order


@pytest.mark.asyncio
async def test_a_chip_on_its_first_strike_defers_the_cut_until_it_is_settled(monkeypatch):
    """A reader of the confirmed chip may also hold the suspect one, still attached: no kill yet."""
    w = _World(monkeypatch)
    await w.samples({"1": ALL_ONES, "2": OK}, {"1": ALL_ONES, "2": ALL_ONES})
    assert w.order == [], "killed while chip 2 was still attached and unconfirmed"
    await w.samples({"2": ALL_ONES})
    assert _steps(w.order) == ["detach1", "detach2", "pollers", "kill"], w.order


@pytest.mark.asyncio
async def test_a_confirmed_chip_is_not_read_again_in_this_reset(monkeypatch):
    w = _World(monkeypatch)
    await w.samples({"0": OK, "1": ALL_ONES}, {"0": OK, "1": ALL_ONES}, {"0": OK})
    assert w.skips[-1] == {"1"}


@pytest.mark.asyncio
async def test_a_chip_confirmed_while_a_cut_runs_gets_its_own_cut(monkeypatch):
    w = _World(monkeypatch)
    w.kill_gate = asyncio.Event()
    await w.samples({"1": ALL_ONES, "2": OK}, {"1": ALL_ONES, "2": OK})
    await asyncio.sleep(0)
    await w.samples({"2": ALL_ONES}, {"2": ALL_ONES})
    first = srv.sampler._inflight_task
    assert srv.sampler._inflight_pending == frozenset({"2"})
    w.kill_gate.set()
    await first
    await w.samples({})
    await srv.sampler._inflight_task
    assert _steps(w.order) == ["detach1", "pollers", "kill", "detach2", "pollers", "kill"], w.order


@pytest.mark.asyncio
async def test_the_reset_end_clears_the_watch(monkeypatch):
    w = _World(monkeypatch)
    await w.samples({"1": ALL_ONES})
    srv.sampler.end_inflight_watch()
    assert srv.sampler.inflight_strikes == {} and srv.sampler.inflight_cut == frozenset()
    assert srv.recovery_mechanism.reset_driver_wait is False
    assert w.order == []


# --- the driver-wait flag ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_flag_rises_at_the_driver_wait_on_every_mesh_reset_and_not_before(monkeypatch):
    mech = srv.recovery_mechanism
    monkeypatch.setattr(mech, "reset_driver_wait", False)
    monkeypatch.setattr(mech, "_isolated_chips", lambda: set())
    seen = []
    stream, state = mech._driver_wait_rescan_hook(["tt-smi", "-glx_reset"], seen.append, lambda m: None)
    stream("Issuing USER_RESET to all chips\n")
    assert mech.reset_driver_wait is False, "the BMC step is not over yet"
    stream(SETTLE + "\n")
    assert mech.reset_driver_wait is True
    assert state.get("task") is None, "no isolated chip: the I81 rescan does not start"
    assert seen[0].startswith("Issuing"), "the caller's own output callback still gets every chunk"


@pytest.mark.asyncio
async def test_a_reset_that_is_not_a_mesh_reset_never_raises_the_flag(monkeypatch):
    mech = srv.recovery_mechanism
    monkeypatch.setattr(mech, "reset_driver_wait", False)
    cb = object()
    stream, state = mech._driver_wait_rescan_hook(["tt-smi", "-r", "0"], cb, lambda m: None)
    assert stream is cb and state == {}
    assert mech.reset_driver_wait is False


# --- who the cut may signal ----------------------------------------------------------------------


def test_the_cut_never_selects_the_broker_the_reset_or_their_children(monkeypatch):
    me = os.getpid()
    internal, in_reset, child, foreign = 101, 102, 103, 104
    monkeypatch.setattr(srv, "is_broker_internal", lambda pid: pid == internal)
    monkeypatch.setattr(srv, "in_broker_scope", lambda pid, prefixes=(): pid == in_reset)
    monkeypatch.setattr(srv, "descends_from", lambda pid, anc: pid == child and anc == me)
    picked = [p for p in (me, internal, in_reset, child, foreign) if srv._inflight_reader(DeviceHolder(pid=p, uid=0))]
    assert picked == [foreign]


def test_reclaim_select_replaces_the_tenant_rule(monkeypatch):
    """Root-owned infrastructure is no tenant, yet the in-flight cut must reach it; select says so."""
    scan = HolderScan(holders=[DeviceHolder(pid=7, uid=0), DeviceHolder(pid=8, uid=0)], complete=True)
    signals = []
    res = reclaim_foreign_holders(
        grace_sec=0,
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _: None,
        rescan=lambda: scan if not signals else HolderScan(holders=[], complete=True),
        read_starttime=lambda pid: "fixed",
        getpid=lambda: 1,
        getpgid=lambda pid: pid,  # every pid its own group: none is the broker's
        select=lambda h: h.pid == 8,
    )
    assert signals == [(8, signal.SIGTERM)]
    assert [h.pid for h in res.signalled] == [8]
