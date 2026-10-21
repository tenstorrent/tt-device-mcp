# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I28: an operator's outage hold holds every automatic mesh reset (``tt-smi -glx_reset``)."""

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import alert
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base, galaxy, pcie_guard

HOLD = {
    "TT_DEVICE_MCP_AUTO_REBOOT": "0",
    "TT_DEVICE_MCP_AUTO_POWER_CYCLE": "0",
}


@pytest.fixture
def held(monkeypatch):
    """The outage-hold drop-in: every hardware rung off, the mesh-reset knob unset."""
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    for var, value in HOLD.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setattr(base, "_mesh_hold_paged", {})


@pytest.fixture
def rec(monkeypatch):
    """A Galaxy gate pass with no tenant, no foreign scope and an open host gate. Records the
    journal, the pages and every mesh reset that fires."""
    out = {"events": [], "alerts": [], "resets": []}
    g = srv.galaxy_recovery

    async def no_scope(log):
        return False

    async def reset(argv, log, *a, **k):
        out["resets"].append(argv)
        return 1, "reset failed"

    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    patch_health_event(monkeypatch, lambda name, **k: out["events"].append(name))
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: out["alerts"].append(event) or object())
    monkeypatch.setattr(g.mechanism, "await_foreign_scope", no_scope)
    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    monkeypatch.setattr(g.mechanism, "last_reset_monotonic", 123.0)
    monkeypatch.setattr(g.mechanism, "last_reset_failed", False)
    monkeypatch.setattr(g.monitor, "expected", lambda n: 4)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 0)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)
    return out


def test_the_hold_reason_follows_the_flags(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    for var in HOLD:
        monkeypatch.delenv(var, raising=False)
    assert base.mesh_reset_hold_reason() == "", "the shipped default resets"
    for i, var in enumerate(HOLD):
        assert base.mesh_reset_hold_reason() == "", f"{i} of the hardware rungs off is a per-host choice"
        monkeypatch.setenv(var, "0")
    assert "outage hold" in base.mesh_reset_hold_reason()
    for var in HOLD:
        monkeypatch.setenv(var, "1")
        assert base.mesh_reset_hold_reason() == "", f"{var} armed again is no outage hold"
        monkeypatch.setenv(var, "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "1")
    assert base.mesh_reset_hold_reason() == "", "=1 re-arms the mesh reset under a hold"


def test_a_stale_tray_rung_setting_is_ignored(monkeypatch):
    """The BMC tray re-power rung is gone, and so is its switch: an old host config that still sets it
    neither holds the mesh reset nor counts toward the outage hold."""
    for var in HOLD:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")
    assert base.mesh_reset_hold_reason() == ""
    assert "TT_DEVICE_MCP_AUTO_UBB_RESET" not in base.OUTAGE_HOLD_VARS
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    assert base.mesh_reset_hold_reason() == "", "one real rung off is still a per-host choice"
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "0")
    for var in HOLD:
        monkeypatch.delenv(var, raising=False)
    assert base.mesh_reset_hold_reason() == "TT_DEVICE_MCP_AUTO_MESH_RESET=0"


@pytest.mark.asyncio
async def test_the_gate_holds_the_mesh_reset_under_an_outage_hold_and_pages_once(held, rec):
    """The observed case: an outage-hold drop-in turned every hardware rung off and the gate still fired
    `tt-smi -glx_reset`. Held now: nothing fires, nothing is recorded as a reset, one page."""
    g = srv.galaxy_recovery
    logs = []
    for _ in range(2):
        assert await g._reset_and_verify_device(["0", "1", "2", "3"], logs.append) is False
    assert rec["resets"] == []
    assert rec["events"].count("mesh_reset_held") == 2 and "reset_begin" not in rec["events"]
    assert len(rec["alerts"]) == 1 and rec["alerts"][0]["kind"] == "mesh_reset_held"
    assert "outage hold" in rec["alerts"][0]["reason"]
    assert any("automatic mesh reset NOT fired" in m for m in logs)
    assert g.mechanism.last_reset_monotonic == 123.0, "a held reset arms no cooldown"
    assert g.mechanism.last_reset_failed is False, "nor a failed-reset flag"


@pytest.mark.asyncio
async def test_the_gate_holds_with_the_explicit_knob_alone(monkeypatch, rec):
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "0")
    monkeypatch.setattr(base, "_mesh_hold_paged", {})
    assert await srv.galaxy_recovery._reset_and_verify_device(["0", "1", "2", "3"], lambda m: None) is False
    assert rec["resets"] == [] and rec["alerts"][0]["reason"] == "TT_DEVICE_MCP_AUTO_MESH_RESET=0"


