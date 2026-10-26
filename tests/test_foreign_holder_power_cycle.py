# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I103: only a running broker job holds an automatic power cycle; a holder outside every
broker job or exec scope never vetoes or delays it, and a queued or waiting job does not either.

Nothing here releases a device: the boot after a cycle runs the startup gate's full check, fabric
included (03 I4), and a partial drop still gets the full ladder and sweep first (04 I100)."""

import functools
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests import test_classac_power_cycle_direct as _direct
from tests import test_offbus_power_cycle as _gated_cycle
from tests.conftest import patch_health_event
from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off
from tests.test_host_action_suppress import REASON, held  # noqa: F401 - the auto power cycle's fixture
from tests.test_pre_reset_holder_scan import READER, _Box, _gap, _scans
from tt_device_mcp import device_holders, holder_identity
from tt_device_mcp import pre_reset_scan as prs
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health.recovery import galaxy

FOREIGN = SimpleNamespace(pid=4242, uid=1000, scoped=False)  # a host process outside every broker scope
JOB_HOLDER = SimpleNamespace(pid=4343, uid=1000, scoped=True)  # a broker job's own process
rig = _direct.rig  # the class A/C rig fixture, shared


def _holders(rig, monkeypatch, *holders):
    monkeypatch.setattr(galaxy, "is_tenant", lambda h: True)
    monkeypatch.setattr(galaxy, "holder_comm", lambda pid: "python")
    rig["scan"] = HolderScan(holders=list(holders), complete=True)


def _job(monkeypatch, status):
    job = srv.Job(id="7", owner="alice", workspace="/w", command="pytest", queued_at="")
    job.status = status
    monkeypatch.setattr(srv, "jobs", {"7": job})
    return job


def _whole_tray(rig):
    rig["beats"] = _beats_without(_off(rig["map"], {2: 8}))
    rig["g"]._td_reset_seen = True  # no prelude: only the hold-off is under test


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["offbus", "offbus-forced"])
async def test_a_foreign_holder_never_delays_the_all_chips_off_cycle(rig, monkeypatch, phase):
    _holders(rig, monkeypatch, FOREIGN)
    _whole_tray(rig)
    assert await rig["g"].escalate(phase, [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["power_cycle"], "straight to the cycle, no wait on the foreign holder"


@pytest.mark.asyncio
async def test_a_foreign_holder_at_the_bound_gets_the_ladder_and_sweep_then_the_cycle(rig, monkeypatch):
    """A partial drop climbing to the host rung with a foreign holder: the full ladder and the
    last-chance sweep run, the sweep's full check fails, and the cycle fires; the holder holds none of it."""
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=10)).isoformat())
    _holders(rig, monkeypatch, FOREIGN)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    await _gate(rig, galaxy.STAGE_POWER_CYCLE)
    assert rig["order"] == ["glx_reset", "sweep", "power_cycle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", [None, JOB_HOLDER])
