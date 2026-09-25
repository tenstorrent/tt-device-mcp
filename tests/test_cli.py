# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for CLI commands."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Get the src directory path for PYTHONPATH
SRC_PATH = str(Path(__file__).parent.parent / "src")

# Use a port that's unlikely to be in use for testing
TEST_PORT = 19999


@pytest.fixture
def temp_log_dir(tmp_path):
    """Provide a temporary directory for daemon state files."""
    return tmp_path


def run_cli(*args, log_dir: str | None = None):
    """Run CLI command with proper PYTHONPATH and test port."""
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_PATH + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "tt_device_mcp.cli", "--port", str(TEST_PORT)]
    if log_dir:
        cmd.extend(["--log-dir", str(log_dir)])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )


def test_help_subcommand_matches_full_help():
    """`tt-device-mcp help` must print the same help as `--help` (the lock banner
    points users at it)."""
    assert run_cli("help").stdout == run_cli("--help").stdout


def test_cli_daemon_status_not_running(temp_log_dir):
    """Test daemon status command when server is not running."""
    result = run_cli("daemon", "status", log_dir=temp_log_dir)
    # Should return 1 when not running
    assert result.returncode == 1
    assert "not running" in result.stdout.lower()


def test_cli_daemon_stop_not_running(temp_log_dir):
    """Test daemon stop command when server is not running."""
    result = run_cli("daemon", "stop", log_dir=temp_log_dir)
    # Should return 0 even when not running
    assert result.returncode == 0


def test_cli_commands_exist():
    """Test that all expected commands are available."""
    result = run_cli("--help")
    assert result.returncode == 0

    # Check for main commands
    expected_commands = ["daemon", "run", "exec", "run-bg", "status", "logs", "kill", "wait", "watch"]
    for cmd in expected_commands:
        assert cmd in result.stdout, f"Command '{cmd}' not found in help output"


def test_cli_daemon_subcommands():
    """Test that daemon subcommands exist."""
    result = run_cli("daemon", "--help")
    assert result.returncode == 0

    expected = ["start", "stop", "status", "start-fg"]
    for cmd in expected:
        assert cmd in result.stdout, f"Daemon subcommand '{cmd}' not found"


def test_cli_run_requires_daemon(temp_log_dir, monkeypatch):
    """Test that run command requires daemon to be running."""
    # Isolate from any real host broker socket (auto-discovered otherwise) so the
    # no-daemon path is exercised deterministically.
    monkeypatch.setenv("TT_DEVICE_MCP_SOCKET", "/nonexistent/tt-device-mcp-test.sock")
    result = run_cli("run", "echo test", log_dir=temp_log_dir)
    assert result.returncode == 1
    assert "not running" in result.stdout.lower() or "daemon" in result.stdout.lower()


# ---- smi-ro wrapper install/remove (the NOPASSWD read-only tt-smi helper that
#      `lock` installs so `tt-device-mcp smi` works on a locked host) ----


def _load_cli():
    """Import tt_device_mcp.cli in-process so we can drive its helpers directly."""
    if SRC_PATH not in sys.path:
        sys.path.insert(0, SRC_PATH)
    import tt_device_mcp.cli as cli

    return cli


def test_is_daemon_running_treats_a_degraded_broker_as_reachable(monkeypatch):
    """A held/degraded device leaves the broker up and serving. Reachability must count it as
    running, or a held box would refuse every CLI command against a perfectly-live broker."""
    cli = _load_cli()
    monkeypatch.setattr(cli, "api_call", lambda *a, **k: {"status": "degraded", "held": True})
    assert cli.is_daemon_running(1234) is True


