# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bounded off-bus hold (spec 04 I27).

With the host reset gate refusing every reset because chips are off the bus, the hold used to stand
until an operator power-cycled the host. These pin the bounded end: past the grace, once the safe
rungs ran, idle and with nothing objecting, the broker runs the host's power-cycle command once per
interval — and otherwise holds or pages, never loops. ``fire_command`` is always a fake here; no test
may run a real power-cycle command (conftest makes the real one a tripwire)."""

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import patch_recovery
from tests.test_device_safety import _arm_offbus_stuck_hold
from tt_device_mcp import alert
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health.recovery import galaxy, pcie_guard
from tt_device_mcp.health.recovery import offbus_power_cycle as opc

GATE_WHY = "3 chip(s) off the bus and TT_DEVICE_MCP_HOST_RESET_GATE=hold"
OFF_BUS, EXPECTED = 3, 32
# Taken at import, before conftest swaps in its tripwire; only ever called on stand-in scripts.
_REAL_FIRE = opc.fire_command
_REAL_GATE = pcie_guard.host_reset_gate


def _gated(monkeypatch, tmp_path, *, held_sec=600, cmd="/usr/local/sbin/host-power-cycle --now", manual="exit 1"):
    """An idle off-bus hold the gate refuses to reset, held ``held_sec``, with the power-cycle command
    set (``cmd=None`` leaves it unset) and a manual-recovery check running ``manual`` (default: a
    clean "none seen"; ``None`` leaves it unset, and conftest's TTDEV_ROOT has none installed).
    Returns (hold_since, rec) where rec collects the fake fires (each with the state file as it stood
    when the command ran), alerts and journal events."""
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
    if manual is not None:
        monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", _script(tmp_path, "mr-default", manual))
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
@pytest.mark.parametrize("blocker", ["tenant", "scope", "standback"])
async def test_a_blocked_power_cycle_pages_once_past_its_bound(monkeypatch, tmp_path, clear_job_state, blocker):
    """Whatever keeps the cycle from firing, the hold does not stand silent: past the grace plus
    the blocked-page bound it pages once, high severity, naming the blocker, and still never fires."""
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=300 + 1800 - 10)
    if blocker == "tenant":
        monkeypatch.setattr(
            srv,
            "enumerate_device_holders",
            lambda: HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True),
        )
    elif blocker == "scope":
        monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "tt-reset@1.scope")
    else:
        marker = tmp_path / "hold-off"
        marker.touch()
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_FILE", str(marker))
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["alerts"] == [], "inside the bound it only holds"
    later = (datetime.fromisoformat(hold_since) - timedelta(seconds=20)).isoformat()
    srv.device_hold_episode_since = later
    _ladder_ran(later)
    for _ in range(3):
        assert await _pass(later) is False
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_power_cycle_blocked"], "one page per episode"
    page = rec["alerts"][0]
    assert page["severity"] == "high" and page["host_at_risk"] is True and page["held_since"] == later
    assert rec["fires"] == []


def test_a_page_the_alert_hook_drops_is_sent_again(monkeypatch):
    """A page dropped because the hook is busy is retried on the next pass; journaled only once."""
    sent, events = [], []
    monkeypatch.setattr(opc, "health_event", lambda kind, **f: events.append(kind))
    monkeypatch.setattr(alert, "alert_argv", lambda: ["/bin/true"])
    results = iter([None, object()])
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: sent.append(event) or next(results))
    opc.arm("2026-10-13T19:29:00")
    for _ in range(3):
        opc.page_once("offbus_gated_hold_page", lambda m: None, "held")
    assert len(sent) == 2 and events.count("offbus_gated_hold_page") == 1


def test_the_blocked_page_bound_defaults_and_rejects_nonsense(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_BLOCKED_PAGE_SEC", raising=False)
    assert opc.blocked_page_sec() == 1800
    for bad in ("0", "-1", "later"):
        monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_BLOCKED_PAGE_SEC", bad)
        assert opc.blocked_page_sec() == 1800
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_BLOCKED_PAGE_SEC", "600")
    assert opc.blocked_page_sec() == 600


@pytest.mark.asyncio
async def test_a_latch_never_blocks_a_rung_after_its_boot_nor_the_power_cycle_on_it(
    monkeypatch, tmp_path, clear_job_state
):
    """The I25 latch is scoped to its boot. After a later boot the real gate allows resets again and
    the drop goes back to the ladder (no hold, no cycle); on the latched boot the gate refuses every
    reset, and that refusal is exactly what the bounded power cycle answers."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", _REAL_GATE)
    monkeypatch.setenv("TT_DEVICE_MCP_HOST_RESET_GATE", "off")
    (pcie_guard.health_dir() / pcie_guard.LATCH_FILE).write_text(
        json.dumps({"boot_id": "boot-a", "latched_on_boot": "boot-b"})
    )
    _ladder_ran(hold_since)
    monkeypatch.setattr(pcie_guard, "_boot_id", lambda: "boot-c")
    assert await _pass(hold_since) is None
    assert rec["fires"] == [] and rec["alerts"] == []
    monkeypatch.setattr(pcie_guard, "_boot_id", lambda: "boot-b")
    assert await _pass(hold_since) is True
    assert len(rec["fires"]) == 1 and "--clear-hang-latch" in rec["fires"][0]["reason"]


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
async def test_no_manual_recovery_check_never_fires_and_pages_once(monkeypatch, tmp_path, clear_job_state):
    """A power-cycle command with no manual-recovery check (unset, none installed) is unguarded: it
    never fires, past the grace or not, and pages once, high severity. Fail closed, not open."""
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=300 + 1800 + 60, manual=None)
    _ladder_ran(hold_since)
    for _ in range(3):
        assert await _pass(hold_since) is None
    assert rec["fires"] == [] and opc.load_state() == {}
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_power_cycle_unguarded"]
    assert rec["alerts"][0]["severity"] == "high" and rec["alerts"][0]["host_at_risk"] is True
    assert "no manual-recovery check" in opc.standback_reason(), "standback_reason fails closed on its own too"
    monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", "'unbalanced")
    assert opc.manual_recovery_argv() is None and await _pass(hold_since) is None
    assert rec["fires"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body, fires", [("exit 1", True), ("echo sudo reboot; exit 0", False)])
async def test_the_installed_check_is_the_default(monkeypatch, tmp_path, clear_job_state, body, fires):
    """Unset, the check is the template the installer stages under TTDEV_ROOT."""
    hold_since, rec = _gated(monkeypatch, tmp_path, manual=None)
    root = tmp_path / "root"
    root.mkdir()
    _script(root, opc.MANUAL_RECOVERY_CHECK, body)
    monkeypatch.setenv("TTDEV_ROOT", str(root))
    assert opc.manual_recovery_argv() == [str(root / opc.MANUAL_RECOVERY_CHECK)]
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is (True if fires else False)
    assert len(rec["fires"]) == (1 if fires else 0)
    assert "offbus_power_cycle_unguarded" not in [a["kind"] for a in rec["alerts"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["unreadable", "raises", "times_out"])
async def test_a_manual_recovery_check_that_cannot_answer_holds_until_the_bound(
    monkeypatch, tmp_path, clear_job_state, source
):
    """Evidence the check cannot read, a check that raises and one that times out are all a possible
    operator recovery: the cycle holds, the reason is journaled, every pass asks again, and past
    grace + the blocked-page bound it pages once — the same bounded end as any other blocker."""
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=300 + 1800 - 10)
    if source == "unreadable":
        monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, ["Failed to open journal"], rc=1))
        monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", f"bash {_CHECK}")
        why = "could not answer (exit 2)"
    elif source == "raises":

        def boom(argv, timeout, env=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(opc, "_run", boom)
        why = "RuntimeError('boom')"
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", _script(tmp_path, "mr", "sleep 30"))
        monkeypatch.setattr(opc, "CHECK_TIMEOUT_DEFAULT_SEC", 0.3)
        why = "timed out"
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["alerts"] == []
    later = (datetime.fromisoformat(hold_since) - timedelta(seconds=20)).isoformat()
    srv.device_hold_episode_since = later
    _ladder_ran(later)
    for _ in range(2):
        assert await _pass(later) is False
    assert rec["fires"] == [] and opc.load_state() == {}
    standing = [f["why"] for k, f in rec["events"] if f.get("phase") == "standing_back"]
    assert standing and all(why in w for w in standing)
    assert [a["kind"] for a in rec["alerts"]] == ["offbus_power_cycle_blocked"]
    assert why in rec["alerts"][0]["why"]


@pytest.mark.asyncio
async def test_a_clean_manual_recovery_template_lets_it_fire(monkeypatch, tmp_path, clear_job_state):
    """The shipped template reading a readable journal with no recovery and no auth.log says none,
    and the cycle runs."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, ["-- No entries --"]))
    monkeypatch.setenv("TT_MANUAL_RECOVERY_AUTHLOG", str(tmp_path / "no-auth.log"))
    monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", f"bash {_CHECK}")
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is True
    assert len(rec["fires"]) == 1


@pytest.mark.asyncio
async def test_a_tenant_arriving_during_the_checks_holds(monkeypatch, tmp_path, clear_job_state):
    """The stand-back checks can take seconds: the holder scan is read again after them."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    scans = iter(
        [
            HolderScan(holders=[], complete=True),
            HolderScan(holders=[DeviceHolder(pid=4242, uid=srv.MIN_TENANT_UID)], complete=True),
        ]
    )
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: next(scans))
    _ladder_ran(hold_since)
    assert await _pass(hold_since) is False
    assert rec["fires"] == [] and opc.load_state() == {}


