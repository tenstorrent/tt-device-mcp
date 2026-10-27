# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The broker's own reads of a chip read as dead are fenced, so none holds up its PCI remove (03 I97).

The kernel's PCI remove of a chip waits for every sysfs read of that chip's driver attributes still
in flight, and each telemetry or link-config attribute of a dead chip is a PCIe read that stalls. A
containment stops other processes; the fence stops the broker's own threads: it is set before the
containment's first step and checked before each attribute read, so a snapshot that is mid-chip
stops at its next read. The containment record counts from the first evidence too, on the sampler's
path as on the watch's (I96), and an AER stop with no proof the chip is off the bus kills no reader
while the chip is still attached (I92). No device is touched and no process is spawned: every read,
the remove and every signal are stubbed.
"""

import threading
import time

import pytest

from tests.test_aer_check_stop import _sig
from tests.test_aer_stop_contain import _held, _rows, _sample, _stop_row, watch  # noqa: F401 - the fixture
from tests.test_deadchip_detach_before_kill import FABRIC, valve  # noqa: F401 - the fixture
from tt_device_mcp import server as srv
from tt_device_mcp import telemetry
from tt_device_mcp.health import ALL_ONES, chip_state
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.post_reset import FABRIC_DEBT

# One stalled PCIe read of a dead chip's attribute; a snapshot's eleven of them (eight attributes,
# three link fields) outlast the remove budget, as a snapshot of a dead chip did on a real drop.
READS_PER_CHIP = 11
READ_STALL_SEC = 0.5


def _node(idx):
    (pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}").mkdir(exist_ok=True)


# The stub remove returns once no read of the chip ran for this long: back-to-back reads of one
# snapshot keep it waiting, as on the real drop.
QUIET_SEC = 0.2


class _Reads:
    """A stand-in for pci._read_attr: chip 13's reads stall, with an in-flight count the stub remove
    waits on, as the kernel's remove waits for the sysfs reads of the chip."""

    def __init__(self, stall=READ_STALL_SEC):
        self.stall = stall
        self.cond = threading.Condition()
        self.in_flight = 0
        self.last_end = 0.0
        self.done = {}  # chip -> reads finished
        self.started = threading.Event()

    def __call__(self, path):
        idx = next(p.rsplit("!", 1)[-1] for p in path.parts if p.startswith("tenstorrent!"))
        if idx == "13":
            with self.cond:
                self.in_flight += 1
            self.started.set()
            time.sleep(self.stall)
            with self.cond:
                self.in_flight -= 1
                self.last_end = time.monotonic()
                self.cond.notify_all()
        self.done[idx] = self.done.get(idx, 0) + 1
        return "4294967295" if idx == "13" else "100"

    def remove(self, log):
        def quiet():
            return self.in_flight == 0 and time.monotonic() - self.last_end >= QUIET_SEC

        def isolate(idx):
            deadline = time.monotonic() + telemetry.DETACH_TIMEOUT_SEC - 1
            with self.cond:
                while not quiet() and time.monotonic() < deadline:
                    self.cond.wait(QUIET_SEC / 4)
                drained = quiet()
            log.append(("detach", idx))
            return drained

        return isolate


# --- fence-mid-snapshot ----------------------------------------------------------------------


def _snapshot_during_containment(monkeypatch, log, fenced):
    _node("13")
    reads = _Reads()
    monkeypatch.setattr(pci, "_read_attr", reads)
    monkeypatch.setattr(srv, "isolate_chip", reads.remove(log))
    if not fenced:
        # The control: a snapshot that checked the chip once, at its start, before the drop.
        monkeypatch.setattr(chip_state, "read_blocked", lambda idx: False)
    out = {}
    t = threading.Thread(target=lambda: out.update(pci.chip_snapshot()), daemon=True)
    t.start()
    assert reads.started.wait(5), "the snapshot is reading chip 13 when the containment starts"
    row = srv.sampler.contain_on_thread(["13"], ["13"], "test drop")
    t.join(15)
    assert not t.is_alive()
    return row, reads, out


