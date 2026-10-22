# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The broker's power cycle brings the host OS down cleanly first (spec 04 I88).

The broker checks the BMC read-only, writes a request, starts a fallback unit that cycles the host
once the shutdown bound passes, then asks systemd to reboot; the installed systemd-shutdown hook fires
the cycle once the filesystems are read-only. Where that cannot start nothing fires: the escalation
retracts its ledger entry, keeps the hold and pages. No test here runs a real step: ``_run_step`` is
always a stub, and the shell pieces run only against stub commands.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import alert
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
CYCLE_ABS = ["/usr/bin/ipmitool", "chassis", "power", "cycle"]


def _key(argv):
    if argv[0] == "systemd-run":
        return "systemd-run"
    if "reboot" in argv:
        return "reboot"
    if argv[-2:] == ["mc", "info"]:
        return "preflight"
    if argv[-3:] == ["raw", "0x00", "0x07"]:
        return "restart_cause"
    for k in ("is-active", "kill", "stop"):
        if k in argv:
            return k
    raise AssertionError(f"unexpected step {argv}")


@pytest.fixture
def os_first(monkeypatch, tmp_path):
    """The clean path wired to stubs: available, its request in tmp_path, each step recorded and
    answered from ``rc`` (step name -> rc), commands resolved under /usr/bin."""
    calls, events = [], []
    rc = {"systemd-run": 0, "reboot": 0, "stop": 0, "kill": 0, "is-active": 3, "preflight": 0, "restart_cause": 0}
    out = {"restart_cause": " 01 0f"}  # chassis power control command
    req = tmp_path / "power-cycle.request"
    monkeypatch.setattr(pc, "REQUEST_FILE", str(req))
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "")
    monkeypatch.setattr(pc, "_resolve_command", lambda c: c if c.startswith("/") else f"/usr/bin/{c}")
    monkeypatch.setattr(pc, "health_event", lambda name, **k: events.append((name, k)))
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_CMD_TIMEOUT_SEC", raising=False)

    def step(argv, timeout=pc.STEP_TIMEOUT_SEC):
        key = _key(argv)
        calls.append((key, list(argv), req.exists(), timeout))
        return rc[key], out.get(key, "" if rc[key] == 0 else f"{key} failed")

    monkeypatch.setattr(pc, "_run_step", step)
    return {"calls": calls, "events": events, "rc": rc, "out": out, "req": req}


def _steps(s):
    return [c[0] for c in s["calls"]]


def _not_launched(s, argv=CYCLE, reason=REASON):
    with pytest.raises(pc.PowerCycleNotLaunched) as e:
        pc.fire_os_first(argv, reason)
    assert not isinstance(e.value, recovery_base.HostActionTimedOut)
    assert not s["req"].exists() and not Path(pc.fallback_pid_file()).exists()
    assert s["events"][-1][0] == "power_cycle_os_first" and s["events"][-1][1]["phase"] == "not_launched"
    return e.value.why


def test_clean_path_checks_the_bmc_writes_the_request_then_the_fallback_then_reboots(os_first):
    assert pc.fire_os_first(CYCLE, REASON) == "os_first"
    assert _steps(os_first) == ["preflight", "systemd-run", "reboot"]
    assert os_first["calls"][0][1] == ["/usr/bin/ipmitool", "mc", "info"]
    assert os_first["calls"][0][3] == pc.PREFLIGHT_TIMEOUT_SEC <= 10
    assert not os_first["calls"][0][2], "the pre-flight runs before anything is written"
    assert all(seen for _, _, seen, _ in os_first["calls"][1:]), "the request must exist before the unit and reboot"
    at, reason, try_timeout, *argv = os_first["req"].read_text().splitlines()
    assert abs(int(at) - time.time()) < 60 and reason == REASON and argv == CYCLE_ABS
    assert int(try_timeout) == pc.hook_try_timeout_sec(pc.DEFAULT_POWER_CYCLE_TIMEOUT_SEC)
    assert oct(os_first["req"].stat().st_mode & 0o777) == "0o600"
    assert os_first["calls"][2][1] == list(pc.REBOOT_ARGV)
    assert os_first["events"][-1][1]["phase"] == "reboot_requested"


def test_the_fallback_unit_stays_out_of_the_shutdown_and_survives_its_killall(os_first, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD_TIMEOUT_SEC", "45")
    _real_fire_command(CYCLE, REASON)
    argv = os_first["calls"][1][1]
    assert "--property=DefaultDependencies=no" in argv and "--collect" in argv
    assert f"--setenv=TT_DEVICE_MCP_POWER_CYCLE_REASON={REASON}" in argv
    i = argv.index("/bin/bash")
    assert argv[i + 1 : i + 5] == ["-c", pc.FALLBACK_LAUNCH_SH, pc.FALLBACK_ARGV0, pc.FALLBACK_SH]
    assert pc.FALLBACK_ARGV0.startswith("@") and pc.FALLBACK_FIRE_ARGV0.startswith("@")
    assert argv[i + 5 :] == [pc.fallback_pid_file(), str(pc.SHUTDOWN_BOUND_DEFAULT_SEC), "45", *CYCLE_ABS]
    assert os_first["req"].read_text().splitlines()[2] == str(pc.HOOK_TRY_TIMEOUT_MAX_SEC)


def _run_fallback(tmp_path, bound, cmd, timeout=5):
    pidf = tmp_path / "fallback.pid"
    # The fallback runs the installed hook's IPMI preparation: never the host's, a stub here.
    prep = tmp_path / "prepare-ipmi"
    prep.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path / "prepared"}"\n')
    prep.chmod(0o755)
    script = pc.FALLBACK_SH
    for d in pc.SHUTDOWN_HOOK_DIRS:
        script = script.replace(f"{d}/{pc.SHUTDOWN_HOOK_NAME}", str(prep))
    assert str(prep) in script
    argv = [
        "/bin/bash",
        "-c",
        pc.FALLBACK_LAUNCH_SH,
        pc.FALLBACK_ARGV0,
        script,
        str(pidf),
        str(bound),
        str(timeout),
        *cmd,
    ]
    env = {"PATH": "/usr/bin:/bin", "TT_DEVICE_MCP_POWER_CYCLE_REASON": REASON}
    return pidf, subprocess.Popen(argv, env=env)


def _cmdline(pid):
    return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")