def test_the_pages_name_the_host(monkeypatch):
    sent = []
    monkeypatch.setattr(opc, "health_event", lambda kind, **f: None)
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: sent.append(event))
    opc.arm("2026-10-13T19:29:00")
    opc.page_once("offbus_power_cycle_unguarded", lambda m: None, "held")
    opc.page_if_blocked("2026-10-13T19:29:00", 10**6, lambda m: None, "a marker")
    assert [e["host"] for e in sent] == [socket.gethostname()] * 2


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


# --- the manual-recovery check template -----------------------------------------------------------

_CHECK = str(Path(__file__).resolve().parent.parent / "deploy" / "tt-manual-recovery-check.sh")


def _journal(tmp_path, lines, rc=0):
    """A stand-in journalctl printing ``lines`` (and recording its argv), never the real journal."""
    body = "\n".join(f"echo '{line}'" for line in lines)
    return _script(tmp_path, "journalctl", f'echo "$@" > {tmp_path / "argv"}\n{body}\nexit {rc}')


@pytest.mark.parametrize(
    "lines, rc",
    [
        (["... sudo[1]: ops : TTY=pts/0 ; PWD=/ ; USER=root ; COMMAND=/usr/sbin/reboot"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/systemctl reboot"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool -H bmc chassis power cycle"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool chassis power off"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool power soft"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool -I lanplus mc reset cold"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool raw 0x06 0x02"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool raw 0x00 0x02 0x02"], 0),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool sensor list"], 1),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool chassis power status"], 1),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool dcmi power reading"], 1),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool raw 0x06 0x01"], 1),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/ipmitool chassis status"], 1),
        (["... sudo[1]: ops : USER=root ; COMMAND=/usr/bin/apt-get install halt-utils"], 1),
        ([], 1),
    ],
    ids=[
        "reboot",
        "systemctl-reboot",
        "ipmitool-power",
        "ipmitool-power-off",
        "ipmitool-power-soft",
        "ipmitool-mc-reset",
        "ipmitool-raw-bmc-reset",
        "ipmitool-raw-chassis-control",
        "ipmitool-read",
        "ipmitool-power-status",
        "ipmitool-dcmi-power-reading",
        "ipmitool-raw-get-device-id",
        "ipmitool-chassis-status",
        "unrelated",
        "nothing",
    ],
)
def test_the_manual_recovery_check_template(tmp_path, monkeypatch, lines, rc):
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, lines))
    monkeypatch.setenv("TT_MANUAL_RECOVERY_AUTHLOG", str(tmp_path / "no-auth.log"))
    p = subprocess.run(["bash", _CHECK, "45"], capture_output=True, text=True)
    assert p.returncode == rc, p.stderr
    assert "_COMM=sudo" in (tmp_path / "argv").read_text() and "-45min" in (tmp_path / "argv").read_text()


