# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A rung this process cannot execute reads OFF, and says which privilege it wanted (spec 04 I17).

The opt-out env vars still mean what they meant; this is the second conjunct beside them. What it
buys is that the refusal lands at boot, in the rung inventory, instead of at the top of the ladder
during a wedge — where a host rung that raises looks like a ladder that terminated.
"""

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp import privileges
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.stages import bridge_reset


def _latch(monkeypatch, **overrides):
    """Stage a host by latching its probe record — the one place both the capability accessors
    and the boot line read."""
    record = dict.fromkeys(privileges._PROBES, False)
    record.update(overrides)
    monkeypatch.setattr(privileges, "_LATCHED", record)
    return record


@pytest.fixture(autouse=True)
def unprivileged(monkeypatch):
    """A non-root daemon with nothing installed — the container shape."""
    _latch(monkeypatch)


@pytest.fixture
def privileged(monkeypatch):
    """Root on a systemd host with a reachable BMC — the shared-broker shape.

    The opt-outs come off too: conftest seals them to 0 suite-wide, which would make every
    assertion below pass on the env var alone and prove nothing about privilege."""
    _latch(monkeypatch, root=True, systemd=True, setpci_bin=True, ipmitool=True, ipmi_node=True)
    for var in ("TT_DEVICE_MCP_AUTO_REBOOT", "TT_DEVICE_MCP_AUTO_POWER_CYCLE", "TT_DEVICE_MCP_AUTO_UBB_RESET"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def said(monkeypatch):
    """Capture the broker's own logger; it does not propagate to caplog."""
    lines = []

    class _Logger:
        def info(self, msg):
            lines.append(("info", msg))

        def warning(self, msg):
            lines.append(("warning", msg))

        def error(self, msg):
            lines.append(("error", msg))

    monkeypatch.setattr(srv, "logger", _Logger())
    return lines


def test_a_non_root_daemon_has_no_reboot_rung(monkeypatch, privileged):
    """`systemctl reboot` needs root. Armed-by-default is about the operator's intent, not about
    whether the call would be permitted."""
    assert srv._auto_reboot_enabled() is True
    _latch(monkeypatch, systemd=True, setpci_bin=True, ipmitool=True, ipmi_node=True)
    assert srv._auto_reboot_enabled() is False


def test_a_host_without_systemd_has_no_reboot_rung(monkeypatch, privileged):
    """The reboot is issued through systemctl, so a container with root but no init cannot fire
    it either."""
    _latch(monkeypatch, root=True, setpci_bin=True, ipmitool=True, ipmi_node=True)
    assert srv._auto_reboot_enabled() is False


def test_an_unreachable_bmc_has_no_power_cycle_rung(monkeypatch, privileged):
    """Already true for a missing binary; now also for a binary with no node to talk to."""
    assert srv._auto_power_cycle_enabled() is True
    _latch(monkeypatch, root=True, systemd=True, setpci_bin=True, ipmitool=True)
    assert srv._auto_power_cycle_enabled() is False


def test_an_unreachable_bmc_has_no_tray_rung(monkeypatch, privileged):
    """The per-tray re-power goes out over the same `ipmitool raw` as the cold rung."""
    assert galaxy._ubb_reset_enabled() is True
    _latch(monkeypatch, root=True, systemd=True, setpci_bin=True, ipmitool=True)
    assert galaxy._ubb_reset_enabled() is False


def _commit_platform(monkeypatch, which):
    """Stage what resolve_platform committed at boot: "per-target", "galaxy", or None (unresolved)."""
    monkeypatch.setattr(srv.fsm, "galaxy", srv.galaxy_recovery)
    monkeypatch.setattr(srv.fsm, "per_target", srv.per_target_recovery)
    committed = {"per-target": srv.per_target_recovery, "galaxy": srv.galaxy_recovery, None: None}[which]
    monkeypatch.setattr(srv.fsm, "recovery", committed)


def _inventory(said):
    line = next(m for lvl, m in said if lvl == "info" and m.startswith("RUNG INVENTORY"))
    return dict(item.split("=") for item in line.removeprefix("RUNG INVENTORY: ").split(", "))