def test_install_smi_ro_wrapper_creates_wrapper_and_sudoers(tmp_path, monkeypatch):
    """`_install_smi_ro_wrapper` writes the root-only (0700) wrapper and a visudo-checked
    owner-only (0400) sudoers entry. chown/visudo are stubbed so the test needs no root."""
    cli = _load_cli()
    wrapper = tmp_path / "tt-device-mcp-smi-ro"
    sudoers = tmp_path / "sudoers.d_tt-device-mcp-smi-ro"
    monkeypatch.setattr(cli, "_SMI_RO_WRAPPER", str(wrapper))
    monkeypatch.setattr(cli, "_SMI_RO_SUDOERS", str(sudoers))
    monkeypatch.setattr(cli.os, "chown", lambda *a, **k: None)  # no root in CI

    real_run = cli.subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "visudo":
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli._install_smi_ro_wrapper()

    assert wrapper.exists(), "wrapper not installed"
    body = wrapper.read_text()
    assert body.startswith("#!/bin/sh")
    assert "refusing non-read-only flag" in body
    assert wrapper.stat().st_mode & 0o777 == 0o700, "root-only: it is only ever run via sudo"
    assert sudoers.exists(), "sudoers entry not installed"
    assert "NOPASSWD" in sudoers.read_text()
    assert sudoers.stat().st_mode & 0o777 == 0o400, "owner-only: sudo reads it as root"


def test_install_smi_ro_wrapper_skips_sudoers_on_visudo_failure(tmp_path, monkeypatch):
    """If visudo rejects the sudoers file, we must NOT install it (no broken
    /etc/sudoers.d that could lock everyone out of sudo)."""
    cli = _load_cli()
    wrapper = tmp_path / "tt-device-mcp-smi-ro"
    sudoers = tmp_path / "sudoers.d_tt-device-mcp-smi-ro"
    monkeypatch.setattr(cli, "_SMI_RO_WRAPPER", str(wrapper))
    monkeypatch.setattr(cli, "_SMI_RO_SUDOERS", str(sudoers))
    monkeypatch.setattr(cli.os, "chown", lambda *a, **k: None)

    real_run = cli.subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "visudo":
            return subprocess.CompletedProcess(cmd, 1, b"", b"parse error")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli._install_smi_ro_wrapper()

    assert wrapper.exists(), "wrapper should still install"
    assert not sudoers.exists(), "invalid sudoers must not be installed"


def test_remove_smi_ro_wrapper_is_idempotent(tmp_path, monkeypatch):
    """`_remove_smi_ro_wrapper` deletes both files and is safe to call twice."""
    cli = _load_cli()
    wrapper = tmp_path / "tt-device-mcp-smi-ro"
    sudoers = tmp_path / "sudoers.d_tt-device-mcp-smi-ro"
    wrapper.write_text("#!/bin/sh\n")
    sudoers.write_text("ALL ALL=(root) NOPASSWD: x\n")
    monkeypatch.setattr(cli, "_SMI_RO_WRAPPER", str(wrapper))
    monkeypatch.setattr(cli, "_SMI_RO_SUDOERS", str(sudoers))

    cli._remove_smi_ro_wrapper()
    assert not wrapper.exists()
    assert not sudoers.exists()
    cli._remove_smi_ro_wrapper()  # second call must not raise


def test_smi_ro_wrapper_rejects_reset_flag(tmp_path):
    """The wrapper runs as root via NOPASSWD sudo, so it must reject non-read-only
    flags (e.g. -r) itself, before exec'ing tt-smi."""
    cli = _load_cli()
    wrapper = tmp_path / "tt-device-mcp-smi-ro"
    wrapper.write_text(cli._SMI_RO_WRAPPER_BODY)
    wrapper.chmod(0o755)

    res = subprocess.run([str(wrapper), "-r", "0"], capture_output=True, text=True)
    assert res.returncode == 2
    assert "refusing non-read-only flag" in res.stderr


