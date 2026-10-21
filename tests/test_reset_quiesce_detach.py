# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A reset step detaches every all-ones chip before it stops the pollers (spec 03 I84, I66).

Every reset that quiets the bus does it in ``reset_with_quiesce`` (the operator reset, every ladder
rung, the mesh reset and its repeat in the last-chance sweep, the hold-deadline ladder) or around the
startup check in ``_pollers_quiesced_for``. A stopped poller closes its fds, and tt-kmd's close path
reaches every chip that is not detached. So a chip reading all-ones that is still attached is
PCI-removed first; one that will not detach refuses the reset (the pollers stay up, nothing runs),
the hold stays, and the 10-min bound asks for the power cycle. A chip that only stalled still answers:
the pollers stop at once with no detach. No process is spawned and no device touched: the remove,
the reset and the poller stop are stubbed, and the chips are sysfs directories in a temp dir.
"""

import ast
import inspect
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import fsm_dirty
from tests.test_classac_power_cycle_direct import rig  # noqa: F401 - the fixture
from tests.test_hold_bound import _drain, _present_hold_at_the_bound
from tests.test_hold_lift_full_check import FABRIC, VERDICTS
from tests.test_post_reset_gate import mesh  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import chip_state, read_health_events
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base

CHIPS = [str(i) for i in range(4)]
OPERATOR = ["tt-smi", "-r"]  # the reset tool and the ladder's chip reset
MESH = ["tt-smi", "-glx_reset"]  # the mesh reset and the last-chance sweep's repeat


class _NoRescan:
    def __init__(self, *_a):
        pass

    def write_text(self, *_a):
        return None


@pytest.fixture
def bus(monkeypatch):
    """Chips 0-3 attached (sysfs class nodes); one ordered log of every remove, poller change and reset."""
    for i in CHIPS:
        (pci.SYSFS_CLASS_DIR / f"tenstorrent!{i}").mkdir(exist_ok=True)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_pollers_owed", "")
    monkeypatch.setattr(srv.sampler, "quiesce_refused", {})
    monkeypatch.setattr(srv.sampler, "dead_chip_strikes", {})
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: f"0000:46:00.{idx}")
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None, raising=False)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_fence", lambda: "")
    monkeypatch.setattr(base, "Path", _NoRescan)
    monkeypatch.setattr(base, "drop_returned_isolated", lambda *a, **k: None)
    log: list = []
    detach_ok: dict = {}

    def remove(idx):
        log.append(("detach", idx))
        ok = detach_ok.get(idx, True)
        if ok:
            (pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}").rmdir()
        return ok

    async def pollers(active, _log):
        log.append(("pollers", active))
        return ["tt-telemetry"] if not active else []

    async def run(argv, _log, owner="[broker]health-gate", on_output=None):
        log.append(("reset", argv[-1]))
        return 0, ""

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "isolate_chip", remove)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(base.asyncio, "sleep", no_sleep)
    return log, detach_ok


def _all_ones(*ids, strike=False):
    """Chips the sampler saw reading all-ones: lost on two samples, or a first strike."""
    if strike:
        for i in ids:
            srv.sampler.dead_chip_strikes[i] = 1
    else:
        chip_state.mark_lost(ids, "test", chip_state.chip_nodes(ids))


def _at(log, entry):
    return log.index(entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [OPERATOR, MESH], ids=["operator-or-ladder", "mesh-reset-or-sweep"])
@pytest.mark.parametrize("strike", [False, True], ids=["lost", "first-strike"])
async def test_a_reset_detaches_the_all_ones_chip_before_the_poller_stop(bus, argv, strike):
    log, _ = bus
    _all_ones("2", strike=strike)
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(argv, lambda m: None, owner="manual")
    assert rc == 0
    assert _at(log, ("detach", "2")) < _at(log, ("pollers", False)) < _at(log, ("reset", argv[-1])), log
    assert "2" in srv.isolated_chips, "the reset's rescan brings the removed chip back"
    (ev,) = read_health_events(kinds={"reset_quiesce_detach"})
    assert ev["detached"] == ["2"] and ev["refused"] is None
    assert not srv.sampler.quiesce_refused


@pytest.mark.asyncio
async def test_the_sweep_s_repeat_reset_with_the_flag_already_set_detaches_first_too(bus):
    """The last-chance sweep sets reset_in_flight before it calls in; the detach still runs first and
    the flag is the sweep's to clear."""
    log, _ = bus
    _all_ones("1")
    srv.recovery_mechanism.reset_in_flight = True
    try:
        rc, _ = await srv.recovery_mechanism.reset_with_quiesce(MESH, lambda m: None)
    finally:
        srv.recovery_mechanism.reset_in_flight = False
    assert rc == 0 and _at(log, ("detach", "1")) < _at(log, ("pollers", False)), log