def test_a_hung_shutdown_reaches_the_cycle_once_the_bound_passes(tmp_path):
    """The fallback's own chain, run for real against a stub cycle: it records its pid under an '@'
    argv[0] (systemd-shutdown's killall spares it) with no sleep child to kill, waits the bound, then
    runs the command with its argv intact and the reason in its environment."""
    out = tmp_path / "fired"
    cmd = ["/bin/sh", "-c", 'printf "%s|" "$TT_DEVICE_MCP_POWER_CYCLE_REASON" "$@" > "$0"', str(out), "a b", "c"]
    t0 = time.monotonic()
    pidf, proc = _run_fallback(tmp_path, 2, cmd)
    try:
        deadline = time.monotonic() + 5
        while not pidf.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(pidf.read_text())
        assert pid == proc.pid
        assert _cmdline(pid)[0] == pc.FALLBACK_ARGV0.encode()
        time.sleep(0.3)
        kids = subprocess.run(["ps", "-o", "args=", "--ppid", str(pid)], capture_output=True, text=True).stdout
        assert "sleep" not in kids, "a sleep child would die in the killall and fire the cycle early"
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    took = time.monotonic() - t0
    assert out.read_text() == f"{REASON}|a b|c|"
    assert 2 <= took < 2 + 2 + 5, f"fired after {took:.1f}s for a 2s bound"


def test_the_fallback_has_the_hook_prepare_ipmi_before_it_fires(tmp_path):
    """A shutdown that hangs may hang past openipmi's stop (drivers unloaded, /dev/ipmi0 gone): the
    fallback has the installed hook load them again, bounded, before its cycle command runs. Its unit
    has DefaultDependencies=no, so no ordering against openipmi could keep them loaded for it."""
    out = tmp_path / "fired"
    cmd = ["/bin/sh", "-c", f'cat "{tmp_path / "prepared"}" > "$0"', str(out)]
    _, proc = _run_fallback(tmp_path, 0, cmd)
    assert proc.wait(timeout=30) == 0
    assert out.read_text() == "--prepare-ipmi\n"
    for d in pc.SHUTDOWN_HOOK_DIRS:
        assert f"{d}/{pc.SHUTDOWN_HOOK_NAME}" in pc.FALLBACK_SH
    assert f'timeout {pc.HOOK_IPMI_PREPARE_SEC} "$h" --prepare-ipmi' in pc.FALLBACK_SH
    assert pc.FALLBACK_SH.index("--prepare-ipmi") < pc.FALLBACK_SH.index(f"exec -a {pc.FALLBACK_FIRE_ARGV0}")


def test_the_fallback_fires_under_an_at_sign_argv0_and_its_timeout(tmp_path):
    out = tmp_path / "fired"
    cmd = ["/bin/sh", "-c", 'tr "\\0" " " </proc/$PPID/cmdline > "$0"; exec sleep 30', str(out)]
    pidf, proc = _run_fallback(tmp_path, 1, cmd, timeout=1)
    assert proc.wait(timeout=30) != 0, "the command's timeout ends a hung cycle command"
    assert out.read_text().startswith(f"{pc.FALLBACK_FIRE_ARGV0} 1 ")


@pytest.mark.parametrize(
    "raw,want",
    [("", 120), ("90", 90), ("0", 30), ("-5", 30), ("99999", 300), ("off", 120), ("inf", 120), ("nan", 120)],
)
def test_the_shutdown_bound_is_always_finite_and_clamped(monkeypatch, raw, want):
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_SHUTDOWN_BOUND_SEC", raw)
    assert pc.shutdown_bound_sec() == want


def test_no_hook_installed_raises_not_launched_and_runs_no_step(os_first, monkeypatch):
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "no executable hook")
    assert _not_launched(os_first) == "no executable hook"
    assert _steps(os_first) == []


def test_an_unwritable_request_raises_not_launched(os_first, monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "REQUEST_FILE", str(tmp_path / "missing-dir" / "req"))
    assert "could not write" in _not_launched(os_first)
    assert _steps(os_first) == ["preflight"]


def test_a_fallback_unit_that_cannot_start_never_reboots(os_first):
    os_first["rc"]["systemd-run"] = 1
    assert "fallback unit did not start" in _not_launched(os_first)
    assert _steps(os_first) == ["preflight", "systemd-run"], "no reboot without the fallback that bounds it"


def test_a_failed_reboot_stops_the_fallback_and_raises_not_launched(os_first):
    os_first["rc"]["reboot"] = 1
    assert "systemctl reboot failed" in _not_launched(os_first)
    unit = next(a for a in os_first["calls"][1][1] if a.startswith("--unit=")).split("=", 1)[1]
    assert [c[1] for c in os_first["calls"][3:]] == [
        ["systemctl", "stop", f"{unit}.service"],
        ["systemctl", "is-active", "--quiet", f"{unit}.service"],
    ]


def test_a_fallback_that_will_not_stop_keeps_the_action_pending(os_first):
    """After a failed reboot, a unit that outlives stop and kill may still fire at its bound: no
    retract (the caller keeps the action pending), never a second fire from here."""
    os_first["rc"].update(reboot=1, stop=1, **{"is-active": 0})
    assert pc.fire_os_first(CYCLE, REASON) == "fallback_pending"
    assert _steps(os_first) == ["preflight", "systemd-run", "reboot", "stop", "kill", "is-active"]
    assert os_first["events"][-1][1]["phase"] == "fallback_pending"


def test_an_unanswered_is_active_reads_as_alive(os_first):
    os_first["rc"].update(reboot=1, stop=1, **{"is-active": None})
    assert pc.fire_os_first(CYCLE, REASON) == "fallback_pending"


def test_a_stopped_fallback_after_a_failed_stop_raises_not_launched(os_first):
    os_first["rc"].update(reboot=1, stop=1)
    _not_launched(os_first)
    assert _steps(os_first)[-3:] == ["stop", "kill", "is-active"]


def test_an_argv_the_hook_cannot_carry_raises_not_launched(os_first):
    _not_launched(os_first, ["cycler", "bad\nword"])
    assert _steps(os_first) == []


def test_a_missing_cycle_command_raises_not_launched(os_first, monkeypatch):
    monkeypatch.setattr(pc, "_resolve_command", lambda c: None)
    assert "not found" in _not_launched(os_first)
    assert _steps(os_first) == []


