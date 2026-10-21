# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I66: no broker poller reads a chip it has confirmed lost, or any chip while a reset step owns
them. Each sysfs telemetry read is a PCIe read of the chip; a read to an endpoint that left the bus or
hung stalls a core until the completion timeout, and enough of them take the host down."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.conftest import fsm_healthy, patch_health_event, patch_recovery, stub_device_pollers
from tt_device_mcp import server as srv
from tt_device_mcp.health import chip_state
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitors import heartbeat, pci

ALL_ONES = heartbeat.ALL_ONES
N = 4


class _Clock:
    """chip_state's own monotonic clock, moved by hand (never the process-wide one asyncio uses)."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()

    class _T:
        monotonic = staticmethod(c.monotonic)

    monkeypatch.setattr(chip_state, "time", _T)
    return c


@pytest.fixture
def sysfs(monkeypatch, tmp_path):
    """N chips laid out as on a real host: the class entry is a link to a node under its PCI endpoint,
    so a remove and rescan (a new endpoint) gives the class entry a new identity."""
    cls_dir = tmp_path / "class"
    cls_dir.mkdir()
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", cls_dir)
    reads: list = []
    real_read_text = Path.read_text

    def spy(self, *a, **k):
        if self.parent.name.startswith("tenstorrent!"):
            reads.append((self.parent.name.rsplit("!", 1)[-1], self.name))
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)

    def node(i: int, beat: int, *, endpoint: str = "a") -> Path:
        ep = tmp_path / f"devices-{endpoint}" / f"0000:{i + 1:02x}:00.0"
        real = ep / "tenstorrent" / f"tenstorrent!{i}"
        real.mkdir(parents=True, exist_ok=True)
        (real / "device").unlink(missing_ok=True)
        (real / "device").symlink_to(ep)
        for attr, val in (("tt_heartbeat", beat), ("tt_aiclk", 800), ("tt_arcclk", 800), ("tt_therm_trip_count", 0)):
            (real / attr).write_text(f"{val}\n")
        link = cls_dir / f"tenstorrent!{i}"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(real)
        return link

    for i in range(N):
        node(i, 100)
    return {"node": node, "reads": reads, "dir": cls_dir}


def _reads_of(sysfs, idx):
    return [a for i, a in sysfs["reads"] if i == str(idx)]


# --- the gate on a lost chip ---------------------------------------------------------------------


def test_a_lost_chip_is_not_read_again_within_the_reprobe_window(sysfs, clock):
    """Once lost, read_heartbeats and chip_sample report the chip all-ones without a read, until its
    backstop re-read is due; then exactly one read, and the window starts again."""
    sysfs["node"](2, ALL_ONES)
    chip_state.mark_lost(["2"], "test", {"2": sysfs["dir"] / "tenstorrent!2"})
    sysfs["reads"].clear()

    for _ in range(3):
        assert heartbeat.read_heartbeats()["2"] == ALL_ONES
        assert pci.chip_sample()["2"][:4] == [ALL_ONES, None, None, None]
        clock.now += chip_state.LOST_REPROBE_SEC / 4
    assert _reads_of(sysfs, 2) == [], "read a lost chip on the poll clock"
    assert _reads_of(sysfs, 1), "the healthy chips are still read"
    snap = pci.chip_snapshot()
    assert snap["2"].get("lost") is True
    assert _reads_of(sysfs, 2) == [], "the incident snapshot read a lost chip"

    clock.now += chip_state.LOST_REPROBE_SEC
    assert pci.chip_sample()["2"][0] == ALL_ONES
    assert _reads_of(sysfs, 2) == ["tt_heartbeat"], "the due re-read is the heartbeat alone, once"
    assert heartbeat.read_heartbeats()["2"] == ALL_ONES
    assert _reads_of(sysfs, 2) == ["tt_heartbeat"], "the next re-read waits a full window again"
    assert chip_state.is_lost("2")


def test_a_re_created_node_is_read_at_once_and_a_valid_read_clears_lost(sysfs, clock, monkeypatch):
    """A remove and rescan (or a tray re-power) makes a new endpoint: it is read on the next poll, not a
    window later, and a counter that is not all-ones clears the mark and the published file."""
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr("tt_device_mcp.health.evidence.health_event", lambda kind, **f: events.append((kind, f)))
    sysfs["node"](3, ALL_ONES)
    chip_state.mark_lost(["3"], "test", {"3": sysfs["dir"] / "tenstorrent!3"})
    assert json.loads(chip_state.state_path().read_text())["lost"] == ["3"]
    sysfs["reads"].clear()

    sysfs["node"](3, 4242, endpoint="b")  # re-enumerated: a new node under a new endpoint
    assert heartbeat.read_heartbeats()["3"] == 4242
    assert _reads_of(sysfs, 3) == ["tt_heartbeat"]
    assert not chip_state.is_lost("3")
    assert any(k == "chip_lost_cleared" and f["chip"] == "3" and f["why"] == "sysfs node re-created" for k, f in events)
    assert json.loads(chip_state.state_path().read_text())["lost"] == []

    # Same node, the backstop re-read finds it answering: that clears it too.
    chip_state.mark_lost(["1"], "test", {"1": sysfs["dir"] / "tenstorrent!1"})
    clock.now += chip_state.LOST_REPROBE_SEC
    assert heartbeat.read_heartbeats()["1"] == 100
    assert not chip_state.is_lost("1")
    assert any(k == "chip_lost_cleared" and f["why"] == "answering again" for k, f in events)


def test_the_published_state_is_atomic_and_names_every_expected_chip(sysfs, monkeypatch):
    """Third-party readers stay off a chip by this file: version, writer pid, recovering, lost (marked, or
    expected with no sysfs node), written by rename so a reader never sees half a file."""
    chip_state.configure(present=lambda: ["0", "1", "2"], expected=lambda: N)
    monkeypatch.setattr(chip_state, "_present", lambda: ["0", "1", "2"])
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(
        chip_state.os, "replace", lambda a, b: replaced.append((Path(a).name, Path(b))) or real_replace(a, b)
    )
    chip_state.mark_lost(["1"], "all-ones on two samples")

    path = chip_state.state_path()
    assert replaced and replaced[-1][1] == path and replaced[-1][0].startswith(".chip-state.json.")
    body = json.loads(path.read_text())
    assert body["version"] == 1 and body["pid"] == os.getpid()
    assert body["recovering"] is False and body["lost"] == ["1", "3"]
    assert body["chips"]["1"]["lost_reason"] == "all-ones on two samples"
    assert body["chips"]["3"]["lost_reason"] == "not in sysfs"
    assert body["chips"]["0"] == {"lost": False, "recovering": False}
    assert not list(path.parent.glob(".chip-state.json.*")), "no temp file left behind"

    n = len(replaced)
    chip_state.publish()
    assert len(replaced) == n, "an unchanged state is not rewritten"
    chip_state.set_recovering("op:1", True)
    assert json.loads(path.read_text())["chips"]["0"]["recovering"] is True
    chip_state.set_recovering("op:1", False)
    assert json.loads(path.read_text())["recovering"] is False
    assert oct(path.stat().st_mode & 0o777) == "0o644"


def test_the_default_path_follows_the_device_op_lock(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_CHIP_STATE_FILE")
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", raising=False)
    assert chip_state.state_path() == Path("/run/tt-device-broker/chip-state.json")
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/run/user/1000/tt/device-op.lock")
    assert chip_state.state_path() == Path("/run/user/1000/tt/chip-state.json")


# --- the sampler ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_single_blip_is_not_marked_lost_but_two_samples_are(monkeypatch, sysfs):
    isolated = []

    async def fake(dead):
        isolated.extend(dead)

    monkeypatch.setattr(srv.sampler, "isolate_dead_chips", fake)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    stub_device_pollers(monkeypatch)
    live = lambda b: [b, 800, 800, 0, 0, 0]  # noqa: E731
    await srv.sampler.check_for_dead_chips({"0": live(1), "2": live(ALL_ONES), "3": live(1)})
    assert chip_state.lost_chips() == [], "one sample is a blip"
    await srv.sampler.check_for_dead_chips({"0": live(2), "2": live(3), "3": live(2)})
    await srv.sampler.check_for_dead_chips({"0": live(3), "2": live(ALL_ONES), "3": live(3)})
    assert chip_state.lost_chips() == [], "a cleared strike starts over"
    await srv.sampler.check_for_dead_chips({"0": live(4), "2": live(ALL_ONES), "3": live(4)})
    assert chip_state.lost_chips() == ["2"] and isolated == ["2"], "lost on the second sample, before isolation"


def _one_pass(monkeypatch):
    class _Stop(Exception):
        pass

    async def _sleep_once(_):
        raise _Stop()

    monkeypatch.setattr(srv.asyncio, "sleep", _sleep_once)
    monkeypatch.setattr(srv, "_refresh_idle_hold_ledger", lambda: None)
    monkeypatch.setattr(srv, "_spawn_idle_relift", lambda: None, raising=False)
    with pytest.raises(_Stop):
        asyncio.run(srv.sampler.run())


@pytest.mark.parametrize("owner", ["reset_in_flight", "scope"])
def test_the_sampler_reads_no_chip_while_a_reset_owns_them(monkeypatch, sysfs, owner):
    """A reset step (our flag), or a reset scope we stopped waiting for, owns the chips: the sampler
    takes no sample at all, and reads again once the step is over."""
    samples = []
    monkeypatch.setattr(srv, "chip_sample", lambda: samples.append(1) or {})
    monkeypatch.setattr(srv, "append_trace", lambda s: None)
    if owner == "reset_in_flight":
        srv.recovery_mechanism.reset_in_flight = True
    else:
        monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "ttdev-reset-1-1")
        srv.sampler.scope_seen = True
        chip_state.set_recovering("scope", True)
    _one_pass(monkeypatch)
    assert samples == [], "sampled chips a reset owns"
    assert json.loads(chip_state.state_path().read_text())["recovering"] is True

    if owner == "reset_in_flight":
        srv.recovery_mechanism.reset_in_flight = False
    else:
        monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    _one_pass(monkeypatch)
    assert samples == [1]
    assert not chip_state.recovering() and not srv.sampler.scope_seen


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["all-ones", "stalled heartbeat"])
async def test_the_first_hung_sample_stops_the_pollers(monkeypatch, kind):
    """A present chip that reads all-ones, or whose heartbeat did not move since the last sample, stops
    the pollers on that first sample (isolation still waits for its second); once per episode."""
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv.sampler, "isolate_dead_chips", lambda d: asyncio.sleep(0))
    events = []
    patch_health_event(monkeypatch, lambda k, **f: events.append((k, f)))
    monkeypatch.setattr(srv.sampler._deps, "health_event", lambda k, **f: events.append((k, f)), raising=False)
    pollers = stub_device_pollers(monkeypatch, stopped=["tt-poller.service"])
    row = lambda b: [b, 800, 800, 0, 0, 0]  # noqa: E731
    await srv.sampler.check_for_dead_chips({str(i): row(100 + i) for i in range(N)})
    assert pollers == []
    bad = ALL_ONES if kind == "all-ones" else 101
    hung = {"0": row(200), "1": row(bad), "2": row(202), "3": row(203)}
    await srv.sampler.check_for_dead_chips(hung)
    assert pollers == [False], "the pollers did not stop at the first hung sample"
    stops = [f for k, f in events if k == "pollers_quiesced_hung_chip"]
    assert stops and (stops[0]["all_ones"] if kind == "all-ones" else stops[0]["stalled"]) == ["1"]
    hung2 = {"0": row(300), "1": row(bad), "2": row(302), "3": row(303)}
    await srv.sampler.check_for_dead_chips(hung2)
    assert pollers == [False], "one episode stops them once"


@pytest.mark.asyncio
async def test_a_whole_mesh_blip_or_a_reset_does_not_stop_the_pollers(monkeypatch):
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    patch_health_event(monkeypatch, lambda k, **f: None)
    pollers = stub_device_pollers(monkeypatch)
    await srv.sampler.check_for_dead_chips({str(i): [ALL_ONES] for i in range(N)})
    assert pollers == [], "every chip at once is the re-init blip the blackout path proves on two samples"
    srv.sampler.dead_chip_strikes = {}
    srv.recovery_mechanism.reset_in_flight = True
    await srv.sampler.check_for_dead_chips({"0": [ALL_ONES], "1": [5], "2": [5], "3": [5]})
    assert pollers == [], "a reset's own all-ones never stops them"


# --- a reset step --------------------------------------------------------------------------------


def _reset_seams(monkeypatch, order, rc):
    async def pollers(active, log):
        order.append(f"pollers:{active}")
        return ["telem"] if not active else []

    async def run(argv, log, owner="[broker]health-gate"):
        order.append(("reset", chip_state.recovering()))
        return rc, "out"

    async def to_thread(fn, *a, **k):
        return None

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
@pytest.mark.parametrize("rc, restored", [(0, True), (1, False)])
async def test_a_failed_reset_leaves_the_pollers_stopped(monkeypatch, rc, restored):
    """A reset that exited non-zero leaves chips that may still be off the bus or hung, and the device
    held: the pollers stay down, owed to the next HEALTHY (04 I12). A completed one restores them. The
    step is published as recovering while it runs."""
    order = []
    events = []
    patch_health_event(monkeypatch, lambda k, **f: events.append((k, f)))
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None, raising=False)
    _reset_seams(monkeypatch, order, rc)
    got, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert got == rc
    assert ("reset", True) in order, "the reset step was not published as recovering"
    assert (order[-1] == "pollers:True") is restored, order
    assert order.count("pollers:True") == (1 if restored else 0)
    assert not chip_state.recovering()
    if not restored:
        assert any(k == "pollers_left_quiesced" for k, _ in events)


def test_the_post_reset_grace_reads_lost_chips_for_real(sysfs, clock, monkeypatch):
    """A blackout marks every chip lost. While the reset step runs its own reads go through; once it
    ends, the verify's heartbeat_verdict reads every chip for real (at most once a second each) instead
    of waiting out the backstop, so the post-reset check judges the chips as they are."""
    chip_state.mark_lost(
        [str(i) for i in range(N)], "blackout", {str(i): sysfs["dir"] / f"tenstorrent!{i}" for i in range(N)}
    )
    sysfs["reads"].clear()
    assert heartbeat.read_heartbeats() == {str(i): ALL_ONES for i in range(N)}
    assert sysfs["reads"] == [], "a lost chip read before any reset"

    srv.recovery_mechanism.reset_in_flight = True
    heartbeat.read_heartbeats()
    assert len(sysfs["reads"]) == N, "the step's own reads are ungated"
    srv.recovery_mechanism.reset_in_flight = False
    sysfs["reads"].clear()

    step = iter(range(200, 10_000, 100))

    def advance(_):
        v = next(step)
        for i in range(N):
            sysfs["node"](i, v + i)

    monkeypatch.setattr(heartbeat.time, "sleep", advance)
    verdict, detail, _ = heartbeat.heartbeat_verdict(expected_count=N)
    assert verdict is Verdict.HEALTHY, detail
    assert chip_state.lost_chips() == []
    assert len(sysfs["reads"]) >= N


@pytest.mark.asyncio
async def test_the_post_reset_health_check_runs_fabric_on_chips_marked_lost(sysfs, monkeypatch):
    """Goal of 03 I66 is fewer reads, never a skipped check: after a reset of a mesh whose chips were
    marked lost, the full post-reset check (heartbeat, enumeration, eth, fabric) runs and passes."""
    chip_state.mark_lost(
        [str(i) for i in range(N)], "blackout", {str(i): sysfs["dir"] / f"tenstorrent!{i}" for i in range(N)}
    )
    srv.recovery_mechanism.reset_in_flight = True
    srv.recovery_mechanism.reset_in_flight = False  # the step ended: grace

    step = iter(range(200, 10_000, 100))

    def advance(_):
        v = next(step)
        for i in range(N):
            sysfs["node"](i, v + i)

    monkeypatch.setattr(heartbeat.time, "sleep", advance)
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)
    monkeypatch.setattr(monitor_mod.hostpci, "host_pci_verdict", lambda: (True, "ok", {}))
    ran = []

    async def snap(expected):
        ran.append("snapshot")
        return True, "ok"

    async def eth(**_):
        ran.append("eth")
        return True, "ok"

    async def fabric(*a, **k):
        ran.append("fabric")
        return True, "fabric links healthy"

    monkeypatch.setattr(srv.health_monitor, "_verify_device", snap)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth)
    monkeypatch.setattr(srv.health_monitor, "verify_fabric_health", fabric)
    fsm_healthy(srv)
    state = await srv.health_monitor.update("post-reset", run_fabric=True, expected=N)
    verdicts = {o.monitor: o.verdict for o in state.observations}
    assert verdicts.get("heartbeat") is Verdict.HEALTHY, state.observations
    assert ran == ["snapshot", "eth", "fabric"], "the post-reset check stopped short of the fabric pass"
    assert verdicts.get("fabric") is Verdict.HEALTHY


# --- a check is not a poll: it reads a lost chip for real ------------------------------------------


def _ticking(monkeypatch, sysfs, ids):
    """The settle sleep advances the counters of ``ids`` (the chips that answer)."""
    step = iter(range(200, 10_000, 100))

    def advance(_):
        v = next(step)
        for i in ids:
            sysfs["node"](i, v + i)

    monkeypatch.setattr(heartbeat.time, "sleep", advance)


def test_a_lost_chip_that_answers_again_passes_the_health_check_inside_the_reprobe_window(sysfs, clock, monkeypatch):
    """A chip marked lost (a whole-mesh blackout, a failed isolation) that answers again on the same node
    must not be judged off the bus on a value nobody read: the gate's heartbeat verdict reads it for real,
    at once, so it passes and the mark clears. Before, the gated all-ones failed every check (gate, dispatch
    recheck, idle relift) as 'FELL OFF THE PCIe BUS' for up to the 30 s backstop."""
    sysfs["node"](2, ALL_ONES)
    chip_state.mark_lost(["2"], "all-ones on two samples", chip_state.chip_nodes(["2"]))
    clock.now += 1.0  # well inside the reprobe window
    sysfs["node"](2, 12345)  # same node, answering again
    sysfs["reads"].clear()
    assert heartbeat.read_heartbeats()["2"] == ALL_ONES, "a poll inside the window still reads nothing"
    assert _reads_of(sysfs, 2) == []

    _ticking(monkeypatch, sysfs, range(N))
    verdict, detail, _ = heartbeat.heartbeat_verdict(expected_count=N)
    assert verdict is Verdict.HEALTHY, detail
    assert not chip_state.is_lost("2")


def test_a_check_reads_a_still_lost_chip_once_and_judges_it_on_that_read(sysfs, clock, monkeypatch):
    """A chip still off the bus is read once per check (not twice, not on the poll clock), and the check
    fails on that real read. The reprobe clock restarts, so the pollers stay off it."""
    sysfs["node"](2, ALL_ONES)
    chip_state.mark_lost(["2"], "test", chip_state.chip_nodes(["2"]))
    clock.now += 1.0
    sysfs["reads"].clear()
    _ticking(monkeypatch, sysfs, [0, 1, 3])
    verdict, detail, ev = heartbeat.heartbeat_verdict(expected_count=N)
    assert verdict is Verdict.UNHEALTHY and ev["dead"] == ["2"], detail
    assert _reads_of(sysfs, 2) == ["tt_heartbeat"], "one real read per check"
    assert chip_state.is_lost("2")
    clock.now += chip_state.LOST_REPROBE_SEC / 2
    heartbeat.read_heartbeats()
    assert _reads_of(sysfs, 2) == ["tt_heartbeat"], "the check restarted the reprobe clock"


def test_the_reset_gate_does_not_count_a_lost_chip_that_answers_again(sysfs, clock, monkeypatch):
    """The per-host reset gate's off-bus count (spec 04 I24) is a decision too: a lost chip that answers
    again on its node is on the bus and must not hold a mesh reset; one still all-ones counts."""
    from tt_device_mcp.health.recovery import pcie_guard

    monkeypatch.setattr(pcie_guard, "tt_endpoints", lambda: {f"0000:{i + 1:02x}:00.0": None for i in range(N)})
    sysfs["node"](2, ALL_ONES)
    chip_state.mark_lost(["2"], "test", chip_state.chip_nodes(["2"]))
    clock.now += 1.0
    assert pcie_guard.chips_off_bus(N) == 1
    sysfs["node"](2, 777)
    clock.now += 1.0
    assert pcie_guard.chips_off_bus(N) == 0
    assert not chip_state.is_lost("2")


def _lost_and_back(sysfs, clock, idx="2"):
    """Chip ``idx`` marked lost on a real all-ones read, then answering again on the same node,
    inside the reprobe window: a poll still reports the mark, a decision read must not."""
    sysfs["node"](int(idx), ALL_ONES)
    chip_state.mark_lost([idx], "test", chip_state.chip_nodes([idx]))
    clock.now += chip_state.PROBE_MIN_SEC
    sysfs["node"](int(idx), 12345)
    sysfs["reads"].clear()


def test_a_decision_read_judges_a_lost_chip_on_a_real_read_and_reads_a_dead_one_once(sysfs, clock):
    """The broker's other decision reads (the DOWN off-bus recheck, the hold-deadline escalation, the
    gate's rung counts, the recovery ladder's reads) go through _decision_heartbeats: a lost chip that
    answers again is read and cleared, not counted off the bus on the mark. A chip still dead is read
    once however many decisions run back to back, again only PROBE_MIN_SEC later, and polls stay gated."""
    _lost_and_back(sysfs, clock)
    assert heartbeat.read_heartbeats()["2"] == ALL_ONES and _reads_of(sysfs, 2) == [], "polls stay gated"
    beats = srv._decision_heartbeats()
    assert beats["2"] == 12345 and _reads_of(sysfs, 2) == ["tt_heartbeat"]
    assert srv._offbus_count(beats, N) == 0 and not chip_state.is_lost("2")

    sysfs["node"](3, ALL_ONES)
    chip_state.mark_lost(["3"], "test", chip_state.chip_nodes(["3"]))
    clock.now += chip_state.PROBE_MIN_SEC
    sysfs["reads"].clear()
    for _ in range(3):
        assert srv._offbus_count(srv._decision_heartbeats(), N) == 1
    assert _reads_of(sysfs, 3) == ["tt_heartbeat"], "back-to-back decisions read a dead chip once"
    clock.now += chip_state.PROBE_MIN_SEC
    srv._decision_heartbeats()
    assert _reads_of(sysfs, 3) == ["tt_heartbeat"] * 2
    heartbeat.read_heartbeats()
    assert _reads_of(sysfs, 3) == ["tt_heartbeat"] * 2, "a decision read restarts the poll clock"


def test_the_recovery_ladder_reads_lost_chips_for_real(sysfs, clock):
    """The galaxy ladder's rung selection and verify counts read through the recovery deps."""
    _lost_and_back(sysfs, clock)
    assert srv._recovery_deps.read_heartbeats()["2"] == 12345
    assert not chip_state.is_lost("2")


