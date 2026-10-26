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


# --- a refused reset is not a reset: nothing it would settle is settled ---


@pytest.fixture
def refused_mesh(monkeypatch):
    """A Galaxy gate pass whose mesh reset the pre-reset scan refuses, with one chip off the bus.
    Records the journal, the AER mask restores and the off-bus intent's begin and end."""
    out = {"events": [], "restores": [], "intent": [], "logs": []}
    g = srv.galaxy_recovery

    async def no_scope(log):
        return False

    async def refused(argv, log, *a, **k):
        return None, "reset refused: tenant pid 4202 holds /dev/tenstorrent"

    class _Mask:
        def restore(self):
            out["restores"].append(1)

    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    patch_health_event(monkeypatch, lambda name, **k: out["events"].append(name))
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", no_scope)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", refused)
    monkeypatch.setattr(g.mechanism, "last_reset_monotonic", 123.0)
    monkeypatch.setattr(g.mechanism, "last_reset_failed", False)
    monkeypatch.setattr(g.monitor, "expected", lambda n: 4)
    pg = recovery_pkg.pcie_guard
    monkeypatch.setattr(pg, "chips_off_bus", lambda expected: 1)
    monkeypatch.setattr(pg, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pg, "mask_for_mesh_reset", lambda log: _Mask())
    monkeypatch.setattr(pg, "begin_offbus_reset", lambda kind, n: out["intent"].append(("begin", kind)))
    monkeypatch.setattr(pg, "end_offbus_reset", lambda: out["intent"].append(("end",)))
    return out


@pytest.mark.asyncio
async def test_a_refused_mesh_reset_is_journalled_as_refused_and_settles_no_debt(monkeypatch, refused_mesh):
    """A refusal is no reset: it is logged and journalled as itself (not 'timed out' / reset_failed),
    the cooldown clock goes back so a kill-reset debt taken before it stays owed, and the AER mask and
    the off-bus intent are both released. The hold stays for the 10-min ladder."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(srv, "_kill_reset_owed", "job 9 killed")
    monkeypatch.setattr(srv, "_kill_reset_owed_at", 200.0)
    healthy = await g._reset_and_verify_device(["0", "1", "2", "3"], refused_mesh["logs"].append)

    assert healthy is False
    assert "reset_refused" in refused_mesh["events"] and "reset_failed" not in refused_mesh["events"]
    assert any(m.startswith("reset refused:") for m in refused_mesh["logs"])
    assert not any("timed out" in m for m in refused_mesh["logs"])
    assert g.mechanism.last_reset_monotonic == 123.0 and g.mechanism.last_reset_failed is False
    assert srv._kill_reset_debt() == "job 9 killed", "a refused reset settled the kill-reset debt"
    assert refused_mesh["restores"] == [1], "the AER mask was not restored after a refusal"
    assert refused_mesh["intent"] == [("begin", "mesh reset"), ("end",)]


@pytest.mark.asyncio
async def test_a_refused_sweep_reset_is_journalled_and_releases_the_aer_mask(monkeypatch, refused_mesh):
    """The last-chance sweep's mesh reset refused: journalled as refused, the AER mask released, and
    the sweep still hands back to its caller's verify so a failed one climbs to the host rung."""
    g = srv.galaxy_recovery

    async def no_sbr(log):
        return None

    monkeypatch.setattr(g, "_recover_isolated_chips", no_sbr)
    assert await g._issue_all_resets_back_to_back([4], 4, lambda m: None) is True
    assert "reset_refused" in refused_mesh["events"]
    assert "last_chance_reset_sweep_error" not in refused_mesh["events"]
    assert refused_mesh["restores"] == [1]
    assert g.mechanism.reset_in_flight is False


# --- an incomplete scan (a holder the scan could not see) ---


def _scans(monkeypatch, *scans):
    """enumerate_device_holders answers ``scans`` in turn (the last one repeats); returns the call log."""
    calls = []

    def enumerate_holders():
        calls.append(1)
        return scans[min(len(calls), len(scans)) - 1]

    monkeypatch.setattr(device_holders, "enumerate_device_holders", enumerate_holders)
    return calls


def _gap(pid, *holders):
    scan = HolderScan(holders=list(holders), source="driver")
    scan.mark_incomplete(f"pid {pid} named by the driver, not attributable", pid=pid)
    return scan


