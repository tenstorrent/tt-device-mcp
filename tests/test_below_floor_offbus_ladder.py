# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I89: the kept rungs of a below-floor off-bus hold.

At the gate the hold gets one PCI rescan at once, and an SBR only for the chips the defaults allow
(an isolated chip, or a chip gone from the bus when TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET is set),
once per hold episode and off-bus set. Only a passing full check, fabric included, lifts it.

At the 10-min bound (I76) the ladder runs every kept rung in order: PCI rescan, SBR of the bridged
off-bus chips (an isolated or broker-removed chip, or a gone chip with the opt-in: without it a gone chip
has no bridge, so no SBR is tried and it is not queued), its one mesh reset, the full check with fabric,
then the power cycle. A chip still enumerated with its link up is on the bus: never SBR'd, the mesh
reset covers it. One whose node is present but whose link reads down or whose heartbeat reads all-ones
is removed first (the broker's readers stopped), then SBR'd; the climb's sweep runs its SBR when this
hold has run none on those chips.
Below the bound a forced run keeps its own path (the mesh reset, no rescan or SBR: the gate ran those at
once). Spec 04 I97: when the SBR before the host rung has every chip back but the full check fails on
chips left ARC-not-ready, one mesh reset of the whole mesh and the full check with fabric run before the
power cycle, once per hold and only when it ends before the bound. No test reaches a real reset, setpci
or power cycle."""

import asyncio
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold, _rung
from tests.test_fabric_topology_mismatch import _LOST, topo  # noqa: F401 - the fixture
from tests.test_inflight_reader_cut import _World
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.health import ALL_ONES
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.monitors import heartbeat, hostpci, pci
from tt_device_mcp.health.recovery import (
    OUTCOME_RECOVERED,
    OUTCOME_TERMINAL,
    OUTCOME_WAITING,
    STAGE_BRIDGE_RESET,
    base,
    galaxy,
    pcie_guard,
)
from tt_device_mcp.health.recovery.stages import bridge_reset as bridge

GONE = "31"
ALL = {str(i): 100 for i in range(32)}
SHORT = {str(i): 100 for i in range(31)}  # chip 31 off the bus: 1 of 32, below the floor of 16


def _rig(
    monkeypatch,
    tmp_path,
    *,
    opt_in=False,
    enumerated=False,
    rescan_heals=False,
    sbr_heals=False,
    mesh_rc=1,
    verdicts=(),
    held_min=21,
):
    """A 32-chip mesh holding with chip 31 off the bus. ``enumerated``: its PCIe node is still present
    (an ARC stall, not a drop). ``rescan_heals``/``sbr_heals``: that rung brings it back. ``verdicts``
    are the full checks' results in order (False once they run out). Returns (order, events, counters):
    ``order`` lists every rung, check and power cycle as it ran."""
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=1, isolated=set(), reset_result=False, sbr_verify_healthy=False
    )
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=held_min)).isoformat()
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    if opt_in:
        monkeypatch.setenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", "1")
    else:
        monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    srv.device_pci_map = {str(i): f"0000:{0x40 + i:02x}:00.0" for i in range(32)}
    pci_dir = tmp_path / "pci"
    pci_dir.mkdir()
    if enumerated:
        (pci_dir / "0000:5f:00.0").mkdir()  # chip 31's own node
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", pci_dir)
    bus = {"beats": dict(SHORT)}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(bus["beats"]))
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: expected - len(bus["beats"]))
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)
    monkeypatch.setattr(galaxy, "refuse_mesh_reset", lambda *a, **k: False)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_SETTLE_SEC", 0)  # no real settle in these tests
    order, events = [], []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))

    def rescan():
        order.append("rescan")
        if rescan_heals:
            bus["beats"] = dict(ALL)

    async def sbr(log):
        order.append(f"sbr {sorted(srv.isolated_chips, key=int)}")
        srv.galaxy_recovery.last_bridge_reset_reasons = {}  # as the real rung: every target fired
        srv.galaxy_recovery.last_bridge_reset_fired = set(srv.isolated_chips) | set(pci.removed_chip_ids())
        if sbr_heals:
            bus["beats"] = dict(ALL)
            srv.isolated_chips.clear()
        return sbr_heals

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        return mesh_rc, ""

    pending = list(verdicts)

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check" if run_fabric else "check without fabric")
        healthy = pending.pop(0) if pending else False
        return healthy, {"snapshot": {"ok": healthy}, "fabric": {"ok": healthy}}

    async def bare_mesh(indices, log):
        # The stuck-hold ladder's own mesh reset: a launched reset (rc recorded for I58), then its
        # full check.
        order.append("mesh reset")
        srv.recovery_mechanism.last_reset_rc = mesh_rc
        healthy, _evidence = await verify(32, log, run_fabric=True)
        return healthy

    async def power_cycle(log, reason):
        order.append("power cycle")

    monkeypatch.setattr(galaxy, "_pci_rescan", rescan)
    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", mesh)
    patch_recovery(monkeypatch, "_reset_and_verify_device", bare_mesh)
    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    return order, events, counters


async def _bound_ladder():
    return await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None, bound=True)


async def _gate():
    return await srv.galaxy_recovery._fire_gate_rung(
        "post-job", galaxy.BELOW_FLOOR, [GONE], 32, lambda m: None, ev=None, beats=dict(SHORT)
    )


# --- the 10-min bound ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opt_in, expected_order",
    [
        # No bridge for a gone chip without the opt-in: no SBR is tried. The mesh reset failed, so the
        # last-chance sweep runs it again with its full check right before the power cycle.
        (False, ["rescan", "mesh reset", "full check", "mesh reset", "full check", "power cycle"]),
        # Opted in, the stuck-hold ladder's own bridge rung SBRs it first; the sweep SBRs it again.
        (
            True,
            [
                "sbr ['31']",
                "rescan",
                "mesh reset",
                "full check",
                "sbr ['31']",
                "mesh reset",
                "full check",
                "power cycle",
            ],
        ),
    ],
)
async def test_the_bound_runs_every_kept_rung_in_order_then_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state, opt_in, expected_order
):
    order, events, counters = _rig(monkeypatch, tmp_path, opt_in=opt_in)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert order == expected_order
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed check never lifts"
    hold_since = srv.device_hold_episode_since
    assert srv.recovery_mechanism.rung_failed_unchanged(hold_since, "mesh_reset", {GONE}), "I58 record kept"


@pytest.mark.asyncio
async def test_the_bound_rescans_and_sbrs_before_the_cycle_after_a_failed_mesh_reset(
    monkeypatch, tmp_path, clear_job_state
):
    """An earlier forced run below the bound does not stand in for the bound's ladder: the bound runs
    the rescan and SBR again, and its last-chance sweep runs every rung once more (the SBR and the mesh
    reset that already failed on the same chips, then the full check) right before the power cycle."""
    sweep = ["mesh reset", "full check", "power cycle"]
    for opt_in, expected_order in (
        (True, ["rescan", "sbr ['31']", "sbr ['31']", *sweep]),
        (False, ["rescan", *sweep]),
    ):
        (tmp_path / str(opt_in)).mkdir()
        order, _events, _counters = _rig(monkeypatch, tmp_path / str(opt_in), opt_in=opt_in)
        srv.isolated_chips.clear()
        hold_since = srv.device_hold_episode_since
        srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", {GONE}, {GONE}, rc=1)
        await _bound_ladder()
        assert order == expected_order, f"opt_in={opt_in}"


@pytest.mark.asyncio
async def test_a_forced_run_below_the_bound_repeats_a_failed_mesh_reset_before_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """I58 no longer skips a mesh reset that already failed on the same chips: the sweep runs it again,
    with its full check, right before the power cycle."""
    order, events, _counters = _rig(monkeypatch, tmp_path)
    hold_since = srv.device_hold_episode_since
    srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order == ["mesh reset", "full check", "power cycle"]
    assert "reset_rung_skipped_failed_unchanged" not in events


@pytest.mark.asyncio
async def test_sbr_that_heals_at_the_bound_lifts_on_the_full_check_with_no_mesh_reset(
    monkeypatch, tmp_path, clear_job_state
):
    """Bridged chips only: the gone chip here has the opt-in. The mesh reset is already spent (I87), so
    the bound's own rescan and SBR run, not the stuck-hold ladder's bridge rung."""
    order, _events, counters = _rig(monkeypatch, tmp_path, opt_in=True, sbr_heals=True, verdicts=[True])
    srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    await _bound_ladder()
    assert order == ["rescan", "sbr ['31']", "full check"], "the shortest path: no mesh reset, no power cycle"
    assert counters["dirty_cleared"] == 1, "lifted once, on the passing full check"


@pytest.mark.asyncio
async def test_an_enumerated_chip_is_never_sbrd_even_at_the_bound(monkeypatch, tmp_path, clear_job_state):
    order, _events, _counters = _rig(monkeypatch, tmp_path, opt_in=True, enumerated=True)
    await _bound_ladder()
    assert order == ["rescan", "mesh reset", "full check", "mesh reset", "full check", "power cycle"]


@pytest.mark.asyncio
async def test_below_the_bound_a_gone_chip_waits_for_the_opt_in_and_the_host_rung_for_the_bound(
    monkeypatch, tmp_path, clear_job_state
):
    """A forced run on a young hold keeps its own path: the mesh reset, no rescan and no SBR of a gone
    chip (the gate ran the rescan at once; the SBR waits for the opt-in or the bound), and the host
    rung waits for the ladder's end (I38; a reset with no recorded failure, so I58 does not climb at
    once)."""
    order, events, _counters = _rig(monkeypatch, tmp_path, held_min=3, mesh_rc=None)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order == ["mesh reset", "full check"]
    assert "host_rung_deferred_until_ladder_end" in events


@pytest.mark.asyncio
async def test_below_the_bound_a_mesh_reset_that_failed_on_the_same_chips_climbs_at_once(
    monkeypatch, tmp_path, clear_job_state
):
    """I58: every rung below the host pair has run once, so no ceiling wait. The full ladder still runs
    right before the power cycle: the sweep repeats the failed mesh reset under the I97 deadline, then
    the full check with fabric, and only its failure requests the cycle."""
    order, events, _counters = _rig(monkeypatch, tmp_path, held_min=3)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order == ["mesh reset", "full check", "mesh reset", "full check", "power cycle"]
    assert "host_rung_not_deferred" in events
    assert "last_chance_sweep_repeats_failed_reset" in events and "reset_rung_skipped_failed_unchanged" not in events
    order.clear()
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=10, seconds=5)).isoformat()
    await _bound_ladder()
    assert order == ["rescan", "mesh reset", "full check", "power cycle"], "the bound's full ladder (no bridge: no SBR)"


@pytest.mark.asyncio
async def test_a_cooldown_denial_at_the_bound_still_runs_the_ladder_and_arms_the_retry(
    monkeypatch, tmp_path, clear_job_state
):
    """The kept rungs run ahead of the host-rung permission: a cooldown delays only the power cycle,
    which the I65 retry asks for the moment the cooldown ends."""
    order, _events, _counters = _rig(monkeypatch, tmp_path)
    rm = srv.recovery_mechanism
    until = datetime.now().timestamp() + 900
    monkeypatch.setattr(rm, "power_cycle_retry", None)
    monkeypatch.setattr(rm, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(rm, "_journal_auto_recovery_denied", lambda *a: None)
    state = {"cooling": True}

    def allowed(action, tenant_active=False, **k):
        if state["cooling"]:
            return rm._deny_power_cycle(
                {"until": until, "last": until - 1800, "cause": "boot", "interval_sec": 1800, "why": "cooldown"}
            )
        return True, ""

    monkeypatch.setattr(rm, "auto_recovery_allowed", allowed)
    await _bound_ladder()
    assert order == ["rescan", "mesh reset", "full check"], "every rung ran; the cycle waits"
    assert rm.power_cycle_retry is not None and not rm.power_cycle_retry_due(until - 1)
    assert rm.power_cycle_retry_due(until), "asked again the moment the cooldown ends"
    state["cooling"] = False
    order.clear()
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_bound_tenant_blocker", lambda *a: None)
    await srv._hold_bound_host_rung(601, lambda m: None)
    assert order == ["power cycle"], "the cooldown's end goes straight to the power cycle, no second ladder"


@pytest.mark.asyncio
async def test_a_remove_blocked_in_the_kernel_still_lets_the_bound_reach_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """Spec 03 I83 / 04 I76: a post-reset cut whose PCI remove blocks in the kernel holds the PCI
    rescan/remove lock, so every later rescan would queue behind it. The remove's timeout latches
    PCI-wedged: the bound's rescan rung is refused at once (no second write, no second timeout), counts
    as failed and the ladder goes on to the full check and the power cycle."""
    order, events, _counters = _rig(monkeypatch, tmp_path)
    kernel_lock = threading.Event()  # never released while the test runs: the blocked remove
    w = _World(monkeypatch, detach_ok=lambda idx: kernel_lock.wait(30) and True, beats={GONE: ALL_ONES})
    monkeypatch.setattr(telemetry, "INFLIGHT_DETACH_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(base, "PCI_SYSFS_WRITE_TIMEOUT_SEC", 0.2)

    def blocked_rescan():
        order.append("rescan")
        kernel_lock.wait(30)

    monkeypatch.setattr(galaxy, "_pci_rescan", blocked_rescan)
    try:
        assert await srv.sampler.cut_post_reset_failed([GONE]) is False
        assert w.events[-1][1]["remove_blocked"] is True
        t0 = time.monotonic()
        assert await asyncio.wait_for(_bound_ladder(), 10) == OUTCOME_TERMINAL
        assert time.monotonic() - t0 < 5
    finally:
        kernel_lock.set()  # free the worker threads
    # The latch asks for the power cycle at once (the watchdog's tick), before any ladder runs; the
    # bound's own ladder then skips the refused rescan and ends in the power cycle too, after its sweep
    # repeats the failed mesh reset and its full check.
    assert order[0] == "power cycle", "the PCI-wedged latch requested the power cycle at once"
    assert order[1:] == [
        "mesh reset",
        "full check",
        "mesh reset",
        "full check",
        "power cycle",
    ], "the rescan never started a second write"
    assert srv.PCI_WRITER.latched, "the blocked remove latched PCI-wedged"
    assert "pci_rescan_timeout" not in events, "one PCI timeout per ladder: the refused rescan did not wait"


def _real_bridge_rung(monkeypatch, tmp_path):
    """Undo _rig's fake: the real _recover_isolated_chips and reset_chip_via_bridge run, against a fake
    sysfs where chip 31's bridge (0000:40:1f.0) is still enumerated with chip 31's bus as its secondary
    bus and chip 31's own node is gone. Returns ``spy``: the bridge lookups by secondary bus, the setpci
    argvs and the scoped rescans."""
    for inst in (srv.galaxy_recovery, srv.per_target_recovery):
        monkeypatch.delattr(inst, "_recover_isolated_chips", raising=False)
    spy = {"find": [], "setpci": [], "rescan": 0}
    real_find = bridge.find_bridge_by_secondary_bus

    def find(bdf):
        spy["find"].append(bdf)
        return real_find(bdf)

    def setpci(argv, *a, **k):
        spy["setpci"].append(list(argv))
        out = "0000" if argv[-1] == "BRIDGE_CONTROL" else ""
        return bridge.subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def rescan():
        spy["rescan"] += 1
        return ["0000:40:1f.0"]

    br = pci.PCI_DEVICES_DIR / "0000:40:1f.0"
    br.mkdir()
    (br / "secondary_bus_number").write_text(str(0x5F) + "\n")
    cls = tmp_path / "class"
    cls.mkdir()
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", cls)
    monkeypatch.setattr(bridge, "find_bridge_by_secondary_bus", find)
    monkeypatch.setattr(bridge.subprocess, "run", setpci)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(pcie_guard, "rescan_tt_root_ports", rescan)
    monkeypatch.setattr(recovery_pkg, "bridge_reset_enabled", lambda: True)
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    return spy


@pytest.mark.asyncio
async def test_without_the_opt_in_the_bound_tries_no_sbr_on_a_gone_chip_and_queues_nothing(
    monkeypatch, tmp_path, clear_job_state
):
    """The review finding: the bound used to queue a gone chip whatever the opt-in, the real rung found
    no bridge (no SBR fired) and the chip stayed isolated, so every later gate pass went to a no-op
    bridge rung. Now: no bridge lookup, no setpci, nothing queued, and the next pass does not route to
    the bridge rung."""
    order, events, _counters = _rig(monkeypatch, tmp_path)
    spy = _real_bridge_rung(monkeypatch, tmp_path)
    srv.isolated_chips.clear()
    for spent in (False, True):  # the floor branch, then the I87 spent-reset branch
        if spent:
            srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
        order.clear()
        await _bound_ladder()
        assert spy["find"] == [] and spy["setpci"] == [], "no SBR is tried on a gone chip without the opt-in"
        assert srv.isolated_chips == set(), "the bound leaves isolated_chips as it found it"
        assert "below_floor_sbr_skipped" in events, "the skipped SBR is journaled"
        assert order[0] == "rescan" and order[-1] == "power cycle"
    stage = _rung(monkeypatch, srv.galaxy_recovery.deps, off_bus=1, sbr_candidates=len(srv.isolated_chips))
    assert stage != STAGE_BRIDGE_RESET, "the next gate pass is not sent to a no-op bridge rung"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["opt_in", "recorded_bridge"])
async def test_a_bridged_gone_chip_reaches_the_bridge_write_at_the_bound(monkeypatch, tmp_path, clear_job_state, case):
    """The real rung: an opted-in gone chip resolves its bridge by the secondary bus; a chip the broker
    removed resets through the bridge it recorded (I69). Either way the SBR write reaches the bridge.
    The mesh reset is spent (I87), so only the bound's kept rungs reset it."""
    order, events, _counters = _rig(monkeypatch, tmp_path, opt_in=case == "opt_in")
    spy = _real_bridge_rung(monkeypatch, tmp_path)
    srv.isolated_chips.clear()
    if case == "recorded_bridge":
        # The record isolate_chip leaves when the broker removes a chip (I69), with no state file.
        monkeypatch.setattr(pci, "_save_removed_chips", lambda: None)
        monkeypatch.setitem(pci.REMOVED_CHIPS, GONE, {"bdf": "0000:5f:00.0", "bridge": "0000:40:1f.0"})
    srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    await _bound_ladder()
    writes = [a for a in spy["setpci"] if a[-1].startswith("BRIDGE_CONTROL=")]
    assert writes and all(a[2] == "0000:40:1f.0" for a in writes), spy["setpci"]
    if case == "opt_in":
        assert spy["find"] and set(spy["find"]) == {"0000:5f:00.0"}, "found by the secondary bus (once per try)"
        assert order[0] == "rescan"
    else:
        assert order[0] != "rescan", "a removed chip withholds the bare rescan (I69)"
    assert order[-1] == "power cycle", "still off after the SBR: the ladder goes on"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["bound", "gate"])
