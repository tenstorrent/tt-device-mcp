# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A DOWN fabric-unverified episode carried from an earlier boot reopens on this boot's clock once the
startup fabric pass returns 77 with every chip healthy, so the idle relift, its backoff and its bound
own it again instead of a hold that pages once and stands forever (spec 03 I49).

The record carries state/why/since/latches across a reboot. A startup 77 then landed on DOWN: no
idle relift re-checks DOWN, the prerequisite watch wants RECOVERING, and the deadline escalation
holds a fabric-only hold without a reset, so the box stayed held on a healthy mesh.
"""

import json

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerFsm, ServerState

_OLD_SINCE = "2026-10-01T00:00:00Z"
_HOLD_DETAIL = "gate/startup: enum+ARC healthy, fabric unverified"
_FABRIC_77 = (
    True,
    {
        "snapshot": {"ok": True},
        "eth_heartbeat": {"ok": True, "detail": "advancing"},
        "fabric": {"ok": None, "detail": "could not run (77)"},
    },
)
_FABRIC_OK = (True, {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "fabric healthy"}})


def _carried(tmp_path, *, boot_after="boot-after", state=ServerState.DOWN, why="fabric_unverified"):
    """An episode written under one boot and loaded under ``boot_after``, as run_startup_tasks does."""
    path = tmp_path / "fsmdir" / "fsm.json"
    path.parent.mkdir(exist_ok=True)
    before = ServerFsm(path, current_boot_id="boot-before")
    before.on_fault(why, detail=_HOLD_DETAIL, dirty=False)
    before.set_latch("escalated", True)
    before.set_latch("ubb_reset_fired", True)
    if state is ServerState.DOWN:
        before.on_outcome("terminal")
    raw = json.loads(path.read_text())
    raw["since"] = _OLD_SINCE
    path.write_text(json.dumps(raw))
    after = ServerFsm(path, current_boot_id=boot_after)
    after.boot_merge(open_episode=after.record, attributed_boot=None)
    assert after.state is state
    return after


def _broker(monkeypatch, tmp_path, fsm, verdict=_FABRIC_77):
    """A 32-chip broker with ``fsm`` as its record and ``_verify_device`` returning ``verdict``."""
    devs = tmp_path / "dev"
    devs.mkdir(exist_ok=True)
    for i in range(32):
        (devs / str(i)).write_text("")
    monkeypatch.setattr(srv, "fsm", fsm)
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(devs))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "")
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    # A host with the broad relift off: only an owed pass re-runs the fabric.
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_fault_reported", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "capture_incident", lambda *a, **k: None)
    monkeypatch.setattr(srv, "chip_snapshot_event", lambda *a, **k: {})
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", 0.0)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_failed", False)
    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", None)
    monkeypatch.setattr(srv, "device_hold_escalated_monotonic", srv.time.monotonic())
    monkeypatch.setattr(srv, "_fabric_77_recheck_since", None)
    monkeypatch.setattr(srv, "_fabric_77_rechecks", 0)
    monkeypatch.setattr(srv, "_fabric_77_recheck_monotonic", 0.0)
    monkeypatch.setattr(srv, "_fabric_77_streak_since", None)
    monkeypatch.setattr(srv, "_fabric_77_streak", 0)
    monkeypatch.setattr(srv, "_fabric_77_paged_since", None)
    monkeypatch.setattr(srv.alert, "send_alert", lambda *a, **k: None)
    events = []
    patch_health_event(monkeypatch, lambda name, *a, **k: events.append((name, k)))
    seen = {"resets": 0, "fabric": []}

    async def reset(indices, log):
        seen["resets"] += 1
        return True

    async def verify(expected, log, run_fabric=True, **_):
        seen["fabric"].append(run_fabric)
        return seen.get("verdict", verdict)

    patch_recovery(monkeypatch, "_reset_and_verify_device", reset)
    patch_recovery(monkeypatch, "_verify_device", verify)
    rungs = []

    async def escalate(rung, *a, **k):
        rungs.append(rung)
        return srv.OUTCOME_WAITING

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    seen["rungs"] = rungs
    return events, seen


async def _reopened(monkeypatch, tmp_path):
    fsm = _carried(tmp_path)
    events, seen = _broker(monkeypatch, tmp_path, fsm)
    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)
    return events, seen


@pytest.mark.asyncio
async def test_a_carried_down_fabric_hold_reopens_on_a_startup_77(monkeypatch, tmp_path, clear_job_state):
    events, seen = await _reopened(monkeypatch, tmp_path)

    assert seen["resets"] == 0, "a 77 is never reset for"
    assert srv.fsm.state is ServerState.RECOVERING, "a carried DOWN must not stand on a healthy mesh"
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY
    assert srv.fsm.record.since != _OLD_SINCE, "the bound counts from this boot"
    assert not srv.fsm.record.dirty
    assert not srv.fsm.latch("escalated"), "this boot's ladder is not pre-spent"
    assert srv.fsm.latch("ubb_reset_fired"), "the tray rung's latch is kept"
    assert srv.device_hold_escalated_monotonic == 0.0
    assert srv._startup_fabric_owed(), "one startup pass is owed even with the relift off"
    assert srv._idle_relift_armed() == (False, True, False)
    reverify = [k for name, k in events if name == "boot_fabric_reverify"]
    assert reverify and reverify[0]["prior_since"] == _OLD_SINCE
    raw = json.loads((tmp_path / "fsmdir" / "fsm.json").read_text())
    assert raw["state"] == "recovering" and "escalated" not in raw["latches"], "persisted"


@pytest.mark.asyncio
async def test_the_reopened_hold_lifts_on_a_healthy_relift(monkeypatch, tmp_path, clear_job_state):
    _events, seen = await _reopened(monkeypatch, tmp_path)
    seen["verdict"] = _FABRIC_OK

    await srv._attempt_idle_relift()

    assert seen["fabric"][-1] is True, "the owed pass runs the fabric check"
    assert seen["resets"] == 0 and seen["rungs"] == []
    assert srv.fsm.state is ServerState.HEALTHY
    assert not srv.fsm.latch("escalated") and not srv.fsm.latch("ubb_reset_fired")


@pytest.mark.asyncio
async def test_a_reopened_hold_that_77s_again_is_held_without_a_reset_then_bounded(
    monkeypatch, tmp_path, clear_job_state
):
    """Another 77 holds it (no reset), and the I40 bound still ends it: past its retries the startup
    checks gate it and a healthy eth read releases it."""
    _events, seen = await _reopened(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", "2")
    monkeypatch.setattr(srv, "_fabric_77_recheck_due", lambda: True)

    async def eth_read(*a, **k):
        return True, "all active-eth cores advancing"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_read)

    await srv._attempt_idle_relift()
    assert seen["fabric"][-1] is True
    assert seen["resets"] == 0 and seen["rungs"] == []
    assert srv.fsm.state is ServerState.RECOVERING, "one more 77 holds"

    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    await srv._attempt_idle_relift()
    assert seen["resets"] == 0 and seen["rungs"] == []
    assert srv.fsm.state is ServerState.HEALTHY, "the I40 bound releases on the startup checks"


@pytest.mark.asyncio
async def test_a_reopened_known_fabric_fault_is_held_without_a_climb(monkeypatch, tmp_path, clear_job_state):
    """03 I46: a known fabric fault stays held past the bound: no release, no rung, no reset."""
    _events, seen = await _reopened(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", "1")
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "known fabric fault: 2 jobs in a row")

    async def eth_read(*a, **k):
        return True, "all active-eth cores advancing"

    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_read)

    await srv._attempt_idle_relift()

    assert seen["resets"] == 0 and seen["rungs"] == []
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_a_carried_dirty_down_of_another_cause_reopens_on_a_clean_startup_77(
    monkeypatch, tmp_path, clear_job_state
):
    """The reopen reads the record after the gate's own 77 hold (why fabric_unverified, dirty
    cleared), so a carried DOWN of any cause or dirty mark reopens, as a fresh record's 77 would."""
    path = tmp_path / "fsmdir" / "fsm.json"
    path.parent.mkdir()
    before = ServerFsm(path, current_boot_id="boot-before")
    before.on_fault("job_killed", detail="chip 3 isolated by the sampler", dirty=True)
    before.on_outcome("terminal")
    raw = json.loads(path.read_text())
    raw["since"] = _OLD_SINCE
    path.write_text(json.dumps(raw))
    fsm = ServerFsm(path, current_boot_id="boot-after")
    fsm.boot_merge(open_episode=fsm.record, attributed_boot=None)
    assert fsm.state is ServerState.DOWN and fsm.record.dirty
    _events, seen = _broker(monkeypatch, tmp_path, fsm)

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    assert srv.fsm.state is ServerState.RECOVERING
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY and not srv.fsm.record.dirty
    assert srv.fsm.record.since != _OLD_SINCE
    assert srv._startup_fabric_owed()