@pytest.mark.asyncio
async def test_a_pid_gap_is_rescanned_once_then_refuses_an_automatic_reset(monkeypatch):
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303, READER))
    result = await box.run(tenants_block=True, rescan_settle_sec=0)
    assert len(calls) == 2, "a pid gap gets exactly one rescan"
    assert result.outcome == "scan_incomplete" and "4303" in result.refusal()
    assert box.sent == {}, "nothing is signalled past a holder the scan could not see"


@pytest.mark.asyncio
async def test_a_pid_gap_that_clears_on_the_rescan_lets_the_reset_proceed(monkeypatch):
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303, READER), HolderScan(holders=[READER]))
    result = await box.run(tenants_block=True, rescan_settle_sec=0)
    assert len(calls) == 2 and result.outcome == "killed" and result.refusal() == ""
    assert box.sent[READER.pid][0] == "TERM"


@pytest.fixture
def fresh_zombies(monkeypatch):
    monkeypatch.setattr(prs, "_zombies_waited", set())


def _zombie(verdict, pid=4303):
    """A zombie the driver names: ``exited`` (whole thread group gone), ``holds`` (a live thread shows a
    device fd) or ``unknown`` (live threads whose fds cannot be read)."""
    live = [] if verdict == "exited" else [pid + 1]
    return device_holders.ZombieInfo(
        pid=pid,
        state="Z",
        threads=1 + len(live),
        live_tids=live,
        ppid=4000,
        parent_comm="pytest",
        parent_state="S",
        holds=True if verdict == "holds" else None,
    )


def _zombies(**by_pid):
    """zombie_of answering from ``by_pid`` (pid -> verdict, or a callable of the scan count)."""
    return lambda pid: (lambda v: _zombie(v, pid) if v else None)(by_pid.get(f"p{pid}"))


def _clock_of(calls):
    """One second per scan: the bounded wait runs on the scan count, without sleeping."""
    return lambda: float(len(calls))


@pytest.mark.asyncio
async def test_a_zombie_whose_whole_thread_group_exited_is_no_holder_and_the_reset_runs_at_once(
    monkeypatch, fresh_zombies
):
    """Spec 04 I92: a killed job's leader that is a zombie with no thread left holds no fd and no mapping
    (do_exit releases both before EXIT_ZOMBIE): the owed reset runs with no rescan and no wait, and the
    reader beside it is cleared as before. The post-reset full check with fabric still decides the lift."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303, READER))
    walked = []
    result = await box.run(
        tenants_block=True,
        rescan_settle_sec=0,
        zombie_wait_sec=465,
        clock=_clock_of(calls),
        zombie_of=_zombies(p4303="exited"),
        users_outside=lambda named: walked.append(set(named)) or ([], True),
    )
    assert len(calls) == 1, "no rescan, no wait"
    assert result.gaps == [] and result.refusal() == "" and result.outcome == "killed"
    assert walked == [{4303, READER.pid}], "the file the zombie opened is looked for in every other process"
    assert [z["verdict"] for z in result.zombies] == ["exited"] and result.zombies[0]["waited_sec"] == 0.0
    assert box.sent[READER.pid][0] == "TERM" and 4303 not in box.sent, "nothing new is signalled"


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", [([4999], True), ([], False)], ids=["open_elsewhere", "walk_incomplete"])
async def test_an_exited_zombie_whose_file_may_be_open_elsewhere_still_refuses(monkeypatch, fresh_zombies, outside):
    """The driver names only the opener: a forked child can still hold its file. Found, or a walk that
    could not see every process: the gap stands, one rescan, refused; nothing is signalled."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303))
    result = await box.run(
        tenants_block=True,
        rescan_settle_sec=0,
        zombie_wait_sec=465,
        clock=_clock_of(calls),
        zombie_of=_zombies(p4303="exited"),
        users_outside=lambda named: outside,
    )
    assert len(calls) == 2 and result.outcome == "scan_incomplete" and "4303" in result.refusal()
    assert result.zombies[0]["verdict"] in ("open_elsewhere", "walk_incomplete") and box.sent == {}


