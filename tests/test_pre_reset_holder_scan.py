# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I92: before every reset, the device is cleared of readers.

A reader that is not a tenant gets SIGTERM, ``PRE_RESET_TERM_GRACE_SEC`` (10 s) to let go, then
SIGKILL and the I67 release wait. A tenant refuses an automatic reset and is named. A reader stuck in
the kernel is fenced as a leftover so the I70 clock takes it to the host rung. A chip already off the
bus is detached before any of its readers is killed; if it cannot be, nothing is signalled and the
reset is refused. No signal here reaches a real process: every kill is a recorder."""

from __future__ import annotations

import asyncio
import functools
import signal
import time

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import device_holders
from tt_device_mcp import pre_reset_scan as prs
from tt_device_mcp import server as srv
from tt_device_mcp.constants import HOLDER_RELEASE_WAIT_SEC, PRE_RESET_TERM_GRACE_SEC
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.post_reset import FABRIC_DEBT

READER = DeviceHolder(pid=4101, uid=1000)
TENANT = DeviceHolder(pid=4202, uid=1001)


class _Box:
    """The holder scan's seams: who holds, who is a tenant, who lets go at which wait."""

    def __init__(self, monkeypatch, holders, *, tenants=(), release_after=("TERM",), stuck=(), order=None):
        self.holders = list(holders)
        self.tenants = {h.pid for h in tenants}
        self.release_after = release_after  # the signals after which a reader lets go
        self.stuck = set(stuck)
        self.order = order if order is not None else []
        self.waits: list = []
        self.sent: dict = {}
        monkeypatch.setattr(device_holders, "enumerate_device_holders", lambda: HolderScan(holders=list(self.holders)))
        monkeypatch.setattr(device_holders, "wait_device_released", self._wait)
        monkeypatch.setattr(device_holders, "holder_state", lambda pid: "D" if pid in self.stuck else "S")
        monkeypatch.setattr(device_holders, "holder_comm", lambda pid: f"proc{pid}")
        monkeypatch.setattr(device_holders, "descends_from", lambda pid, anc: pid == anc)
        monkeypatch.setattr(device_holders, "is_broker_internal", lambda pid, **k: False)
        monkeypatch.setattr(device_holders, "note_broker_internal", lambda pid, **k: None)
        monkeypatch.setattr(prs, "is_tenant", lambda h: h.pid in self.tenants)

    def kill(self, pid, sig):
        name = signal.Signals(sig).name[3:]
        self.order.append(f"kill:{pid}:{name}")
        self.sent.setdefault(pid, []).append(name)

    async def _wait(self, pids, timeout):
        self.waits.append(timeout)
        return [p for p in pids if p in self.stuck or not any(s in self.release_after for s in self.sent.get(p, []))]

    def run(self, **kw):
        kw.setdefault("kill", self.kill)
        kw.setdefault("read_starttime", lambda pid: "100")
        kw.setdefault("getpgid", lambda pid: 1 if pid == 1 else 2)
        kw.setdefault("getpid", lambda: 1)
        kw.setdefault("unkillable", lambda pid: pid in self.stuck)
        kw.setdefault("offbus_chips", lambda: [])
        kw.setdefault("holder_in_reset", lambda pid: False)
        return prs.clear_device_readers(**kw)


# --- the scan itself ---


@pytest.mark.asyncio
async def test_nothing_holding_costs_one_scan_and_reads_no_chip(monkeypatch):
    box = _Box(monkeypatch, [])
    read = []
    result = await box.run(tenants_block=True, offbus_chips=lambda: read.append(1) or [])
    assert result.outcome == "clear" and result.refusal() == ""
    assert read == [] and box.waits == [] and box.sent == {}


@pytest.mark.asyncio
async def test_a_reader_gets_sigterm_then_sigkill_after_the_10s_grace(monkeypatch):
    box = _Box(monkeypatch, [READER], release_after=("KILL",))
    result = await box.run(tenants_block=True)
    assert box.sent == {READER.pid: ["TERM", "KILL"]}
    assert PRE_RESET_TERM_GRACE_SEC == 10
    assert box.waits == [PRE_RESET_TERM_GRACE_SEC, HOLDER_RELEASE_WAIT_SEC]
    assert result.outcome == "killed" and result.refusal() == ""
    assert result.killed[0]["signal"] == "SIGKILL" and result.killed[0]["released"] is True