def _stamp(ago_min, iso=True):
    t = datetime.now().astimezone() - timedelta(minutes=ago_min)
    return t.isoformat() if iso else t.strftime("%b %d %H:%M:%S")


@pytest.mark.parametrize(
    "ago_min, iso, rc",
    [(5, True, 0), (5, False, 0), (90, True, 1), (90, False, 1)],
    ids=["rfc3339-recent", "syslog-recent", "rfc3339-old", "syslog-old"],
)
def test_the_manual_recovery_check_reads_auth_log_within_the_window(tmp_path, monkeypatch, ago_min, iso, rc):
    """A sudo reboot only auth.log shows counts inside the window and not before it, in both of
    auth.log's stamp styles; an unrelated sudo line never counts."""
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, []))
    log = tmp_path / "auth.log"
    log.write_text(
        f"{_stamp(1, iso)} host sudo:   ops : TTY=pts/0 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/ls\n"
        f"{_stamp(ago_min, iso)} host sudo:   ops : TTY=pts/0 ; PWD=/ ; USER=root ; COMMAND=/usr/sbin/reboot\n"
    )
    monkeypatch.setenv("TT_MANUAL_RECOVERY_AUTHLOG", str(log))
    p = subprocess.run(["bash", _CHECK, "30"], capture_output=True, text=True)
    assert p.returncode == rc, p.stderr
    assert ("COMMAND=/usr/sbin/reboot" in p.stdout) == (rc == 0)


