"""A hold placed because a process outside the broker holds the device is bounded (spec 03 I30).

The broker polls for the holder's exit on its own short clock; once it is gone, the full gate
(fabric included) must pass before the queue reopens, and a failed check goes to the recovery
ladder, never to a release. Past a deadline the broker names the holder once and keeps waiting: it
never signals, kills or resets under it. Every release records the time it took."""

import time

import pytest

from tests.conftest import fsm_dirty, fsm_healthy, patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState

HOLDER = DeviceHolder(pid=124766, uid=1000)


@pytest.fixture
def box(monkeypatch, tmp_path, clear_job_state):
    """An idle one-chip box with the device lock, events, holder scan and gate probes stubbed."""
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 1)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv, "_holder_cmd", lambda pid: "tt-smi")
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", 0.0)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_failed", False)
    srv.device_op_lock = None
    srv.device_op_active = ""
    srv.last_relift_monotonic = 0.0
    srv.last_foreign_poll_monotonic = 0.0
    srv.foreign_hold.clear()
    srv.last_foreign_hold_recovery.clear()

    state = {"scan": HolderScan(holders=[HOLDER], complete=True), "verify": [], "resets": 0, "kills": 0}
    state["verdict"] = (True, {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "links healthy"}})
    state["events"] = []
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: state["scan"])
    patch_health_event(monkeypatch, lambda kind, **f: state["events"].append((kind, f)))

    async def verify(expected, log, run_fabric=True, **_):
        state["verify"].append(run_fabric)
        return state["verdict"]

    async def reset(indices, log):
        state["resets"] += 1
        return False

    async def no_kill(*a, **k):
        state["kills"] += 1
        return []

    patch_recovery(monkeypatch, "_verify_device", verify)
    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    monkeypatch.setattr(srv, "_kill_device_holders", no_kill)
    monkeypatch.setattr(srv.os, "kill", lambda *a: state.__setitem__("kills", state["kills"] + 1))
    yield state
    srv.foreign_hold.clear()
    srv.last_foreign_hold_recovery.clear()


def _kinds(state, kind):
    return [f for k, f in state["events"] if k == kind]


async def _poll():
    """One sampler-driven pass, past the poll interval."""
    srv.last_foreign_poll_monotonic = 0.0
    await srv._attempt_idle_relift()


@pytest.mark.asyncio
async def test_a_foreign_holder_on_a_healthy_device_is_reset_debt(box):
    """A sighting holds the door even on a device that read HEALTHY: nobody knows what the process
    did to the mesh. Fails on base, where an unheld device stays HEALTHY beside the holder."""
    fsm_healthy(srv)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)

    assert srv.fsm.state is ServerState.RECOVERING, "a foreign holder must hold the queue, healthy or not"
    assert srv.fsm.record.why == "foreign_holder" and not srv.fsm.record.dirty
    assert box["verify"] == [], "nothing may probe the device beside a foreign process"
    held = _kinds(box, "foreign_holder_hold")
    assert held and held[0]["holders"] == [{"pid": 124766, "uid": 1000, "user": HOLDER.username, "cmd": "tt-smi"}]


@pytest.mark.asyncio
async def test_holder_exits_then_the_fabric_check_passes_and_the_device_is_released(box):
    """Once the holder is gone the next poll runs the full gate with the fabric pass and lifts the
    hold only on its healthy verdict, recording the time to recover. Fails on base, where the hold
    waits for the 600 s ceiling and then resets instead of checking."""
    fsm_healthy(srv)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    srv.foreign_hold["since"] = time.time() - 42

    await _poll()  # holder still there: no probe
    assert box["verify"] == [] and srv.fsm.state is not ServerState.HEALTHY

    box["scan"] = HolderScan(holders=[], complete=True)
    await _poll()

    assert box["verify"] == [True], "the release check must include the fabric traffic pass"
    assert box["resets"] == 0
    assert srv.fsm.state is ServerState.HEALTHY, "a passed full check must release the device"
    rec = _kinds(box, "foreign_holder_recovered")
    assert len(rec) == 1 and rec[0]["time_to_recover_sec"] >= 42
    assert srv._health_payload()["last_foreign_hold_recovery"]["time_to_recover_sec"] >= 42
    assert srv.foreign_hold == {}, "the tracked hold must close with the release"