@pytest.mark.asyncio
async def test_a_reader_that_lets_go_on_sigterm_gets_no_sigkill(monkeypatch):
    box = _Box(monkeypatch, [READER])
    result = await box.run(tenants_block=True)
    assert box.sent == {READER.pid: ["TERM"]}
    assert result.outcome == "killed" and result.refusal() == ""


@pytest.mark.asyncio
async def test_a_tenant_blocks_an_automatic_reset_and_nothing_is_signalled(monkeypatch):
    box = _Box(monkeypatch, [TENANT, READER], tenants=[TENANT])
    result = await box.run(tenants_block=True)
    assert box.sent == {}
    assert result.outcome == "tenant_blocked"
    assert f"pid {TENANT.pid}" in result.refusal()


@pytest.mark.asyncio
async def test_an_operator_reset_records_the_tenant_but_still_clears_the_readers(monkeypatch):
    box = _Box(monkeypatch, [TENANT, READER], tenants=[TENANT])
    result = await box.run(tenants_block=False)
    assert box.sent == {READER.pid: ["TERM"]}
    assert result.refusal() == "" and [t["pid"] for t in result.tenants] == [TENANT.pid]


@pytest.mark.asyncio
async def test_the_running_job_is_a_tenant_never_a_reader(monkeypatch):
    box = _Box(monkeypatch, [READER])
    result = await box.run(tenants_block=False, protected=[READER.pid])
    assert box.sent == {} and result.outcome == "clear"


@pytest.mark.asyncio
async def test_a_d_state_reader_is_unkillable_and_refuses_the_reset(monkeypatch):
    box = _Box(monkeypatch, [READER], stuck=[READER.pid])
    result = await box.run(tenants_block=True)
    assert box.sent == {READER.pid: ["TERM", "KILL"]}
    assert result.outcome == "unkillable"
    assert result.unkillable[0]["pid"] == READER.pid and "host reboot" in result.refusal()


@pytest.mark.asyncio
async def test_a_reused_pid_is_never_signalled(monkeypatch):
    box = _Box(monkeypatch, [READER], release_after=())
    starts = iter(["100", "999"])  # the scan's starttime, then another process under the same pid
    await box.run(tenants_block=True, read_starttime=lambda pid: next(starts, "999"))
    assert box.sent == {}


@pytest.mark.asyncio
async def test_on_bus_chips_readers_are_killed_with_no_detach(monkeypatch):
    box = _Box(monkeypatch, [READER])
    detached = []
    result = await box.run(tenants_block=True, offbus_chips=lambda: [], detach_chip=detached.append)
    assert detached == [] and box.sent == {READER.pid: ["TERM"]} and result.refusal() == ""


@pytest.mark.asyncio
async def test_an_off_bus_chip_is_detached_before_any_reader_is_killed(monkeypatch):
    order = []
    box = _Box(monkeypatch, [READER], order=order)
    result = await box.run(
        tenants_block=True, offbus_chips=lambda: ["7"], detach_chip=lambda idx: order.append(f"detach:{idx}") or True
    )
    assert order == ["detach:7", f"kill:{READER.pid}:TERM"]
    assert result.detached == ["7"] and result.refusal() == ""


@pytest.mark.asyncio
async def test_an_off_bus_chip_that_cannot_be_detached_keeps_its_readers_alive(monkeypatch):
    box = _Box(monkeypatch, [READER])
    result = await box.run(tenants_block=True, offbus_chips=lambda: ["7"], detach_chip=lambda idx: False)
    assert box.sent == {}
    assert result.outcome == "offbus_undetached" and result.undetached == ["7"]
    assert "7" in result.refusal() and f"pid {READER.pid}" in result.refusal()


@pytest.mark.asyncio
async def test_a_detach_that_raises_counts_as_not_detached(monkeypatch):
    box = _Box(monkeypatch, [READER])

    def boom(idx):
        raise OSError("sysfs gone")

    result = await box.run(tenants_block=True, offbus_chips=lambda: ["7"], detach_chip=boom)
    assert box.sent == {} and result.outcome == "offbus_undetached"


@pytest.mark.asyncio
async def test_a_scan_that_fails_reports_it_and_signals_nothing(monkeypatch):
    box = _Box(monkeypatch, [READER])

    def broken():
        raise RuntimeError("proc walk failed")

    monkeypatch.setattr(device_holders, "enumerate_device_holders", broken)
    result = await box.run(tenants_block=True)
    assert result.outcome == "scan_error" and box.sent == {}


