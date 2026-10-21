# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 03 I83: after a mesh reset whose POST_RESET failed on a chip that now reads all-ones, the chip is
detached (removed from the kernel) BEFORE any of its readers is killed.

Driver facts the trigger rests on: USER_RESET sets the chip's needs_hw_init, so until POST_RESET every
telemetry read fails with ENODATA and an fd close sends the chip nothing; a watch in the driver wait sees
no all-ones at all. POST_RESET clears needs_hw_init even when it fails, and tt-smi then prints
`POST_RESET failed for device /dev/tenstorrent/N.` and exits. From there a read is a real bus read and an
fd close of an attached device sends the chip a power-state message; only the PCI remove marks it
detached. So: the failed line, one confirming read, detach, then kill; a chip that cannot be detached
leaves every reader alone, and the hold with its bounded recovery decides.
"""

import asyncio
import errno
import os
import signal
import threading
import time

import pytest

from tests.test_glx_reset_isolated_rescan import SETTLE, _hw, _real_reset_path
from tests.test_no_repeat_failed_rung import EXPECTED
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.device_holders import DeviceHolder, HolderScan, reclaim_foreign_holders
from tt_device_mcp.health import ALL_ONES
from tt_device_mcp.health.monitors import heartbeat, pci
from tt_device_mcp.health.recovery import base

OK = 0x1234
FAILED_31 = "Error: POST_RESET failed for device /dev/tenstorrent/31.\n"


class _World:
    """The sampler's deps as the cut sees them, recording every call in order."""

    def __init__(self, monkeypatch, *, readers=(4242,), complete=True, detach_ok=True, isolate=True, beats=None):
        self.order: list = []
        self.events: list = []
        self.read_only: list = []
        self.beats = beats if beats is not None else {}
        self.isolated: set = set()
        self.readers = list(readers)
        self.complete = complete
        self.detach_ok = detach_ok
        deps = srv.sampler._deps
        monkeypatch.setattr(deps, "read_inflight_heartbeats", self.read)
        monkeypatch.setattr(deps, "find_inflight_readers", lambda: (list(self.readers), self.complete))
        monkeypatch.setattr(deps, "isolate_chip", self.detach if isolate else None)
        monkeypatch.setattr(deps, "isolated_chips", lambda: self.isolated)
        monkeypatch.setattr(deps, "set_device_pollers", self.pollers)
        monkeypatch.setattr(deps, "owe_device_pollers", lambda why: self.order.append(("owe", why)))
        monkeypatch.setattr(deps, "cut_inflight_readers", self.kill)
        monkeypatch.setattr(deps, "health_event", lambda kind, **f: self.events.append((kind, f)))

    def read(self, only):
        self.read_only.append(set(only))
        self.order.append(("read", sorted(only, key=int)))
        return {i: v for i, v in self.beats.items() if i in only}

    def detach(self, idx):
        self.order.append(("detach", idx))
        return self.detach_ok if not callable(self.detach_ok) else self.detach_ok(idx)

    async def pollers(self, on, log=None):
        self.order.append(("pollers", on))

    async def kill(self, reason):
        self.order.append(("kill", reason))
        return []

    def kinds(self):
        return [k for k, _ in self.events]

    def steps(self):
        return [o[0] for o in self.order]


async def _cut(chips):
    return await srv.sampler.cut_post_reset_failed(chips)


# --- the cut itself --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_post_reset_chip_reading_all_ones_is_detached_before_its_readers_are_killed(monkeypatch):
    w = _World(monkeypatch, beats={"31": ALL_ONES})
    assert await _cut(["31"]) is True
    assert w.steps() == ["read", "detach", "pollers", "owe", "kill"]
    assert w.order[1] == ("detach", "31") and w.order[2] == ("pollers", False)
    assert w.isolated == {"31"}, "a removed chip is isolated, so the closing rescan re-adds it if it answers"
    assert ("reset_post_reset_failed", {"chips": ["31"], "off_bus": ["31"], "beats": {"31": ALL_ONES}}) in w.events


@pytest.mark.asyncio
async def test_a_failed_chip_that_answers_is_not_cut(monkeypatch):
    w = _World(monkeypatch, beats={"31": OK})
    assert await _cut(["31"]) is True
    assert w.steps() == ["read"], "the failed POST_RESET alone is not proof the chip is off the bus"


@pytest.mark.asyncio
async def test_an_unreadable_failed_chip_is_not_cut(monkeypatch):
    """A read that fails (ENODATA, a gone node) omits the chip: it is not all-ones, so nothing is cut."""
    w = _World(monkeypatch, beats={})
    assert await _cut(["31"]) is True
    assert w.steps() == ["read"]


