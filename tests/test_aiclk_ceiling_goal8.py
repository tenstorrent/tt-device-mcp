# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""An armed AICLK ceiling never stands in for the health check (spec 03 I35, 04 I32).

With a ceiling configured, every reset path still runs reset -> ceiling -> the full probe pass,
fabric included -> release, and a fabric FAIL keeps the hold. The startup ceiling waits for the
hugepage pool; a disarmed ceiling still runs the full pass; a job's own fmax restore is capped
again before the post-job traffic pass; the added time stays within ~10 s per pass.
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold, _arm_stuck_hold, _restart_broker
from tests.test_post_reset_gate import _REAL_CLEAR_DIRTY
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import aiclk_ceiling as ac
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health import post_reset
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.aiclk_ceiling import CEILING
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base, galaxy

_REAL_VERIFY_ON_START = srv._verify_fabric_on_start
FABRIC_OK = (True, "fabric links healthy")
FABRIC_FAIL = (False, "chip 17: 2 eth links down")


@pytest.fixture(autouse=True)
def _fresh_ceiling(monkeypatch):
    for k in (ac.ENV_MHZ, ac.ENV_CMD, ac.ENV_TIMEOUT, ac.ENV_PYTHON, ac.ENV_SETTLE):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(CEILING, "owed", True)
    monkeypatch.setattr(CEILING, "disarmed", "")
    monkeypatch.setattr(CEILING, "last", {})
    monkeypatch.setattr(CEILING, "proc", None)
    monkeypatch.setattr(ac, "health_event", lambda kind, **f: None)


class _Rig:
    """One armed 32-chip host: the real probe pass (HealthMonitor.update) and the real
    reset_with_quiesce, with every probe and the reset command stubbed to record their order."""

    def __init__(self):
        self.order: list[str] = []
        self.fabric = FABRIC_OK
        self.ceiling: list = []  # canned apply verdicts (True/False/None), then True

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
    monkeypatch.setenv(ac.ENV_MHZ, "800")
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

    async def apply(where, *, log=None, terminate=None, settle=0.0):
        r.order.append(f"ceiling:{where}")
        ok = r.ceiling.pop(0) if r.ceiling else True
        if ok:
            CEILING.owed = False
        detail = {True: "ceiling 800 MHz applied to 32/32", False: "NOT verified", None: "not armed"}[ok]
        return ok, detail, {}

    monkeypatch.setattr(CEILING, "apply", apply)

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
            if getattr(fn, "__self__", None) == Path("/sys/bus/pci/rescan"):
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


async def _ubb_tray_sweep(r, monkeypatch):
    fsm_dirty(srv, "gate/post-job: tray 3 off the bus", why="job_killed")
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "_clear_device_reported_fault", lambda why: None)

    async def no_bridge(log):
        return False

    def ubb(bitmap, ids):
        r.order.append("reset")

    patch_recovery(monkeypatch, "_recover_isolated_chips", no_bridge)
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", ubb)
    offbus = {str(i) for i in range(24, 32)}
    await g._fire_tray_down_no_window(offbus, 32, lambda m: None, gate_phase="post-job", ev=None)


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
    "ubb-tray-sweep": _ubb_tray_sweep,
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
async def test_every_reset_path_runs_ceiling_then_full_fabric_pass_then_releases(rig, monkeypatch, tmp_path, path):
    await _drive(path, rig, monkeypatch, tmp_path)
    after = rig.after_last_reset()
    assert "fabric" in after, f"no fabric pass after the reset: {rig.order}"
    ceilings = [i for i, e in enumerate(after) if e.startswith("ceiling:")]
    assert ceilings and ceilings[0] < after.index("fabric"), f"traffic ran before the ceiling: {rig.order}"
    assert after[-1] == "release" and after.index("fabric") < after.index("release"), rig.order
    assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(PATHS))