@pytest.mark.asyncio
async def test_a_stalled_only_chip_stops_the_pollers_at_once_with_no_detach(bus):
    """A frozen heartbeat that still answers is not all-ones: nothing to detach, the stop is immediate."""
    log, _ = bus
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(OPERATOR, lambda m: None)
    assert rc == 0 and log[0] == ("pollers", False) and not [e for e in log if e[0] == "detach"], log
    assert not read_health_events(kinds={"reset_quiesce_detach"})


@pytest.mark.asyncio
async def test_a_chip_already_detached_needs_no_remove(bus):
    """The sampler's valve already removed it: no node, nothing to detach, the stop is immediate."""
    log, _ = bus
    _all_ones("3")
    (pci.SYSFS_CLASS_DIR / "tenstorrent!3").rmdir()
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(OPERATOR, lambda m: None)
    assert rc == 0 and log[0] == ("pollers", False), log


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["failed", "timed out", "unavailable"])
async def test_a_failed_detach_refuses_the_reset_and_stops_nothing(bus, monkeypatch, fault):
    log, detach_ok = bus
    if fault == "failed":
        detach_ok["2"] = False
    elif fault == "timed out":
        monkeypatch.setattr(telemetry, "DETACH_TIMEOUT_SEC", 0.2)
        monkeypatch.setattr(srv, "isolate_chip", lambda idx: log.append(("detach", idx)) or time.sleep(1) or True)
    else:
        monkeypatch.setattr(srv.sampler._deps, "isolate_chip", None)
    _all_ones("2")
    rc, out = await srv.recovery_mechanism.reset_with_quiesce(OPERATOR, lambda m: None, owner="manual")
    assert rc is None and out.startswith("reset refused") and "not detached" in out
    assert ("pollers", False) not in log and not [e for e in log if e[0] == "reset"], log
    assert not srv.recovery_mechanism.reset_in_flight
    assert srv.sampler.quiesce_refused_live() == ["2"]
    ev = read_health_events(kinds={"reset_quiesce_detach"})[-1]
    assert ev["refused"] == ("detach_unavailable" if fault == "unavailable" else "detach_failed")
    assert ev["host_at_risk"] is True
    assert read_health_events(kinds={"reset_fenced"})[-1]["owner"] == "manual"
    assert srv._kill_refused_reason(), "the 10-min bound goes to the host rung"


@pytest.mark.asyncio
async def test_a_second_reset_over_the_same_stuck_chip_is_refused_with_no_new_remove(bus):
    log, detach_ok = bus
    detach_ok["2"] = False
    _all_ones("2")
    await srv.recovery_mechanism.reset_with_quiesce(OPERATOR, lambda m: None)
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(MESH, lambda m: None)
    assert rc is None and log == [("detach", "2")], log
    assert read_health_events(kinds={"reset_quiesce_detach"})[-1]["refused"] == "detach_failed_earlier"


@pytest.mark.asyncio
async def test_every_chip_all_ones_detaches_nothing_and_refuses_the_reset(bus):
    """A whole-mesh all-ones is never isolated (spec 03 I19); that class goes to the power cycle."""
    log, _ = bus
    _all_ones(*CHIPS)
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(MESH, lambda m: None)
    assert rc is None and log == [], log
    assert read_health_events(kinds={"reset_quiesce_detach"})[-1]["refused"] == "all_chips_all_ones"


