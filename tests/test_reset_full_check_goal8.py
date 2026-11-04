# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Every reset path runs the full health check before release (spec 04 I32).

Every reset path runs reset -> the full probe pass, fabric included -> release, and a fabric FAIL
keeps the hold. The startup check waits for the hugepage pool.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold, _arm_stuck_hold, _restart_broker
from tests.test_post_reset_gate import _REAL_CLEAR_DIRTY
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health import post_reset
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base

_REAL_VERIFY_ON_START = srv._verify_fabric_on_start
FABRIC_OK = (True, "fabric links healthy")
FABRIC_FAIL = (False, "chip 17: 2 eth links down")


class _Rig:
    """One 32-chip host: the real probe pass (HealthMonitor.update) and the real
    reset_with_quiesce, with every probe and the reset command stubbed to record their order."""

    def __init__(self):
        self.order: list[str] = []
        self.fabric = FABRIC_OK

    def released(self, _record=None):
        self.order.append("release")

    def after_last_reset(self) -> list[str]:
        resets = [i for i, e in enumerate(self.order) if e == "reset"]
        assert resets, f"no reset ran: {self.order}"
        return self.order[resets[-1] + 1 :]


@pytest.fixture
def rig(monkeypatch, tmp_path, clear_job_state):
    r = _Rig()
    for i in range(32):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(post_reset, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    srv.recovery_mechanism.last_reset_monotonic = 0.0
    srv.recovery_mechanism.last_reset_failed = False

    # The probe pass: host PCI skips, no sysfs heartbeat, the snapshot passes, eth unconfigured.
    monkeypatch.setattr(monitor_mod.hostpci, "host_pci_verdict", lambda: (None, "skipped", {}))
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: False)

    async def snapshot(expected):
        r.order.append("snapshot")
        return True, f"{expected} chip(s)"

    async def eth(timeout_sec=60.0):
        r.order.append("eth")
        return None, "not configured"

    async def fabric_pass(*a, **k):
        r.order.append("fabric")
        return r.fabric

    monkeypatch.setattr(srv.health_monitor, "_verify_device", snapshot)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth)
    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", fabric_pass)

    # The reset command and the rescan after it: recorded, instant.
    async def run_scoped(argv, log, owner="[broker]health-gate", on_output=None):
        r.order.append("reset")
        return 0, ""

    async def pollers(active, log):
        return [] if active else ["telem"]

    class _Asyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(_):
            return None

        @staticmethod
        async def to_thread(fn, *a, **k):
            if fn is base._scoped_rescan:
                return None
            return await asyncio.to_thread(fn, *a, **k)

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run_scoped)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(base, "asyncio", _Asyncio())
    return r


def _real_recovery(monkeypatch):
    """Undo the helpers' recovery fakes so the real reset + verify (and probe pass) run."""
    for name in ("_reset_and_verify_device", "_verify_device"):
        for inst in (srv.galaxy_recovery, srv.per_target_recovery):
            if name in vars(inst):
                monkeypatch.delattr(inst, name)
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)


# --- each reset path, driven from its own entry point ---------------------------------------


async def _operator_reset(r, monkeypatch):
    fsm_dirty(srv, "operator reset", why="job_killed")
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-glx_reset"], lambda m: None)
    assert rc == 0
    await srv._verify_operator_reset(32, srv.galaxy_recovery, lambda m: None, "operator reset")


async def _ladder_glx_reset(r, monkeypatch):
    fsm_dirty(srv, "gate/post-job: eth core frozen", why="job_killed")
    await srv.galaxy_recovery._fire_gate_rung(
        "post-job", recovery_pkg.STAGE_SMI_RESET, [str(i) for i in range(32)], 32, lambda m: None, ev=None, beats=None
    )


async def _bridge_rung(r, monkeypatch):
    fsm_dirty(srv, "gate/post-job: chip 8 off the bus", why="job_killed")

    async def bridge(log):
        FABRIC_DEBT.note_reset("bridge reset of chip 8")
        r.order.append("reset")
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", bridge)
    await srv.galaxy_recovery._fire_gate_rung(
        "post-job", recovery_pkg.STAGE_BRIDGE_RESET, ["8"], 32, lambda m: None, ev=None, beats=None
    )


async def _idle_offbus_escalation(r, monkeypatch):
    _arm_offbus_stuck_hold(monkeypatch, isolated={"8"})
    _real_recovery(monkeypatch)
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="job_killed")

    async def bridge(log):
        FABRIC_DEBT.note_reset("bridge reset of chip 8")
        r.order.append("reset")
        return True

    patch_recovery(monkeypatch, "_recover_isolated_chips", bridge)
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)