async def test_fabric_fail_after_any_reset_keeps_the_hold_and_enters_bounded_recovery(rig, monkeypatch, tmp_path, path):
    """A fabric FAIL after the reset (and its ceiling) never releases: the device stays held with the
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
    monkeypatch.setattr(srv, "_maybe_alert_stuck_hold", lambda *a: None)
    since = datetime.now() - timedelta(seconds=srv._stuck_hold_ceiling_sec() + 60)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    srv._check_hold_deadline()
    await asyncio.sleep(0)
    assert forced, "a held box past the ceiling must get the forced recovery ladder"


# --- startup waits for the hugepage pool before the ceiling ---------------------------------


def _hugepages(monkeypatch, r, seq):
    seq = list(seq)

    def shortfall():
        short = seq.pop(0) if len(seq) > 1 else seq[0]
        r.order.append("hugepages-short" if short else "hugepages-ready")
        return short

    monkeypatch.setattr(srv, "_hugepages_shortfall", shortfall)
    monkeypatch.setattr(srv, "HUGEPAGES_POLL_SEC", 0.01)


@pytest.mark.asyncio
async def test_startup_waits_for_hugepages_then_ceiling_then_full_fabric_pass(rig, monkeypatch, tmp_path):
    """Startup before the 1G pool is ready: hugepage wait -> ceiling -> full fabric pass, with the
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
    assert "ceiling:startup" in o and ready < o.index("ceiling:startup"), f"ceiling before hugepages: {o}"
    assert not any(e.startswith("ceiling:") or e == "fabric" for e in o[:ready]), o
    assert o.index("ceiling:startup") < o.index("fabric") < o.index("release"), o
    assert door == [ServerState.RECOVERING], "the queue opened before the startup fabric pass passed"
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_startup_without_hugepages_applies_no_ceiling_and_stays_held(rig, monkeypatch, tmp_path):
    await _restart_broker(monkeypatch, tmp_path / "fsm.json")
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.03)
    monkeypatch.setattr(srv, "_await_startup_prerequisites", lambda missing: asyncio.sleep(0))
    _hugepages(monkeypatch, rig, [(0, 32)])
    await _REAL_VERIFY_ON_START()
    assert not any(e.startswith("ceiling:") or e == "fabric" for e in rig.order), rig.order
    assert srv.fsm.state is ServerState.RECOVERING


# --- a disarmed ceiling still runs the full pass ---------------------------------------------


def _helper_cannot_run_here(monkeypatch, r):
    """The real CEILING.apply against a helper that exits 77 (wrong architecture, no tt-umd)."""
    monkeypatch.delattr(CEILING, "apply")
    clock = [100.0]

    async def run_probe(argv, *, timeout_sec, track, stream, env, terminate, name=""):
        r.order.append("ceiling-helper")
        return ac.UNSUPPORTED_RC, b"cannot run here"

    async def sleep(d):
        clock[0] += d

    monkeypatch.setattr(ac, "run_probe", run_probe)
    monkeypatch.setattr(ac, "_now", lambda: clock[0])
    monkeypatch.setattr(ac, "_sleep", sleep)


@pytest.mark.asyncio
@pytest.mark.parametrize("fabric", [FABRIC_OK, FABRIC_FAIL], ids=["fabric-ok", "fabric-fail"])
@pytest.mark.parametrize("path", list(PATHS))
async def test_disarmed_ceiling_still_runs_the_full_fabric_pass(rig, monkeypatch, tmp_path, path, fabric):
    """A ceiling that disarms itself (helper exit 77) is a skipped probe, never a skipped check:
    the full pass runs uncapped after the reset and releases only on a fabric OK."""
    _helper_cannot_run_here(monkeypatch, rig)
    rig.fabric = fabric
    await _drive(path, rig, monkeypatch, tmp_path)
    assert CEILING.disarmed == "cannot run here" and not CEILING.armed()
    after = rig.after_last_reset()
    assert "fabric" in after, f"a disarmed ceiling skipped the fabric pass: {rig.order}"
    if fabric == FABRIC_OK:
        assert after[-1] == "release" and after.index("fabric") < after.index("release"), rig.order
        assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""
    else:
        assert "release" not in rig.order, rig.order
        assert srv.fsm.state is ServerState.RECOVERING
        assert FABRIC_DEBT.blocks_release(32)