@pytest.mark.asyncio
async def test_only_the_failed_chips_are_read(monkeypatch):
    w = _World(monkeypatch, beats={"30": ALL_ONES, "31": ALL_ONES, "5": OK})
    await _cut(["31", "30"])
    assert w.read_only == [{"30", "31"}], "one read of each failed chip and no other"
    assert [o[1] for o in w.order if o[0] == "detach"] == ["30", "31"]


@pytest.mark.asyncio
async def test_a_chip_that_cannot_be_detached_keeps_every_reader_alive(monkeypatch):
    w = _World(monkeypatch, detach_ok=False, beats={"31": ALL_ONES})
    assert await _cut(["31"]) is True
    assert "kill" not in w.steps() and "pollers" not in w.steps()
    kind, f = w.events[-1]
    assert kind == "reset_inflight_cut_refused" and f["host_at_risk"] is True and f["remove_blocked"] is False


@pytest.mark.asyncio
async def test_one_failed_detach_of_two_kills_no_reader(monkeypatch):
    w = _World(monkeypatch, detach_ok=lambda idx: idx == "30", beats={"30": ALL_ONES, "31": ALL_ONES})
    await _cut(["30", "31"])
    assert "kill" not in w.steps()
    assert w.events[-1][1]["chips"] == ["31"] and w.events[-1][1]["detached"] == ["30"]


@pytest.mark.asyncio
async def test_with_no_detach_wired_nothing_is_killed(monkeypatch):
    w = _World(monkeypatch, isolate=False, beats={"31": ALL_ONES})
    await _cut(["31"])
    assert "kill" not in w.steps() and w.kinds()[-1] == "reset_inflight_cut_refused"


@pytest.mark.asyncio
async def test_no_reader_means_no_detach_and_no_kill(monkeypatch):
    w = _World(monkeypatch, readers=(), beats={"31": ALL_ONES})
    await _cut(["31"])
    assert w.steps() == ["read"] and w.kinds()[-1] == "reset_inflight_readers_cut"


@pytest.mark.asyncio
async def test_an_incomplete_holder_scan_counts_as_a_reader(monkeypatch):
    w = _World(monkeypatch, readers=(), complete=False, beats={"31": ALL_ONES})
    await _cut(["31"])
    assert w.steps() == ["read", "detach", "pollers", "owe", "kill"]


@pytest.mark.asyncio
async def test_a_remove_that_blocks_refuses_the_cut_in_time(monkeypatch):
    """A PCI remove stuck in the kernel: the cut gives up at its bound, kills nobody, says the thread may
    still be blocked, and tells the reset to skip its rescan (it would queue behind the remove)."""
    release = threading.Event()
    w = _World(monkeypatch, detach_ok=lambda idx: release.wait(10) and True, beats={"31": ALL_ONES})
    monkeypatch.setattr(telemetry, "INFLIGHT_DETACH_TIMEOUT_SEC", 0.2)
    t0 = time.monotonic()
    try:
        assert await _cut(["31"]) is False
        assert time.monotonic() - t0 < 2
    finally:
        release.set()  # free the worker thread
    assert "kill" not in w.steps() and "pollers" not in w.steps()
    kind, f = w.events[-1]
    assert kind == "reset_inflight_cut_refused" and f["remove_blocked"] is True and f["host_at_risk"] is True


@pytest.mark.asyncio
async def test_a_kill_that_overruns_its_bound_is_journaled(monkeypatch):
    w = _World(monkeypatch, beats={"31": ALL_ONES})
    monkeypatch.setattr(telemetry, "INFLIGHT_KILL_TIMEOUT_SEC", 0.05)

    async def slow_kill(reason):
        await asyncio.sleep(5)

    monkeypatch.setattr(srv.sampler._deps, "cut_inflight_readers", slow_kill)
    assert await _cut(["31"]) is True
    assert w.kinds()[-1] == "reset_inflight_cut_timeout"


# --- the trigger: tt-smi's POST_RESET failure line ------------------------------------------------


def test_the_watch_records_post_reset_failures_split_across_reads(monkeypatch):
    mech = srv.recovery_mechanism
    monkeypatch.setattr(mech, "_isolated_chips", lambda: set())
    monkeypatch.setattr(mech, "post_reset_failed", set())
    seen = []
    stream, _ = mech._driver_wait_rescan_hook(["tt-smi", "-glx_reset"], seen.append, lambda m: None)
    stream("Issuing USER_RESET on 32 devices before IPMI reset...\n" + SETTLE + "\n")
    assert mech.post_reset_failed == set(), "the re-power and the driver wait are no trigger"
    stream("Error: POST_RESET failed for device /dev/tenstorrent/3")
    stream("1.\nError: POST_RESET failed for device /dev/tenstorrent/7.\n")
    assert mech.post_reset_failed == {"31", "7"}
    assert seen[0].startswith("Issuing"), "the caller's own output callback still gets every chunk"