async def _hold_deadline_escalation(r, monkeypatch):
    """The hold deadline's forced climb on a present mesh: the gate's own galaxy reset."""
    _arm_stuck_hold(monkeypatch)
    _real_recovery(monkeypatch)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", _record_reset(r))
    fsm_dirty(srv, "gate/post-job: eth-core heartbeat frozen", why="job_killed")
    assert await srv.galaxy_recovery._escalate_stuck_hold(["0", "1"], 32, lambda m: None, force=True) is True


async def _startup(r, monkeypatch, tmp_path):
    """After a boot or a broker restart: the start is the reset (FABRIC_DEBT 'broker start')."""
    await _restart_broker(monkeypatch, tmp_path / "fsm.json")
    srv.fsm.on_next_close(r.released)
    r.order.append("reset")
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    await _REAL_VERIFY_ON_START()


def _record_reset(r):
    async def run_scoped(argv, log, owner="[broker]health-gate", on_output=None):
        r.order.append("reset")
        return 0, ""

    return run_scoped


PATHS = {
    "operator-reset": _operator_reset,
    "ladder-glx_reset": _ladder_glx_reset,
    "bridge-rung": _bridge_rung,
    "idle-offbus-escalation": _idle_offbus_escalation,
    "hold-deadline-escalation": _hold_deadline_escalation,
    "startup": None,  # driven by _drive: needs tmp_path
}


async def _drive(path, r, monkeypatch, tmp_path):
    if path == "startup":
        await _startup(r, monkeypatch, tmp_path)
    else:
        srv.fsm.on_next_close(r.released)
        await PATHS[path](r, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(PATHS))
async def test_every_reset_path_runs_the_full_fabric_pass_then_releases(rig, monkeypatch, tmp_path, path):
    await _drive(path, rig, monkeypatch, tmp_path)
    after = rig.after_last_reset()
    assert "fabric" in after, f"no fabric pass after the reset: {rig.order}"
    assert after[-1] == "release" and after.index("fabric") < after.index("release"), rig.order
    assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(PATHS))
async def test_fabric_fail_after_any_reset_keeps_the_hold_and_enters_bounded_recovery(rig, monkeypatch, tmp_path, path):
    """A fabric FAIL after the reset never releases: the device stays held with the
    fabric still owed, and the hold-deadline watchdog forces the recovery ladder past its ceiling."""
    rig.fabric = FABRIC_FAIL
    await _drive(path, rig, monkeypatch, tmp_path)
    after = rig.after_last_reset()
    assert "fabric" in after and "release" not in rig.order, rig.order
    assert srv.fsm.state is ServerState.RECOVERING
    assert FABRIC_DEBT.blocks_release(32), "a failed post-reset pass must leave the fabric owed"
    assert not srv._fabric_77_only_hold(), "a link-down FAIL is not a 77: the watchdog may force a reset"

    forced = []

    async def force():
        forced.append(srv.device_hold_episode_since)

    monkeypatch.setattr(srv, "_force_escalate_stuck_hold", force)
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "_hold_episode_tenant", "")
    since = datetime.now() - timedelta(seconds=srv._stuck_hold_ceiling_sec() + 60)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    srv._check_hold_deadline()
    await asyncio.sleep(0)
    assert forced, "a held box past the ceiling must get the forced recovery ladder"


# --- startup waits for the hugepage pool before the fabric pass -----------------------------


def _hugepages(monkeypatch, r, seq):
    seq = list(seq)

    def shortfall():
        short = seq.pop(0) if len(seq) > 1 else seq[0]
        r.order.append("hugepages-short" if short else "hugepages-ready")
        return short

    monkeypatch.setattr(srv, "_hugepages_shortfall", shortfall)
    monkeypatch.setattr(srv, "HUGEPAGES_POLL_SEC", 0.01)


@pytest.mark.asyncio
async def test_startup_waits_for_hugepages_then_full_fabric_pass(rig, monkeypatch, tmp_path):
    """Startup before the 1G pool is ready: hugepage wait -> full fabric pass, with the
    queue closed (startup hold) until that pass has passed."""
    await _restart_broker(monkeypatch, tmp_path / "fsm.json")
    assert srv.fsm.state is ServerState.RECOVERING  # the startup hold: the door is closed
    srv.fsm.on_next_close(rig.released)
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    _hugepages(monkeypatch, rig, [(0, 32), (16, 32), None])
    door = []
    real_fabric = srv.health_monitor.verify_fabric_health

    async def fabric_pass(*a, **k):
        door.append(srv.fsm.state)
        return await real_fabric(*a, **k)

    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", fabric_pass)
    await _REAL_VERIFY_ON_START()

    o = rig.order
    ready = o.index("hugepages-ready")
    assert "fabric" not in o[:ready], f"fabric pass before hugepages: {o}"
    assert ready < o.index("fabric") < o.index("release"), o
    assert door == [ServerState.RECOVERING], "the queue opened before the startup fabric pass passed"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_startup_without_hugepages_stays_held(rig, monkeypatch, tmp_path):
    await _restart_broker(monkeypatch, tmp_path / "fsm.json")
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.03)
    monkeypatch.setattr(srv, "_await_startup_prerequisites", lambda missing: asyncio.sleep(0))
    _hugepages(monkeypatch, rig, [(0, 32)])
    await _REAL_VERIFY_ON_START()
    assert "fabric" not in rig.order, rig.order
    assert srv.fsm.state is ServerState.RECOVERING