@pytest.mark.parametrize("rc", [1, None])
def test_a_bmc_that_fails_the_preflight_never_reboots(os_first, rc):
    """F2: a BMC that cannot answer `mc info` cannot cycle; the host would only reboot warm."""
    os_first["rc"]["preflight"] = rc
    assert "pre-flight" in _not_launched(os_first)
    assert _steps(os_first) == ["preflight"]


def test_the_preflight_keeps_the_configured_commands_bmc_options(os_first):
    _real_fire_command(["ipmitool", "-I", "lanplus", "-H", "bmc", "chassis", "power", "cycle"], REASON)
    assert os_first["calls"][0][1] == ["/usr/bin/ipmitool", "-I", "lanplus", "-H", "bmc", "mc", "info"]


def test_an_opaque_configured_command_gets_only_the_executable_check(os_first):
    """TT_DEVICE_MCP_POWER_CYCLE_CMD (I27) is what the hook and the fallback fire, not ipmitool."""
    _real_fire_command(["/usr/local/sbin/site-cycle", "--now"], REASON)
    assert _steps(os_first) == ["systemd-run", "reboot"]
    assert os_first["req"].read_text().splitlines()[3:] == ["/usr/local/sbin/site-cycle", "--now"]
    assert os_first["calls"][0][1][-2:] == ["/usr/local/sbin/site-cycle", "--now"]


def test_the_configured_command_never_runs_under_a_live_os(os_first, monkeypatch):
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "no hook")
    with pytest.raises(pc.PowerCycleNotLaunched):
        _real_fire_command(["site-cycle"], REASON)
    assert _steps(os_first) == []


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