def test_a_snapshot_mid_chip_stops_at_its_next_read_and_the_remove_is_in_budget(watch, monkeypatch):  # noqa: F811
    _w, log, _detach_ok, _burst = watch
    row, reads, out = _snapshot_during_containment(monkeypatch, log, fenced=True)
    assert row["removed"] is True and row["stuck"] == [], row
    assert row["trigger_to_remove_s"] <= telemetry.WATCH_REMOVE_BUDGET_SEC, row
    assert (
        row["trigger_to_remove_s"] <= READ_STALL_SEC + QUIET_SEC + 0.5
    ), f"only the read in flight was waited for: {row}"
    assert reads.done["13"] == 1, f"no read of chip 13 after the fence: {reads.done}"
    assert out["13"].get("lost") is True, out
    print(f"fenced trigger_to_remove_s={row['trigger_to_remove_s']}")


def test_without_the_fence_the_same_snapshot_holds_the_remove_past_its_budget(watch, monkeypatch):  # noqa: F811
    """The control: the test sees the fault the fence fixes."""
    _w, log, _detach_ok, _burst = watch
    row, reads, _out = _snapshot_during_containment(monkeypatch, log, fenced=False)
    assert reads.done["13"] >= READS_PER_CHIP, reads.done
    assert row["trigger_to_remove_s"] > telemetry.WATCH_REMOVE_BUDGET_SEC, row
    print(f"control trigger_to_remove_s={row['trigger_to_remove_s']}")


# --- fence-per-attribute ---------------------------------------------------------------------


@pytest.mark.parametrize("at", range(1, READS_PER_CHIP))
def test_a_fence_set_mid_snapshot_stops_the_next_attribute_read(monkeypatch, at):
    """Fenced after the ``at``-th of chip 13's eleven reads (attributes, then link config): no further
    read of it, the chip is listed lost, its AER counters (kernel memory) and chip 14 stay read."""
    for i in ("13", "14"):
        _node(i)
    done = {"13": 0, "14": 0}

    def read(path):
        idx = next(p.rsplit("!", 1)[-1] for p in path.parts if p.startswith("tenstorrent!"))
        done[idx] += 1
        if idx == "13" and done[idx] == at:
            chip_state.fence(["13"], "test")
        return "100"

    monkeypatch.setattr(pci, "_read_attr", read)
    dev = pci.SYSFS_CLASS_DIR / "tenstorrent!13" / "device"
    dev.mkdir()
    (dev / "aer_dev_fatal").write_text("TOTAL_ERR_FATAL 3\n")
    out = pci.chip_snapshot()
    assert done["13"] == at, f"read {done['13']} times, fenced after {at}"
    assert out["13"]["lost"] is True, out
    assert out["13"]["aer_fatal"] == 3, "kernel-memory AER counters stay readable"
    assert done["14"] == READS_PER_CHIP and "lost" not in out["14"], "the fence is per chip"


def test_a_fence_ends_with_the_broker_s_own_reset_step(monkeypatch):
    _node("13")
    monkeypatch.setattr(pci, "_read_attr", lambda path: "100")
    chip_state.fence(["13"], "test")
    assert chip_state.read_blocked("13")
    chip_state.set_recovering("op:reset", True)
    assert not chip_state.read_blocked("13"), "the broker's own reset step reads the chips for real"
    chip_state.set_recovering("op:reset", False)
    assert not chip_state.is_fenced("13"), "a reset step ends every fence"
    assert "lost" not in pci.chip_snapshot()["13"]