def test_device_nodes_ignores_the_by_id_directory(monkeypatch, tmp_path):
    """/dev/tenstorrent holds a `by-id/` dir (0755 root:root) alongside the device
    nodes. Sampling it for permissions reads as 'unlocked' on a locked host, which
    sent `smi` down the bare-metal path and printed 'No Tenstorrent devices'."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    dev = tmp_path / "tenstorrent"
    dev.mkdir()
    (dev / "by-id").mkdir()  # the decoy — an unsorted glob can return it first
    for i in (0, 1, 15):
        (dev / str(i)).write_text("")
    monkeypatch.setattr(cli, "_TT_DEV_GLOB", str(dev / "*"))

    nodes = cli.device_nodes()
    assert len(nodes) == 3
    assert not any(n.endswith("by-id") for n in nodes)
    assert all(os.path.basename(n).isdigit() for n in nodes)


def test_timezone_offset_and_label(monkeypatch, tmp_path):
    """An explicit offset is a fixed UTC zone and the label is what the TIME column
    header shows; the timestamp renders as 'dd hh:mm:ss' in that zone."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    monkeypatch.setenv("TT_DEVICE_MCP_TZ", "1")
    cli._resolve_tz.cache_clear()
    _, label = cli._resolve_tz()
    assert label == "UTC+1"
    # 2026-06-25T12:00:00Z -> 13:00:00 on the 25th at UTC+1
    assert cli._fmt_when("2026-06-25T12:00:00") == "25 13:00:00"

    monkeypatch.setenv("TT_DEVICE_MCP_TZ", "-8")
    cli._resolve_tz.cache_clear()
    _, label = cli._resolve_tz()
    assert label == "UTC-8"
    assert cli._fmt_when("2026-06-25T12:00:00") == "25 04:00:00"
    cli._resolve_tz.cache_clear()


def test_timezone_falls_back_to_utc_without_a_tz_database(monkeypatch):
    """A slim container image can ship no tzdata, and then even the default zone raises.
    The CLI must still render times — a status listing is how an operator sees the queue."""
    sys.path.insert(0, SRC_PATH)
    import zoneinfo

    from tt_device_mcp import cli

    def no_tzdata(_key):
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key")

    monkeypatch.delenv("TT_DEVICE_MCP_TZ", raising=False)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", no_tzdata)
    cli._resolve_tz.cache_clear()
    tz, label = cli._resolve_tz()
    assert label == "UTC"
    assert cli._fmt_when("2026-06-25T12:00:00") == "25 12:00:00"
    cli._resolve_tz.cache_clear()


def test_timezone_default_is_pacific_dst_aware(monkeypatch):
    """Default is a zone, not a fixed -8, so it stays right across DST. The label is
    the city, which doesn't flip with the season."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    monkeypatch.delenv("TT_DEVICE_MCP_TZ", raising=False)
    monkeypatch.setattr(cli, "_tz_config_path", lambda: Path("/nonexistent/tz"))
    cli._resolve_tz.cache_clear()
    tz, label = cli._resolve_tz()
    assert label == "Los Angeles"
    # Same zone, both sides of the DST boundary -> different offsets, same label.
    from datetime import datetime

    assert datetime(2026, 1, 15, tzinfo=tz).utcoffset().total_seconds() == -8 * 3600
    assert datetime(2026, 7, 15, tzinfo=tz).utcoffset().total_seconds() == -7 * 3600
    cli._resolve_tz.cache_clear()


def test_canonical_city_names_are_unique():
    """The invariant the city-name UX rests on: every canonical zone has exactly one
    official Area/City id, and no two share a city. (The collisions in
    available_timezones() are all backward-compat links, which are pruned.)"""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    zones = cli.canonical_zones()
    cities = [z.rsplit("/", 1)[-1] for z in zones]
    assert len(cities) == len(set(cities)), "a city name maps to >1 canonical zone"
    assert len(zones) > 200


def test_common_zone_shortlist_is_real_and_small():
    """The bare `--list` is a curated shortlist (the full table is 312 rows). Every
    entry must be a real canonical zone, or the list would offer something unsettable."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    canon = set(cli.canonical_zones())
    assert cli._COMMON_ZONES, "shortlist is empty"
    assert len(cli._COMMON_ZONES) < 60, "shortlist has grown into a phone book"
    for z in cli._COMMON_ZONES:
        assert z in canon, f"{z} is not a canonical zone"
    assert cli.zone_offsets("America/Los_Angeles") == "UTC-8/-7"  # DST zone
    assert cli.zone_offsets("Asia/Tokyo") == "UTC+9"  # no DST
    assert cli.zone_offsets("Asia/Kolkata") == "UTC+5:30"  # half-hour