# --- the broker's wiring: every reset runs it first ---


def _broker_seams(monkeypatch, box, order, events):
    """The reset seams of reset_with_quiesce plus the broker's scan with a recording kill."""

    async def pollers(active, log):
        order.append(f"pollers:{active}")
        return ["telem"] if not active else []

    async def run(argv, log, owner="[broker]health-gate"):
        order.append("reset")
        return 0, "out"

    async def to_thread(fn, *a, **k):
        order.append("rescan")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        srv,
        "clear_device_readers",
        functools.partial(
            prs.clear_device_readers,
            kill=box.kill,
            read_starttime=lambda pid: "100",
            getpid=lambda: 1,
            getpgid=lambda pid: 1 if pid == 1 else 2,
            unkillable=lambda pid: pid in box.stuck,
            holder_in_reset=lambda pid: False,
            offbus_chips=lambda: [],
        ),
    )
    monkeypatch.setattr(srv.recovery_mechanism, "pre_reset_scan", srv._pre_reset_holder_scan)
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    srv._pre_reset_blocker = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv,owner",
    [
        (["tt-smi", "-r"], "[broker]health-gate"),  # the mesh reset rung
        (["tt-smi", "-r", "--sweep"], "[broker]sweep"),  # the last-chance sweep
        (["tt-smi", "-r"], "operator:alice"),  # the operator reset tool
    ],
)
async def test_every_reset_kills_the_reader_after_the_pollers_stop_and_before_the_reset(monkeypatch, argv, owner):
    order, events = [], []
    box = _Box(monkeypatch, [READER], order=order)
    _broker_seams(monkeypatch, box, order, events)
    await srv.recovery_mechanism.reset_with_quiesce(argv, lambda m: None, owner=owner)
    assert order[:3] == ["pollers:False", f"kill:{READER.pid}:TERM", "reset"]
    scans = [f for k, f in events if k == "pre_reset_holder_scan"]
    assert len(scans) == 1 and scans[0]["outcome"] == "killed" and scans[0]["owner"] == owner


@pytest.mark.asyncio
async def test_a_clean_reset_still_emits_one_scan_event(monkeypatch):
    order, events = [], []
    box = _Box(monkeypatch, [], order=order)
    _broker_seams(monkeypatch, box, order, events)
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert "reset" in order
    assert [f["outcome"] for k, f in events if k == "pre_reset_holder_scan"] == ["clear"]
    assert srv._health_payload()["pre_reset_blocked_by"] is None


@pytest.mark.asyncio
async def test_a_tenant_refuses_an_automatic_reset_named_in_health_and_the_fabric_stays_owed(monkeypatch):
    order, events = [], []
    box = _Box(monkeypatch, [TENANT], tenants=[TENANT], order=order)
    _broker_seams(monkeypatch, box, order, events)
    FABRIC_DEBT.owed = ""
    rc, out = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert rc is None and "reset refused" in out and "reset" not in order
    assert box.sent == {}
    # Pollers stay stopped (owed to the next close to HEALTHY), and the fabric pass the reset owes
    # still gates any lift (goals: full check, fabric included, before a hold lifts).
    assert order == ["pollers:False"]
    assert FABRIC_DEBT.owed
    blocked = srv._health_payload()["pre_reset_blocked_by"]
    assert blocked["blocker"] == "tenant" and blocked["holders"][0]["pid"] == TENANT.pid
    assert [f["outcome"] for k, f in events if k == "pre_reset_holder_scan"] == ["tenant_blocked"]
    FABRIC_DEBT.owed = ""


@pytest.mark.asyncio
async def test_an_operator_reset_is_not_refused_by_a_tenant(monkeypatch):
    order, events = [], []
    box = _Box(monkeypatch, [TENANT], tenants=[TENANT], order=order)
    _broker_seams(monkeypatch, box, order, events)
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None, owner="operator:alice")
    assert "reset" in order and box.sent == {}


