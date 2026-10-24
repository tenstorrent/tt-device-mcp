# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I89: on a multi-tray mesh the reset a stopped job owes (I69) first waits, bounded, until
every process of the job is gone and nothing outside the broker holds a device node, then lets the
mesh settle and reads every chip. One ``post_kill_settle`` event names the route and the waits.

The gate tests run the real probe pass on the kill-reset tests' 32-chip mesh (leaf probes stubbed)."""

import asyncio
import os
import platform
import re
import signal
import subprocess
import sys
import time

import pytest

from tests.conftest import patch_health_event
from tests.test_eth_timeout_reset import ADVANCING, mesh  # noqa: F401 (fixture)
from tests.test_job_kill_reset import killed  # noqa: F401 (fixture)
from tt_device_mcp import constants, device_holders, job_reap
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors.heartbeat import ALL_ONES
from tt_device_mcp.job_reap import JOB_TAG_ENV

CHIPS = 32
# The one line each post-kill reset logs; nothing else in the broker uses the name.
LINE_RE = re.compile(r"\bpost_kill_settle: route (mesh_reset|sbr|power_cycle|holder_left|own_rung);")
_SYS_EXIT = {"x86_64": 60, "aarch64": 93}.get(platform.machine())
FAKE_PID = 4_000_000  # above any pid_max: never a real process, so nothing real is read or signalled


@pytest.fixture
def events(monkeypatch, mesh):  # noqa: F811
    """Every health event the gate and the ladder send, with its fields."""
    rows = []

    def record(name, *a, **k):
        mesh["events"].append(name)
        rows.append((name, k))

    patch_health_event(monkeypatch, record)
    return rows


def _settle(rows) -> dict:
    found = [k for name, k in rows if name == "post_kill_settle"]
    assert len(found) == 1, f"one post_kill_settle event per post-kill reset: {found}"
    return found[0]


async def _gate():
    await srv._device_health_gate(None, phase="post-job", run_fabric=False)