@pytest.mark.asyncio
async def test_with_the_relift_off_the_reopened_hold_gets_only_the_owed_pass(monkeypatch, tmp_path, clear_job_state):
    """TT_DEVICE_MCP_FABRIC_RELIFT=0 (I34): one owed pass; a second 77 leaves the hold standing."""
    _events, seen = await _reopened(monkeypatch, tmp_path)
    runs = len(seen["fabric"])

    await srv._attempt_idle_relift()
    assert len(seen["fabric"]) == runs + 1, "the owed pass runs"
    assert srv.fsm.state is ServerState.RECOVERING
    assert not srv._startup_fabric_owed()

    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    await srv._attempt_idle_relift()
    assert len(seen["fabric"]) == runs + 1, "no further pass with the relift off"
    assert seen["resets"] == 0 and seen["rungs"] == []
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_a_same_boot_down_is_not_reopened(monkeypatch, tmp_path, clear_job_state):
    """Within one boot, DOWN is the verdict of a ladder that ran on this silicon: it stands."""
    fsm = _carried(tmp_path, boot_after="boot-before")
    _events, seen = _broker(monkeypatch, tmp_path, fsm)

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    assert seen["resets"] == 0
    assert srv.fsm.state is ServerState.DOWN
    assert srv.fsm.latch("escalated")