async def test_a_running_broker_job_defers_the_cycle_until_it_ends(rig, monkeypatch, holder):
    _holders(rig, monkeypatch, *([FOREIGN] + ([holder] if holder else [])))
    _whole_tray(rig)
    job = _job(monkeypatch, srv.JobStatus.RUNNING)
    assert await rig["g"].escalate("offbus", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_WAITING
    assert rig["order"] == [], "the running job holds the cycle"
    job.status = srv.JobStatus.COMPLETED
    rig["scan"] = HolderScan(holders=[FOREIGN], complete=True)  # the job's processes are gone
    assert await rig["g"].escalate("offbus", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["power_cycle"], "the cycle fires once the job ends"


@pytest.mark.asyncio
async def test_a_waiting_or_queued_job_does_not_defer_the_cycle(rig, monkeypatch):
    """A job queued behind the hold, or waiting on a foreign holder to let go, is not running: it
    never holds the cycle that would free the device for it."""
    _holders(rig, monkeypatch, FOREIGN)
    _whole_tray(rig)
    _job(monkeypatch, srv.JobStatus.QUEUED)
    assert await rig["g"].escalate("offbus", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert rig["order"] == ["power_cycle"]


@pytest.mark.asyncio
async def test_the_pre_cycle_scan_clears_a_foreign_holder_and_refuses_a_job_holder(monkeypatch):
    foreign = DeviceHolder(pid=4202, uid=1001)
    box = _Box(monkeypatch, [foreign, READER], tenants=[foreign])
    result = await box.run(tenants_block=True, foreign_yield=True)
    assert box.sent == {foreign.pid: ["TERM"], READER.pid: ["TERM"]}
    assert result.outcome == "killed" and result.refusal() == ""

    job_holder = DeviceHolder(pid=4303, uid=1001, scoped=True)
    box = _Box(monkeypatch, [job_holder, READER], tenants=[job_holder])
    result = await box.run(tenants_block=True, foreign_yield=True)
    assert box.sent == {} and result.outcome == "tenant_blocked"
    assert f"pid {job_holder.pid}" in result.refusal()


# ---- the pre-cycle runs drive the broker's real pre-reset scan ----------------------------------


def _scanned_resets(rig, monkeypatch, box):
    """The rig's mesh reset and sweep first run the broker's real pre-reset holder scan, as
    ``reset_with_quiesce`` does: ``clear_readers_before_reset`` -> ``_pre_reset_holder_scan`` ->
    ``clear_device_readers`` (only its signal and /proc seams are the box's). A refused scan runs no reset."""
    g, order = rig["g"], rig["order"]
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
            rescan_settle_sec=0,
        ),
    )
    monkeypatch.setattr(g.mechanism, "pre_reset_scan", srv._pre_reset_holder_scan)
    monkeypatch.setattr(srv, "_pre_reset_blocker", None)
    rig["refusals"] = []

    def scanned(name, inner):
        async def run(*a, **k):
            refused = await g.mechanism.clear_readers_before_reset(["tt-smi", "-glx_reset"], f"[broker]{name}", print)
            rig["refusals"].append((name, refused))
            if refused:
                order.append(f"{name}:refused")
                return False
            return await inner(*a, **k)

        return run

    monkeypatch.setattr(g, "_reset_and_verify_device", scanned("glx_reset", g._reset_and_verify_device))
    monkeypatch.setattr(g, "_issue_all_resets_back_to_back", scanned("sweep", g._issue_all_resets_back_to_back))


FOREIGN_FD = DeviceHolder(pid=4242, uid=1000)  # FOREIGN as the pre-reset scan reads it


@pytest.mark.asyncio
async def test_the_bound_ladder_and_sweep_resets_clear_a_foreign_holder_then_the_cycle(rig, monkeypatch):
    """Spec 04 I103 at the 10-min bound: the bound's own ladder (escalate bound=True) and the sweep
    run their resets through the real pre-reset scan, which TERMs the unscoped holder and refuses
    neither reset; the full check still fails, so the cycle fires and nothing is released."""
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=10)).isoformat())
    _holders(rig, monkeypatch, FOREIGN)
    box = _Box(monkeypatch, [FOREIGN_FD], tenants=[FOREIGN_FD])
    _scanned_resets(rig, monkeypatch, box)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    off = sorted(set(map(str, range(EXPECTED))) - set(rig["beats"]), key=int)
    await rig["g"].escalate("offbus-forced", off, EXPECTED, lambda m: None, bound=True)
    assert rig["order"] == ["glx_reset", "sweep", "power_cycle"]
    assert [name for name, refused in rig["refusals"] if refused] == []
    assert {name for name, _ in rig["refusals"]} == {"glx_reset", "sweep"}
    assert box.sent[FOREIGN_FD.pid][0] == "TERM"
    assert "released" not in rig["order"]