# --- sampler-path-record ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [None, 3.0])
async def test_the_sampler_path_writes_its_containment_record_from_the_first_evidence(
    valve, monkeypatch, evidence  # noqa: F811
):
    log, _ = valve
    fenced_at_remove = []

    def isolate(idx):
        fenced_at_remove.append(chip_state.read_blocked(idx))
        log.append(("detach", idx))
        return True

    monkeypatch.setattr(srv, "isolate_chip", isolate)
    if evidence is not None:
        chip_state.note_evidence(["13"], "aer_burst", time.time() - evidence)
    await srv.sampler.isolate_dead_chips(["13"])
    rows = _rows("chip_contained")
    assert rows, "the sampler's path writes chip_contained"
    row = rows[-1]
    assert row["path"] == "sampler" and row["chips"] == ["13"] and row["removed"] is True, row
    assert row["first_evidence_to_remove_s"] >= row["trigger_to_remove_s"], row
    assert row["first_evidence_ts"] <= row["trigger_ts"] <= row["sigstop_ts"] <= row["remove_ts"], row
    if evidence is None:
        assert row["first_evidence"] == "trigger" and row["first_evidence_ts"] == row["trigger_ts"], row
    else:
        assert row["first_evidence"] == "aer_burst", row
        assert row["first_evidence_to_remove_s"] >= evidence, row
    assert fenced_at_remove == [True], "no read of the broker's reaches the chip while it is removed"
    detach = log.index(("detach", "13"))
    kills = [i for i, e in enumerate(log) if e[0] in ("kill", "pollers")]
    assert kills and detach < min(kills), log


def test_the_watch_path_record_names_its_path_and_first_evidence(watch):  # noqa: F811
    _w, _log, _detach_ok, burst = watch
    _sample("13")
    assert burst()
    row = _rows("chip_contained")[-1]
    assert row["path"] == "watch" and row["first_evidence"] == "aer_burst", row
    assert row["first_evidence_ts"] <= row["trigger_ts"], row
    assert row["first_evidence_to_remove_s"] >= row["trigger_to_remove_s"], row


# --- i92-no-kill-without-proof ---------------------------------------------------------------


def _poll(w, monkeypatch, sig):
    monkeypatch.setattr(srv, "link_signals", lambda: sig)
    w._clock.t += 1
    return w.poll_once()


def _stopped_only(log):
    assert log and all(e == ("stop", FABRIC) for e in log), f"no SIGKILL while chip 13 is attached: {log}"


def test_a_burst_with_no_proof_keeps_the_readers_stopped_until_the_broker_removed_the_chip(
    watch, monkeypatch  # noqa: F811
):
    w, log, _detach_ok, burst = watch
    assert burst()
    _stopped_only(log)
    for _ in range(3):
        assert _poll(w, monkeypatch, _sig(14)) is None
    _sample(age=5.0)  # a sample from before the stop proves nothing
    assert _poll(w, monkeypatch, _sig(14)) is None
    _stopped_only(log)
    assert w.pending_stop and w.pending_stop["chips"] == ["13"]
    # The broker's own remove took chip 13 out: its node is gone and nothing can reach it.
    srv.isolated_chips.add("13")
    assert _poll(w, monkeypatch, dict(_sig(14), chips=["14"]))
    assert log[-1] == ("kill", FABRIC) and w.pending_stop is None, log
    assert _rows("check_aer_stop_kill")[-1]["reason"].endswith("detached")
    assert not _rows("chip_contained"), "the broker's own remove is no new containment"


def test_a_burst_chip_that_vanishes_while_its_readers_are_stopped_is_contained_before_the_kill(
    watch, monkeypatch  # noqa: F811
):
    w, log, _detach_ok, burst = watch
    assert burst()
    _stopped_only(log)
    assert _poll(w, monkeypatch, dict(_sig(14), chips=["14"]))
    row = _rows("chip_contained")[-1]
    assert row["chips"] == ["13"] and row["vanished"] == ["13"], row
    assert chip_state.is_lost("13"), "a vanished chip is marked lost, never read again"
    kills = [i for i, e in enumerate(log) if e[0] == "kill"]
    assert kills and ("kill", FABRIC) in log, log
    assert w.pending_stop is None


def test_a_burst_chip_read_all_ones_after_the_stop_is_removed_before_the_kill(watch, monkeypatch):  # noqa: F811
    w, log, _detach_ok, burst = watch
    assert burst()
    _stopped_only(log)
    _sample("13")
    assert _poll(w, monkeypatch, _sig(14))
    detach = log.index(("detach", "13"))
    kills = [i for i, e in enumerate(log) if e[0] == "kill"]
    assert kills and detach < min(kills), f"the remove before any kill: {log}"
    assert _rows("chip_contained")[-1]["chips"] == ["13"]
    assert _stop_row()["offbus"] == [] and w.pending_stop is None


