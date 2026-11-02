# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A present mesh with chips whose ARC is not ready takes the mesh reset at once (spec 04 I105).

Every chip on the bus, at least one reading ENODATA on a bound, link-up endpoint (spec 04 I24), none
off it: the driver waits for the post-reset init only the mesh reset (POST_RESET, board re-init)
runs. The gate resets on its first pass instead of holding below the galaxy-reset floor for the
10-min bound. The full check after that reset, fabric included, still decides the release; a failed
one keeps the hold and the bound's ladder still fires. A fabric/eth wedge with every ARC ticking
keeps the below-floor hold. No test reaches a real reset."""

from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_arc_not_ready import EXPECTED, _fake_sysfs, _live_health
from tests.test_device_safety import _no_holders, _rung
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.recovery import OUTCOME_TERMINAL, galaxy

ALL = range(EXPECTED)
BOUND = 600


# ---------------------------------------------------------------- the router


@pytest.mark.parametrize("not_ready", [EXPECTED, 1])
def test_a_present_mesh_with_arc_not_ready_routes_to_the_mesh_reset_not_the_floor(monkeypatch, health_deps, not_ready):
    """(a) 32 present, 0 ARC ready, 0 off the bus: the mesh reset now. One chip is enough: no narrower
    rung re-inits an ARC on a Galaxy."""
    assert _rung(monkeypatch, health_deps, off_bus=0, frozen_chips=0, arc_not_ready=not_ready) == galaxy.STAGE_SMI_RESET


@pytest.mark.parametrize("frozen", [0, 3])
def test_a_wedge_with_every_arc_ticking_still_holds_below_the_floor(monkeypatch, health_deps, frozen):
    """(c) Nothing off the bus, every ARC ready: today's below-floor hold, never a mesh reset."""
    assert _rung(monkeypatch, health_deps, off_bus=0, frozen_chips=frozen, arc_not_ready=0) == galaxy.BELOW_FLOOR


def test_arc_not_ready_with_a_chip_off_the_bus_keeps_the_off_bus_routing(monkeypatch, health_deps):
    """A chip off the bus is the drop the floor guards (a mesh reset there can invert it): ARC-not-ready
    chips beside it change nothing."""
    assert _rung(monkeypatch, health_deps, off_bus=1, frozen_chips=0, arc_not_ready=31) == galaxy.BELOW_FLOOR


@pytest.mark.parametrize(
    "kw, want",
    [
        ({"eth_frozen": True}, galaxy.WAIT),
        ({"scope_active": True}, galaxy.DEFER),
        ({"sbr_candidates": 1}, galaxy.STAGE_BRIDGE_RESET),
    ],
)
def test_arc_not_ready_keeps_the_guards_before_it(monkeypatch, health_deps, kw, want):
    """The frozen-eth hold, a live reset scope and the surgical rung still come first."""
    assert _rung(monkeypatch, health_deps, off_bus=0, arc_not_ready=EXPECTED, **kw) == want


def test_arc_not_ready_after_a_failed_mesh_reset_does_not_loop_the_reset(monkeypatch, health_deps):
    """(b) The rule fires once per decision: after the mesh reset failed, the router does not hand it
    back; the hold stays and the 10-min bound owns the rest."""
    stage = _rung(
        monkeypatch,
        health_deps,
        off_bus=0,
        arc_not_ready=EXPECTED,
        last_action=galaxy.STAGE_SMI_RESET,
        last_action_recovered=False,
    )
    assert stage != galaxy.STAGE_SMI_RESET


# ---------------------------------------------------------------- the gate


