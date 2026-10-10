# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""ladder-v2: the two named hold branches, the power-cycle cooldown, the settle-before-host-rung,
and the device-fault job rule.

Behaviour under test (user decisions 2026-09-16/17):
- TRAY_DOWN_NO_WINDOW fires every reset type back-to-back (SBR -> per-tray re-power -> mesh reset),
  ONE settle, ONE verify, then the power cycle — no retries, no per-rung verify.
- GENERIC keeps the verify-between shape and retries the mesh reset up to the 600s ceiling.
- A power cycle is spaced by a 30-min (1800s) cooldown, the only per-box rate limit; the boot-loop
  denial (per-boot cap, unreadable-ledger fail-closed) is kept.
- A job SIGKILLed by device recovery falls off the queue: restore and scope reconciliation drop it.
"""

import pathlib
from dataclasses import replace

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.recovery import Evidence, galaxy


async def _async_true(*a, **k):
    return True


def _ev(**over):
    """A minimal gate Evidence for the tray-stage dispatch tests. Only ``off_bus`` and
    ``bridge_reset_failed`` matter to the STAGE_UBB_TRAY rung; everything else takes an inert
    default so a test names just the field it exercises."""
    base = dict(
        off_bus=0,
        frozen_chips=0,
        expected=32,
        sbr_candidates=0,
        eth_frozen=False,
        cooling=False,
        scope_active=False,
        was_failing=False,
        healthy=False,
        fault_reported=False,
        fabric_ok=None,
        fabric_ran=False,
        dirty=False,
        fabric_forced=False,
        last_action=None,
        last_action_recovered=None,
        off_bus_before=None,
        reset_exit_nonzero=False,
        holding_fabric_unverified=False,
        bridge_reset_failed=None,
    )
    base.update(over)
    return Evidence(**base)


# --------------------------------------------------------------------------- _classify_hold


def test_classify_hold_names_the_two_branches(galaxy_trays):
    """A whole UBB tray off the bus with no bridge window on any chip is TRAY_DOWN_NO_WINDOW;
    everything else — a partial window, a not-whole-tray drop, or an unknown window — is GENERIC."""
    # Chips 8-15 sit on bus group 0x40, which is UBB tray 2 on Blackhole (I16); index arithmetic
    # would call it tray 1. The classification keys on the bus-derived tray map.
    tray = {str(i) for i in range(8, 16)}
    no_window = {c: {"reason": "no_bridge"} for c in tray}
    assert galaxy._classify_hold(tray, 32, no_window, galaxy_trays) == galaxy.HOLD_CLASS_TRAY_DOWN_NO_WINDOW

    # one chip still has a bridge window (SBR reason != no_bridge) -> not the no-window class
    partial = dict(no_window)
    partial["8"] = {"reason": "reset_ineffective"}
    assert galaxy._classify_hold(tray, 32, partial, galaxy_trays) == galaxy.HOLD_CLASS_GENERIC

    # not a whole tray (2 chips of one tray) -> generic even if both no_bridge
    two = {"8", "9"}
    assert (
        galaxy._classify_hold(two, 32, {"8": {"reason": "no_bridge"}, "9": {"reason": "no_bridge"}}, galaxy_trays)
        == galaxy.HOLD_CLASS_GENERIC
    )

    # a whole tray but no SBR reason recorded -> window UNKNOWN -> generic (never assume no_bridge)
    assert galaxy._classify_hold(tray, 32, {}, galaxy_trays) == galaxy.HOLD_CLASS_GENERIC

    # empty drop / single-tray host / no bus map -> generic
    assert galaxy._classify_hold(set(), 32, {}, galaxy_trays) == galaxy.HOLD_CLASS_GENERIC
    assert galaxy._classify_hold({"0"}, 8, {"0": {"reason": "no_bridge"}}, galaxy_trays) == galaxy.HOLD_CLASS_GENERIC
    assert galaxy._classify_hold(tray, 32, no_window, None) == galaxy.HOLD_CLASS_GENERIC


# ------------------------------------------ the branch is LIVE: escalate()'s gate path classifies + fires


@pytest.mark.asyncio
async def test_gate_tray_down_no_window_dispatches_the_back_to_back_sweep(monkeypatch, galaxy_trays):
    """Through escalate()'s gate path (the live STAGE_UBB_TRAY rung, not the helper directly): a whole
    tray off the bus whose every off-bus chip reported no_bridge — threaded onto
    Evidence.bridge_reset_failed by the server — is classified TRAY_DOWN_NO_WINDOW and dispatched to
    _fire_tray_down_no_window: every reset type back-to-back (SBR -> per-tray re-power -> mesh reset),
    NO verify between, exactly ONE settle + ONE verify, then the power cycle. The generic
    verify-between walk must NOT run."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))

    order = []

    async def sbr(log):
        order.append("sbr")
        return False  # a gone tray has no bridge — SBR is a no-op, fired anyway (nothing to lose)

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: order.append(("tray", bitmap)))

    async def mesh(argv, log, *a, **k):
        order.append("mesh")
        return 0, ""

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)

    verify_calls = {"n": 0}

    async def verify(expected, log, run_fabric=True, **k):
        verify_calls["n"] += 1
        return False, {"snapshot": {"ok": False}}  # still bad -> straight to the power cycle

    patch_recovery(monkeypatch, "_verify_device", verify)

    generic = {"n": 0}

    async def generic_walk(beats, off_bus, expected, log):
        generic["n"] += 1
        return True

    monkeypatch.setattr(g, "_attempt_ubb_tray_reset", generic_walk)

    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: False)
    monkeypatch.setattr(g.mechanism, "auto_recovery_allowed", lambda action, tenant_active=False, **k: (True, ""))
    cycles = {"n": 0}

    async def fake_pc(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)

    beats = {str(i): 100 for i in range(24)}  # chips 0-23 present; UBB3 (24-31) off the bus
    offbus = {str(i) for i in range(24, 32)}
    ev = _ev(off_bus=8, bridge_reset_failed={c: {"reason": "no_bridge"} for c in offbus})

    out = await g.escalate(
        "gate/post-job", sorted(offbus, key=int), 32, lambda m: None, stage=galaxy.STAGE_UBB_TRAY, ev=ev, beats=beats
    )

    assert out == galaxy.OUTCOME_WAITING
    steps = [o if isinstance(o, str) else o[0] for o in order]
    assert steps == ["sbr", "tray", "mesh"], "every reset type fires back-to-back, in order, no verify between"
    # Chips 24-31 sit on bus group 0xc0 which is UBB3 on Blackhole (I16); the BMC bitmap bit is
    # tray-1 (1-based tray, 0-based bit) so UBB3 → bit 2, not bit 3 (which would be UBB4 on BH).
    assert order[1] == ("tray", 1 << 2), "the affected tray (UBB3) is re-powered"
    assert verify_calls["n"] == 1, "exactly ONE settle+verify after the whole sweep — no retries, no ceiling"
    assert cycles["n"] == 1, "a sweep that did not recover climbs straight to the cold power cycle"
    assert generic["n"] == 0, "the generic verify-between walk must NOT run for a tray-down-no-window drop"