@pytest.mark.asyncio
async def test_a_reset_outside_a_pre_cycle_run_is_refused_and_the_sweep_clears_the_holder(rig, monkeypatch):
    """The gate's own mesh reset (no bound, no cycle next) still refuses under the unscoped holder;
    the last-chance sweep right before the cycle clears it and runs."""
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=10)).isoformat())
    _holders(rig, monkeypatch, FOREIGN)
    box = _Box(monkeypatch, [FOREIGN_FD], tenants=[FOREIGN_FD])
    _scanned_resets(rig, monkeypatch, box)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert rig["order"] == ["glx_reset:refused"] and box.sent == {}
    assert f"pid {FOREIGN_FD.pid}" in rig["refusals"][0][1]
    await _gate(rig, galaxy.STAGE_POWER_CYCLE)
    assert rig["order"] == ["glx_reset:refused", "sweep", "power_cycle"]
    assert box.sent == {FOREIGN_FD.pid: ["TERM"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["scoped", "running-job"])
async def test_the_pre_cycle_scan_still_refuses_a_job_holder_through_the_broker(monkeypatch, who):
    """Through the broker's own scan, inside a pre-cycle run: an unscoped holder is cleared, while a
    holder inside a broker scope or the running job's own process refuses the reset."""
    events: list = []
    holder = DeviceHolder(pid=4303, uid=1001, scoped=who == "scoped")
    box = _Box(monkeypatch, [FOREIGN_FD, holder], tenants=[FOREIGN_FD, holder])
    monkeypatch.setattr(
        srv,
        "clear_device_readers",
        functools.partial(
            prs.clear_device_readers,
            kill=box.kill,
            read_starttime=lambda pid: "100",
            getpid=lambda: 1,
            getpgid=lambda pid: 1 if pid == 1 else 2,
            unkillable=lambda pid: False,
            holder_in_reset=lambda pid: False,
            offbus_chips=lambda: [],
        ),
    )
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv.recovery_mechanism, "pre_reset_scan", srv._pre_reset_holder_scan)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "_pre_reset_blocker", None)
    job = SimpleNamespace(pid=holder.pid, returncode=None) if who == "running-job" else None
    monkeypatch.setattr(srv, "current_process", job)

    async def scan():
        return await srv.recovery_mechanism.clear_readers_before_reset(["tt-smi", "-glx_reset"], "[broker]t", print)

    with prs.foreign_holders_yield():
        refused = await scan()
    assert f"pid {holder.pid}" in refused and box.sent == {}

    box.holders = [FOREIGN_FD]
    with prs.foreign_holders_yield():
        assert await scan() == ""
    assert box.sent == {FOREIGN_FD.pid: ["TERM"]}
    box.sent.clear()
    assert f"pid {FOREIGN_FD.pid}" in await scan(), "outside a pre-cycle run the unscoped holder still refuses"
    assert box.sent == {}


GAP = 4303  # a pid the driver names whose fd table the scan cannot read


def _gap_seams(monkeypatch, *, scoped=False):
    monkeypatch.setattr(device_holders, "_proc_uid", lambda pid: 1000)
    monkeypatch.setattr(device_holders, "in_broker_scope", lambda pid, *a: scoped)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, outcome",
    [("visible", "killed"), ("no-proc", "scan_incomplete"), ("scoped", "tenant_blocked"), ("d-state", "unkillable")],
)
async def test_a_pre_cycle_reset_runs_past_a_visible_gap_but_not_an_unseen_scoped_or_stuck_one(
    monkeypatch, case, outcome
):
    """Spec 04 I103: in a pre-cycle run, a gap pid /proc shows here outside every broker scope is run
    past, unsignalled, and recorded; one with no /proc entry here (another pid namespace) still refuses,
    one inside a broker scope is a tenant, one stuck in the kernel keeps the I21 refusal."""
    _gap_seams(monkeypatch, scoped=case == "scoped")
    box = _Box(monkeypatch, [], stuck=[GAP] if case == "d-state" else [])
    _scans(monkeypatch, _gap(GAP, READER))
    starts = {GAP: None} if case == "no-proc" else {}
    result = await box.run(
        tenants_block=True, foreign_yield=True, rescan_settle_sec=0, read_starttime=lambda pid: starts.get(pid, "100")
    )
    assert result.outcome == outcome
    if case == "visible":
        assert not result.refusal() and box.sent == {READER.pid: ["TERM"]}
        assert result.gaps == [] and len(result.gaps_passed) == 1 and str(GAP) in result.gaps_passed[0]
    else:
        assert str(GAP) in result.refusal() and box.sent == {} and result.gaps_passed == []