def test_a_per_target_host_has_no_tray_rung(monkeypatch, privileged, said):
    """A per-target host has no UBB trays, so full privilege still leaves nothing to re-power. The
    inventory must say so, and must name the platform rather than a privilege the host has."""
    _commit_platform(monkeypatch, "per-target")

    srv.log_rung_inventory()

    assert _inventory(said)["auto-tray-reset"] == "OFF"
    assert any("RUNG OFF auto-tray-reset" in m and "per-target" in m for lvl, m in said if lvl == "warning")


def test_an_unresolved_platform_keeps_the_tray_rung(monkeypatch, privileged, said):
    """A degraded Galaxy is exactly the host whose board reads fail and whose platform stays
    unresolved, and a below-floor tray-down is what this rung recovers. Unresolved is not
    per-target."""
    _commit_platform(monkeypatch, None)

    srv.log_rung_inventory()

    assert _inventory(said)["auto-tray-reset"] == "on"


def test_a_galaxy_keeps_the_tray_rung(monkeypatch, privileged, said):
    _commit_platform(monkeypatch, "galaxy")

    srv.log_rung_inventory()

    assert _inventory(said)["auto-tray-reset"] == "on"


def test_a_non_root_daemon_has_no_bridge_rung(monkeypatch, privileged):
    """The gentlest rung, and the one that had no arming predicate at all: unprivileged, its first
    setpci read fails with a permission error the ladder then RETRIES to its attempt budget."""
    assert bridge_reset.bridge_reset_enabled() is True
    _latch(monkeypatch, root=True, systemd=True, ipmitool=True, ipmi_node=True)
    assert bridge_reset.bridge_reset_enabled() is False


