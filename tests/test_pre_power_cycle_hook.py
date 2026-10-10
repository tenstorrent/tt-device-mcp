# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The opt-in pre-power-cycle hook (spec 04 I22).

A BMC power cycle cuts everything else on the host. TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK lets the
operator drain it first. The hook is a courtesy, never a gate: whatever it does (fail, hang, not
exist), the cycle still fires, and unset nothing changes.
"""

import asyncio
import os
import threading
import time

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery.stages import power_cycle as pc

REASON = "reset and reboot could not recover it"


@pytest.fixture
def cycle(monkeypatch, tmp_path):
    """A power-cycle rung wired to a ledger in tmp_path, a fire that only records, and the hook's
    log lines and health events captured. ``fire`` must stay a stub: no test may power-cycle."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    events = []
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.delenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK_TIMEOUT_SEC", raising=False)
    order = []
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: order.append("fire"))
    lines = []

    async def run():
        await srv._auto_power_cycle_host(lines.append, REASON)

    return {"run": run, "order": order, "lines": lines, "events": events, "dir": tmp_path}


def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


def _ledger_actions():
    return [r["action"] for r in srv.recovery_mechanism.read_auto_recovery_ledger()]


@pytest.mark.asyncio
async def test_unset_hook_runs_nothing_and_the_cycle_is_unchanged(cycle):
    await cycle["run"]()
    assert cycle["order"] == ["fire"]
    assert not any("PRE-POWER-CYCLE" in ln for ln in cycle["lines"])
    assert not any(n == "pre_power_cycle_hook" for n, _ in cycle["events"])


@pytest.mark.asyncio
async def test_the_hook_runs_after_the_record_and_before_the_fire(cycle, monkeypatch):
    """It runs only for a cycle that is really about to happen: the ledger entry and the jobs row
    are already written, and the fire comes right after it returns. It is told why."""
    seen = cycle["dir"] / "seen"
    hook = _script(
        cycle["dir"],
        "hook",
        f'cat {cycle["dir"]}/auto_recovery.jsonl > {seen}\necho "reason=$TT_DEVICE_MCP_POWER_CYCLE_REASON"\n',
    )
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", str(hook))
    real_fire_order = cycle["order"]
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: real_fire_order.append(("fire", seen.exists())))

    await cycle["run"]()

    assert real_fire_order == [("fire", True)], "the hook finished before the fire"
    assert '"power-cycle"' in seen.read_text(), "the escalation was recorded before the hook ran"
    assert any("exited 0" in ln and f"reason={REASON}" in ln for ln in cycle["lines"]), cycle["lines"]
    ev = [k for n, k in cycle["events"] if n == "pre_power_cycle_hook"]
    assert len(ev) == 1 and ev[0]["rc"] == 0 and ev[0]["timed_out"] is False


@pytest.mark.asyncio
async def test_a_failing_hook_still_power_cycles(cycle, monkeypatch):
    hook = _script(cycle["dir"], "hook", "echo drain refused >&2\nexit 3\n")
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", str(hook))
    await cycle["run"]()
    assert cycle["order"] == ["fire"]
    assert any("exited 3" in ln and "drain refused" in ln for ln in cycle["lines"]), cycle["lines"]
    assert _ledger_actions() == ["power-cycle"], "a fired cycle keeps its rate-limit entry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["/nonexistent/drain-harnesses", "'unterminated", str(os.devnull)],
    ids=["missing", "unparseable", "not-executable"],
)
async def test_a_hook_that_cannot_run_still_power_cycles(cycle, monkeypatch, value):
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", value)
    await cycle["run"]()
    assert cycle["order"] == ["fire"]
    assert any("PRE-POWER-CYCLE HOOK" in ln for ln in cycle["lines"]), "the skip is said, not silent"