def test_zone_from_name_accepts_city_and_rejects_non_zones():
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    assert cli.zone_from_name("Los_Angeles") == "America/Los_Angeles"
    assert cli.zone_from_name("Belgrade") == "Europe/Belgrade"
    assert cli.zone_from_name("America/New_York") == "America/New_York"  # full name too

    # Not real zones: a header reading "localtime" is meaningless, and the legacy
    # aliases are what made bare city names collide.
    for bad in ("localtime", "Factory", "US/Pacific", "Nowhereville"):
        with pytest.raises(ValueError):
            cli.zone_from_name(bad)


def test_timezone_zone_name_label_is_the_city(monkeypatch):
    """A saved zone renders as the city in the column header."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    monkeypatch.setenv("TT_DEVICE_MCP_TZ", "Europe/Belgrade")
    cli._resolve_tz.cache_clear()
    _, label = cli._resolve_tz()
    assert label == "Belgrade"
    cli._resolve_tz.cache_clear()


def test_server_down_message_distinguishes_wedged_from_absent(monkeypatch, tmp_path):
    """Wedged (socket exists) vs absent must read differently so the user knows
    whether to wait/escalate or to start a daemon."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    sock = tmp_path / "broker.sock"
    sock.write_text("")
    monkeypatch.setattr(cli, "resolve_socket", lambda *a, **k: str(sock))
    assert "isn't responding" in cli._server_down_message()

    monkeypatch.setattr(cli, "resolve_socket", lambda *a, **k: None)
    assert "daemon start" in cli._server_down_message()


def test_refresh_banner_tracks_lock_state(monkeypatch, tmp_path):
    """The banner must reconcile with the lock state (written when locked, removed
    when not) so a text change lands on update without re-locking."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    banner = tmp_path / "banner.sh"
    monkeypatch.setattr(cli, "_LOCK_BANNER", str(banner))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    class Args:
        pass

    monkeypatch.setattr(cli, "_device_locked", lambda: True)
    assert cli.cmd_refresh_banner(Args()) == 0
    assert banner.read_text() == cli._LOCK_BANNER_TEXT

    monkeypatch.setattr(cli, "_device_locked", lambda: False)
    assert cli.cmd_refresh_banner(Args()) == 0
    assert not banner.exists()


def _stub_lock_env(monkeypatch, cli, tmp_path, deny_rc):
    """Wire cmd_lock's side effects to tmp paths / no-ops and drive both self-checks
    through a fake subprocess.run: the admitted (gid=ttdev) probe always opens, the
    non-ttdev deny probe returns ``deny_rc``. Returns the list of issued argv lists."""
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "_require_root_broker", lambda: True)
    monkeypatch.setattr(cli, "_set_service_device_group", lambda enable: None)
    monkeypatch.setattr(cli, "_install_smi_ro_wrapper", lambda: None)
    monkeypatch.setattr(cli, "_remove_smi_ro_wrapper", lambda: None)
    monkeypatch.setattr(cli, "device_nodes", lambda: ["/dev/tenstorrent/0"])
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    monkeypatch.setattr(cli, "_UDEV_RULE_PATH", str(tmp_path / "99-ttdev.rules"))
    monkeypatch.setattr(cli, "_LOCK_BANNER", str(tmp_path / "banner.sh"))

    issued = []

    def fake_run(cmd, *a, **k):
        issued.append(cmd)
        if cmd[:1] == ["systemd-run"]:
            # gid=ttdev present -> the admitted-job probe; otherwise the deny probe.
            if f"--gid={cli._LOCK_GROUP}" in cmd:
                return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=deny_rc)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return issued


def test_cmd_lock_refuses_when_deny_probe_opens_the_node(monkeypatch, tmp_path, capsys):
    """A non-ttdev open that SUCCEEDS means the udev rule never took: the node is still
    world-open. cmd_lock must not print LOCKED — that would advertise a lock that denies
    nobody (silent fail-open). It reverts and returns non-zero."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    issued = _stub_lock_env(monkeypatch, cli, tmp_path, deny_rc=0)  # 0 == opened -> leak

    rc = cli.cmd_lock(object())
    out = capsys.readouterr().out

    assert rc != 0
    # The success line ("LOCKED: ...; bare-metal denied.") must not print — and its
    # sentinel isn't the "UNLOCKED:" the revert emits, so match the exact phrase.
    assert "bare-metal denied." not in out
    assert "not denied" in out.lower() or "did not take effect" in out.lower()
    # The deny probe (a systemd-run WITHOUT --gid=ttdev) must actually have been issued.
    assert any(c[:1] == ["systemd-run"] and f"--gid={cli._LOCK_GROUP}" not in c for c in issued)