@pytest.mark.asyncio
async def test_the_startup_check_detaches_before_its_poller_stop(bus):
    log, _ = bus
    _all_ones("1")
    async with srv._pollers_quiesced_for("the startup fabric check", lambda m: None) as stopped:
        assert stopped == ["tt-telemetry"]
    assert _at(log, ("detach", "1")) < _at(log, ("pollers", False)) < _at(log, ("pollers", True)), log


@pytest.mark.asyncio
async def test_a_failed_detach_at_startup_keeps_the_pollers_up_and_the_check_runs(bus):
    log, detach_ok = bus
    detach_ok["1"] = False
    _all_ones("1")
    ran = False
    async with srv._pollers_quiesced_for("the startup fabric check", lambda m: None) as stopped:
        ran = True
        assert stopped == []
    assert ran and log == [("detach", "1")], log
    assert srv._kill_refused_reason()


@pytest.mark.asyncio
async def test_a_refused_detach_gets_the_power_cycle_at_the_bound_with_no_ladder(
    rig, monkeypatch, clear_job_state  # noqa: F811
):
    """Goal 10: the hold keeps its bound. No holder was refused, only the reset's detach: the bound
    still skips the ladder (its resets would be refused) and asks for the power cycle."""
    (pci.SYSFS_CLASS_DIR / "tenstorrent!13").mkdir(exist_ok=True)
    monkeypatch.setattr(srv.sampler, "quiesce_refused", {"13": time.time()})
    monkeypatch.setattr(srv, "_kill_refused_pids", {})
    monkeypatch.setattr(srv, "_kill_refused_job_pid", None)
    monkeypatch.setattr(srv, "current_process", None)
    rig["scan"] = HolderScan(holders=[], complete=True)
    _present_hold_at_the_bound(rig, monkeypatch, why="gate_error", detail="chip 13 reads all-ones")
    chip_state.mark_lost(["13"], "test", chip_state.chip_nodes(["13"]))

    srv._check_hold_deadline(now=datetime.now())
    await _drain()

    order = rig["order"]
    kinds = [k for k, _ in rig["events"]]
    assert order.count("power_cycle") == 1, order
    assert "glx_reset" not in order and "sweep" not in order, order
    assert "hold_bound_kill_refused" in kinds
    assert "released" not in order


@pytest.mark.parametrize("verdict", VERDICTS)
def test_the_refusal_record_clears_only_on_a_passing_full_check(mesh, monkeypatch, verdict):  # noqa: F811
    """Goals 8/11: the record lifts nothing; a failed full check, or fewer links than cabled, keeps
    both the hold and the record."""
    monkeypatch.setattr(srv.sampler, "quiesce_refused", {"13": time.time()})
    fsm_dirty(srv, "chip(s) 13 read all-ones", why="heartbeat")
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC[verdict])})

    released = srv._clear_device_dirty(verified=True, why="gate/idle: verified healthy")

    assert released is (verdict == "pass")
    assert (srv.sampler.quiesce_refused == {}) is (verdict == "pass")
    assert (srv.fsm.state is ServerState.HEALTHY) is (verdict == "pass")


def test_every_poller_stop_is_behind_a_detach_or_the_all_ones_deferral():
    """The only poller stops: the reset step and the startup block (both detach first), the
    sampler's hung-chip stop (it defers while a chip reads all-ones) and the valve (after its
    remove). A new stop site has to come through one of them."""
    sites = []
    for mod in (srv, base, telemetry):
        tree = ast.parse(Path(inspect.getsourcefile(mod)).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "attr", getattr(node.func, "id", "")).endswith("set_device_pollers")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value is False
                ):
                    sites.append(fn.name)
    assert sorted(set(sites)) == sorted(
        {"reset_with_quiesce", "_pollers_quiesced_for", "stop_pollers_for_hung", "isolate_dead_chips"}
    ), sites