async def test_an_opted_in_gone_chip_with_no_bridge_is_unqueued(monkeypatch, tmp_path, clear_job_state, path):
    """Opted in, the gone chip is queued, but no bridge has its bus as secondary: the real rung fires no
    SBR, so the chip is un-queued. Left isolated, every later gate pass would route to a no-op bridge
    rung; at the bound the ladder still goes on to the power cycle."""
    order, events, _counters = _rig(monkeypatch, tmp_path, opt_in=True)
    spy = _real_bridge_rung(monkeypatch, tmp_path)
    (pci.PCI_DEVICES_DIR / "0000:40:1f.0" / "secondary_bus_number").unlink()
    (pci.PCI_DEVICES_DIR / "0000:40:1f.0").rmdir()
    srv.isolated_chips.clear()
    if path == "bound":
        srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
        await _bound_ladder()
        assert order[-1] == "power cycle", "still off with no SBR: the ladder goes on"
    else:
        assert await _gate() == OUTCOME_WAITING
    assert spy["find"], "the opted-in rung looked for the bridge"
    assert spy["setpci"] == [], "no bridge: no SBR fired"
    assert srv.isolated_chips == set(), "the chip the rung could not fire on is un-queued"
    assert "gone_chip_bridge_reset_unqueued" in events
    stage = _rung(monkeypatch, srv.galaxy_recovery.deps, off_bus=1, sbr_candidates=len(srv.isolated_chips))
    assert stage != STAGE_BRIDGE_RESET, "the next gate pass is not sent to a no-op bridge rung"