def test_cmd_lock_locks_when_deny_probe_is_refused(monkeypatch, tmp_path, capsys):
    """The other side of the gate: when the non-ttdev probe is DENIED (exit 13) the lock
    is proven real, so cmd_lock prints LOCKED and returns 0 — the fix must not over-block
    a genuinely-locked node."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    _stub_lock_env(monkeypatch, cli, tmp_path, deny_rc=13)  # 13 == EACCES -> denied

    rc = cli.cmd_lock(object())
    out = capsys.readouterr().out

    assert rc == 0
    assert "LOCKED:" in out


def test_daemon_start_refuses_on_broker_host(monkeypatch, tmp_path):
    """A per-user daemon is shadowed by the system broker, so `daemon start` must
    no-op (not spawn another server) when a healthy broker socket is present."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    bsock = tmp_path / "broker.sock"
    bsock.write_text("")
    monkeypatch.setattr(cli, "DEFAULT_SOCKET", str(bsock))
    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)

    class Args:
        log_dir = None
        port = 9

    assert cli.cmd_daemon_start(Args()) == 0


def test_main_bare_piped_stdin_runs_stdio_adapter(monkeypatch):
    """One binary: bare invocation with a piped (non-tty) stdin is an MCP client
    attaching — main routes to the stdio adapter, not the CLI."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli, stdio_shim

    called = {}
    monkeypatch.setattr(stdio_shim, "serve_stdio", lambda *a, **k: called.setdefault("hit", True))
    monkeypatch.setattr(sys, "argv", ["tt-device-mcp"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert cli.main() == 0
    assert called.get("hit") is True


def test_main_bare_tty_shows_help_not_adapter(monkeypatch):
    """Bare invocation on a tty is a human — show help, never the stdio adapter."""
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli, stdio_shim

    called = {}
    monkeypatch.setattr(stdio_shim, "serve_stdio", lambda *a, **k: called.setdefault("hit", True))
    monkeypatch.setattr(sys, "argv", ["tt-device-mcp"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    assert cli.main() == 1  # no subcommand -> help
    assert "hit" not in called


def _step_args(**over):
    """The global flags every command reads, plus this command's own."""
    import argparse

    base = {"port": 0, "log_dir": None, "exit_code": 0, "no_reclaim": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_step_commands_exist():
    """A Slurm prologue and epilogue call these by name; a rename is a silent site breakage."""
    from tt_device_mcp import cli

    assert hasattr(cli, "cmd_pre_step"), "cmd_pre_step is missing"
    assert hasattr(cli, "cmd_post_step"), "cmd_post_step is missing"


def test_pre_step_exits_zero_only_when_the_device_is_fit_and_free(monkeypatch):
    """Binary contract: Slurm reads one bit. A non-zero prologue drains the node and requeues the
    job, so the bit has to mean exactly 'fit and free'."""
    from tt_device_mcp import cli

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    monkeypatch.setattr(cli, "mcp_tool_call", lambda *a, **k: {"status": "ok", "ok": True, "reason": ""})
    assert cli.cmd_pre_step(_step_args()) == 0

    monkeypatch.setattr(
        cli, "mcp_tool_call", lambda *a, **k: {"status": "unfit", "ok": False, "reason": "chip 3 off the bus"}
    )
    assert cli.cmd_pre_step(_step_args()) == 1


def test_post_step_sends_the_exit_code_and_reclaim_flag(monkeypatch):
    """`--exit-code` is how the driver tells the broker the step failed; it is what forces the
    fabric traffic pass, and the CLI is the only thing that can carry it."""
    from tt_device_mcp import cli

    sent = {}

    def fake_call(port, tool, arguments, **kwargs):
        sent["tool"] = tool
        sent["arguments"] = arguments
        return {"status": "ok", "ok": True, "reason": ""}

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    monkeypatch.setattr(cli, "mcp_tool_call", fake_call)

    rc = cli.cmd_post_step(_step_args(exit_code=137, no_reclaim=True))
    assert rc == 0
    assert sent["tool"] == "tt_device_post_step", sent["tool"]
    assert sent["arguments"]["exit_code"] == 137, sent["arguments"]
    assert sent["arguments"]["reclaim"] is False, sent["arguments"]


def test_a_step_refusal_exits_non_zero(monkeypatch):
    """A refusal is not a fit device. Reporting 0 would dispatch a job onto an unproven mesh."""
    from tt_device_mcp import cli

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    monkeypatch.setattr(
        cli, "mcp_tool_call", lambda *a, **k: {"status": "refused", "ok": False, "reason": "must be driven by root"}
    )
    assert cli.cmd_pre_step(_step_args()) == 1
    assert cli.cmd_post_step(_step_args()) == 1


def test_a_step_client_timeout_tracks_the_server_deadline_env_var(monkeypatch):
    """cli.py:988/:1006 used to hardcode 180/660 independent of the server's own deadline
    (server.py's TT_DEVICE_MCP_{PRE,POST}_STEP_DEADLINE_SEC). A site that raises the server
    deadline past a hardcoded client timeout gets a transport failure reported for a step the
    broker is still resolving — the client timeout must be derived from the same env var plus
    the fixed margin, so raising one raises the other."""
    from tt_device_mcp import cli
    from tt_device_mcp.constants import STEP_CLIENT_TIMEOUT_MARGIN_SEC

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    seen = {}

    def fake_call(port, tool, arguments, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return {"status": "ok", "ok": True, "reason": ""}

    monkeypatch.setattr(cli, "mcp_tool_call", fake_call)

    monkeypatch.setenv("TT_DEVICE_MCP_PRE_STEP_DEADLINE_SEC", "900")
    cli.cmd_pre_step(_step_args())
    assert seen["timeout"] == 900 + STEP_CLIENT_TIMEOUT_MARGIN_SEC, seen["timeout"]

    monkeypatch.setenv("TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC", "1000")
    cli.cmd_post_step(_step_args())
    assert seen["timeout"] == 1000 + STEP_CLIENT_TIMEOUT_MARGIN_SEC, seen["timeout"]


def test_a_non_finite_step_deadline_falls_back_to_the_default(monkeypatch):
    """float("inf") > 0 is True, so a bare positivity check accepts an infinite deadline --
    disabling the server's own step timeout and, through step_client_timeout_sec, the CLI's
    socket timeout with it. Both must fall back to the default exactly like a malformed or
    non-positive value already does."""
    from tt_device_mcp.constants import (
        POST_STEP_DEADLINE_DEFAULT_SEC,
        POST_STEP_DEADLINE_ENV,
        STEP_CLIENT_TIMEOUT_MARGIN_SEC,
        step_client_timeout_sec,
        step_deadline_sec,
    )

    default = float(POST_STEP_DEADLINE_DEFAULT_SEC)
    for bad in ("inf", "+inf", "-inf"):
        monkeypatch.setenv(POST_STEP_DEADLINE_ENV, bad)
        assert (
            step_deadline_sec(POST_STEP_DEADLINE_ENV, POST_STEP_DEADLINE_DEFAULT_SEC) == default
        ), f"{bad!r} was not rejected by step_deadline_sec"
        assert (
            step_client_timeout_sec(POST_STEP_DEADLINE_ENV, POST_STEP_DEADLINE_DEFAULT_SEC)
            == default + STEP_CLIENT_TIMEOUT_MARGIN_SEC
        ), f"{bad!r} leaked into the derived client timeout"


def test_a_non_finite_step_deadline_env_var_does_not_disable_the_cli_socket_timeout(monkeypatch):
    """End-to-end version of the above through the actual step commands: an inf deadline must
    not leave the CLI waiting forever on the broker socket."""
    from tt_device_mcp import cli
    from tt_device_mcp.constants import POST_STEP_DEADLINE_DEFAULT_SEC, STEP_CLIENT_TIMEOUT_MARGIN_SEC

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    seen = {}

    def fake_call(port, tool, arguments, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return {"status": "ok", "ok": True, "reason": ""}

    monkeypatch.setattr(cli, "mcp_tool_call", fake_call)

    monkeypatch.setenv("TT_DEVICE_MCP_POST_STEP_DEADLINE_SEC", "inf")
    cli.cmd_post_step(_step_args())
    assert seen["timeout"] == float(POST_STEP_DEADLINE_DEFAULT_SEC) + STEP_CLIENT_TIMEOUT_MARGIN_SEC, seen["timeout"]


def test_a_step_with_no_broker_exits_non_zero(monkeypatch):
    """No broker means nothing proved the device fit. 07 I2's refusal applies to the step verbs
    too — an unreachable broker on a Slurm node is itself a reason to drain."""
    from tt_device_mcp import cli

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: False)
    assert cli.cmd_pre_step(_step_args()) == 1


def test_cmd_reset_opens_the_stream_with_the_arguments_it_was_given(monkeypatch, capsys):
    """Every device-touching command reaches the broker through a call built here, and nothing
    else in the suite executes `cmd_reset` — a name that does not resolve in this function is a
    NameError at the moment someone resets a wedged device, which is the worst possible moment
    to learn about it. Stub the stream and assert on what the call was handed.
    """
    sys.path.insert(0, SRC_PATH)
    from tt_device_mcp import cli

    seen = {}

    class _Resp:
        pass

    class _Conn:
        def close(self):
            seen["closed"] = True

    def _fake_open(port, payload, **kwargs):
        seen["port"], seen["payload"], seen["kwargs"] = port, payload, kwargs
        return _Conn(), _Resp()

    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_open_reset_stream", _fake_open)
    monkeypatch.setattr(cli, "_print_stream_with_dots", lambda resp: "reset_complete")
    monkeypatch.setattr(cli, "get_owner", lambda: "tester")

    class Args:
        port = 8333
        force = True

    assert cli.cmd_reset(Args()) == 0
    assert seen["port"] == 8333, seen
    # No owner: the broker derives it from the peer uid (spec 05 I6), and a field it ignores
    # would read as though the caller still names the reset's owner.
    assert seen["payload"] == {"force": True}, seen
    assert seen["closed"] is True, "the connection was not closed"
    assert "Reset complete." in capsys.readouterr().out
