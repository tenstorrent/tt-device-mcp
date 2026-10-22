# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A whole tray off the bus reaches the power cycle on every boot (spec 04 I27, I37, I65).

Replays a measured hold: the box came back from the broker's power cycle with one tray (8/32
chips) still off; the broker asked for a second cycle that boot, which never took the host down
(the BMC refused it); hours later the broker restarted with the tray still off. The second request
used to stop every later cycle on that boot (a per-boot cap, then the off-bus path's own 6 h
attempt limit), with no deadline on /health: an open-ended hold. Here each wait is the I65
cooldown, counted from the latest cycle or boot of any cause, stated on /health, and the cycle fires
the moment it ends. No test reaches a real reset or power cycle."""

from datetime import datetime

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off, rig  # noqa: F401
from tests.test_power_cycle_cooldown import (  # noqa: F401
    LADDER,
    T0,
    _bound_watchdog,
    _cycles,
    _ledger,
    _tray_off,
    _watchdog,
    cool,
)
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery import offbus_power_cycle as opc

# The measured timeline, on the test clock: the broker's cycle (boot A went down), the boot it
# brought, the second request on that boot (it never went down), and the broker restart hours later.
CYCLE_1 = T0
BOOT_B = T0 + 120
CYCLE_2 = T0 + 1800
RESTART = T0 + 5 * 3600 + 25 * 60


def _replay(cool, monkeypatch, *, now, boot_btime=BOOT_B, boot_id="boot-B"):  # noqa: F811
    """The ledger and the off-bus attempt file as that boot left them, tray 2 off, the clock at ``now``."""
    cool["boot"].update(id=boot_id, btime=boot_btime)
    _ledger(
        {"action": "power-cycle", "boot_id": "boot-A", "at_epoch": CYCLE_1, "at": "x"},
        {"action": "power-cycle", "boot_id": "boot-B", "at_epoch": CYCLE_2, "at": "x"},
    )
    opc.save_state({"attempts": [{"at_epoch": CYCLE_2, "episode": "earlier", "reason": "tray", "boot_id": "boot-B"}]})
    monkeypatch.setattr(opc, "now_epoch", lambda: cool["clk"][0])
    cool["clk"][0] = now
    _tray_off(cool)


def _deadline(cool):  # noqa: F811
    state = cool["g"].mechanism.power_cycle_cooldown_state
    return None if state is None else state["until"]


def _health_cooldown(cool, monkeypatch, at):  # noqa: F811
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("tray off", srv.device_hold_episode_since))
    return srv._health_payload(now=datetime.fromtimestamp(at)).get("power_cycle_cooldown")


@pytest.mark.asyncio
async def test_a_restart_hours_after_a_cycle_that_never_went_down_cycles_at_once(cool, monkeypatch):  # noqa: F811
    """The measured case: restart 5 h after the second request on the same boot, tray still off. Two
    cycles healed nothing, so the cooldown is 1 h from the latest of them; it ended long ago."""
    _replay(cool, monkeypatch, now=RESTART)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 1 and not LADDER & set(cool["order"])
    assert cool["g"].mechanism.power_cycle_retry is None


@pytest.mark.asyncio
async def test_inside_the_cooldown_the_hold_shows_its_deadline_and_cycles_when_it_ends(cool, monkeypatch):  # noqa: F811
    """Restart 10 min after the second request: held to its stated end, with the full ladder once in
    the 10-min hold bound's run (spec 04 I76 with I73); then the watchdog asks once and the cycle
    fires, whatever the per-boot count."""
    # The hold opened at boot B's startup; the restarted broker keeps it (same boot), windows fresh.
    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.fromtimestamp(BOOT_B + 60).isoformat())
    for name in ("device_hold_escalate_bucket", "device_hold_deadline_bucket"):
        monkeypatch.setattr(srv, name, 0)
    _replay(cool, monkeypatch, now=CYCLE_2 + 600)
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    until = CYCLE_2 + recovery_base.power_cycle_interval_sec(2)
    assert _cycles(cool) == 0 and not LADDER & set(cool["order"])
    assert _deadline(cool) == until
    cd = _health_cooldown(cool, monkeypatch, CYCLE_2 + 600)
    assert cd["escalation_stage"] == "power-cycle"
    assert cd["next_deadline"] == datetime.fromtimestamp(until).isoformat(timespec="seconds")
    # Past the bound and 10 min past the anchor (the missed request): the first tick is the bound's run.
    mark = CYCLE_2 + 600
    assert _bound_watchdog(cool, monkeypatch, mark) == ["ceiling_window"], "one run: the I76 bound's"
    assert _bound_watchdog(cool, monkeypatch, mark + 10) == [], "once per bound window"
    await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda m: None, bound=True)
    assert cool["order"][-2:] == ["denied", "sweep"] and _cycles(cool) == 0, "the full ladder, cycle still gated"
    assert _watchdog(cool, monkeypatch, until - 1) == []
    assert _watchdog(cool, monkeypatch, until) == [until]
    cool["clk"][0] = until
    assert await cool["g"].escalate("offbus-forced", [], EXPECTED, lambda m: None) == galaxy.OUTCOME_TERMINAL
    assert _cycles(cool) == 1


@pytest.mark.asyncio
async def test_an_external_reboot_counts_toward_the_cooldown_and_the_cycle_follows_it(cool, monkeypatch):  # noqa: F811
    """A cycle the broker did not fire (manual, BMC or datacenter) brought a new boot with the tray
    still off: the cooldown runs from that boot, not from the broker's request, and fires at its end."""
    external = RESTART - 600
    _replay(cool, monkeypatch, now=RESTART, boot_btime=external, boot_id="boot-C")
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    until = external + recovery_base.power_cycle_interval_sec(2)
    assert _cycles(cool) == 0 and _deadline(cool) == until
    assert cool["g"].mechanism.power_cycle_cooldown_state["cause"].startswith("host boot")
    cool["clk"][0] = until
    await _gate(cool, galaxy.STAGE_SMI_RESET, phase="startup")
    assert _cycles(cool) == 1 and not LADDER & set(cool["order"])


