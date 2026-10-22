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

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import Evidence, galaxy


async def _async_true(*a, **k):
    return True


def _ev(**over):
    """A minimal gate Evidence for the below-floor dispatch tests. Only ``off_bus`` matters to the
    BELOW_FLOOR rung; everything else takes an inert default so a test names just the field it
    exercises."""
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
    )
    base.update(over)
    return Evidence(**base)


# --------------------------------------------------------------------------- _classify_hold


# ------------------------------------------ the branch is LIVE: escalate()'s gate path classifies + fires


# ------------------------------------------------------- (a) TRAY_DOWN_NO_WINDOW back-to-back sweep


def _wire_failed_tray_down_sweep(monkeypatch, read_heartbeats, *, allowed=True):
    """Wire the climb after a failed reset so the one verify fails and the power cycle is opted in.
    Returns the recorded health events and the power-cycle counter."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    events = []
    patch_health_event(monkeypatch, lambda name, **k: events.append((name, k)))
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_recovery(monkeypatch, "_recover_isolated_chips", _async_true)

    async def mesh(argv, log, *a, **k):
        return 0, ""

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)

    async def verify(expected, log, run_fabric=True, **k):
        return False, {"snapshot": {"ok": False}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(
        g.mechanism,
        "auto_recovery_allowed",
        lambda action, tenant_active=False, **k: (allowed, "" if allowed else "cooldown"),
    )
    monkeypatch.setattr(g.mechanism, "_journal_auto_recovery_denied", lambda *a, **k: None)
    cycles = {"n": 0}

    async def fake_pc(log, reason):
        cycles["n"] += 1

    monkeypatch.setattr(srv, "_auto_power_cycle_host", fake_pc)
    monkeypatch.setattr(srv, "read_heartbeats", read_heartbeats)
    return events, cycles


def _raise_oserror():
    raise OSError("sysfs read failed")


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
    # The sweep did not run (an unreadable holder scan reads as a tenant): the full ladder has not
    # been tried, so the host rung holds (spec 04 I22).
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=False))
    out_held = await g._fire_gate_rung(
        "post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={}
    )
    assert (
        out_held == galaxy.OUTCOME_WAITING and cycles["n"] == 0
    ), "a host rung never fires when the last-chance sweep was skipped"

    # The full sweep ran and the mesh is still bad: the host rung fires.
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))

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
    """The gate ladder's own host rung: SBR -> mesh reset fire back-to-back, then
    the single settle+verify, and only then the power cycle."""
    g, order, cycles = _arm_sweep(monkeypatch)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    assert order == ["sbr", "mesh"], f"every reset type once, in order, before the power cycle; got {order}"
    assert out == galaxy.OUTCOME_WAITING and cycles["n"] == 1, "still bad after the sweep -> the power cycle fires"


@pytest.mark.asyncio
async def test_a_partial_drop_last_chance_sweep_runs_no_tray_step(monkeypatch, galaxy_trays):
    """Spec 04 I22/I23: the last-chance sweep for a partial drop runs SBR and the mesh reset only, so
    the sweep counts as the full ladder and the caller climbs (False), never the open-ended hold (None)."""
    g, order, cycles = _arm_sweep(monkeypatch)
    off = {str(i) for i in range(24, 30)}  # part of UBB3 (a whole tray skips the sweep, spec 04 I37)
    assert await g._settle_and_verify_before_host_rung(32, lambda m: None, "post-job", offbus_chips=off) is False
    assert order == ["sbr", "mesh"], f"no tray step in the sweep; got {order}"


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [False, True])
async def test_the_sweep_releases_only_after_the_full_health_check(monkeypatch, galaxy_trays, healthy):
    """The sweep's SBR and mesh reset are followed by the full health check, fabric included, and
    only its pass counts as recovered."""
    g, order, cycles = _arm_sweep(monkeypatch, verify_healthy=healthy)
    checks = []

    async def verify(expected, log, run_fabric=True, **k):
        checks.append(run_fabric)
        return healthy, {"snapshot": {"ok": healthy}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    off = {str(i) for i in range(24, 30)}  # part of UBB3 (a whole tray skips the sweep, spec 04 I37)
    out = await g._settle_and_verify_before_host_rung(32, lambda m: None, "post-job", offbus_chips=off)
    assert order == ["sbr", "mesh"]
    assert checks and all(checks), "every verify after the resets runs the fabric check"
    assert out is healthy, "recovered only on a passing full health check; else climb, never release"


@pytest.mark.asyncio
async def test_a_sweep_that_recovers_the_mesh_cancels_the_power_cycle(monkeypatch, galaxy_trays):
    """The point of sweeping: if the last-chance resets bring the mesh back, the box is NOT cycled."""
    g, order, cycles = _arm_sweep(monkeypatch, verify_healthy=True)
    out = await g._fire_gate_rung("post-job", galaxy.STAGE_POWER_CYCLE, ["0"], 32, lambda m: None, ev=None, beats={})
    assert order == ["sbr", "mesh"]
    assert out == galaxy.OUTCOME_RECOVERED and cycles["n"] == 0, "a mesh the sweep recovered must not be power-cycled"


@pytest.mark.asyncio
async def test_the_idle_escalation_road_to_a_power_cycle_goes_through_the_sweep():
    """Wiring test for the host-rung road the gate ladder does not own: the idle/stuck-hold
    escalation (`stuck_hold_host_escalation` — the road MOST real power cycles took). It must pass
    through the last-chance sweep; before ladder-v2 it climbed to the host rung having only ever
    re-tried the mesh reset. There is no post-reboot road: the broker has no reboot rung."""
    src = pathlib.Path(galaxy.__file__).read_text()
    fn = "_climb_to_host_recovery_after_failed_reset"
    start = src.index(f"async def {fn}(")
    nxt = [i for i in (src.find("\n    async def ", start + 10), src.find("\n    def ", start + 10)) if i != -1]
    body = src[start : min(nxt) if nxt else len(src)]
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
# box B, 2026-09-17 10:07:09Z: the stuck-hold escalation read an empty holder scan 106 s into a
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


def test_the_i75_stand_down_blocks_only_inside_the_escalate_that_carries_it(monkeypatch):
    """Spec 03 I75 (review F3): the caller's stand-down is re-read at the ladder's tenant check, where a
    re-adopted job that ended mid-run would otherwise let a reset through ahead of the startup gate. A
    running job still names itself first (job_running), and outside an escalate nothing is blocked."""
    g = srv.galaxy_recovery
    monkeypatch.setattr(g, "_stand_down", lambda: True)
    monkeypatch.setattr(g, "startup_verify_deferred", False)
    monkeypatch.setattr(g.deps, "job_running", lambda: True)
    assert g._tenant_blocker(_empty_scan(), force=True) == {"blocker": "job_running"}
    assert g.startup_verify_deferred is False
    monkeypatch.setattr(g.deps, "job_running", lambda: False)
    assert g._tenant_blocker(_empty_scan(), force=True) == {"blocker": "startup_verify"}
    assert g.startup_verify_deferred is True
    assert "startup gate" in g._describe_blocker({"blocker": "startup_verify"})
    monkeypatch.setattr(g, "_stand_down", None)
    assert g._tenant_blocker(_empty_scan(), force=True) is None


@pytest.mark.asyncio
async def test_escalate_carries_the_stand_down_only_for_its_own_run(monkeypatch):
    """The stand-down an escalate() call carries is cleared when it returns, whatever the outcome."""
    g = srv.galaxy_recovery
    seen = []

    async def ladder(indices, expected, log, *, force=False, measured_fault=False):
        seen.append(g._stand_down() if g._stand_down else None)
        return False

    monkeypatch.setattr(g, "_escalate_stuck_hold", ladder)
    monkeypatch.setattr(g.mechanism, "host_action_blocks_reset", lambda log, context: False)
    monkeypatch.setattr(g, "_td_reset_seen", True)

    async def no_latch(expected, beats):
        return None

    async def no_direct(*a, **_):
        return None

    monkeypatch.setattr(g, "_tray_down_latch", no_latch)
    monkeypatch.setattr(g, "_power_cycle_direct_from_escalate", no_direct)
    monkeypatch.setattr(g.deps, "device_degraded", lambda: True)
    assert (
        await g.escalate("present-forced", ["0"], 32, lambda m: None, stand_down=lambda: True) == galaxy.OUTCOME_WAITING
    )
    assert seen == [True] and g._stand_down is None


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