@pytest.mark.asyncio
@pytest.mark.parametrize("off", list(HOLD))
async def test_the_gate_still_resets_with_one_host_rung_off(monkeypatch, rec, off):
    """One host rung off on its own is a per-host choice: the ladder still climbs through the mesh reset."""
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    for var in HOLD:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(off, "0")
    monkeypatch.setattr(base, "_mesh_hold_paged", {})
    await srv.galaxy_recovery._reset_and_verify_device(["0", "1", "2", "3"], lambda m: None)
    assert rec["resets"] == [["tt-smi", "-glx_reset"]] and "mesh_reset_held" not in rec["events"]


class _Said:
    def __init__(self):
        self.said = []

    def info(self, m):
        self.said.append(("info", m))

    def warning(self, m):
        self.said.append(("warning", m))

    def error(self, m):
        self.said.append(("error", m))


def test_an_outage_hold_logs_the_held_mesh_reset_at_boot(monkeypatch):
    """Both host rungs at 0 with the mesh-reset knob unset hold every automatic mesh reset: the boot
    inventory says so by name and says how to re-arm it. One rung off, or the knob set either way,
    logs no such warning (``=0`` is the operator's explicit choice, ``=1`` re-arms it)."""
    log = _Said()
    monkeypatch.setattr(srv, "logger", log)
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    for var in HOLD:
        monkeypatch.delenv(var, raising=False)

    def held_lines():
        log.said.clear()
        srv.log_rung_inventory()
        return [m for lvl, m in log.said if lvl == "warning" and m.startswith("MESH RESET HELD")]

    assert held_lines() == [], "the shipped default holds nothing"
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    assert held_lines() == [], "one host rung off still resets"
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    (line,) = held_lines()
    for name in ("TT_DEVICE_MCP_AUTO_REBOOT=0", "TT_DEVICE_MCP_AUTO_POWER_CYCLE=0", "glx_reset", "operator"):
        assert name in line, line
    assert "TT_DEVICE_MCP_AUTO_MESH_RESET=1" in line and "I28" in line
    assert base.outage_hold_boot_warning() in line
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "1")
    assert held_lines() == [], "=1 re-arms the mesh reset"
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "0")
    assert held_lines() == [], "an explicit =0 is the RUNG GAP warning's, not this one"


@pytest.mark.asyncio
async def test_an_explicit_opt_in_re_arms_the_mesh_reset_under_an_outage_hold(monkeypatch, held, rec):
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "1")
    await srv.galaxy_recovery._reset_and_verify_device(["0", "1", "2", "3"], lambda m: None)
    assert len(rec["resets"]) == 1 and rec["alerts"] == []


@pytest.mark.asyncio
async def test_the_last_chance_sweep_is_refused_under_an_outage_hold(monkeypatch, held, rec):
    """The sweep's last step is the mesh reset, so the whole sweep is refused and reports incomplete:
    no host rung climbs on it (I22)."""
    g = srv.galaxy_recovery

    async def no_sbr(log):
        pytest.fail("a held sweep ran its SBR step")

    monkeypatch.setattr(g, "_recover_isolated_chips", no_sbr)
    assert await g._issue_all_resets_back_to_back([4], 4, lambda m: None) is False
    assert rec["resets"] == [] and "mesh_reset_held" in rec["events"]


@pytest.mark.asyncio
async def test_a_dropped_page_is_sent_again_and_a_lifted_hold_re_arms_it(monkeypatch, held, rec):
    g = srv.galaxy_recovery
    results = iter([None, object(), object()])
    sent = []
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: sent.append(event) or next(results))
    monkeypatch.setattr(alert, "alert_argv", lambda: ["page"])
    await g._reset_and_verify_device(["0"], lambda m: None)  # dropped: hook busy
    await g._reset_and_verify_device(["0"], lambda m: None)  # sent again
    await g._reset_and_verify_device(["0"], lambda m: None)  # already paged
    assert len(sent) == 2
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "1")
    await g._reset_and_verify_device(["0"], lambda m: None)  # the hold lifted: a reset fires
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET")
    await g._reset_and_verify_device(["0"], lambda m: None)  # a new hold pages again
    assert len(sent) == 3 and len(rec["resets"]) == 1


