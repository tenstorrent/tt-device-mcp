# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The two durable-state defaults (health journal, Prometheus textfile) resolve per deployment:
root gets the system path unchanged, anyone else gets a writable path under the same per-user
state base every other per-user daemon file already uses (constants.user_state_dir), and an
explicit env override always wins over both. A per-user daemon that silently kept the root-only
default lost its FSM's durability with no error at all — the genuine regression test below proves
the per-user default is not just a path string, but an actual place ServerFsm can round-trip
through."""

import ast
import asyncio
import json
import logging
import os
import stat
import sys
from datetime import datetime
from pathlib import Path

import pytest
from mcp.client import Client

from tests.conftest import local_reset_mechanism
from tt_device_mcp import constants, metrics, utils
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerFsm, ServerState
from tt_device_mcp.health import evidence
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import base as recovery_base

# ---- evidence._default_health_dir() -------------------------------------------------------------


def test_root_health_dir_default_is_unchanged(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.setattr(evidence.os, "geteuid", lambda: 0)
    assert evidence._default_health_dir() == evidence.Path("/var/lib/tt-device-broker/health")


def test_non_root_health_dir_default_lands_under_user_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(evidence.os, "geteuid", lambda: 1000)

    resolved = evidence._default_health_dir()

    assert resolved == constants.user_state_dir() / "health"
    resolved.mkdir(parents=True)
    (resolved / "probe").write_text("ok")  # would raise if this default were not writable
    assert (resolved / "probe").read_text() == "ok"


@pytest.mark.parametrize("euid", [0, 1000])
def test_health_dir_env_override_beats_both_defaults(monkeypatch, tmp_path, euid):
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_DIR", str(tmp_path / "mine"))
    monkeypatch.setattr(evidence.os, "geteuid", lambda: euid)
    assert evidence._default_health_dir() == tmp_path / "mine"


# ---- metrics._textfile_dir() ---------------------------------------------------------------------


def test_root_textfile_dir_default_is_unchanged(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_TEXTFILE_DIR", raising=False)
    monkeypatch.setattr(metrics.os, "geteuid", lambda: 0)
    assert metrics._textfile_dir() == metrics.Path("/var/lib/prometheus/node-exporter")


def test_non_root_textfile_dir_default_lands_under_user_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TT_DEVICE_MCP_TEXTFILE_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(metrics.os, "geteuid", lambda: 1000)
    assert metrics._textfile_dir() == constants.user_state_dir() / "metrics"


@pytest.mark.parametrize("euid", [0, 1000])
def test_textfile_dir_env_override_beats_both_defaults(monkeypatch, tmp_path, euid):
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(tmp_path / "mine"))
    monkeypatch.setattr(metrics.os, "geteuid", lambda: euid)
    assert metrics._textfile_dir() == tmp_path / "mine"


def test_non_root_textfile_writer_writes_and_leaves_no_tmp(monkeypatch, tmp_path):
    """write_textfile() itself, pointed at the non-root default rather than an explicit
    TT_DEVICE_MCP_TEXTFILE_DIR override — the path this module actually reaches for absent
    root and absent an operator override, not just the resolver in isolation."""
    monkeypatch.delenv("TT_DEVICE_MCP_TEXTFILE_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(metrics.os, "geteuid", lambda: 1000)
    metrics.reset_for_tests()

    metrics.state_entered("healthy", now=0.0)
    metrics.write_textfile()

    target_dir = constants.user_state_dir() / "metrics"
    target = target_dir / metrics.TEXTFILE_NAME
    assert target.exists()
    assert target.read_bytes() == metrics.render()
    assert not any(target_dir.glob(f"{metrics.TEXTFILE_NAME}.*.tmp"))
    metrics.reset_for_tests()


# ---- genuine durability: ServerFsm round-trips through the per-user default ----------------------


def test_fsm_survives_restart_at_the_per_user_default_health_dir(monkeypatch, tmp_path):
    """The regression this whole fix is about: a per-user daemon that silently kept the root-only
    default reported fsm_state over /health but had NO fsm.json anywhere, so a restart forgot a
    non-HEALTHY episode with no error to point at. Pointing a real ServerFsm at the resolved
    per-user default (not an arbitrary tmp_path) and reloading it from a second instance is the
    only way to prove the default is actually durable, not merely a plausible-looking path."""
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(evidence.os, "geteuid", lambda: 1000)

    health_dir = evidence._default_health_dir()
    fsm_path = health_dir / "fsm.json"
    assert not fsm_path.exists()

    first = ServerFsm(fsm_path)
    first.on_fault("eth_frozen", detail="active-eth heartbeat frozen")

    assert fsm_path.is_file(), "a non-HEALTHY episode must be fsync'd to disk, not held in-memory only"

    second = ServerFsm(fsm_path)
    assert second.state is ServerState.RECOVERING
    assert second.record.why == "eth_frozen"
    assert second.record.detail == "active-eth heartbeat frozen"


# ---- spec 06 I9: every log and state record the system broker writes is readable by every ------
# ---- local user ---------------------------------------------------------------------------------


@pytest.fixture
def system_trees(monkeypatch, tmp_path):
    """A temp tree standing in for the two system defaults: the constants the roots' predicate
    compares against point here, and the health dir resolves here. Neither root exists yet — the
    broker makes them, exactly as a first start on a fresh host does."""
    log_root = tmp_path / "var" / "log" / "tt-device-broker"
    health_root = tmp_path / "var" / "lib" / "tt-device-broker" / "health"
    monkeypatch.setattr(srv, "SYSTEM_LOG_DIR", str(log_root))
    monkeypatch.setattr(srv, "SYSTEM_HEALTH_DIR", str(health_root))
    monkeypatch.setattr(evidence, "HEALTH_DIR", health_root)
    return log_root, health_root


def _modes(root: Path) -> dict[Path, int]:
    """Mode bits of every directory and regular file under ``root`` — symlinks skipped: one has no
    mode of its own, and its target is asserted at its own path."""
    seen: dict[Path, int] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        seen[d] = stat.S_IMODE(d.lstat().st_mode)
        for name in filenames:
            p = d / name
            if not p.is_symlink():
                seen[p] = stat.S_IMODE(p.lstat().st_mode)
    return seen


def _stub_preflight_host(monkeypatch, tmp_path):
    """The stable-condition stubs the existing preflight tests use: a device node, every binary
    'on PATH', the local reset backend, no opt-in rungs."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "0").touch()
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev))
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_CHECK", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")