@pytest.fixture
def escalation(os_first, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(recovery_base, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    hevents = []
    monkeypatch.setattr(recovery_base, "health_event", lambda name, **k: hevents.append((name, k)))
    monkeypatch.delenv("TT_DEVICE_MCP_PRE_POWER_CYCLE_HOOK", raising=False)
    mech = srv.recovery_mechanism
    monkeypatch.setattr(mech, "host_action_requested", None)
    monkeypatch.setattr(mech, "_power_cycle_not_launched_paged", None)
    monkeypatch.setattr(mech, "_device_hold_episode_since", lambda: "held-since-1")
    pages = []
    monkeypatch.setattr(alert, "send_alert", lambda ev: pages.append(ev) or 0)
    os_first.update(mech=mech, pages=pages, hevents=hevents)
    return os_first


@pytest.mark.asyncio
async def test_the_escalation_is_unchanged_around_the_clean_path(escalation):
    """Ledger record before the fire, the request pending after it (no device reset races the
    shutdown, I34), the stage counted as fired."""
    mech = escalation["mech"]
    assert await srv._auto_power_cycle_host(lambda _l: None, REASON) is True
    assert [r["action"] for r in mech.read_auto_recovery_ledger()] == ["power-cycle"]
    assert mech.host_action_requested[0] == "power-cycle"
    assert _steps(escalation) == ["preflight", "systemd-run", "reboot"] and escalation["pages"] == []


@pytest.mark.asyncio
async def test_a_cycle_that_cannot_launch_retracts_holds_and_pages_once_per_episode(escalation, monkeypatch):
    """F1: no live power cut. The ledger entry is retracted, nothing is pending, and the page names
    why — once per hold episode."""
    mech = escalation["mech"]
    monkeypatch.setattr(pc, "os_first_unavailable", lambda: "no executable hook")
    assert await srv._auto_power_cycle_host(lambda _l: None, REASON) is False
    assert mech.read_auto_recovery_ledger() == [] and mech.host_action_requested is None
    assert [p["kind"] for p in escalation["pages"]] == ["power_cycle_not_launched"]
    assert "no executable hook" in escalation["pages"][0]["message"]
    assert await srv._auto_power_cycle_host(lambda _l: None, REASON) is False
    assert len(escalation["pages"]) == 1, "one page per hold episode"
    assert sum(n == "power_cycle_not_launched" for n, _ in escalation["hevents"]) == 2
    monkeypatch.setattr(mech, "_device_hold_episode_since", lambda: "held-since-2")
    await srv._auto_power_cycle_host(lambda _l: None, REASON)
    assert len(escalation["pages"]) == 2
    assert _steps(escalation) == []


@pytest.mark.asyncio
async def test_a_preflight_failure_retracts_and_never_reboots(escalation):
    escalation["rc"]["preflight"] = 1
    assert await srv._auto_power_cycle_host(lambda _l: None, REASON) is False
    assert escalation["mech"].read_auto_recovery_ledger() == []
    assert _steps(escalation) == ["preflight"] and "pre-flight" in escalation["pages"][0]["why"]


@pytest.mark.asyncio
async def test_a_fallback_left_alive_keeps_the_action_pending_and_the_ledger(escalation):
    escalation["rc"].update(reboot=1, stop=1, **{"is-active": 0})
    assert await srv._auto_power_cycle_host(lambda _l: None, REASON) is True
    assert [r["action"] for r in escalation["mech"].read_auto_recovery_ledger()] == ["power-cycle"]
    assert escalation["mech"].host_action_requested[0] == "power-cycle"


# F2(b): on the next boot the BMC says whether the request really was a cycle.

BOOT_NOW = 1_800_000_000.0


@pytest.fixture
def booted(escalation, monkeypatch, tmp_path):
    """A ledger whose last row is a power-cycle request from the previous boot, 100 s before this
    boot started."""
    mech = escalation["mech"]
    btime = BOOT_NOW - 200
    monkeypatch.setattr(mech, "_current_boot_id", lambda: "boot-2")
    monkeypatch.setattr(mech, "_boot_btime_id", lambda: str(int(btime)))
    monkeypatch.setattr(opc, "power_cycle_argv", lambda: None)
    row = {"action": "power-cycle", "reason": REASON, "at": "earlier", "at_epoch": btime - 100, "boot_id": "boot-1"}
    (tmp_path / recovery_base.AUTO_RECOVERY_LEDGER).write_text(json.dumps(row) + "\n")
    escalation["tmp"] = tmp_path
    return escalation


def _row(s):
    return s["mech"].read_auto_recovery_ledger()[-1]


def test_a_confirmed_cycle_counts_as_before(booted):
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "confirmed"
    row = _row(booted)
    assert row["cycle_checked"] == "confirmed" and "took_host_down" not in row
    assert row["host_rebooted"] is True and row["power_cycled"] is True
    assert booted["calls"][-1][1] == ["/usr/bin/ipmitool", "raw", "0x00", "0x07"]
    assert booted["mech"]._cycles_that_ran([row], BOOT_NOW) == [row]
    assert (booted["tmp"] / recovery_base.POWER_CYCLE_CAUSE_CAPABLE_FILE).exists()
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) is None, "checked once"


@pytest.mark.parametrize("code,cause", [(0x00, "unknown"), (0x0A, "soft reset")])
def test_a_refuted_cycle_is_counted_missed_never_late(booted, code, cause):
    """The hook's command failed and the host rebooted warm: the row is missed (no I65 anchor, no
    streak, I72 retry), never re-counted as a late cycle, and the boot is still the broker's own."""
    (booted["tmp"] / recovery_base.POWER_CYCLE_CAUSE_CAPABLE_FILE).write_text("{}\n")
    booted["out"]["restart_cause"] = f" {code:02x} 0f"
    mech = booted["mech"]
    assert mech.confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "refuted"
    row = _row(booted)
    assert row["took_host_down"] is False and row["cycle_refuted"] == cause
    ledger = mech.read_auto_recovery_ledger()
    assert mech._cycles_that_ran(ledger, BOOT_NOW) == []
    assert recovery_base._late_power_cycle(ledger, "boot-2", BOOT_NOW - 200, BOOT_NOW) is None
    mech.mark_late_power_cycle(now_epoch=BOOT_NOW)
    assert "late" not in _row(booted)
    assert any(n == "power_cycle_request_missed" and k.get("refuted") == cause for n, k in booted["hevents"])
    assert mech.boot_from_broker_escalation("boot-2") is not None


def _refute_next_boot(s, monkeypatch, boot_id, btime, at):
    """A power-cycle request recorded on the current boot at ``at``, the hook's command failing, and
    the host coming up warm as ``boot_id`` at ``btime``; the BMC refutes the cycle."""
    mech = s["mech"]
    row = {
        "action": "power-cycle",
        "reason": REASON,
        "at": "earlier",
        "at_epoch": at,
        "boot_id": mech._current_boot_id(),
    }
    with (s["tmp"] / recovery_base.AUTO_RECOVERY_LEDGER).open("a") as f:
        f.write(json.dumps(row) + "\n")
    monkeypatch.setattr(mech, "_current_boot_id", lambda: boot_id)
    monkeypatch.setattr(mech, "_boot_btime_id", lambda: str(int(btime)))
    assert mech.confirm_power_cycle_on_boot(now_epoch=btime + 100) == "refuted"


def test_a_refuted_cycle_is_retried_under_i72_and_pages_from_the_second(booted, monkeypatch):
    """Spec 04 I72/I88: a refuted cycle is the broker's missed attempt. The warm boot it brought up
    anchors no I65 cooldown; the cycle is asked again at the request's pending window + 0 s, then
    + 300 s from the second miss on, with the I72 page from the second."""
    monkeypatch.setattr(recovery_base, "_last_external_power_cycle", lambda _now: None)
    (booted["tmp"] / recovery_base.POWER_CYCLE_CAUSE_CAPABLE_FILE).write_text("{}\n")
    booted["out"]["restart_cause"] = " 0a 0f"
    mech = booted["mech"]
    window = recovery_base.host_action_pending_sec()
    first_at = BOOT_NOW - 300
    assert mech.confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "refuted"
    ledger = mech.read_auto_recovery_ledger()
    assert mech.power_cycle_cooldown(now_epoch=BOOT_NOW, ledger=[], requests=ledger) is None, "no I65 anchor"
    state = mech.power_cycle_miss_state(ledger, now_epoch=BOOT_NOW)
    assert state["missed"] == 1 and state["until"] == first_at + window + recovery_base.POWER_CYCLE_MISS_RETRY_SEC[0]
    assert [p for p in booted["pages"] if p["kind"] == "power_cycle_missed"] == []
    allowed, why = mech.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=BOOT_NOW)
    assert not allowed and "never took the host down" in why
    assert mech.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=state["until"] + 1)[0]

    # The retry fires, the hook fails again and the host comes up warm once more.
    second_at = state["until"] + 1
    _refute_next_boot(booted, monkeypatch, "boot-3", second_at + 200, second_at)
    now = second_at + 300
    ledger = mech.read_auto_recovery_ledger()
    assert mech.power_cycle_cooldown(now_epoch=now, ledger=[], requests=ledger) is None
    state = mech.power_cycle_miss_state(ledger, now_epoch=now)
    assert state["missed"] == 2 and state["until"] == second_at + window + recovery_base.POWER_CYCLE_MISS_RETRY_SEC[1]
    assert [p["count"] for p in booted["pages"] if p["kind"] == "power_cycle_missed"] == [2]
    assert not mech.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=now)[0]
    assert mech.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=state["until"] + 1)[0]

    # A verified release ends the streak: the refuted rows no longer count.
    monkeypatch.setattr(mech, "last_verified_release", lambda: {"at_epoch": now, "boot_id": "boot-3"})
    assert mech.power_cycle_miss_state(ledger, now_epoch=now + 1) is None


def test_refuted_cycles_count_toward_the_backstop(booted, monkeypatch):
    """A hook that keeps failing restarts the host warm at most POWER_CYCLE_BACKSTOP_MAX times per
    backstop window: the refuted rows count there, so the fourth request waits and pages."""
    monkeypatch.setattr(recovery_base, "_last_external_power_cycle", lambda _now: None)
    (booted["tmp"] / recovery_base.POWER_CYCLE_CAUSE_CAPABLE_FILE).write_text("{}\n")
    booted["out"]["restart_cause"] = " 0a 0f"
    mech = booted["mech"]
    assert mech.confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "refuted"
    first_at = BOOT_NOW - 300
    at = BOOT_NOW + 1000
    for n in range(recovery_base.POWER_CYCLE_BACKSTOP_MAX - 1):
        _refute_next_boot(booted, monkeypatch, f"boot-{n + 3}", at + 200, at)
        at += 1500
    allowed, why = mech.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=at)
    assert not allowed and "backstop" in why
    assert mech.power_cycle_cooldown_state["until"] == first_at + recovery_base.POWER_CYCLE_BACKSTOP_WINDOW_SEC