@pytest.mark.asyncio
async def test_gate_partial_tray_or_a_bridge_window_takes_the_generic_walk(monkeypatch):
    """Anything that is not a whole tray with no window on EVERY chip is GENERIC — the verify-between
    per-tray walk, never the back-to-back sweep. Covers a partial-tray drop and a whole tray where one
    chip still has a bridge window."""
    g = srv.galaxy_recovery
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: None)

    sweep = {"n": 0}

    async def sweep_fn(*a, **k):
        sweep["n"] += 1
        return galaxy.OUTCOME_RECOVERED

    monkeypatch.setattr(g, "_fire_tray_down_no_window", sweep_fn)

    generic = {"n": 0}

    async def generic_walk(beats, off_bus, expected, log):
        generic["n"] += 1
        return True  # recovered -> the rung releases

    monkeypatch.setattr(g, "_attempt_ubb_tray_reset", generic_walk)

    # (1) partial tray: only 4 of UBB3's chips off the bus, every one no_bridge -> not a whole tray
    beats = {str(i): 100 for i in range(28)}  # 28-31 off -> partial UBB3
    ev = _ev(off_bus=4, bridge_reset_failed={str(i): {"reason": "no_bridge"} for i in range(28, 32)})
    out = await g.escalate(
        "gate/post-job", ["28", "29", "30", "31"], 32, lambda m: None, stage=galaxy.STAGE_UBB_TRAY, ev=ev, beats=beats
    )
    assert out == galaxy.OUTCOME_RECOVERED
    assert sweep["n"] == 0 and generic["n"] == 1, "a partial-tray drop is GENERIC, never the sweep"

    # (2) whole tray but one chip still has a bridge window (reason != no_bridge) -> GENERIC
    beats2 = {str(i): 100 for i in range(24)}
    br = {str(i): {"reason": "no_bridge"} for i in range(24, 32)}
    br["24"] = {"reason": "reset_ineffective"}
    ev2 = _ev(off_bus=8, bridge_reset_failed=br)
    out2 = await g.escalate(
        "gate/post-job",
        [str(i) for i in range(24, 32)],
        32,
        lambda m: None,
        stage=galaxy.STAGE_UBB_TRAY,
        ev=ev2,
        beats=beats2,
    )
    assert out2 == galaxy.OUTCOME_RECOVERED
    assert sweep["n"] == 0 and generic["n"] == 2, "a whole tray with ANY bridge window is GENERIC, never the sweep"