def test_a_failed_fence_never_stops_the_containment(watch, monkeypatch):  # noqa: F811
    _w, log, _detach_ok, burst = watch

    def broken(*a, **k):
        raise RuntimeError("fence failed")

    monkeypatch.setattr(chip_state, "fence", broken)
    _sample("13")
    assert burst()
    detach = log.index(("detach", "13"))
    kills = [i for i, e in enumerate(log) if e[0] == "kill"]
    assert kills and detach < min(kills), log
    assert _rows("chip_contained")[-1]["removed"] is True
    assert chip_state.is_lost("13"), "the chip is still marked lost, so no poll reads it"


# A reader the broker starts after an AER stop: a later post-reset or post-job check.
LATER = 5151


class _Proc:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None


def _signalled(log, pid):
    return [e for e in log if e[0] in ("stop", "kill") and e[1] == pid]


@pytest.mark.parametrize("then", ["answering", "detached"])
def test_a_pending_stop_never_signals_a_reader_started_after_its_readers_ended(watch, monkeypatch, then):  # noqa: F811
    """The stopped fabric run ended (its own timeout) and a new check runs: the old stop is dropped,
    and the new run is never stopped or killed when the burst chip answers or is removed (I92)."""
    w, log, _detach_ok, burst = watch
    assert burst()
    _stopped_only(log)
    srv.health_monitor.fabric_check_proc.returncode = -9
    monkeypatch.setattr(srv.health_monitor, "fabric_check_proc", _Proc(LATER))
    if then == "answering":
        _sample(age=-1.0)
        _poll(w, monkeypatch, _sig(14))
    else:
        srv.isolated_chips.add("13")
        _poll(w, monkeypatch, dict(_sig(14), chips=["14"]))
    assert not _signalled(log, LATER), f"a reader started after the stop was signalled: {log}"
    assert w.pending_stop is None
    assert _rows("check_aer_stop_cleared")[-1]["why"] == "the stopped readers ended"
    assert FABRIC_DEBT.owed, "the stopped run is still no verdict: a full pass is owed"


def test_a_pending_stop_kills_only_the_readers_it_stopped(watch, monkeypatch):  # noqa: F811
    w, log, _detach_ok, burst = watch
    assert burst()
    _stopped_only(log)
    monkeypatch.setattr(srv.health_monitor, "snapshot_proc", _Proc(LATER), raising=False)
    assert w.answer()
    assert log[-1] == ("kill", FABRIC) and not _signalled(log, LATER), log
    assert _rows("check_aer_stop_kill")[-1]["stopped"] == [{"pid": FABRIC, "user": "[broker]fabric-check"}]


@pytest.mark.parametrize("step", ["reset_in_flight", "scope_seen"])
def test_a_reset_step_drops_a_pending_stop_and_its_check_is_never_killed_for_it(watch, monkeypatch, step):  # noqa: F811
    w, log, _detach_ok, burst = watch
    assert burst()
    _stopped_only(log)
    owner = srv.recovery_mechanism if step == "reset_in_flight" else srv.sampler
    monkeypatch.setattr(owner, step, True)
    assert _poll(w, monkeypatch, _sig(14)) is None
    assert w.pending_stop is None, "the reset step owns the chips and ends their readers itself"
    assert _rows("check_aer_stop_cleared")[-1]["why"].startswith("a reset or recovery step")
    monkeypatch.setattr(owner, step, False)
    # The post-reset full check runs, with the stopped run's pid still listed.
    monkeypatch.setattr(srv.health_monitor, "snapshot_proc", _Proc(LATER), raising=False)
    w._clock.t += telemetry.POST_STEP_AER_QUIET_SEC + 1
    _sample(age=-1.0)
    for _ in range(int(telemetry.PENDING_STOP_CAP_SEC) + 2):
        assert _poll(w, monkeypatch, _sig(14)) is None
    assert not [e for e in log if e[0] == "kill"] and not _signalled(log, LATER), log


