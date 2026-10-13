# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bounded off-bus hold (spec 04 I27).

With the host reset gate refusing every reset because chips are off the bus, the hold used to stand
until an operator power-cycled the host. These pin the bounded end: past the grace, once the safe
rungs ran, idle and with nothing objecting, the broker runs the host's power-cycle command once per
interval — and otherwise holds or pages, never loops. ``fire_command`` is always a fake here; no test
may run a real power-cycle command (conftest makes the real one a tripwire)."""

from datetime import datetime, timedelta

import pytest

from tests.test_device_safety import _arm_offbus_stuck_hold
from tt_device_mcp import alert
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery import offbus_power_cycle as opc
from tt_device_mcp.health.recovery import pcie_guard

GATE_WHY = "3 chip(s) off the bus and TT_DEVICE_MCP_HOST_RESET_GATE=hold"
OFF_BUS, EXPECTED = 3, 32
# Taken at import, before conftest swaps in its tripwire; only ever called on stand-in scripts.
_REAL_FIRE = opc.fire_command


def _gated(monkeypatch, tmp_path, *, held_sec=600, cmd="/usr/local/sbin/host-power-cycle --now"):
    """An idle off-bus hold the gate refuses to reset, held ``held_sec``, with the power-cycle command
    set (``cmd=None`` leaves it unset). Returns (hold_since, rec) where rec collects the fake fires
    (each with the state file as it stood when the command ran), alerts and journal events."""
    _arm_offbus_stuck_hold(monkeypatch, off_bus=OFF_BUS, isolated=set(), reset_result=False)
    for name in (
        "TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC",
        "TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC",
        "TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE",
        "TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD",
        "TT_DEVICE_MCP_MANUAL_RECOVERY_CMD",
        "TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK",
    ):
        monkeypatch.delenv(name, raising=False)
    if cmd is None:
        monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", raising=False)
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", cmd)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (False, GATE_WHY))
    hold_since = (datetime.now() - timedelta(seconds=held_sec)).isoformat()
    srv.device_hold_episode_since = hold_since
    rec = {"fires": [], "alerts": [], "events": []}

    def fire(argv, reason, timeout=opc.FIRE_TIMEOUT_DEFAULT_SEC):
        rec["fires"].append({"argv": argv, "reason": reason, "state": opc.load_state()})

    monkeypatch.setattr(opc, "fire_command", fire)
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: rec["alerts"].append(event))
    for mod in (opc, galaxy):
        monkeypatch.setattr(mod, "health_event", lambda kind, **f: rec["events"].append((kind, f)))
    return hold_since, rec


def _ladder_ran(hold_since):
    opc.arm(hold_since)
    opc.note_ladder_ran(hold_since)


async def _pass(hold_since):
    return await srv.galaxy_recovery._offbus_gated_power_cycle(OFF_BUS, EXPECTED, hold_since, lambda m: None)


def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


# --- when it applies ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gate_that_allows_resets_hands_back_to_the_ladder(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is None
    assert rec["fires"] == [] and rec["alerts"] == []


@pytest.mark.asyncio
async def test_no_power_cycle_before_the_safe_rungs_ran(monkeypatch, tmp_path, clear_job_state):
    """The ladder's safe rungs run once first: the first gated pass arms and hands back."""
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=3600)
    assert await _pass(hold_since) is None
    assert rec["fires"] == []
    assert opc._pending.get("episode") == hold_since, "the refused episode is armed"
    assert [k for k, f in rec["events"] if f.get("phase") == "grace"] == ["offbus_gated_hold"]


@pytest.mark.asyncio
async def test_an_unset_command_pages_once_and_the_hold_stands(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path, cmd=None)
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is None
    assert await _pass(hold_since) is None
    assert rec["fires"] == []
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_gated_hold_page"], "one page per episode"
    assert "TT_DEVICE_MCP_POWER_CYCLE_CMD is not set" in rec["alerts"][0]["message"]


@pytest.mark.asyncio
async def test_within_the_grace_it_holds(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=120)
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["fires"] == []