class _BootReached(Exception):
    """main() got as far as boot_broker(): the log tree, server.log and the first stats JSON exist."""


@pytest.mark.asyncio
async def test_every_file_and_directory_under_both_trees_is_readable_under_a_strict_umask(
    monkeypatch, tmp_path, strict_umask, pristine_loggers, clear_job_state, system_trees
):
    """Each writer the broker has, run under umask 077 so no backstop can hide one that skipped the
    helpers: afterwards every directory under both trees is 0755 and every regular file 0644 — the
    local-reset lock excepted, which stays 0600 (its reason is anchored in test_device_safety). A
    writer added later is caught by the create tripwire below, not by this walk."""
    log_root, health_root = system_trees

    # main() up to the boot boundary, for real: the log root (chmod'ed directly, at the default),
    # stats/, server.log and the first stats JSON plus its stats/current symlink.
    for name in ("logger", "job_log_dir", "stats", "stats_dir", "stats_file", "job_runner_task", "stats_task"):
        monkeypatch.setattr(srv, name, getattr(srv, name))  # main()/lifespan assign these; restore them
    monkeypatch.setattr(srv, "boot_broker", lambda **_kw: (_ for _ in ()).throw(_BootReached()))
    monkeypatch.setattr(sys, "argv", ["tt-device-mcp-server", "--log-dir", str(log_root)])
    with pytest.raises(_BootReached):
        srv.main()

    # The I7 preflight: creates the health dir and brings both state roots to mode.
    _stub_preflight_host(monkeypatch, tmp_path)
    fails, warns, _serialize_only = srv._preflight_required_capabilities(socket_path=None)
    assert not [m for m in fails + warns if "health dir" in m], (fails, warns)

    # A job log through the one funnel that creates it (_queue_job, spec 01 I3), via the
    # non-blocking tool; the runner is idled so the job is queued, never run.
    workspace = tmp_path / "ws"
    (workspace / "tt-metal" / "python_env").mkdir(parents=True)

    async def _idle():
        return None

    monkeypatch.setattr(srv, "job_runner", _idle)
    monkeypatch.setattr(srv, "stats_persistence_loop", _idle)
    monkeypatch.setattr(srv, "run_startup_tasks", _idle)
    async with Client(srv.create_mcp_server()) as client:
        result = await client.call_tool(
            "tt_device_job_run_bg",
            {"params": {"owner": "walker", "workspace": str(workspace), "command": "echo walk", "timeout_sec": 60}},
        )
    assert result.is_error is False, result.content
    payload = result.structured_content if result.structured_content is not None else json.loads(result.content[0].text)
    job_log = Path(payload["log_file"])
    assert job_log.is_file()

    # Every other writer, by the function that owns its create.
    srv._write_action_log_file("900", "[broker]reset", "tt-smi -r", datetime.now(), "completed", 0, 1.0)
    # The in-flight row is process state `_recent_jobs` reports; it must not outlive this test.
    monkeypatch.setattr(srv, "_action_row", None)
    srv._begin_action_row("[broker]fabric", "fabric check")
    srv._persist_hold_episode(datetime.now().isoformat())
    srv._persist_queued_job(srv.Job(id="901", owner="walker", workspace=str(workspace), command="c", queued_at="t"))
    monkeypatch.setattr(srv, "reset_killed_job_ids", set())
    srv._mark_job_device_fault_failed("902", "power_cycle")
    evidence.health_event("walk")
    evidence.append_trace([{"chip": 0}])
    pci.chip_snapshot_event("walk")
    ServerFsm(health_root / "fsm.json").on_fault("eth_frozen", detail="walk")
    assert srv.health_monitor.expected(2) == 2  # no baseline yet: present becomes the baseline on disk
    assert srv.recovery_mechanism.record_auto_recovery("host_reboot", "walk") is True
    srv.recovery_mechanism.retract_last_auto_recovery("host_reboot")  # the rewrite path of the ledger
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-walk")
    monkeypatch.setattr(srv, "previous_boot_error", lambda: {})
    monkeypatch.setattr(srv, "previous_boot_bus_locks", lambda: None)
    srv._record_previous_boot_error()  # the boot marker, buslock.log's boot mark, a reboot history row
    monkeypatch.setattr(evidence, "kernel_log", lambda: "")
    bundle = evidence.capture_incident(
        "walk", job={"id": "903"}, job_log=job_log, trace=[{"t": 1}], reset_output="reset", fabric_output="fabric"
    )
    assert bundle is not None
    # The detached local reset, walked IN FLIGHT: its output file lives only as long as the child
    # and the lock is held — the state a restarted broker finds and adopts (spec 04 I5).
    release = tmp_path / "release"
    mechanism = local_reset_mechanism(health_root, monkeypatch)
    waiter = f"import pathlib,time; p=pathlib.Path({str(release)!r})\nwhile not p.exists(): time.sleep(.01)"
    reset = asyncio.create_task(mechanism.run_scoped([sys.executable, "-c", waiter], lambda _m: None))
    try:
        for _ in range(500):
            if mechanism.scope_active() == recovery_base.LOCAL_RESET_NAME:
                break
            await asyncio.sleep(0.01)
        assert mechanism.scope_active() == recovery_base.LOCAL_RESET_NAME
        seen = {**_modes(log_root), **_modes(health_root.parent)}  # the state root holds health/
    finally:
        release.write_text("")
        rc, _output = await reset
    assert rc == 0
    names = {p.name for p in seen}
    for required in (
        "server.log",
        "stats",
        "queued",
        "901.json",
        srv.DEVICE_FAULT_FAILED_DIR,
        "902.json",
        job_log.name,
        evidence.EVENTS_FILE,
        evidence.TRACE_FILE,
        pci.CHIPS_FILE,
        evidence.BUSLOCK_FILE,
        "fsm.json",
        srv.HOLD_EPISODE_FILE,
        ".boot_recorded_boot-walk",
        recovery_base.AUTO_RECOVERY_LEDGER,
        srv.health_monitor.CHIP_BASELINE_FILE,
        recovery_base.LOCAL_RESET_LOCK,
        evidence.INCIDENTS_DIR,
        bundle.name,
        "incident.json",
        "kernel.log",
        "telemetry_trace.json",
        "job.log",
        "reset.log",
        "fabric.log",
    ):
        assert required in names, f"the run never created {required!r}; the walk would prove nothing about it"
    assert any(p.name.startswith(f"{recovery_base.LOCAL_RESET_NAME}-") and p.suffix == ".log" for p in seen)
    assert any(p.parent.name == "stats" and p.suffix == ".json" for p in seen), "no stats JSON"
    assert (log_root / "stats" / "current").is_symlink(), "the symlink the walk must skip is missing"
    assert len([p for p in seen if p.suffix == ".log" and p.parent == log_root]) >= 4  # job, action, row, reboot

    wrong = {}
    for path, mode in seen.items():
        if path.is_dir():
            want = 0o755
        elif path.name == recovery_base.LOCAL_RESET_LOCK:
            want = 0o600
        else:
            want = 0o644
        if mode != want:
            wrong[str(path.relative_to(tmp_path))] = f"{mode:04o} != {want:04o}"
    assert not wrong, f"not readable by every local user under umask 077: {wrong}"