# --- the gate -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_rescans_at_once_and_lifts_only_on_the_full_check(monkeypatch, tmp_path, clear_job_state):
    order, _events, counters = _rig(monkeypatch, tmp_path, rescan_heals=True, verdicts=[True])
    assert await _gate() == OUTCOME_RECOVERED
    assert order == ["rescan", "full check"], "the rescan brought it back: no SBR, no mesh reset"
    assert counters["dirty_cleared"] == 1


@pytest.mark.asyncio
async def test_the_gate_sbrs_a_gone_chip_when_opted_in_once_per_episode_and_set(monkeypatch, tmp_path, clear_job_state):
    order, _events, _counters = _rig(monkeypatch, tmp_path, opt_in=True)
    assert await _gate() == OUTCOME_WAITING
    assert order == ["rescan", "sbr ['31']"], "still off after the SBR: no check, the hold stands"
    order.clear()
    assert await _gate() == OUTCOME_WAITING
    assert order == [], "latched: once per hold episode and off-bus set"
    srv.device_hold_episode_since = datetime.now().isoformat()  # a new episode
    await _gate()
    assert order[0] == "rescan"


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in, enumerated", [(False, False), (True, True)])
async def test_the_gate_keeps_the_sbr_defaults(monkeypatch, tmp_path, clear_job_state, opt_in, enumerated):
    """Before the bound: a gone chip without the opt-in, and an enumerated chip ever, get no SBR."""
    order, _events, counters = _rig(monkeypatch, tmp_path, opt_in=opt_in, enumerated=enumerated)
    assert await _gate() == OUTCOME_WAITING
    assert order == ["rescan"]
    assert counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_the_gate_waits_and_retries_when_a_tenant_holds_the_device(monkeypatch, tmp_path, clear_job_state):
    order, _events, _counters = _rig(monkeypatch, tmp_path, rescan_heals=True, verdicts=[True])
    monkeypatch.setattr(srv.galaxy_recovery, "_tenant_active", lambda scan, **k: True)
    assert await _gate() == OUTCOME_WAITING and order == []
    monkeypatch.setattr(srv.galaxy_recovery, "_tenant_active", lambda scan, **k: False)
    assert await _gate() == OUTCOME_RECOVERED, "nothing ran, so no latch: the next pass runs the rungs"


@pytest.mark.asyncio
async def test_a_fabric_check_short_of_the_cabled_links_keeps_the_hold(
    monkeypatch, tmp_path, clear_job_state, topo  # noqa: F811
):
    """Every chip is back, but the validator finds one port connection the cabling descriptor expects
    missing: the check fails and the hold stands."""
    m, state = topo
    state["first"] = _LOST
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", raising=False)
    order, events, counters = _rig(monkeypatch, tmp_path, rescan_heals=True)

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check")
        ok, detail = await m._verify_fabric_health_body(60)
        return ok, {"snapshot": {"ok": True}, "fabric": {"ok": ok, "detail": detail}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    assert await _gate() == OUTCOME_WAITING
    assert order == ["rescan", "full check"]
    assert "below_floor_kept_rungs_unverified" in events
    assert counters["dirty_cleared"] == 0, "fewer links than the cabling expects is a failed check"


# --- a chip off the bus with its PCIe node still present ---------------------------------------


def _dead_link(monkeypatch, tmp_path, order, how):
    """Chip 31's node is present (``_rig(enumerated=True)``) but its link reads down (``how`` "link")
    or its heartbeat reads all-ones (``how`` "all-ones"); ``how`` None arms only the hooks. Its remove is recorded in ``order`` after the
    broker's readers are stopped, and makes it an isolated chip, as the real hook does. Returns the
    reader process groups SIGKILLed."""
    if how == "link":
        (tmp_path / "pci" / "0000:5f:00.0" / "current_link_speed").write_text("Unknown\n")
    elif how == "all-ones":
        beats = dict(SHORT, **{GONE: ALL_ONES})
        monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(beats))
        monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 1)
    deps = srv.galaxy_recovery.deps

    def freeze(reason):
        order.append("readers stopped")
        return [{"pid": 4242, "user": "[broker]fabric-check"}]

    def isolate(idx):
        assert order[-1] == "readers stopped", "no broker-owned reader may touch the chip at its remove"
        order.append(f"isolate {idx}")
        srv.isolated_chips.add(idx)
        return True

    monkeypatch.setattr(deps, "freeze_device_readers", freeze)
    monkeypatch.setattr(deps, "isolate_chip", isolate)
    killed = []
    monkeypatch.setattr(galaxy.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    return killed


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["link", "all-ones"])
async def test_a_dead_link_chip_with_its_node_present_is_isolated_then_sbrd_at_the_bound(
    monkeypatch, tmp_path, clear_job_state, how
):
    order, events, counters = _rig(monkeypatch, tmp_path, enumerated=True)
    killed = _dead_link(monkeypatch, tmp_path, order, how)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert killed == [(4242, signal.SIGKILL)], "a reader stopped for the remove is ended once it worked"
    i = order.index("readers stopped")
    assert order[i : i + 3] == ["readers stopped", f"isolate {GONE}", f"sbr ['{GONE}']"]
    assert not any(o.startswith("sbr") for o in order[:i]), "never an SBR before the chip is removed"
    assert "full check" in order[i + 3 :] and order[-1] == "power cycle", "a failed check keeps the hold, escalates"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    assert "below_floor_linkdown_isolated" in events