@pytest.mark.asyncio
async def test_a_zombie_leader_with_a_live_thread_holding_a_device_fd_is_refused(monkeypatch, fresh_zombies):
    """A live thread shows a device fd: a real holder. One rescan, no zombie wait, refused; the hold then
    runs the full ladder at 10 min and the power cycle (I76)."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303))
    result = await box.run(
        tenants_block=True,
        rescan_settle_sec=0,
        zombie_wait_sec=465,
        clock=_clock_of(calls),
        zombie_of=_zombies(p4303="holds"),
        users_outside=lambda named: pytest.fail("no walk for a zombie that is not exited"),
    )
    assert len(calls) == 2 and result.outcome == "scan_incomplete" and result.refusal()
    assert result.zombies[0]["verdict"] == "holds" and box.sent == {}


@pytest.mark.asyncio
async def test_a_zombie_leader_whose_live_threads_cannot_be_read_is_rescanned_until_it_settles_45s_later(
    monkeypatch, fresh_zombies
):
    """Live threads whose fds cannot be read: the release may be running in one. Rescanned every second;
    the driver drops it 45 s on and the owed reset runs then, inside the bound."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, *([_gap(4303)] * 46), HolderScan())
    result = await box.run(
        tenants_block=True,
        rescan_settle_sec=0,
        zombie_wait_sec=465,
        clock=_clock_of(calls),
        zombie_of=_zombies(p4303="unknown"),
    )
    assert len(calls) == 47, "polled every second until the driver dropped it"
    assert result.outcome == "clear" and result.refusal() == "" and result.gaps == [] and box.sent == {}
    assert result.zombies[0]["verdict"] == "unknown" and result.zombies[0]["waited_sec"] == 45.0


@pytest.mark.asyncio
async def test_a_zombie_leader_that_settles_as_exited_lets_the_reset_run(monkeypatch, fresh_zombies):
    """Its last live thread exits mid-wait: the zombie is now exited, no holder, and the reset runs."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303))
    result = await box.run(
        tenants_block=True,
        rescan_settle_sec=0,
        zombie_wait_sec=465,
        clock=_clock_of(calls),
        zombie_of=lambda pid: _zombie("unknown" if len(calls) < 10 else "exited", pid),
        users_outside=lambda named: ([], True),
    )
    assert len(calls) == 10 and result.outcome == "clear" and result.gaps == []


@pytest.mark.asyncio
async def test_a_zombie_that_never_settles_hits_the_bound_refuses_and_is_waited_for_once(monkeypatch, fresh_zombies):
    """Never settled: the wait ends at its bound (an automatic reset's ends before the 10-min ladder
    mark), the refusal stands with nothing signalled, and the same (pid, start time) is not waited for
    again, so the ladder's own resets at 10 min are not delayed by it."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303))
    unknown = dict(tenants_block=True, rescan_settle_sec=0, zombie_wait_sec=465, zombie_of=_zombies(p4303="unknown"))
    result = await box.run(clock=_clock_of(calls), **unknown)
    assert 465 <= len(calls) <= 468, "bounded by zombie_wait_sec"
    assert result.outcome == "scan_incomplete" and "4303" in result.refusal() and box.sent == {}
    calls.clear()
    again = await box.run(clock=_clock_of(calls), **unknown)
    assert len(calls) == 2 and again.outcome == "scan_incomplete", "the same zombie gets the one rescan only"