@pytest.mark.asyncio
async def test_past_the_grace_and_idle_it_fires_once_with_the_attempt_on_disk_first(
    monkeypatch, tmp_path, clear_job_state
):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is True
    assert len(rec["fires"]) == 1
    fired = rec["fires"][0]
    assert fired["argv"] == ["/usr/local/sbin/host-power-cycle", "--now"]
    assert "3/32 chip(s) off the bus" in fired["reason"]
    attempts = fired["state"].get("attempts")
    assert attempts and attempts[0]["episode"] == hold_since, "the attempt is on disk before the command runs"
    assert ("offbus_gated_hold", "firing") in [(k, f.get("phase")) for k, f in rec["events"]]
    # The same episode a pass later: the attempt counts, so it pages instead of firing again.
    assert await _pass(hold_since) is None
    assert len(rec["fires"]) == 1
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_power_cycle_rate_limited"]


@pytest.mark.asyncio
async def test_the_ladder_then_the_power_cycle_through_the_escalation(monkeypatch, tmp_path, clear_job_state):
    """End to end through _escalate_offbus_stuck_hold: the first forced pass climbs the ladder (and
    records that it ran), the next one reaches the bounded end and fires — not the latch's hold."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert opc.ladder_ran(hold_since) and rec["fires"] == []
    ran = await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None)
    assert ran is True and len(rec["fires"]) == 1


# --- what makes it hold ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scan",
    [
        HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True),
        HolderScan(holders=[], complete=False),
    ],
    ids=["tenant", "unreadable-scan"],
)
async def test_a_tenant_or_an_unreadable_scan_holds(monkeypatch, tmp_path, clear_job_state, scan):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: scan)
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["fires"] == [] and opc.load_state() == {}


@pytest.mark.asyncio
async def test_a_reset_in_its_own_scope_holds(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "tt-reset@1.scope")
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["fires"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup, why",
    [
        ("standback_file", "stand-back marker"),
        ("standback_cmd_yes", "the stand-back check says stand back"),
        ("manual_recovery", "an operator recovery is in progress"),
        ("cmd_error", "could not answer (exit 3)"),
        ("cmd_timeout", "timed out"),
        ("cmd_unstartable", "could not start"),
    ],
)
async def test_anything_objecting_stands_back(monkeypatch, tmp_path, clear_job_state, setup, why):
    """A marker, a check saying yes, an operator recovery — and a check that cannot answer — all
    stand back; only exit 1 clears a check."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    if setup == "standback_file":
        marker = tmp_path / "hold-off"
        marker.touch()
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE", f"{tmp_path / 'absent'}:{marker}")
    elif setup == "standback_cmd_yes":
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD", _script(tmp_path, "sb", "exit 0"))
    elif setup == "manual_recovery":
        monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", _script(tmp_path, "mr", "echo sudo reboot; exit 0"))
    elif setup == "cmd_error":
        monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", _script(tmp_path, "mr", "exit 3"))
    elif setup == "cmd_timeout":
        monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", _script(tmp_path, "mr", "sleep 30"))
        monkeypatch.setattr(opc, "CHECK_TIMEOUT_DEFAULT_SEC", 0.3)
    elif setup == "cmd_unstartable":
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD", str(tmp_path / "missing"))
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["fires"] == [] and opc.load_state() == {}, "standing back never spends the attempt"
    standing = [f["why"] for k, f in rec["events"] if f.get("phase") == "standing_back"]
    assert len(standing) == 1 and why in standing[0]