def test_a_reset_that_is_not_a_mesh_reset_records_nothing(monkeypatch):
    mech = srv.recovery_mechanism
    cb = object()
    stream, state = mech._driver_wait_rescan_hook(["tt-smi", "-r", "0"], cb, lambda m: None)
    assert stream is cb and state == {}


@pytest.mark.asyncio
async def test_the_mechanism_is_wired_to_the_samplers_cut():
    assert srv.recovery_mechanism.post_reset_cut == srv.sampler.cut_post_reset_failed


# --- the real reset path --------------------------------------------------------------------------


def _glx(hw, lines, rc, during=None):
    """A fake `tt-smi -glx_reset`: USER_RESET, the settle, the driver wait, then ``lines``; ``during``
    runs inside the driver wait."""

    async def run_scoped(argv, log, owner="[broker]health-gate", on_output=None):
        emit = on_output or (lambda s: None)
        hw["in_reset"] = True
        try:
            hw["resets"].append(list(argv))
            emit("Issuing USER_RESET on 32 devices before IPMI reset...\n")
            hw["repowered"] = True
            emit(SETTLE + "\n")
            if during is not None:
                during()
            for line in lines:
                emit(line)
            return rc, "done"
        finally:
            hw["in_reset"] = False

    return run_scoped


def _reset_world(monkeypatch, hw, lines, rc, during=None, cut=None):
    _real_reset_path(monkeypatch, hw, [0])
    mech = srv.recovery_mechanism
    monkeypatch.setattr(mech, "run_scoped", _glx(hw, lines, rc, during))
    monkeypatch.setattr(mech, "scope_active", lambda: False)
    monkeypatch.setattr(srv, "isolated_chips", set())
    calls: list = []

    async def record(chips):
        calls.append(("cut", list(chips), list(hw["rescans"])))
        return True

    monkeypatch.setattr(mech, "post_reset_cut", cut or record)
    logs: list = []
    return mech, calls, logs


@pytest.mark.asyncio
async def test_enodata_in_the_driver_wait_reads_as_absent_and_cuts_nothing(monkeypatch, tmp_path):
    """The driver wait, as tt-kmd really answers it: every heartbeat read fails with ENODATA
    (needs_hw_init). The read omits the chip, never reports all-ones, and with no POST_RESET failure
    line the reset cuts nothing; its journal says so."""
    for i in ("30", "31"):
        (tmp_path / f"tenstorrent!{i}").mkdir()
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", tmp_path)

    def enodata(node):
        raise OSError(errno.ENODATA, "No data available")

    monkeypatch.setattr(heartbeat, "_read_beat", enodata)
    hw = _hw()
    hw["off"] = set()
    reads: list = []
    mech, calls, logs = _reset_world(
        monkeypatch,
        hw,
        [f'Waiting for all {EXPECTED} chips to show up in "/dev/tenstorrent", found {EXPECTED - 1}\n'],
        1,
        during=lambda: reads.append(heartbeat.read_heartbeats(only=frozenset({"30", "31"}))),
    )
    rc, _ = await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], logs.append)
    assert rc == 1
    assert reads == [{}], "ENODATA omits the chip: it never reads as all-ones in the driver wait"
    assert calls == [], "no POST_RESET failure line: no cut"
    assert any("no POST_RESET failure" in m for m in logs)


@pytest.mark.asyncio
async def test_a_post_reset_failure_is_cut_after_tt_smi_exits_and_before_the_closing_rescan(monkeypatch):
    hw = _hw()
    mech, calls, logs = _reset_world(
        monkeypatch, hw, ["Issuing POST_RESET on 32 devices after IPMI reset...\n", FAILED_31], 1
    )
    rc, _ = await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], logs.append)
    assert rc == 1
    assert calls == [("cut", ["31"], [])], "cut once tt-smi exited, before any rescan"
    assert hw["rescans"] == ["after"]
    assert any("POST_RESET failed on chip(s) ['31']" in m for m in logs)


