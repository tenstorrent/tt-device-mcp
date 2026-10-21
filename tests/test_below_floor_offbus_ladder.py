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
then the power cycle. A chip still enumerated is on the bus: never SBR'd, the mesh reset covers it.
Below the bound a forced run keeps its own path (the mesh reset, no rescan or SBR: the gate ran those at
once). No test reaches a real reset, setpci or power cycle."""

import asyncio
import pathlib
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
from tt_device_mcp.health.monitors import pci
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
    order, events = [], []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append(name))

    def rescan():
        order.append("rescan")
        if rescan_heals:
            bus["beats"] = dict(ALL)

    async def sbr(log):
        order.append(f"sbr {sorted(srv.isolated_chips, key=int)}")
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
        # No bridge for a gone chip without the opt-in: no SBR is tried.
        (False, ["rescan", "mesh reset", "full check", "power cycle"]),
        # Opted in, the stuck-hold ladder's own bridge rung SBRs it first; the bound does not repeat it.
        (True, ["sbr ['31']", "rescan", "mesh reset", "full check", "power cycle"]),
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
    the rescan and SBR again before the power cycle. The mesh reset that already failed on the same
    chips is not repeated (one per episode, I87; no sweep repeat of it, I22)."""
    for opt_in, expected_order in ((True, ["rescan", "sbr ['31']", "power cycle"]), (False, ["rescan", "power cycle"])):
        (tmp_path / str(opt_in)).mkdir()
        order, _events, _counters = _rig(monkeypatch, tmp_path / str(opt_in), opt_in=opt_in)
        srv.isolated_chips.clear()
        hold_since = srv.device_hold_episode_since
        srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", {GONE}, {GONE}, rc=1)
        await _bound_ladder()
        assert order == expected_order, f"opt_in={opt_in}"


@pytest.mark.asyncio
async def test_a_forced_run_below_the_bound_skips_a_mesh_reset_that_already_failed(
    monkeypatch, tmp_path, clear_job_state
):
    order, _events, _counters = _rig(monkeypatch, tmp_path)
    hold_since = srv.device_hold_episode_since
    srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert "mesh reset" not in order, "below the bound I58 still skips a known-failed reset"


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
    assert order == ["rescan", "mesh reset", "full check", "power cycle"]


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
    """I58, unchanged: every rung below the host pair has run once, so no ceiling wait."""
    order, events, _counters = _rig(monkeypatch, tmp_path, held_min=3)
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, lambda m: None)
    assert order == ["mesh reset", "full check", "power cycle"]
    assert "host_rung_not_deferred" in events
    order.clear()
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=10, seconds=5)).isoformat()
    await _bound_ladder()
    assert order == ["rescan", "power cycle"], "the bound adds the rescan (no bridge: no SBR), no second reset"


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
    # bound's own ladder then skips the refused rescan and ends in the power cycle too.
    assert order[0] == "power cycle", "the PCI-wedged latch requested the power cycle at once"
    assert order[1:] == ["mesh reset", "full check", "power cycle"], "the rescan never started a second write"
    assert srv.PCI_WRITER.latched, "the blocked remove latched PCI-wedged"
    assert "pci_rescan_timeout" not in events, "one PCI timeout per ladder: the refused rescan did not wait"


def _real_bridge_rung(monkeypatch, tmp_path):
    """Undo _rig's fake: the real _recover_isolated_chips and reset_chip_via_bridge run, against a fake
    sysfs where chip 31's bridge (0000:40:1f.0) is still enumerated with chip 31's bus as its secondary
    bus and chip 31's own node is gone. Returns ``spy``: the bridge lookups by secondary bus, the setpci
    argvs and the global rescans."""
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

    real_write = pathlib.Path.write_text

    def write_text(path, data, *a, **k):
        if str(path) == "/sys/bus/pci/rescan":
            spy["rescan"] += 1
            return len(data)
        return real_write(path, data, *a, **k)

    br = pci.PCI_DEVICES_DIR / "0000:40:1f.0"
    br.mkdir()
    (br / "secondary_bus_number").write_text(str(0x5F) + "\n")
    cls = tmp_path / "class"
    cls.mkdir()
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", cls)
    monkeypatch.setattr(bridge, "find_bridge_by_secondary_bus", find)
    monkeypatch.setattr(bridge.subprocess, "run", setpci)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(pathlib.Path, "write_text", write_text)
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
