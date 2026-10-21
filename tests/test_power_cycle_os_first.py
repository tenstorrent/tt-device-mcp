# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The broker's power cycle brings the host OS down cleanly first (spec 04 I88).

The broker writes a request, starts a fallback unit that cycles the host once the shutdown bound
passes, then asks systemd to reboot; the installed systemd-shutdown hook fires the cycle once the
filesystems are read-only. Where that cannot start, the broker cycles directly, as before. No test
here runs a real step: ``_run_step`` and the direct fire are always stubs, and the shell pieces run
only against stub commands.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import offbus_power_cycle as opc
from tt_device_mcp.health.recovery.offbus_power_cycle import fire_command as _real_fire_command  # before the tripwire
from tt_device_mcp.health.recovery.stages import power_cycle as pc

_real_installed_shutdown_hook = pc.installed_shutdown_hook  # before conftest stubs it

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "deploy" / "tt-device-mcp-power-cycle.shutdown"
APPLY = REPO / "deploy" / "apply-host-config.sh"
REASON = "10-min hold bound: the full ladder did not heal it"
CYCLE = ["ipmitool", "chassis", "power", "cycle"]


@pytest.fixture
def os_first(monkeypatch, tmp_path):
    """The clean path wired to stubs: available, its request in tmp_path, each step recorded and
    answered from ``rc`` (step name -> rc), the direct fire recorded."""
    calls, events = [], []
    rc = {"systemd-run": 0, "reboot": 0, "stop": 0}
    req = tmp_path / "power-cycle.request"
    monkeypatch.setattr(pc, "REQUEST_FILE", str(req))
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "")
    monkeypatch.setattr(pc, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC", raising=False)

    def step(argv, timeout=pc.STEP_TIMEOUT_SEC):
        key = "systemd-run" if argv[0] == "systemd-run" else ("reboot" if "reboot" in argv else "stop")
        calls.append((key, list(argv), req.exists()))
        return rc[key], "" if rc[key] == 0 else f"{key} failed"

    monkeypatch.setattr(pc, "_run_step", step)
    direct = []
    return {"calls": calls, "events": events, "rc": rc, "req": req, "direct": direct, "fire": lambda: direct.append(1)}


def _steps(s):
    return [c[0] for c in s["calls"]]


def test_clean_path_writes_the_request_then_the_fallback_then_reboots(os_first):
    assert pc.fire_os_first(CYCLE, REASON, os_first["fire"]) == "os_first"
    assert _steps(os_first) == ["systemd-run", "reboot"]
    assert all(seen for _, _, seen in os_first["calls"]), "the request must exist before any step runs"
    assert os_first["direct"] == [], "the clean path never cuts power with the OS up"
    at, reason, *argv = os_first["req"].read_text().splitlines()
    assert abs(int(at) - time.time()) < 60 and reason == REASON and argv == CYCLE
    assert oct(os_first["req"].stat().st_mode & 0o777) == "0o600"
    assert os_first["calls"][1][1] == list(pc.REBOOT_ARGV)
    assert os_first["events"][-1][1]["phase"] == "reboot_requested"


def test_the_fallback_unit_stays_out_of_the_shutdown_and_fires_the_same_command_at_the_bound(os_first):
    pc.fire_os_first(CYCLE, REASON, os_first["fire"])
    argv = os_first["calls"][0][1]
    assert "--property=DefaultDependencies=no" in argv and "--collect" in argv
    assert argv[argv.index("-c") + 1] == pc.FALLBACK_SH
    assert argv[-5:] == [str(pc.SHUTDOWN_BOUND_DEFAULT_SEC), *CYCLE]


def test_a_hung_shutdown_reaches_the_cycle_once_the_bound_passes(tmp_path):
    """The fallback's own shell, run for real against a stub cycle: it waits the bound, then runs the
    command with its argv intact (a word with a space stays one word)."""
    out = tmp_path / "fired"
    cmd = ["/bin/sh", "-c", 'printf "%s|" "$@" > "$0"', str(out), "a b", "c"]
    t0 = time.monotonic()
    subprocess.run(["/bin/sh", "-c", pc.FALLBACK_SH, "fallback", "1", *cmd], timeout=30, check=True)
    took = time.monotonic() - t0
    assert out.read_text() == "a b|c|"
    assert 1 <= took < 1 + 2 + 5, f"fired after {took:.1f}s for a 1s bound"


@pytest.mark.parametrize(
    "raw,want",
    [("", 120), ("90", 90), ("0", 30), ("-5", 30), ("99999", 300), ("off", 120), ("inf", 120), ("nan", 120)],
)
def test_the_shutdown_bound_is_always_finite_and_clamped(monkeypatch, raw, want):
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC", raw)
    assert pc.shutdown_bound_sec() == want


def test_no_hook_installed_cycles_directly_and_runs_no_step(os_first, monkeypatch):
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "no executable hook")
    assert pc.fire_os_first(CYCLE, REASON, os_first["fire"]) == "direct"
    assert _steps(os_first) == [] and os_first["direct"] == [1] and not os_first["req"].exists()
    assert os_first["events"][-1][1] == {"phase": "unavailable", "reason": REASON, "why": "no executable hook"}