@pytest.mark.asyncio
async def test_gate_tray_down_no_window_names_the_command_and_holds_when_not_opted_in(monkeypatch, galaxy_trays):
    """With TT_DEVICE_MCP_AUTO_UBB_RESET=0 the no-window branch honours the same opt-out the generic
    path does: it NAMES the BMC command (ubb_reset_required) and holds — it never re-powers silicon."""
    g = srv.galaxy_recovery
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")
    events = []
    patch_health_event(monkeypatch, lambda kind, **k: events.append(kind))

    sweep = {"n": 0}

    async def sweep_fn(*a, **k):
        sweep["n"] += 1
        return galaxy.OUTCOME_WAITING

    monkeypatch.setattr(g, "_fire_tray_down_no_window", sweep_fn)

    beats = {str(i): 100 for i in range(24)}
    offbus = {str(i) for i in range(24, 32)}
    ev = _ev(off_bus=8, bridge_reset_failed={c: {"reason": "no_bridge"} for c in offbus})
    out = await g.escalate(
        "gate/post-job", sorted(offbus, key=int), 32, lambda m: None, stage=galaxy.STAGE_UBB_TRAY, ev=ev, beats=beats
    )

    assert out == galaxy.OUTCOME_WAITING
    assert sweep["n"] == 0, "the sweep must not re-power silicon when the per-tray fire is opted out"
    assert "ubb_reset_required" in events, "the exact BMC command is named for the operator, then held"