def _spawn(*argv, **kw) -> subprocess.Popen:
    return subprocess.Popen(list(argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def _record_reset_state(monkeypatch, proc) -> list:
    """The run state of ``proc`` each time the gate's reset runs."""
    seen = []
    mech = srv.galaxy_recovery.mechanism
    inner = mech.reset_with_quiesce

    async def reset(argv, log, *a, **k):
        seen.append(srv._leftover_state(proc.pid)[0])
        return await inner(argv, log, *a, **k)

    monkeypatch.setattr(mech, "reset_with_quiesce", reset)
    return seen


def _real_proc(monkeypatch):
    """The suite points the leftover reads at an empty dir; these tests read their own children."""
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", "/proc")


def _holders(monkeypatch, *pids):
    monkeypatch.setattr(
        srv, "enumerate_device_holders", lambda: HolderScan(holders=[DeviceHolder(pid=p, uid=0) for p in pids])
    )


class _Log:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)


# ---------------------------------------------------------------- the wait for the job


@pytest.mark.asyncio
async def test_the_reset_waits_for_the_jobs_processes_to_exit(killed, events, monkeypatch):  # noqa: F811
    """A job process still running when the gate starts holds the reset back until it exits; its
    zombie (not yet reaped by its parent) no longer does."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 10)
    _real_proc(monkeypatch)
    proc = _spawn("sleep", "30")
    try:
        start = srv._leftover_state(proc.pid)[1]
        srv._note_kill_reset_job({"survivors": [(proc.pid, start)], "exited_at": srv.time.monotonic()})
        at_reset = _record_reset_state(monkeypatch, proc)
        asyncio.get_running_loop().call_later(0.4, proc.kill)
        await _gate()
    finally:
        proc.kill()
        proc.wait()
    assert at_reset and at_reset[0] in ("Z", "X", "?"), f"the reset ran while the job process lived: {at_reset}"
    ev = _settle(events)
    assert ev["route"] == "mesh_reset" and ev["killed"] == [] and ev["job_left"] == []
    assert ev["reap_wait_sec"] >= 0.3, ev
    assert killed["resets"] == [["tt-smi", "-glx_reset"]]


# ------------------------------------------- zombies and gaps on the driver route (spec 04 I92 rules)
#
# These read tt-kmd's pids file (staged), /proc and the tests' own children for real. The broker runs
# as root, which reads an exited process's fd table as empty: the driver-named pid is a scan gap
# ("holds no device fd here"). An unprivileged scan gets EACCES there and attributes the pid instead.
# ``_view`` pins which of the two the fd read gives a zombie, whoever runs the suite; everything else is
# the real scan. The /proc walk for the file open elsewhere sees only the test's own processes.


def _driver_route(monkeypatch, tmp_path, *pids) -> str:
    """Stage tt-kmd's pids file naming ``pids`` and the device node; returns the node's path."""
    (tmp_path / "driver" / "0").mkdir(parents=True)
    (tmp_path / "driver" / "0" / "pids").write_text("".join(f"{p}\n" for p in pids))
    (tmp_path / "dev").mkdir(exist_ok=True)
    (tmp_path / "dev" / "0").touch()
    monkeypatch.setattr(device_holders, "DRIVER_PROC_DIR", str(tmp_path / "driver"))
    monkeypatch.setattr(device_holders, "DEVICE_DIR", str(tmp_path / "dev"))
    monkeypatch.setattr(srv, "enumerate_device_holders", device_holders.enumerate_device_holders)
    return str(tmp_path / "dev" / "0")


def _driver_route_names(tmp_path, *pids) -> None:
    (tmp_path / "driver" / "0" / "pids").write_text("".join(f"{p}\n" for p in pids))


def _view(monkeypatch, as_root: bool) -> None:
    """What the fd read of a zombie gives: root reads it empty, an unprivileged scan gets EACCES."""
    real = device_holders._process_holds_device

    def holds(pid):
        if device_holders.holder_state(pid) not in ("Z", "X"):
            return real(pid)
        if as_root:
            return False
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(device_holders, "_process_holds_device", holds)


def _walk_only(monkeypatch, *pids) -> None:
    monkeypatch.setattr(device_holders, "_iter_pids", lambda: [os.getpid(), *pids])


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _zombie(*argv, kill=True, **kw) -> subprocess.Popen:
    """A child that prints its line, then (``kill``) is SIGKILLed; left unreaped: a zombie of this process."""
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, text=True, **kw)
    proc.line = proc.stdout.readline().strip()
    if not proc.line.startswith("ready"):
        proc.kill()
        proc.wait()
        raise AssertionError(f"child did not start: {proc.line!r}")
    if kill:
        os.kill(proc.pid, signal.SIGKILL)
    assert _until(lambda: device_holders.holder_state(proc.pid) == "Z"), "a zombie"
    return proc


def _reap(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_an_exited_zombie_the_driver_still_names_ends_the_wait_at_once(monkeypatch, tmp_path):
    """Root's view: the zombie is a scan gap, ruled exited with nothing open elsewhere, so the wait
    ends on the first look and the settle runs; the 30 s bound is not paid."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 5)
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 0.2)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(CHIPS)})
    _view(monkeypatch, as_root=True)
    proc = _zombie(sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)")
    try:
        _driver_route(monkeypatch, tmp_path, proc.pid)
        _walk_only(monkeypatch, proc.pid)
        scan = device_holders.enumerate_device_holders()
        assert scan.holders == [] and scan.gap_pids == [proc.pid], "root's shape: a gap, not a holder"
        q = await srv._post_kill_quiesce(CHIPS, _Log())
    finally:
        _reap(proc)
    assert q["reap_wait_sec"] < 1.0 and q["holders"] == [] and q["gaps"] == [], q
    assert q["settle_sec"] == 0.2 and q["chips"], "the settle ran and every chip was read"
    assert srv._post_kill_route(q, CHIPS, True) == "mesh_reset"