def test_admission_and_exec_probe_a_lost_chip_status_does_not(sysfs, clock, monkeypatch):
    """Job admission, exec and a Slurm step decide whether a tenant gets the device: a lost chip that
    answers again is not refused as 'fell off the PCIe bus' on an all-ones nobody read. Status and
    /health polls keep the gated answer and read nothing."""
    monkeypatch.setattr(srv, "heartbeat_supported", lambda: True)
    monkeypatch.setattr(srv, "_device_unavailable_for_tenant", lambda: "")
    monkeypatch.setattr(srv, "health_monitor", None)
    _lost_and_back(sysfs, clock)
    assert "0xFFFFFFFF" in srv._device_degraded_for_tenant() and _reads_of(sysfs, 2) == []
    assert srv._device_degraded_for_decision() == ""
    assert _reads_of(sysfs, 2) == ["tt_heartbeat"] and not chip_state.is_lost("2")


def _all_ones_unless_probed(monkeypatch, n):
    """read_heartbeats as the gate gives it for lost chip 5 that is back: all-ones on a poll, its real
    counter on a read just after chip_state.reprobe_due."""
    calls: list = []
    monkeypatch.setattr(chip_state, "reprobe_due", lambda: calls.append("due"))

    def beats():
        probed = calls[-1:] == ["due"]
        calls.append("read")
        return {str(i): ALL_ONES if i == 5 and not probed else 100 + i for i in range(n)}

    monkeypatch.setattr(srv, "read_heartbeats", beats)
    return calls