@pytest.mark.asyncio
async def test_bridge_rung_records_no_bridge_and_it_survives_the_server_replace(monkeypatch, galaxy_trays):
    """The seam that makes the branch live: the bridge rung records each gone chip's no_bridge outcome
    on last_bridge_reset_reasons, the server carries it onto Evidence.bridge_reset_failed via
    replace(), and _classify_hold reads it to name TRAY_DOWN_NO_WINDOW. A chip whose window was never
    recorded reads unknown -> GENERIC (the sweep never fires on a guess)."""
    g = srv.galaxy_recovery
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    tray3 = {str(i) for i in range(24, 32)}
    saved_iso, saved_map = srv.isolated_chips, srv.device_pci_map
    try:
        srv.isolated_chips = set(tray3)
        srv.device_pci_map = {c: f"0000:{40 + int(c):02x}:00.0" for c in tray3}
        monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda idx, bdf: None)  # gone -> no_bridge

        ok = await g._recover_isolated_chips(lambda m: None)

        assert ok is False, "an all-gone tray never reads as recovered"
        assert g.last_bridge_reset_reasons == {c: {"reason": "no_bridge"} for c in tray3}

        # the server's replace(ev, bridge_reset_failed=…) after the bridge rung, then classify:
        ev = replace(_ev(off_bus=8), bridge_reset_failed=g.last_bridge_reset_reasons)
        assert (
            galaxy._classify_hold(tray3, 32, ev.bridge_reset_failed, galaxy_trays)
            == galaxy.HOLD_CLASS_TRAY_DOWN_NO_WINDOW
        )

        # drop one chip's recorded window -> unknown -> GENERIC (never assume no_bridge)
        partial = dict(g.last_bridge_reset_reasons)
        partial.pop("24")
        ev2 = replace(_ev(off_bus=8), bridge_reset_failed=partial)
        assert galaxy._classify_hold(tray3, 32, ev2.bridge_reset_failed, galaxy_trays) == galaxy.HOLD_CLASS_GENERIC
    finally:
        srv.isolated_chips, srv.device_pci_map = saved_iso, saved_map


# ------------------------------------------------------- (a) TRAY_DOWN_NO_WINDOW back-to-back sweep


@pytest.mark.asyncio
async def test_tray_down_no_window_fires_all_rungs_back_to_back_then_one_verify(monkeypatch, galaxy_trays):
    """Every reset type fires back-to-back with NO verify/settle between them — SBR, per-tray
    re-power of the affected tray, then the mesh-wide reset — then exactly ONE settle and ONE verify.
    A verify that comes back healthy releases without a power cycle."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    order = []
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))

    async def sbr(log):
        order.append("sbr")
        return False  # a gone tray has no bridge — SBR is a no-op, fired anyway (nothing to lose)

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: order.append(("tray", bitmap)))

    async def mesh(argv, log, *a, **k):
        order.append("mesh")
        return 0, ""

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)
    verify_calls = {"n": 0}

    async def verify(expected, log, run_fabric=True, **k):
        verify_calls["n"] += 1
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    offbus = {str(i) for i in range(24, 32)}  # UBB3, whole tray of 8
    out = await g._fire_tray_down_no_window(offbus, 32, lambda m: None, gate_phase="post-job", ev=None)

    assert out == galaxy.OUTCOME_RECOVERED
    steps = [o if isinstance(o, str) else o[0] for o in order]
    assert steps == ["sbr", "tray", "mesh"], "every reset type fires back-to-back, in order, no verify between"
    assert order[1] == ("tray", 1 << 2), "the affected tray (UBB3) is re-powered"
    assert verify_calls["n"] == 1, "exactly ONE verify, after the whole back-to-back sweep"


@pytest.mark.asyncio
async def test_tray_down_no_window_power_cycles_when_the_sweep_does_not_recover(monkeypatch):
    """When the one verify is still bad, the branch goes straight to the power cycle — no retries, no
    ceiling wait. A whole tray off the bus is warm-reboot-futile, so it is the cold rung, not a reboot."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_recovery(monkeypatch, "_recover_isolated_chips", _async_true)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: None)

    async def mesh(argv, log, *a, **k):
        return 0, ""

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)

    async def verify(expected, log, run_fabric=True, **k):
        return False, {"snapshot": {"ok": False}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: False)
    monkeypatch.setattr(g.mechanism, "auto_recovery_allowed", lambda action, tenant_active=False, **k: (True, ""))
    reboots = {"n": 0}
    cycles = {"n": 0}

    async def fake_pc(log, reason):
        cycles["n"] += 1

    async def fake_reboot(log, reason):
        reboots["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)
    monkeypatch.setattr(g, "_auto_reboot_host", fake_reboot)

    offbus = {str(i) for i in range(24, 32)}
    out = await g._fire_tray_down_no_window(offbus, 32, lambda m: None, gate_phase="post-job", ev=None)

    assert out == galaxy.OUTCOME_WAITING
    assert cycles["n"] == 1, "a sweep that did not recover the mesh climbs straight to the power cycle"
    assert reboots["n"] == 0, "a whole tray off the bus is warm-reboot-futile — never the warm reboot"


@pytest.mark.asyncio
async def test_tray_down_no_window_holds_under_a_tenant(monkeypatch):
    """The sweep re-powers silicon, so it never runs under a tenant on the device."""
    g = srv.galaxy_recovery
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=False))
    fired = {"n": 0}
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    out = await g._fire_tray_down_no_window(
        {str(i) for i in range(24, 32)}, 32, lambda m: None, gate_phase="x", ev=None
    )
    assert out == galaxy.OUTCOME_WAITING and fired["n"] == 0, "an unreadable/held scan holds the sweep"