@pytest.mark.parametrize("seen", ["no sample", "ARC not ready", "sample from before the stop"])
def test_a_pending_stop_with_no_proof_ends_in_containment_within_its_cap(watch, monkeypatch, seen):  # noqa: F811
    """Chips that neither answer nor read all-ones: the stop does not wait for the readers' own
    timeouts. At PENDING_STOP_CAP_SEC the still-attached chip is contained, the remove before any
    kill (I96), and the hold stays on its bounded recovery: only a full pass with fabric lifts it."""
    w, log, _detach_ok, burst = watch
    if seen == "sample from before the stop":
        _sample(age=1.0)
    assert burst()
    _stopped_only(log)
    at = w.pending_stop["at"]
    acted = None
    for _ in range(int(telemetry.PENDING_STOP_CAP_SEC / telemetry.LINK_WATCH_SEC) + 1):
        if seen == "ARC not ready":
            srv.sampler.ring.append({"ts": time.time() + 1, "chips": {"13": [None], "14": [100]}})
        acted = _poll(w, monkeypatch, _sig(14))
        if acted:
            break
        _stopped_only(log)
        assert w.pending_stop, "stopped, not killed, before the cap"
    took = w._clock.t - at
    assert acted and "unproven" in acted, acted
    assert telemetry.PENDING_STOP_CAP_SEC <= took <= telemetry.PENDING_STOP_CAP_SEC + telemetry.LINK_WATCH_SEC
    detach = log.index(("detach", "13"))
    kills = [i for i, e in enumerate(log) if e[0] == "kill"]
    assert kills and detach < min(kills) and ("kill", FABRIC) in log, f"the remove before any kill: {log}"
    assert ("detach", "14") not in log
    assert _rows("chip_contained")[-1]["chips"] == ["13"]
    assert w.pending_stop is None and chip_state.is_lost("13")
    assert FABRIC_DEBT.owed
    _held()


def test_the_pending_stop_cap_is_three_fast_samples():
    """Three fast samples after the stop: a chip that answers or reads all-ones shows it by then."""
    assert telemetry.PENDING_STOP_CAP_SEC == 3 * telemetry.FAST_SAMPLE_SEC


def _beat(idx, value):
    node = pci.SYSFS_CLASS_DIR / f"tenstorrent!{idx}"
    node.mkdir(exist_ok=True)
    (node / "tt_heartbeat").write_text(f"{value}\n")


@pytest.mark.parametrize("age", [0.0, chip_state.PROBE_MIN_SEC + 1])
def test_the_pre_reset_scan_never_detaches_a_chip_fenced_on_one_sample(monkeypatch, age):
    """One all-ones sample fences a chip (maybe a blip). The pre-reset scan's off-bus read is a
    decision: it reads the chip for real when due, and never detaches it on the all-ones the fence
    reports without a read (I97)."""
    from tt_device_mcp import pre_reset_scan

    _beat("13", 100)
    _beat("14", 101)
    chip_state.fence(["13"], "read all-ones")
    chip_state._fenced["13"]["read"] -= age
    assert pre_reset_scan._offbus_bound_chips() == []
    if age:
        assert not chip_state.is_fenced("13"), "the real read answered: the fence ends"


def test_the_pre_reset_scan_still_detaches_a_fenced_chip_a_real_read_finds_dead(monkeypatch):
    from tt_device_mcp import pre_reset_scan

    _beat("13", ALL_ONES)
    _beat("14", 101)
    chip_state.fence(["13"], "read all-ones")
    chip_state._fenced["13"]["read"] -= chip_state.PROBE_MIN_SEC + 1
    assert pre_reset_scan._offbus_bound_chips() == ["13"]
    # A chip marked lost (two strikes or a containment) still counts with no new read.
    chip_state._fenced["13"]["read"] = time.monotonic()
    monkeypatch.setattr(chip_state, "is_lost", lambda idx: str(idx) == "13")
    assert pre_reset_scan._offbus_bound_chips() == ["13"]