@pytest.mark.asyncio
@pytest.mark.parametrize("finding", ["fabric_bad", "fault_reported", "isolated", "no_hold"])
async def test_a_carried_down_with_a_finding_is_not_reopened(monkeypatch, tmp_path, clear_job_state, finding):
    """Only a startup 77 with every chip healthy and nothing named reopens the hold."""
    fsm = _carried(tmp_path, why="eth_frozen" if finding == "no_hold" else "fabric_unverified")
    events, _seen = _broker(monkeypatch, tmp_path, fsm)
    if finding != "no_hold":
        srv._hold_device_fabric_unverified(_HOLD_DETAIL)
    # else a caller that did not place the 77 hold first: the record still names its carried cause.
    if finding == "fabric_bad":
        monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", False)
    elif finding == "fault_reported":
        monkeypatch.setattr(srv, "device_fault_reported", "runtime: ethernet core timeout")
    elif finding == "isolated":
        monkeypatch.setattr(srv, "isolated_chips", {3})

    assert srv._reopen_boot_carried_fabric_hold() is False
    assert srv.fsm.state is ServerState.DOWN
    assert srv.fsm.record.since == _OLD_SINCE
    assert srv.fsm.latch("escalated")
    assert not srv._startup_fabric_owed()
    assert not [n for n, _ in events if n == "boot_fabric_reverify"]


def test_reopen_after_boot_is_one_shot_and_needs_a_known_other_boot(tmp_path):
    fsm = _carried(tmp_path)
    assert fsm.reopen_after_boot() is True
    fsm.on_outcome("terminal")
    assert fsm.reopen_after_boot() is False, "once per boot"

    (tmp_path / "x").mkdir()
    unknown = _carried(tmp_path / "x", boot_after="")
    assert unknown.reopen_after_boot() is False, "an unreadable boot id is no proof of a reboot"

    (tmp_path / "y").mkdir()
    recovering = _carried(tmp_path / "y", state=ServerState.RECOVERING)
    assert recovering.reopen_after_boot() is False, "only DOWN needs reopening"
    assert recovering.record.since == _OLD_SINCE