def test_the_manual_recovery_check_cannot_answer_on_an_unreadable_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, ["Failed to open journal"], rc=1))
    assert subprocess.run(["bash", _CHECK], capture_output=True).returncode == 2
    assert subprocess.run(["bash", _CHECK, "soon"], capture_output=True).returncode == 2
    hint = "Hint: You are currently not seeing messages from other users and the system."
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, [hint]))
    monkeypatch.setenv("TT_MANUAL_RECOVERY_AUTHLOG", str(tmp_path / "no-auth.log"))
    assert subprocess.run(["bash", _CHECK], capture_output=True).returncode == 2, "a partial journal is unreadable"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_the_manual_recovery_check_cannot_answer_on_an_unreadable_auth_log(tmp_path, monkeypatch):
    """An auth.log that exists but cannot be read is unknown, not empty; only an absent one is skipped."""
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, []))
    log = tmp_path / "auth.log"
    log.write_text("")
    log.chmod(0)
    monkeypatch.setenv("TT_MANUAL_RECOVERY_AUTHLOG", str(log))
    assert subprocess.run(["bash", _CHECK], capture_output=True).returncode == 2
    log.unlink()
    assert subprocess.run(["bash", _CHECK], capture_output=True).returncode == 1


def test_the_manual_recovery_check_cannot_answer_when_auth_log_fails_to_read(tmp_path, monkeypatch):
    """A read that fails past the permission check (here a directory) is unknown, never 'no recovery'."""
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, []))
    monkeypatch.setenv("TT_MANUAL_RECOVERY_AUTHLOG", str(tmp_path))
    p = subprocess.run(["bash", _CHECK], capture_output=True, text=True)
    assert p.returncode == 2, p.stderr


def test_the_manual_recovery_check_never_globs_an_auth_log_line(tmp_path, monkeypatch):
    """A '*' leading a line stays a '*': it never expands to a file name that reads as a recent stamp."""
    monkeypatch.setenv("TT_MANUAL_RECOVERY_JOURNALCTL", _journal(tmp_path, []))
    log = tmp_path / "auth.log"
    log.write_text("* host sudo:   ops : TTY=pts/0 ; PWD=/ ; USER=root ; COMMAND=/usr/sbin/reboot\n")
    monkeypatch.setenv("TT_MANUAL_RECOVERY_AUTHLOG", str(log))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / _stamp(5)).touch()
    assert subprocess.run(["bash", _CHECK, "30"], capture_output=True, cwd=cwd).returncode == 1


# --- the BMC's IPMI failed the tray re-power ------------------------------------------------------

IPMI_FAIL = "ubb tray reset exited 1: No data available"


def _ipmi_failed(monkeypatch, hold_since, ago_sec):
    """The gate allows resets, but the BMC's IPMI failed this episode's tray re-power ``ago_sec`` ago."""
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.delenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", raising=False)
    opc.note_ipmi_unavailable(hold_since, IPMI_FAIL, now=datetime.now() - timedelta(seconds=ago_sec))