@pytest.fixture
def one_off_bus(monkeypatch):
    """One chip off the PCIe bus, with the host reset gate at its default (off), so nothing but the
    outage hold stands between the gate and the mesh reset. Records the off-bus intent instead of
    writing it."""
    intents = []
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 1)
    monkeypatch.setattr(pcie_guard, "begin_offbus_reset", lambda kind, off_bus: intents.append((kind, off_bus)))
    return intents


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["startup", "pre-job"])
async def test_the_gate_holds_with_a_chip_off_the_bus_under_an_outage_hold(held, rec, one_off_bus, phase):
    """The observed case: with an outage hold in force and a chip off the bus, the startup and
    pre-job gates fired `tt-smi -glx_reset` again and again (each exited 1 after ~130 s) until the
    whole mesh was off the bus. Held now: no reset, no off-bus intent, and the page names the drop."""
    g = srv.galaxy_recovery
    logs = []
    outcome = await g._fire_gate_rung(
        phase, galaxy.STAGE_SMI_RESET, ["0", "1", "2", "3"], 4, logs.append, ev=None, beats={}
    )
    assert outcome == galaxy.OUTCOME_WAITING, "held, never terminal: the episode stands for an operator"
    assert rec["resets"] == [] and one_off_bus == []
    assert "reset_begin" not in rec["events"] and "mesh_reset_held" in rec["events"]
    assert len(rec["alerts"]) == 1 and rec["alerts"][0]["off_bus"] == 1
    assert any("1 chip(s) off the bus" in m and "HELD" in m for m in logs)
    assert g.mechanism.last_reset_monotonic == 123.0 and g.mechanism.last_reset_failed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["startup", "pre-job"])
async def test_without_the_hold_a_chip_off_the_bus_still_gets_the_mesh_reset(monkeypatch, rec, one_off_bus, phase):
    """No hold, the shipped default: the same gate pass resets exactly as before."""
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_MESH_RESET", raising=False)
    for var in HOLD:
        monkeypatch.delenv(var, raising=False)
    g = srv.galaxy_recovery
    await g._fire_gate_rung(phase, galaxy.STAGE_SMI_RESET, ["0", "1", "2", "3"], 4, lambda m: None, ev=None, beats={})
    assert rec["resets"] == [["tt-smi", "-glx_reset"]]
    assert one_off_bus == [("mesh reset", 1)] and "reset_begin" in rec["events"]
    assert "mesh_reset_held" not in rec["events"] and rec["alerts"] == []


@pytest.mark.asyncio
async def test_a_standing_hold_pages_again_after_the_repage_interval(monkeypatch, held, rec, one_off_bus):
    """Bounded: the hold never sits in silence. Within the interval one page; past it, another."""
    g = srv.galaxy_recovery
    await g._reset_and_verify_device(["0"], lambda m: None)
    await g._reset_and_verify_device(["0"], lambda m: None)
    assert len(rec["alerts"]) == 1
    why = base.mesh_reset_hold_reason()
    base._mesh_hold_paged[why] -= base.mesh_hold_repage_sec()
    await g._reset_and_verify_device(["0"], lambda m: None)
    assert len(rec["alerts"]) == 2 and rec["resets"] == []
    monkeypatch.setenv("TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC", "0")
    base._mesh_hold_paged[why] -= 10 * base.MESH_HOLD_REPAGE_DEFAULT_SEC
    await g._reset_and_verify_device(["0"], lambda m: None)
    assert len(rec["alerts"]) == 2, "0 pages once per hold"


def test_the_repage_knob_defaults_and_rejects_nonsense(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC", raising=False)
    assert base.mesh_hold_repage_sec() == 3600
    for bad in ("soon", "-5", ""):
        monkeypatch.setenv("TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC", bad)
        assert base.mesh_hold_repage_sec() == 3600
    monkeypatch.setenv("TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC", "600")
    assert base.mesh_hold_repage_sec() == 600