@pytest.mark.skipif(_SYS_EXIT is None, reason="raw exit syscall number known for x86_64 and aarch64 only")
@pytest.mark.asyncio
async def test_a_zombie_leader_with_a_live_thread_holding_the_node_is_a_holder(
    killed, events, monkeypatch, tmp_path  # noqa: F811
):
    """The leader exited, a thread still holds the node: a holder in the post_kill_settle event, with
    its verdict, and the episode routes holder_left with no settle (the reset's own scan rules next)."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 5)
    _view(monkeypatch, as_root=True)
    node = str(tmp_path / "dev" / "0")
    child = (
        "import ctypes, threading, time\n"
        f"fd = open({node!r})\n"
        "threading.Thread(target=time.sleep, args=(60,)).start()\n"
        "print('ready', flush=True)\n"
        f"ctypes.CDLL(None).syscall({_SYS_EXIT}, 0)\n"  # only this thread exits; the leader turns zombie
    )
    _driver_route(monkeypatch, tmp_path)
    proc = _zombie(sys.executable, "-c", child, kill=False)
    try:
        _driver_route_names(tmp_path, proc.pid)
        assert _until(lambda: (z := device_holders.zombie_info(proc.pid)) is not None and z.verdict == "holds")
        await _gate()
    finally:
        _reap(proc)
    ev = _settle(events)
    assert ev["route"] == "holder_left" and ev["settle_sec"] == 0.0, ev
    assert [(h["pid"], h["verdict"]) for h in ev["holders"]] == [(proc.pid, "holds")], ev["holders"]


@pytest.mark.asyncio
async def test_an_unattributed_live_gap_pid_is_reported_and_never_signalled(monkeypatch, tmp_path):
    """A live pid the driver names that shows no device fd here is a holder (verdict live), not quiet:
    no settle, route holder_left, and it is never signalled."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 5)
    proc = _spawn("sleep", "30")
    try:
        _driver_route(monkeypatch, tmp_path, proc.pid)
        assert device_holders.enumerate_device_holders().gap_pids == [proc.pid]
        q = await srv._post_kill_quiesce(CHIPS, _Log())
        assert proc.poll() is None, "a holder that is not the job's is never signalled"
    finally:
        proc.kill()
        proc.wait()
    assert [(h["pid"], h["verdict"]) for h in q["holders"]] == [(proc.pid, "live")], q
    assert "holds no device fd here" in q["holders"][0]["why"]
    assert q["settle_sec"] == 0.0 and srv._post_kill_route(q, CHIPS, True) == "holder_left"


@pytest.mark.parametrize("as_root", [True, False], ids=["root-gap", "unprivileged-holder"])
@pytest.mark.asyncio
async def test_an_exited_zombie_whose_node_is_open_in_a_forked_child_still_holds(monkeypatch, tmp_path, as_root):
    """The pre-reset scan's rule, not a weaker copy: an exited zombie whose file a forked child still
    holds is a holder (verdict open_elsewhere), whether the scan saw it as a gap or a holder."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 5)
    _view(monkeypatch, as_root=as_root)
    node = _driver_route(monkeypatch, tmp_path)
    child = (
        "import os, sys, time\n"
        f"fd = open({node!r})\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    sys.stdin.read()\n"  # the forked child keeps the node open until the test closes stdin
        "    os._exit(0)\n"
        "print(f'ready {pid}', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = _zombie(sys.executable, "-c", child, stdin=subprocess.PIPE)
    forked = int(proc.line.split()[1])
    try:
        _driver_route_names(tmp_path, proc.pid)
        _walk_only(monkeypatch, proc.pid, forked)
        q = await srv._post_kill_quiesce(CHIPS, _Log())
    finally:
        proc.stdin.close()
        _reap(proc)
    assert [(h["pid"], h["verdict"]) for h in q["holders"]] == [(proc.pid, "open_elsewhere")], q
    assert q["settle_sec"] == 0.0 and srv._post_kill_route(q, CHIPS, True) == "holder_left"


@pytest.mark.asyncio
async def test_a_live_holder_past_the_bound_is_never_signalled(monkeypatch):
    """A holder that is not the job's is left to the reset's own holder scan: no signal, no settle."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 5)
    _holders(monkeypatch, FAKE_PID)
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: sent.append((pid, sig)) if pid == FAKE_PID else real_kill(pid, sig)
    )
    q = await srv._post_kill_quiesce(CHIPS, _Log())
    assert sent == [] and q["killed"] == []
    assert q["reap_wait_sec"] >= 0.2 and q["settle_sec"] == 0.0, q
    assert srv._post_kill_route(q, CHIPS, True) == "holder_left"


