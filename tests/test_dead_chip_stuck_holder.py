# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A killed holder stuck on a dead chip fences PCI re-enumeration and escalates in 120 s (spec 04 I77).

Fault class J-dead-chip-stuck-holder: the dead-chip path (spec 03 I19) killed a process that had the
chip mapped, and past the release wait it is still alive, stuck in the kernel (state D or SIGKILL
pending) with the chip's node or BAR referenced. Re-enumerating PCI under it (bridge reset, rescan,
tray re-power, the back-to-back sweep) can stall the host, so none runs. It joins the I70 leftovers:
every reset and dispatch is fenced, it is journaled host_at_risk and paged, and past 120 s the host
rung fires (a power cycle with the chip off the bus). Nothing on the path releases the device. No
test touches a device, a real /proc entry or a real reboot or cycle."""

import os
import threading
import time

import pytest

from tests.test_classac_power_cycle_direct import EXPECTED, rig  # noqa: F401 - the fixture
from tests.test_reaped_leftover import SIGKILL_PENDING, _stage
from tests.test_ubb_tray_map import galaxy_seen  # noqa: F401 - the fixture
from tt_device_mcp import job_reap
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.health.recovery import galaxy, offbus_power_cycle

PID = 6300
CHIP = "3"
BDF = "0000:46:00.0"
BAR = f"/sys/devices/pci0000:40/0000:40:01.1/{BDF}/resource0"
BUDGET = srv.DEAD_CHIP_STUCK_BUDGET_SEC
RETRY = srv.LEFTOVER_HOST_RUNG_RETRY_SEC
KILLED = [{"pid": PID, "uid": 1234}]


def _stage_holder(proc, pid=PID, *, state="D", sigpnd="0" * 16, fds=("/dev/tenstorrent/3",), maps=None):
    """A killed holder outside any job scope; ``fds`` its fd targets (None: fd dir unreadable),
    ``maps`` its mapping paths (None: no maps file, i.e. unreadable)."""
    _stage(proc, pid, job_id="", state=state, sigpnd=sigpnd, wchan="tt_cdev_mmap")
    d = proc / str(pid)
    if fds is not None:
        (d / "fd").mkdir()
        for n, target in enumerate(fds, start=5):
            os.symlink(target, d / "fd" / str(n))
    if maps is not None:
        (d / "maps").write_text(
            "".join(f"7f00000{n}000-7f00000{n}fff rw-s 00000000 00:05 1 {m}\n" for n, m in enumerate(maps))
        )
    return d


@pytest.fixture
def stuck(rig, monkeypatch, tmp_path):  # noqa: F811 - the imported fixture
    """A staged pid stuck in D on chip 3's node, on the rig's 32-chip host with every chip present.
    ``s['pages']`` records the alert hook, ``s['order']`` the rig's rungs, ``s['events']`` the journal."""
    proc = tmp_path / "proc"
    proc.mkdir()
    _stage_holder(proc)
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", str(proc))
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "readopted_scopes", {})
    monkeypatch.setattr(srv, "current_job_id", None)
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "proc_uid", lambda pid: 1234)
    monkeypatch.setattr(srv, "read_proc_starttime", lambda pid: "424242" if (proc / str(pid)).exists() else None)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: BDF if str(idx) == CHIP else None)
    rig["beats"] = {str(i): 100 for i in range(EXPECTED)}
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: [str(i) for i in range(EXPECTED)])
    monkeypatch.setattr(srv.health_monitor, "expected", lambda n=0: EXPECTED)
    monkeypatch.setattr(srv, "_auto_reboot_enabled", lambda: True)
    monkeypatch.setattr(srv.recovery_mechanism, "_reboot_already_attempted", lambda **k: False)
    fired = []
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_pending", lambda: ("power-cycle", 1.0) if fired else None)
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    monkeypatch.setattr(srv, "_clear_device_dirty", lambda **k: rig["order"].append("cleared_dirty"))

    async def reboot(log, reason):
        rig["order"].append("reboot")
        fired.append("reboot")

    async def power_cycle(log, reason):
        rig["order"].append("power_cycle")
        fired.append("power_cycle")

    monkeypatch.setattr(rig["g"], "_auto_reboot_host", reboot)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    pages = []
    s = {"proc": proc, "pages": pages, "order": rig["order"], "events": rig["events"], "rig": rig, "deliver": True}
    monkeypatch.setattr(srv.alert, "alert_argv", lambda: ["page"])
    monkeypatch.setattr(
        srv.alert, "send_alert", lambda e, log=None: (pages.append(e), object())[1] if s["deliver"] else None
    )
    return s


