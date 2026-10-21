# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I86: the last-chance sweep re-probes the bus after SBR and, once every expected chip is
back, runs the full check (fabric included) before any further rung. The mesh reset fires only when
that check fails, and the caller still climbs to the power cycle when the mesh never verifies. The
bus probe never lifts a hold by itself."""

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import galaxy, pcie_guard

# Chips 16-18 off the bus: part of one tray, never a whole one (a whole tray skips the sweep, I37).
PARTIAL = {"16", "17", "18"}


def _arm(monkeypatch, *, heals_after_sbr: bool, verdicts: list, isolated=PARTIAL):
    """A 32-chip mesh with ``isolated`` chips off the bus (fake sysfs: the off-bus count the probe
    reads). SBR brings them back when ``heals_after_sbr``; the mesh reset always does. ``verdicts``
    are the full checks' results in order. Returns (g, rec) where rec records the rungs, the full
    checks, the releases and the power cycles."""
    g = srv.galaxy_recovery
    bus = {"off": len(isolated), "isolated": set(isolated)}
    rec = {"rungs": [], "checks": [], "released": [], "cycles": 0, "events": []}
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    patch_health_event(monkeypatch, lambda name, **k: rec["events"].append(name))
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(g.deps, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(g.deps, "isolated_chips", lambda: set(bus["isolated"]))
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: bus["off"])
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(galaxy, "refuse_mesh_reset", lambda *a, **k: False)
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)

    async def sbr(log):
        rec["rungs"].append("sbr")
        if heals_after_sbr:
            bus["off"], bus["isolated"] = 0, set()
        return True

    async def mesh(argv, log, *a, **k):
        rec["rungs"].append("mesh")
        bus["off"], bus["isolated"] = 0, set()
        return 0, ""

    async def verify(expected, log, run_fabric=True, **k):
        rec["checks"].append(run_fabric)
        healthy = verdicts.pop(0)
        return healthy, {"snapshot": {"ok": healthy}, "fabric": {"ok": healthy}}

    async def power_cycle(log, reason):
        rec["cycles"] += 1

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)
    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **kw: rec["released"].append(kw) or True)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    return g, rec


async def _sweep(g, off=PARTIAL):
    return await g._settle_and_verify_before_host_rung(32, lambda m: None, "post-job", offbus_chips=set(off))


@pytest.mark.asyncio
async def test_healed_after_sbr_runs_no_later_rung_and_lifts_only_on_the_full_check(monkeypatch, galaxy_trays):
    g, rec = _arm(monkeypatch, heals_after_sbr=True, verdicts=[True])
    assert await _sweep(g) is True
    assert rec["rungs"] == ["sbr"], f"the bus is whole after SBR: no later rung fires; got {rec['rungs']}"
    assert rec["checks"] == [True], "one full check, fabric included"
    assert len(rec["released"]) == 1 and rec["released"][0]["verified"] is True
    assert "last_chance_sweep_bus_whole" in rec["events"]


@pytest.mark.asyncio
async def test_bus_whole_but_full_check_fails_runs_the_remaining_rung_then_the_caller_climbs(
    monkeypatch, galaxy_trays
):
    g, rec = _arm(monkeypatch, heals_after_sbr=True, verdicts=[False, False])
    assert await _sweep(g) is False, "the full ladder ran and the mesh did not verify: the caller climbs"
    assert rec["rungs"] == ["sbr", "mesh"], "a failed full check resumes the remaining rung"
    assert rec["checks"] == [True, True], "every verify is the full check, fabric included"
    assert rec["released"] == [], "the bus probe alone never lifts"
    assert "last_chance_sweep_resumed" in rec["events"]


@pytest.mark.asyncio
async def test_bus_whole_check_fails_then_the_mesh_reset_heals_it_lifts_on_the_second_check(
    monkeypatch, galaxy_trays
):
    g, rec = _arm(monkeypatch, heals_after_sbr=True, verdicts=[False, True])
    assert await _sweep(g) is True
    assert rec["rungs"] == ["sbr", "mesh"]
    assert rec["checks"] == [True, True]
    assert len(rec["released"]) == 1, "released once, on the passing full check after the mesh reset"


@pytest.mark.asyncio
async def test_never_healed_runs_every_rung_back_to_back_with_one_check(monkeypatch, galaxy_trays):
    g, rec = _arm(monkeypatch, heals_after_sbr=False, verdicts=[False])
    assert await _sweep(g) is False
    assert rec["rungs"] == ["sbr", "mesh"], "the bus is still short after SBR: the mesh reset follows at once"
    assert rec["checks"] == [True], "one settle and one full check after the whole sweep, as before"
    assert rec["released"] == []
    assert "last_chance_sweep_bus_whole" not in rec["events"]


@pytest.mark.asyncio
async def test_an_unreadable_bus_probe_fires_the_next_rung(monkeypatch, galaxy_trays):
    g, rec = _arm(monkeypatch, heals_after_sbr=True, verdicts=[False])
    calls = {"n": 0}

    def flaky(expected):
        calls["n"] += 1
        if calls["n"] == 2:  # the first read is the sweep's own gate; the second is the re-probe
            raise OSError("sysfs read failed")
        return 0

    monkeypatch.setattr(pcie_guard, "chips_off_bus", flaky)
    assert await _sweep(g) is False
    assert rec["rungs"] == ["sbr", "mesh"], "a probe that cannot read the bus never skips a rung"


@pytest.mark.asyncio
async def test_with_no_isolated_chip_the_sweep_is_the_mesh_reset_alone(monkeypatch, galaxy_trays):
    g, rec = _arm(monkeypatch, heals_after_sbr=False, verdicts=[False], isolated=set())
    assert await _sweep(g) is False
    assert rec["rungs"] == ["mesh"], "no SBR to fire, so no re-probe; the mesh reset is the sweep"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heals, verdicts, rungs, cycles",
    [
        (True, [True], ["sbr"], 0),
        (True, [False, False], ["sbr", "mesh"], 1),
        (False, [False], ["sbr", "mesh"], 1),
    ],
)
async def test_the_gate_ladder_power_cycles_only_when_the_full_check_still_fails(
    monkeypatch, galaxy_trays, heals, verdicts, rungs, cycles
):
    """I76: the gate's host rung runs the sweep, and requests the power cycle exactly when the full
    check after the whole ladder still fails."""
    g, rec = _arm(monkeypatch, heals_after_sbr=heals, verdicts=verdicts)
    beats = {str(i): (0xFFFFFFFF if str(i) in PARTIAL else 100) for i in range(32)}
    out = await g._fire_gate_rung(
        "post-job", galaxy.STAGE_POWER_CYCLE, ["16", "17", "18"], 32, lambda m: None, ev=None, beats=beats
    )
    assert rec["rungs"] == rungs
    assert rec["cycles"] == cycles
    assert out == (galaxy.OUTCOME_RECOVERED if cycles == 0 else galaxy.OUTCOME_WAITING)


@pytest.mark.asyncio
async def test_a_whole_tray_drop_still_skips_the_sweep(monkeypatch, galaxy_trays):
    """I37: a whole tray off the bus goes to the power cycle, with no rung and no re-probe."""
    tray = {str(i) for i in range(8, 16)}
    g, rec = _arm(monkeypatch, heals_after_sbr=True, verdicts=[], isolated=tray)
    assert await _sweep(g, off=tray) is False
    assert rec["rungs"] == [] and rec["checks"] == []