@pytest.mark.asyncio
async def test_the_down_offbus_recheck_does_not_hold_on_a_lost_chip_that_is_back(
    monkeypatch, tmp_path, clear_job_state
):
    """Every chip is back, one of them still marked lost: the recheck runs the full gate at once instead
    of logging 'still off the bus' and waiting out another relift interval (up to 120 s)."""
    from tests.test_device_safety import _setup_down_offbus

    _setup_down_offbus(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)
    monkeypatch.setattr(srv, "arc_not_ready_chips", lambda beats: set())
    calls = _all_ones_unless_probed(monkeypatch, 32)
    gated: list = []

    async def gate(*a, **k):
        gated.append(True)
        return False, {}

    monkeypatch.setattr(srv, "_full_check_gate", gate)
    await srv._attempt_down_offbus_recheck()
    assert calls[:2] == ["due", "read"] and gated == [True]


@pytest.mark.asyncio
async def test_the_hold_deadline_escalation_does_not_count_a_lost_chip_that_is_back(monkeypatch, tmp_path):
    """A lost chip back on the bus is not off the bus to the hold-deadline escalation: it climbs the
    present-mesh ladder, not the forced off-bus one (bridge reset, tray re-power, host rungs)."""
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_fault_reported", "job 1 eth/fabric fault", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    dev = tmp_path / "tenstorrent"
    dev.mkdir()
    for i in range(32):
        (dev / str(i)).mkdir()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: present)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)
    monkeypatch.setattr(srv, "device_op_lock", None)
    calls = _all_ones_unless_probed(monkeypatch, 32)
    climbed: list = []

    async def present(indices, expected, log, *, force=False, **_):
        climbed.append("present")

    async def offbus(indices, expected, log, *, force=False):
        climbed.append("offbus")

    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_stuck_hold", present)
    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_offbus_stuck_hold", offbus)
    await srv._force_escalate_stuck_hold()
    assert "due" in calls and climbed == ["present"], (calls, climbed)