def _events(s, kind):
    return [f for k, f in s["events"] if k == kind]


def _note(s):
    stuck = srv._note_dead_chip_stuck_holders([CHIP], KILLED)
    if srv._leftover_unkillable_mono is not None:  # pinned to a whole second (see the I70 tests)
        srv._leftover_unkillable_mono = float(int(srv._leftover_unkillable_mono))
    return stuck


def _gone(s, pid=PID):
    import shutil

    shutil.rmtree(s["proc"] / str(pid))


async def _tick(now):
    spawned = srv._maybe_spawn_leftover_escalation(now=now)
    if spawned:
        await srv._leftover_rung_task
    return spawned


# --- detection ------------------------------------------------------------------------------------


def test_refs_name_device_fds_node_maps_and_bar_maps(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    _stage_holder(
        proc, fds=("/dev/tenstorrent/3 (deleted)", "/tmp/log"), maps=("/dev/tenstorrent/3", BAR, "/usr/lib/libc.so.6")
    )
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", str(proc))

    refs = job_reap.device_refs(PID, [BDF])
    assert refs == ["fd 5 -> /dev/tenstorrent/3 (deleted)", "map /dev/tenstorrent/3", f"map {BAR}"]


@pytest.mark.parametrize(
    "state,sigpnd,fds,maps,is_stuck",
    [
        ("D", "0" * 16, ("/dev/tenstorrent/3",), None, True),  # D with the node open
        ("D", "0" * 16, (), (BAR,), True),  # D with only the BAR mapped
        ("S", SIGKILL_PENDING, ("/dev/tenstorrent/3",), None, True),  # SIGKILL queued, not acted on
        ("D", "0" * 16, None, None, True),  # refs unreadable: fail closed
        ("S", "0" * 16, ("/dev/tenstorrent/3",), None, False),  # not stuck: it can still exit
        ("D", "0" * 16, ("/tmp/log",), ("/usr/lib/libc.so.6",), False),  # stuck, but on nothing of ours
        ("Z", SIGKILL_PENDING, ("/dev/tenstorrent/3",), None, False),  # a zombie holds nothing
    ],
)
def test_stuck_device_holder(tmp_path, monkeypatch, state, sigpnd, fds, maps, is_stuck):
    proc = tmp_path / "proc"
    proc.mkdir()
    _stage_holder(proc, state=state, sigpnd=sigpnd, fds=fds, maps=maps)
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", str(proc))

    found = job_reap.stuck_device_holder(PID, "dead-chip:3", 1234, [BDF])
    assert (found is not None) is is_stuck
    if found:
        assert found.unkillable and found.job_id == "dead-chip:3"
        assert found.describe().startswith(f"killed holder of chip(s) 3 pid {PID}")


def test_a_gone_pid_is_not_stuck(tmp_path, monkeypatch):
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", str(tmp_path))
    assert job_reap.stuck_device_holder(PID, "dead-chip:3", 1234, [BDF]) is None


def test_a_maps_read_that_blocks_is_bounded_and_fds_still_count(tmp_path, monkeypatch):
    """A D-state process can hold its mmap lock: the maps read gives up at its bound."""
    proc = tmp_path / "proc"
    proc.mkdir()
    _stage_holder(proc, maps=(BAR,))
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", str(proc))
    release = threading.Event()
    real = job_reap._read_leftover
    monkeypatch.setattr(
        job_reap, "_read_leftover", lambda pid, name: release.wait(5) if name == "maps" else real(pid, name)
    )
    try:
        t0 = time.monotonic()
        refs = job_reap.device_refs(PID, [BDF], maps_timeout=0.2)
        assert time.monotonic() - t0 < 2
        assert refs == ["fd 5 -> /dev/tenstorrent/3"]
    finally:
        release.set()


# --- journal, page, I70 episode -------------------------------------------------------------------


def test_a_stuck_holder_is_journaled_host_at_risk_paged_once_and_opens_the_episode(stuck):
    assert _note(stuck) == [PID]

    (ev,) = _events(stuck, "dead_chip_holder_stuck")
    assert ev["host_at_risk"] is True and ev["severity"] == "high"
    assert ev["fault_class"] == "J-dead-chip-stuck-holder" and ev["budget_sec"] == BUDGET
    assert ev["chips"] == [CHIP] and ev["holders"][0]["pid"] == PID and ev["holders"][0]["state"] == "D"
    (page,) = stuck["pages"]
    assert page["kind"] == "dead_chip_holder_stuck" and page["host_at_risk"] is True
    srv._reaped_leftovers()
    srv._reaped_leftovers()
    assert len(stuck["pages"]) == 1, "paged once, not once per scan"
    assert srv._leftover_unkillable_mono is not None
    fenced = srv._health_payload()["leftover_fence"]
    assert fenced["fault_class"] == "J-dead-chip-stuck-holder" and fenced["budget_sec"] == BUDGET
    assert f"killed holder of chip(s) 3 pid {PID}" in fenced["leftovers"]


def test_a_dropped_page_is_sent_on_the_next_scan(stuck):
    stuck["deliver"] = False
    _note(stuck)
    assert stuck["pages"] == []

    stuck["deliver"] = True
    srv._reaped_leftovers()
    srv._reaped_leftovers()
    assert [p["kind"] for p in stuck["pages"]] == ["dead_chip_holder_stuck"]


def test_a_killed_holder_that_exited_or_is_not_stuck_names_nothing(stuck):
    _gone(stuck)
    _stage_holder(stuck["proc"], 6301, state="S")  # alive, but it can still act on its SIGKILL
    killed = KILLED + [{"pid": 6301}, {"pid": None}, "junk"]

    assert srv._note_dead_chip_stuck_holders([CHIP], killed) == []
    assert _events(stuck, "dead_chip_holder_stuck") == [] and stuck["pages"] == []
    assert srv._dead_chip_stuck == {} and srv._dead_chip_stuck_reason() == ""
    assert srv._leftover_unkillable_mono is None


def test_the_bars_are_read_at_registration_not_after_the_removal(stuck, monkeypatch):
    """Once the chip is removed its sysfs link is gone: the BAR mapping is still recognised."""
    _gone(stuck)
    _stage_holder(stuck["proc"], fds=(), maps=(BAR,))
    _note(stuck)
    monkeypatch.setattr(srv, "chip_pci_bdf", lambda idx: "device")  # what a removed chip resolves to

    assert [f.pid for f in srv._reaped_leftovers()] == [PID]
    assert srv._dead_chip_stuck_reason()


# --- the re-enumeration fence ---------------------------------------------------------------------


def test_the_broker_wires_both_fences(stuck):
    _note(stuck)
    reason = srv.recovery_mechanism.reenumerate_fence()
    assert f"pid {PID}" in reason and "re-enumerating PCI" in reason
    assert f"killed holder of chip(s) 3 pid {PID}" in srv.recovery_mechanism.reset_fence()


async def test_the_bridge_rung_is_withheld_and_the_hold_stays_generic(stuck, monkeypatch):
    from tt_device_mcp.health import recovery as recovery_pkg

    _note(stuck)
    srv.isolated_chips = {CHIP}
    srv.device_pci_map = {CHIP: BDF}
    monkeypatch.setattr(
        recovery_pkg, "reset_chip_via_bridge", lambda *a: pytest.fail("bridge reset under a stuck holder")
    )
    g = srv.galaxy_recovery

    assert await g._recover_isolated_chips(lambda m: None) is False
    assert await g._recover_isolated_chips(lambda m: None) is False
    assert srv.isolated_chips == {CHIP}, "the chip stays out"
    assert g.last_bridge_reset_reasons == {CHIP: {"reason": "stuck_holder"}}
    withheld = _events(stuck, "reenumerate_withheld")
    assert len(withheld) == 1 and withheld[0]["host_at_risk"] is True, "journaled once per reason"
    # A stuck-holder window is not the no-window class: the generic branch (and its whole-tray
    # power-cycle hand-off) owns the hold, never the sweep that is itself fenced.
    tray = set(range(24, 32))
    assert galaxy._classify_hold(
        tray, EXPECTED, {c: {"reason": "stuck_holder"} for c in tray}, stuck["rig"]["map"]
    ) == (galaxy.HOLD_CLASS_GENERIC)


async def test_the_tray_down_rescan_is_withheld(stuck, monkeypatch):
    _note(stuck)
    g = srv.galaxy_recovery
    monkeypatch.setattr(galaxy, "_pci_rescan", lambda: pytest.fail("PCI rescan under a stuck holder"))
    monkeypatch.setattr(g, "_tray_down_capture", lambda onset, expected: {})
    g._td = {
        "path": "TRAY_DOWN",
        "onset": {4: 8},
        "off": list(range(24, 32)),
        "epoch": time.time(),
        "rescanned": False,
        "captured": False,
        "prelude_done": False,
    }

    assert await g._tray_down_prelude(EXPECTED, lambda m: None, phase="gate") is False
    assert g._td["rescanned"] is False and g._td["prelude_done"] is False, "the rescan runs once the holder is gone"


async def test_the_back_to_back_sweep_is_withheld_and_reports_incomplete(stuck, monkeypatch):
    _note(stuck)
    g = srv.galaxy_recovery

    async def no_sbr(log):
        pytest.fail("the sweep's SBR ran under a stuck holder")

    monkeypatch.setattr(g, "_recover_isolated_chips", no_sbr)
    monkeypatch.setattr(galaxy.pcie_guard, "host_reset_gate", lambda off: pytest.fail("the sweep got past its fence"))
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda *a, **k: pytest.fail("a tray re-power under a stuck holder"))
    assert await type(g)._issue_all_resets_back_to_back(g, 0b1000, [3], [4], 4, lambda m: None) is False