def test_an_unwritable_request_cycles_directly(os_first, monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "REQUEST_FILE", str(tmp_path / "missing-dir" / "req"))
    assert pc.fire_os_first(CYCLE, REASON, os_first["fire"]) == "direct"
    assert _steps(os_first) == [] and os_first["direct"] == [1]


def test_a_fallback_unit_that_cannot_start_never_reboots_without_its_bound(os_first):
    os_first["rc"]["systemd-run"] = 1
    assert pc.fire_os_first(CYCLE, REASON, os_first["fire"]) == "direct"
    assert _steps(os_first) == ["systemd-run"], "no reboot without the fallback that bounds it"
    assert os_first["direct"] == [1] and not os_first["req"].exists()


def test_a_failed_reboot_stops_the_fallback_and_cycles_directly(os_first):
    os_first["rc"]["reboot"] = 1
    assert pc.fire_os_first(CYCLE, REASON, os_first["fire"]) == "direct"
    assert _steps(os_first) == ["systemd-run", "reboot", "stop"]
    unit = next(a for a in os_first["calls"][0][1] if a.startswith("--unit=")).split("=", 1)[1]
    assert os_first["calls"][2][1] == ["systemctl", "stop", f"{unit}.service"]
    assert os_first["direct"] == [1] and not os_first["req"].exists()


def test_an_argv_the_hook_cannot_carry_cycles_directly(os_first):
    assert pc.fire_os_first(["cycler", "bad\nword"], REASON, os_first["fire"]) == "direct"
    assert _steps(os_first) == [] and os_first["direct"] == [1]


def test_a_direct_fire_that_fails_still_raises_so_the_ledger_retracts(os_first, monkeypatch):
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "no hook")

    def broken():
        raise RuntimeError("ipmitool power cycle exited 1")

    with pytest.raises(RuntimeError):
        pc.fire_os_first(CYCLE, REASON, broken)


def test_unavailable_without_systemd_or_the_hook(monkeypatch, tmp_path):
    real_isdir = os.path.isdir
    monkeypatch.setattr(pc.os.path, "isdir", lambda p: False if p == "/run/systemd/system" else real_isdir(p))
    assert "systemd" in pc.os_first_unavailable()
    monkeypatch.setattr(pc.os.path, "isdir", lambda p: True if p == "/run/systemd/system" else real_isdir(p))
    monkeypatch.setattr(pc, "installed_shutdown_hook", lambda: "")
    assert pc.SHUTDOWN_HOOK_NAME in pc.os_first_unavailable()


def test_installed_shutdown_hook_needs_an_executable_file(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "SHUTDOWN_HOOK_DIRS", (str(tmp_path / "a"), str(tmp_path)))
    hook = tmp_path / pc.SHUTDOWN_HOOK_NAME
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o644)
    assert _real_installed_shutdown_hook() == ""
    hook.chmod(0o755)
    assert _real_installed_shutdown_hook() == str(hook)


def test_the_configured_power_cycle_command_takes_the_same_clean_path(os_first):
    """TT_DEVICE_MCP_POWER_CYCLE_CMD (I27) is what the hook and the fallback fire, not ipmitool."""
    _real_fire_command(["/usr/local/sbin/site-cycle", "--now"], REASON)
    assert _steps(os_first) == ["systemd-run", "reboot"]
    assert os_first["req"].read_text().splitlines()[2:] == ["/usr/local/sbin/site-cycle", "--now"]
    assert os_first["calls"][0][1][-2:] == ["/usr/local/sbin/site-cycle", "--now"]


def test_the_configured_command_runs_directly_where_the_clean_path_cannot(os_first, monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "no hook")
    seen = []
    monkeypatch.setattr(opc, "fire_command_direct", lambda argv, reason, timeout=0: seen.append(argv))
    _real_fire_command(["site-cycle"], REASON)
    assert seen == [["site-cycle"]] and _steps(os_first) == []