@pytest.mark.asyncio
async def test_a_failed_bmc_ipmi_hands_off_past_its_bound(monkeypatch, tmp_path, clear_job_state):
    """The 10-14 incident: the tray re-power returned no data, the gate still allowed resets, and the
    hold re-ran the ladder and paged for an hour. Past the bound, once the ladder ran, it fires once."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    _ipmi_failed(monkeypatch, hold_since, 400)
    assert await _pass(hold_since) is None, "the ladder runs once first"
    assert rec["fires"] == []
    opc.note_ladder_ran(hold_since)
    assert await _pass(hold_since) is True
    assert len(rec["fires"]) == 1
    assert "IPMI" in rec["fires"][0]["reason"] and "No data available" in rec["fires"][0]["reason"]
    assert ("offbus_gated_hold", "ipmi_unavailable") in [(k, f.get("phase")) for k, f in rec["events"]]


@pytest.mark.asyncio
async def test_a_regressed_tray_rung_hands_off_past_the_grace(monkeypatch, tmp_path, clear_job_state):
    """A tray re-power on this boot took chips on other trays off the bus: the gate still allows resets,
    but the per-tray rung is declined, so the hold takes the same bounded end (ladder once, grace, one
    power cycle) instead of re-running the ladder."""
    hold_since, rec = _gated(monkeypatch, tmp_path, held_sec=120)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    assert await _pass(hold_since) is None, "nothing latched: back to the ladder"
    pcie_guard.disable_tray_rung(tray=[3], new_off=["5", "13"], signature="position5")
    assert await _pass(hold_since) is None, "the ladder runs once first"
    opc.note_ladder_ran(hold_since)
    assert await _pass(hold_since) is False, "inside the grace: hold"
    assert rec["fires"] == []
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_GATED_GRACE_SEC", "60")
    assert await _pass(hold_since) is True
    assert len(rec["fires"]) == 1
    assert "other trays off the bus" in rec["fires"][0]["reason"]


@pytest.mark.asyncio
async def test_within_the_ipmi_bound_it_holds_and_the_bound_is_configurable(monkeypatch, tmp_path, clear_job_state):
    hold_since, rec = _gated(monkeypatch, tmp_path)
    _ipmi_failed(monkeypatch, hold_since, 120)
    opc.note_ladder_ran(hold_since)
    assert await _pass(hold_since) is False, "inside the bound: hold, do not re-run the futile ladder"
    assert rec["fires"] == []
    monkeypatch.setenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", "60")
    assert await _pass(hold_since) is True and len(rec["fires"]) == 1


@pytest.mark.asyncio
async def test_a_cycle_in_progress_stands_the_ipmi_hand_off_back(monkeypatch, tmp_path, clear_job_state):
    """A stand-back command that exits 0 (e.g. a host-cycle tool reporting a cycle in progress) holds."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    _ipmi_failed(monkeypatch, hold_since, 400)
    opc.note_ladder_ran(hold_since)
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_STANDBACK_CMD", _script(tmp_path, "busy", "echo cycling; exit 0"))
    assert await _pass(hold_since) is False
    assert rec["fires"] == []


