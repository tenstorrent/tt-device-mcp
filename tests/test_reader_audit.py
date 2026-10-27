# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I66, reader by reader: nothing of the broker's reads a chip it marked lost while that chip
is still attached (its sysfs node is the one marked), outside a decision read, this broker's own reset
step and its grace, or a re-created node. A chip whose PCI remove failed, timed out or was skipped is
sealed: no decision read reaches it either. A hung chip that is still present is held for the ladder,
never PCI-removed, and its hold lifts only on a full check that passed with fabric on every cabled link.

Each sysfs read is a PCIe read of the chip; a read of an endpoint that left the bus stalls a core until
the completion timeout. Device-free: sysfs is a tmp tree, every remove, kill, probe and check stubbed."""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

import pytest

from tests.conftest import fsm_healthy, stub_device_pollers
from tests.test_chip_state import _reads_of, _ticking, clock, sysfs  # noqa: F401  (fixtures)
from tests.test_deadchip_detach_before_kill import valve  # noqa: F401  (fixture)
from tests.test_hold_lift_full_check import FABRIC, VERDICTS, _hold_marks
from tests.test_post_reset_gate import mesh  # noqa: F401  (fixture)
from tt_device_mcp import health_probe
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import aiclk_ceiling as ac
from tt_device_mcp.health import chip_state, read_health_events
from tt_device_mcp.health.aiclk_ceiling import CEILING
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitors import eth, heartbeat, pci
from tt_device_mcp.health.pci_writer import PCI_WRITER
from tt_device_mcp.health.post_reset import FABRIC_DEBT

ALL_ONES = heartbeat.ALL_ONES
N = 4
LOST = "2"
HOUR = 3600.0


def _lose(sysfs, idx=LOST):  # noqa: F811
    """Chip ``idx`` reads all-ones and is marked lost on its own node: still attached."""
    sysfs["node"](int(idx), ALL_ONES)
    chip_state.mark_lost([idx], "test", chip_state.chip_nodes([idx]))
    sysfs["reads"].clear()


def _end_reset_step():
    chip_state.set_recovering("op:test", True)
    chip_state.set_recovering("op:test", False)


# --- the telemetry sampler and the incident snapshot ----------------------------------------------


def test_the_sampler_reads_a_lost_attached_chip_zero_times_in_an_hour(sysfs, clock):  # noqa: F811
    """One sample every 10 s for an hour (the sampler's own readers, and the incident snapshot): the
    lost chip is reported all-ones from its mark, never read; the others are read on every sample."""
    _lose(sysfs)
    samples = 0
    while clock.now < 1000.0 + HOUR:
        assert pci.chip_sample()[LOST][0] == ALL_ONES
        assert heartbeat.read_heartbeats()[LOST] == ALL_ONES
        samples += 1
        clock.now += 10.0
    pci.chip_snapshot()
    assert samples == 360
    assert _reads_of(sysfs, LOST) == [], "a poller read a lost, attached chip"
    assert _reads_of(sysfs, 0).count("tt_heartbeat") >= 2 * samples, "the healthy chips are still read"


# --- /health and the health probe that restarts the pollers ----------------------------------------


def test_health_reads_no_lost_chip_and_the_probe_stands_back_on_it(sysfs, clock, monkeypatch, clear_job_state):  # noqa: F811
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: N)
    monkeypatch.setattr(srv.health_monitor, "_health_check_enabled", lambda: True)
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_healthy(srv)
    _lose(sysfs)
    for _ in range(3):
        payload = srv._health_payload()
        clock.now += HOUR
    assert _reads_of(sysfs, LOST) == [], "/health read a lost, attached chip"
    assert payload["lost_chips"] == [LOST]
    # The probe would restart the pollers onto the chip (an older driver keeps its mapping): it refuses,
    # even with every other field of the payload healthy and idle.
    clean = {**payload, "held": False, "fsm_state": "healthy", "device_degraded": None, "running": 0, "queued": 0}
    assert "lost" in health_probe.device_verdict(clean)
    assert "lost" in health_probe.idle_verdict(clean)
    for owner in ("recovering", "reset_in_flight"):
        busy = {**clean, "lost_chips": [], owner: True}
        assert "reset step" in health_probe.device_verdict(busy)
    assert health_probe.idle_verdict({**clean, "lost_chips": []}) is None


# --- whole-device readers: the eth-heartbeat probe and the aiclk-ceiling helper --------------------


@pytest.mark.asyncio
async def test_the_eth_reader_is_not_built_while_a_lost_chip_is_attached(sysfs, clock, monkeypatch):  # noqa: F811
    """The eth reader maps every chip: not built, nothing spawned, a skip (never a pass). Inside this
    broker's own reset step it runs as before, and once the lost chip's node is gone (removed) too."""
    built = []
    monkeypatch.setattr(eth, "build", lambda: built.append(1) or None)
    monkeypatch.setattr(eth, "unavailable", lambda: ("not_configured", "test"))
    _lose(sysfs)
    ok, detail = await srv.health_monitor._verify_eth_heartbeat_body(60.0)
    assert ok is None and "lost and still attached" in detail and built == []

    chip_state.set_recovering("op:test", True)
    try:
        await srv.health_monitor._verify_eth_heartbeat_body(60.0)
    finally:
        chip_state.set_recovering("op:test", False)
    assert built == [1], "the reset step's own reads are ungated"

    clock.now += chip_state.GRACE_SEC + 1
    (sysfs["dir"] / f"tenstorrent!{LOST}").unlink()  # removed: no longer attached
    await srv.health_monitor._verify_eth_heartbeat_body(60.0)
    assert built == [1, 1]
    assert _reads_of(sysfs, LOST) == []


@pytest.mark.asyncio
async def test_the_aiclk_ceiling_helper_does_not_run_on_a_lost_attached_chip(sysfs, clock, monkeypatch):  # noqa: F811
    for k in (ac.ENV_CMD, ac.ENV_TIMEOUT, ac.ENV_PYTHON, ac.ENV_SETTLE):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(ac.ENV_MHZ, "900")
    monkeypatch.setattr(CEILING, "owed", True)
    monkeypatch.setattr(CEILING, "disarmed", "")
    monkeypatch.setattr(CEILING, "last", {})
    monkeypatch.setattr(CEILING, "proc", None)
    monkeypatch.setattr(ac, "health_event", lambda kind, **f: None)
    runs = []

    async def run_probe(argv, **_):
        runs.append(argv)
        return 0, '{"summary": true, "mhz": 900, "chips": 4, "ok": 4, "sent": 4}'

    monkeypatch.setattr(ac, "run_probe", run_probe)
    _lose(sysfs)
    ok, detail, ev = await CEILING.apply("post-job")
    assert ok is False and ev == {"lost_chips": [LOST]} and "lost and still attached" in detail
    assert runs == [] and CEILING.owed, "not verified: the ceiling stays owed"

    chip_state.set_recovering("op:test", True)
    try:
        await CEILING.apply("post-reset")
    finally:
        chip_state.set_recovering("op:test", False)
    assert len(runs) == 1, "the reset step's own apply runs"
    assert _reads_of(sysfs, LOST) == []


# --- the backstop is gone: decision reads, the seal, the grace, a re-created node ------------------


def test_no_clock_makes_a_lost_chip_due_only_a_decision_read_does_once(sysfs, clock):  # noqa: F811
    _lose(sysfs)
    node = sysfs["dir"] / f"tenstorrent!{LOST}"
    for _ in range(24):
        clock.now += HOUR
        assert chip_state.may_read(LOST, node) is False
    chip_state.reprobe_due([LOST])
    assert chip_state.may_read(LOST, node) is True
    assert chip_state.may_read(LOST, node) is False, "one decision, one read"
    clock.now += HOUR
    assert chip_state.may_read(LOST, node) is False


def test_a_sealed_chip_is_read_by_no_decision_only_by_the_grace_or_a_new_node(sysfs, clock, monkeypatch):  # noqa: F811
    """Its remove failed: still attached. The check's heartbeat verdict, a decision read, fails it on its
    mark without reading it. A reset step's grace, or a re-created node, reads it for real."""
    _lose(sysfs)
    assert chip_state.seal([LOST], "PCI remove failed") == [LOST]
    assert chip_state.seal([LOST], "again") == [], "sealed once"
    clock.now += HOUR
    chip_state.reprobe_due()
    assert heartbeat.read_heartbeats(probe_lost=True)[LOST] == ALL_ONES
    _ticking(monkeypatch, sysfs, [0, 1, 3])
    verdict, detail, ev = heartbeat.heartbeat_verdict(expected_count=N)
    assert verdict is Verdict.UNHEALTHY and LOST in ev["dead"], detail
    assert _reads_of(sysfs, LOST) == [], "a decision read reached a sealed chip"

    _end_reset_step()
    heartbeat.read_heartbeats()
    assert _reads_of(sysfs, LOST) == ["tt_heartbeat"], "the post-reset grace reads it for real"
    assert chip_state.is_lost(LOST)

    sysfs["reads"].clear()
    clock.now += chip_state.GRACE_SEC + 1
    heartbeat.read_heartbeats(probe_lost=True)
    assert _reads_of(sysfs, LOST) == [], "past the grace the seal holds again"
    sysfs["node"](int(LOST), 777, endpoint="b")  # removed and rescanned: a new endpoint
    assert heartbeat.read_heartbeats()[LOST] == 777
    assert not chip_state.is_lost(LOST)


def test_a_node_the_mark_found_none_for_is_read_once_it_appears(sysfs, clock):  # noqa: F811
    """Marked with no node (already gone from sysfs), then a node appears: a rescan, so it is read."""
    (sysfs["dir"] / f"tenstorrent!{LOST}").unlink()
    chip_state.mark_lost([LOST], "not in sysfs", chip_state.chip_nodes([LOST]))
    sysfs["node"](int(LOST), 555, endpoint="b")
    sysfs["reads"].clear()
    assert heartbeat.read_heartbeats()[LOST] == 555
    assert not chip_state.is_lost(LOST)


# --- the dead-chip valve seals a chip it could not remove -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["failed", "timed out", "skipped, PCI-wedged"])
async def test_a_remove_that_failed_timed_out_or_was_skipped_seals_the_chip(valve, sysfs, clock, monkeypatch, fault):  # noqa: F811
    log, detach_ok = valve
    release = threading.Event()
    if fault == "failed":
        detach_ok[LOST] = False
    elif fault == "timed out":
        monkeypatch.setattr(telemetry, "DETACH_TIMEOUT_SEC", 0.3)

        def hung_remove(idx):
            log.append(("detach", idx))
            release.wait(5)
            return True

        monkeypatch.setattr(srv, "isolate_chip", hung_remove)
    else:
        PCI_WRITER.latch("test: a remove returned a failure")
    _lose(sysfs)
    try:
        await srv.sampler.isolate_dead_chips([LOST])
    finally:
        release.set()
    assert ("detach", LOST) in log or fault.startswith("skipped")
    sealed = read_health_events(kinds={"chip_lost_sealed"})
    assert sealed and sealed[-1]["chips"] == [LOST]
    clock.now += HOUR
    chip_state.reprobe_due()
    heartbeat.read_heartbeats(probe_lost=True)
    assert _reads_of(sysfs, LOST) == [], "a decision read reached a chip whose remove did not happen"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_removed_chip_is_not_sealed(valve, sysfs, clock):  # noqa: F811
    _lose(sysfs)
    await srv.sampler.isolate_dead_chips([LOST])
    assert read_health_events(kinds={"chip_lost_sealed"}) == []


# --- a hung chip that is still present is held, never removed ---------------------------------------


def _row(beat):
    return [beat, 800, 800, 0, 0, 0]


_TICK = itertools.count(1000, 10)


async def _samples(frozen, rounds):
    """``rounds`` samples; chips in ``frozen`` keep one heartbeat, the rest tick on every sample."""
    for _ in range(rounds):
        t = next(_TICK)
        await srv.sampler.check_for_dead_chips({str(i): _row(100 if str(i) in frozen else t + i) for i in range(N)})


@pytest.mark.asyncio
async def test_a_hung_present_chip_is_held_once_and_never_removed(valve, monkeypatch):  # noqa: F811
    log, _ = valve
    dirty = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, **kw: dirty.append((reason, kw.get("why"))))
    await _samples({"1"}, 2)
    assert dirty == [], "one frozen sample is no hold (stops the pollers only)"
    await _samples({"1"}, 1)
    assert dirty == [("chip(s) 1 heartbeat frozen", "heartbeat")]
    await _samples({"1"}, 3)
    assert len(dirty) == 1, "held once per episode"
    assert [e for e in log if e[0] == "detach"] == [], "a present hung chip got a PCI remove"
    assert chip_state.lost_chips() == []
    held = read_health_events(kinds={"chip_hung_held"})
    assert len(held) == 1 and held[0]["chips"] == ["1"] and held[0]["removed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["reset_in_flight", "scope", "every chip"])
async def test_no_hung_chip_hold_under_a_reset_or_a_whole_mesh_stall(valve, monkeypatch, owner):  # noqa: F811
    dirty = []
    monkeypatch.setattr(srv, "_mark_device_dirty", lambda reason, **kw: dirty.append(reason))
    stub_device_pollers(monkeypatch)
    frozen = {"1"}
    if owner == "reset_in_flight":
        srv.recovery_mechanism.reset_in_flight = True
    elif owner == "scope":
        monkeypatch.setattr(srv.sampler, "scope_seen", True)
    else:
        frozen = {str(i) for i in range(N)}
    try:
        await _samples(frozen, 4)
    finally:
        srv.recovery_mechanism.reset_in_flight = False
    assert dirty == [] and read_health_events(kinds={"chip_hung_held"}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", VERDICTS)
async def test_a_hung_chip_hold_lifts_only_on_a_full_check_with_every_cabled_link(
    valve, sysfs, mesh, monkeypatch, verdict  # noqa: F811
):
    """The hold the sampler places on a frozen heartbeat goes through the same release funnel (I78):
    a check that failed, or found a cabled link missing, keeps the hold, its episode and its clock."""
    log, _ = valve
    for extra in range(N, 32):
        (Path(srv.TT_DEV_DIR) / str(extra)).unlink()
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: N)
    stub_device_pollers(monkeypatch)
    fsm_healthy(srv)
    await _samples({"1"}, 3)
    assert srv.fsm.state is ServerState.RECOVERING and srv.fsm.record.why == "heartbeat"
    assert [k for k, _ in mesh] == ["pollers_quiesced_hung_chip", "chip_hung_held"], "held by the hung chip alone"
    assert not [e for e in log if e[0] == "detach"]
    before = _hold_marks()
    FABRIC_DEBT.note_evidence({"fabric": dict(FABRIC[verdict])})
    released = srv._clear_device_dirty(verified=True, why="gate/idle: verified healthy")
    assert released is (verdict == "pass")
    if verdict != "pass":
        assert _hold_marks() == before