# ----------------------------------------------------------- (c) settle + verify before a host rung


@pytest.mark.asyncio
async def test_settle_and_verify_gates_the_power_cycle_host_rung(monkeypatch):
    """Before any host rung fires, the ladder settles once and verifies once: a mesh that returned
    during the settle skips the reboot/power cycle; one still bad proceeds to it."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    cycles = {"n": 0}

    async def fake_pc(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)

    async def healthy(expected, log, run_fabric=True, **k):
        return True, {"snapshot": {"ok": True}}

    patch_recovery(monkeypatch, "_verify_device", healthy)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    assert (
        out == galaxy.OUTCOME_RECOVERED and cycles["n"] == 0
    ), "a mesh that returned during the settle skips the host rung"

    async def bad(expected, log, run_fabric=True, **k):
        return False, {"snapshot": {"ok": False}}

    patch_recovery(monkeypatch, "_verify_device", bad)
    # The sweep did not run (no holder scan stubbed, so it reads as a tenant): the full ladder has not
    # been tried, so the host rung holds (spec 04 I18).
    out_held = await g._fire_gate_rung(
        "post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={}
    )
    assert (
        out_held == galaxy.OUTCOME_WAITING and cycles["n"] == 0
    ), "a host rung never fires when the last-chance sweep was skipped"

    # The full sweep ran and the mesh is still bad: the host rung fires.
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: None)

    async def fake_reset(argv, log, *a, **k):
        return 0, ""

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", fake_reset)
    out2 = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    assert (
        out2 == galaxy.OUTCOME_WAITING and cycles["n"] == 1
    ), "a mesh still bad after the full sweep, settle and verify proceeds to the host rung"


# ---------------------------------------------------------------- (d) power-cycle cooldown (1800s)


def test_power_cycle_cooldown_is_thirty_minutes(monkeypatch):
    """A power cycle is denied less than 1800s (30 min) after the previous one, and allowed past it —
    the only per-box spacing guard. The old 3600s per-severity interval no longer gates a power
    cycle (a power cycle 1900s ago used to be denied)."""
    rm = srv.recovery_mechanism
    now = 1_000_000.0
    monkeypatch.setattr(rm, "_current_boot_id", lambda: "this-boot")  # prior cycle took the box down -> different boot

    def ledger_at(age):
        return lambda: [{"action": "power-cycle", "boot_id": "prior-boot", "at_epoch": now - age}]

    monkeypatch.setattr(rm, "read_auto_recovery_ledger", ledger_at(1700))
    allowed, why = rm.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=now)
    assert not allowed and "1800" in why, "a power cycle within 30 min is denied by the cooldown"

    monkeypatch.setattr(rm, "read_auto_recovery_ledger", ledger_at(1900))
    allowed2, _ = rm.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=now)
    assert allowed2, "a power cycle past 30 min is allowed (the old 3600s interval would still deny it)"


def test_power_cycle_cooldown_keeps_the_boot_loop_denial(monkeypatch):
    """The cooldown replaces only the power cycle's spacing. The boot-loop denial stays: an
    unreadable ledger fails closed, and a tenant on the device is absolute."""
    rm = srv.recovery_mechanism
    monkeypatch.setattr(rm, "read_auto_recovery_ledger", lambda: None)  # unreadable
    allowed, why = rm.auto_recovery_allowed("power-cycle", tenant_active=False)
    assert not allowed and "unreadable" in why, "an unreadable ledger fails closed (boot-loop guard kept)"

    monkeypatch.setattr(rm, "read_auto_recovery_ledger", lambda: [])
    allowed2, why2 = rm.auto_recovery_allowed("power-cycle", tenant_active=True)
    assert not allowed2 and "tenant" in why2, "a tenant on the device is absolute"


def test_reboot_interval_is_unchanged_by_the_power_cycle_cooldown(monkeypatch):
    """Only the power cycle's spacing changed. A reboot is still gated by the general 3600s interval,
    so a reboot 1900s ago still denies a reboot (a power cycle at the same age would be allowed)."""
    rm = srv.recovery_mechanism
    now = 1_000_000.0
    monkeypatch.setattr(rm, "_current_boot_id", lambda: "this-boot")
    monkeypatch.setattr(
        rm, "read_auto_recovery_ledger", lambda: [{"action": "reboot", "boot_id": "prior-boot", "at_epoch": now - 1900}]
    )
    allowed, why = rm.auto_recovery_allowed("reboot", tenant_active=False, now_epoch=now)
    assert not allowed and "3600" in why, "a reboot still uses the 3600s interval, unchanged by ladder-v2"


# --------------------------------------------------------- (e) a device-fault-killed job falls off


@pytest.mark.asyncio
async def test_a_device_fault_killed_job_is_not_restored_after_a_restart(monkeypatch, tmp_path, clear_job_state):
    """A job SIGKILLed by device recovery is recorded durably and dropped by queue restore — the
    exact job that wedged the device never re-runs after the reboot (that is how boot loops start).
    A job with no such record is restored normally."""
    monkeypatch.setattr(srv, "job_log_dir", str(tmp_path))
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)

    killed = srv.Job(id="042", owner="u", workspace="/w", command="wedged", queued_at="2026-01-01T00:00:00")
    ok = srv.Job(id="043", owner="u", workspace="/w", command="fine", queued_at="2026-01-01T00:00:01")
    srv._persist_queued_job(killed)
    srv._persist_queued_job(ok)
    srv._mark_job_device_fault_failed("042", "chip 7 left the PCIe bus")
    assert srv._device_fault_failed_reason("042"), "the kill is recorded durably"

    await srv._restore_queued_jobs()

    assert "042" not in srv.jobs, "a device-fault-killed job must not be re-queued"
    assert "043" in srv.jobs, "an ordinary queued job is still restored"
    assert not srv._queued_spec_path(
        "042"
    ).exists(), "the killed job's spec is dropped so a later boot never restores it"
    assert srv._device_fault_failed_reason("042") is None, "the record is consumed once the restart it guarded has run"


@pytest.mark.asyncio
async def test_a_device_fault_killed_scope_is_not_readopted(monkeypatch, tmp_path, clear_job_state):
    """Scope reconciliation also drops a device-fault-killed job: its running scope is not re-adopted
    after a broker restart."""
    monkeypatch.setattr(srv, "job_log_dir", str(tmp_path))
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "list_active_job_scopes", lambda: {"042": "ttdev-job-042.scope"})
    srv._mark_job_device_fault_failed("042", "chip left the bus")

    await srv.reconcile_running_scopes()

    assert "042" not in srv.jobs, "a device-fault-killed running scope must not be re-adopted"


# ------------------------------------------------------------------------ (f) the scrub grep test


def test_no_fabricated_self_heal_timing_left_in_src():
    """The '15-89 min' / '50 min' self-recovery rationale is fabricated and must be gone: a reset
    brings those cases back at once, so the ladder never waits that long."""
    root = pathlib.Path(srv.__file__).resolve().parent
    offenders = []
    for p in root.rglob("*.py"):
        text = p.read_text()
        for pat in ("15-89", "15–89", "50 min"):
            if pat in text:
                offenders.append(f"{p.relative_to(root)}: {pat!r}")
    assert not offenders, f"fabricated self-heal timing must be scrubbed from src/: {offenders}"


# --------------------------------------- (c2) the last-chance reset sweep gates EVERY host rung
# User, 2026-09-17: "SBR -> tray re-power -> mesh reset back-to-back should be called always before
# any power cycle. We always need to give the resets one last chance before power cycle."
# Before this, only TRAY_DOWN_NO_WINDOW swept; the gate ladder's ceiling, the idle/stuck-hold
# escalation (the road most real power cycles took) and the post-reboot cold climb settled and
# verified but never re-issued the rungs.


def _arm_sweep(monkeypatch, *, verify_healthy=False, tenant=False):
    """Count every reset the last-chance sweep issues, in order, and stub the host rungs."""
    g = srv.galaxy_recovery
    order, cycles = [], {"n": 0}
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    uid = 4242 if tenant else 0
    holders = [type("H", (), {"uid": uid, "pid": 1, "user": "t", "cmd": "x"})()] if tenant else []
    monkeypatch.setattr(g.deps, "enumerate_device_holders", lambda: HolderScan(holders=holders, complete=True))
    monkeypatch.setattr(g.deps, "isolated_chips", lambda: {"0"})

    async def sbr(log):
        order.append("sbr")
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: order.append(("tray", bitmap)))

    async def mesh(argv, log, *a, **k):
        order.append("mesh")
        return 0, ""

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)

    async def verify(expected, log, run_fabric=True, **k):
        return verify_healthy, {"snapshot": {"ok": verify_healthy}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    async def fake_pc(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)
    return g, order, cycles


@pytest.mark.asyncio
async def test_gate_ladder_sweeps_every_reset_before_the_power_cycle(monkeypatch, galaxy_trays):
    """The gate ladder's own host rung: SBR -> tray re-power -> mesh reset fire back-to-back, then
    the single settle+verify, and only then the power cycle."""
    g, order, cycles = _arm_sweep(monkeypatch)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    steps = [o if isinstance(o, str) else o[0] for o in order]
    assert steps == ["sbr", "tray", "mesh"], f"every reset type once, in order, before the power cycle; got {steps}"
    assert out == galaxy.OUTCOME_WAITING and cycles["n"] == 1, "still bad after the sweep -> the power cycle fires"


@pytest.mark.asyncio
async def test_a_sweep_that_recovers_the_mesh_cancels_the_power_cycle(monkeypatch, galaxy_trays):
    """The point of sweeping: if the last-chance resets bring the mesh back, the box is NOT cycled."""
    g, order, cycles = _arm_sweep(monkeypatch, verify_healthy=True)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    assert [o if isinstance(o, str) else o[0] for o in order] == ["sbr", "tray", "mesh"]
    assert out == galaxy.OUTCOME_RECOVERED and cycles["n"] == 0, "a mesh the sweep recovered must not be power-cycled"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["idle-escalation", "post-reboot"])
async def test_the_other_two_roads_to_a_power_cycle_go_through_the_sweep(path):
    """Wiring test for the two host-rung roads the gate ladder does not own: the idle/stuck-hold
    escalation (`stuck_hold_host_escalation` — the road MOST real power cycles took on blx01/02/03)
    and the post-reboot cold climb. Both must pass through the last-chance sweep; before ladder-v2
    they climbed to the host rung having only ever re-tried the mesh reset."""
    src = pathlib.Path(galaxy.__file__).read_text()
    fn = "_climb_to_host_recovery_after_failed_reset" if path == "idle-escalation" else "_verify_post_reboot_recovery"
    body = src[src.index(f"async def {fn}(") : src.index("async def ", src.index(f"async def {fn}(") + 10)]
    assert "_settle_and_verify_before_host_rung" in body, (
        f"{fn} must call the last-chance sweep before its host rung — every road to a power cycle "
        f"gives the resets one more try (user, 2026-09-17)"
    )
    # and the sweep is reached BEFORE the rung fires, not after it
    assert body.index("_settle_and_verify_before_host_rung") < body.index(
        "auto_power_cycle_host"
    ), f"{fn} calls the sweep AFTER the power cycle — it must gate it"


@pytest.mark.asyncio
async def test_the_last_chance_sweep_never_resets_over_a_tenant(monkeypatch):
    """The one guard the sweep never crosses: a tenant on the mesh. The settle+verify still runs."""
    g, order, cycles = _arm_sweep(monkeypatch, tenant=True)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    assert order == [], "no reset may fire over a tenant, not even the last-chance sweep"
    assert out == galaxy.OUTCOME_WAITING


# --------------------------------- (c3) a RUNNING broker job is a tenant, fd scan or not
# g15blx02, 2026-09-17 10:07:09Z: the stuck-hold escalation read an empty holder scan 106 s into a
# running job (which was compiling, so it held no /dev/tenstorrent fd), fired a mesh reset, and the
# job died of SIGPIPE at 10:09:18Z the instant the reset released the device. The broker knew the job
# was RUNNING the whole time — nothing asked it.


def _empty_scan():
    return HolderScan(holders=[], complete=True)


def test_a_running_job_is_a_tenant_even_with_an_empty_holder_scan(monkeypatch):
    """The fd scan is blind while a job is off the device; the queue is not."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(g.deps, "job_running", lambda: True)
    assert g._tenant_active(_empty_scan()) is True, "a RUNNING job must count as a tenant"
    monkeypatch.setattr(g.deps, "job_running", lambda: False)
    assert g._tenant_active(_empty_scan()) is False, "an idle box with a clean scan is not a tenant"