@pytest.mark.asyncio
async def test_the_whole_box_is_re_read_before_the_cycle(monkeypatch, tmp_path, clear_job_state):
    """A tray re-power can drop or bring back other trays: the pass re-reads every chip before firing,
    and a box back on the bus never cycles."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    _ipmi_failed(monkeypatch, hold_since, 400)
    opc.note_ladder_ran(hold_since)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(EXPECTED)})
    assert await _pass(hold_since) is None
    assert rec["fires"] == []
    assert ("offbus_gated_hold", "back_on_bus") in [(k, f.get("phase")) for k, f in rec["events"]]


def test_only_a_bmc_ipmi_failure_starts_the_clock(monkeypatch, clear_job_state):
    from tt_device_mcp.health.recovery.stages.ubb_tray import BmcIpmiError

    since = datetime.now().isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", since)
    monkeypatch.setattr(opc, "health_event", lambda *a, **k: None)
    srv.galaxy_recovery._note_bmc_ipmi_failure(RuntimeError("tray map disagrees"))
    assert opc.ipmi_unavailable_age_sec(since) is None
    srv.galaxy_recovery._note_bmc_ipmi_failure(BmcIpmiError(IPMI_FAIL))
    assert opc.ipmi_unavailable_age_sec(since) is not None and opc.ipmi_detail(since) == IPMI_FAIL


@pytest.mark.asyncio
async def test_a_tray_sweep_the_bmc_failed_starts_the_clock(monkeypatch, clear_job_state):
    from tt_device_mcp.health.recovery.stages.ubb_tray import BmcIpmiError

    g = srv.galaxy_recovery
    since = datetime.now().isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", since)
    monkeypatch.setattr(opc, "health_event", lambda *a, **k: None)
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")

    def no_data(*a, **k):
        raise BmcIpmiError(IPMI_FAIL)

    async def ok_reset(*a, **k):
        return 0, ""

    monkeypatch.setattr(galaxy, "_fire_ubb_reset", no_data)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", ok_reset)
    await g._issue_all_resets_back_to_back(0b1000, [3], [4], 4, lambda m: None, do_sbr=False)
    assert opc.ipmi_unavailable_age_sec(since) is not None


def test_the_watchdog_forces_a_pass_at_the_ipmi_bound(monkeypatch, clear_job_state):
    fired = []
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 1200)
    monkeypatch.setattr(opc, "health_event", lambda *a, **k: None)
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/bin/true")
    monkeypatch.delenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", raising=False)
    since = datetime(2026, 10, 14, 10, 49, 0)
    ep = since.isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", ep)
    failed = since + timedelta(seconds=100)
    opc.note_ipmi_unavailable(ep, IPMI_FAIL, now=failed)
    opc.note_ladder_ran(ep)
    assert not opc.due_for_watchdog(ep, failed + timedelta(seconds=299))
    srv._check_hold_deadline(now=failed + timedelta(seconds=299))
    assert fired == []
    srv._check_hold_deadline(now=failed + timedelta(seconds=301))
    assert fired == [True]
    assert "BMC IPMI hand-off bound" in srv.device_hold_escalation_trigger


def test_the_ipmi_bound_defaults_and_rejects_nonsense(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", raising=False)
    assert opc.ipmi_handoff_sec() == 300
    for bad in ("0", "-5", "soon", ""):
        monkeypatch.setenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", bad)
        assert opc.ipmi_handoff_sec() == 300
    monkeypatch.setenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", "120")
    assert opc.ipmi_handoff_sec() == 120


def test_the_ladder_noted_before_the_ipmi_failure_still_counts(monkeypatch, clear_job_state):
    """The real order: the escalation notes the ladder before its tray rung fails over IPMI. With the gate
    allowing resets nothing tracked the episode yet; the ladder must still count, or the watchdog never
    forces the pass at the bound and the hold waits for the next ceiling window."""
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/bin/true")
    monkeypatch.delenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", raising=False)
    monkeypatch.setattr(opc, "health_event", lambda *a, **k: None)
    since = datetime(2026, 10, 14, 10, 49, 0)
    ep = since.isoformat()
    opc.note_ladder_ran(ep)
    assert opc.ladder_ran(ep)
    assert not opc.due_for_watchdog(ep, since + timedelta(hours=1)), "tracking alone forces nothing"
    failed = since + timedelta(seconds=100)
    opc.note_ipmi_unavailable(ep, IPMI_FAIL, now=failed)
    assert opc.ladder_ran(ep), "the IPMI note keeps the ladder it found"
    assert not opc.due_for_watchdog(ep, failed + timedelta(seconds=299))
    assert opc.due_for_watchdog(ep, failed + timedelta(seconds=300))
    opc.note_ladder_ran("")
    assert opc.ladder_ran(ep), "an empty episode tracks nothing"


@pytest.mark.asyncio
async def test_the_escalations_own_tray_rung_failing_over_ipmi_hands_off_at_the_bound(
    monkeypatch, tmp_path, clear_job_state
):
    """End to end through _escalate_offbus_stuck_hold with the gate allowing resets: the forced pass runs
    the ladder, whose tray rung the BMC's IPMI fails. Past the bound the watchdog is due and its forced
    pass fires the power cycle once, without a second ladder run."""
    from tt_device_mcp.health.recovery.stages.ubb_tray import BmcIpmiError

    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.delenv("TT_DEVICE_MCP_IPMI_UNAVAILABLE_HANDOFF_SEC", raising=False)
    g = srv.galaxy_recovery
    tray_rungs = []

    async def tray_rung(beats, off_bus, expected, log):
        tray_rungs.append(off_bus)
        g._note_bmc_ipmi_failure(BmcIpmiError(IPMI_FAIL))
        return False

    monkeypatch.setattr(g, "_attempt_ubb_tray_reset", tray_rung)
    await g._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert tray_rungs, "the ladder reached the tray rung"
    assert opc.ladder_ran(hold_since) and opc.ipmi_unavailable_age_sec(hold_since) is not None
    assert rec["fires"] == []
    now = datetime.now()
    assert not opc.due_for_watchdog(hold_since, now)
    assert opc.due_for_watchdog(hold_since, now + timedelta(seconds=opc.ipmi_handoff_sec() + 1))
    opc._pending["ipmi_at"] -= timedelta(seconds=opc.ipmi_handoff_sec() + 1)  # the bound has passed
    ran = await g._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert ran is True and len(rec["fires"]) == 1
    assert len(tray_rungs) == 1, "the ladder ran once, not again before the cycle"


# --- a failed or regressed tray walk hands off; no below-floor mesh reset after it ----------------


def _tray_walk_episode(monkeypatch, tmp_path, *, cmd="/usr/local/sbin/host-power-cycle --now", drops=()):
    """The 10-14 replay: chips 0-7 (tray 1) off the bus and isolated, below the 16-chip floor, the gate
    allowing resets, the per-tray walk opted in and the bridge rung not bringing them back. Re-powering
    tray 1 leaves 0-7 off and takes ``drops`` off with them. Returns (hold_since, rec, counters, state)."""
    hold_since, rec = _gated(monkeypatch, tmp_path, cmd=cmd)
    counters = _arm_offbus_stuck_hold(
        monkeypatch, off_bus=8, isolated={str(c) for c in range(8)}, sbr_recovers=False, reset_result=False
    )
    srv.device_hold_episode_since = hold_since
    for mod in (opc, galaxy):  # the re-arm patched the journal again; keep recording into rec
        monkeypatch.setattr(mod, "health_event", lambda kind, **f: rec["events"].append((kind, f)))
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pcie_guard, "_TRAY_RUNG_DISABLED", None)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_UBB_SINGLE_TRAY_WALK", "1")  # the single-tray walk under test is opt-in
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC", "0.5")  # floor = 16
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_TRAY_WALK_FAILED_HANDOFF_SEC", raising=False)
    srv.fsm.set_latch("ubb_reset_fired", False)
    state = {"off": set(range(8)), "fired": [], "verifies": []}
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32) if i not in state["off"]})

    def fire(bitmap, ids):
        state["fired"].append(bitmap)
        state["off"] |= set(drops)

    async def verify(expected, log, run_fabric=True, **_):
        state["verifies"].append(run_fabric)
        return False, {"snapshot": {"ok": False}}

    monkeypatch.setattr(galaxy, "_fire_ubb_reset", fire, raising=False)
    patch_recovery(monkeypatch, "_verify_device", verify)
    return hold_since, rec, counters, state


@pytest.mark.asyncio
async def test_a_regressed_tray_walk_hands_off_to_the_power_cycle_not_the_mesh_reset(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """10-14, 15:23-15:32: chips 0-7 off, the walk's tray re-power took 16-31 off too, and the below-floor
    mesh reset that followed overran 180 s and exited 1, after which nothing ran. Now the walk stops at
    the regression, no mesh reset runs, the hold stands, and the power cycle fires once at the hand-off
    bound without a second walk. The device is never released: every verify was a full one, fabric
    included, and none passed. Fails on base, which runs the mesh reset."""
    hold_since, rec, counters, state = _tray_walk_episode(monkeypatch, tmp_path, drops=range(16, 32))
    g = srv.galaxy_recovery
    ran = await g._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert ran is True
    assert state["fired"] == [0x01], "only the affected tray was re-powered"
    assert counters["resets"] == 0, "no below-floor mesh reset after a walk that regressed the mesh"
    kinds = [k for k, _ in rec["events"]]
    assert "ubb_reset_regressed_offbus" in kinds and "galaxy_reset_skipped_after_tray_walk" in kinds
    assert "stuck_hold_galaxy_reset_below_floor_attempt" not in kinds
    assert opc.tray_walk_failed_age_sec(hold_since) is not None and rec["fires"] == []
    assert counters["power_cycles"] == 0 and counters["reboots"] == 0, "no host rung before the bound"
    now = datetime.now()
    assert not opc.due_for_watchdog(hold_since, now)
    assert opc.due_for_watchdog(hold_since, now + timedelta(seconds=opc.tray_walk_handoff_sec() + 1))
    opc._pending["tray_walk_at"] -= timedelta(seconds=opc.tray_walk_handoff_sec() + 1)  # the bound has passed
    ran = await g._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert ran is True and len(rec["fires"]) == 1
    assert "per-tray reset walk did not recover" in rec["fires"][0]["reason"]
    assert state["fired"] == [0x01] and counters["resets"] == 0, "no second walk, no mesh reset"
    # Goal: no release without the full check, fabric included.
    assert state["verifies"] and all(state["verifies"]), "every verify ran the fabric pass"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


@pytest.mark.asyncio
async def test_by_default_the_one_tray_drop_is_never_walked(monkeypatch, tmp_path, clear_job_state, galaxy_trays):
    """The same 10-14 drop (chips 0-7, all on tray 1, below the floor) without the single-tray opt-in:
    the walk declines, so tray 1 is never re-powered and 16-31 cannot follow it off the bus. The device
    stays held; nothing releases it without the full check, fabric included."""
    hold_since, rec, counters, state = _tray_walk_episode(monkeypatch, tmp_path, drops=range(16, 32))
    monkeypatch.delenv("TT_DEVICE_MCP_UBB_SINGLE_TRAY_WALK", raising=False)
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert state["fired"] == [], "no single-tray re-power for a below-floor drop on one tray"
    assert state["off"] == set(range(8)), "the other trays stayed on the bus"
    assert "ubb_reset_declined_single_tray" in [k for k, _ in rec["events"]]
    assert all(state["verifies"]), "every verify ran the fabric pass"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "the device stays held"


@pytest.mark.asyncio
async def test_a_walk_that_did_not_recover_skips_the_mesh_reset_on_every_later_pass(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """The other failure: the walk re-powered tray 1 and its chips stayed off (nothing worse). No mesh
    reset now, nor on a later pass of the episode, where the walk's latch declines a second walk."""
    hold_since, rec, counters, state = _tray_walk_episode(monkeypatch, tmp_path)
    g = srv.galaxy_recovery
    await g._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert state["fired"] == [0x01] and counters["resets"] == 0
    assert "ubb_reset_did_not_recover" in [k for k, _ in rec["events"]]
    await g._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert state["fired"] == [0x01] and counters["resets"] == 0, "the latch holds the walk, still no mesh reset"
    assert rec["fires"] == [] and counters["dirty_cleared"] == 0