def test_the_tree_roots_are_brought_to_mode_only_at_the_system_default_paths(monkeypatch, tmp_path, strict_umask):
    """Path equality against the path matrix's defaults, never the euid: at the (patched) defaults
    a 0700 root opens to 0755 — the state tree's parent with it — and a redirected root of the
    same shape is left exactly as found."""
    default_log = tmp_path / "default" / "log"
    default_health = tmp_path / "default" / "lib" / "health"
    redirected_log = tmp_path / "elsewhere" / "log"
    redirected_health = tmp_path / "elsewhere" / "lib" / "health"
    for d in (default_log, default_health, redirected_log, redirected_health):
        d.mkdir(parents=True)  # 0700 under umask 077, like a root a strict-umask host pre-created
    monkeypatch.setattr(srv, "SYSTEM_LOG_DIR", str(default_log))
    monkeypatch.setattr(srv, "SYSTEM_HEALTH_DIR", str(default_health))

    srv._open_system_log_root(default_log)
    srv._open_system_health_roots(default_health)
    srv._open_system_log_root(redirected_log)
    srv._open_system_health_roots(redirected_health)

    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731 - a one-line reader
    assert mode(default_log) == 0o755
    assert mode(default_health) == 0o755
    assert mode(default_health.parent) == 0o755, "the state root above health/ is a tree root too"
    assert mode(redirected_log) == 0o700
    assert mode(redirected_health) == 0o700
    assert mode(redirected_health.parent) == 0o700