@pytest.mark.asyncio
async def test_the_wait_is_once_per_pid_and_start_time_not_per_pid(monkeypatch, fresh_zombies):
    """A reused pid is another process: a new start time gets its own wait; the same one does not."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303))
    start = {"t": "100"}
    unknown = dict(tenants_block=True, rescan_settle_sec=0, zombie_wait_sec=5, zombie_of=_zombies(p4303="unknown"))
    await box.run(clock=_clock_of(calls), read_starttime=lambda pid: start["t"], **unknown)
    assert (4303, "100") in prs._zombies_waited
    for t, rescans in (("100", 2), ("200", 7), ("200", 2)):
        calls.clear()
        start["t"] = t
        await box.run(clock=_clock_of(calls), read_starttime=lambda pid: start["t"], **unknown)
        assert len(calls) == rescans, t


@pytest.mark.asyncio
async def test_a_live_pid_gap_gets_no_zombie_wait_and_still_refuses(monkeypatch, fresh_zombies):
    """A gap on a process that is not a zombie (or one already gone) keeps the single rescan."""
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303))
    result = await box.run(
        tenants_block=True, rescan_settle_sec=0, zombie_wait_sec=465, clock=_clock_of(calls), zombie_of=_zombies()
    )
    assert len(calls) == 2 and result.outcome == "scan_incomplete" and result.zombies == [] and box.sent == {}


@pytest.mark.asyncio
async def test_an_unknown_zombie_beside_a_live_gap_gets_no_wait(monkeypatch, fresh_zombies):
    box = _Box(monkeypatch, [])
    scan = _gap(4303)
    scan.mark_incomplete("pid 4304 named by the driver, not attributable", pid=4304)
    calls = _scans(monkeypatch, scan)
    result = await box.run(
        tenants_block=True,
        rescan_settle_sec=0,
        zombie_wait_sec=465,
        clock=_clock_of(calls),
        zombie_of=_zombies(p4303="unknown"),
    )
    assert len(calls) == 2 and result.outcome == "scan_incomplete"


@pytest.mark.asyncio
async def test_a_live_holder_still_blocks_beside_an_exited_zombie_and_the_job_is_never_signalled(
    monkeypatch, fresh_zombies
):
    """The zombie is no holder, but the running job and a tenant still hold: refused, nothing sent."""
    job = DeviceHolder(pid=4500, uid=1000)
    box = _Box(monkeypatch, [], tenants=[TENANT])
    _scans(monkeypatch, _gap(4303, job, TENANT))
    result = await box.run(
        tenants_block=True,
        protected=[job.pid],
        rescan_settle_sec=0,
        zombie_of=_zombies(p4303="exited"),
        users_outside=lambda named: ([], True),
    )
    assert result.outcome == "tenant_blocked" and result.refusal() and result.gaps == []
    assert {t["pid"] for t in result.tenants} == {job.pid, TENANT.pid}
    assert box.sent == {}, "neither the running job nor a tenant is signalled"


def test_an_automatic_resets_zombie_wait_ends_before_the_hold_ladder_mark(monkeypatch):
    """Spec 04 I76/I92: the wait ends the reserve (sized to the host's chips) before the hold's ladder
    mark, never past it; an operator reset or one with no hold clock waits the driver-release bound."""
    from datetime import datetime, timedelta

    from tt_device_mcp.constants import PRE_RESET_ZOMBIE_WAIT_SEC, pre_reset_zombie_ladder_reserve_sec

    now = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(srv, "_hold_bound_mark_sec", lambda: 600.0)
    monkeypatch.setattr(srv, "_zombie_reserve_chips", lambda: 32)
    reserve = pre_reset_zombie_ladder_reserve_sec(32)
    for age, want in ((0, 600 - reserve), (100, 500 - reserve), (500, 0.0), (900, 0.0)):
        since = (now - timedelta(seconds=age)).isoformat()
        monkeypatch.setattr(srv, "device_hold_episode_since", since)
        monkeypatch.setattr(srv, "hold_bound_deadline", lambda n=None, s=since: datetime.fromisoformat(s))
        assert srv._pre_reset_zombie_wait_sec(True, now) == want, age
        assert srv._pre_reset_zombie_wait_sec(False, now) == PRE_RESET_ZOMBIE_WAIT_SEC
    monkeypatch.setattr(srv, "hold_bound_deadline", lambda n=None: None)
    assert srv._pre_reset_zombie_wait_sec(True, now) == PRE_RESET_ZOMBIE_WAIT_SEC


def _hold_clock(monkeypatch, since, cooldown_until=None):
    """The real hold_bound_deadline on a hold that began at ``since``: a 600 s ladder mark, a 32-chip host,
    and a power-cycle cooldown running until ``cooldown_until`` (epoch), if given."""
    from types import SimpleNamespace

    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    monkeypatch.setattr(srv, "_hold_bound_mark_sec", lambda: 600.0)
    monkeypatch.setattr(srv, "_startup_verify_owns_hold", lambda *a, **k: False)
    if srv.health_monitor is None:
        monkeypatch.setattr(srv, "health_monitor", SimpleNamespace(expected=lambda default=0: 32))
    else:
        monkeypatch.setattr(srv.health_monitor, "expected", lambda default=0: 32)
    state = {"until": cooldown_until, "cause": "boot"} if cooldown_until else None
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", state)


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_age", [0, 60, 150])
async def test_a_zombie_cleared_on_the_last_poll_has_its_reset_and_full_check_done_before_the_ladder_mark(
    monkeypatch, fresh_zombies, reset_age
):
    """Spec 04 I76/I92: an automatic reset at hold age ``reset_age`` waits on a zombie that clears only on
    the wait's last poll. The mesh reset (~60 s on a Galaxy) and the full check with fabric after it (the
    fabric pass's 180 s bound + 3 s a chip, 32 chips) hold the device lock the 10-min ladder needs: on the
    simulated clock they still end before hold_bound_deadline, so the ladder is never late."""
    from datetime import datetime, timedelta

    from tt_device_mcp.constants import FABRIC_CHECK_TIMEOUT_SEC

    since = datetime(2026, 1, 1, 12, 0, 0)
    _hold_clock(monkeypatch, since)
    wait = srv._pre_reset_zombie_wait_sec(True, since + timedelta(seconds=reset_age))
    assert wait > 0, "a reset this early in the hold still waits for the zombie"
    box = _Box(monkeypatch, [])
    calls = _scans(monkeypatch, _gap(4303))
    clock = lambda: reset_age + float(len(calls))  # noqa: E731 - hold age: one second per scan
    # The first scan and the one rescan come before the wait; the zombie clears on its last poll.
    result = await box.run(
        tenants_block=True,
        rescan_settle_sec=0,
        zombie_wait_sec=wait,
        clock=clock,
        zombie_of=lambda pid: _zombie("exited" if len(calls) - 2 >= wait else "unknown", pid),
        users_outside=lambda named: ([], True),
    )
    assert result.outcome == "clear" and result.gaps == [] and result.zombies[0]["waited_sec"] >= wait
    reset_and_full_check = 60 + FABRIC_CHECK_TIMEOUT_SEC + 3 * 32
    done = since + timedelta(seconds=clock() + reset_and_full_check)
    assert done <= srv.hold_bound_deadline(done), "the reset and full check ran past the 10-min ladder mark"


@pytest.mark.asyncio
async def test_a_hold_begun_in_a_power_cycle_cooldown_ends_the_wait_one_reserve_before_the_ladder_mark(
    monkeypatch, clear_job_state
):
    """Spec 04 I76/I92: a hold that began in a boot or post-power-cycle cooldown running past its 10-min
    mark. hold_bound_deadline is the cooldown's end, but only the power cycle waits for it: the ladder
    still runs at the mark. So the zombie wait ends one reserve before the mark, not before the
    cooldown-extended deadline, and the ladder fires at the mark with the cooldown still running."""
    from datetime import datetime, timedelta

    from tests.test_device_safety import _arm_fabric_only_deadline_hold
    from tt_device_mcp.constants import pre_reset_zombie_ladder_reserve_sec
    from tt_device_mcp.health.recovery import OUTCOME_TERMINAL

    escalations, _alerts, _events = _arm_fabric_only_deadline_hold(
        monkeypatch, why="eth_frozen", detail="gate/post-job: eth/fabric fault on a present mesh", age_min=10.1
    )
    since = datetime.fromisoformat(srv.device_hold_episode_since)
    cooldown_end = since + timedelta(seconds=900)
    _hold_clock(monkeypatch, since, cooldown_until=cooldown_end.timestamp())
    reset_at = since + timedelta(seconds=60)
    assert srv.hold_bound_deadline(reset_at) == cooldown_end, "the cooldown extends the I76 deadline"
    reserve = pre_reset_zombie_ladder_reserve_sec(32)
    assert srv._pre_reset_zombie_wait_sec(True, reset_at) == 600 - 60 - reserve

    async def failed_ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    await srv._force_escalate_stuck_hold()
    assert escalations == ["present-forced"], "the ladder runs at the mark inside the cooldown"


@pytest.mark.asyncio
async def test_the_scan_event_names_the_zombie_its_parent_and_its_threads(monkeypatch, fresh_zombies):
    """The pre_reset_holder_scan event shows each zombie gap's parent (pid, comm, state) and thread count,
    so a parent that does not reap its children is visible."""
    order, events = [], []
    box = _Box(monkeypatch, [], order=order)
    _scans(monkeypatch, _gap(4303))
    _broker_seams(monkeypatch, box, order, events)
    monkeypatch.setattr(device_holders, "zombie_info", _zombies(p4303="exited"))
    monkeypatch.setattr(device_holders, "device_users_outside", lambda named: ([], True))
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert rc == 0 and "reset" in order
    (scan,) = [f for k, f in events if k == "pre_reset_holder_scan"]
    assert scan["outcome"] == "clear" and scan["gaps"] == []
    (z,) = scan["zombies"]
    assert z["pid"] == 4303 and z["verdict"] == "exited"
    assert (z["ppid"], z["parent_comm"], z["parent_state"], z["threads"]) == (4000, "pytest", "S", 1)


@pytest.mark.asyncio
async def test_an_operator_reset_records_a_pid_gap_and_still_clears_the_readers(monkeypatch):
    box = _Box(monkeypatch, [])
    _scans(monkeypatch, _gap(4303, READER))
    result = await box.run(tenants_block=False, rescan_settle_sec=0)
    assert result.gaps and result.outcome == "killed" and result.refusal() == ""


@pytest.mark.asyncio
async def test_a_gap_no_pid_explains_is_recorded_and_the_reset_proceeds(monkeypatch):
    box = _Box(monkeypatch, [])
    scan = HolderScan(holders=[READER])
    scan.mark_incomplete("3 fd tables unreadable")
    calls = _scans(monkeypatch, scan)
    result = await box.run(tenants_block=True, rescan_settle_sec=0)
    assert len(calls) == 1, "no pid to wait for, so no rescan"
    assert result.scan_complete is False and result.gaps == []
    assert result.outcome == "killed" and result.refusal() == ""


@pytest.mark.asyncio
async def test_an_automatic_reset_past_an_unseen_holder_is_refused_and_the_fabric_stays_owed(monkeypatch):
    order, events = [], []
    box = _Box(monkeypatch, [], order=order)
    _scans(monkeypatch, _gap(4303))
    _broker_seams(monkeypatch, box, order, events)
    FABRIC_DEBT.owed = ""
    try:
        rc, out = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
        assert rc is None and out.startswith("reset refused:") and "reset" not in order
        assert [f["outcome"] for k, f in events if k == "pre_reset_holder_scan"] == ["scan_incomplete"]
        assert FABRIC_DEBT.owed
    finally:
        FABRIC_DEBT.owed = ""


@pytest.mark.asyncio
async def test_a_scan_that_raises_lets_the_reset_proceed(monkeypatch):
    """A scan bug must not stop a recovery: the reset runs and the scan event says scan_error."""
    order, events = [], []
    box = _Box(monkeypatch, [], order=order)
    _broker_seams(monkeypatch, box, order, events)

    def broken():
        raise RuntimeError("proc walk failed")

    monkeypatch.setattr(device_holders, "enumerate_device_holders", broken)
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert rc == 0 and "reset" in order
    assert [f["outcome"] for k, f in events if k == "pre_reset_holder_scan"] == ["scan_error"]


@pytest.mark.asyncio
async def test_each_holder_is_identified_before_it_is_signalled(monkeypatch):
    """Logging only: the scan reads each holder's identity when it finds it, before any signal, so
    a reader it kills is still named afterwards. The verdict and the signals are unchanged."""
    order: list = []
    box = _Box(monkeypatch, [TENANT, READER], order=order)
    monkeypatch.setattr(
        prs, "holder_identity", lambda pid: order.append(f"identify:{pid}") or {"pid": pid, "status": "live"}
    )
    result = await box.run(tenants_block=False)
    assert [r["pid"] for r in result.identities] == [TENANT.pid, READER.pid]
    assert order.index(f"identify:{READER.pid}") < order.index(f"kill:{READER.pid}:TERM")
    assert box.sent == {READER.pid: ["TERM"]} and result.outcome == "killed"


@pytest.mark.asyncio
async def test_a_holder_gone_before_its_identity_is_read_changes_nothing(monkeypatch, tmp_path):
    from tt_device_mcp import holder_identity

    monkeypatch.setattr(holder_identity, "PROC_DIR", str(tmp_path))  # no /proc entry: exited after the scan
    box = _Box(monkeypatch, [READER])
    result = await box.run(tenants_block=True)
    assert result.identities == [{"pid": READER.pid, "status": "gone"}]
    assert box.sent == {READER.pid: ["TERM"]} and result.outcome == "killed"


@pytest.mark.asyncio
async def test_the_scan_event_and_log_carry_the_holder_identities(monkeypatch):
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    row = {"pid": 4101, "status": "live", "ppid": 1, "user": "u", "cmdline": "python3 x.py"}
    monkeypatch.setattr(srv, "clear_device_readers", _scan_returning(prs.PreResetScan(found=[{}], identities=[row])))
    lines = []
    monkeypatch.setattr(
        srv, "logger", type("L", (), {"info": lambda s, m: lines.append(m), "warning": lambda s, m: None})()
    )
    assert await srv._pre_reset_holder_scan(["tt-smi", "-r"], "[broker]ladder", lambda m: None) == ""
    (scan,) = [f for k, f in events if k == "pre_reset_holder_scan"]
    assert scan["identities"] == [row]
    assert any("pid 4101" in m and "cmdline='python3 x.py'" in m for m in lines), lines


def _scan_returning(result):
    async def fake(**kw):
        return result

    return fake
