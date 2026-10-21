"""Spec 04 I23: there is no per-tray BMC re-power anywhere in the broker.

The rung (``ipmitool raw 0x30 0x8b`` per tray) was deleted outright: its pulse dropped the other
trays on the same box, and a sweep's tray step after a hung job once took a host storage array
down with it. Nothing replaces it — no flag, env var, config key or CLI option — so these tests
check both the source tree and every sweep a recovery path can reach (the TRAY_DOWN_NO_WINDOW
branch, the last-chance sweep before any host rung, and the class A 10-min sweep).
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import re
import subprocess

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import galaxy, pcie_guard

ROOT = pathlib.Path(__file__).resolve().parents[1]
TRAY_POWER = re.compile(r"0x30.{0,6}0x8b", re.IGNORECASE)
GONE_NAMES = (
    "AUTO_UBB_RESET",
    "UBB_SINGLE_TRAY_WALK",
    "TRAY_WALK_FAILED_HANDOFF",
    "IPMI_HANDOFF",
    "_fire_ubb_reset",
    "_attempt_ubb_tray_reset",
    "disable_tray_rung",
)


def _shipped_files():
    for top in ("src", "deploy"):
        for path in sorted((ROOT / top).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                yield path


def test_no_shipped_file_builds_a_tray_power_command():
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _shipped_files()
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1)
        if TRAY_POWER.search(line)
    ]
    assert hits == [], f"a per-tray BMC power command is back: {hits}"


def test_the_tray_rung_module_is_gone():
    assert importlib.util.find_spec("tt_device_mcp.health.recovery.stages.ubb_tray") is None
    assert not list((ROOT / "src").rglob("ubb_tray*.py"))


@pytest.mark.parametrize("name", GONE_NAMES)
def test_no_switch_or_hook_for_the_tray_rung_is_left(name):
    hits = [str(p.relative_to(ROOT)) for p in _shipped_files() if name in p.read_text(errors="replace")]
    assert hits == [], f"{name} still appears in {hits}"


@pytest.fixture
def sweep_rig(monkeypatch):
    """Stub every rung the sweeps can fire and record them, the journal and any spawned argv.

    A stale ``TT_DEVICE_MCP_AUTO_UBB_RESET=1`` left in a unit is set on purpose: it must change
    nothing."""
    g = srv.galaxy_recovery
    rig = {"order": [], "events": [], "argv": [], "verifies": []}
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(galaxy, "_settle_before_host_rung_sec", lambda: 0)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    patch_health_event(monkeypatch, lambda kind, **f: rig["events"].append(kind))

    def spy(real):
        def wrapped(argv, *a, **k):
            rig["argv"].append([str(x) for x in (argv if isinstance(argv, (list, tuple)) else [argv])])
            return real(argv, *a, **k)

        return wrapped

    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, spy(getattr(subprocess, name)))
    real_exec = asyncio.create_subprocess_exec

    async def exec_spy(*argv, **k):
        rig["argv"].append([str(x) for x in argv])
        return await real_exec(*argv, **k)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_spy)

    async def sbr(log):
        rig["order"].append("sbr")
        return False

    async def mesh(argv, log, *a, **k):
        rig["order"].append("mesh")
        return 0, ""

    async def verify(expected, log, run_fabric=True, **k):
        rig["verifies"].append(run_fabric)
        return False, {}

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", mesh)
    return rig


def _assert_no_tray_step(rig):
    assert rig["order"] and rig["order"][-1] == "mesh", rig["order"]
    assert set(rig["order"]) <= {"sbr", "mesh"}, f"a rung other than SBR or the mesh reset fired: {rig['order']}"
    assert not [k for k in rig["events"] if k.startswith(("ubb_reset", "ubb_tray_rung"))], rig["events"]
    assert not [a for a in rig["argv"] if any("0x8b" in x.lower() for x in a)], rig["argv"]
    assert rig["verifies"] == [True], "one full check, fabric included, after the sweep"


@pytest.mark.asyncio
async def test_the_last_chance_sweep_before_a_host_rung_has_no_tray_step(sweep_rig):
    """The idle and stuck-hold escalations, the 10-min ladder and the startup climb all reach the
    host rungs through this one sweep."""
    healthy = await srv.galaxy_recovery._settle_and_verify_before_host_rung(
        32, lambda m: None, "idle", offbus_chips={"30"}, context="idle escalation"
    )
    assert healthy is False, "the full sweep ran and the check failed: the caller climbs"
    assert "last_chance_reset_sweep" in sweep_rig["events"]
    _assert_no_tray_step(sweep_rig)


@pytest.mark.asyncio
async def test_the_tray_down_no_window_sweep_has_no_tray_step(sweep_rig, monkeypatch):
    cycles = []

    async def cycle(*a, **k):
        cycles.append(a)
        return True

    monkeypatch.setattr(srv.galaxy_recovery, "_offbus_gated_power_cycle", cycle)
    await srv.galaxy_recovery._fire_tray_down_no_window(
        {str(i) for i in range(24, 32)}, 32, lambda m: None, gate_phase="post-job", ev=None
    )
    _assert_no_tray_step(sweep_rig)


@pytest.mark.asyncio
async def test_the_class_a_ten_minute_sweep_has_no_tray_step(sweep_rig, monkeypatch):
    monkeypatch.setattr(srv.galaxy_recovery.mechanism, "class_a_ladder_due", lambda *a, **k: True)
    out = await srv.galaxy_recovery._class_a_cooldown_sweep({str(i) for i in range(24, 32)}, 32, lambda m: None)
    assert out == galaxy.OUTCOME_TERMINAL, "the check failed: the hold stays, the cycle follows the cooldown"
    _assert_no_tray_step(sweep_rig)
