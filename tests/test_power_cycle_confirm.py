# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Each power cycle is confirmed by the boot id (spec 04 I68).

A power-cycle command can exit 0 once the cycle is accepted and still not cycle the host. The broker
keeps the request on disk; a new boot id confirms it. With the boot id unchanged past the bound
(15 min) the cycle is unconfirmed: logged at error level and paged, it counts as no cycle for the
I65 cooldown, and it is re-fired once. Each later unconfirmed attempt waits 1 h, 2 h, then 4 h. The
device stays held throughout. The real ``_auto_power_cycle_host`` runs; only ``_fire_power_cycle``
is faked, so no test reaches a real power cycle."""

import json
from datetime import datetime

import pytest

from tests.test_classac_power_cycle_direct import _gate, rig  # noqa: F401
from tests.test_power_cycle_cooldown import T0, _ledger, _tray_off, _watchdog, cool  # noqa: F401
from tt_device_mcp import alert
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.base import (
    AUTO_RECOVERY_LEDGER,
    POWER_CYCLE_CONFIRM_FILE,
    POWER_CYCLE_MIN_INTERVAL_SEC,
    power_cycle_confirm_sec,
    power_cycle_unconfirmed_backoff_sec,
)

REAL_AUTO_POWER_CYCLE_HOST = srv._auto_power_cycle_host
BOUND = 900


@pytest.fixture
def pc(cool, monkeypatch):  # noqa: F811
    """``cool`` with the real power-cycle rung: the ledger, the confirm record and the hand-off are the
    real ones; ``_fire_power_cycle`` returns as an rc-0 command does. The monotonic clock follows the
    epoch clock, so the host-action pending window ends on it too."""
    clk = cool["clk"]
    fired, alerts = [], []
    monkeypatch.setattr(recovery_base.time, "monotonic", lambda: clk[0])
    monkeypatch.setattr(srv, "_auto_power_cycle_host", REAL_AUTO_POWER_CYCLE_HOST)
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: fired.append(clk[0]))
    monkeypatch.setattr(srv, "_run_pre_power_cycle_hook", lambda log, reason: None)
    monkeypatch.setattr(cool["g"].mechanism, "_write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: alerts.append(event))
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_CONFIRM_SEC", raising=False)
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    cool["fired"], cool["alerts"] = fired, alerts
    _tray_off(cool)
    return cool


async def _pass(r, at):
    r["clk"][0] = at
    return await _gate(r, galaxy.STAGE_SMI_RESET, phase="startup")


def _record():
    path = recovery_base.health_dir() / POWER_CYCLE_CONFIRM_FILE
    return json.loads(path.read_text()) if path.exists() else None


def _kinds(r):
    return [k for k, _ in r["events"]]


def _this_boot_cycles():
    path = recovery_base.health_dir() / AUTO_RECOVERY_LEDGER
    lines = path.read_text().splitlines() if path.exists() else []
    return [r for r in map(json.loads, lines) if r.get("action") == "power-cycle" and r.get("boot_id") == "boot-A"]


# ---- (a) rc 0, boot id unchanged: exactly one re-fire after the bound ----------------------------


@pytest.mark.asyncio
async def test_an_rc0_cycle_that_does_not_reboot_is_re_fired_once_after_the_bound(pc, monkeypatch):
    m = pc["g"].mechanism
    await _pass(pc, T0)
    assert pc["fired"] == [T0], "class A: the cycle fires at once"
    rec = _record()
    assert rec["state"] == "awaiting_reboot" and rec["boot_id"] == "boot-A" and rec["deadline_epoch"] == T0 + BOUND
    assert rec["fault_class"], "the fault class is on the record"
    assert m.power_cycle_retry[0] == T0 + BOUND, "the watchdog asks again at the bound"
    # Inside the bound: the hold stays, nothing re-fires.
    assert await _pass(pc, T0 + BOUND - 1) == galaxy.OUTCOME_WAITING
    assert pc["fired"] == [T0]
    assert _watchdog(pc, monkeypatch, T0 + BOUND - 1) == []
    assert _watchdog(pc, monkeypatch, T0 + BOUND) == [T0 + BOUND]
    # At the bound: unconfirmed, logged and paged, re-fired once.
    await _pass(pc, T0 + BOUND)
    assert pc["fired"] == [T0, T0 + BOUND]
    assert "power_cycle_unconfirmed" in _kinds(pc)
    assert len(pc["alerts"]) == 1, "paged once"
    rec = _record()
    assert rec["attempts"] == 2 and rec["unconfirmed"] == 1 and rec["state"] == "awaiting_reboot"
    # The re-fire does not reboot either: the next attempt waits the 1 h backoff, not at once.
    await _pass(pc, T0 + 2 * BOUND)
    assert pc["fired"] == [T0, T0 + BOUND], "exactly one re-fire"
    rec = _record()
    assert rec["state"] == "unconfirmed" and rec["unconfirmed"] == 2
    assert rec["next_attempt_epoch"] == T0 + 2 * BOUND + 3600
    assert "released" not in pc["order"]


# ---- (b) a new boot id confirms: no re-fire, the record cleared ----------------------------------


@pytest.mark.asyncio
async def test_a_new_boot_id_confirms_the_cycle_and_clears_the_record(pc):
    m = pc["g"].mechanism
    await _pass(pc, T0)
    pc["boot"].update(id="boot-B", btime=T0 + 120)
    pc["clk"][0] = T0 + 300
    assert m.settle_power_cycle_confirm() is None
    assert _record() is None and "power_cycle_confirmed" in _kinds(pc)
    assert m.power_cycle_confirm_status() is None
    # Past the bound on the new boot, with the tray still off: the I65 cooldown from the boot holds.
    await _pass(pc, T0 + BOUND + 60)
    assert pc["fired"] == [T0], "no re-fire on a confirmed cycle"
    assert "power_cycle_unconfirmed" not in _kinds(pc)
    assert m.power_cycle_cooldown_state["until"] == T0 + 120 + POWER_CYCLE_MIN_INTERVAL_SEC


def test_the_first_broker_start_of_a_boot_settles_the_record(monkeypatch, tmp_path):
    from tests.test_device_safety import _boot_recording

    _boot_recording(monkeypatch, tmp_path, {"present": False})
    calls = []
    monkeypatch.setattr(srv.recovery_mechanism, "settle_power_cycle_confirm", lambda **k: calls.append(k))
    srv._record_previous_boot_error()
    assert calls == [{}]


# ---- (c) an unconfirmed cycle on /health, a bounded next step, the device held -------------------


@pytest.mark.asyncio
async def test_an_unconfirmed_cycle_shows_on_health_and_its_next_step_is_bounded(pc, monkeypatch):
    m = pc["g"].mechanism
    await _pass(pc, T0)
    health = srv._health_payload(now=datetime.fromtimestamp(T0 + 60))
    assert health["power_cycle_confirm"]["state"] == "awaiting_reboot"
    assert health["power_cycle_confirm"]["next_deadline_epoch"] == T0 + BOUND
    await _pass(pc, T0 + BOUND)  # unconfirmed once: the one re-fire
    await _pass(pc, T0 + 2 * BOUND)  # unconfirmed twice: the 1 h backoff
    until = T0 + 2 * BOUND + 3600
    status = srv._health_payload(now=datetime.fromtimestamp(T0 + 2 * BOUND))["power_cycle_confirm"]
    assert status["state"] == "unconfirmed" and status["unconfirmed"] == 2
    assert status["next_deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")
    assert m.power_cycle_retry[0] == until and "unconfirmed" in m.power_cycle_cooldown_state["cause"]
    assert _watchdog(pc, monkeypatch, until - 1) == []
    assert _watchdog(pc, monkeypatch, until) == [until]
    await _pass(pc, until - 1)
    assert len(pc["fired"]) == 2
    await _pass(pc, until)
    assert pc["fired"][-1] == until
    # Unconfirmed a third time: 2 h, then 4 h at most.
    await _pass(pc, until + BOUND)
    assert _record()["next_attempt_epoch"] == until + BOUND + 7200
    assert "released" not in pc["order"], "the device stays held: nothing released without a full check"
    assert not {"glx_reset", "sweep", "ubb_tray"} & set(pc["order"])


def test_the_backoff_and_the_bound():
    assert [power_cycle_unconfirmed_backoff_sec(n) for n in range(1, 7)] == [0, 3600, 7200, 14400, 14400, 14400]


@pytest.mark.parametrize(
    "raw, want", [("", 900), ("120", 120), ("junk", 900), ("-5", 900), ("nan", 900), ("1e9", 3600)]
)
def test_the_confirm_bound_env(monkeypatch, raw, want):
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CONFIRM_SEC", raw)
    assert power_cycle_confirm_sec() == want


# ---- (d) an unconfirmed attempt starts no I65 cooldown -------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfirmed_cycle_starts_no_cooldown(pc, monkeypatch, tmp_path):
    m = pc["g"].mechanism
    await _pass(pc, T0)
    assert len(_this_boot_cycles()) == 1
    # The external cycler stamped the cycle it accepted; that stamp is no anchor once unconfirmed.
    hist = tmp_path / "cycles.jsonl"
    hist.write_text(json.dumps({"at_epoch": T0 + 5, "cause": "BMC chassis power cycle"}) + "\n")
    monkeypatch.setenv(recovery_base.POWER_CYCLE_HISTORY_FILE_ENV, str(hist))
    pc["clk"][0] = T0 + BOUND
    assert m.settle_power_cycle_confirm()["state"] == "unconfirmed"
    assert _this_boot_cycles() == [], "the attempt that caused no boot is retracted from the ledger"
    assert m.power_cycle_cooldown(now_epoch=T0 + BOUND) is None
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + BOUND)
    assert allowed, why


# ---- (e) the record survives a broker restart ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_record_survives_a_broker_restart(pc):
    m = pc["g"].mechanism
    await _pass(pc, T0)
    # A restart on the same boot: every in-memory trace of the request is gone.
    m.power_cycle_retry = m.power_cycle_cooldown_state = m.host_action_requested = None
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + 300)
    assert not allowed and "has not rebooted yet" in why
    assert m.power_cycle_retry[0] == T0 + BOUND, "the watchdog's ask at the bound is restored"
    await _pass(pc, T0 + BOUND)
    assert pc["fired"] == [T0, T0 + BOUND]
    assert _record()["unconfirmed"] == 1


# ---- a fire that never launched is no cycle to confirm -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc, kept", [(RuntimeError("no ipmitool"), False), (recovery_base.HostActionTimedOut("t"), True)]
)
async def test_a_launch_failure_drops_the_record_and_a_timeout_keeps_it(pc, monkeypatch, exc, kept):
    def fire():
        raise exc

    monkeypatch.setattr(srv, "_fire_power_cycle", fire)
    await _pass(pc, T0)
    assert (_record() is not None) is kept
