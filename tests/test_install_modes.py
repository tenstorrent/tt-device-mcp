# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Two install scenarios, inferred rather than asked for.

bare-metal is the system broker: a unit, privsep, root-owned state paths. per-user is a socket
daemon under a machine-local per-uid base, with no unit and no self-update. The mode comes from
whether the host can actually run the full suite.

The choice is about SHAPE AND PATHS ONLY. Neither arm confers or withholds authority over the
device: health gating is on in both, and which recovery rungs exist is measured at boot from the
platform and from what the process can execute (spec 04 I17). Nothing here persists a profile.
"""

import argparse
import os
import subprocess
from pathlib import Path

import pytest

from tt_device_mcp import cli, constants

_ROOT = Path(__file__).resolve().parent.parent
_INSTALL = _ROOT / "install.sh"
_USER_INSTALL = _ROOT / "deploy" / "install-user.sh"
_BROKER_INSTALL = _ROOT / "deploy" / "install-tt-device-broker.sh"


def _inferred_mode(uid, systemd, tmp_path, arg=""):
    """Report which installer install.sh chooses, without running one.

    Both `exec` lines are replaced by echoes and the replacements are ASSERTED: unreplaced, this
    would run the real installer under the test's cwd — venv rebuild, crontab rewrite and all.
    """
    body = _INSTALL.read_text()
    for target, mode in (
        ('exec "$HERE/deploy/install-tt-device-broker.sh"', "broker"),
        ('exec "$HERE/deploy/install-user.sh" "$HERE"', "user"),
    ):
        assert target in body, f"install.sh no longer contains {target!r}; this stub would exec it"
        body = body.replace(target, f"echo MODE={mode}; exit 0")
    assert "exec " not in body, "an exec survived the stub; the real installer could run"

    systemd_dir = tmp_path / "systemd"
    if systemd:
        systemd_dir.mkdir(exist_ok=True)
    stub = f'id() {{ [ "$1" = -u ] && echo {uid} || command id "$@"; }}\n'
    out = subprocess.run(
        ["bash", "-c", stub + body, "bash", arg],
        capture_output=True,
        text=True,
        env={**os.environ, "SYSTEMD_DIR": str(systemd_dir)},
    )
    return out.stdout + out.stderr


def test_root_on_a_systemd_host_gets_the_full_suite(tmp_path):
    assert "MODE=broker" in _inferred_mode(0, systemd=True, tmp_path=tmp_path)


def test_root_without_systemd_gets_the_per_user_daemon(tmp_path):
    """The bug: uid alone chose the system broker, which then died at `systemctl enable` with the
    venv already built. Root in a container has no systemd and cannot run that install."""
    assert "MODE=user" in _inferred_mode(0, systemd=False, tmp_path=tmp_path)


def test_non_root_gets_the_per_user_daemon(tmp_path):
    assert "MODE=user" in _inferred_mode(1000, systemd=True, tmp_path=tmp_path)


def test_there_is_no_authority_flag_to_pass(tmp_path):
    """Device authority is measured, not declared (spec 04 I17), so the flag that used to declare
    it is gone rather than accepted-and-ignored — a silently tolerated flag would leave operators
    believing they had asked for something."""
    out = _inferred_mode(1000, systemd=False, tmp_path=tmp_path, arg="--reservation")
    assert "MODE=" not in out
    assert "usage:" in out, out


def test_explicit_broker_is_refused_without_systemd(tmp_path):
    """`--broker` overrides the inference, not the requirement."""
    out = _inferred_mode(0, systemd=False, tmp_path=tmp_path, arg="--broker")
    assert "MODE=broker" not in out
    assert "needs systemd" in out, out


def test_per_user_install_owns_its_venv_and_never_uses_user_site():
    """pip refuses --user inside an active virtualenv, and the tt-metalium dev image ships one whose
    python has no pip at all — so the install died on its first line and no daemon was created."""
    body = _USER_INSTALL.read_text()
    offenders = [
        ln.strip()
        for ln in body.splitlines()
        if "pip install" in ln and "--user" in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, f"pip --user cannot install inside an active venv: {offenders}"
    assert "python3 -m venv" in body, "the per-user install must build its own venv"
    assert (
        'ln -sf "$VENV/bin/tt-device-mcp" "$BINDIR/tt-device-mcp"' in body
    ), "the venv entry point must be linked into $BINDIR, which daemon start and cron invoke"


def test_the_self_update_command_is_gone_not_merely_unscheduled():
    """Leaving a broken command behind is a trap: it could not work from a venv at all, since pip
    refuses --user inside a virtualenv."""
    assert "self_update" not in (_ROOT / "src" / "tt_device_mcp" / "cli.py").read_text()


def _run_full_user_install(tmp_path, seed_crontab="", install_dir=None):
    """Run install-user.sh end to end with every external tool stubbed on PATH, and return what it
    piped into `crontab -`. Text assertions could not see this block at all — re-adding the
    self-update cron left the suite green.

    `install_dir` is always set (defaulting to a tmp_path sandbox), never left to the script's own
    default of `/tmp/tt-device-mcp-<uid>` — that default names a real, shared, per-uid path on
    whatever host runs this test, which a test must never create or touch.
    """
    home = tmp_path / "home"
    bin_ = tmp_path / "stubs"
    home.mkdir(parents=True)
    bin_.mkdir()
    install_dir = install_dir if install_dir is not None else tmp_path / "install"
    written = tmp_path / "crontab.written"
    (bin_ / "crontab").write_text(
        f'#!/bin/sh\nif [ "$1" = -l ]; then printf "%s" \'{seed_crontab}\'; else cat > "{written}"; fi\n'
    )
    # `python3 -m venv --clear <venv>` must leave behind both the interpreter the script then
    # runs pip with and the entry point it links into $BINDIR.
    (bin_ / "python3").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -m ] && [ "$2" = venv ]; then\n'
        '  for v in "$@"; do :; done\n'
        '  mkdir -p "$v/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$v/bin/python"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$v/bin/tt-device-mcp"\n'
        '  chmod +x "$v/bin/python" "$v/bin/tt-device-mcp"\n'
        "fi\nexit 0\n"
    )
    for name in ("claude",):
        (bin_ / name).write_text("#!/bin/sh\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(0o755)

    out = subprocess.run(
        ["bash", str(_USER_INSTALL), str(_ROOT)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "XDG_DATA_HOME": "",
            "TT_DEVICE_MCP_INSTALL_DIR": str(install_dir),
            "PATH": f"{bin_}:{os.environ['PATH']}",
        },
    )
    assert "DONE" in out.stdout, f"install did not finish: {out.stdout}\n{out.stderr}"
    return written.read_text() if written.exists() else ""


def test_no_cron_entry_is_installed(tmp_path):
    """The base is boot-cleared (06 I4), so an `@reboot` entry would re-exec `$BINDIR` at a path
    guaranteed to be gone — reboot persistence promised and silently not delivered, every boot.
    On a clean crontab the installer must write nothing at all, which is observable here as never
    piping to `crontab -`."""
    crontab = _run_full_user_install(tmp_path)
    assert crontab == "", f"the installer wrote a crontab entry: {crontab!r}"


def test_the_install_persists_no_authority_profile(tmp_path):
    """There is no install-time authority decision left to recover. A leftover profile file would
    be read by nothing and would suggest an install still chooses what the ladder may do."""
    install_dir = tmp_path / "install"
    _run_full_user_install(tmp_path, install_dir=install_dir)
    assert not (install_dir / "profile").exists()


def test_a_stale_self_update_cron_is_removed(tmp_path):
    """This shape has no self-update command, so such an entry can only fail; an install must
    take it out rather than leave it firing every ten minutes."""
    crontab = _run_full_user_install(tmp_path, seed_crontab="*/10 * * * * $HOME/.local/bin/tt-device-mcp self-update\n")
    assert "self-update" not in crontab, crontab


def test_removing_a_stale_entry_leaves_the_users_own_cron_alone(tmp_path):
    """Retiring our entry means filtering the user's crontab, and their own jobs live in that same
    table. Take ours out, leave everything else exactly where it was."""
    crontab = _run_full_user_install(
        tmp_path,
        seed_crontab=(
            "*/5 * * * * /usr/local/bin/backup.sh\n" "@reboot sleep 10 && /old/bin/tt-device-mcp daemon start\n"
        ),
    )
    assert "backup.sh" in crontab, crontab
    assert "tt-device-mcp" not in crontab, crontab


def test_per_user_install_writes_nothing_under_home(tmp_path):
    """The regression this whole fix is about: root can't write to a root $HOME, and $HOME may be a
    network share mounted by several machines while the venv/socket/pid/logs this installs are
    machine-specific.
    Run the real installer with a sandboxed, pre-existing HOME and assert it comes back untouched —
    not even an rc file."""
    home = tmp_path / "home"
    _run_full_user_install(tmp_path)
    assert home.is_dir(), "the sandbox HOME must still exist to prove it was left alone"
    left_behind = list(home.rglob("*"))
    assert left_behind == [], f"install-user.sh wrote under $HOME: {left_behind}"


def test_venv_and_cli_land_under_the_machine_local_install_base(tmp_path):
    """The venv (compiled wheels) and the CLI symlink are the two artifacts this installer creates;
    both must land under the install base, not $HOME/.local/*."""
    install_dir = tmp_path / "custom-install"
    _run_full_user_install(tmp_path, install_dir=install_dir)
    assert (install_dir / "venv" / "bin" / "tt-device-mcp").exists()
    assert (install_dir / "bin" / "tt-device-mcp").is_symlink()


def test_install_dir_is_overridable_with_a_dedicated_env_var(tmp_path):
    """TT_DEVICE_MCP_INSTALL_DIR, consistent with TT_DEVICE_MCP_STATE_DIR for runtime state — the
    seam a test needs so it never has to touch the real per-uid default."""
    install_dir = tmp_path / "somewhere-else" / "entirely"
    _run_full_user_install(tmp_path, install_dir=install_dir)
    assert (install_dir / "bin" / "tt-device-mcp").is_symlink()


def test_install_base_defaults_to_tmp_uid_not_home():
    """The whole per-user footprint is boot-cleared by design (06 I4) — this shape has no boot
    recovery to protect — and never under $HOME, which may be a network share. Extract just the
    base computation rather than running the full installer, so this stays a check on the default
    (no override) without ever creating the real per-uid path."""
    body = _USER_INSTALL.read_text()
    start = body.index("INSTALL_BASE=")
    end = body.index("\n\n", start)
    snippet = body[start:end]
    out = subprocess.run(
        ["bash", "-c", snippet + '\necho "$INSTALL_BASE"'],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "TT_DEVICE_MCP_INSTALL_DIR"},
    )
    assert out.stdout.strip() == f"/tmp/tt-device-mcp-{os.getuid()}", out.stdout + out.stderr


def _run_user_install(tmp_path, *, bindir_exe, venv_exe):
    """Run install-user.sh far enough to observe the stop, with the venv build and everything after
    it stubbed out. Records which entry points were invoked, in order."""
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    calls = tmp_path / "calls"
    home.mkdir(parents=True)
    (install_dir / "bin").mkdir(parents=True)
    venv_bin = install_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)

    for present, path, label in (
        (bindir_exe, install_dir / "bin/tt-device-mcp", "bindir"),
        (venv_exe, venv_bin / "tt-device-mcp", "venv"),
    ):
        if present:
            path.write_text(f'#!/bin/sh\necho {label} "$@" >> "{calls}"\n')
            path.chmod(0o755)

    body = _USER_INSTALL.read_text()
    stop = body.index("python3 -m venv")  # everything from the rebuild on is not under test
    script = body[:stop] + "\necho REACHED_REBUILD\n"
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "XDG_DATA_HOME": "", "TT_DEVICE_MCP_INSTALL_DIR": str(install_dir)},
    )
    assert "REACHED_REBUILD" in out.stdout, f"the stub never reached the rebuild: {out.stderr}"
    return calls.read_text() if calls.exists() else ""


def test_reinstalling_stops_the_daemon_before_replacing_its_venv(tmp_path):
    """Re-running the installer is the only upgrade path, and it rebuilds the venv the daemon runs
    out of — while `daemon start` returns 0 on an already-running one, so without a stop it reports
    DONE with the previous build still serving."""
    called = _run_user_install(tmp_path, bindir_exe=True, venv_exe=True)
    assert "daemon stop" in called, f"the daemon was not stopped before the rebuild: {called!r}"


def test_the_stop_is_not_gated_on_the_bindir_symlink(tmp_path):
    """A daemon can be up while $BINDIR/tt-device-mcp is absent — the symlink is easy to lose, and
    the venv is what the daemon actually runs from. Gating the stop on the symlink alone skipped it
    and the rebuild landed under the live daemon anyway."""
    called = _run_user_install(tmp_path, bindir_exe=False, venv_exe=True)
    assert (
        "venv daemon stop" in called
    ), f"with the symlink gone, the venv entry point must still be used to stop: {called!r}"


def test_a_first_install_with_nothing_to_stop_still_proceeds(tmp_path):
    """No previous install: there is no daemon and no entry point, and that is not an error."""
    assert _run_user_install(tmp_path, bindir_exe=False, venv_exe=False) == ""


def test_broker_install_restarts_so_an_upgrade_takes_effect():
    """`enable --now` starts nothing on an already-active unit, so re-running the installer to
    upgrade rebuilt the venv and left the previous build running out of it."""
    body = _BROKER_INSTALL.read_text()
    assert (
        "systemctl restart tt-device-broker" in body
    ), "the installer must restart the broker, or an upgrade never takes effect"
    assert "systemctl enable --now tt-device-broker" not in body, "`enable --now` is a no-op on an active unit"


def _capture_daemon_env(monkeypatch, tmp_path):
    """Start the per-user daemon with Popen stubbed; return the env the child would have received."""
    captured = {}

    class _Proc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs.get("env") or {})
        captured["_env_passed"] = "env" in kwargs
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "is_daemon_running", lambda *a, **k: False)
    monkeypatch.setattr(cli.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(cli.os, "kill", lambda *_a: None)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    monkeypatch.setenv("TT_DEVICE_MCP_INSTALL_DIR", str(install_dir))
    cli.cmd_daemon_start(argparse.Namespace(log_dir=str(tmp_path), port=None))
    return captured


def test_the_per_user_daemon_health_gates_like_any_other(monkeypatch, tmp_path):
    """Health gating is not an authority: probing costs the device nothing a job does not already
    cost it, and an unprivileged daemon can run every probe the gate needs. Leaving it off was
    what made a container silently unable to notice a wedge it was dispatching onto."""
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_CHECK", raising=False)
    env = _capture_daemon_env(monkeypatch, tmp_path)
    assert env["_env_passed"], "pass the daemon env to the child rather than mutating our own"
    assert "TT_DEVICE_MCP_HEALTH_CHECK" not in env, "the gate is on by default; nothing is set here"


def test_the_daemon_env_declares_no_rung_policy(monkeypatch, tmp_path):
    """The host rungs are measured (spec 04 I17), so pinning them at spawn would override a
    measurement with a guess, and would hide a real capability on a root daemon."""
    for key in (
        "TT_DEVICE_MCP_HEALTH_CHECK",
        "TT_DEVICE_MCP_AUTO_REBOOT",
        "TT_DEVICE_MCP_AUTO_POWER_CYCLE",
        "TT_DEVICE_MCP_AUTO_UBB_RESET",
    ):
        monkeypatch.delenv(key, raising=False)

    env = _capture_daemon_env(monkeypatch, tmp_path)

    for key in (
        "TT_DEVICE_MCP_HEALTH_CHECK",
        "TT_DEVICE_MCP_AUTO_REBOOT",
        "TT_DEVICE_MCP_AUTO_POWER_CYCLE",
        "TT_DEVICE_MCP_AUTO_UBB_RESET",
        "TT_DEVICE_MCP_RESERVATION",
    ):
        assert key not in env, f"{key} must come from measurement or the operator, not from spawn"


def test_the_daemon_env_preserves_explicit_operator_overrides(monkeypatch, tmp_path):
    """Spawn must not override an operator's own arming; it copies the environment, not edits it."""
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_UBB_RESET", "1")
    env = _capture_daemon_env(monkeypatch, tmp_path)
    assert env["TT_DEVICE_MCP_AUTO_REBOOT"] == "1"
    assert env["TT_DEVICE_MCP_AUTO_UBB_RESET"] == "1"


def test_per_user_daemon_keeps_its_state_somewhere_writable(monkeypatch, tmp_path):
    """Two dirs default under root-only paths and a non-root daemon loses both silently: the event
    journal (/var/lib — written whatever the health flag says) and the per-job exit status (/run —
    only root can create a dir there, so every job's exit trap has nowhere to write). Both become
    subdirectories of the daemon's own state dir."""
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_JOB_EXIT_DIR", raising=False)
    env = _capture_daemon_env(monkeypatch, tmp_path)
    for var, name, unwritable in (
        ("TT_DEVICE_MCP_HEALTH_DIR", "health", "/var/lib"),
        ("TT_DEVICE_MCP_JOB_EXIT_DIR", "jobexit", "/run/"),
    ):
        got = env.get(var)
        assert got, f"{var} must be redirected somewhere the daemon can write"
        assert not got.startswith(unwritable), f"a non-root daemon cannot create {got}"
        assert got == str(tmp_path / name), f"{var}={got}"
    assert tmp_path.stat().st_mode & 0o777 == 0o700, "another tenant must not read this host's device state"


def test_a_state_dir_that_cannot_be_secured_stops_the_daemon_with_an_explanation(tmp_path):
    """The base lives in shared /tmp, so another tenant can hold the path this daemon wants; only its
    owner can chmod it, and the daemon must not settle for whatever mode it finds. It refuses — but
    an operator whose `daemon start` ends in a chmod traceback has no way to tell that from a bug, so
    the refusal names the path, the owner and the two ways out."""
    blocker = tmp_path / "held-by-someone-else"
    blocker.write_text("")  # a file where the dir must go: EPERM-equivalent for root as well
    with pytest.raises(SystemExit) as exc:
        cli._daemon_state_dir(str(blocker / "state"))
    msg = str(exc.value)
    assert "Traceback" not in msg
    assert str(blocker) in msg, msg
    assert "--log-dir" in msg and "TT_DEVICE_MCP_STATE_DIR" in msg, msg


def test_all_per_user_state_is_machine_local(monkeypatch):
    """$HOME may be a network share mounted by several machines: two hosts there would collide on the
    socket and the pid file and interleave each other's journals. Nothing per-user goes under $HOME
    — and the base is per-uid, because /tmp is only private inside a container."""
    monkeypatch.delenv("TT_DEVICE_MCP_STATE_DIR", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_INSTALL_DIR", raising=False)
    base = str(constants.user_state_dir())
    # Under the install base, not beside it: one tree is one thing to find and one to delete.
    assert base == f"/tmp/tt-device-mcp-{os.getuid()}/state", base
    home = os.path.expanduser("~")
    assert not base.startswith(home)
    assert not constants.user_socket_path().startswith(home), constants.user_socket_path()


def test_the_state_dir_is_overridable_so_tests_never_touch_the_live_one(monkeypatch, tmp_path):
    """The live base is a fixed path, so without a seam a test run would create and chmod the
    directory a deployed daemon is using."""
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "elsewhere"))
    assert constants.user_state_dir() == tmp_path / "elsewhere"
    assert constants.user_socket_path() == str(tmp_path / "elsewhere" / "daemon.sock")


def test_start_fg_gets_the_same_environment_as_start(monkeypatch, tmp_path):
    """start-fg is the documented way to debug this daemon; booting it with a different
    configuration than the one being debugged is worse than not having it."""
    # start-fg is in-process, so it writes the variable into our own environ; a copy keeps that out
    # of every test that runs after this one (monkeypatch cannot undo a write to a name it deleted).
    env = {k: v for k, v in os.environ.items() if k != "TT_DEVICE_MCP_HEALTH_CHECK"}
    monkeypatch.setattr(cli.os, "environ", env)
    seen = {}
    monkeypatch.setattr(cli, "_daemon_socket", lambda *a, **k: str(tmp_path / "s.sock"))
    monkeypatch.setattr(
        "tt_device_mcp.server.main",
        lambda: seen.update(lock=env.get("TT_DEVICE_MCP_DEVICE_OP_LOCK"), health="TT_DEVICE_MCP_HEALTH_CHECK" in env),
    )
    cli.cmd_daemon_start_fg(argparse.Namespace(log_dir=str(tmp_path), port=None))
    assert seen.get("health") is False, "start-fg must not pin a gate setting `start` leaves alone"
    assert seen.get("lock"), "start-fg must get the same redirected state paths as start"


def test_the_shipped_systemd_path_is_the_real_one():
    """Every mode test injects SYSTEMD_DIR, so the production default is otherwise unpinned — and a
    wrong one sends every root+systemd host to the per-user daemon, inverting the fix."""
    assert "SYSTEMD_DIR:-/run/systemd/system" in _INSTALL.read_text()


def test_the_inference_matches_this_host_with_nothing_injected(tmp_path):
    """Exercise the default rather than the seam: with SYSTEMD_DIR unset the mode must follow what
    this host actually has."""
    body = _INSTALL.read_text()
    for target, mode in (
        ('exec "$HERE/deploy/install-tt-device-broker.sh"', "broker"),
        ('exec "$HERE/deploy/install-user.sh" "$HERE"', "user"),
    ):
        assert target in body
        body = body.replace(target, f"echo MODE={mode}; exit 0")
    env = {k: v for k, v in os.environ.items() if k != "SYSTEMD_DIR"}
    out = subprocess.run(
        ["bash", "-c", 'id() { [ "$1" = -u ] && echo 0 || command id "$@"; }\n' + body],
        capture_output=True,
        text=True,
        env=env,
    )
    expected = "MODE=broker" if Path("/run/systemd/system").is_dir() else "MODE=user"
    assert expected in out.stdout + out.stderr, out.stdout + out.stderr


def test_an_operator_can_still_turn_the_gate_off(monkeypatch, tmp_path):
    """On is the default, not a mandate: a host that can never verify (no tt-smi) must be able to
    say so and keep the serializer."""
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "0")
    assert _capture_daemon_env(monkeypatch, tmp_path).get("TT_DEVICE_MCP_HEALTH_CHECK") == "0"


def test_preset_state_dirs_are_left_alone(monkeypatch, tmp_path):
    """An operator who placed the journal deliberately must not have it moved under them."""
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_DIR", str(tmp_path / "mine"))
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_EXIT_DIR", str(tmp_path / "theirs"))
    env = _capture_daemon_env(monkeypatch, tmp_path)
    assert env["TT_DEVICE_MCP_HEALTH_DIR"] == str(tmp_path / "mine")
    assert env["TT_DEVICE_MCP_JOB_EXIT_DIR"] == str(tmp_path / "theirs")


def test_the_per_user_daemon_redirects_the_device_op_lock(monkeypatch, tmp_path):
    """The third root-owned path from tt-device-mcp#21, alongside the journal and the exit status."""
    monkeypatch.delenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", raising=False)
    env = _capture_daemon_env(monkeypatch, tmp_path)
    assert env.get("TT_DEVICE_MCP_DEVICE_OP_LOCK") == str(tmp_path / "device-op.lock")