@pytest.mark.asyncio
async def test_each_tenant_decision_takes_the_probing_verdict(monkeypatch):
    """Admission, exec and the Slurm step verdict call _device_degraded_for_decision, not the polled one."""
    from tests.test_device_safety import _free_device_lock

    seen: list = []
    monkeypatch.setattr(srv, "_device_degraded_for_decision", lambda: seen.append("decision") or "")
    monkeypatch.setattr(srv, "_device_degraded_for_tenant", lambda: seen.append("poll") or "")

    srv._slurm_step_verdict(require_free=False)
    assert seen == ["decision"]

    async def clean(_log):
        return None

    monkeypatch.setattr(srv, "_ensure_device_clean_for_next_job", clean)
    monkeypatch.setattr(srv, "device_op_active", "")
    seen.clear()
    assert await srv._await_device_free_for_tenant(None) == ""
    assert seen == ["decision"]

    _free_device_lock(monkeypatch)
    srv.jobs.clear()
    seen.clear()
    mcp = srv.create_mcp_server()
    await mcp.call_tool("tt_device_exec", {"params": {"owner": "tenant", "command": "true"}})
    assert seen[:1] == ["decision"] and "poll" not in seen, seen


# --- stale gate state ----------------------------------------------------------------------------


def _lost_chip_read_on_next_poll(sysfs) -> bool:
    sysfs["reads"].clear()
    heartbeat.read_heartbeats()
    return _reads_of(sysfs, 2) == ["tt_heartbeat"]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["raises", "cancelled"])