@pytest.mark.asyncio
async def test_a_dead_link_chip_whose_remove_fails_is_not_reset(monkeypatch, tmp_path, clear_job_state):
    order, _events, counters = _rig(monkeypatch, tmp_path, enumerated=True)
    killed = _dead_link(monkeypatch, tmp_path, order, "link")
    monkeypatch.setattr(srv.galaxy_recovery.deps, "isolate_chip", lambda idx: order.append("remove failed") and False)
    await _bound_ladder()
    assert not killed, "a reader of a chip still attached stays stopped: its kill would reach the chip"
    assert not any(o.startswith("sbr") for o in order), "an SBR under a chip still bound would hit a live node"
    assert order[-1] == "power cycle" and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heals, verdict, how",
    [(True, True, "link"), (True, False, "link"), (False, False, "link"), (False, False, "all-ones")],
)
async def test_the_sweep_runs_its_sbr_when_none_ran_this_hold(
    monkeypatch, tmp_path, clear_job_state, heals, verdict, how
):
    """Below the bound, this hold's mesh reset already failed on exactly these chips (I58). The
    last-chance sweep still runs in full before the host rung: the readers stop, the dead-link chip is
    isolated and gets the SBR no SBR ran on this hold, and, unless that SBR and the full check with
    fabric heal it, the failed mesh reset runs once more under the I97 deadline with its own full check.
    Only a pass lifts, else the power cycle."""
    order, events, counters = _rig(
        monkeypatch, tmp_path, held_min=3, enumerated=True, sbr_heals=heals, verdicts=[verdict]
    )
    _dead_link(monkeypatch, tmp_path, order, how)
    logs = []
    srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, logs.append)
    sbr = f"sbr ['{GONE}']"
    assert order[:3] == ["readers stopped", f"isolate {GONE}", sbr], "readers stop before the isolation and SBR"
    assert not any("every rung below the host pair has run once" in m for m in logs), "no SBR had run: not true"
    if heals:
        assert order[order.index(f"sbr ['{GONE}']") + 1] == "full check"
    if heals and verdict:
        assert "power cycle" not in order
        assert counters["dirty_cleared"] == 1, "lifted once, on the just-passed full check with fabric"
        assert "mesh reset" not in order, "the SBR healed it: the failed mesh reset is not repeated"
    else:
        assert order[order.index(sbr) :][-3:] == ["mesh reset", "full check", "power cycle"]
        assert order.count("mesh reset") == 1, "the failed mesh reset runs once more, not twice"
        assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    assert "reset_rung_skipped_failed_unchanged" not in events
    assert "last_chance_sweep_repeats_failed_reset" in events


@pytest.mark.asyncio
async def test_the_sweep_sbrs_a_chip_whose_round_this_hold_already_spent(monkeypatch, tmp_path, clear_job_state):
    """The chip already had its SBR this hold and it failed: the sweep right before the power cycle still
    writes it (every rung for every off-bus chip, whatever failed earlier), then repeats the failed mesh
    reset and its full check."""
    order, _events, _counters = _rig(monkeypatch, tmp_path, held_min=3, enumerated=True)
    _dead_link(monkeypatch, tmp_path, order, "link")
    hold_since = srv.device_hold_episode_since
    srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    srv.galaxy_recovery._sbr_hold = (hold_since, frozenset({GONE}))
    logs = []
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, logs.append)
    sbr = f"sbr ['{GONE}']"
    assert order[:3] == ["readers stopped", f"isolate {GONE}", sbr], "the spent round does not skip it"
    assert order[-3:] == ["mesh reset", "full check", "power cycle"]
    assert not any("no second SBR round" in m for m in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("off", [32, 20], ids=["all-off", "above-floor"])
async def test_the_sweep_sbr_is_below_the_floor_only(monkeypatch, tmp_path, clear_job_state, off):
    """At or above the floor (16 of 32) the drop is a mass drop, all chips off (class C) included: once
    its mesh reset failed on exactly these chips, the climb goes straight to the power cycle. No reader
    is stopped, no chip removed and no per-chip SBR runs, even with no SBR this hold and every off chip's
    node present with its link down."""
    order, _events, counters = _rig(monkeypatch, tmp_path, held_min=3)
    gone = {str(i) for i in range(32 - off, 32)}
    beats = {str(i): 100 for i in range(32 - off)}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(beats))
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: off)
    for c in gone:
        node = tmp_path / "pci" / srv.device_pci_map[c]
        node.mkdir()
        (node / "current_link_speed").write_text("Unknown\n")
    _dead_link(monkeypatch, tmp_path, order, None)
    hold_since = srv.device_hold_episode_since
    srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", gone, gone, rc=1)
    await srv.galaxy_recovery._climb_to_host_recovery_after_failed_reset(32, off, lambda m: None)
    assert "readers stopped" not in order and not any(o.startswith(("isolate", "sbr")) for o in order)
    assert order[-1] == "power cycle"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


# --- a mesh reset that dropped a chip: the SBR runs before the power cycle ---------------------


def _regressed(monkeypatch, tmp_path, *, heals=False, verdicts=(), how="link"):
    """This hold's mesh reset found the bus whole and left chip 31 off it (off before [], after [31]):
    a regressed reset (I31), so the climb skips the mesh-reset ladder and goes to the host rung, whose
    last-chance sweep runs first. Chip 31's node is still present."""
    order, events, counters = _rig(
        monkeypatch, tmp_path, held_min=3, enumerated=True, sbr_heals=heals, verdicts=list(verdicts)
    )
    killed = _dead_link(monkeypatch, tmp_path, order, how)
    hold_since = srv.device_hold_episode_since
    srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", set(), {GONE}, rc=1)
    srv.recovery_mechanism.note_reset_regressed(hold_since)
    return order, events, counters, killed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heals, verdict, how",
    [(True, True, "link"), (True, False, "link"), (False, False, "link"), (False, False, "all-ones")],
)
async def test_a_regressed_reset_gets_an_sbr_before_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state, heals, verdict, how
):
    """Off before [] and after [31]: the forced run climbs past the regressed reset, and its sweep
    removes the chip (readers stopped first) and SBRs it before any power cycle, even though nothing was
    isolated before. Only a just-passed full check with fabric lifts; otherwise the power cycle."""
    order, events, counters, killed = _regressed(monkeypatch, tmp_path, heals=heals, verdicts=[verdict], how=how)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert "galaxy_reset_skipped_regressed" in events
    i = order.index("readers stopped")
    assert order[i : i + 3] == ["readers stopped", f"isolate {GONE}", f"sbr ['{GONE}']"]
    assert killed == [(4242, signal.SIGKILL)]
    assert order.count(f"sbr ['{GONE}']") == 1, "one SBR per chip per hold"
    if heals:
        assert order[i + 3] == "full check", "the bus is whole: the full check runs before any mesh reset"
    if heals and verdict:
        assert "power cycle" not in order and "mesh reset" not in order
        assert counters["dirty_cleared"] == 1, "lifted once, on the just-passed full check with fabric"
    else:
        assert order[-1] == "power cycle", "a failed check after the SBR keeps the hold and escalates"
        assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["link", "all-ones"])
async def test_the_sweep_sbrs_a_dead_link_chip_with_nothing_isolated_before(
    monkeypatch, tmp_path, clear_job_state, how
):
    order, events, counters = _rig(monkeypatch, tmp_path, held_min=3, enumerated=True, sbr_heals=True, verdicts=[True])
    _dead_link(monkeypatch, tmp_path, order, how)
    assert not srv.isolated_chips
    assert await srv.galaxy_recovery._settle_and_verify_before_host_rung(32, lambda m: None, "idle-escalation") is True
    assert order[:3] == ["readers stopped", f"isolate {GONE}", f"sbr ['{GONE}']"]
    # The all-ones rig keeps the chip's beat all-ones after the SBR, so the bus never reads whole and the
    # sweep's mesh reset still runs before the one full check.
    assert order[3:] == (["full check"] if how == "link" else ["mesh reset", "full check"])
    assert counters["dirty_cleared"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("heals, verdict", [(True, True), (True, False), (False, False)])
async def test_a_chip_the_valve_isolates_during_the_settle_is_sbrd_before_the_host_rung(
    monkeypatch, tmp_path, clear_job_state, heals, verdict
):
    """The sweep sees chip 31 off the bus with its link up (nothing to remove or SBR) and fires its mesh
    reset. During the settle the dead-chip valve isolates the chip. The full check fails; before the host
    rung the chip, never SBR'd this hold, gets the kept rungs and the full check with fabric."""
    order, events, counters, _killed = _regressed(
        monkeypatch, tmp_path, heals=heals, verdicts=[False, verdict], how=None
    )

    async def mesh_then_valve(argv, log, *a, **k):
        order.append("mesh reset")
        srv.isolated_chips.add(GONE)  # the dead-chip valve, during the sweep's settle
        return 1, ""

    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", mesh_then_valve)
    monkeypatch.setattr(galaxy, "TRAY_DOWN_RESCAN_SETTLE_SEC", 0)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order[:2] == ["mesh reset", "full check"], "the sweep: no SBR target yet"
    assert order[2:4] == ["rescan", f"sbr ['{GONE}']"], "the SBR runs before the host rung"
    assert "host_rung_sbr_owed" in events
    if heals and verdict:
        assert order[4:] == ["full check"] and counters["dirty_cleared"] == 1
    else:
        # A chip still off after the SBR gets no check (nothing could pass); a failed one keeps the hold.
        assert order[4:] == (["full check"] if heals else []) + ["power cycle"]
        assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["A", "C"])