# ---- the off-bus hold's own path (spec 04 I27) ----------------------------------------------------


@pytest.fixture
def gated(cool, monkeypatch, tmp_path):  # noqa: F811
    """The idle off-bus hold's bounded end with a whole tray down, the real governor and its ledger:
    the command records a cycle instead of running."""
    hold_since = srv.device_hold_episode_since
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/usr/local/sbin/host-power-cycle --now")
    check = tmp_path / "manual-check"
    check.write_text("#!/bin/sh\nexit 1\n")
    check.chmod(0o755)
    monkeypatch.setenv("TT_DEVICE_MCP_MANUAL_RECOVERY_CMD", str(check))
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", raising=False)
    monkeypatch.setattr(opc, "fire_command", lambda argv, reason, timeout=0: cool["order"].append("power_cycle"))
    monkeypatch.setattr(galaxy, "run_pre_power_cycle_hook", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(galaxy.pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    opc._pending.clear()
    opc.note_whole_tray_down(hold_since, "tray 2 (chips 8-15) off")
    yield hold_since
    opc._pending.clear()


async def _gated_pass(cool, hold_since):  # noqa: F811
    return await cool["g"]._offbus_gated_power_cycle(8, EXPECTED, hold_since, lambda m: None)


@pytest.mark.asyncio
async def test_the_off_bus_path_no_longer_holds_six_hours_on_an_attempt_that_never_went_down(
    cool, monkeypatch, gated  # noqa: F811
):  # noqa: F811
    """5 h after the second request the off-bus path used to page "limit one per 21600s" and stand,
    with no deadline. The I65 cooldown (1 h after two cycles that healed nothing) is the wait now."""
    _replay(cool, monkeypatch, now=RESTART)
    assert await _gated_pass(cool, gated) is True
    assert _cycles(cool) == 1


@pytest.mark.asyncio
async def test_the_off_bus_path_inside_the_cooldown_holds_with_a_deadline_and_no_ladder(
    cool, monkeypatch, gated  # noqa: F811
):  # noqa: F811
    """Inside the cooldown the off-bus path holds (False: no fall-through to the reset ladder), with
    the deadline on /health and the watchdog retry set; at the deadline it fires."""
    _replay(cool, monkeypatch, now=CYCLE_2 + 600)
    assert await _gated_pass(cool, gated) is False
    until = CYCLE_2 + recovery_base.power_cycle_interval_sec(2)
    assert _cycles(cool) == 0 and _deadline(cool) == until
    assert cool["g"].mechanism.power_cycle_retry[0] == until
    assert _health_cooldown(cool, monkeypatch, CYCLE_2 + 600)["next_deadline"] == datetime.fromtimestamp(
        until
    ).isoformat(timespec="seconds")
    cool["clk"][0] = until
    assert await _gated_pass(cool, gated) is True
    assert _cycles(cool) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["600", "86400"])
async def test_an_operator_attempt_interval_is_a_stated_wait_capped_at_the_backoff_cap(
    cool, monkeypatch, gated, env  # noqa: F811
):  # noqa: F811
    """``TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC`` still spaces attempts when set, but as a
    stated wait on /health that the watchdog retries at its end, and never past the I65 cap."""
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", env)
    # The ledger cooldown has ended (a record from long ago); only the attempt file is recent.
    cool["boot"].update(id="boot-B", btime=T0 - 86400)
    _ledger({"action": "power-cycle", "boot_id": "boot-A", "at_epoch": T0 - 86400, "at": "x"})
    opc.save_state({"attempts": [{"at_epoch": T0 + 300, "episode": "earlier", "reason": "tray", "boot_id": "boot-B"}]})
    monkeypatch.setattr(opc, "now_epoch", lambda: cool["clk"][0])
    cool["clk"][0] = T0 + 360
    _tray_off(cool)
    assert await _gated_pass(cool, gated) is False
    until = T0 + 300 + min(int(env), recovery_base.POWER_CYCLE_BACKOFF_CAP_SEC)
    assert _deadline(cool) == until and cool["g"].mechanism.power_cycle_retry[0] == until
    assert _cycles(cool) == 0
    cool["clk"][0] = until
    assert await _gated_pass(cool, gated) is True
    assert _cycles(cool) == 1


def test_the_attempt_interval_default_adds_no_wait_beyond_the_cooldown(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", raising=False)
    assert opc.min_interval_sec() == 0
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", str(10**6))
    assert opc.min_interval_sec() == recovery_base.POWER_CYCLE_BACKOFF_CAP_SEC


@pytest.mark.asyncio
async def test_a_whole_tray_seen_again_among_partial_reads_still_cycles_at_the_deadline(
    cool, monkeypatch, gated  # noqa: F811
):  # noqa: F811
    """A flapping tray: forced passes alternate a whole-tray read (8 off) and a partial one (3 off)
    inside the cooldown. No read clears the episode's whole-tray clock, every pass holds with the
    same stated deadline, and the first pass at the deadline cycles, whichever read it sees. Here the
    second cycle took the host down (boot C), so it counts as a cycle (spec 04 I72)."""
    _replay(cool, monkeypatch, now=CYCLE_2 + 600, boot_btime=CYCLE_2 + 120, boot_id="boot-C")
    until = CYCLE_2 + 120 + recovery_base.power_cycle_interval_sec(2)
    for i, off in enumerate((8, 3, 8, 3, 3, 8)):
        cool["clk"][0] = CYCLE_2 + 600 + i * 300
        assert await cool["g"]._offbus_gated_power_cycle(off, EXPECTED, gated, lambda m: None) is False
        assert opc.whole_tray_down_age_sec(gated) is not None, "a partial read never clears the whole-tray clock"
        assert _deadline(cool) == until and _cycles(cool) == 0
    cool["clk"][0] = until
    assert await cool["g"]._offbus_gated_power_cycle(3, EXPECTED, gated, lambda m: None) is True
    assert _cycles(cool) == 1 and not LADDER & set(cool["order"])
