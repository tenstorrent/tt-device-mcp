# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The power cycle's backoff across cycles that leave the same fault (spec 03 I67).

A whole tray dead in hardware is not healed by a power cycle. Each cycle that took the host down
and left the same tray off doubles the next cooldown (30 min, 1 h, 2 h, then 4 h at most), counted
on disk so it survives the restart and the reboot the cycle causes. A verified release or a
different fault resets it; a request the power-cycle command did not carry out is no cycle and is
asked again after the base interval. The cycle always fires at the stated deadline. No test reaches
a real reset or power cycle: the ``rig`` records them."""

import json
from datetime import datetime

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off, rig  # noqa: F401
from tests.test_power_cycle_cooldown import LADDER, T0, _cycles, _ledger, _tray_off, _watchdog, cool  # noqa: F401
from tt_device_mcp import server as srv
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.base import (
    AUTO_RECOVERY_LEDGER,
    POWER_CYCLE_BACKOFF_CAP_SEC,
    POWER_CYCLE_BACKOFF_FILE,
    POWER_CYCLE_MIN_INTERVAL_SEC,
)

BASE = POWER_CYCLE_MIN_INTERVAL_SEC
BOOT_SEC = 120  # a cycle's boot, two minutes after the request


def _records():
    path = recovery_base.health_dir() / AUTO_RECOVERY_LEDGER
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def _boot_after_cycle(r, n):
    """The cycle the rig recorded took the host down: a new boot ``BOOT_SEC`` later."""
    r["boot"].update(id=f"boot-{n}", btime=r["clk"][0] + BOOT_SEC)
    r["clk"][0] += BOOT_SEC + 60


def _health(r, monkeypatch):
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    return srv._health_payload(now=datetime.fromtimestamp(r["clk"][0])).get("power_cycle_cooldown")


# ---- items 1, 2 and 6: the first cycle at once, then a growing, capped cooldown on /health ------


@pytest.mark.asyncio
async def test_a_tray_that_stays_off_backs_off_30_60_120_240_and_caps(cool, monkeypatch):
    """The first cycle for a whole-tray drop fires at once (no recent cycle or boot). Each boot with
    the same tray still off doubles the next cooldown up to the cap, never a 30-min boot loop; no
    ladder rung runs; at each deadline the watchdog asks once and exactly one cycle fires."""
    _tray_off(cool)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 1, "the first cycle is not delayed"
    rec = _records()[-1]
    assert rec["fault"] == {"class": "A", "off_bus": sorted(str(c) for c in _off(cool["map"], {2: 8}))}
    gaps = []
    for n in range(1, 6):
        _boot_after_cycle(cool, n)
        await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
        assert _cycles(cool) == n, "held through the cooldown"
        state = cool["g"].mechanism.power_cycle_cooldown_state
        until = state["until"]
        assert state["attempts"] == n and state["fault_class"] == "A"
        gaps.append(until - cool["boot"]["btime"])
        cd = _health(cool, monkeypatch)
        assert cd["fault_class"] == "A" and cd["attempts"] == n and cd["not_performed"] is None
        assert cd["next_deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")
        assert _watchdog(cool, monkeypatch, until - 1) == []
        assert _watchdog(cool, monkeypatch, until) == [until], "the cycle is asked at its deadline"
        cool["clk"][0] = until
        await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda m: None)
        assert _cycles(cool) == n + 1
    assert gaps == [2 * BASE, 4 * BASE, POWER_CYCLE_BACKOFF_CAP_SEC, POWER_CYCLE_BACKOFF_CAP_SEC, POWER_CYCLE_BACKOFF_CAP_SEC]
    assert not LADDER & set(cool["order"])


@pytest.mark.asyncio
async def test_the_field_sequence_cycle_then_boot_with_the_tray_still_off(cool):
    """A cycle at 04:05, the boot at 04:07 with the same tray off: the next cycle is due one hour
    after the boot (not 30 min), the one after that two hours after its boot, then every 4 h."""
    t_cycle = T0
    cool["clk"][0] = t_cycle
    _tray_off(cool)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    cool["boot"].update(id="boot-1", btime=t_cycle + 120)
    cool["clk"][0] = t_cycle + 120 + 360  # the startup gate, six minutes after the boot
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    state = cool["g"].mechanism.power_cycle_cooldown_state
    assert state["until"] == t_cycle + 120 + 3600 and state["cause"] == "host boot after the broker power-cycle"
    cool["clk"][0] = t_cycle + 120 + 1800  # where the old 30-min cooldown would have fired
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 1


def test_the_interval_knob_is_clamped(monkeypatch):
    """No setting switches the cycle off or waits past the stated bounds: the base is clamped to
    10 min..1 h, the 4 h cap is not a setting."""
    for raw, want in (("0", 600), ("-5", 600), ("999999", 3600), ("junk", 1800), ("1200", 1200)):
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_MIN_INTERVAL_SEC", raw)
        assert recovery_base._power_cycle_min_interval_env() == want
    assert recovery_base.power_cycle_interval_sec(10**6) == POWER_CYCLE_BACKOFF_CAP_SEC


# ---- item 3: on disk, across the broker restart and the reboot ----------------------------------


@pytest.mark.asyncio
async def test_the_count_and_deadline_survive_a_broker_restart(cool, monkeypatch):
    _tray_off(cool)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    _boot_after_cycle(cool, 1)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    m = cool["g"].mechanism
    before = dict(m.power_cycle_cooldown_state)
    saved = json.loads((recovery_base.health_dir() / POWER_CYCLE_BACKOFF_FILE).read_text())
    assert saved["fault"]["class"] == "A"
    # The restart: nothing in memory.
    m.power_cycle_cooldown_state = m.power_cycle_retry = m.power_cycle_fault = None
    after = m.restore_power_cycle_backoff()
    assert after["until"] == before["until"] and after["attempts"] == 1 and after["fault_class"] == "A"
    assert m.power_cycle_retry[0] == before["until"]
    assert _health(cool, monkeypatch)["attempts"] == 1


@pytest.mark.parametrize("carried", [True, False], ids=["broker-restart", "host-reboot"])
def test_a_new_hold_restores_the_saved_deadline(cool, monkeypatch, tmp_path, clear_job_state, carried):
    """The hold that opens in the next process restores the deadline and the watchdog's ask,
    whether the episode clock carried (a broker restart) or the cycle's reboot ended it."""
    m = cool["g"].mechanism
    fault = {"class": "A", "off_bus": ["8"]}
    _ledger({"action": "power-cycle", "boot_id": "old-0", "at_epoch": T0 - 600, "fault": fault})
    m.note_power_cycle_fault(fault)
    m.power_cycle_cooldown_state = m.power_cycle_retry = m.power_cycle_fault = None
    monkeypatch.setattr(srv, "recovery_mechanism", m)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(srv, "device_hold_logged", False)
    monkeypatch.setattr(srv, "_hold_episode_restorable", True)
    monkeypatch.setattr(srv, "_restore_hold_episode", lambda: "2026-01-01T00:00:00" if carried else "")
    srv._note_tenant_gate_verdict("8/32 chip(s) off the bus")
    assert m.power_cycle_cooldown_state["attempts"] == 1
    assert m.power_cycle_retry[0] == m.power_cycle_cooldown_state["until"] == T0 - 600 + 2 * BASE