@pytest.mark.asyncio
async def test_a_chip_the_cut_removes_is_readded_by_the_closing_rescan_when_it_answers(monkeypatch):
    """No chip isolated at the reset's start; the cut removes 31 (POST_RESET failed, read all-ones); the
    closing rescan runs and 31, answering again, leaves the isolated set."""
    hw = _hw()
    w = _World(monkeypatch, beats={"31": ALL_ONES})
    monkeypatch.setattr(srv.sampler._deps, "isolated_chips", lambda: srv.isolated_chips)
    mech, _, logs = _reset_world(monkeypatch, hw, [FAILED_31], 1, cut=srv.sampler.cut_post_reset_failed)
    rc, _ = await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], logs.append)
    assert rc == 1
    assert w.steps() == ["read", "detach", "pollers", "owe", "kill"]
    assert hw["rescans"] == ["after"]
    assert srv.isolated_chips == set(), "the re-powered chip answered the rescan: no longer isolated"


@pytest.mark.asyncio
async def test_a_blocked_remove_skips_the_closing_rescan(monkeypatch):
    hw = _hw()

    async def blocked(chips):
        return False

    mech, _, logs = _reset_world(monkeypatch, hw, [FAILED_31], 1, cut=blocked)
    await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], logs.append)
    assert hw["rescans"] == []
    assert any("rescan after the reset skipped" in m for m in logs)


@pytest.mark.asyncio
async def test_a_closing_rescan_that_blocks_is_bounded(monkeypatch):
    hw = _hw()
    hw["off"] = set()
    mech, _, logs = _reset_world(monkeypatch, hw, [], 0)
    monkeypatch.setattr(base, "PCI_SYSFS_WRITE_TIMEOUT_SEC", 0.05)
    never = asyncio.Event()

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__self__", None) == base.Path("/sys/bus/pci/rescan"):
            await never.wait()
        return fn(*a, **k)

    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    rc, _ = await asyncio.wait_for(mech.reset_with_quiesce(["tt-smi", "-glx_reset"], logs.append), timeout=5)
    assert rc == 0
    assert any("PCI rescan after the reset did not complete" in m for m in logs)


@pytest.mark.parametrize("rc", [1, 0])
@pytest.mark.asyncio
async def test_a_cancel_during_the_cut_still_settles_the_pollers(monkeypatch, rc):
    """The cut runs after tt-smi exits and can take about 45 s. A cancel that lands in it must still
    reach the pollers' restore step: they stay stopped (the cut's chips are undecided) and are owed to
    the next close to HEALTHY, never left stopped and owed to nobody."""
    hw = _hw()
    started = asyncio.Event()
    never = asyncio.Event()

    async def stuck_cut(chips):
        started.set()
        await never.wait()
        return True

    mech, _, logs = _reset_world(monkeypatch, hw, [FAILED_31], rc, cut=stuck_cut)
    order: list = []

    async def pollers(active, log):
        order.append(("pollers", active))
        return ["tt-telemetry"]

    monkeypatch.setattr(mech, "_set_device_pollers", pollers)
    monkeypatch.setattr(mech, "owe_device_pollers", lambda after: order.append(("owe", after)))
    task = asyncio.ensure_future(mech.reset_with_quiesce(["tt-smi", "-glx_reset"], logs.append))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == [("pollers", False), ("owe", f"reset rc={rc}, cut cancelled")]
    assert hw["rescans"] == [], "no closing rescan after a cancelled cut"
    assert mech.reset_in_flight is False


@pytest.mark.asyncio
async def test_post_reset_failures_are_cleared_at_each_reset_start(monkeypatch):
    hw = _hw()
    hw["off"] = set()
    mech, calls, logs = _reset_world(monkeypatch, hw, [], 0)
    mech.post_reset_failed = {"5"}  # left by an earlier, abandoned reset
    await mech.reset_with_quiesce(["tt-smi", "-glx_reset"], logs.append)
    assert calls == []


# --- who the cut may signal ----------------------------------------------------------------------


def test_the_cut_never_selects_the_broker_the_reset_or_their_children(monkeypatch):
    me = os.getpid()
    internal, in_reset, child, foreign = 101, 102, 103, 104
    asked: list = []

    def scope(pid, prefixes=()):
        asked.append(tuple(prefixes))
        return pid == in_reset

    monkeypatch.setattr(srv, "is_broker_internal", lambda pid: pid == internal)
    monkeypatch.setattr(srv, "in_broker_scope", scope)
    monkeypatch.setattr(srv, "descends_from", lambda pid, anc: pid == child and anc == me)
    picked = [p for p in (me, internal, in_reset, child, foreign) if srv._inflight_reader(DeviceHolder(pid=p, uid=0))]
    assert picked == [foreign]
    assert asked and all(a == (base.RESET_SCOPE_PREFIX,) for a in asked), "the reset's scope, by its prefix"


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
