# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Spec 04 I71/I87: the host reboot rung, the tray step and a repeated mesh reset are gone for good.

No env var, config key or CLI option brings any of them back: a stale switch left in a unit changes
nothing. The last-chance sweep runs at most once per hold phase (below the 10-min bound, at it).
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_no_tray_repower import sweep_rig  # noqa: F401
from tests.test_power_cycle_host_rung import _arm_partial_drop, _deny
from tt_device_mcp import constants
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import STAGE_BRIDGE_RESET, STAGE_POWER_CYCLE, STAGE_SMI_RESET
from tt_device_mcp.health.recovery import galaxy as galaxy_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]
GONE_NAMES = (
    "TT_DEVICE_MCP_AUTO_REBOOT",
    "TT_DEVICE_MCP_POST_REBOOT_VERIFY",
    "TT_DEVICE_MCP_PROGRESS_RETRY_SEC",
    "STAGE_HOST_REBOOT",
)
REBOOT_ARGV = re.compile(r"""["']systemctl["']\s*,\s*["']reboot["']|/sbin/reboot|shutdown\s+-r""")
STALE_SWITCHES = {
    "TT_DEVICE_MCP_AUTO_REBOOT": "1",
    "TT_DEVICE_MCP_POST_REBOOT_VERIFY": "1",
    "TT_DEVICE_MCP_AUTO_UBB_RESET": "1",
    "TT_DEVICE_MCP_PROGRESS_RETRY_SEC": "1",
    "TTDEV_AUTO_REBOOT": "1",
}


def _src_files():
    return [p for p in sorted((ROOT / "src").rglob("*.py")) if "__pycache__" not in p.parts]


@pytest.mark.parametrize("name", GONE_NAMES)
def test_no_switch_for_a_removed_rung_is_read(name):
    hits = [str(p.relative_to(ROOT)) for p in _src_files() if name in p.read_text(errors="replace")]
    assert hits == [], f"{name} still appears in {hits}"


def test_the_reboot_rung_is_gone_and_the_power_cycle_is_the_only_host_rung():
    assert importlib.util.find_spec("tt_device_mcp.health.recovery.stages.host_reboot") is None
    assert constants.STAGE_NAMES == (STAGE_BRIDGE_RESET, STAGE_SMI_RESET, STAGE_POWER_CYCLE)
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _src_files()
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1)
        if REBOOT_ARGV.search(line)
    ]
    assert hits == [], f"a host reboot command is back: {hits}"


@pytest.mark.parametrize("off_bus", [0, 8, 16, 32])
def test_stale_switches_never_make_the_chooser_name_a_reboot(monkeypatch, off_bus):
    for k, v in STALE_SWITCHES.items():
        monkeypatch.setenv(k, v)
    for armed in (True, False):
        action, hold = galaxy_mod._host_escalation_for_drop(off_bus, 32, auto_power_cycle_enabled=lambda a=armed: a)
        assert action in ("power-cycle", None) and hold is (action is None)


@pytest.mark.asyncio
async def test_stale_switches_bring_back_no_reboot_tray_step_or_second_mesh_reset(monkeypatch, clear_job_state):
    """A denied power cycle with every old switch set: one mesh reset in the episode, only the power
    cycle is ever asked for, and the hold stands."""
    for k, v in STALE_SWITCHES.items():
        monkeypatch.setenv(k, v)
    counters = _arm_partial_drop(monkeypatch, 8)
    stub = srv.galaxy_recovery._reset_and_verify_device

    async def failing_reset(*a, **k):
        ok = await stub(*a, **k)
        if not ok:
            srv.recovery_mechanism.mark_mesh_reset_failed()  # what the real reset records
        return ok

    patch_recovery(monkeypatch, "_reset_and_verify_device", failing_reset)
    asked = _deny(monkeypatch, "power-cycle")
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append(kind))
    try:
        await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None)
        await srv.galaxy_recovery._escalate_offbus_stuck_hold(["0", "1"], 32, lambda m: None, force=True)
    finally:
        srv.recovery_mechanism.mesh_reset_failed_since_release = False
        srv.recovery_mechanism.mesh_reset_failed_episode = None
    assert counters["resets"] == 1, "one mesh reset per hold episode (I87)"
    assert asked and set(asked) == {"power-cycle"}, asked
    assert counters["power_cycles"] == 0
    assert counters["fault_cleared"] == 0 and counters["dirty_cleared"] == 0
    assert not [k for k in events if "reboot" in k or k.startswith(("ubb_reset", "ubb_tray"))], events


@pytest.mark.asyncio
async def test_the_last_chance_sweep_runs_at_most_once_per_phase(sweep_rig, monkeypatch):  # noqa: F811
    g = srv.galaxy_recovery
    since = (datetime.now() - timedelta(seconds=60)).isoformat()
    monkeypatch.setattr(srv, "device_hold_episode_since", since)
    monkeypatch.setattr(g.mechanism, "sweep_spent", None)

    async def sweep():
        return await g._settle_and_verify_before_host_rung(
            32, lambda m: None, "idle", offbus_chips={"30"}, context="idle escalation"
        )

    assert await sweep() is False
    assert await sweep() is False, "a repeat below the bound climbs without a second sweep"
    assert sweep_rig["order"].count("mesh") == 1
    assert "last_chance_sweep_skipped" in sweep_rig["events"]

    monkeypatch.setattr(galaxy_mod, "_stuck_hold_ceiling_sec", lambda: 30)
    assert await sweep() is False
    assert sweep_rig["order"].count("mesh") == 2, "the 10-min bound is a new phase: its ladder sweeps once"
    assert await sweep() is False
    assert sweep_rig["order"].count("mesh") == 2