# IPMI 2.0 table 28-11, every code of the low nibble: (name, whether the power went off and on).
RAW_RESTART_CAUSES = {
    0x0: ("unknown", False),
    0x1: ("chassis power control command", True),
    0x2: ("reset via pushbutton", False),
    0x3: ("power-up via pushbutton", True),
    0x4: ("watchdog expired", False),
    0x5: ("oem", False),
    0x6: ("power-up due to always-restore power policy", True),
    0x7: ("power-up due to restore-previous power policy", True),
    0x8: ("reset via pef", False),
    0x9: ("power-cycle via pef", True),
    0xA: ("soft reset", False),
    0xB: ("power-up via rtc wakeup", True),
    **{code: (f"unmapped code 0x{code:02x}", False) for code in range(0xC, 0x10)},
}


@pytest.mark.parametrize(
    "out,want,cycle",
    [
        *[(f" {code:02x} 0f", name, cycle) for code, (name, cycle) in RAW_RESTART_CAUSES.items()],
        ("Close Session command failed\n 1a 0f\n", "soft reset", False),
        ("invalid", None, None),
    ],
)
def test_restart_cause_names_the_raw_code(os_first, out, want, cycle):
    """ipmitool's `chassis restart_cause` prints "invalid" for 0xa/0xb: the raw code is read and
    named per IPMI 2.0 (low nibble of the first byte); reserved codes are unmapped, not a cause.
    Exactly 0x1, 0x3, 0x6, 0x7, 0x9 and 0xb mean the power went off and on."""
    os_first["out"]["restart_cause"] = out
    assert pc.read_restart_cause(CYCLE) == want
    assert os_first["calls"][-1][1] == ["/usr/bin/ipmitool", "raw", "0x00", "0x07"]
    if want is not None:
        assert pc.restart_cause_is_cycle(want) is cycle
        assert pc.restart_cause_is_mapped(want) is not want.startswith("unmapped code")


def test_rtc_wakeup_confirms_the_cycle(booted):
    booted["out"]["restart_cause"] = " 0b 0f"
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "confirmed"
    row = _row(booted)
    assert row["restart_cause"] == "power-up via rtc wakeup"
    assert row["host_rebooted"] is True and row["power_cycled"] is True and "took_host_down" not in row


def test_an_unmapped_code_is_unconfirmed_and_stamps_nothing(booted):
    """A reserved code neither proves the BMC reports causes nor refutes the cycle."""
    booted["out"]["restart_cause"] = " 0c 0f"
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "unconfirmed"
    row = _row(booted)
    assert row["restart_cause"] == "unmapped code 0x0c" and row["power_cycled"] is None
    assert row["host_rebooted"] is True and "took_host_down" not in row
    assert not (booted["tmp"] / recovery_base.POWER_CYCLE_CAUSE_CAPABLE_FILE).exists()


@pytest.fixture
def warm_rebooted(booted, monkeypatch):
    """The cycle was refuted (0x0a soft reset): the host rebooted warm, a missed I72 attempt."""
    monkeypatch.setattr(recovery_base, "_last_external_power_cycle", lambda _now: None)
    mech = booted["mech"]
    for attr in (
        "power_cycle_retry",
        "power_cycle_retry_cooldown",
        "_missed_retry_episode",
        "power_cycle_cooldown_state",
    ):
        monkeypatch.setattr(mech, attr, getattr(mech, attr))
    monkeypatch.setattr(mech, "power_cycle_retry", None)
    monkeypatch.setattr(mech, "_missed_retry_episode", "")
    (booted["tmp"] / recovery_base.POWER_CYCLE_CAUSE_CAPABLE_FILE).write_text("{}\n")
    booted["out"]["restart_cause"] = " 0a 0f"
    assert mech.confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "refuted"
    row = _row(booted)
    assert row["host_rebooted"] is True and row["power_cycled"] is False and row["cycle_refuted"] == "soft reset"
    return booted


def test_a_warm_reboot_that_healed_asks_no_further_cycle(warm_rebooted, monkeypatch):
    """Spec 04 I72/I88: the startup check, fabric included, passed after the warm reboot. That
    verified release ends the streak: it is said once, and a later hold arms no retry."""
    s, mech = warm_rebooted, warm_rebooted["mech"]
    monkeypatch.setattr(mech, "_device_hold_episode_since", lambda: "")
    mech.record_verified_release("startup", now_epoch=BOOT_NOW + 60)
    mech.note_mesh_verified(now_epoch=BOOT_NOW + 60)
    healed = [k for n, k in s["hevents"] if n == "power_cycle_miss_healed"]
    assert healed == [{"requested_at": "earlier", "missed": 1, "warm_reboot": True}]
    monkeypatch.setattr(mech, "_device_hold_episode_since", lambda: "held-since-2")
    for now in (BOOT_NOW + 120, BOOT_NOW + 7200):
        assert not mech.power_cycle_retry_due(now)
    assert mech.power_cycle_retry is None


def test_a_warm_reboot_still_held_asks_the_cycle_again_in_the_new_episode(warm_rebooted, monkeypatch):
    """Spec 04 I72/I88: the warm boot's startup check failed, so the device is held in a new episode
    while the retry the old one held was lost with the old broker. The retry is armed for the new
    episode at the miss's until, once, and the governor then allows the cycle."""
    mech = warm_rebooted["mech"]
    monkeypatch.setattr(mech, "_device_hold_episode_since", lambda: "held-since-2")
    until = mech.power_cycle_miss_state(now_epoch=BOOT_NOW)["until"]
    assert not mech.power_cycle_retry_due(until - 1)
    assert mech.power_cycle_retry == (until, "held-since-2")
    assert mech.power_cycle_retry_due(until), "asked again the moment the I72 backoff ends"
    assert mech.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=until + 1)[0]
    mech.clear_power_cycle_retry()
    assert not mech.power_cycle_retry_due(until + 1), "armed once per hold episode"


def _prepend_cycles_that_ran(s, *ats):
    """Real broker power cycles (they took the host down) recorded before the refuted request."""
    path = s["tmp"] / recovery_base.AUTO_RECOVERY_LEDGER
    rows = [
        {
            "action": "power-cycle",
            "reason": REASON,
            "at": "before",
            "at_epoch": at,
            "boot_id": f"boot-0{n}",
            "cycle_checked": "confirmed",
            "took_host_down": True,
        }
        for n, at in enumerate(ats)
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + path.read_text())