def _gate(monkeypatch, tmp_path, *, enodata, reset_ok, verdict=None):
    """The post-job gate on a fake 32-chip sysfs with ``enodata`` chips ARC-not-ready. Verify fails as
    enum+ARC does on such a mesh unless ``verdict`` is given; the mesh reset is stubbed to return
    ``reset_ok`` (its own full check, fabric included). Returns (resets, events)."""
    _fake_sysfs(monkeypatch, tmp_path, enodata=enodata)
    _live_health(monkeypatch)
    dev = tmp_path / "dev"
    dev.mkdir()
    for i in ALL:
        (dev / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    _no_holders(monkeypatch)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    srv.recovery_mechanism.last_reset_monotonic = 0.0
    srv.recovery_mechanism.last_reset_failed = False
    events = []
    patch_health_event(monkeypatch, lambda kind, **k: events.append(kind))
    resets = []

    async def reset(indices, log):
        resets.append(sorted(indices or [], key=int))
        return reset_ok

    if verdict is None:
        verdict = (False, {"arc": {"ok": False, "detail": f"{len(list(enodata))} chip(s) ARC not ready"}})

    async def verify(expected, log, run_fabric=True, **_):
        return verdict

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    patch_recovery(monkeypatch, "_verify_device", verify)
    return resets, events


@pytest.mark.asyncio
async def test_the_gate_resets_a_present_arc_not_ready_mesh_on_its_first_pass(monkeypatch, tmp_path, clear_job_state):
    """(a) The measured shape: a job ends, all 32 chips on the bus, none with a ready ARC. The gate runs
    the mesh reset on that pass, not 10 min later, and never takes the below-floor hold."""
    resets, events = _gate(monkeypatch, tmp_path, enodata=ALL, reset_ok=True)

    srv._mark_device_dirty("job ended rc 6")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert len(resets) == 1, "an ARC-not-ready present mesh was held for the 10-min bound instead of reset"
    assert "arc_not_ready_mesh_reset" in events
    assert "galaxy_reset_suppressed_below_mass_threshold" not in events


@pytest.mark.asyncio
async def test_a_failed_check_after_that_reset_keeps_the_hold_and_the_bound_still_fires(
    monkeypatch, tmp_path, clear_job_state
):
    """(b) The reset's full check fails (fabric short of its cabled links): the device stays held, nothing
    releases it, and at 10 min the bound's own ladder runs, then the host rung (spec 04 I76)."""
    resets, events = _gate(monkeypatch, tmp_path, enodata=ALL, reset_ok=False)
    released = []
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: released.append(why))

    srv._mark_device_dirty("job ended rc 6")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert len(resets) >= 1 and "arc_not_ready_mesh_reset" in events
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed post-reset check released the device"
    assert not released
    assert srv.device_hold_episode_since, "no hold episode open for the 10-min bound to time"

    # The bound: the same hold, 10 min on.
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(seconds=BOUND + 3)).isoformat())
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in ALL])
    monkeypatch.setattr(srv, "_fabric_no_reset_paged_since", None)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(srv.alert, "send_alert", lambda event, log=None: None)
    monkeypatch.setattr(srv.fsm, "on_outcome", lambda outcome: None)
    ladders = []

    async def failed_ladder(kind, indices, expected, log, **_):
        ladders.append(kind)
        return OUTCOME_TERMINAL

    async def host_rung(age, log):
        events.append("host_rung")

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    monkeypatch.setattr(srv, "_hold_bound_host_rung", host_rung)
    await srv._force_escalate_stuck_hold()

    assert ladders, "the 10-min bound's ladder did not run on the held ARC-not-ready mesh"
    assert "hold_deadline_forced_escalation" in events
    assert events.count("host_rung") == 1, "a ladder that did not heal goes on to the host rung"
    assert not released


@pytest.mark.asyncio
async def test_the_gate_still_holds_a_fabric_wedge_with_every_arc_ticking(monkeypatch, tmp_path, clear_job_state):
    """(c) Every chip on the bus with a ready ARC, the fabric failing: today's below-floor hold, no
    reset."""
    fabric_bad = (
        False,
        {
            "snapshot": {"ok": True, "detail": "32 chips"},
            "eth_heartbeat": {"ok": None, "detail": "could not check"},
            "fabric": {"ok": False, "detail": "links down"},
        },
    )
    resets, events = _gate(monkeypatch, tmp_path, enodata=(), reset_ok=True, verdict=fabric_bad)

    srv._mark_device_dirty("job ended rc 6")
    await srv._device_health_gate(None, phase="post-job", run_fabric=True)

    assert resets == [], "a wedge with every ARC ticking took the mesh reset"
    assert "galaxy_reset_suppressed_below_mass_threshold" in events
    assert "arc_not_ready_mesh_reset" not in events
    assert srv.fsm.state is not ServerState.HEALTHY