@pytest.mark.asyncio
async def test_a_live_holder_past_the_bound_keeps_the_hold(
    killed, events, monkeypatch, armed_power_cycle  # noqa: F811
):
    """With a holder still there, the episode goes on through the owed-reset path; the gate never
    releases a device whose post-kill check has not passed."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    _holders(monkeypatch, FAKE_PID)
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: sent.append((pid, sig)) if pid == FAKE_PID else real_kill(pid, sig)
    )
    killed["reset_heals"] = False
    await _gate()
    assert _settle(events)["route"] == "holder_left"
    assert srv.fsm.state is not ServerState.HEALTHY, "never released without a passing full check"


@pytest.mark.asyncio
async def test_a_setsid_child_that_ignores_term_is_killed_at_the_bound_and_logged(monkeypatch):
    """A job child that left the job's session and ignores SIGTERM is found by the job's tag, and at the
    bound gets SIGKILL, logged with its pid and cmdline, before the reset runs."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.3)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(CHIPS)})
    _holders(monkeypatch)
    _real_proc(monkeypatch)
    tag = f"post-kill-test-{os.getpid()}"
    proc = _spawn(
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        start_new_session=True,
        env={**os.environ, JOB_TAG_ENV: tag},
    )
    try:
        for _ in range(100):  # the tag is readable once the interpreter has started
            if proc.pid in srv.job_processes(None, None, tag):
                break
            await asyncio.sleep(0.02)
        srv._owe_kill_reset("job 8 preempted")
        srv._note_kill_reset_job({"tag": tag, "exited_at": srv.time.monotonic()})
        log = _Log()
        q = await srv._post_kill_quiesce(CHIPS, log)
        assert proc.wait(timeout=5) == -signal.SIGKILL
    finally:
        proc.kill()
        proc.wait()
        srv._clear_kill_reset_debt()
    assert [k["pid"] for k in q["killed"]] == [proc.pid] and "time.sleep" in q["killed"][0]["cmdline"]
    assert any(f"pid {proc.pid}" in line and "SIGKILL" in line for line in log.lines), log.lines
    assert q["job_left"] == [] and q["settle_sec"] == 0.0  # the zeroed default settle ran
    assert srv._post_kill_route(q, CHIPS, True) == "mesh_reset"


# ---------------------------------------------------------------- the settle and the chip read


@pytest.mark.asyncio
async def test_the_settle_delay_is_applied_before_the_reset(killed, events, monkeypatch):  # noqa: F811
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 0.3)
    loop = asyncio.get_running_loop()
    start = loop.time()
    mech = srv.galaxy_recovery.mechanism
    inner = mech.reset_with_quiesce
    reset_at = []

    async def reset(argv, log, *a, **k):
        reset_at.append(loop.time() - start)
        return await inner(argv, log, *a, **k)

    monkeypatch.setattr(mech, "reset_with_quiesce", reset)
    await _gate()
    assert _settle(events)["settle_sec"] == 0.3
    assert reset_at and reset_at[0] >= 0.3, reset_at


def test_the_defaults_wait_and_settle_inside_the_ladder_bound():
    """About 30 s for the job, 10 s to settle, and the SIGKILL wait at the bound: all inside the
    10-min ladder mark, ahead of the pre-reset reserve."""
    assert constants.POST_KILL_REAP_WAIT_SEC == 30 and constants.POST_KILL_SETTLE_SEC == 10
    used = constants.POST_KILL_REAP_WAIT_SEC + constants.SURVIVOR_KILL_WAIT_SEC + constants.POST_KILL_SETTLE_SEC
    assert used + constants.pre_reset_zombie_ladder_reserve_sec(32) < 600
    assert constants.SURVIVOR_TERM_GRACE_SEC >= 10, "SIGTERM gets 10 s before SIGKILL"


@pytest.mark.asyncio
async def test_an_unreadable_chip_routes_to_its_bridge_reset(monkeypatch):
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): (ALL_ONES if i == 5 else 100) for i in range(CHIPS)})
    _holders(monkeypatch)
    q = await srv._post_kill_quiesce(CHIPS, _Log())
    assert q["off"] == ["5"] and q["chips"]["5"] == "off" and q["chips"]["4"] == "ok"
    assert q["whole_trays"] == []
    # The present-mesh reset is due only on a mesh the probe pass read whole; a chip still off leaves
    # that pass unhealthy, so the chip's own rung (bridge reset and rescan) goes first.
    assert srv._post_kill_route(q, CHIPS, False) == "sbr"