def test_a_new_hold_with_no_saved_streak_sets_no_ask(cool):
    m = cool["g"].mechanism
    assert m.restore_power_cycle_backoff() is None and m.power_cycle_retry is None


# ---- item 4: a heal or a different fault resets the streak ---------------------------------------


def test_a_verified_release_resets_the_count(cool):
    m = cool["g"].mechanism
    fault = {"class": "A", "off_bus": ["8", "9"]}
    _ledger(*({"action": "power-cycle", "boot_id": f"old-{i}", "at_epoch": T0 - 9000 + i, "fault": fault} for i in range(2)))
    assert m.power_cycle_attempts(fault=fault) == 2
    m.note_power_cycle_fault(fault)
    m.note_mesh_verified(now_epoch=T0)
    assert m.power_cycle_attempts(fault=fault) == 0 and m.power_cycle_interval(fault=fault) == BASE
    assert not (recovery_base.health_dir() / POWER_CYCLE_BACKOFF_FILE).exists()


def test_a_different_fault_starts_at_the_base(cool):
    m = cool["g"].mechanism
    tray2 = {"class": "A", "off_bus": ["8", "9"]}
    _ledger(*({"action": "power-cycle", "boot_id": f"old-{i}", "at_epoch": T0 - 9000 + i, "fault": tray2} for i in range(3)))
    assert m.power_cycle_interval(fault=tray2) == POWER_CYCLE_BACKOFF_CAP_SEC
    assert m.power_cycle_attempts(fault={"class": "C", "off_bus": [str(i) for i in range(EXPECTED)]}) == 0
    assert m.power_cycle_attempts(fault={"class": "A", "off_bus": ["16", "17"]}) == 0, "another tray"
    # Back to tray 2 after one cycle for another fault: the streak was broken, it counts afresh.
    other = {"action": "power-cycle", "boot_id": "old-x", "at_epoch": T0 - 100, "fault": {"class": "C", "off_bus": []}}
    _ledger(*_records(), other)
    assert m.power_cycle_attempts(fault=tray2) == 0