@pytest.mark.parametrize(
    "earlier,cause,cooldown",
    [
        # I65: a real broker cycle 600 s before the boot still spaces the next one.
        ((BOOT_NOW - 600,), "broker power-cycle", True),
        # I9: two real cycles plus the refuted one reach the backstop.
        ((BOOT_NOW - 3000, BOOT_NOW - 2000), "power-cycle backstop", False),
    ],
)
def test_a_warm_reboot_still_held_under_a_cooldown_or_the_backstop_waits_for_the_governor(
    warm_rebooted, monkeypatch, earlier, cause, cooldown
):
    """Spec 04 I72/I88: the missed retry is armed in the new episode at the miss's until, the governor
    then refuses the cycle (I65 cooldown or I9 backstop) and its own retry at that wait's end is the
    only one left: the missed retry is never armed again in the episode."""
    mech = warm_rebooted["mech"]
    for attr in ("_power_cycle_backstop_logged", "_power_cycle_backstop_paged", "_ac_power_info"):
        monkeypatch.setattr(mech, attr, getattr(mech, attr))
    _prepend_cycles_that_ran(warm_rebooted, *earlier)
    monkeypatch.setattr(mech, "_device_hold_episode_since", lambda: "held-since-2")
    until = mech.power_cycle_miss_state(now_epoch=BOOT_NOW)["until"]
    assert not mech.power_cycle_retry_due(until - 1)
    assert mech.power_cycle_retry == (until, "held-since-2") and mech.power_cycle_retry_cooldown is False
    assert mech.power_cycle_retry_due(until)

    allowed, why = mech.auto_recovery_allowed("power-cycle", tenant_active=False, now_epoch=until)
    state = mech.power_cycle_cooldown_state
    assert not allowed and why == state["why"] and cause in state["cause"]
    wait_ends = state["until"]
    assert wait_ends > until
    assert mech.power_cycle_retry == (wait_ends, "held-since-2")
    assert mech.power_cycle_retry_cooldown is cooldown

    for now in (until + 1, wait_ends - 1):
        assert not mech.power_cycle_retry_due(now)
        assert mech.power_cycle_retry == (wait_ends, "held-since-2"), "the missed retry is not armed again"
        assert mech.power_cycle_retry_cooldown is cooldown
    assert mech.power_cycle_retry_due(wait_ends), "the governor's retry at the wait's end"
    mech.clear_power_cycle_retry()
    assert not mech.power_cycle_retry_due(wait_ends + 1)
    assert mech.power_cycle_retry is None


def test_the_missed_retry_leaves_a_cooldown_retry_of_the_same_episode(warm_rebooted, monkeypatch):
    """Spec 04 I72/I88: a retry the governor already set in this episode (a cooldown) is kept, its
    cooldown flag too; the missed retry never replaces it with an earlier one."""
    mech = warm_rebooted["mech"]
    monkeypatch.setattr(mech, "_device_hold_episode_since", lambda: "held-since-2")
    until = mech.power_cycle_miss_state(now_epoch=BOOT_NOW)["until"]
    later = until + 1800
    mech.power_cycle_retry = (later, "held-since-2")
    mech.power_cycle_retry_cooldown = True
    mech._arm_missed_power_cycle_retry(BOOT_NOW)
    assert mech.power_cycle_retry == (later, "held-since-2") and mech.power_cycle_retry_cooldown is True
    assert not mech.power_cycle_retry_due(until)
    assert mech.power_cycle_retry == (later, "held-since-2") and mech.power_cycle_retry_cooldown is True


def test_a_bmc_that_never_reports_a_cause_leaves_the_cycle_unconfirmed_and_counted(booted):
    """Measured on a Galaxy BMC: `chassis restart_cause` answers "Unspecified error". The row still
    counts as a cycle (the boot-loop-safe side); the event says it was not confirmed."""
    booted["rc"]["restart_cause"] = 1
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "unconfirmed"
    row = _row(booted)
    assert row["cycle_checked"] == "unconfirmed" and "took_host_down" not in row
    assert row["host_rebooted"] is True and row["power_cycled"] is None
    assert any(n == "power_cycle_unconfirmed" for n, _ in booted["hevents"])


def test_unknown_from_a_bmc_never_seen_to_report_a_cause_is_unconfirmed(booted):
    booted["out"]["restart_cause"] = " 00 0f"
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "unconfirmed"


def test_a_boot_long_after_the_request_is_not_checked(booted, monkeypatch):
    monkeypatch.setattr(booted["mech"], "_boot_btime_id", lambda: str(int(BOOT_NOW + 7200)))
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW + 7300) is None
    assert booted["calls"] == []


# F3: the I27 contract on both paths.


def test_the_hooks_worst_case_fits_systemd_shutdowns_hook_timeout():
    assert pc.hook_worst_case_sec() < pc.SYSTEMD_SHUTDOWN_HOOK_TIMEOUT_SEC
    text = HOOK.read_text()
    assert f"timeout {pc.HOOK_MDADM_WAIT_SEC} mdadm" in text
    assert f'timeout {pc.HOOK_IPMI_PREPARE_SEC} "$0" --prepare-ipmi &' in text
    # The IPMI preparation runs beside the RAID wait, never before or after it.
    assert text.index("--prepare-ipmi &") < text.index("mdadm --wait-clean") < text.index('wait "$ipmi_pid"')
    assert f'timeout {pc.HOOK_SEL_TIMEOUT_SEC} "$@" raw' in text
    assert f"-le {pc.HOOK_TRY_TIMEOUT_MAX_SEC} ] || try_timeout={pc.HOOK_TRY_TIMEOUT_MAX_SEC}" in text
    assert f"-ge {pc.HOOK_TRY_TIMEOUT_MIN_SEC} ] || try_timeout={pc.HOOK_TRY_TIMEOUT_MIN_SEC}" in text
    assert f"-le {pc.HOOK_TRIES} ]" in text and f"sleep {pc.HOOK_RETRY_PAUSE_SEC}\n" in text
    assert f"sleep {pc.HOOK_POWER_DROP_WAIT_SEC}\n" in text