def test_force_waives_an_unreadable_scan_but_never_a_running_job(monkeypatch):
    """`force` (the hold-deadline backstop) exists to get past BLINDNESS, not past a known tenant."""
    g = srv.galaxy_recovery
    unreadable = HolderScan(holders=[], complete=False)
    monkeypatch.setattr(g.deps, "job_running", lambda: False)
    assert g._tenant_active(unreadable) is True, "an unreadable scan counts as a tenant by default"
    assert g._tenant_active(unreadable, force=True) is False, "force waives an unreadable scan"
    monkeypatch.setattr(g.deps, "job_running", lambda: True)
    assert g._tenant_active(unreadable, force=True) is True, "force must NEVER waive a running job"


@pytest.mark.asyncio
async def test_the_last_chance_sweep_never_resets_over_a_running_job(monkeypatch):
    """End to end: with a job RUNNING and no fd holder, the sweep issues no reset."""
    g, order, cycles = _arm_sweep(monkeypatch)
    monkeypatch.setattr(g.deps, "job_running", lambda: True)
    await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    assert order == [], "no reset may fire while the broker has a job RUNNING"


def test_every_tenant_guard_goes_through_the_one_helper():
    """No rung may hand-roll the fd-only check again — that is how this bug got in."""
    src = pathlib.Path(galaxy.__file__).read_text()
    body = src[src.index("def _tenant_active") :]
    rest = src[: src.index("def _tenant_active")] + body[body.index("async def _issue_all_resets_back_to_back") :]
    assert "h.uid >= MIN_TENANT_UID" not in rest, (
        "a tenant check outside _tenant_active() — route it through the helper so a RUNNING job is "
        "always counted, not just open device fds"
    )