@pytest.mark.asyncio
async def test_a_visible_gap_still_refuses_outside_a_pre_cycle_run(monkeypatch):
    _gap_seams(monkeypatch)
    box = _Box(monkeypatch, [])
    _scans(monkeypatch, _gap(GAP, READER))
    result = await box.run(tenants_block=True, rescan_settle_sec=0)
    assert result.outcome == "scan_incomplete" and box.sent == {} and result.gaps_passed == []


@pytest.mark.asyncio
async def test_an_unreadable_scan_at_the_bound_runs_the_ladder_and_sweep_resets_then_the_cycle(rig, monkeypatch):
    """Spec 04 I103 at the 10-min bound: a holder scan left incomplete by a pid /proc shows here
    outside every broker scope costs neither the ladder's reset nor the sweep's; the full check still
    fails, so the cycle fires and nothing is released. The pre_reset_holder_scan event names the gap."""
    events: list = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=10)).isoformat())
    _holders(rig, monkeypatch)
    _gap_seams(monkeypatch)
    box = _Box(monkeypatch, [])
    _scans(monkeypatch, _gap(GAP))
    _scanned_resets(rig, monkeypatch, box)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    off = sorted(set(map(str, range(EXPECTED))) - set(rig["beats"]), key=int)
    await rig["g"].escalate("offbus-forced", off, EXPECTED, lambda m: None, bound=True)
    assert rig["order"] == ["rescan", "glx_reset", "sweep", "power_cycle"], "no tenant: the rescan rung runs first"
    assert [name for name, refused in rig["refusals"] if refused] == []
    assert box.sent == {} and "released" not in rig["order"]
    scans = [f for kind, f in events if kind == "pre_reset_holder_scan"]
    assert len(scans) == 2 and all(str(GAP) in " ".join(f["gaps_passed"]) for f in scans)


@pytest.mark.asyncio
async def test_an_unreadable_scan_outside_a_pre_cycle_run_still_refuses_and_the_sweep_runs(rig, monkeypatch):
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=10)).isoformat())
    _holders(rig, monkeypatch)
    _gap_seams(monkeypatch)
    box = _Box(monkeypatch, [])
    _scans(monkeypatch, _gap(GAP))
    _scanned_resets(rig, monkeypatch, box)
    rig["beats"] = _beats_without(_off(rig["map"], {3: 1}))
    await _gate(rig, galaxy.STAGE_SMI_RESET)
    assert rig["order"] == ["glx_reset:refused"]
    await _gate(rig, galaxy.STAGE_POWER_CYCLE)
    assert rig["order"] == ["glx_reset:refused", "sweep", "power_cycle"]


# --- the journal line on every power-cycle road (I103) ---

SCOPED_FD = DeviceHolder(pid=4343, uid=1000, scoped=True)  # a broker job's own process


def _fake_proc(tmp_path, monkeypatch, *holders):
    """/proc for FOREIGN_FD (pid 4242, uid 1000, a secret on its command line) and its parent; the
    holder scan finds ``holders``."""
    root = tmp_path / "proc"
    pdir = root / str(FOREIGN_FD.pid)
    pdir.mkdir(parents=True)
    (pdir / "stat").write_text(f"{FOREIGN_FD.pid} (python) S 1 " + " ".join(["0"] * 17) + " 12345 0 0\n")
    (pdir / "status").write_text("Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n")
    (pdir / "cmdline").write_bytes(b"python\0train.py\0--token\0hunter2\0")
    (pdir / "cgroup").write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    (root / "1").mkdir()
    (root / "1" / "comm").write_text("bash\n")
    monkeypatch.setattr(holder_identity, "PROC_DIR", str(root))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=list(holders)))