@pytest.mark.parametrize("configured,want", [(120, 15), (8, 8), (1, 5), ("x", 15)])
def test_the_per_try_timeout_honours_the_configured_one_within_the_hook_budget(configured, want):
    assert pc.hook_try_timeout_sec(configured) == want


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
    # The IPMI node and its sysfs entry live in tmp_path; by default the node is there (a link to
    # /dev/null, a character device) and modprobe and mknod are stubs that never touch the host.
    dev, sysfs = tmp_path / "dev", tmp_path / "sys-class-ipmi"
    dev.mkdir()
    sysfs.mkdir()
    (dev / "ipmi0").symlink_to("/dev/null")
    (stubs / "modprobe").write_text(
        "#!/bin/sh\n"
        f'echo "modprobe $*" >> "{trace}"\n'
        '[ -n "$MODPROBE_LOADS" ] && [ "$2" = ipmi_si ] || exit "${MODPROBE_RC:-0}"\n'
        f'mkdir -p "{sysfs}/ipmi0" && echo 238:0 > "{sysfs}/ipmi0/dev"\n'
    )
    (stubs / "mknod").write_text(f'#!/bin/sh\necho "mknod $*" >> "{trace}"\nln -s /dev/null "$3"\n')
    for name in ("modprobe", "mknod"):
        (stubs / name).chmod(0o755)
    cycle = tmp_path / "cycle"
    cycle.write_text(f'#!/bin/sh\necho "cycle $*" >> "{trace}"\nexit "${{CYCLE_RC:-0}}"\n')
    cycle.chmod(0o755)
    # An in-band ipmitool stand-in: records its argv, fails `chassis power cycle` with CYCLE_RC.
    ipmitool = tmp_path / "ipmitool"
    ipmitool.write_text(
        f'#!/bin/sh\necho "ipmitool $*" >> "{trace}"\n'
        'case "$*" in *"chassis power cycle") exit "${CYCLE_RC:-0}" ;; esac\n'
    )
    ipmitool.chmod(0o755)
    req = tmp_path / "req"
    last = {}

    def run(verb, request=None, cycle_rc=0, **extra_env):
        if request is not None:
            req.write_text(request)
        env = {
            "PATH": f"{stubs}:/usr/bin:/bin",
            "TT_DEVICE_MCP_POWER_CYCLE_REQUEST": str(req),
            "TT_DEVICE_MCP_IPMI_DEV": str(dev),
            "TT_DEVICE_MCP_IPMI_SYSFS": str(sysfs),
            "CYCLE_RC": str(cycle_rc),
            "HOOK_VERB": verb,
            **extra_env,
        }
        argv = ["/bin/sh", "-c", 'set -- "$HOOK_VERB"; . "$0"', str(HOOK)]
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        last["stderr"] = r.stderr
        return r.returncode, [t.rstrip() for t in trace.read_text().splitlines()] if trace.exists() else []

    return {
        "run": run,
        "cycle": str(cycle),
        "ipmitool": str(ipmitool),
        "stubs": stubs,
        "trace": trace,
        "dev": dev,
        "last": last,
        "pid_file": tmp_path / "req.fallback.pid",
    }


def _request(argv, age=0):
    return pc.request_text(argv, REASON, time.time() - age)


def test_hook_fires_the_requested_cycle_after_flushing_on_reboot(hook):
    rc, trace = hook["run"]("reboot", _request([hook["cycle"], "chassis power", "cycle"]))
    assert rc == 0
    assert "sync" in trace and any(t.startswith("mdadm --wait-clean") for t in trace)
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


def _no_sleeps(trace):
    return [t for t in trace if not t.startswith("sleep 0.2")]


def test_hook_loads_the_ipmi_drivers_and_makes_the_node_before_an_in_band_cycle(hook):
    """openipmi's stop unloaded ipmi_si/ipmi_devintf and deleted /dev/ipmi0 before the hook ran: the
    hook loads them again and makes the node from sysfs, and only then fires the cycle."""
    (hook["dev"] / "ipmi0").unlink()
    rc, trace = hook["run"]("reboot", _request([hook["ipmitool"], "chassis", "power", "cycle"]), MODPROBE_LOADS="1")
    assert rc == 0
    trace = _no_sleeps(trace)
    loads = [t for t in trace if t.startswith("modprobe")]
    assert loads == [f"modprobe -q {m}" for m in ("ipmi_msghandler", "ipmi_devintf", "ipmi_si")]
    mknod = trace.index(f"mknod -m 0600 {hook['dev']}/ipmi0 c 238 0")
    assert trace.index(loads[-1]) < mknod < trace.index("ipmitool chassis power cycle")
    assert "loaded the IPMI drivers again" in hook["last"]["stderr"]


def test_hook_leaves_loaded_drivers_alone(hook):
    rc, trace = hook["run"]("reboot", _request([hook["cycle"]]))
    assert rc == 0 and not [t for t in trace if t.startswith(("modprobe", "mknod"))]


def test_hook_with_no_ipmi_device_says_so_loudly_and_still_tries(hook):
    """No driver loads and no node appears: the hook gives up within its bound, says so, still tries
    the command (it may be out-of-band) and, when every try fails, says the reboot goes on warm."""
    (hook["dev"] / "ipmi0").unlink()
    rc, trace = hook["run"](
        "reboot", _request([hook["ipmitool"], "chassis", "power", "cycle"]), cycle_rc=1, MODPROBE_RC="1"
    )
    assert rc == 1
    assert len([t for t in trace if t.startswith("sleep 0.2")]) == 49, "the preparation is bounded on its own"
    assert [t for t in trace if t.endswith("chassis power cycle")] == ["ipmitool chassis power cycle"] * 3
    err = hook["last"]["stderr"]
    assert "NO IPMI DEVICE" in err and "POWER CYCLE FAILED" in err and "WARM" in err


def _sel_records(trace, prefix):
    """The OEM SEL records the hook added, as (timestamp, stage, detail)."""
    out = []
    for t in trace:
        if t.startswith(prefix + " raw 0x0a 0x44 "):
            b = t[len(prefix) + 1 :].split()[3:]
            assert b[:3] == ["0x00", "0x00", "0xc0"] and b[7:14] == ["0x00"] * 3 + ["0x54", "0x54", "0x50", "0x43"]
            assert len(b) == 16
            out.append((sum(int(x) << (8 * i) for i, x in enumerate(b[3:7])), b[14], b[15]))
    return out


