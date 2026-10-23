# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""/health stays held while a broker op runs inside an open hold (spec 03 I80).

The jobs list suppresses the HOLD row while a broker op runs (the op's row is the recovery), and
/health used to read the same latch, so a forced rung such as a glx_reset read as a lifted hold for
its whole length. /health now keeps the hold until the full gate, fabric included, passes.
"""

import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, fsm_healthy
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState

REASON = "gate/startup: 1/32 chip(s) off the bus, below the reset floor — held for self-heal, not reset"


def _isolate_hold(monkeypatch):
    """Every hold global these tests touch is restored afterwards, so no hold leaks into later tests."""
    for name, value in (
        ("device_hold_logged", False),
        ("device_hold_episode_since", ""),
        ("device_hold_episode_reason", ""),
        ("device_held_since", ""),
        ("_hold_row", None),
        ("device_hold_escalation_trigger", ""),
        ("_hold_carry", None),
        ("_hold_carry_loaded", False),
    ):
        monkeypatch.setattr(srv, name, value)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-a")
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_persist_hold_episode", lambda *a, **k: None)


@pytest.fixture
def held(monkeypatch, clear_job_state, tmp_path):
    """A below-floor off-bus hold opened at the returned episode start."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    _isolate_hold(monkeypatch)
    monkeypatch.setattr(srv, "isolated_chips", {"8"})
    fsm_dirty(srv, REASON, why="off_bus")
    since = datetime(2026, 10, 15, 1, 36, 0)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    return since


def test_health_stays_held_while_a_forced_rung_runs(held, monkeypatch):
    """/health read held:false, status ok, while the forced glx_reset of a hold ran, then held:true again
    once it ended. The op inside an open hold episode is the recovery of THAT hold: the device is still
    refused, and a watcher that reads ``held`` must not see it lifted until the full gate, fabric
    included, passes."""
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    before = srv._health_payload(now=held + timedelta(seconds=30))
    assert before["held"] is True

    class Running:
        def done(self):
            return False

    monkeypatch.setattr(srv, "_forced_escalation_task", Running())
    monkeypatch.setattr(srv, "device_op_active", "hold-deadline-escalate")
    monkeypatch.setattr(srv, "device_op_detail", "galaxy reset")
    during = srv._health_payload(now=held + timedelta(seconds=700))
    assert during["held"] is True, "a forced rung must not read as a lifted hold"
    assert during["status"] == "degraded" and during["fsm_state"] != ServerState.HEALTHY.value
    assert during["held_since"] == before["held_since"], "the hold keeps its clock across the op"
    assert during["held_age_sec"] >= before["held_age_sec"]
    assert during["held_reason"].startswith(f"device unverified: {REASON} ["), "the hold's own reason, not the op's"
    assert "broker device op in flight" in during["device_degraded"]
    assert not [
        r for r in srv._recent_jobs(20) if r["job_id"] == "--" and "HELD" in r["command"]
    ], "the jobs list still shows the op's row, not a second hold row"
    assert srv._device_hold_state() is None, "the jobs list, gate and release keep the op-suppressed latch"

    # The rung ended but the post-reset check has not passed: still held.
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "_forced_escalation_task", None)
    assert srv._health_payload(now=held + timedelta(seconds=760))["held"] is True


def test_health_lifts_the_hold_only_when_the_gate_passes_inside_the_op(held, monkeypatch):
    """The post-reset check runs inside the op; held drops the moment that full gate passes (it closes
    the episode), not when the op ends and not before."""
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-reset")
    assert srv._health_payload()["held"] is True
    srv._note_tenant_gate_verdict("")  # the gate passed, fabric included
    payload = srv._health_payload()
    assert payload["held"] is False and "held_reason" not in payload


def test_a_routine_op_on_a_device_that_was_not_held_does_not_read_held(monkeypatch, clear_job_state):
    """A between-jobs check or reset on a device no hold was open for is not a hold: status stays ok and
    device_degraded names the op, as before."""
    _isolate_hold(monkeypatch)
    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    fsm_dirty(srv, "job 070 ended killed")
    payload = srv._health_payload()
    assert payload["held"] is False and payload["status"] == "ok"
    assert payload["device_degraded"]


def test_a_hold_opened_inside_an_op_keeps_its_held_since_after_the_op(held, monkeypatch):
    """A hold the post-job gate opens while its op runs: /health showed the episode start during the
    op, then the op-suppressed latch stamped a fresh held_since when the op ended, so held_since
    jumped forward and held_age_sec fell (300 -> 0) with the hold still open. One clock per episode."""
    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    monkeypatch.setattr(srv, "device_hold_episode_since", held.isoformat())
    during = srv._health_payload(now=held + timedelta(seconds=300))
    assert during["held"] is True and during["held_since"] == held.isoformat()

    monkeypatch.setattr(srv, "device_op_active", "")
    after = srv._health_payload(now=held + timedelta(seconds=310))
    assert after["held"] is True
    assert after["held_since"] == during["held_since"], "the hold keeps its clock when the op ends"
    assert after["held_age_sec"] >= during["held_age_sec"]


