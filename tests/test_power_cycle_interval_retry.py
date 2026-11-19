# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A power cycle refused only by its min interval is retried as the interval ends (spec 04 I10).

The hold-deadline watchdog used to retry it at its next ceiling window, which can land most of a
window after the interval ran out. The interval itself stays enforced, and the retried pass releases
nothing without a passing fabric check (spec 04 I32).
"""

from datetime import timedelta

import pytest

from tests.conftest import fsm_dirty
from tests.test_device_safety import _hold_deadline_probe, _ledger_dir
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import OUTCOME_RECOVERED, OUTCOME_WAITING
from tt_device_mcp.health.recovery import base as recovery_base

INTERVAL = 1800
CYCLED_AT = 1000.0  # the earlier power cycle, recorded under the boot before this one
EXPIRY = CYCLED_AT + INTERVAL  # spec 04 I65: asked the moment the cooldown ends, no slack


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    d = _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    monkeypatch.setattr(recovery_base, "POWER_CYCLE_MIN_INTERVAL_SEC", INTERVAL)
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-10-15T09:00:00")
    monkeypatch.setattr(srv.recovery_mechanism, "power_cycle_retry", None)
    path = d / recovery_base.AUTO_RECOVERY_LEDGER
    path.write_text(f'{{"action": "power-cycle", "boot_id": "boot-A", "at_epoch": {CYCLED_AT}}}\n')
    return path


def _allowed(action="power-cycle", at=CYCLED_AT + INTERVAL - 47):
    return srv.recovery_mechanism.auto_recovery_allowed(action, tenant_active=False, now_epoch=at)


def test_a_cycle_refused_by_its_interval_is_due_again_at_expiry(ledger):
    allowed, why = _allowed()
    assert allowed is False
    assert "48s more" in why and "boot-loop guard" in why
    assert "did not fix" not in why, "the earlier cycle may have fixed the box and a new fault followed"
    rm = srv.recovery_mechanism
    assert not rm.power_cycle_retry_due(EXPIRY - 1)
    assert rm.power_cycle_retry_due(EXPIRY)


def test_the_interval_is_still_enforced(ledger):
    assert _allowed(at=CYCLED_AT + INTERVAL - 1)[0] is False
    assert _allowed(at=CYCLED_AT + INTERVAL)[0] is True


@pytest.mark.parametrize("case", ["reboot", "other episode", "no episode"])
def test_no_retry_for_a_reboot_or_outside_the_refused_episode(ledger, monkeypatch, case):
    action = "power-cycle"
    if case == "reboot":
        action = "reboot"
    elif case == "no episode":
        monkeypatch.setattr(srv, "device_hold_episode_since", "")
    assert _allowed(action)[0] is False
    if case == "other episode":
        monkeypatch.setattr(srv, "device_hold_episode_since", "2026-10-15T10:00:00")
    assert not srv.recovery_mechanism.power_cycle_retry_due(EXPIRY + 3600)


def test_a_cycle_the_per_boot_cap_also_refuses_is_still_asked_again_at_its_end(ledger):
    # Was the "per-boot cap" case of the no-retry test: the merged gate bounds the cap by the cooldown
    # interval too (spec 04 I65), so the cycle is asked again as both end, never left to the ceiling.
    with ledger.open("a") as f:
        f.write(f'{{"action": "reboot", "boot_id": "boot-B", "at_epoch": {CYCLED_AT}}}\n')
    assert _allowed()[0] is False
    rm = srv.recovery_mechanism
    assert rm.power_cycle_cooldown_state["until"] == EXPIRY
    assert not rm.power_cycle_retry_due(EXPIRY - 1)
    assert rm.power_cycle_retry_due(EXPIRY)
    assert _allowed(at=EXPIRY)[0] is True


def test_the_watchdog_forces_one_pass_at_expiry_not_at_the_next_window(ledger, monkeypatch):
    fired, since = _hold_deadline_probe(monkeypatch, isolated=set())
    monkeypatch.setattr(srv, "device_hold_episode_reason", "chips 10,11 fell off the PCIe bus")
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 10**6)  # no ceiling window in range
    monkeypatch.setattr(srv.offbus_power_cycle, "due_for_watchdog", lambda *a: False)
    assert _allowed()[0] is False
    now_at = lambda epoch: since + timedelta(seconds=epoch - since.timestamp())  # noqa: E731

    srv._check_hold_deadline(now=now_at(EXPIRY - 1))
    assert fired == []
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: False)  # an op in flight
    srv._check_hold_deadline(now=now_at(EXPIRY))
    assert srv.recovery_mechanism.power_cycle_retry_due(EXPIRY), "a declined spawn must keep the retry"
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    srv._check_hold_deadline(now=now_at(EXPIRY + 1))
    assert fired == [True]
    assert srv.device_hold_escalation_kind == "power_cycle_cooldown"
    srv._check_hold_deadline(now=now_at(EXPIRY + 2))
    assert fired == [True], "one pass per refusal"


@pytest.mark.asyncio
async def test_the_retried_pass_releases_only_after_a_passing_fabric_check(ledger, monkeypatch, tmp_path):
    for i in range(32):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {})
    monkeypatch.setattr(srv, "_offbus_count", lambda beats, expected: 0)
    monkeypatch.setattr(srv, "_fabric_no_reset_hold", lambda: "")
    monkeypatch.setattr(srv, "health_event", lambda kind, **f: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "device_hold_escalation_kind", "power_cycle_cooldown")
    fsm_dirty(srv, "chips 10,11 off the bus", why="job_killed")

    async def ladder(phase, indices, expected, log, **_):
        # The ladder's reset verified enum+ARC; the funnel decides whether that may release.
        FABRIC_DEBT.note_reset("tt-smi -glx_reset")
        released = srv._clear_device_dirty(verified=True, why="hold-deadline: reset verified")
        return OUTCOME_RECOVERED if released else OUTCOME_WAITING

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", ladder)
    await srv._force_escalate_stuck_hold()
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == srv.FABRIC_RELIFT_WHY
    FABRIC_DEBT.note_fabric(True)
    assert srv._clear_device_dirty(verified=True, why="idle relift: fabric healthy") is True
    assert srv.fsm.state is ServerState.HEALTHY