@pytest.mark.asyncio
async def test_an_unkillable_reader_is_fenced_for_the_host_rung(monkeypatch):
    order, events = [], []
    box = _Box(monkeypatch, [READER], stuck=[READER.pid], order=order)
    _broker_seams(monkeypatch, box, order, events)
    monkeypatch.setattr(srv, "_reaped_leftovers", lambda *a, **k: [])
    monkeypatch.setattr(srv, "_leftover_state", lambda pid: ("D", "100"))
    srv.reaped_survivors.pop(READER.pid, None)
    rc, out = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    try:
        assert rc is None and "reset" not in order
        assert srv.reaped_survivors[READER.pid] == (srv.PRE_RESET_READER_JOB, "100")
        assert srv._health_payload()["pre_reset_blocked_by"]["blocker"] == "unkillable"
    finally:
        srv.reaped_survivors.pop(READER.pid, None)
        FABRIC_DEBT.owed = ""


@pytest.mark.asyncio
async def test_a_scan_bug_does_not_stop_the_reset(monkeypatch):
    order, events = [], []
    box = _Box(monkeypatch, [], order=order)
    _broker_seams(monkeypatch, box, order, events)

    async def broken(argv, owner, log):
        raise RuntimeError("bug")

    monkeypatch.setattr(srv.recovery_mechanism, "pre_reset_scan", broken)
    await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert "reset" in order
    assert [f["outcome"] for k, f in events if k == "pre_reset_holder_scan"] == ["scan_error"]


# --- the bridge rung's secondary bus resets ---


@pytest.mark.asyncio
async def test_the_bridge_rung_journals_its_detached_targets_and_kills_nothing(monkeypatch):
    """Its targets are cut out of the kernel: nobody can map them, closing a holder sends them
    nothing, so the rung stops no poller and signals no process, but still journals the scan."""
    events, sbrs = [], []
    srv.isolated_chips = {"13"}
    srv.device_pci_map = {"13": "0000:46:00.0"}
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: sbrs.append(idx) or True)

    async def no_scan(argv, owner, log):
        raise AssertionError("the bridge rung must not run the holder scan")

    monkeypatch.setattr(srv.recovery_mechanism, "pre_reset_scan", no_scan)
    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)
    assert ok is True and sbrs == ["13"]
    scans = [f for k, f in events if k == "pre_reset_holder_scan"]
    assert len(scans) == 1 and scans[0]["outcome"] == "targets_detached" and scans[0]["chips"] == ["13"]


# --- review fixes: a bounded detach, a reset in flight, an incomplete scan ---


def test_a_detach_that_blocks_in_the_kernel_is_bounded_and_refuses_the_reset(monkeypatch):
    import threading

    box = _Box(monkeypatch, holders=[READER])
    never = threading.Event()

    def blocked_detach(idx):
        never.wait(5)  # stands in for a sysfs remove stuck in the kernel; released at the end
        return True

    async def scenario():
        ticks = []

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        result = await box.run(
            tenants_block=True, offbus_chips=lambda: ["3"], detach_chip=blocked_detach, detach_timeout_sec=0.2
        )
        took = time.monotonic() - t0
        t.cancel()
        return result, took, len(ticks)

    try:
        result, took, ticks = asyncio.run(scenario())
    finally:
        never.set()
    assert result.outcome == "offbus_undetached" and result.detach_timed_out
    assert result.undetached == ["3"] and result.refusal()
    assert took < 2 and ticks >= 5  # the loop kept running while the detach was stuck
    assert box.sent == {}  # nothing signalled


def test_a_holder_inside_a_reset_is_never_signalled_and_the_reset_is_refused(monkeypatch):
    box = _Box(monkeypatch, holders=[READER])
    result = asyncio.run(box.run(tenants_block=False, holder_in_reset=lambda pid: pid == READER.pid))
    assert result.outcome == "reset_in_flight" and result.refusal()
    assert box.sent == {}


def test_a_reset_already_running_refuses_before_the_scan(monkeypatch):
    box = _Box(monkeypatch, holders=[READER])
    result = asyncio.run(box.run(tenants_block=False, reset_active=lambda: "ttdev-reset-9-1"))
    assert result.outcome == "reset_in_flight" and "ttdev-reset-9-1" in result.refusal()
    assert box.sent == {} and result.found == []


def test_a_registered_local_reset_and_its_child_read_as_in_reset(monkeypatch):
    monkeypatch.setattr(prs.device_holders, "descends_from", lambda pid, anc: pid == anc or (pid, anc) == (52, 51))
    prs.note_reset_pid(51)
    try:
        assert prs.in_reset(51) and prs.in_reset(52)
    finally:
        prs.forget_reset_pid(51)
    assert not prs._reset_pids