@pytest.mark.asyncio
async def test_holder_exits_but_the_check_fails_so_the_device_stays_held_for_the_ladder(box, monkeypatch):
    """A failed check after the holder left never releases: the gate hands the device to the
    recovery ladder (a reset here), and the hold stands while that fails."""
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n: 32)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {"0": 100})  # 31 of 32 off the bus: the ladder resets
    fsm_dirty(srv, "foreign holder present: someone(pid 124766)", why="foreign_holder", dirty=False)
    box["scan"] = HolderScan(holders=[], complete=True)
    box["verdict"] = (False, {"snapshot": {"ok": False, "detail": "chips did not enumerate"}})

    await _poll()

    assert box["verify"] and box["verify"][0] is True, "the check must have run with the fabric pass"
    assert box["resets"] >= 1, "a failed check must go to the recovery ladder"
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed check must never release the device"
    assert _kinds(box, "foreign_holder_check_failed")
    assert _kinds(box, "foreign_holder_recovered") == []
    assert srv.foreign_hold, "time to recover still runs until the device is released"


@pytest.mark.asyncio
async def test_past_the_deadline_one_escalation_names_the_holder_and_nothing_is_killed(box, monkeypatch):
    """Past TT_DEVICE_MCP_FOREIGN_HOLDER_DEADLINE_SEC the broker escalates once, naming pid, user and
    command, then keeps polling. It never kills, signals, probes or resets under the holder."""
    monkeypatch.setenv("TT_DEVICE_MCP_FOREIGN_HOLDER_DEADLINE_SEC", "60")
    fsm_healthy(srv)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    srv.foreign_hold["since"] = time.time() - 61

    for _ in range(3):
        await _poll()

    esc = _kinds(box, "foreign_holder_past_deadline")
    assert len(esc) == 1, "one escalation per hold, not one per poll"
    assert esc[0]["holders"][0]["pid"] == 124766 and esc[0]["holders"][0]["cmd"] == "tt-smi"
    assert esc[0]["deadline_sec"] == 60
    assert box["kills"] == 0 and box["resets"] == 0 and box["verify"] == []
    assert srv.fsm.record.why == "foreign_holder", "the hold stands while the holder is there"
    assert srv._health_payload()["foreign_holder"]["escalated"] is True

    box["scan"] = HolderScan(holders=[], complete=True)
    await _poll()
    assert srv.fsm.state is ServerState.HEALTHY, "polling continues past the deadline and releases on exit"


@pytest.mark.asyncio
async def test_a_stale_holder_record_clears_on_the_next_poll(box):
    """A hold whose named pid is already gone (e.g. carried across a broker restart) clears on the
    next short poll, not on the 120 s self-heal clock or the 600 s ceiling."""
    fsm_dirty(srv, "held: someone(pid 124766) holds the device", why="foreign_holder", dirty=False)
    box["scan"] = HolderScan(holders=[], complete=True)
    srv.last_relift_monotonic = time.monotonic()  # the self-heal clock just ran: it must not gate this

    await _poll()

    assert box["verify"] == [True]
    assert srv.fsm.state is ServerState.HEALTHY
    assert len(_kinds(box, "foreign_holder_recovered")) == 1


@pytest.mark.asyncio
async def test_the_poll_runs_on_its_own_short_interval(box, monkeypatch):
    """Polls inside TT_DEVICE_MCP_FOREIGN_HOLDER_POLL_SEC do not re-scan; the next one past it does."""
    monkeypatch.setenv("TT_DEVICE_MCP_FOREIGN_HOLDER_POLL_SEC", "10")
    fsm_dirty(srv, "held: someone(pid 124766) holds the device", why="foreign_holder", dirty=False)
    scans = {"n": 0}

    def scan():
        scans["n"] += 1
        return HolderScan(holders=[HOLDER], complete=True)

    monkeypatch.setattr(srv, "enumerate_device_holders", scan)
    await _poll()
    await srv._attempt_idle_relift()
    assert scans["n"] == 1
    srv.last_foreign_poll_monotonic = time.monotonic() - 11
    await srv._attempt_idle_relift()
    assert scans["n"] == 2