def test_the_opt_out_still_wins_where_the_privilege_exists(monkeypatch, privileged):
    """Privilege is a second conjunct, never a replacement: an operator who turned a rung off on a
    fully privileged host still has it off."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "0")

    assert srv._auto_reboot_enabled() is False
    assert srv._auto_power_cycle_enabled() is False
    assert galaxy._ubb_reset_enabled() is False


def test_the_device_scoped_resets_survive_without_systemd(monkeypatch):
    """`tt-smi -r` and `-glx_reset` are ioctls on /dev/tenstorrent, which the submitter already
    holds, so a container has a working rung 1. Lacking a PID-1 scope picks the other backend
    (spec 04 I4) rather than cancelling the reset; the argv is asserted in test_reset.py."""
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)

    assert srv.recovery_mechanism._scoped_reset_backend() is False
    assert not hasattr(srv, "_local_reset_enabled"), "the backend follows systemd; no second predicate to drift"


def test_the_inventory_names_the_privilege_a_rung_lacked(monkeypatch, said):
    """A rung reported OFF is only actionable with its cause. An operator who never set the env
    var must not read their own opt-out back at them when the real reason is euid."""
    for var in ("TT_DEVICE_MCP_AUTO_REBOOT", "TT_DEVICE_MCP_AUTO_POWER_CYCLE"):
        monkeypatch.delenv(var, raising=False)

    srv.log_rung_inventory()

    warnings = [m for lvl, m in said if lvl == "warning"]
    assert any("RUNG OFF auto-reboot" in m and "root" in m for m in warnings)
    assert any("RUNG OFF auto-power-cycle" in m and "ipmitool is not on PATH" in m for m in warnings)
    assert not any("TT_DEVICE_MCP_AUTO_REBOOT=0" in m for m in warnings), "the opt-out was never set"


def test_boot_states_the_privileges_it_measured(said):
    """One line, all four probes, so the rung inventory below it can be read against them."""
    srv.log_rung_inventory()

    assert any(lvl == "info" and "BOOT privilege:" in m for lvl, m in said)


def test_the_bridge_rung_names_which_half_it_lacks(monkeypatch, privileged, said):
    """Root-with-no-setpci and setpci-with-no-root are different problems with different fixes,
    and a single hardcoded cause would send half of them to the wrong one."""
    _latch(monkeypatch, root=True, systemd=True, ipmitool=True, ipmi_node=True)

    _latch(monkeypatch, systemd=True, setpci_bin=True, ipmitool=True, ipmi_node=True)
    srv.log_rung_inventory()
    assert any("RUNG OFF bridge-reset" in m and "not root" in m for lvl, m in said if lvl == "warning")

    said.clear()
    _latch(monkeypatch, root=True, systemd=True, ipmitool=True, ipmi_node=True)
    srv.log_rung_inventory()
    assert any("RUNG OFF bridge-reset" in m and "setpci is not on PATH" in m for lvl, m in said if lvl == "warning")


def test_the_forced_ladder_cannot_fire_a_rung_privilege_denies(monkeypatch):
    """The hold-deadline watchdog's forced escalation bypasses the fail-closed DEFERS, not the
    arming. An unprivileged daemon whose hold outlives the ceiling must still not reach for a
    reboot it cannot issue — otherwise the bypass would route to a rung that raises."""
    from tt_device_mcp.health.recovery.galaxy import _choose_recovery_escalation

    chosen = _choose_recovery_escalation(
        auto_reboot_enabled=srv._auto_reboot_enabled,
        auto_power_cycle_enabled=srv._auto_power_cycle_enabled,
        reboot_already_attempted=lambda: False,
    )

    assert chosen is None, "no host rung is reachable unprivileged, forced or not"


@pytest.mark.asyncio
async def test_a_per_user_daemon_still_prints_its_rung_inventory(monkeypatch, said):
    """The startup sequence returns early for a non-privsep daemon — no re-adoption, no startup
    gate. The inventory is not part of that sequence: it states what this process can execute,
    and the unprivileged shape is the one most likely to be missing rungs."""
    monkeypatch.setattr(srv, "should_privsep", lambda: False)
    monkeypatch.setattr(srv, "_startup_tasks_done", False)

    await srv.run_startup_tasks()

    assert any(lvl == "info" and "BOOT privilege:" in m for lvl, m in said)
    assert any(lvl == "info" and "RUNG INVENTORY" in m for lvl, m in said)


def test_an_unprivileged_daemon_still_sees_a_root_started_reset_scope(monkeypatch):
    """Listing systemd units needs no privilege, and seeing a scope is a different question from
    being able to start one. A per-user daemon beside a root broker must still adopt the root
    broker's reset (spec 04 I5) rather than fire its own into a mesh already cycling."""
    _latch(monkeypatch, systemd=True)  # systemd present, not root
    seen = []

    class _Done:
        returncode = 0
        stdout = "ttdev-reset-4242-1.scope loaded active running\n"

    monkeypatch.setattr(srv.recovery_mechanism, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(
        "tt_device_mcp.health.recovery.base.subprocess.run",
        lambda argv, **kw: (seen.append(argv) or _Done()),
    )

    # Through the class: conftest pins the instance attribute off so no test reaches the real
    # host, and this one is about the method itself.
    found = recovery_base.RecoveryMechanism.scope_active(srv.recovery_mechanism)

    assert found == "ttdev-reset-4242-1.scope"
    assert seen, "the unit list was never queried"


@pytest.mark.asyncio
async def test_without_setpci_the_rescan_still_runs_and_no_chip_is_called_no_bridge(monkeypatch, clear_job_state):
    """The SBR needs root and setpci; the bare PCI rescan below it needs neither of those from
    this rung's point of view, and is the cheapest recovery there is. Gating the whole function
    would lose it. And a chip nobody wrote to must NOT be recorded `no_bridge` — that reason means
    the endpoint left the bus, which `_classify_hold` escalates straight to a power cycle."""
    _latch(monkeypatch, root=True, systemd=True, ipmitool=True, ipmi_node=True)  # no setpci
    rescanned = []
    monkeypatch.setattr(recovery_base.Path, "write_text", lambda self, text: rescanned.append(str(self)), raising=False)
    mech = srv.galaxy_recovery
    monkeypatch.setattr(mech.deps, "isolated_chips", lambda: {"0", "1"})
    monkeypatch.setattr(mech.deps, "device_pci_map", lambda: {"0": "0000:31:00.0", "1": "0000:4b:00.0"})

    fired = []
    monkeypatch.setattr(srv.metrics, "stage_fired", lambda stage, outcome: fired.append((stage, outcome)))

    await mech._recover_isolated_chips(lambda m: None)

    reasons = mech.last_bridge_reset_reasons
    assert set(reasons) == {"0", "1"}, "every target needs a recorded window"
    assert all(r["reason"] == "no_privilege" for r in reasons.values()), reasons
    assert ("bridge_reset", "blocked") in fired, "operator-fixable, so not not_applicable"
    assert any("rescan" in path for path in rescanned), f"the cheap rung was skipped: {rescanned}"