def test_a_per_user_base_is_never_brought_to_mode_at_its_root(monkeypatch, tmp_path, strict_umask):
    """The per-user daemon's log dir is its state dir and its health dir is <state>/health, both
    under the 0700 base spec 06 I6 guarantees; the roots' chmod must not reach any of them."""
    monkeypatch.setenv("TT_DEVICE_MCP_INSTALL_DIR", str(tmp_path / "base"))
    monkeypatch.delenv("TT_DEVICE_MCP_STATE_DIR", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_HEALTH_DIR", raising=False)
    monkeypatch.setattr(evidence.os, "geteuid", lambda: 1000)
    base, state = constants.user_install_dir(), constants.user_state_dir()
    health = evidence._default_health_dir()
    health.mkdir(parents=True)
    base.chmod(0o700)

    srv._open_system_log_root(state)
    srv._open_system_health_roots(health)

    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731 - a one-line reader
    assert mode(base) == 0o700
    assert mode(state) == 0o700
    assert mode(health) == 0o700


def test_a_log_dir_root_that_cannot_be_brought_to_mode_fails_the_start(
    monkeypatch, tmp_path, pristine_loggers, system_trees
):
    """Same failure as a root that cannot be created: main() raises before it serves, and nothing
    is logged into a tree nobody but root could read."""
    log_root, _health_root = system_trees
    for name in ("logger", "job_log_dir", "stats", "stats_dir", "stats_file"):
        monkeypatch.setattr(srv, name, getattr(srv, name))
    monkeypatch.setattr(sys, "argv", ["tt-device-mcp-server", "--log-dir", str(log_root)])
    # A regression that stopped the root chmod from raising must fail here, at the boot boundary,
    # never boot and serve: the socket path would be the live broker's on a broker host.
    monkeypatch.delenv("TT_DEVICE_MCP_SOCKET", raising=False)
    monkeypatch.setattr(srv, "boot_broker", lambda **_kw: (_ for _ in ()).throw(_BootReached()))

    def _refused(path, mode, *args, **kwargs):
        raise PermissionError(f"chmod {mode:o} {path}: operation not permitted")

    monkeypatch.setattr(os, "chmod", _refused)

    with pytest.raises(PermissionError):
        srv.main()

    assert log_root.is_dir(), "the root was created; it is the mode that refused the start"
    assert not (log_root / "server.log").exists()


@pytest.mark.parametrize(
    ("health_checks", "sink"),
    [("1", "fails"), ("0", "warns")],
    ids=["health-checks-on-fails", "health-checks-off-warns"],
)
def test_a_health_dir_root_that_cannot_be_brought_to_mode_follows_the_i7_preflight_rule(
    monkeypatch, tmp_path, system_trees, health_checks, sink
):
    """I7's own rule for the health dir: a failure with health checks on, a warning with them off —
    the same sink an unwritable health dir lands in."""
    _log_root, health_root = system_trees
    _stub_preflight_host(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", health_checks)

    def _refused(path, mode, *args, **kwargs):
        if Path(path) in (health_root, health_root.parent):
            raise PermissionError(f"chmod {mode:o} {path}: operation not permitted")

    monkeypatch.setattr(os, "chmod", _refused)

    fails, warns, _serialize_only = srv._preflight_required_capabilities(socket_path=None)

    reports = {"fails": fails, "warns": warns}
    assert any("brought to mode 0755" in m and str(health_root) in m for m in reports[sink]), reports
    other = "warns" if sink == "fails" else "fails"
    assert not any("health dir" in m for m in reports[other]), reports


def test_the_readable_helpers_never_raise_and_log_once_per_path(monkeypatch, tmp_path, caplog):
    """A mode that cannot be set never fails the write — a file the broker could not open to its
    readers is still the file it needed — and logs one line per path: a second failure on the same
    path adds nothing (the textfile writer's once-per-process shape, keyed per path), while a
    different path gets its own line."""
    monkeypatch.setattr(utils, "_mode_failed_logged", set())

    def _foreign(target, mode, *args, **kwargs):
        raise PermissionError(f"chmod {mode:o} {target}: operation not permitted")

    monkeypatch.setattr(utils.os, "chmod", _foreign)
    monkeypatch.setattr(utils.os, "fchmod", _foreign)
    # caplog listens at the root; the broker's logger stops propagating once setup_logging has run.
    monkeypatch.setattr(logging.getLogger("tt-device-mcp"), "propagate", True)
    with caplog.at_level(logging.WARNING, logger="tt-device-mcp"):
        with utils.open_readable(tmp_path / "a.log", "a") as f:
            f.write("written\n")
        utils.write_text_readable(tmp_path / "a.log", "rewritten\n")
        utils.readable_dir(tmp_path / "d")
        utils.readable_dir(tmp_path / "d")
        utils.write_text_readable(str(tmp_path / "b.log"), "b\n")

    assert (tmp_path / "a.log").read_text() == "rewritten\n", "the write went ahead"
    lines = [r.getMessage() for r in caplog.records if "could not set mode" in r.getMessage()]
    assert len(lines) == 3, lines
    assert sum(str(tmp_path / "a.log") in ln for ln in lines) == 1
    assert sum(str(tmp_path / "d") in ln for ln in lines) == 1
    assert sum(str(tmp_path / "b.log") in ln for ln in lines) == 1
    assert any("0644" in ln for ln in lines) and any("0755" in ln for ln in lines)


def test_open_readable_creates_and_reasserts_0644_under_a_strict_umask(tmp_path, strict_umask):
    # The create carries the mode, so there is no second step to forget; a file found at another
    # mode is brought to 0644 by the next open, as the two-step helper did after every write.
    path = tmp_path / "x.jsonl"
    with utils.open_readable(path, "a") as f:
        f.write("1\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.chmod(0o600)
    utils.write_text_readable(path, "2\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and path.read_text() == "2\n"


# Every create in the package that does NOT go through the I9 helpers, with why it may not. A write
# into either tree through plain open()/write_text()/mkdir() fails the test below: add it to the
# helpers, or — if it is genuinely outside both trees — here, with its reason.
_CREATES_OUTSIDE_THE_TREES = {
    ("cli.py", "_clear_lock_opt_in"): "the operator's /etc/default/tt-device-broker",
    ("cli.py", "_daemon_state_dir"): "the per-user base, secured at 0700 (I6)",
    ("cli.py", "_install_smi_ro_wrapper"): "the read-only tt-smi wrapper under /usr/local",
    ("cli.py", "_set_service_device_group"): "the systemd unit file under /etc",
    ("cli.py", "cmd_daemon_start"): "per-user socket dir, daemon.log and pid file, inside the 0700 base",
    ("cli.py", "cmd_lock"): "the udev rule and login banner under /etc",
    ("cli.py", "cmd_refresh_banner"): "the login banner under /etc",
    ("cli.py", "cmd_timezone"): "the user's display-timezone setting",
    ("cli.py", "per_user_daemon_env"): "redirect targets inside the 0700 per-user base",
    ("device_lock.py", "mark_applied"): "the device-lock marker under /run",
    ("device_lock.py", "record_drift"): "the device-lock drift record under /run",
    ("health/chip_state.py", "publish"): "the published chip state under /run (0644, atomic rename; 03 I66)",
    ("health/cpld_dump.py", "_open_lock"): "the CPLD dump lock under /run/lock, 0644 by its own contract",
    ("health/monitors/eth.py", "build"): "the probe's tt-metal cache under /var/cache",
    ("health/monitors/fabric.py", "build_command"): "the fabric check's cache and output under /var/cache",
    ("health/monitors/pci.py", "isolate_chip"): "a sysfs control file",
    ("health/recovery/base.py", "_run_local_reset"): "the local-reset lock, private 0600 by I9",
    ("health/recovery/base.py", "local_reset_active"): "the local-reset lock, private 0600 by I9",
    ("health/recovery/ipmi_guard.py", "arm"): "the ipmitool guard under /run",
    ("health/recovery/pcie_guard.py", "rescan_tt_root_ports"): "a sysfs control file",
    ("health/recovery/pcie_guard.py", "write"): "a sysfs config-space file",
    ("health/recovery/stages/power_cycle.py", "_write_request"): "the shutdown hook's request under /run, 0600",
    ("health/recovery/stages/power_cycle.py", "write_pending_marker"): "the pending power-cycle marker under /run",
    ("holder_identity.py", "_read"): "a read: its mode is 'rb' or 'r', picked at runtime",
    ("job_spool.py", "__init__"): "the job output spool, private 0700 (path matrix)",
    ("job_spool.py", "open_job_spool"): "the job output spool, private 0700 (path matrix)",
    ("metrics.py", "write_textfile"): "node_exporter's textfile directory, outside both trees (path matrix)",
    ("server.py", "_acquire_device_op_flock"): "the device-op flock under /run, private 0600 (I8)",
    ("server.py", "_device_op"): "the device-op inhibit file under /run",
    ("server.py", "_dispatch_probe"): "the dispatch probe's cache under /var/cache",
    ("server.py", "ensure_job_exit_dir"): "the jobexit dir under /run, its own rule",
    ("server.py", "main"): "the log-dir root, moded by _open_system_log_root at the system default",
    ("socket_transport.py", "serve_unix_socket"): "the socket directory under /run",
    ("utils.py", "open_readable"): "the helper itself",
    ("utils.py", "readable_mkdir"): "the helper itself",
}


def _creates_outside_the_helpers() -> set[tuple[str, str]]:
    """(module, enclosing function) of every write-mode open(), write_text/write_bytes, mkdir and
    O_CREAT os.open in the package that is not the I9 helpers."""
    root = Path(utils.__file__).parent
    found: set[tuple[str, str]] = set()
    for module in root.rglob("*.py"):
        rel = module.relative_to(root).as_posix()
        stack: list[str] = []

        class _Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                name = ast.unparse(node.func)
                create = False
                if name == "open":
                    mode = (
                        node.args[1]
                        if len(node.args) > 1
                        else next((k.value for k in node.keywords if k.arg == "mode"), ast.Constant("r"))
                    )
                    create = not isinstance(mode, ast.Constant) or any(c in str(mode.value) for c in "wax+")
                elif name.endswith((".write_text", ".write_bytes", ".mkdir")) or name in ("os.makedirs", "os.mkdir"):
                    create = True
                elif name == "os.open":
                    create = "O_CREAT" in ast.unparse(node)
                if create:
                    found.add((rel, stack[-1] if stack else "<module>"))
                self.generic_visit(node)

        _Visitor().visit(ast.parse(module.read_text()))
    return found


def test_every_create_in_the_package_goes_through_the_i9_helpers_or_is_named_outside_the_trees():
    """Spec 06 I9: a writer into either tree creates through open_readable / write_text_readable /
    readable_mkdir, so the mode is part of the create and cannot be forgotten. Any other create must
    be named above as outside both trees; an entry that no longer matches a create is stale."""
    found = _creates_outside_the_helpers()
    unexplained = sorted(found - _CREATES_OUTSIDE_THE_TREES.keys())
    assert unexplained == [], f"creates that bypass the I9 helpers: {unexplained}"
    stale = sorted(_CREATES_OUTSIDE_THE_TREES.keys() - found)
    assert stale == [], f"allowlist entries with no create left: {stale}"


def test_a_redirected_health_dir_the_operator_made_keeps_its_mode_while_what_the_broker_writes_inside_is_readable(
    monkeypatch, tmp_path, strict_umask
):
    """The broker brings to mode only what it creates. A `TT_DEVICE_MCP_HEALTH_DIR` pointed at an
    operator's private 0700 directory — and a 0700 `incidents/` they pre-made inside it — stays
    0700 after every kind of write; the files written there are 0644, and a directory the broker
    makes fresh inside (an incident bundle) is 0755."""
    private = tmp_path / "private"
    private.mkdir()  # 0700 under umask 077: the operator's own directory
    (private / evidence.INCIDENTS_DIR).mkdir()
    monkeypatch.setattr(evidence, "HEALTH_DIR", private)
    monkeypatch.setattr(evidence, "kernel_log", lambda: "")

    evidence.health_event("redirected")
    evidence.mark_boot("boot-redirected")
    ServerFsm(private / "fsm.json").on_fault("eth_frozen", detail="redirected")
    assert srv.recovery_mechanism.record_auto_recovery("host_reboot", "redirected") is True
    assert local_reset_mechanism(private, monkeypatch).local_reset_active() is False
    srv._persist_hold_episode(datetime.now().isoformat())
    bundle = evidence.capture_incident("redirected", job={"id": "1"}, reset_output="reset")
    assert bundle is not None

    mode = lambda p: stat.S_IMODE(p.lstat().st_mode)  # noqa: E731 - a one-line reader
    assert mode(private) == 0o700, "the operator's directory is not the broker's to widen"
    assert mode(private / evidence.INCIDENTS_DIR) == 0o700, "nor is a subdirectory they pre-made"
    assert mode(bundle) == 0o755, "but a directory the broker created is at mode"
    files = {p: mode(p) for p in private.rglob("*") if p.is_file()}
    assert {p.name for p in files} >= {
        evidence.EVENTS_FILE,
        evidence.BUSLOCK_FILE,
        "fsm.json",
        recovery_base.AUTO_RECOVERY_LEDGER,
        recovery_base.LOCAL_RESET_LOCK,
        srv.HOLD_EPISODE_FILE,
        "incident.json",
    }
    wrong = {
        str(p): f"{m:04o}"
        for p, m in files.items()
        if m != (0o600 if p.name == recovery_base.LOCAL_RESET_LOCK else 0o644)
    }
    assert not wrong, wrong