@pytest.mark.asyncio
async def test_checks_that_say_no_let_it_fire(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", _script(tmp_path, "mr", "exit 1"))
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD", _script(tmp_path, "sb", "exit 1"))
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is True
    assert len(rec["fires"]) == 1


@pytest.mark.asyncio
async def test_an_attempt_within_the_interval_pages_instead(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    opc.save_state({"attempts": [{"at_epoch": opc.now_epoch() - 3600, "episode": "earlier"}], "last_error": "boom"})
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is None
    assert await _pass(hold_since) is None
    assert rec["fires"] == []
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_power_cycle_rate_limited"]
    assert rec["alerts"][0]["last_error"] == "boom"


@pytest.mark.asyncio
async def test_an_attempt_older_than_the_interval_does_not_limit(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", "600")
    opc.save_state({"attempts": [{"at_epoch": opc.now_epoch() - 601, "episode": "earlier"}]})
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is True
    assert len(opc.load_state()["attempts"]) == 2


@pytest.mark.asyncio
async def test_the_mechanism_limits_still_apply(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setattr(
        srv.recovery_mechanism, "auto_recovery_allowed", lambda action, **k: (False, "per-boot cap reached")
    )
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is None
    assert rec["fires"] == []
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_power_cycle_denied"]
    assert "per-boot cap reached" in rec["alerts"][0]["message"]


@pytest.mark.asyncio
async def test_an_unwritable_attempt_record_never_fires(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)

    def no_disk(state, now, episode, reason):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(opc, "record_attempt", no_disk)
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["fires"] == []


@pytest.mark.asyncio
async def test_a_failed_fire_keeps_its_error_for_the_next_page(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)

    def broken(argv, reason, timeout=0):
        raise RuntimeError("power-cycle command exited 2: BMC unreachable")

    monkeypatch.setattr(opc, "fire_command", broken)
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is True
    state = opc.load_state()
    assert "BMC unreachable" in state["last_error"] and len(state["attempts"]) == 1
    assert await _pass(hold_since) is None
    assert "BMC unreachable" in rec["alerts"][0]["last_error"]


def test_fire_command_raises_on_a_non_zero_exit_and_passes_the_reason(tmp_path):
    """The real fire, on harmless stand-in scripts: a non-zero exit raises (so the ledger retracts an
    action that never launched) and the reason reaches the command's environment."""
    out = tmp_path / "reason"
    ok = _script(tmp_path, "ok", f'printf %s "$TT_DEVICE_MCP_POWER_CYCLE_REASON" > {out}')
    _REAL_FIRE([ok], "3/32 off the bus")
    assert out.read_text() == "3/32 off the bus"
    with pytest.raises(RuntimeError, match="exited 2"):
        _REAL_FIRE([_script(tmp_path, "bad", "exit 2")], "x")


# --- the watchdog and the knobs -------------------------------------------------------------------


def test_due_for_the_watchdog_once_per_episode(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/bin/true")
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC", raising=False)
    since = datetime(2026, 10, 13, 19, 29, 0)
    ep = since.isoformat()
    assert not opc.due_for_watchdog(ep, since + timedelta(seconds=400)), "not armed"
    opc.arm(ep)
    assert not opc.due_for_watchdog(ep, since + timedelta(seconds=400)), "the safe rungs have not run"
    opc.note_ladder_ran(ep)
    assert not opc.due_for_watchdog(ep, since + timedelta(seconds=299))
    assert opc.due_for_watchdog(ep, since + timedelta(seconds=300))
    opc.mark_spawned(ep)
    assert not opc.due_for_watchdog(ep, since + timedelta(seconds=900)), "once per episode"
    later = (since + timedelta(hours=1)).isoformat()
    opc.arm(later)
    opc.note_ladder_ran(later)
    assert opc.due_for_watchdog(later, since + timedelta(hours=1, seconds=301)), "a new episode re-arms"
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_CMD")
    assert not opc.due_for_watchdog(later, since + timedelta(hours=1, seconds=301)), "nothing to fire"


def test_the_hold_watchdog_forces_one_pass_at_the_grace(monkeypatch, clear_job_state):
    fired = []
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 1200)
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/bin/true")
    since = datetime(2026, 10, 13, 19, 29, 0)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    _ladder_ran(since.isoformat())
    srv._check_hold_deadline(now=since + timedelta(seconds=299))
    assert fired == []
    srv._check_hold_deadline(now=since + timedelta(seconds=301))
    assert fired == [True]
    assert "gated off-bus grace" in srv.device_hold_escalation_trigger
    srv._check_hold_deadline(now=since + timedelta(seconds=400))
    assert fired == [True], "one forced pass per episode from the grace"


def test_the_knobs_default_and_reject_nonsense(monkeypatch):
    for name in ("TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC", "TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC"):
        monkeypatch.delenv(name, raising=False)
    assert opc.grace_sec() == 300 and opc.min_interval_sec() == 21600
    for bad in ("0", "-5", "soon", ""):
        monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC", bad)
        monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", bad)
        assert opc.grace_sec() == 300 and opc.min_interval_sec() == 21600
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC", "90")
    assert opc.grace_sec() == 90
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "")
    assert opc.power_cycle_argv() is None
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "cycle 'unterminated")
    assert opc.power_cycle_argv() is None, "an unparseable command is no command"


def test_a_corrupt_state_file_reads_empty_and_a_bad_stamp_counts_as_now(monkeypatch):
    (opc.health_dir() / opc.STATE_FILE).write_text("{not json")
    assert opc.load_state() == {}
    now = 1_000_000.0
    assert opc.last_attempt_within({"attempts": [{"at_epoch": "?"}]}, now, 600) == now
    assert opc.last_attempt_within({"attempts": [{"at_epoch": now - 700}]}, now, 600) is None