def _ubb_rung(*a):
    """The real rung: the rig stubs the instance's."""
    g = srv.galaxy_recovery
    return type(g)._attempt_ubb_tray_reset(g, *a, lambda m: None)


@pytest.fixture
def tray_rung(stuck, galaxy_seen, monkeypatch):  # noqa: F811 - the imported fixture
    srv.fsm.set_latch("ubb_reset_fired", False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
    fired = []
    monkeypatch.setattr(galaxy, "_fire_ubb_reset", lambda bitmap, ids: fired.append((bitmap, ids)), raising=False)
    _note(stuck)
    return fired


async def test_the_per_tray_walk_is_withheld(tray_rung, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_UBB_SINGLE_TRAY_WALK", "1")
    beats = {str(i): 100 for i in range(32) if i != 24}

    assert await _ubb_rung(beats, 1, 32) is None
    assert tray_rung == []


async def test_a_whole_tray_still_goes_to_the_power_cycle_hand_off(tray_rung, monkeypatch):
    """The whole-tray hand-off re-powers nothing and is the rung that clears the holder: not fenced."""
    noted = []
    monkeypatch.setattr(offbus_power_cycle, "note_whole_tray_down", lambda ep, detail: noted.append(detail))
    monkeypatch.setattr(srv.galaxy_recovery.deps, "device_hold_episode_since", lambda: "ep-1")
    beats = {str(i): 100 for i in range(24)}

    assert await _ubb_rung(beats, 8, 32) is False
    assert noted and tray_rung == []


async def test_reset_with_quiesce_is_fenced_until_the_holder_is_gone(stuck, monkeypatch):
    _note(stuck)
    ran = []

    async def run(argv, log, owner="[broker]health-gate", on_output=None):
        ran.append(argv)
        return 0, ""

    async def pollers(active, log):
        return []

    async def no_rescan(fn, *a, **k):
        return None

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.asyncio, "to_thread", no_rescan)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)

    rc, out = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None, owner="manual")
    assert rc is None and "reset refused" in out and f"pid {PID}" in out and not ran
    _gone(stuck)
    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r"], lambda m: None)
    assert rc == 0 and ran == [["tt-smi", "-r"]]