def test_hook_records_the_cycle_in_the_bmc_sel_on_the_cycles_own_bmc(hook):
    """The trace that outlives the reboot: an OEM SEL record before the cycle, through the same
    ipmitool options as the cycle command, saying whether the hook had to restore the IPMI device."""
    (hook["dev"] / "ipmi0").unlink()
    argv = [hook["ipmitool"], "-I", "open", "chassis", "power", "cycle"]
    rc, trace = hook["run"]("reboot", _request(argv), MODPROBE_LOADS="1")
    assert rc == 0
    recs = _sel_records(trace, "ipmitool -I open")
    assert [(s, d) for _, s, d in recs] == [("0x01", "1")]
    assert abs(recs[0][0] - time.time()) < 60
    sel = next(i for i, t in enumerate(trace) if " raw 0x0a 0x44 " in t)
    assert sel < trace.index("ipmitool -I open chassis power cycle")


def test_hook_records_a_cycle_that_failed_every_try_in_the_sel(hook):
    rc, trace = hook["run"]("reboot", _request([hook["ipmitool"], "chassis", "power", "cycle"]), cycle_rc=1)
    assert rc == 1
    assert [(s, d) for _, s, d in _sel_records(trace, "ipmitool")] == [("0x01", "0"), ("0x02", "3")]
    assert [i for i, t in enumerate(trace) if " raw " in t][-1] > max(
        i for i, t in enumerate(trace) if t.endswith("chassis power cycle")
    )


def test_hook_adds_no_sel_record_for_an_opaque_command(hook):
    rc, trace = hook["run"]("reboot", _request([hook["cycle"], "chassis", "power", "cycle"]))
    assert rc == 0 and not [t for t in trace if " raw " in t]


def _gone(pid):
    """True once ``pid`` has exited (gone, or a zombie no one reaped yet)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except (FileNotFoundError, ProcessLookupError):
        return True


def _children(pid):
    out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


@pytest.mark.parametrize("fired", [False, True], ids=["waiting-out-the-bound", "cycle-command-running"])
def test_hook_stops_the_fallback_and_its_children_before_any_wait(hook, tmp_path, fired):
    """The fallback, run for real: still waiting out its bound, or past it, as `timeout` with the cycle
    command as its child. The hook stops it and every process it started before the RAID wait (the
    stub mdadm sees none of them alive), so no orphan can cut power alongside the hook's own tries.
    A SIGKILL is delivered at once but the kernel tears the process down after kill(2) returns, so a
    single look can still see a killed process: the stub waits up to 5 s for the processes to go,
    while the hook is held in the RAID wait, so only what the hook did before that wait can end them
    (none of them exits on its own within 60 s)."""
    pidf, proc = _run_fallback(tmp_path, 0 if fired else 300, ["/bin/sleep", "60"], timeout=60)
    kids = []
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pidf.exists() and pidf.read_text().strip():
                kids = _children(proc.pid)
                if not fired or (kids and _cmdline(proc.pid)[0] == pc.FALLBACK_FIRE_ARGV0.encode()):
                    break
            time.sleep(0.05)
        assert not fired or kids, "the fallback's cycle command never started"
        hook["pid_file"].write_text(pidf.read_text())
        watched = " ".join(str(p) for p in [proc.pid, *kids])
        mdadm = hook["stubs"] / "mdadm"
        mdadm.write_text(
            "#!/bin/sh\n"
            "end=$(($(date +%s) + 5))\n"
            'while n=$(ps -o stat= -p "$WATCHED" | grep -vc Z); [ "$n" -gt 0 ] && [ "$(date +%s)" -lt "$end" ]; do\n'
            "    /bin/sleep 0.05\n"
            "done\n"
            f'echo "mdadm alive:$n" >> "{hook["trace"]}"\n'
        )
        rc, trace = hook["run"]("reboot", _request([hook["cycle"]]), WATCHED=watched)
        assert rc == 0
        assert "mdadm alive:0" in trace, "the fallback or a child of it outlived the stop into the RAID wait"
        assert proc.wait(timeout=5) == -9
        deadline = time.monotonic() + 5
        while not all(_gone(k) for k in kids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert all(_gone(k) for k in kids), "a child of the fallback survived it"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_hook_stops_the_fallback_first_and_kills_its_process_group():
    """Static order: the fallback is stopped before the flush and the RAID wait, never alongside them,
    and the stop takes the process group the fallback leads, not just its pid."""
    text = HOOK.read_text()
    stop = text.index('stop_fallback "$fpid"')
    assert stop < text.index("sync &") < text.index("mdadm --wait-clean")
    assert stop < text.index('timeout "$try_timeout" "$@"')
    body = text[text.index("stop_fallback() {") :]
    body = body[: body.index("\n}\n")]
    assert "kill -STOP $gen" in body and 'kill -9 "-$1"' in body and "kill -9 $tree" in body


def test_the_installer_and_autoupdate_both_deploy_the_hook_with_no_hand_step():
    """Both the installer and every auto-update run apply-host-config.sh, which installs the hook
    (and with it the IPMI preparation the fallback unit calls) fail-closed under set -e."""
    apply = APPLY.read_text()
    assert "set -euo pipefail" in apply
    line = next(t for t in apply.splitlines() if t.startswith("install -D -m 0755") and HOOK.name in t)
    assert "||" not in line, "the hook install must stay fatal"
    for script in ("install-tt-device-broker.sh", "tt-device-autoupdate.sh"):
        text = (REPO / "deploy" / script).read_text()
        assert 'deploy/apply-host-config.sh" "$' in text, script


def test_the_installer_installs_the_hook_where_the_broker_looks():
    text = APPLY.read_text()
    assert f'"$DEPLOY/{HOOK.name}" /usr/lib/systemd/system-shutdown/{pc.SHUTDOWN_HOOK_NAME}' in text
    assert HOOK.name == pc.SHUTDOWN_HOOK_NAME and os.access(HOOK, os.X_OK)
    assert "/usr/lib/systemd/system-shutdown" in pc.SHUTDOWN_HOOK_DIRS
    assert pc.REQUEST_FILE == "/run/tt-device-mcp-power-cycle.request"
    assert f":-{pc.REQUEST_FILE}}}" in HOOK.read_text()