async def test_a_reset_step_that_raises_or_is_cancelled_clears_recovering(sysfs, clock, monkeypatch, how):
    """A reset that raises or is cancelled must not leave the chips gated: recovering clears on every exit
    and the post-reset grace opens, so the next read of a lost chip goes to it at once."""
    order: list = []
    _reset_seams(monkeypatch, order, 0)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None, raising=False)
    exc = RuntimeError("boom") if how == "raises" else asyncio.CancelledError()

    async def run(argv, log, owner="[broker]health-gate"):
        order.append(("reset", chip_state.recovering()))
        raise exc

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    sysfs["node"](2, ALL_ONES)
    chip_state.mark_lost(["2"], "test", chip_state.chip_nodes(["2"]))
    clock.now += 1.0
    with pytest.raises(type(exc)):
        await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert ("reset", True) in order
    assert not chip_state.recovering()
    assert json.loads(chip_state.state_path().read_text())["recovering"] is False
    assert _lost_chip_read_on_next_poll(sysfs), "the grace did not open after the step ended"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["raises", "cancelled"])
async def test_the_galaxy_reset_sweep_clears_recovering_on_every_exit(sysfs, clock, monkeypatch, how):
    """The back-to-back sweep holds recovering across all its rungs; a mesh reset that raises (the sweep
    goes on to the host rung) or a cancel mid-sweep clears it and opens the grace."""
    from tt_device_mcp.health.recovery import pcie_guard

    g = srv.galaxy_recovery
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 0)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    seen = []

    async def reset(*a, **k):
        seen.append(chip_state.recovering())
        if how == "cancelled":
            raise asyncio.CancelledError()
        raise RuntimeError("boom")

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", reset)
    sysfs["node"](2, ALL_ONES)
    chip_state.mark_lost(["2"], "test", chip_state.chip_nodes(["2"]))
    clock.now += 1.0
    if how == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await g._issue_all_resets_back_to_back([], N, lambda m: None, do_sbr=False)
    else:
        assert await g._issue_all_resets_back_to_back([], N, lambda m: None, do_sbr=False) is True
    assert seen == [True], "the sweep's mesh reset did not run under recovering"
    assert not chip_state.recovering()
    assert _lost_chip_read_on_next_poll(sysfs)