@pytest.mark.asyncio
async def test_an_unreadable_chip_takes_its_own_rung_not_the_mesh_reset(killed, events, monkeypatch):  # noqa: F811
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): (ALL_ONES if i == 5 else 100) for i in range(CHIPS)})
    killed["snapshot"] = (False, "31/32 chips")
    await _gate()
    ev = _settle(events)
    assert ev["route"] == "sbr" and ev["sbr_chips"] == ["5"]
    assert "kill_mesh_reset" not in killed["events"], "a chip off the bus is not paid by the present-mesh reset"
    assert srv.fsm.state is not ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_whole_tray_off_routes_to_the_power_cycle(monkeypatch, galaxy_trays):
    tray, chips = next((t, c) for t, c in sorted(galaxy_trays.items()) if c)
    off = {str(c) for c in chips}
    monkeypatch.setattr(
        srv, "read_heartbeats", lambda: {str(i): (ALL_ONES if str(i) in off else 100) for i in range(CHIPS)}
    )
    _holders(monkeypatch)
    q = await srv._post_kill_quiesce(CHIPS, _Log())
    assert q["whole_trays"] == [tray]
    assert srv._post_kill_route(q, CHIPS, False) == "power_cycle"


@pytest.mark.asyncio
async def test_all_chips_off_routes_to_the_power_cycle(monkeypatch):
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): ALL_ONES for i in range(CHIPS)})
    _holders(monkeypatch)
    q = await srv._post_kill_quiesce(CHIPS, _Log())
    assert len(q["off"]) == CHIPS
    assert srv._post_kill_route(q, CHIPS, False) == "power_cycle"


# ---------------------------------------------------------------- the release and the bound


@pytest.mark.asyncio
async def test_release_still_needs_the_full_check_with_fabric(killed, events):  # noqa: F811
    await _gate()
    assert killed["fabric"] == [1], "the fabric pass ran after the reset"
    assert srv.fsm.state is ServerState.HEALTHY
    assert _settle(events)["route"] == "mesh_reset"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["fabric_bad", "fabric_77"])
async def test_a_post_kill_fabric_that_fails_or_cannot_check_keeps_the_hold(killed, events, fault):  # noqa: F811
    killed["reset_heals"] = False if fault == "fabric_bad" else True
    killed[fault] = True
    await _gate()
    assert killed["resets"] and killed["resets"][0] == ["tt-smi", "-glx_reset"]
    assert srv.fsm.state is not ServerState.HEALTHY, "no lift without a just-passed full check"
    assert srv._device_unavailable_for_tenant()


@pytest.mark.asyncio
async def test_a_failed_post_kill_check_still_climbs_to_the_power_cycle(
    killed, events, armed_power_cycle, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 0.05)
    killed["reset_heals"] = False
    srv.recovery_mechanism.last_reset_failed = True
    await _gate()
    assert _settle(events)["settle_sec"] == 0.05
    assert srv.fsm.state is ServerState.RECOVERING
    assert armed_power_cycle == ["power-cycle"], "the broker's own power cycle still fires"


# ---------------------------------------------------------------- the event


@pytest.mark.asyncio
async def test_one_post_kill_settle_event_and_line_per_reset(killed, events):  # noqa: F811
    await _gate()
    ev = _settle(events)
    for field in ("route", "reap_wait_sec", "settle_sec", "since_exit_sec", "holders", "chips", "sbr_chips", "killed"):
        assert field in ev, field
    assert len(ev["chips"]) == CHIPS and set(ev["chips"].values()) == {"ok"}
    lines = [line for line in killed["log"].text().splitlines() if LINE_RE.search(line)]
    assert len(lines) == 1, lines


@pytest.mark.asyncio
async def test_no_post_kill_settle_without_a_debt(mesh, events):  # noqa: F811
    mesh["eth"] = ADVANCING
    await _gate()
    assert "post_kill_settle" not in [name for name, _ in events]
