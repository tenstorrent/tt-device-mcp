# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I89: on a multi-tray mesh the reset a stopped job owes (I69) first waits, bounded, until
every process of the job is gone and nothing outside the broker holds a device node, then lets the
mesh settle and reads every chip. One ``post_kill_settle`` event names the route and the waits.

The gate tests run the real probe pass on the kill-reset tests' 32-chip mesh (leaf probes stubbed)."""

import asyncio
import os
import re
import signal
import subprocess
import sys

import pytest

from tests.conftest import patch_health_event
from tests.test_eth_timeout_reset import ADVANCING, mesh  # noqa: F401 (fixture)
from tests.test_job_kill_reset import killed  # noqa: F401 (fixture)
from tt_device_mcp import constants, job_reap
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan, ZombieInfo
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors.heartbeat import ALL_ONES
from tt_device_mcp.job_reap import JOB_TAG_ENV

CHIPS = 32
# The one line each post-kill reset logs; nothing else in the broker uses the name.
LINE_RE = re.compile(r"\bpost_kill_settle: route (mesh_reset|sbr|power_cycle|holder_left|own_rung);")
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


@pytest.mark.asyncio
async def test_a_zombie_holder_whose_threads_all_exited_does_not_block(monkeypatch):
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 5)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(CHIPS)})
    _holders(monkeypatch, FAKE_PID)
    monkeypatch.setattr(
        srv, "zombie_info", lambda pid: ZombieInfo(pid, "Z", 1, [], 1, "python3", "S", None) if pid == FAKE_PID else None
    )
    q = await srv._post_kill_quiesce(CHIPS, _Log())
    assert q["holders"] == [] and q["reap_wait_sec"] < 1.0, q
    assert srv._post_kill_route(q, CHIPS, True) == "mesh_reset"


@pytest.mark.asyncio
async def test_a_zombie_with_a_live_holding_thread_still_blocks(monkeypatch):
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    _holders(monkeypatch, FAKE_PID)
    monkeypatch.setattr(
        srv,
        "zombie_info",
        lambda pid: ZombieInfo(pid, "Z", 2, [pid + 1], 1, "python3", "S", True) if pid == FAKE_PID else None,
    )
    q = await srv._post_kill_quiesce(CHIPS, _Log())
    assert [h["pid"] for h in q["holders"]] == [FAKE_PID] and q["holders"][0]["zombie"] == "holds"
    assert srv._post_kill_route(q, CHIPS, True) == "holder_left"


@pytest.mark.asyncio
async def test_a_live_holder_past_the_bound_is_never_signalled(monkeypatch):
    """A holder that is not the job's is left to the reset's own holder scan: no signal, no settle."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 5)
    _holders(monkeypatch, FAKE_PID)
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)) if pid == FAKE_PID else real_kill(pid, sig))
    q = await srv._post_kill_quiesce(CHIPS, _Log())
    assert sent == [] and q["killed"] == []
    assert q["reap_wait_sec"] >= 0.2 and q["settle_sec"] == 0.0, q
    assert srv._post_kill_route(q, CHIPS, True) == "holder_left"


@pytest.mark.asyncio
async def test_a_live_holder_past_the_bound_keeps_the_hold(killed, events, monkeypatch, armed_power_cycle):  # noqa: F811
    """With a holder still there, the episode goes on through the owed-reset path; the gate never
    releases a device whose post-kill check has not passed."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0.2)
    _holders(monkeypatch, FAKE_PID)
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)) if pid == FAKE_PID else real_kill(pid, sig))
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
    assert used + constants.PRE_RESET_ZOMBIE_LADDER_RESERVE_SEC < 600
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
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): (ALL_ONES if str(i) in off else 100) for i in range(CHIPS)})
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
async def test_a_failed_post_kill_check_still_climbs_to_the_power_cycle(killed, events, armed_power_cycle, monkeypatch):  # noqa: F811
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