async def test_a_whole_tray_or_all_off_skips_the_sbr_and_power_cycles(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays, case
):
    """Class A (a whole tray off) and class C (every chip off) go straight to the power cycle after a
    regressed reset: no reader stopped, no chip removed, no SBR, no sweep."""
    order, _events, counters, _killed = _regressed(monkeypatch, tmp_path, how=None)
    tray = sorted(galaxy_trays)[-1]
    gone = {str(c) for c in galaxy_trays[tray]} if case == "A" else {str(i) for i in range(32)}
    beats = {str(i): 100 for i in range(32) if str(i) not in gone}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(beats))
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: len(gone))
    for c in gone:
        node = tmp_path / "pci" / srv.device_pci_map[c]
        node.mkdir(exist_ok=True)
        (node / "current_link_speed").write_text("Unknown\n")
    if case == "C":
        # Class C is all-off after a failed mesh reset (an all-off bus gets one mesh reset first).
        srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", gone, gone, rc=1)
    await srv.galaxy_recovery._climb_to_host_recovery_after_failed_reset(32, len(gone), lambda m: None)
    assert "readers stopped" not in order and not any(o.startswith(("isolate", "sbr", "rescan")) for o in order)
    assert order == ["power cycle"]
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_after_the_sweep_sbr_a_fabric_check_short_of_the_cabled_links_power_cycles(
    monkeypatch, tmp_path, clear_job_state, topo  # noqa: F811
):
    """The SBR brings chip 31 back, but the validator finds a port connection the cabling expects
    missing: the check fails, the hold stands and the climb goes on to the power cycle."""
    m, state = topo
    state["first"] = _LOST
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", raising=False)
    order, events, counters, _killed = _regressed(monkeypatch, tmp_path, heals=True)

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check")
        ok, detail = await m._verify_fabric_health_body(60)
        return ok, {"snapshot": {"ok": True}, "fabric": {"ok": ok, "detail": detail}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert f"sbr ['{GONE}']" in order and order[-1] == "power cycle"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "short of the cabling is a fail"


# --- spec 04 I97: one mesh reset of the whole mesh before the power cycle ----------------------

NOT_READY = {"0", "1", "2", "3"}
MESH_COST = galaxy.MESH_RESET_BEFORE_HOST_RUNG_SEC + galaxy.MESH_RESET_BEFORE_HOST_RUNG_SETTLE_SEC
GAVE_UP = galaxy.MESH_RESET_BEFORE_HOST_RUNG_GAVE_UP_SEC
CHECK = galaxy.MESH_RESET_BEFORE_HOST_RUNG_CHECK_SEC
NEEDS = max(GAVE_UP, MESH_COST + CHECK)  # a reset that gives up, or one in budget + the check's allowance


def _clock(monkeypatch):
    """galaxy's clock, moved on by ``shift`` seconds: the hold's age as the broker reads it, without
    touching the episode's stamp (its key for the once-per-episode marks)."""

    class Clock(datetime):
        shift = 0.0

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=cls.shift)

    monkeypatch.setattr(galaxy, "datetime", Clock)
    return Clock


def _b1_sweep(monkeypatch, tmp_path, *, verdicts, mesh_heals=False, held_sec=120):
    """Class B1 after a mesh reset whose tt-smi gave up at its driver wait: chip 31 is off the bus (node
    present, link down) and that reset failed on exactly it, so the climb skips the sweep's mesh reset
    and runs the SBR this hold owes. The SBR brings chip 31 back, but the chips tt-smi found never got
    POST_RESET: chips 0-3 read ARC-not-ready (missing from the heartbeat read, ENODATA on a bound
    endpoint whose config reads back, as :func:`arc_not_ready_chips` reads them). ``mesh_heals``: a mesh
    reset after the SBR brings them up. Returns (order, events, counters, reasons): ``events`` holds
    (name, fields), ``reasons`` each power cycle's reason."""
    order, _names, counters = _rig(
        monkeypatch, tmp_path, held_min=held_sec / 60, enumerated=True, sbr_heals=True, verdicts=verdicts
    )
    _dead_link(monkeypatch, tmp_path, order, "link")
    read, sbr = srv.read_heartbeats, f"sbr ['{GONE}']"

    def beats():
        b = read()
        if sbr in order and not (mesh_heals and "mesh reset" in order[order.index(sbr) :]):
            b = {c: v for c, v in b.items() if c not in NOT_READY}
        return b

    monkeypatch.setattr(srv, "read_heartbeats", beats)
    monkeypatch.setattr(
        heartbeat, "enodata_presence", lambda b: {c: hostpci.CONFIG_PRESENT for c in NOT_READY - set(b)}
    )
    # No real settle here: the reset's budget takes the settle's seconds, so the bound still counts 70 s.
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_SEC", MESH_COST)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_SETTLE_SEC", 0)
    events, reasons = [], []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append((name, k)))

    async def power_cycle(log, reason):
        order.append("power cycle")
        reasons.append(reason)

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    return order, events, counters, reasons


def _i97(events) -> list:
    return [k for n, k in events if n == "whole_mesh_reset_before_host_rung"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, False], ids=["check-passes", "check-fails"])
async def test_an_sbr_that_has_every_chip_back_on_arc_not_ready_chips_gets_one_mesh_reset_first(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """The SBR brings chip 31 back and the full check fails on the chips left ARC-not-ready. One mesh
    reset of the now-whole mesh and the full check with fabric run before the power cycle: only that
    check passing lifts the hold. A failed one requests the power cycle in the same pass, its reason read
    from the bus as it is now."""
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, verdict], mesh_heals=verdict)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    tail = [sbr, "full check", "mesh reset", "full check"]
    assert order[order.index(sbr) :] == tail + ([] if verdict else ["power cycle"])
    assert order.count("mesh reset") == 1, "the failed first reset is not repeated before the SBR"
    fired = _i97(events)
    assert len(fired) == 1 and fired[0]["fired"] is True and fired[0]["arc_not_ready"] == sorted(NOT_READY, key=int)
    if verdict:
        assert counters["dirty_cleared"] == 1 and not reasons, "lifted once, on the just-passed full check"
    else:
        assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
        assert "whole_mesh_reset_before_host_rung_unverified" in [n for n, _ in events]
        assert len(reasons) == 1 and "0/32 chip(s) off the bus, 4 ARC not ready" in reasons[0]


@pytest.mark.asyncio
async def test_a_refused_repeated_mesh_reset_holds_with_no_power_cycle(monkeypatch, tmp_path, clear_job_state):
    """The outage hold (I28) starts after the sweep's SBR and refuses the repeated mesh reset: the full
    ladder did not run, so nothing lifts, no power cycle is requested on it (I22) and the sweep is not
    spent (the second verdict, a pass, is never read)."""
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, True])
    calls = []

    def refuse(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            return False  # the sweep's SBR
        order.append("refused")
        return True

    monkeypatch.setattr(galaxy, "refuse_mesh_reset", refuse)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check", "refused"]
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    assert not reasons and "host_rung_held" in [n for n, _ in events]
    spent = srv.recovery_mechanism.sweep_spent
    assert spent is None or spent[0] != srv.device_hold_episode_since, "the sweep is not spent: the next pass runs it"


@pytest.mark.asyncio
async def test_an_outage_hold_refuses_the_sweep_and_holds_for_the_operator(monkeypatch, tmp_path, clear_job_state):
    """The outage hold (I28) refuses the whole sweep, as it does for every sweep: the full ladder did not
    run, so nothing lifts and no power cycle is requested on it (I22); the device stays held for the
    operator (the second verdict, a pass, is never read)."""
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, True])
    monkeypatch.setattr(galaxy, "refuse_mesh_reset", lambda *a, **k: order.append("refused") or True)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order[order.index("readers stopped") :] == ["readers stopped", f"isolate {GONE}", "refused", "full check"]
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    assert not reasons and "host_rung_held" in [n for n, _ in events]