def _release_inside_an_op(held, monkeypatch):
    """A hold opened inside an op, reported, then released by the gate passing inside the op."""
    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    monkeypatch.setattr(srv, "device_hold_episode_since", held.isoformat())
    assert srv._health_payload(now=held + timedelta(seconds=300))["held_since"] == held.isoformat()

    fsm_healthy(srv)
    monkeypatch.setattr(srv, "isolated_chips", set())
    srv._note_tenant_gate_verdict("")  # the gate passed inside the op: the episode is over
    monkeypatch.setattr(srv, "device_op_active", "")
    # no /health, queue-status or jobs read lands while the device is fit
    assert not srv.device_held_since and not srv.device_hold_episode_since


def test_a_closed_episodes_held_since_never_carries_into_the_next_hold(held, monkeypatch):
    """The in-op latch outlived its episode: the gate passed inside the op and closed the hold, but no
    status read landed while the device was fit, so the next hold opened inside an op reported the
    previous episode's start (days old) as its held_since. Each episode keeps its own clock. The next
    hold here comes after the spec 04 I82 carry window, so it is a new episode."""
    _release_inside_an_op(held, monkeypatch)
    srv._hold_carry["released"] = time.time() - srv._HOLD_CARRY_SEC - 1

    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    fsm_dirty(srv, "new fault", why="off_bus")
    srv._note_tenant_gate_verdict("device unverified: new fault")
    payload = srv._health_payload()
    assert payload["held"] is True
    assert payload["held_since"] == srv.device_hold_episode_since != held.isoformat()
    assert payload["held_age_sec"] < 60


def test_a_rehold_inside_the_carry_window_reports_the_carried_start_in_and_after_the_op(held, monkeypatch):
    """Spec 04 I82 with I80: a release followed within _HOLD_CARRY_SEC by a new hold continues the
    episode. Opened inside an op, that hold reports the carried start as held_since, during the op
    and after it, and the 10-min clock keeps it: a lift/fail cycle never restarts the clock."""
    _release_inside_an_op(held, monkeypatch)

    monkeypatch.setattr(srv, "device_op_active", "health-gate/post-job")
    fsm_dirty(srv, "new fault", why="off_bus")
    srv._note_tenant_gate_verdict("device unverified: new fault")
    assert srv.device_hold_episode_since == held.isoformat(), "the I82 carry keeps the episode start"
    during = srv._health_payload()
    assert during["held"] is True and during["held_since"] == held.isoformat()

    monkeypatch.setattr(srv, "device_op_active", "")
    after = srv._health_payload()
    assert after["held"] is True and after["held_since"] == held.isoformat()
    assert after["held_age_sec"] >= during["held_age_sec"] > 300


def _rehold_after_a_read_hold_releases_inside_an_op(held, monkeypatch):
    """A hold opened outside any op and reported (latched), then released by its recovery op's gate
    inside the op with no status read while the device was fit. Returns the reported start."""
    srv._note_tenant_gate_verdict(f"device unverified: {REASON}")
    first = srv._health_payload()
    assert first["held"] is True
    monkeypatch.setattr(srv, "device_op_active", "reset")
    fsm_healthy(srv)
    monkeypatch.setattr(srv, "isolated_chips", set())
    srv._note_tenant_gate_verdict("")  # the gate passed inside the op: the episode is over
    return first["held_since"]


def test_an_in_window_rehold_inside_an_op_with_no_read_keeps_the_carried_start_after_it(held, monkeypatch):
    """Spec 04 I82 with I80: the release cleared the latch, the in-window re-hold carried the episode
    start, but nothing read /health during the op, so the first read after it latched a fresh clock
    (now) and held_since restarted with the hold still the same episode."""
    first = _rehold_after_a_read_hold_releases_inside_an_op(held, monkeypatch)
    fsm_dirty(srv, "new fault", why="off_bus")
    srv._note_tenant_gate_verdict("device unverified: new fault")
    monkeypatch.setattr(srv, "device_op_active", "")
    carried = srv.device_hold_episode_since
    assert carried == first, "the I82 carry keeps the episode start"
    after = srv._health_payload()
    assert after["held"] is True and after["held_since"] == carried


def test_an_in_window_rehold_outside_an_op_reports_the_carried_start(held, monkeypatch):
    """The same re-hold opened after the op ended, outside any op: held_since is the carried start."""
    first = _rehold_after_a_read_hold_releases_inside_an_op(held, monkeypatch)
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "new fault", why="off_bus")
    srv._note_tenant_gate_verdict("device unverified: new fault")
    assert srv.device_hold_episode_since == first
    after = srv._health_payload()
    assert after["held"] is True and after["held_since"] == first


def test_a_read_before_an_in_window_reholds_verdict_yields_to_the_carried_start(held, monkeypatch):
    """A status read that lands between the re-hold's fault and its gate verdict latches its own clock
    before the episode is logged; the verdict then carries the earlier start, and held_since follows it."""
    first = _rehold_after_a_read_hold_releases_inside_an_op(held, monkeypatch)
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_dirty(srv, "new fault", why="off_bus")
    monkeypatch.setattr(srv, "isolated_chips", {"8"})
    early = srv._health_payload(now=datetime.now() + timedelta(seconds=60))
    assert early["held"] is True and early["held_since"] != first
    srv._note_tenant_gate_verdict("device unverified: new fault")
    after = srv._health_payload()
    assert after["held_since"] == srv.device_hold_episode_since == first