@pytest.mark.asyncio
async def test_a_hung_hook_is_killed_with_its_children_and_the_cycle_goes_ahead(cycle, monkeypatch):
    """A drain that never returns must not hold recovery: past the timeout the hook's whole
    process group is killed (a child it forked too) and the cycle fires."""
    pidfile = cycle["dir"] / "child.pid"
    hook = _script(cycle["dir"], "hook", f"sleep 300 &\necho $! > {pidfile}\nwait\n")
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", str(hook))
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK_TIMEOUT_SEC", "1")

    t0 = time.monotonic()
    await cycle["run"]()

    assert time.monotonic() - t0 < 10
    assert cycle["order"] == ["fire"]
    assert any("killed after 1s" in ln for ln in cycle["lines"]), cycle["lines"]
    ev = [k for n, k in cycle["events"] if n == "pre_power_cycle_hook"]
    assert ev and ev[0]["timed_out"] is True
    child = int(pidfile.read_text())
    for _ in range(50):  # SIGKILL is delivered at once; give the reaper a moment
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        pytest.fail("the hook's child outlived the timeout")


@pytest.mark.asyncio
async def test_a_background_child_holding_output_does_not_stretch_the_wait(cycle, monkeypatch):
    """The wait ends when the hook itself exits, even if something it started keeps its stdout."""
    hook = _script(cycle["dir"], "hook", "sleep 5 &\necho started\nexit 0\n")
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", str(hook))
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK_TIMEOUT_SEC", "30")
    t0 = time.monotonic()
    await cycle["run"]()
    assert time.monotonic() - t0 < 4
    assert cycle["order"] == ["fire"]


@pytest.mark.asyncio
async def test_the_hook_never_runs_when_the_ledger_aborts_the_cycle(cycle, monkeypatch):
    """No cycle, no drain: co-tenants are never stopped for a cycle that will not happen."""
    ran = cycle["dir"] / "ran"
    hook = _script(cycle["dir"], "hook", f"touch {ran}\n")
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", str(hook))
    monkeypatch.setattr(srv.recovery_mechanism, "record_auto_recovery", lambda *a, **k: False)
    await cycle["run"]()
    assert cycle["order"] == []
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_hook_that_raises_still_power_cycles(cycle, monkeypatch):
    def boom(log, reason):
        raise RuntimeError("bug in the runner")

    monkeypatch.setattr(srv, "_run_pre_power_cycle_hook", boom)
    await cycle["run"]()
    assert cycle["order"] == ["fire"]


@pytest.mark.asyncio
async def test_cancelled_while_waiting_on_the_hook_retracts_the_ledger_entry(cycle, monkeypatch):
    """Stopped mid-drain (broker shutdown), nothing fired: the entry must not spend the retry the
    still-wedged box needs on its next start."""
    entered, release = threading.Event(), threading.Event()

    def blocking_hook(log, reason):
        entered.set()
        release.wait(10)
        return {"ran": True, "rc": 0}

    monkeypatch.setattr(srv, "_run_pre_power_cycle_hook", blocking_hook)
    task = asyncio.create_task(cycle["run"]())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.01)
        assert _ledger_actions() == ["power-cycle"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert cycle["order"] == []
    assert _ledger_actions() == []


@pytest.mark.asyncio
async def test_the_warm_reboot_does_not_run_the_power_cycle_hook(cycle, monkeypatch):
    ran = cycle["dir"] / "ran"
    hook = _script(cycle["dir"], "hook", f"touch {ran}\n")
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", str(hook))
    fired = []
    monkeypatch.setattr(galaxy, "_fire_host_reboot", lambda: fired.append(True))
    await srv.galaxy_recovery._auto_reboot_host(lambda _m: None, REASON)
    assert fired == [True]
    assert not ran.exists()


@pytest.mark.parametrize(
    "raw,want",
    [
        ("", pc.PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC),
        ("junk", pc.PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC),
        ("0", pc.PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC),
        ("-5", pc.PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC),
        ("inf", pc.PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC),
        ("nan", pc.PRE_POWER_CYCLE_HOOK_TIMEOUT_DEFAULT_SEC),
        ("120", 120.0),
        ("99999999", pc.PRE_POWER_CYCLE_HOOK_TIMEOUT_MAX_SEC),
    ],
)
def test_the_timeout_is_always_finite_and_positive(monkeypatch, raw, want):
    monkeypatch.setenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK_TIMEOUT_SEC", raw)
    assert pc.pre_power_cycle_hook_timeout_sec() == want