@pytest.mark.asyncio
async def test_at_the_bound_the_sweep_reruns_a_spent_sbr_and_the_failed_mesh_reset_before_the_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """A replayed partial drop at the 10-min idle escalation: the chip's SBR already failed this hold
    (endpoint not re-enumerated) and the mesh reset failed on it. Right before the power cycle the
    sweep still stops the readers, isolates the chip, writes its SBR, repeats the mesh reset and runs
    the full check with fabric; only that check's failure requests the cycle (spec 04 I58, I98)."""
    order, events, counters = _rig(monkeypatch, tmp_path, enumerated=True)
    _dead_link(monkeypatch, tmp_path, order, "link")
    hold_since = srv.device_hold_episode_since
    srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    srv.galaxy_recovery._sbr_hold = (hold_since, frozenset({GONE}))
    await _bound_ladder()
    sbr = f"sbr ['{GONE}']"
    sweep = order[order.index("readers stopped") :]
    assert sweep[:3] == ["readers stopped", f"isolate {GONE}", sbr], "the spent SBR is written again"
    assert sweep[-3:] == ["mesh reset", "full check", "power cycle"]
    assert "sbr_skipped_spent" not in events[events.index("last_chance_reset_sweep") :]
    assert "reset_rung_skipped_failed_unchanged" not in events
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed check keeps the hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("margin", [-5, 5], ids=["ends-before-the-deadline", "would-end-past-it"])
async def test_the_repeated_mesh_reset_runs_at_once_under_the_deadline_or_one_from_its_start(
    monkeypatch, tmp_path, clear_job_state, margin
):
    """The deadline is a hold age: the 10-min bound (I76) less the kill grace and the reap (585 s). A
    reset that gives up at its driver wait (300 s), or one in its 60 s budget with the 10 s settle and
    the check's 180 s allowance, ends before it when it starts 300 s before it. The sweep's repeated
    mesh reset never waits for the bound: when it starts later, its deadline is its start plus those
    300 s, and its full check with fabric still decides between the lift and the power cycle."""
    deadline = galaxy._mesh_reset_before_host_rung_deadline_sec()
    assert MESH_COST == 70 and GAVE_UP == 300 and CHECK == 180 and NEEDS == 300 and deadline == 585
    order, events, counters, reasons = _b1_sweep(
        monkeypatch, tmp_path, verdicts=[False, False], held_sec=deadline - NEEDS + margin
    )
    assert galaxy._stuck_hold_ceiling_sec() == 600
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check", "mesh reset", "full check", "power cycle"]
    fired = _i97(events)
    assert [k["fired"] for k in fired] == [True] and fired[0]["repeat"] is True
    assert fired[0]["deadline_sec"] == max(deadline, round(fired[0]["hold_age_sec"] + NEEDS, 1))
    assert "last_chance_sweep_waits_for_bound" not in [n for n, _ in events]
    assert counters["dirty_cleared"] == 0 and len(reasons) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [False, True], ids=["i97-reset", "sweep-repeat"])
async def test_the_i97_reset_keeps_its_deadline_and_a_refusal_power_cycles_but_the_repeat_holds(
    monkeypatch, tmp_path, clear_job_state, repeat
):
    """I97's own reset, past the latest start its deadline allows, does not run, and a refused one
    returns False: the caller power-cycles. The sweep's repeat at that age runs under a deadline from its
    start, and its failed check returns False; refused, it returns None: it did not run, so no power
    cycle. Nothing lifts on either."""
    deadline = galaxy._mesh_reset_before_host_rung_deadline_sec()
    order, events, counters, _reasons = _b1_sweep(
        monkeypatch, tmp_path, verdicts=[False], held_sec=deadline - NEEDS + 5
    )
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(ALL))
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 0)
    run, failed_hb = srv.galaxy_recovery._whole_mesh_reset_before_host_rung, {"heartbeat": {"verdict": "unhealthy"}}
    assert await run(32, lambda m: None, "idle-escalation", failed_hb, repeat=repeat) is False
    if repeat:
        assert order[-2:] == ["mesh reset", "full check"]
    else:
        assert "mesh reset" not in order and "would not end" in _i97(events)[-1]["reason"]
    srv.device_hold_episode_since = (datetime.now() - timedelta(seconds=60)).isoformat()  # a new episode
    monkeypatch.setattr(galaxy, "refuse_mesh_reset", lambda *a, **k: True)
    resets = order.count("mesh reset")
    assert await run(32, lambda m: None, "idle-escalation", failed_hb, repeat=repeat) is (None if repeat else False)
    assert order.count("mesh reset") == resets, "a refused reset never launches"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_sec, gives_up", [(60, False), (GAVE_UP, True)], ids=["in-budget", "gives-up-again"])
async def test_a_deadline_crossed_during_the_sweep_sbr_still_runs_the_repeated_mesh_reset(
    monkeypatch, tmp_path, clear_job_state, reset_sec, gives_up
):
    """The sweep starts 5 s before the latest hold age the I97 deadline allows the repeat, and its SBR
    takes 20 s. The repeated mesh reset still runs, with a deadline from its own start: a skipped repeat
    is never read as one that ran and failed, so no power cycle is requested without it. Its full check
    runs when the reset left the check's allowance; nothing lifts on a failed check, and the power cycle
    is requested within the repeat's start plus the reset's and check's 300 s, the kill grace and the
    reap."""
    deadline = galaxy._mesh_reset_before_host_rung_deadline_sec()
    order, events, counters, reasons = _b1_sweep(
        monkeypatch, tmp_path, verdicts=[False, False], held_sec=deadline - NEEDS - 5
    )
    clock, cycled_at = _clock(monkeypatch), []
    mech, gr = srv.recovery_mechanism, srv.galaxy_recovery
    real_sbr = gr._sbr

    async def slow_sbr(log, **k):
        clock.shift += 20
        return await real_sbr(log, **k)

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        clock.shift += reset_sec
        monkeypatch.setattr(mech, "last_reset_driver_wait_failed", gives_up)
        monkeypatch.setattr(mech, "last_reset_rc", 1)
        return 1, ""

    async def power_cycle(log, reason):
        order.append("power cycle")
        cycled_at.append(galaxy._hold_age_sec(srv.device_hold_episode_since))

    monkeypatch.setattr(gr, "_sbr", slow_sbr)
    monkeypatch.setattr(mech, "reset_with_quiesce", mesh)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    await gr.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    checked = [] if gives_up else ["full check"]
    assert order[order.index(sbr) :] == [sbr, "full check", "mesh reset"] + checked + ["power cycle"]
    fired = _i97(events)
    assert [k["fired"] for k in fired] == [True] and fired[0]["repeat"] is True
    start = fired[0]["hold_age_sec"]
    assert start > deadline - NEEDS, "the SBR carried the hold past the latest start the I97 deadline allows"
    allowance = NEEDS + galaxy.MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC + galaxy.MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC
    assert len(cycled_at) == 1 and cycled_at[0] <= start + allowance, f"power cycle requested at {cycled_at[0]:.0f}s"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed check keeps the hold"


@pytest.mark.asyncio
async def test_a_tenant_that_arrives_before_the_repeated_mesh_reset_holds_with_no_power_cycle(
    monkeypatch, tmp_path, clear_job_state
):
    """A tenant takes the device after the sweep's SBR, before its repeated mesh reset: that reset does
    not run over it, so the full ladder did not run. The hold stays, no power cycle is requested this
    pass (I22), nothing lifts, and the sweep is not spent: the next pass runs it in full."""
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, True])
    gr, tenant = srv.galaxy_recovery, {"on": False}
    real_sbr = gr._sbr

    async def sbr_then_tenant(log, **k):
        healed = await real_sbr(log, **k)
        tenant["on"] = True
        return healed

    monkeypatch.setattr(gr, "_sbr", sbr_then_tenant)
    monkeypatch.setattr(gr, "_tenant_active", lambda scan, **k: tenant["on"])
    await gr.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check"], "no mesh reset over the tenant, no power cycle"
    assert not reasons and counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    held = [k for n, k in events if n == "host_rung_held"]
    assert held and held[-1]["reason"] == "repeated mesh reset did not run"
    assert [k["fired"] for k in _i97(events)] == [False]
    spent = srv.recovery_mechanism.sweep_spent
    assert spent is None or spent[0] != srv.device_hold_episode_since, "the sweep is not spent: the next pass runs it"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, False], ids=["check-passes", "check-fails"])
