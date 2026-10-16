# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A power cycle a later boot verified healthy does not gate the next one (spec 04 I9).

Measured: a tray dropped, the broker power-cycled, the startup gate passed the full check with fabric
and a job ran. The tray dropped again 9 min later, and the new power cycle was refused for 30 min by
the min interval as a "boot loop", though that cycle had healed the box. Only a cycle no later boot
verified still gates; a hard backstop pages instead of cycling a flapping tray forever.
"""

import json

import pytest

from tests.test_boot_fabric_reverify import _FABRIC_OK, _broker
from tests.test_device_safety import _ledger_dir
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerFsm
from tt_device_mcp.health.recovery import base as recovery_base

INTERVAL = 1800
CYCLED_AT = 10_000.0  # the earlier power cycle, recorded under boot-A
VERIFIED_AT = CYCLED_AT + 353  # the startup gate's verified release on boot-B
REDROP_AT = CYCLED_AT + 902  # the new drop's power cycle, inside the interval


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    d = _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    monkeypatch.setattr(recovery_base, "POWER_CYCLE_MIN_INTERVAL_SEC", INTERVAL)
    monkeypatch.setattr(srv.recovery_mechanism, "_power_cycle_backstop_paged", 0.0)
    path = d / recovery_base.AUTO_RECOVERY_LEDGER
    path.write_text(json.dumps({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": CYCLED_AT}) + "\n")
    return path


def _stamp(tmp_path, boot_id="boot-B", at=VERIFIED_AT):
    (tmp_path / recovery_base.AUTO_RECOVERY_VERIFIED).write_text(json.dumps({"boot_id": boot_id, "at_epoch": at}))


def _allowed(action="power-cycle", at=REDROP_AT):
    return srv.recovery_mechanism.auto_recovery_allowed(action, tenant_active=False, now_epoch=at)


def test_a_verified_cycle_does_not_gate_a_new_drop(ledger, tmp_path):
    srv.recovery_mechanism.record_verified_release("gate/startup", now_epoch=VERIFIED_AT)
    assert json.loads((tmp_path / recovery_base.AUTO_RECOVERY_VERIFIED).read_text())["boot_id"] == "boot-B"
    assert _allowed() == (True, "")


def test_an_unverified_cycle_inside_the_interval_is_still_refused(ledger):
    allowed, why = _allowed()
    assert allowed is False and "boot-loop guard" in why
    assert _allowed(at=CYCLED_AT + INTERVAL)[0] is True


@pytest.mark.parametrize("case", ["same boot", "older stamp", "row without boot", "garbled", "reboot"])
def test_a_stamp_that_cannot_prove_a_later_boot_keeps_the_guard(ledger, tmp_path, case):
    action = "power-cycle"
    if case == "same boot":
        _stamp(tmp_path, boot_id="boot-A")
    elif case == "older stamp":
        _stamp(tmp_path, at=CYCLED_AT - 1)
    elif case == "row without boot":
        ledger.write_text(json.dumps({"action": "power-cycle", "at_epoch": CYCLED_AT}) + "\n")
        _stamp(tmp_path)
    elif case == "garbled":
        (tmp_path / recovery_base.AUTO_RECOVERY_VERIFIED).write_text("{not json")
    else:
        # Only the power cycle's own interval is relaxed: a reboot stays behind a recent power cycle.
        _stamp(tmp_path)
        action = "reboot"
    assert _allowed(action)[0] is False


def test_a_stamp_is_written_once_per_boot(ledger, tmp_path):
    rm = srv.recovery_mechanism
    rm.record_verified_release("gate/startup", now_epoch=VERIFIED_AT)
    rm.record_verified_release("gate/post-job", now_epoch=VERIFIED_AT + 600)
    assert json.loads((tmp_path / recovery_base.AUTO_RECOVERY_VERIFIED).read_text())["at_epoch"] == VERIFIED_AT


def test_the_backstop_refuses_and_pages_once_at_the_cap(ledger, tmp_path, monkeypatch):
    pages, events = [], []
    monkeypatch.setattr(recovery_base.alert, "send_alert", lambda ev, *a: pages.append(ev) or object())
    monkeypatch.setattr(recovery_base, "health_event", lambda kind, **f: events.append((kind, f)))
    with ledger.open("a") as f:
        for boot, at in (("boot-B", CYCLED_AT + 1000), ("boot-C", CYCLED_AT + 2000)):
            f.write(json.dumps({"action": "power-cycle", "boot_id": boot, "at_epoch": at}) + "\n")
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-D")
    _stamp(tmp_path, boot_id="boot-D", at=CYCLED_AT + 2400)
    now = CYCLED_AT + 3000  # every cycle verified, the interval and the per-boot cap all clear

    allowed, why = _allowed(at=now)
    assert allowed is False and "backstop" in why
    assert [p["kind"] for p in pages] == ["power_cycle_backstop"]
    assert events and events[0][0] == "power_cycle_backstop" and events[0][1]["host_at_risk"] is True
    assert _allowed(at=now + 60)[0] is False
    assert len(pages) == 1, "a held box pages once per cap reached, not every gate pass"
    # The oldest cycle leaves the window: two remain, under the cap.
    assert _allowed(at=CYCLED_AT + recovery_base.POWER_CYCLE_BACKSTOP_WINDOW_SEC)[0] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fabric", [True, False])
async def test_the_gate_stamps_only_a_release_whose_fabric_passed(monkeypatch, tmp_path, clear_job_state, fabric):
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    fsm = ServerFsm(tmp_path / "fsmdir" / "fsm.json", current_boot_id="boot-B")
    verdict = _FABRIC_OK if fabric else (True, {"snapshot": {"ok": True}})
    _broker(monkeypatch, tmp_path, fsm, verdict=verdict)
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: "")

    await srv._device_health_gate(None, phase="startup", run_fabric=True, force_fabric=True)

    stamp = srv.recovery_mechanism.last_verified_release()
    if fabric:
        assert stamp and stamp["boot_id"] == "boot-B" and stamp["via"] == "gate/startup"
    else:
        assert stamp is None


@pytest.mark.parametrize("fabric_ok", [True, None, False])
def test_a_relift_stamps_only_on_a_passing_fabric_check(monkeypatch, tmp_path, fabric_ok):
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-B")
    srv._stamp_verified_release({"snapshot": {"ok": True}, "fabric": {"ok": fabric_ok}}, "idle relift")
    assert (srv.recovery_mechanism.last_verified_release() is not None) is (fabric_ok is True)