def test_a_released_holder_is_pruned_and_lifts_the_fence(stuck):
    _note(stuck)
    assert srv._dead_chip_stuck_reason()
    _gone(stuck)

    assert srv._reaped_leftovers() == []
    assert srv._dead_chip_stuck == {} and srv._dead_chip_stuck_reason() == ""
    (released,) = _events(stuck, "dead_chip_holder_released")
    assert released["pid"] == PID and released["label"] == "dead-chip:3"
    assert srv._leftover_unkillable_mono is None and srv._health_payload()["leftover_fence"] is None
    assert srv.recovery_mechanism.reenumerate_fence() == ""


def test_a_reused_pid_is_not_the_stuck_holder(stuck, monkeypatch):
    _note(stuck)
    monkeypatch.setattr(srv, "read_proc_starttime", lambda pid: "999999")
    assert srv._reaped_leftovers() == [] and srv._dead_chip_stuck == {}


# --- I70 escalation on the 120 s budget -----------------------------------------------------------


async def test_no_rung_before_120s_then_a_power_cycle_with_the_chip_off_the_bus(stuck):
    stuck["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i != int(CHIP)}  # the removed chip
    _note(stuck)
    t0 = srv._leftover_unkillable_mono

    assert not await _tick(t0 + BUDGET - 1)
    assert stuck["order"] == []
    assert await _tick(t0 + BUDGET)
    assert stuck["order"] == ["power_cycle"]
    (deadline,) = _events(stuck, "leftover_unkillable_deadline")
    assert deadline["fault_class"] == "J-dead-chip-stuck-holder" and deadline["budget_sec"] == BUDGET
    (rung,) = _events(stuck, "leftover_host_rung")
    assert rung["phase"] == "requested" and rung["detail"] == f"power-cycle (1/{EXPECTED} chip(s) off the bus)"


async def test_the_rung_counts_a_stuck_holder_the_scan_misses_and_fires_nothing_once_it_left(stuck, monkeypatch):
    """The I70 rung's fresh-scan 'gone' check (stage ``gone``) reads the I77 registry too: the holder
    scan lists no holder here (a removed chip's BAR or node), so the rung fires only while the
    holder's own check still finds it stuck. One that exits during the stand-back fires nothing."""
    stuck["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i != int(CHIP)}
    _note(stuck)
    t0 = srv._leftover_unkillable_mono
    monkeypatch.setattr(galaxy.offbus_power_cycle, "standback_reason", lambda **k: _gone(stuck) or "")

    assert await _tick(t0 + BUDGET)
    assert stuck["order"] == []
    (rung,) = _events(stuck, "leftover_host_rung")
    assert rung["phase"] == "gone"
    assert srv._leftover_unkillable_mono is None and srv._dead_chip_stuck == {}


async def test_the_i65_cooldown_holds_the_rung_and_it_fires_once_the_cooldown_ends(stuck):
    stuck["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i != int(CHIP)}
    stuck["rig"]["allowed"] = (False, "I65 cooldown until 15:26")
    _note(stuck)
    t0 = srv._leftover_unkillable_mono

    assert await _tick(t0 + BUDGET)
    assert stuck["order"] == []
    fenced = srv._health_payload()["leftover_fence"]
    assert fenced["escalation_stage"] == "held" and "I65 cooldown" in fenced["detail"]
    stuck["rig"]["allowed"] = (True, "")
    assert await _tick(t0 + BUDGET + RETRY)
    assert stuck["order"] == ["power_cycle"]


async def test_time_to_request_is_bounded_and_nothing_releases_the_device(stuck):
    """Fake-clock TTR: from the note, the sampler ticks every second; the request comes at 120 s,
    and nothing on the path releases or clears the device (the boot's startup gate does that)."""
    stuck["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i != int(CHIP)}
    _note(stuck)
    t0 = srv._leftover_unkillable_mono
    requested_at = None
    for s in range(0, BUDGET + RETRY + 1):
        await _tick(t0 + s)
        if stuck["order"] and requested_at is None:
            requested_at = s
    assert requested_at == BUDGET
    assert stuck["order"] == ["power_cycle"]
    assert "released" not in stuck["order"] and "cleared_dirty" not in stuck["order"]
    assert srv._leftover_unkillable_mono == t0


def test_a_job_leftover_alone_keeps_the_600s_budget(stuck):
    assert srv._leftover_budget_sec() == srv.LEFTOVER_UNKILLABLE_BUDGET_SEC
    assert srv._leftover_fault_class() == "J-unkillable-leftover"



def _scan():
    from tt_device_mcp.device_holders import DeviceHolder

    return HolderScan(holders=[DeviceHolder(pid=PID, uid=1234)], complete=True)


@pytest.mark.parametrize("where", ["job-scope", "reaped-survivor"])
def test_a_dead_chip_holder_that_is_also_a_job_leftover_keeps_the_dead_chip_label(stuck, monkeypatch, where):
    """The killed holder sits in an ended job's ttdev-job scope, or the sweep named it that job's
    survivor: the I77 entry wins, so the episode keeps the 120 s budget, its class and the
    re-enumeration fence instead of the job leftover's 600 s."""
    _note(stuck)
    if where == "job-scope":
        (stuck["proc"] / str(PID) / "cgroup").write_text("0::/system.slice/ttdev-job-7.scope\n")
    else:
        monkeypatch.setattr(srv, "reaped_survivors", {PID: ("7", "424242")})

    (f,) = srv._reaped_leftovers(_scan())
    assert f.job_id == "dead-chip:3" and f.unkillable
    assert srv._leftover_budget_sec() == BUDGET
    assert srv._leftover_fault_class() == "J-dead-chip-stuck-holder"
    assert srv._dead_chip_stuck_reason()


def _running_job(monkeypatch, job_id="41"):
    job = srv.Job(id=job_id, owner="tenant", workspace="/tmp", command="x", queued_at="t")
    job.status = srv.JobStatus.RUNNING
    monkeypatch.setattr(srv, "jobs", {job_id: job})
    monkeypatch.setattr(srv, "reset_killed_job_ids", set())
    return job


async def test_the_dead_chip_victim_job_still_running_does_not_hold_the_rung(stuck, monkeypatch):
    """The dead-chip path SIGKILLed the running job's process and marked the job its own kill; the
    process is stuck in D, so the job stays RUNNING. That victim is not a running job to the I70
    rung's guard: the request still comes at 120 s (one sampler tick at most), not never."""
    stuck["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i != int(CHIP)}
    job = _running_job(monkeypatch)
    srv.reset_killed_job_ids.add(job.id)  # what _kill_device_holders' victim marking does
    _note(stuck)
    t0 = srv._leftover_unkillable_mono
    requested_at = None
    for s in range(0, BUDGET + 2):
        await _tick(t0 + s)
        if stuck["order"] and requested_at is None:
            requested_at = s
    assert requested_at is not None and requested_at <= BUDGET + 1
    assert stuck["order"] == ["power_cycle"]
    assert not any(r["phase"] == "held" for r in _events(stuck, "leftover_host_rung"))


async def test_a_live_job_the_broker_did_not_kill_still_holds_the_rung(stuck, monkeypatch):
    stuck["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i != int(CHIP)}
    _running_job(monkeypatch)
    _note(stuck)
    t0 = srv._leftover_unkillable_mono

    assert await _tick(t0 + BUDGET)
    assert stuck["order"] == []
    (rung,) = _events(stuck, "leftover_host_rung")
    assert rung["phase"] == "held" and "a broker job is running" in rung["detail"]


# --- a broker restart keeps the registry and the episode clock ------------------------------------


def _restart(monkeypatch, elapsed=0.0, **record):
    """Simulate a broker restart ``elapsed`` seconds after the note: rewrite the record's fields, then
    drop every in-memory I77/I70 fact the new process would not have."""
    import json

    path = srv.health_dir() / srv.DEAD_CHIP_STUCK_FILE
    saved = json.loads(path.read_text())
    saved["since"] -= elapsed
    saved.update(record)
    path.write_text(json.dumps(saved))
    for name, value in (
        ("_dead_chip_stuck", {}),
        ("_leftover_unkillable_since", None),
        ("_leftover_unkillable_mono", None),
        ("_leftover_dead_chip", False),
        ("_leftover_fence_detail", ""),
    ):
        monkeypatch.setattr(srv, name, value)
    return path


def test_the_registry_is_recorded_and_removed_once_the_holder_is_gone(stuck):
    import json

    _note(stuck)
    path = srv.health_dir() / srv.DEAD_CHIP_STUCK_FILE
    saved = json.loads(path.read_text())
    assert saved["boot_id"] == srv._current_boot_id()
    assert saved["since"] == srv._leftover_unkillable_since
    assert saved["holders"] == [{"pid": PID, "label": "dead-chip:3", "starttime": "424242", "uid": 1234, "bdfs": [BDF]}]
    _gone(stuck)
    srv._reaped_leftovers()
    assert not path.exists()


async def test_a_restart_keeps_the_fence_and_resumes_the_120s_clock(stuck, monkeypatch):
    stuck["rig"]["beats"] = {str(i): 100 for i in range(EXPECTED) if i != int(CHIP)}
    _note(stuck)
    _restart(monkeypatch, elapsed=100)

    assert srv._restore_dead_chip_stuck() == [PID]
    assert srv._dead_chip_stuck[PID] == ("dead-chip:3", "424242", 1234, (BDF,))
    assert srv._dead_chip_stuck_reason() and srv.recovery_mechanism.reenumerate_fence()
    assert srv._leftover_budget_sec() == BUDGET
    assert srv._leftover_fault_class() == "J-dead-chip-stuck-holder"
    (restored,) = _events(stuck, "dead_chip_holder_stuck_restored")
    assert restored["pids"] == [PID]
    t0 = srv._leftover_unkillable_mono
    assert abs((time.monotonic() - t0) - 100) < 5  # the clock resumed, not restarted
    assert not await _tick(t0 + BUDGET - 1)
    assert await _tick(t0 + BUDGET)
    assert stuck["order"] == ["power_cycle"]


@pytest.mark.parametrize("change", ["reused-pid", "other-boot", "no-starttime"])
def test_a_restart_drops_holders_that_are_not_the_recorded_process(stuck, monkeypatch, change):
    _note(stuck)
    if change == "other-boot":
        path = _restart(monkeypatch, boot_id="another-boot")
    elif change == "no-starttime":
        path = _restart(monkeypatch, holders=[{"pid": PID, "label": "dead-chip:3", "starttime": "", "uid": 1234}])
    else:
        path = _restart(monkeypatch)
        monkeypatch.setattr(srv, "read_proc_starttime", lambda pid: "999999")

    assert srv._restore_dead_chip_stuck() == []
    assert srv._dead_chip_stuck == {} and srv._leftover_unkillable_mono is None
    assert srv._dead_chip_stuck_reason() == "" and not path.exists()


def test_an_unreadable_record_restores_nothing(stuck):
    path = srv.health_dir() / srv.DEAD_CHIP_STUCK_FILE
    path.write_text("{not json")
    assert srv._restore_dead_chip_stuck() == []
    assert srv._dead_chip_stuck == {} and not path.exists()


async def test_the_broker_start_restores_before_anything_else(stuck, monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "_startup_tasks_done", False)
    monkeypatch.setattr(srv, "_restore_dead_chip_stuck", lambda: calls.append("restore") or [])
    monkeypatch.setattr(srv, "journal_interrupted_fabric_check", lambda: calls.append("journal") or None)
    monkeypatch.setattr(srv, "should_privsep", lambda: (_ for _ in ()).throw(RuntimeError("stop here")))
    with pytest.raises(RuntimeError, match="stop here"):
        await srv.run_startup_tasks()
    assert calls == ["restore", "journal"]

# --- the dead-chip path calls it -------------------------------------------------------------------


async def test_the_dead_chip_path_notes_stuck_holders_and_says_the_host_is_still_at_risk(monkeypatch):
    import logging

    from tests.test_holder_release_wait import _dead_chip_harness

    monkeypatch.setattr(srv, "HOLDER_RELEASE_WAIT_SEC", 0.2)
    order, _ = _dead_chip_harness(monkeypatch, holding_scans=None)
    noted = []
    monkeypatch.setattr(
        srv, "_note_dead_chip_stuck_holders", lambda chips, killed: noted.append((chips, killed)) or [4242]
    )
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log = logging.getLogger("t1121-dead-chip")
    log.addHandler(Capture())
    monkeypatch.setattr(srv.sampler._deps, "logger", lambda: log)

    await srv.sampler.isolate_dead_chips(["13"])
    await srv.sampler.settle_isolations()

    assert noted and noted[0][0] == ["13"] and [k["pid"] for k in noted[0][1]] == [4242]
    assert ("remove", "13") in order
    text = "\n".join(lines)
    assert "STILL at risk" in text and "out of danger" not in text