async def test_the_repeated_mesh_reset_runs_after_this_holds_i97_reset_already_ran(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """The I97 mesh reset already ran in this hold. The sweep's repeated mesh reset is not that reset:
    its own phase record (I87) spends it, so it still runs before the power cycle, readers stopped, and
    its full check with fabric decides. Only a pass lifts the hold, once; a fail requests the power cycle."""
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, verdict], mesh_heals=verdict)
    gr = srv.galaxy_recovery
    gr._whole_mesh_reset_spent = srv.device_hold_episode_since
    await gr.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check", "mesh reset", "full check"] + (
        [] if verdict else ["power cycle"]
    )
    assert [(k["fired"], k["repeat"]) for k in _i97(events)] == [(True, True)]
    if verdict:
        assert counters["dirty_cleared"] == 1 and not reasons, "lifted once, on the just-passed full check"
    else:
        assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0 and len(reasons) == 1


def _failed_in_an_earlier_hold():
    """Re-key the failed mesh reset on chip 31 to an earlier hold of this boot (or one before a broker
    restart): this hold's one mesh reset is not spent (I87), so the gate meets I58, not the spent rule."""
    mech = srv.recovery_mechanism
    earlier = (datetime.now() - timedelta(hours=1)).isoformat()
    for entry in mech._reset_record()["failed"]:
        entry["episode"] = earlier
    mech.mesh_reset_failed_since_release = False
    assert not mech.mesh_reset_spent(srv.device_hold_episode_since)
    assert mech.rung_failed_unchanged("", "mesh_reset", {GONE})


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["check-passes", "check-fails", "tenant-before-the-repeat"])
async def test_the_gates_i58_skip_reaches_the_power_cycle_only_through_the_sweep(
    monkeypatch, tmp_path, clear_job_state, case
):
    """The gate's mesh reset meets a reset that already failed on exactly chip 31 this boot (I58): it does
    not fire it and reports the rung failed, so the router names the host rung. That rung's last-chance
    sweep stops the readers, isolates the dead link, SBRs chip 31 and repeats the mesh reset under the
    I97 deadline, then runs the full check with fabric. Only a pass lifts the hold, once; a fail requests
    the power cycle within the repeat's start plus the reset's and check's 300 s, the kill grace and the
    reap. A repeat that did not run (a tenant arrived after the SBR) holds with no power cycle."""
    passes = case == "check-passes"
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, passes], mesh_heals=passes)
    _failed_in_an_earlier_hold()
    clock, cycled_at = _clock(monkeypatch), []
    mech, gr, tenant = srv.recovery_mechanism, srv.galaxy_recovery, {"on": False}
    real_sbr = gr._sbr

    async def sbr(log, **k):
        healed = await real_sbr(log, **k)
        tenant["on"] = case == "tenant-before-the-repeat"
        return healed

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        clock.shift += 60
        monkeypatch.setattr(mech, "last_reset_rc", 0 if passes else 1)
        return mech.last_reset_rc, ""

    async def power_cycle(log, reason):
        order.append("power cycle")
        cycled_at.append(galaxy._hold_age_sec(srv.device_hold_episode_since))
        reasons.append(reason)

    monkeypatch.setattr(gr, "_sbr", sbr)
    monkeypatch.setattr(gr, "_tenant_active", lambda scan, **k: tenant["on"])
    monkeypatch.setattr(mech, "reset_with_quiesce", mesh)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)

    gate = await gr.escalate("gate/post-job", [GONE], 32, lambda m: None, stage=galaxy.STAGE_SMI_RESET)
    assert gate == OUTCOME_WAITING and "reset_rung_skipped_failed_unchanged" in [n for n, _ in events]
    assert "mesh reset" not in order and "power cycle" not in order, "I58 skips the gate's reset, nothing more"

    out = await gr.escalate("gate/post-job", [GONE], 32, lambda m: None, stage=galaxy.STAGE_POWER_CYCLE)
    sbr_step = f"sbr ['{GONE}']"
    assert order[: order.index(sbr_step)] == ["readers stopped", f"isolate {GONE}"]
    tail = order[order.index(sbr_step) :]
    if case == "tenant-before-the-repeat":
        assert out == OUTCOME_WAITING and tail == [sbr_step, "full check"], "no reset over the tenant, no cycle"
        assert not reasons and counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
        assert [k["reason"] for n, k in events if n == "host_rung_held"] == ["repeated mesh reset did not run"]
        spent = mech.sweep_spent
        assert spent is None or spent[0] != srv.device_hold_episode_since, "the next pass runs the sweep in full"
        return
    assert tail == [sbr_step, "full check", "mesh reset", "full check"] + ([] if passes else ["power cycle"])
    fired = _i97(events)
    assert [(k["fired"], k["repeat"]) for k in fired] == [(True, True)]
    if passes:
        assert out == OUTCOME_RECOVERED and counters["dirty_cleared"] == 1 and not reasons
        return
    assert out == OUTCOME_WAITING and counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    allowance = NEEDS + galaxy.MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC + galaxy.MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC
    start = fired[0]["hold_age_sec"]
    assert len(cycled_at) == 1 and cycled_at[0] <= start + allowance, f"power cycle requested at {cycled_at[0]:.0f}s"
    assert cycled_at[0] <= galaxy._stuck_hold_ceiling_sec(), "requested by the 10-min bound"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reset_sec, gives_up, checked",
    [(35, False, True), (150, False, False), (315, False, False), (GAVE_UP, True, False)],
    ids=["in-budget", "runs-long", "runs-past-the-deadline", "gives-up-again"],
)
async def test_the_power_cycle_after_the_mesh_reset_before_the_host_rung_is_requested_by_the_bound(
    monkeypatch, tmp_path, clear_job_state, reset_sec, gives_up, checked
):
    """The reset starts at the latest hold age the deadline allows. In its budget, the full check runs
    and its failure requests the power cycle. A reset that runs long leaves less than the check's
    allowance before the deadline, one that runs past the deadline leaves none (a reset is never
    killed mid-run, I4), and one whose tt-smi gave up at its driver wait again left the chips
    ARC-not-ready: no check, the hold stays, the power cycle at once. Either way the power cycle is
    requested by the 600 s bound, never after it."""
    ceiling = galaxy._stuck_hold_ceiling_sec()
    deadline = galaxy._mesh_reset_before_host_rung_deadline_sec()
    order, events, counters, reasons = _b1_sweep(
        monkeypatch, tmp_path, verdicts=[False, False], held_sec=deadline - NEEDS - 10
    )
    clock, cycled_at = _clock(monkeypatch), []
    mech = srv.recovery_mechanism

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        clock.shift += reset_sec
        monkeypatch.setattr(mech, "last_reset_driver_wait_failed", gives_up)
        monkeypatch.setattr(mech, "last_reset_rc", 1 if gives_up else 0)
        return mech.last_reset_rc, ""

    async def power_cycle(log, reason):
        order.append("power cycle")
        cycled_at.append(galaxy._hold_age_sec(srv.device_hold_episode_since))

    monkeypatch.setattr(mech, "reset_with_quiesce", mesh)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check", "mesh reset"] + (["full check"] if checked else []) + [
        "power cycle"
    ]
    assert [k["fired"] for k in _i97(events)] == [True]
    assert len(cycled_at) == 1 and cycled_at[0] <= ceiling, f"power cycle requested at {cycled_at[0]:.0f}s"
    assert counters["dirty_cleared"] == 0
    if not checked:
        skipped = [k for n, k in events if n == "whole_mesh_reset_before_host_rung_unverified"]
        assert len(skipped) == 1 and ("gave up" in skipped[0]["reason"]) is gives_up
        assert not pcie_guard._INTENT_OPEN, "the reset's off-bus intent ends before the power cycle"


# A fabric pass that outlives the deadline and ignores SIGTERM, then passes: only the deadline stops it.
_SLOW_FABRIC = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(6)"