@pytest.mark.asyncio
async def test_with_no_power_cycle_command_a_failed_walk_climbs_without_the_sweep(
    monkeypatch, tmp_path, clear_job_state, galaxy_trays
):
    """Where the bounded power cycle cannot fire (no command), the failed walk pages and climbs to the
    host rung, skipping the last-chance sweep: its back-to-back resets include the same mesh reset."""
    hold_since, rec, counters, state = _tray_walk_episode(monkeypatch, tmp_path, cmd=None, drops=range(16, 32))
    sweeps = []

    async def sweep(*a, **k):
        sweeps.append(True)
        return False

    monkeypatch.setattr(srv.galaxy_recovery, "_settle_and_verify_before_host_rung", sweep)
    await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], EXPECTED, lambda m: None, force=True)
    assert counters["resets"] == 0 and sweeps == []
    assert "offbus_gated_hold_page" in [a.get("kind") for a in rec["alerts"]]
    assert "reset_unrecoverable_power_cycle_required" in [k for k, _ in rec["events"]], "the climb is loud"
    assert counters["dirty_cleared"] == 0


def test_the_watchdog_forces_a_pass_at_the_tray_walk_bound(monkeypatch, clear_job_state):
    fired = []
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 86400)
    monkeypatch.setattr(opc, "health_event", lambda *a, **k: None)
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/bin/true")
    monkeypatch.delenv("TT_DEVICE_MCP_TRAY_WALK_FAILED_HANDOFF_SEC", raising=False)
    since = datetime(2026, 10, 14, 15, 23, 41)
    ep = since.isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", ep)
    opc.note_ladder_ran(ep)
    failed = since + timedelta(seconds=240)
    opc.note_tray_walk_failed(ep, "8/32 chip(s) off the bus after the walk", now=failed)
    srv._check_hold_deadline(now=failed + timedelta(seconds=299))
    assert fired == []
    srv._check_hold_deadline(now=failed + timedelta(seconds=301))
    assert fired == [True]
    assert "failed tray walk hand-off bound" in srv.device_hold_escalation_trigger


def test_the_tray_walk_bound_defaults_and_rejects_nonsense(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_TRAY_WALK_FAILED_HANDOFF_SEC", raising=False)
    assert opc.tray_walk_handoff_sec() == 300
    for bad in ("0", "-5", "soon", ""):
        monkeypatch.setenv("TT_DEVICE_MCP_TRAY_WALK_FAILED_HANDOFF_SEC", bad)
        assert opc.tray_walk_handoff_sec() == 300
    monkeypatch.setenv("TT_DEVICE_MCP_TRAY_WALK_FAILED_HANDOFF_SEC", "60")
    assert opc.tray_walk_handoff_sec() == 60