def test_a_fresh_broker_publishes_clean_state_over_a_stale_file(monkeypatch):
    """A broker that died mid-recovery leaves chip-state.json saying recovering (a per-user daemon's state
    dir outlives the process). The next broker starts with no lost or recovering chip and rewrites the
    file at boot, before any sampler tick."""
    path = chip_state.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "pid": 1, "recovering": True, "lost": ["2"], "chips": {}}))
    chip_state.reset()
    assert not chip_state.recovering() and chip_state.lost_chips() == []
    # Re-run boot_broker's once-only block around the subsystem this suite already built: the fsm hands
    # back the same singletons, and every global it rebinds is restored after the test.
    for name, attr in (
        ("health_monitor", "monitor"),
        ("recovery_mechanism", "mechanism"),
        ("galaxy_recovery", "galaxy"),
        ("per_target_recovery", "per_target"),
        ("select_recovery", "select_recovery"),
        ("sampler", "sampler"),
    ):
        monkeypatch.setattr(srv, name, getattr(srv, name))
        monkeypatch.setattr(srv.fsm, attr, getattr(srv, name), raising=False)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_fence", srv.recovery_mechanism.reset_fence, raising=False)
    monkeypatch.setattr(srv, "_booted", False)
    monkeypatch.setattr(srv.fsm, "boot", lambda deps: None)
    monkeypatch.setattr(srv.fsm, "resolve_platform", lambda probe=None: None)
    srv.boot_broker()
    body = json.loads(path.read_text())
    assert body["pid"] == os.getpid()
    assert body["recovering"] is False and body["lost"] == []