def _journal(monkeypatch, events):
    """Collect the power_cycle_foreign_holders lines in ``events`` (the road's own capture stays)."""
    real = srv.health_event
    monkeypatch.setattr(
        srv,
        "health_event",
        lambda kind, **f: events.append(f) if kind == "power_cycle_foreign_holders" else real(kind, **f),
    )
    return []


async def _auto_road(monkeypatch, tmp_path, held, setup):  # noqa: F811
    setup()
    await srv._auto_power_cycle_host(lambda m: None, REASON)
    return held["fired"]


async def _offbus_road(monkeypatch, tmp_path, setup):
    hold_since, rec = _gated_cycle._gated(monkeypatch, tmp_path)
    setup()  # after the arming, which stands in its own holder scan
    _gated_cycle._ladder_ran(hold_since)
    assert await _gated_cycle._pass(hold_since) is True
    return rec["fires"]


class _Road:
    def __init__(self, road, run):
        self.road, self._run = road, run

    def __call__(self, setup=lambda: None):
        return self._run(setup)


@pytest.fixture
def road(request, monkeypatch, tmp_path, held, clear_job_state):  # noqa: F811
    """Request one power cycle down the named road, ``setup`` run once the road is armed; returns
    what fired."""
    if request.param == "auto":
        return _Road("auto", lambda setup: _auto_road(monkeypatch, tmp_path, held, setup))
    return _Road(request.param, lambda setup: _offbus_road(monkeypatch, tmp_path, setup))


@pytest.mark.asyncio
@pytest.mark.parametrize("road", ["auto", "offbus-gated"], indirect=True)
async def test_every_power_cycle_road_journals_the_foreign_holder_and_not_a_scoped_one(monkeypatch, tmp_path, road):
    """Spec 04 I103: the auto power cycle and the off-bus gated cycle each journal
    ``power_cycle_foreign_holders`` naming the unscoped holder (pid, uid, comm, redacted command
    line); a scoped holder is not listed. (On the off-bus road a scoped holder is a tenant that holds
    the cycle, so that road's scan finds the foreign holder only.)"""
    events: list = []
    holders = [FOREIGN_FD, SCOPED_FD] if road.road == "auto" else [FOREIGN_FD]
    fired = await road(
        lambda: (_fake_proc(tmp_path, monkeypatch, *holders), events.extend(_journal(monkeypatch, events)))
    )
    assert len(fired) == 1
    assert len(events) == 1, "one journal line per power-cycle request"
    rows = events[0]["holders"]
    assert [r["pid"] for r in rows] == [FOREIGN_FD.pid], "the scoped holder is not listed"
    row = rows[0]
    assert row["uid"] == 1000 and row["comm"] == "python" and row["status"] == "live"
    assert "hunter2" not in row["cmdline"] and "train.py" in row["cmdline"] and "***" in row["cmdline"]
    assert events[0]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["hangs", "raises"])
@pytest.mark.parametrize("road", ["auto", "offbus-gated"], indirect=True)
async def test_a_holder_read_that_hangs_or_raises_never_holds_the_cycle(monkeypatch, tmp_path, road, fault):
    events: list = []
    release = threading.Event()
    read: list = []

    def rows():
        read.append(1)
        if fault == "raises":
            raise RuntimeError("proc walk failed")
        release.wait(10)
        return []

    monkeypatch.setattr(srv, "_foreign_holder_rows", rows)
    monkeypatch.setattr(srv, "POWER_CYCLE_HOLDER_LOG_SEC", 0.05)
    try:
        fired = await road(lambda: _journal(monkeypatch, events))
    finally:
        release.set()
    assert read == [1], "the journal read ran on this road"
    assert len(fired) == 1 and events == []