# --- a job that restores fmax at exit is capped again before the post-job traffic -----------


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", ["job-failed", "reset-debt"])
async def test_job_restoring_fmax_is_capped_again_before_post_job_traffic(rig, monkeypatch, forced):
    monkeypatch.delattr(CEILING, "apply")
    fmax = {"mhz": 800}

    async def run_probe(argv, *, timeout_sec, track, stream, env, terminate, name=""):
        rig.order.append("ceiling-helper")
        fmax["mhz"] = int(env[ac.ENV_MHZ])
        return 0, b'{"summary": true, "mhz": 800, "chips": 32, "ok": 32, "sent": 32}\n'

    async def fabric_pass(*a, **k):
        rig.order.append("fabric")
        assert fmax["mhz"] == 800, "fabric traffic ran with the job's restored 1350 MHz"
        return FABRIC_OK

    monkeypatch.setattr(ac, "run_probe", run_probe)
    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", fabric_pass)
    CEILING.owed = False
    fmax["mhz"] = 1350  # the job's exit restores the firmware default
    if forced == "reset-debt":
        FABRIC_DEBT.note_reset("job-internal reset")
    await srv._verify_device_after_job(None, job_failed=forced == "job-failed")
    o = rig.order
    assert "fabric" in o, f"no forced traffic pass: {o}"
    assert "ceiling-helper" in o and o.index("ceiling-helper") < o.index("fabric"), o
    assert fmax["mhz"] == 800 and CEILING.owed is False


# --- time bound: the ceiling adds <= ~10 s per pass ------------------------------------------

_OVER = b'{"summary": true, "mhz": 800, "chips": 32, "ok": 0, "sent": 32, "over": [0]}\n'
_AT = b'{"summary": true, "mhz": 800, "chips": 32, "ok": 32, "sent": 0}\n'
_HUNG = (None, b"", ac.DEFAULT_TIMEOUT_SEC)  # killed at the helper timeout


def _timed_helper(monkeypatch, run):
    """Fake clock and helper: ``run(offset)`` gives (rc, output, seconds) for a run that starts
    ``offset`` seconds into the apply. Returns the clock."""
    clock = [100.0]

    async def run_probe(argv, *, timeout_sec, track, stream, env, terminate, name=""):
        rc, out, sec = run(clock[0] - 100.0)
        clock[0] += sec
        return rc, out

    async def sleep(d):
        clock[0] += d

    monkeypatch.setenv(ac.ENV_MHZ, "800")
    monkeypatch.setattr(ac, "run_probe", run_probe)
    monkeypatch.setattr(ac, "_now", lambda: clock[0])
    monkeypatch.setattr(ac, "_sleep", sleep)
    return clock


_TIME_CASES = {
    "gate-pass-hung": (0.0, lambda t: _HUNG),
    "gate-pass-realistic": (0.0, lambda t: (0, _AT, 0.4)),
    "post-reset-realistic-over": (ac.DEFAULT_SETTLE_SEC, lambda t: (1, _OVER, 0.4)),
    "post-reset-realistic-settles": (ac.DEFAULT_SETTLE_SEC, lambda t: (1, _OVER, 0.4) if t < 2 else (0, _AT, 0.4)),
    "post-reset-first-run-hung": (ac.DEFAULT_SETTLE_SEC, lambda t: _HUNG),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settle", "run"),
    [pytest.param(*v, id=k) for k, v in _TIME_CASES.items()]
    + [
        pytest.param(
            ac.DEFAULT_SETTLE_SEC,
            lambda t: _HUNG if t >= ac.DEFAULT_SETTLE_SEC - ac.SETTLE_POLL_SEC else (1, _OVER, 0.0),
            id="post-reset-late-retry-hung",
            marks=pytest.mark.xfail(
                strict=True,
                reason="known corner: a settle retry started at the window end (3 s) that hangs to the "
                "8 s helper timeout makes the post-reset apply ~11 s; fixed separately",
            ),
        )
    ],
)
async def test_ceiling_apply_adds_at_most_10s(monkeypatch, settle, run):
    clock = _timed_helper(monkeypatch, run)
    await CEILING.apply("post-reset" if settle else "pre-job", settle=settle)
    assert clock[0] - 100.0 <= 10.0, f"the ceiling added {clock[0] - 100.0:.1f}s"
