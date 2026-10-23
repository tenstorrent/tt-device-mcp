# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I96/I89: a below-floor hold on a chip gone from the bus (no PCIe node, absent from the
heartbeat reads) with TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET unset arms the off-bus one-shot. The chip
still gets no SBR (the opt-in is unchanged); the one-shot's mesh reset frees the device only through the
full post-reset check, fabric included (all cabled links). A whole tray gone still goes to the power
cycle with no reset first (I37)."""

import pytest

from tests import test_post_reset_gate as prg
from tests.test_classac_power_cycle_direct import _beats_without, _gate, _off, rig  # noqa: F401 - the fixture
from tests.test_fabric_link_short import LOST_DETAIL
from tests.test_offbus_one_shot_idle import IDLE, _below_floor_hold
from tests.test_offbus_one_shot_returned import tick
from tests.test_offbus_one_shot_unisolated import _real_mesh_reset
from tests.test_post_reset_gate import FOK, UNPROVEN, _assert_settled
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import BELOW_FLOOR, galaxy

mesh = prg.mesh  # the post-reset gate's fixtures, shared

GONE = "8"
LINK_SHORT = {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": LOST_DETAIL}}


def _gone_chip(monkeypatch):
    """Chip 8 has no PCIe node and is in no heartbeat read; nothing is isolated, the opt-in is unset."""
    node = pci.SYSFS_CLASS_DIR / f"tenstorrent!{GONE}"
    if node.exists():
        node.rmdir()
    beats = {str(i): 100 + i for i in range(32) if str(i) != GONE}
    monkeypatch.setattr(srv, "read_heartbeats", lambda *a, **k: dict(beats))
    monkeypatch.setattr(srv, "GONE_CHIP_CONFIRM_SETTLE_SEC", 0)
    monkeypatch.delenv("TT_DEVICE_MCP_GONE_CHIP_BRIDGE_RESET", raising=False)
    srv.isolated_chips = set()
    srv.device_pci_map = {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence, healthy, released",
    [
        (FOK, True, True),
        (UNPROVEN["fabric-77"], True, False),
        (UNPROVEN["no-fabric-verdict"], True, False),
        (LINK_SHORT, False, False),
    ],
    ids=["fabric-ok", "fabric-77", "no-fabric-verdict", "fabric-link-short"],
)
async def test_a_gone_chip_arms_the_one_shot_without_an_sbr(mesh, monkeypatch, evidence, healthy, released):
    """The gate holds the gone chip below the floor and arms the one-shot; the next sampler tick runs its
    mesh reset. No bridge reset or gone-chip SBR runs without the opt-in. Only a full check that passes
    with the fabric on every cabled link frees the device; anything less keeps the hold."""
    _, bridges, count = _below_floor_hold(monkeypatch, mesh, {"now": IDLE})
    monkeypatch.setattr(srv, "_offbus_hold_chips", set(), raising=False)
    _gone_chip(monkeypatch)

    async def unhealthy(expected, log, run_fabric=True, **_):
        return False, {"snapshot": {"ok": False, "detail": f"chip {GONE} off the bus"}}

    prg.patch_recovery(monkeypatch, "_verify_device", unhealthy)
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)
    assert srv._offbus_hold_chips == {GONE}, "the gone chip did not arm the one-shot"
    assert srv.isolated_chips == set() and bridges == [], "a gone chip was queued or SBR'd without the opt-in"
    calls = []

    async def verify(expected, log, run_fabric=True, **_):
        calls.append(run_fabric)
        return healthy, dict(evidence)

    prg.patch_recovery(monkeypatch, "_verify_device", verify)
    resets = _real_mesh_reset(monkeypatch)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert count("hold_deadline_forced_escalation") == 1 and srv.device_hold_escalation_kind == "offbus_one_shot"
    assert resets == ["mesh reset"], "the one-shot did not run the below-floor mesh reset"
    assert calls and all(calls), "the one-shot's post-reset verify skipped the fabric pass"
    assert bridges == [], "a gone chip was SBR'd without the opt-in"
    if released:
        _assert_settled(released)
    else:
        assert srv.fsm.state is not ServerState.HEALTHY, "released without a full check that passed on every link"
        assert healthy is False or FABRIC_DEBT.owed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["gate/post-job", "offbus-forced"])
async def test_a_whole_gone_tray_goes_to_the_power_cycle_with_no_reset(rig, phase):  # noqa: F811
    """A whole tray gone: the gate's below-floor stage and the one-shot's forced run both request the
    power cycle with no reset first (I37); the boot after it re-proves the mesh (03 I4)."""
    gone = _off(rig["map"], {3: 8})
    rig["beats"] = _beats_without(gone)
    if phase.startswith("gate/"):
        await _gate(rig, BELOW_FLOOR)
    else:
        await rig["g"].escalate(phase, [], 32, lambda m: None)
    assert rig["order"] == ["rescan", "capture", "power_cycle"], "a reset ran before the whole-tray power cycle"
    assert "released" not in rig["order"]
    assert galaxy.FAULT_CLASS_WHOLE_TRAY in [f.get("fault_class") for k, f in rig["events"] if k == "power_cycle_direct"]