@pytest.mark.asyncio
async def test_a_full_check_still_running_at_the_deadline_fails_and_power_cycles(
    monkeypatch, tmp_path, clear_job_state, signal_guard
):
    """The check after the mesh reset is slow in its fabric phase and would pass after the deadline.
    At the deadline it counts as failed: its reader gets SIGTERM, then SIGKILL after the grace, the hold
    stays (no lift, ever, on a late pass) and the power cycle is requested by deadline + grace + reap,
    within the 10-min bound. Real seconds, scaled down: the deadline is a wait on the event loop."""
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, True], mesh_heals=True)
    monkeypatch.setattr(os, "killpg", signal_guard.killpg)  # the rig's stub signals nothing; this reader is ours
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_CHECK_SEC", 1)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_TERM_GRACE_SEC", 0.6)
    monkeypatch.setattr(galaxy, "MESH_RESET_BEFORE_HOST_RUNG_REAP_SEC", 0.4)
    ceiling = galaxy._stuck_hold_ceiling_sec()
    deadline = galaxy._mesh_reset_before_host_rung_deadline_sec()
    clock, cycled_at, readers, started, shifted = _clock(monkeypatch), [], [], [], []
    mech, m = srv.recovery_mechanism, srv.galaxy_recovery.monitor
    left = 1.5

    async def mesh(argv, log, *a, **k):
        order.append("mesh reset")
        # The deadline is `left` seconds after this shift, so measure from here: a check that starts
        # later (a loaded host) has less than `left` to run, and that is not the deadline's error.
        shifted.append(time.monotonic())
        clock.shift += deadline - left - galaxy._hold_age_sec(srv.device_hold_episode_since)
        return 0, ""

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check")
        if "mesh reset" not in order:
            return False, {"snapshot": {"ok": False}, "fabric": {"ok": False}}
        started.append(time.monotonic())
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", _SLOW_FABRIC, start_new_session=True)
        readers.append(proc)
        m.fabric_check_proc = proc
        try:
            ok = await proc.wait() == 0
        finally:
            m.fabric_check_proc = None
        return ok, {"snapshot": {"ok": True}, "fabric": {"ok": ok}}

    async def power_cycle(log, reason):
        order.append("power cycle")
        cycled_at.append((galaxy._hold_age_sec(srv.device_hold_episode_since), time.monotonic()))

    monkeypatch.setattr(mech, "reset_with_quiesce", mesh)
    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check", "mesh reset", "full check", "power cycle"]
    assert (
        counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0
    ), "a check cut at the deadline lifts nothing"
    took = cycled_at[0][1] - shifted[0]
    rc = await asyncio.wait_for(readers[0].wait(), 3)
    assert rc == -signal.SIGKILL, "the reader that ignored SIGTERM got SIGKILL after the grace"
    assert left + 0.6 <= took <= left + 0.6 + 0.4 + 0.5, f"power cycle {took:.2f}s after the clock shift"
    assert cycled_at[0][0] <= ceiling + 0.5, f"power cycle requested at {cycled_at[0][0]:.1f}s of hold"
    cut = [k for n, k in events if n == "whole_mesh_reset_before_host_rung_unverified"]
    assert len(cut) == 1 and cut[0]["reason"] == "deadline" and cut[0]["stopped_pids"] == [readers[0].pid]
    assert not pcie_guard._INTENT_OPEN, "the reset's off-bus intent ends before the power cycle"


@pytest.mark.asyncio
async def test_a_fabric_check_short_of_the_cabled_links_after_the_mesh_reset_before_the_host_rung_power_cycles(
    monkeypatch, tmp_path, clear_job_state, topo  # noqa: F811
):
    """The mesh reset heals the chips, but the full check's fabric pass finds a port connection the
    cabling expects missing: fewer links than cabled is a fail, never a pass on the links it saw. The
    hold stays and the power cycle follows in the same pass."""
    m, state = topo
    state["first"] = _LOST
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", raising=False)
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, True], mesh_heals=True)
    fabric = []

    async def verify(expected, log, run_fabric=True, **k):
        order.append("full check")
        if "mesh reset" not in order:
            return False, {"snapshot": {"ok": False}, "fabric": {"ok": False}}
        ok, detail = await m._verify_fabric_health_body(60)
        fabric.append(ok)
        return ok, {"snapshot": {"ok": True}, "fabric": {"ok": ok, "detail": detail}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check", "mesh reset", "full check", "power cycle"]
    assert fabric == [False], "the fabric pass short of the cabling failed"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "short of the cabling lifts nothing"
    assert "whole_mesh_reset_before_host_rung_unverified" in [n for n, _ in events] and len(reasons) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True], ids=["next-pass", "the-bound-pass"])
async def test_a_refused_lift_after_the_mesh_reset_before_the_host_rung_still_power_cycles_by_the_bound(
    monkeypatch, tmp_path, clear_job_state, bound
):
    """The mesh reset heals the chips and the full check passes, but the release funnel refuses the
    lift (a fabric pass still owed, say): the hold stays, and no power cycle comes on a check that
    passed. The next pass, before the bound or the bound's own, runs no third mesh reset and requests
    the power cycle by the bound."""
    order, events, counters, reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, True], mesh_heals=True)
    lifts = []
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **kw: lifts.append(kw["why"]) or False)
    clock, cycled_at = _clock(monkeypatch), []
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    sbr = f"sbr ['{GONE}']"
    assert order[order.index(sbr) :] == [sbr, "full check", "mesh reset", "full check"], "no power cycle yet"
    assert [k["fired"] for k in _i97(events)] == [True]
    assert "host_rung_sweep_unverified" in [n for n, _ in events]
    assert len(lifts) == 1 and "mesh reset of the whole mesh" in lifts[0], "the lift was asked once, and refused"
    assert counters["dirty_cleared"] == 0 and not reasons

    async def power_cycle(log, reason):
        order.append("power cycle")
        cycled_at.append(galaxy._hold_age_sec(srv.device_hold_episode_since))

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    ceiling = galaxy._stuck_hold_ceiling_sec()
    age = galaxy._hold_age_sec(srv.device_hold_episode_since)
    clock.shift = (ceiling if bound else ceiling - 60) - age
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None, bound=bound)
    assert order.count("mesh reset") <= 2, "no third mesh reset in the hold"
    assert len(_i97(events)) == 1 or _i97(events)[-1]["fired"] is False, "the I97 reset runs once per hold"
    assert order[-1] == "power cycle" and order.count("power cycle") == 1
    assert cycled_at[0] <= ceiling + 1, f"power cycle requested at {cycled_at[0]:.0f}s"
    assert len(lifts) == 1, "the next pass's failed check asks for no lift"


@pytest.mark.asyncio
async def test_the_mesh_reset_before_the_host_rung_runs_once_per_hold_episode(monkeypatch, tmp_path, clear_job_state):
    order, events, _counters, _reasons = _b1_sweep(monkeypatch, tmp_path, verdicts=[False, False])
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order.count("mesh reset") == 1
    # Another pass in the same episode (say a power cycle that never took the host down) runs none.
    run = srv.galaxy_recovery._whole_mesh_reset_before_host_rung
    assert await run(32, lambda m: None, "idle-escalation", {}) is False
    assert order.count("mesh reset") == 1 and _i97(events)[-1]["reason"] == "already ran in this hold"
    # A new episode gets its own.
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=1)).isoformat()
    assert await run(32, lambda m: None, "idle-escalation", {}) is False, "its full check fails: no lift"
    assert order.count("mesh reset") == 2 and order[-1] == "full check"


@pytest.mark.asyncio
async def test_a_whole_tray_off_with_arc_not_ready_chips_still_power_cycles_with_no_reset(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """Class A is unchanged (class C has no chip left on the bus: the A/C test above): a whole tray off
    goes straight to the power cycle, even with chips on the other trays ARC-not-ready. No SBR, no
    mesh reset of any kind."""
    order, events, counters, _killed = _regressed(monkeypatch, tmp_path, how=None)
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))
    tray = sorted(galaxy_trays)[-1]
    gone = {str(c) for c in galaxy_trays[tray]}
    assert not gone & NOT_READY
    beats = {str(i): 100 for i in range(32) if str(i) not in gone | NOT_READY}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(beats))
    monkeypatch.setattr(
        heartbeat, "enodata_presence", lambda b: {c: hostpci.CONFIG_PRESENT for c in NOT_READY - set(b)}
    )
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: len(gone))
    for c in gone:
        node = tmp_path / "pci" / srv.device_pci_map[c]
        node.mkdir(exist_ok=True)
        (node / "current_link_speed").write_text("Unknown\n")
    await srv.galaxy_recovery._climb_to_host_recovery_after_failed_reset(32, len(gone), lambda m: None)
    assert order == ["power cycle"]
    assert "whole_mesh_reset_before_host_rung" not in events
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_the_power_cycle_reason_reads_the_bus_as_it_is_now(monkeypatch, clear_job_state):
    beats = {str(i): 100 for i in range(31) if str(i) not in NOT_READY}  # chip 31 off, chips 0-3 not ready
    monkeypatch.setattr(srv, "read_heartbeats", lambda: dict(beats))
    monkeypatch.setattr(
        heartbeat, "enodata_presence", lambda b: {c: hostpci.CONFIG_PRESENT for c in NOT_READY - set(b)}
    )
    cause = await srv.galaxy_recovery._host_rung_cause_now(32, "idle escalation", "fallback")
    assert cause == "idle escalation: 1/32 chip(s) off the bus, 4 ARC not ready after the reset ladder"

    def unreadable():
        raise OSError("sysfs gone")

    monkeypatch.setattr(srv, "read_heartbeats", unreadable)
    assert await srv.galaxy_recovery._host_rung_cause_now(32, "idle escalation", "fallback") == "fallback"
