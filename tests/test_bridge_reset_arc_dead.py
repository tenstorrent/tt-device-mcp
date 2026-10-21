# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I94: a chip the bridge rung brings back counts recovered only with a live ARC heartbeat.
A bridge reset can re-create a chip's sysfs node with its ARC dead (no telemetry). That chip is back on
the bus but not healed: it stays isolated, so the off-bus one-shot still fires on its short clock
instead of the hold waiting for the 10-min ladder."""

from datetime import timedelta

import pytest

from tests import test_post_reset_gate as prg
from tests.conftest import patch_health_event
from tests.test_offbus_one_shot_idle import IDLE, _below_floor_hold
from tests.test_post_reset_gate import _assert_settled, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import chip_state
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.monitors import heartbeat, pci
from tt_device_mcp.health.pci_writer import PciWedged
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery.galaxy import GalaxyRecovery

mesh = prg.mesh  # the post-reset gate's fixtures, shared
verdict = prg.verdict


def _bridge_rung(monkeypatch, idx, beats, attr=True):
    """The real bridge rung for isolated chip ``idx``: its stubbed bridge reset re-creates the chip's
    sysfs node; the node's ``tt_heartbeat`` reads ``beats`` one value per poll (None: ENODATA, the
    ARC is not up). ``attr=False``: a KMD with no ``tt_heartbeat`` attr. Returns the events the rung logs."""
    srv.isolated_chips = {idx}
    srv.device_pci_map = {idx: "0000:46:00.0"}
    monkeypatch.setattr(recovery_pkg, "arc_live_chips", heartbeat.arc_live_chips)
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_SETTLE_SEC", 0)
    monkeypatch.setattr(recovery_pkg, "BRIDGE_RESET_ARC_POLL_SEC", 0)
    monkeypatch.setattr(srv, "_set_device_op_detail", lambda d: None)
    node = pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}"
    reads = iter(beats)

    def bridge_reset(i, bdf):
        node.mkdir(exist_ok=True)
        if attr:
            (node / "tt_heartbeat").write_text("0")
        return True

    def read_beat(n):
        if not (n / "tt_heartbeat").exists():
            raise FileNotFoundError(2, "No such file or directory")
        value = next(reads, beats[-1])
        if value is None:
            raise OSError(61, "No data available")  # ENODATA: ARC not ready
        return value

    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", bridge_reset)
    monkeypatch.setattr(heartbeat, "_read_beat", read_beat)
    monkeypatch.setattr(recovery_pkg, "_arc_check_skip_logged", False, raising=False)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(recovery_pkg, "health_event", lambda kind, **f: events.append((kind, f)))
    return events


@pytest.mark.asyncio
async def test_a_chip_back_on_the_bus_with_a_dead_arc_stays_isolated(monkeypatch):
    events = _bridge_rung(monkeypatch, "13", [None])
    ok = await srv.galaxy_recovery._recover_isolated_chips(lambda m: None)
    assert ok is False, "a re-enumerated chip with no ARC heartbeat counted as recovered"
    assert srv.isolated_chips == {"13"}, "an ARC-dead chip left isolation, disarming the off-bus one-shot"
    kinds = dict(events)
    assert kinds["bridge_reset_arc_dead"]["chips"] == ["13"]
    assert "13" in kinds["chip_recovery"]["still_dead"] and "13" not in kinds["chip_recovery"]["recovered"]


@pytest.mark.asyncio
async def test_an_all_ones_heartbeat_is_not_a_live_arc(monkeypatch):
    _bridge_rung(monkeypatch, "13", [str(heartbeat.ALL_ONES)])
    assert await srv.galaxy_recovery._recover_isolated_chips(lambda m: None) is False
    assert srv.isolated_chips == {"13"}


@pytest.mark.asyncio
async def test_a_chip_back_with_a_live_heartbeat_is_recovered(monkeypatch):
    events = _bridge_rung(monkeypatch, "13", ["12345"])
    assert await srv.galaxy_recovery._recover_isolated_chips(lambda m: None) is True
    assert srv.isolated_chips == set()
    assert not [k for k, _ in events if k == "bridge_reset_arc_dead"]


@pytest.mark.asyncio
async def test_an_arc_that_comes_up_within_the_wait_is_recovered(monkeypatch):
    """The ARC boots a little after the node appears: the rung waits up to BRIDGE_RESET_ARC_WAIT_SEC."""
    _bridge_rung(monkeypatch, "13", [None, None, "777"])
    assert await srv.galaxy_recovery._recover_isolated_chips(lambda m: None) is True
    assert srv.isolated_chips == set()


@pytest.mark.asyncio
async def test_a_pci_wedged_pass_reads_no_heartbeat(monkeypatch):
    """Spec 04 I76/I94: once a bridge write latches PCI-wedged the pass has failed and requested the
    power cycle; the ARC check adds no read or wait after the latch."""
    _bridge_rung(monkeypatch, "13", ["12345"])
    srv.isolated_chips = {"13", "14"}
    srv.device_pci_map = {"13": "0000:46:00.0", "14": "0000:47:00.0"}
    reads = []
    monkeypatch.setattr(heartbeat, "_read_beat", lambda n: reads.append(n) or "12345")

    async def write(context, fn, *args, timeout=None):
        if args[0] == "14":
            raise PciWedged(f"{context}: timed out")
        return fn(*args)

    monkeypatch.setattr(recovery_pkg.PCI_WRITER, "write", write)
    assert await srv.galaxy_recovery._recover_isolated_chips(lambda m: None) is False
    assert reads == [], "the ARC check read a heartbeat after the PCI-wedged latch"
    assert "14" in srv.isolated_chips


@pytest.mark.asyncio
async def test_an_arc_dead_reenumeration_reaches_the_one_shot_on_its_short_clock(mesh, monkeypatch, verdict):
    """Replay: one chip drops, the gate's bridge rung re-creates its node with the ARC dead, the hold
    opens below the reset floor. The chip stays isolated, so the off-bus one-shot fires within one
    sampler tick (before, it waited out the 10-min window), and its reset lifts the hold only through
    the full post-reset check, fabric included."""
    evidence, released = verdict
    tick, bridges, count = _below_floor_hold(monkeypatch, mesh, {"now": IDLE})
    _bridge_rung(monkeypatch, "8", [None])
    patch_health_event(monkeypatch, lambda kind, **f: mesh.append((kind, f)))
    # The gate's own bridge pass (the real rung, not the hold's stub).
    assert await GalaxyRecovery._recover_isolated_chips(srv.galaxy_recovery, lambda m: None) is False
    assert srv.isolated_chips == {"8"}
    calls = []
    _verify_returning(monkeypatch, evidence, calls)
    await tick(srv.SAMPLE_INTERVAL_SEC)
    assert srv._offbus_one_shot_sec() <= srv.SAMPLE_INTERVAL_SEC
    assert bridges == ["8"], "the ARC-dead chip's hold did not reach the off-bus one-shot in one sampler tick"
    assert count("hold_deadline_forced_escalation") == 1 and srv.device_hold_offbus_escalated is True
    assert calls and all(calls), "the one-shot's post-reset verify skipped the fabric pass"
    if released:
        _assert_settled(released)
    else:
        assert srv.fsm.state is not ServerState.HEALTHY and FABRIC_DEBT.owed
        assert count("post_reset_release_refused") == 1


@pytest.mark.asyncio
async def test_a_kmd_without_the_heartbeat_attr_fails_no_chip_and_says_so_once(monkeypatch):
    """An old KMD exposes no tt_heartbeat: the check cannot answer, so it marks no chip ARC-dead (else
    every bridge recovery there fails). The skip is said once, not on every rung."""
    events = _bridge_rung(monkeypatch, "13", [None], attr=False)
    logs = []
    assert await srv.galaxy_recovery._recover_isolated_chips(logs.append) is True
    assert srv.isolated_chips == set()
    srv.isolated_chips = {"13"}
    assert await srv.galaxy_recovery._recover_isolated_chips(logs.append) is True
    kinds = [k for k, _ in events]
    assert "bridge_reset_arc_dead" not in kinds
    assert kinds.count("bridge_reset_arc_check_skipped") == 1
    assert len([m for m in logs if "ARC check skipped" in m]) == 1


@pytest.mark.asyncio
async def test_without_the_attr_the_rung_still_fails_on_its_other_checks(monkeypatch):
    """The skip decides nothing else: a chip whose bridge reset left it off the bus still fails the rung."""
    _bridge_rung(monkeypatch, "13", [None], attr=False)
    srv.isolated_chips = {"13", "14"}
    srv.device_pci_map = {"13": "0000:46:00.0", "14": "0000:47:00.0"}
    node13 = pci.SYSFS_CLASS_DIR / "tenstorrent!13"
    monkeypatch.setattr(recovery_pkg, "reset_chip_via_bridge", lambda i, bdf: i == "13" and (node13.mkdir() or True))
    assert await srv.galaxy_recovery._recover_isolated_chips(lambda m: None) is False
    assert srv.isolated_chips == {"14"}


@pytest.mark.asyncio
async def test_with_the_attr_an_unreadable_heartbeat_is_not_skipped(monkeypatch):
    """ENODATA on every chip makes heartbeat_supported() False too, but the attr is there: that is an
    ARC-dead chip, not an old KMD, and it stays isolated."""
    events = _bridge_rung(monkeypatch, "13", [None])
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: False)
    assert await srv.galaxy_recovery._recover_isolated_chips(lambda m: None) is False
    assert srv.isolated_chips == {"13"}
    assert "bridge_reset_arc_check_skipped" not in [k for k, _ in events]


def test_a_lost_marked_chip_gets_a_real_heartbeat_read(monkeypatch):
    """A chip marked lost is read for real, not reported all-ones from its mark; only the asked chips are read."""
    for i in ("13", "14"):
        node = pci.SYSFS_CLASS_DIR / f"tenstorrent!{i}"
        node.mkdir()
        (node / "tt_heartbeat").write_text("0")
    chip_state.mark_lost(["13"], "test", nodes={"13": pci.SYSFS_CLASS_DIR / "tenstorrent!13"})
    monkeypatch.setattr(chip_state, "PROBE_MIN_SEC", 0)  # its last real read is older than a probe's gap
    reads = []
    monkeypatch.setattr(heartbeat, "_read_beat", lambda n: reads.append(n.name) or "12345")
    assert heartbeat.arc_live_chips(["13"]) == ["13"]
    assert reads == ["tenstorrent!13"]


def test_the_arc_wait_is_bounded():
    """The wait adds at most a few seconds to the bridge rung (goal: shortest recovery per class)."""
    assert 0 < recovery_pkg.BRIDGE_RESET_ARC_WAIT_SEC <= 10
    assert timedelta(seconds=recovery_pkg.BRIDGE_RESET_ARC_WAIT_SEC) < timedelta(seconds=srv.SAMPLE_INTERVAL_SEC)