# --- /health --------------------------------------------------------------------------------------


def test_health_reports_per_chip_state_and_reads_nothing_while_recovering(sysfs, monkeypatch, clear_job_state):
    monkeypatch.setattr(srv, "heartbeat_supported", lambda *a: True)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: N + 1)
    monkeypatch.setattr(srv.health_monitor, "_health_check_enabled", lambda: True)
    monkeypatch.setattr(srv, "device_op_active", "")
    fsm_healthy(srv)
    sysfs["node"](2, ALL_ONES)  # present, not answering
    payload = srv._health_payload()
    assert payload["reset_in_flight"] is False and payload["recovering"] is False
    assert payload["off_bus_chips"] == ["2", "4"] and payload["hung_chips"] == ["2"]
    assert payload["lost_chips"] == [] and payload["resetting_chips"] == []

    chip_state.mark_lost(["2"], "test", {"2": sysfs["dir"] / "tenstorrent!2"})
    srv.recovery_mechanism.reset_in_flight = True
    sysfs["reads"].clear()
    payload = srv._health_payload()
    assert sysfs["reads"] == [], "/health read a chip while a reset step owned them"
    assert payload["reset_in_flight"] is True and payload["recovering"] is True
    assert payload["lost_chips"] == ["2"]
    assert payload["resetting_chips"] == [str(i) for i in range(N + 1)]
    assert payload["off_bus_chips"] == ["2", "4"], "the last live picture is served while recovering"
    assert "reset or recovery step" in payload["device_degraded"]