def test_foreign_holder_timings_default_and_parse(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_FOREIGN_HOLDER_DEADLINE_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FOREIGN_HOLDER_POLL_SEC", raising=False)
    assert srv._foreign_holder_deadline_sec() == 900
    assert srv._foreign_holder_poll_sec() == 10
    monkeypatch.setenv("TT_DEVICE_MCP_FOREIGN_HOLDER_DEADLINE_SEC", "junk")
    monkeypatch.setenv("TT_DEVICE_MCP_FOREIGN_HOLDER_POLL_SEC", "0")
    assert srv._foreign_holder_deadline_sec() == 900
    assert srv._foreign_holder_poll_sec() == 1


# --- an off-bus / stuck hold whose escalation a tenant blocks names the blocker (spec 03 I30) ---


@pytest.fixture
def offbus_blocked(monkeypatch, clear_job_state):
    """An idle off-bus hold past the ceiling with an interactive tt-smi holding the device."""
    from tests.test_device_safety import _arm_offbus_stuck_hold

    # The helper assigns these module globals directly: register them so monkeypatch restores them.
    for name in ("isolated_chips", "device_hold_episode_reason", "device_hold_episode_since"):
        monkeypatch.setattr(srv, name, getattr(srv, name))
    counters = _arm_offbus_stuck_hold(monkeypatch, isolated={"8"})
    counters["kills"] = 0
    counters["events"] = []
    fsm_dirty(srv, "gate/post-job: 1/32 off-bus below the reset floor", why="off_bus", dirty=False)
    holder = DeviceHolder(pid=124766, uid=srv.MIN_TENANT_UID)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[holder], complete=True))
    monkeypatch.setattr(srv, "_holder_cmd", lambda pid: "tt-smi")
    monkeypatch.setattr(srv, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(srv.os, "kill", lambda *a: counters.__setitem__("kills", counters["kills"] + 1))
    patch_health_event(monkeypatch, lambda kind, **f: counters["events"].append((kind, f)))
    srv.hold_blocked_by.clear()
    yield counters
    srv.hold_blocked_by.clear()


@pytest.mark.asyncio
async def test_a_blocked_offbus_escalation_names_the_holder_in_health(offbus_blocked):
    """blx01 10-14: an interactive tt-smi held an off-bus hold's escalation for 3 h 27 min, and
    /health said nothing about who. /health now exposes blocked_by with pid, user and command."""
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)

    assert ran is False and offbus_blocked["resets"] == 0 and offbus_blocked["kills"] == 0
    blocked = srv._health_payload()["blocked_by"]
    assert blocked["why"] == "off_bus"
    who = DeviceHolder(pid=124766, uid=srv.MIN_TENANT_UID).username
    assert blocked["holders"] == [{"pid": 124766, "uid": srv.MIN_TENANT_UID, "user": who, "cmd": "tt-smi"}]
    assert len(_kinds(offbus_blocked, "hold_blocked_by_tenant")) == 1


@pytest.mark.asyncio
async def test_a_foreign_blocker_past_the_deadline_pages_once_and_is_never_killed(offbus_blocked, monkeypatch):
    """The foreign-holder bound covers off-bus holds too: past the deadline the broker pages once,
    from the per-sample watchdog (not the next ceiling window), and never signals the holder. The
    HOLD-WATCHDOG event names the blocker instead of 'manual recovery'."""
    monkeypatch.setenv("TT_DEVICE_MCP_FOREIGN_HOLDER_DEADLINE_SEC", "60")
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    srv.hold_blocked_by["since"] = time.time() - 61
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_hold_logged", True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    old = (srv.datetime.now() - srv.timedelta(seconds=srv._hold_deadline_sec() + 60)).isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", old)

    srv._refresh_idle_hold_ledger()
    srv._refresh_idle_hold_ledger()

    paged = _kinds(offbus_blocked, "foreign_holder_past_deadline")
    assert len(paged) == 1, "one page per blocked hold, not one per sample"
    assert paged[0]["why"] == "off_bus" and paged[0]["holders"][0]["pid"] == 124766
    stuck = _kinds(offbus_blocked, "hold_stuck_past_deadline")
    assert stuck and stuck[0]["blocked_by"]["holders"][0]["cmd"] == "tt-smi"
    assert offbus_blocked["kills"] == 0 and offbus_blocked["resets"] == 0
    assert srv._health_payload()["blocked_by"]["paged"] is True


@pytest.mark.asyncio
async def test_a_running_broker_job_blocks_without_a_page(offbus_blocked, monkeypatch):
    """A broker job blocks by design (issue #3: never reset under a running job) and carries its own
    timeout, so it is named in blocked_by but never paged as a foreign holder."""
    monkeypatch.setenv("TT_DEVICE_MCP_FOREIGN_HOLDER_DEADLINE_SEC", "60")
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    srv.jobs["042"] = srv.Job(
        id="042", owner="u", workspace="/w", command="pytest", queued_at="2026-01-01T00:00:00",
        status=srv.JobStatus.RUNNING,
    )
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    srv.hold_blocked_by["since"] = time.time() - 61
    srv._check_hold_blocked_deadline()

    assert offbus_blocked["resets"] == 0
    assert srv._health_payload()["blocked_by"]["jobs"] == ["042"]
    assert _kinds(offbus_blocked, "foreign_holder_past_deadline") == []


@pytest.mark.asyncio
async def test_blocked_by_clears_once_the_escalation_proceeds(offbus_blocked, monkeypatch):
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert srv.hold_blocked_by
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
    assert srv.hold_blocked_by == {}
    assert "blocked_by" not in srv._health_payload()