@pytest.mark.asyncio
async def test_the_escalation_is_unchanged_around_the_clean_path(os_first, monkeypatch, tmp_path):
    """Ledger record before the fire, the request pending after it (no device reset races the
    shutdown, I34), the stage counted as fired."""
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "health_event", lambda *a, **k: None)
    monkeypatch.delenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", raising=False)
    monkeypatch.setattr(pc, "_fire_direct_power_cycle", os_first["fire"])
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None)
    assert await srv._auto_power_cycle_host(lambda _l: None, REASON) is True
    assert [r["action"] for r in srv.recovery_mechanism.read_auto_recovery_ledger()] == ["power-cycle"]
    assert srv.recovery_mechanism.host_action_requested[0] == "power-cycle"
    assert _steps(os_first) == ["systemd-run", "reboot"] and os_first["direct"] == []


# The hook itself, run against stub commands: sync, mdadm and sleep are stubs on PATH, the cycle a
# script that records its argv. systemd-shutdown passes the verb as $1; the tripwire refuses that
# word anywhere in an argv, so the test hands it over in HOOK_VERB and the shell sets $1 from it.


@pytest.fixture
def hook(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    trace = tmp_path / "trace"
    for name in ("sync", "mdadm", "sleep"):
        p = stubs / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{trace}"\n')
        p.chmod(0o755)
    cycle = tmp_path / "cycle"
    cycle.write_text(f'#!/bin/sh\necho "cycle $*" >> "{trace}"\nexit "${{CYCLE_RC:-0}}"\n')
    cycle.chmod(0o755)
    req = tmp_path / "req"

    def run(verb, request=None, cycle_rc=0):
        if request is not None:
            req.write_text(request)
        env = {
            "PATH": f"{stubs}:/usr/bin:/bin",
            "TT_DEVICE_MCP_POWER_CYCLE_REQUEST": str(req),
            "CYCLE_RC": str(cycle_rc),
            "HOOK_VERB": verb,
        }
        argv = ["/bin/sh", "-c", 'set -- "$HOOK_VERB"; . "$0"', str(HOOK)]
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, [t.rstrip() for t in trace.read_text().splitlines()] if trace.exists() else []

    return {"run": run, "cycle": str(cycle)}


def _request(argv, age=0):
    return pc.request_text(argv, REASON, time.time() - age)


def test_hook_fires_the_requested_cycle_after_flushing_on_reboot(hook):
    rc, trace = hook["run"]("reboot", _request([hook["cycle"], "chassis power", "cycle"]))
    assert rc == 0
    assert trace[0] == "sync" and trace[1].startswith("mdadm --wait-clean")
    assert trace[2] == "cycle chassis power cycle" and trace[3].startswith("sleep")


@pytest.mark.parametrize("verb", ["poweroff", "halt", "kexec", ""])
def test_hook_never_cycles_on_any_other_verb(hook, verb):
    assert hook["run"](verb, _request([hook["cycle"]])) == (0, [])


def test_hook_does_nothing_without_a_request(hook):
    assert hook["run"]("reboot") == (0, [])


def test_hook_ignores_a_stale_request(hook):
    assert hook["run"]("reboot", _request([hook["cycle"]], age=7200)) == (0, [])


def test_hook_ignores_a_request_with_no_command(hook):
    assert hook["run"]("reboot", f"{int(time.time())}\n{REASON}\n") == (0, [])


def test_hook_retries_a_failing_cycle_then_lets_the_reboot_continue(hook):
    rc, trace = hook["run"]("reboot", _request([hook["cycle"]]), cycle_rc=1)
    assert rc == 1 and [t for t in trace if t.startswith("cycle")] == ["cycle"] * 3


def test_the_installer_installs_the_hook_where_the_broker_looks():
    text = APPLY.read_text()
    assert f'"$DEPLOY/{HOOK.name}" /usr/lib/systemd/system-shutdown/{pc.SHUTDOWN_HOOK_NAME}' in text
    assert HOOK.name == pc.SHUTDOWN_HOOK_NAME and os.access(HOOK, os.X_OK)
    assert "/usr/lib/systemd/system-shutdown" in pc.SHUTDOWN_HOOK_DIRS
    assert pc.REQUEST_FILE == "/run/tt-device-mcp-power-cycle.request"
    assert f":-{pc.REQUEST_FILE}}}" in HOOK.read_text()