# ---- item 5: a request the command did not carry out ---------------------------------------------


@pytest.mark.asyncio
async def test_a_power_cycle_the_command_refused_is_not_counted_and_is_retried_at_the_base(cool, monkeypatch):
    """Two cycles left the tray off; the third request's command exits 1 (no reboot). It is no
    cycle for the backoff, /health says 'power cycle not performed' with rc 1, and it is asked again
    after the base interval, not the 4 h the count would give."""
    m = cool["g"].mechanism
    fault = {"class": "A", "off_bus": sorted(str(c) for c in _off(cool["map"], {2: 8}))}
    _ledger(*({"action": "power-cycle", "boot_id": f"old-{i}", "at_epoch": T0 - 20000 + i, "fault": fault} for i in range(2)))
    m.note_power_cycle_fault(fault)
    monkeypatch.setattr(m, "_write_action_log", lambda *a, **k: None)

    def refuse():
        raise RuntimeError("ipmitool power cycle exited 1: Error: Unable to establish IPMI session")

    await m._fire_recovery_escalation(
        "power-cycle", row_owner="broker", label="power cycle", detail="tray off", fire=refuse, log=lambda s: None, reason="tray off"
    )
    assert [r for r in _records() if r.get("boot_id") == "boot-A"] == [], "the ledger entry is retracted"
    assert m.power_cycle_attempts(fault=fault) == 2
    assert m.power_cycle_retry[0] == T0 + BASE
    cd = _health(cool, monkeypatch)
    assert cd["not_performed"]["status"] == "power cycle not performed" and cd["not_performed"]["rc"] == 1
    assert cd["next_deadline"] == datetime.fromtimestamp(T0 + BASE).isoformat(timespec="seconds")
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + BASE - 1)[0] is False
    allowed, why = m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + BASE)
    assert allowed, why


def test_a_cycle_recorded_this_boot_that_never_went_down_waits_the_base_only(cool):
    """The command returned 0 but the host never went down (still the same boot): the record is not
    a cycle for the backoff and the next ask comes after the base interval."""
    m = cool["g"].mechanism
    fault = {"class": "A", "off_bus": ["8"]}
    _ledger(
        *({"action": "power-cycle", "boot_id": f"old-{i}", "at_epoch": T0 - 30000 + i, "fault": fault} for i in range(3)),
        {"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0, "fault": fault},
    )
    assert m.power_cycle_attempts(fault=fault) == 3
    cd = m.power_cycle_cooldown(now_epoch=T0 + 60, fault=fault)
    assert cd["until"] == T0 + BASE
    assert m.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=T0 + BASE)[0] is True


# ---- goal 8: the post-cycle full check gates the release and the reset of the count --------------


def test_after_a_cycle_only_a_passing_full_check_releases_and_resets_the_count(monkeypatch, tmp_path):
    """The boot after a cycle: a release refused for a missing fabric pass keeps the hold and the
    count; the passing full check (fabric included) releases and resets it. The startup pass that
    precedes it waits for the hugepage pool (test_classac_power_cycle_direct)."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    m = srv.recovery_mechanism
    fault = {"class": "A", "off_bus": ["8"]}
    _ledger({"action": "power-cycle", "boot_id": "a-boot-before-this-one", "at_epoch": 1.0, "fault": fault})
    assert m.power_cycle_attempts(fault=fault) == 1
    held = []
    monkeypatch.setattr(srv, "_hold_device_fabric_unverified", lambda why: held.append(why))
    monkeypatch.setattr(srv, "_post_reset_debt", lambda: FABRIC_DEBT.owed)
    FABRIC_DEBT.owed = "power-cycle"
    assert srv._clear_device_dirty(verified=True, why="post-cycle check") is False
    assert held and m.power_cycle_attempts(fault=fault) == 1
    FABRIC_DEBT.owed = ""
    assert srv._clear_device_dirty(verified=True, why="post-cycle check") is True
    assert m.power_cycle_attempts(fault=fault) == 0
