# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I89: the kept rungs of a below-floor off-bus hold.

At the gate the hold gets one PCI rescan at once, and an SBR only for the chips the defaults allow
(an isolated chip, or a chip gone from the bus when TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET is set),
once per hold episode and off-bus set. Only a passing full check, fabric included, lifts it.

At the 10-min bound (I76) the ladder runs every kept rung in order: PCI rescan, SBR of every chip
gone from the bus whatever the opt-in, its one mesh reset, the full check with fabric, then the power
cycle. A chip still enumerated is on the bus: never SBR'd, the mesh reset covers it. Below the bound a
forced run keeps its own path (the mesh reset, no rescan or SBR: the gate ran those at once). No test
reaches a real reset or power cycle."""

from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold
from tests.test_fabric_topology_mismatch import _LOST, topo  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED, OUTCOME_TERMINAL, OUTCOME_WAITING, galaxy, pcie_guard

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
async def test_the_bound_runs_every_kept_rung_in_order_then_the_power_cycle(monkeypatch, tmp_path, clear_job_state):
    order, events, counters = _rig(monkeypatch, tmp_path)
    assert await _bound_ladder() == OUTCOME_TERMINAL
    assert order == ["rescan", "sbr ['31']", "mesh reset", "full check", "power cycle"]
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
    order, _events, _counters = _rig(monkeypatch, tmp_path)
    hold_since = srv.device_hold_episode_since
    srv.recovery_mechanism.note_rung_failed(hold_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    await _bound_ladder()
    assert order == ["rescan", "sbr ['31']", "power cycle"]


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
    order, _events, counters = _rig(monkeypatch, tmp_path, sbr_heals=True, verdicts=[True])
    await _bound_ladder()
    assert order == ["rescan", "sbr ['31']", "full check"], "the shortest path: no mesh reset, no power cycle"
    assert counters["dirty_cleared"] == 1, "lifted once, on the passing full check"


@pytest.mark.asyncio
async def test_an_enumerated_chip_is_never_sbrd_even_at_the_bound(monkeypatch, tmp_path, clear_job_state):
    order, _events, _counters = _rig(monkeypatch, tmp_path, enumerated=True)
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
    assert order == ["rescan", "sbr ['31']", "power cycle"], "the bound adds the rescan and SBR, no second reset"


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
    assert order == ["rescan", "sbr ['31']", "mesh reset", "full check"], "every rung ran; the cycle waits"
    assert rm.power_cycle_retry is not None and not rm.power_cycle_retry_due(until - 1)
    assert rm.power_cycle_retry_due(until), "asked again the moment the cooldown ends"
    state["cooling"] = False
    order.clear()
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_bound_tenant_blocker", lambda *a: None)
    await srv._hold_bound_host_rung(601, lambda m: None)
    assert order == ["power cycle"], "the cooldown's end goes straight to the power cycle, no second ladder"


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