# --- every startup hugepage path leaves one startup_hugepages record (spec 03 I33) -----------


def _record_events(monkeypatch, counts=(32, 32)):
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "health_hugepages_counts", lambda: counts)
    return events


def _startup_hugepages(events):
    got = [f for kind, f in events if kind == "startup_hugepages"]
    assert len(got) == 1, f"want exactly one startup_hugepages record: {events}"
    return got[0]


@pytest.mark.asyncio
async def test_startup_hugepages_ready_after_wait_is_recorded_and_released(rig, monkeypatch, tmp_path):
    await _restart_broker(monkeypatch, tmp_path / "fsm.json")
    srv.fsm.on_next_close(rig.released)
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    _hugepages(monkeypatch, rig, [(0, 32), (16, 32), None])
    events = _record_events(monkeypatch, counts=(32, 32))
    await _REAL_VERIFY_ON_START()
    ev = _startup_hugepages(events)
    assert ev["outcome"] == "ready" and ev["need"] == 32 and ev["have"] == 32 and ev["waited_s"] >= 0, ev
    o = rig.order
    assert o.index("hugepages-ready") < o.index("fabric") < o.index("release"), o
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_startup_hugepages_short_is_recorded_and_still_held(rig, monkeypatch, tmp_path):
    await _restart_broker(monkeypatch, tmp_path / "fsm.json")
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.03)
    monkeypatch.setattr(srv, "_await_startup_prerequisites", lambda missing: asyncio.sleep(0))
    _hugepages(monkeypatch, rig, [(0, 32)])
    events = _record_events(monkeypatch)
    await _REAL_VERIFY_ON_START()
    ev = _startup_hugepages(events)
    assert ev == {"outcome": "short_held", "need": 32, "have": 0, "waited_s": ev["waited_s"]}, ev
    assert ev["waited_s"] >= 0
    assert "fabric_check_unavailable" in [k for k, _ in events], "the cannot-check record is unchanged"
    assert "fabric" not in rig.order and "release" not in rig.order, rig.order
    assert srv.fsm.state is ServerState.RECOVERING
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY, "the short path still holds fabric-unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counts, outcome, reason",
    [
        ((0, 0), "not_needed", "no function needs a 1G page"),
        ((None, 4), "not_needed", "1G pool unreadable"),
        ((4, 4), "ready", None),
    ],
    ids=["need-0", "pool-unreadable", "already-full"],
)
async def test_startup_hugepages_no_wait_is_recorded(rig, monkeypatch, counts, outcome, reason):
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    events = _record_events(monkeypatch, counts=counts)
    assert await srv._await_startup_hugepages() is True
    ev = _startup_hugepages(events)
    assert (ev["outcome"], ev["have"], ev["need"], ev["waited_s"], ev.get("reason")) == (
        outcome,
        counts[0],
        counts[1],
        0.0,
        reason,
    ), ev


@pytest.mark.asyncio
async def test_startup_hugepages_opt_out_is_recorded_and_never_waits(rig, monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_EXPECTED_CHIPS")
    monkeypatch.setattr(srv, "health_hugepages_shortfall", lambda: (0, 32))  # ignored: opted out
    events = _record_events(monkeypatch, counts=(0, 32))
    assert await srv._await_startup_hugepages() is True
    ev = _startup_hugepages(events)
    assert ev["outcome"] == "not_needed" and ev["waited_s"] == 0.0, ev
    assert "TT_DEVICE_MCP_EXPECTED_CHIPS" in ev["reason"], ev


@pytest.mark.asyncio
async def test_startup_hugepages_short_with_a_dirty_episode_is_recorded_and_runs_the_gate(rig, monkeypatch):
    fsm_dirty(srv, "job 12 crashed", why="job_killed")
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.03)
    _hugepages(monkeypatch, rig, [(0, 32)])
    events = _record_events(monkeypatch)
    assert await srv._await_startup_hugepages() is True, "a dirty episode still goes to the gate"
    ev = _startup_hugepages(events)
    assert ev["outcome"] == "short_dirty" and (ev["have"], ev["need"]) == (0, 32), ev
    assert "fabric_check_unavailable" not in [k for k, _ in events]
    assert srv.fsm.record.dirty, "the owed reset is not dropped"
