# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for MCP server functionality."""

import asyncio
import json
import os
import tempfile
import venv as venv_module
from collections import deque
from pathlib import Path

import pytest
import yaml

from tests.conftest import patch_recovery
from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.device_holders import HolderScan
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.server import (
    JOB_CAPTURE_MAX_LINES,
    MAX_TIMEOUT_SEC,
    Job,
    JobStatus,
    Stats,
    _scan_output_for_device_fault,
    _sd_notify,
    _tail_lines,
    _watchdog_heartbeat,
    get_activation_script,
    get_machine_name,
    load_env_file,
    percentile,
    timeout_hint,
)

# Real metal-runtime crash line: an eth core that never re-trained at mesh-open.
# An exit code alone misses this (the harness can catch it and exit 1), so the
# gate relies on matching this text to reset before the next runner.
_ETH_CORE_FAULT_LINE = (
    "[17:09:23] [stderr] Device 0: Timed out while waiting for active ethernet "
    "core 27-25 to become active again. Try resetting the board.\n"
)


def _write_log(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "job.log"
    p.write_text(body)
    return p


def test_scan_output_detects_eth_core_fault(tmp_path):
    """The authoritative eth-core-not-active crash text must flag the device, so a
    benign-exit run can't hand a flapping board to the next runner."""
    log = _write_log(tmp_path, "warmup ok\n" + _ETH_CORE_FAULT_LINE + "teardown\n")
    reason = _scan_output_for_device_fault(log)
    assert reason is not None
    assert "ethernet core" in reason


def test_scan_output_detects_fabric_router_timeout(tmp_path):
    log = _write_log(tmp_path, "opening mesh\nFabric Router Sync: Timeout after 10000 ms on Device 9\n")
    assert _scan_output_for_device_fault(log) is not None


def test_scan_output_clean_log_is_none(tmp_path):
    """A normal run that merely logs about ethernet must NOT trip a reset."""
    log = _write_log(tmp_path, "enabled ethernet cores: 12\nPASSED\n")
    assert _scan_output_for_device_fault(log) is None


def test_scan_output_missing_file_is_none(tmp_path):
    assert _scan_output_for_device_fault(None) is None
    assert _scan_output_for_device_fault(tmp_path / "nope.log") is None


def test_scan_output_finds_signature_in_tail_of_large_log(tmp_path):
    """The signature is read from the tail even when the log is large."""
    body = ("noise line\n" * 100000) + _ETH_CORE_FAULT_LINE
    log = _write_log(tmp_path, body)
    assert _scan_output_for_device_fault(log) is not None


def test_next_job_id_is_3_digit_and_wraps(monkeypatch):
    """Ids are zero-padded 3-digit and wrap 999 -> 000."""
    import tt_device_mcp.server as srv

    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "job_counter", 0)
    assert srv.next_job_id() == "001"
    monkeypatch.setattr(srv, "job_counter", 998)
    assert srv.next_job_id() == "999"
    assert srv.next_job_id() == "000"


def test_seed_job_counter_resumes_across_restart(monkeypatch, tmp_path):
    """A restart must not hand out 001 again while 019 is in the recent list — the
    counter is in-memory, so it is recovered from the newest job log."""
    import tt_device_mcp.server as srv

    for name in ("2026-07-13_100000_017.log", "2026-07-13_100500_019.log", "2026-07-13_100200_018.log", "server.log"):
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "job_counter", 0)

    srv.seed_job_counter()
    assert srv.job_counter == 19  # newest log, not max-of-in-memory
    assert srv.next_job_id() == "020"  # continues, does not restart at 001


def test_seed_job_counter_wraps_at_999(monkeypatch, tmp_path):
    import tt_device_mcp.server as srv

    (tmp_path / "2026-07-13_100000_999.log").write_text("x")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "job_counter", 0)

    srv.seed_job_counter()
    assert srv.next_job_id() == "000"


def test_next_job_id_skips_live_ids(monkeypatch):
    """Wraparound (or a counter reset after a restart) must never hand out an id a
    live job still answers to — in memory or in a surviving scope."""
    import tt_device_mcp.server as srv

    live = Job(id="001", owner="b", workspace="/", command="x", queued_at="t")
    monkeypatch.setattr(srv, "jobs", {"001": live})
    monkeypatch.setattr(srv, "job_counter", 0)
    assert srv.next_job_id() == "002"  # 001 in memory -> skipped

    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "job_counter", 0)
    assert srv.next_job_id(frozenset({"001", "002"})) == "003"  # live scopes skipped


def test_a_restart_does_not_reissue_an_id_a_log_still_holds(monkeypatch, tmp_path):
    """recent_jobs returned TWO jobs both called 482: a tenant's killed pytest run, and a
    [broker]fabric-check. Replayed here exactly as it happened.

    An action log is NAMED for the moment its action started, but its id is drawn when the
    action finishes — so a 45s fabric check files itself 45s in the past, behind a job that
    queued while it ran and holds a LOWER id. Name order is no longer id order. The counter,
    rebuilt after a restart from the newest log NAME, lands one step behind an id the log
    directory already owns, and the next job walks onto it.
    """
    import tt_device_mcp.server as srv

    # 20:07:29 a fabric check starts. 20:08:13 a tenant job queues and takes 481. 20:08:14
    # the fabric check finishes and takes 482 — but files itself under 20:07:29.
    (tmp_path / "2026-07-14_200813_481.log").write_text("JOB ID:      481\n")
    (tmp_path / "2026-07-14_200729_482.log").write_text("JOB ID:      482\n")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "job_counter", 0)

    srv.seed_job_counter()  # 20:14:18, the broker restarts
    assert srv.next_job_id() != "482", (
        "handed out 482 while a log on disk still answers to it — two different jobs now "
        "share one id, and recent_jobs shows both"
    )


def test_no_id_in_the_recent_window_is_ever_reissued(monkeypatch, tmp_path):
    """The general property behind it: an id is free only once nothing a user can still be
    shown answers to it. The in-memory job table is not that set — a restart empties it and
    every log survives."""
    import tt_device_mcp.server as srv

    for i in range(40):
        (tmp_path / f"2026-07-14_1000{i:02d}_{i:03d}.log").write_text("x")
    (tmp_path / "server.log").write_text("not a job")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})

    on_disk = {f"{i:03d}" for i in range(40)}
    for counter in (0, 17, 39):
        monkeypatch.setattr(srv, "job_counter", counter)
        assert srv.next_job_id() not in on_disk


def test_timeout_hint_is_actionable():
    """A timed-out job's message must name the limit that fired, the exact lever
    to raise it, and the cap — clear to a human and an agent."""
    msg = timeout_hint(600)
    assert "600s" in msg  # the limit that fired
    assert "tt-device-mcp run -t" in msg  # the CLI lever
    assert "timeout_sec" in msg  # the MCP lever
    assert str(MAX_TIMEOUT_SEC) in msg  # the hard cap


def test_job_output_capture_is_bounded():
    """A chatty job must not grow in-memory capture without bound (that O(n²) is
    what wedged the broker). The deque caps at JOB_CAPTURE_MAX_LINES, keeping the
    newest lines and evicting the oldest."""
    job = Job(id="1-1", owner="b", workspace="/", command="x", queued_at="t")
    assert isinstance(job.out_buf, deque)
    for i in range(JOB_CAPTURE_MAX_LINES + 100):
        job.out_buf.append(f"line{i}\n")
    assert len(job.out_buf) == JOB_CAPTURE_MAX_LINES
    out = "".join(job.out_buf)
    assert "line0\n" not in out  # oldest evicted
    assert f"line{JOB_CAPTURE_MAX_LINES + 99}\n" in out  # newest kept


def test_tail_lines_reads_only_the_tail(tmp_path):
    p = tmp_path / "log"
    p.write_text("".join(f"l{i}\n" for i in range(10000)))
    assert _tail_lines(str(p), 5) == [f"l{i}" for i in range(9995, 10000)]


def test_tail_lines_missing_file_is_empty(tmp_path):
    assert _tail_lines(str(tmp_path / "nope"), 5) == []


def test_sd_notify_noop_without_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    _sd_notify("READY=1")  # must not raise when not under systemd


def test_watchdog_heartbeat_returns_when_unconfigured(monkeypatch):
    """No WATCHDOG_USEC (not under a watchdog unit) -> the heartbeat returns
    immediately rather than looping forever."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    asyncio.run(_watchdog_heartbeat())


def test_the_app_serves_the_shims_synthetic_socket_host():
    """The stdio shim reaches the broker over a UDS under a synthetic Host, so the SDK's
    loopback DNS-rebinding default must stay off or it 421s the only path tenants use.
    """
    from starlette.testclient import TestClient

    import tt_device_mcp.server as srv

    with TestClient(srv.build_asgi_app(srv.create_mcp_server())) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Host": "tt-device-broker", "Accept": "application/json, text/event-stream"},
        )

    assert "Invalid Host header" not in r.text, f"host validation refused the shim: {r.text}"
    # Only the protocol layer answers in a JSON-RPC envelope; the security layer sends text.
    assert r.json()["error"], f"the protocol layer never answered: {r.text}"


@pytest.mark.asyncio
async def test_a_blocking_job_run_reports_progress_through_the_mcp_layer(monkeypatch, tmp_path, clear_job_state):
    """Drives the tool over a real MCP session so the SDK has to inject the `Context`.

    job_run/job_wait are the only tools taking one, and calling them directly skips the
    injection entirely. `ctx.info` is deprecated (SEP-2577), so this fails on its removal.
    """
    from mcp.client import Client

    import tt_device_mcp.server as srv

    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    srv.device_op_lock = None
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "0")

    # Activation accepts a caller's own venv, sparing the test a tt-metal checkout. Built here
    # rather than reused from sys.executable: in production this is the caller's venv, not the
    # broker's, and the broker's own interpreter only has a bin/activate to borrow when whoever
    # installed the package chose a venv. --without-pip keeps it ~50ms.
    venv = tmp_path / "caller-venv"
    venv_module.create(venv, with_pip=False)

    # Bounded deliberately: job_run polls `while status in (QUEUED, RUNNING)` with no
    # deadline, so a job no runner ever dequeues would hang the suite instead of failing.
    async with Client(srv.create_mcp_server()) as client:
        result = await asyncio.wait_for(
            client.call_tool(
                "tt_device_job_run",
                {
                    "params": {
                        "owner": "tester",
                        "workspace": str(tmp_path),
                        "command": "echo hello-from-the-mcp-layer",
                        "timeout_sec": 60,
                        "inherited_env": {"VIRTUAL_ENV": str(venv)},
                    }
                },
            ),
            timeout=90,
        )

    assert result.is_error is False, result.content
    payload = result.structured_content if result.structured_content is not None else json.loads(result.content[0].text)
    assert payload.get("status") == "completed", payload
    assert payload.get("exit_code") == 0, payload
    assert any(
        "hello-from-the-mcp-layer" in line for line in payload.get("output_tail", [])
    ), f"the job's stdout never came back through the session: {payload}"


class TestJob:
    """Tests for Job dataclass."""

    def test_job_creation(self):
        """Test creating a job with required fields."""
        job = Job(
            id="001",
            owner="bjones",
            workspace="/tmp/test",
            command="echo hello",
            queued_at="2024-01-15T14:30:52",
        )
        assert job.id == "001"
        assert job.owner == "bjones"
        assert job.workspace == "/tmp/test"
        assert job.command == "echo hello"
        assert job.queued_at == "2024-01-15T14:30:52"
        assert job.env is None
        assert job.status == JobStatus.QUEUED
        assert job.timeout_sec == 600
        assert job.output == ""
        assert job.error == ""
        assert job.exit_code is None

    def test_job_status_values(self):
        """Test JobStatus enum values."""
        assert JobStatus.QUEUED.value == "queued"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.TIMEOUT.value == "timeout"
        assert JobStatus.HUNG.value == "hung"
        assert JobStatus.KILLED.value == "killed"

    def test_runtime_sec_property(self):
        """Test runtime_sec calculation."""
        job = Job(
            id="001",
            owner="bjones",
            workspace="/tmp",
            command="echo test",
            queued_at="2024-01-15T14:30:00",
            started_at="2024-01-15T14:30:10",
            finished_at="2024-01-15T14:31:15",
        )
        assert job.runtime_sec == 65.0  # 1 minute 5 seconds

    def test_runtime_sec_none_when_not_finished(self):
        """Test runtime_sec is None when job not finished."""
        job = Job(
            id="001",
            owner="bjones",
            workspace="/tmp",
            command="echo test",
            queued_at="2024-01-15T14:30:00",
            started_at="2024-01-15T14:30:10",
        )
        assert job.runtime_sec is None

    def test_wait_sec_property(self):
        """Test wait_sec calculation."""
        job = Job(
            id="001",
            owner="bjones",
            workspace="/tmp",
            command="echo test",
            queued_at="2024-01-15T14:30:00",
            started_at="2024-01-15T14:32:30",
        )
        assert job.wait_sec == 150.0  # 2 minutes 30 seconds

    def test_wait_sec_none_when_not_started(self):
        """Test wait_sec is None when job not started."""
        job = Job(
            id="001",
            owner="bjones",
            workspace="/tmp",
            command="echo test",
            queued_at="2024-01-15T14:30:00",
        )
        assert job.wait_sec is None


class TestEnvFile:
    """Tests for environment file loading."""

    def test_load_env_file_basic(self):
        """Test loading a simple env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, ".tt-env.yaml")
            with open(env_file, "w") as f:
                yaml.dump(
                    {
                        "TT_METAL_HOME": "/path/to/tt-metal",
                        "PYTHONPATH": "/path/to/python",
                        "DEBUG": "1",
                    },
                    f,
                )

            env_vars = load_env_file(env_file, tmpdir)

            assert env_vars["TT_METAL_HOME"] == "/path/to/tt-metal"
            assert env_vars["PYTHONPATH"] == "/path/to/python"
            assert env_vars["DEBUG"] == "1"

    def test_load_env_file_relative_path(self):
        """Test loading env file with path relative to workspace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, "envs.yaml")
            with open(env_file, "w") as f:
                yaml.dump({"VAR1": "value1"}, f)

            # Use relative path
            env_vars = load_env_file("envs.yaml", tmpdir)
            assert env_vars["VAR1"] == "value1"

    def test_load_env_file_absolute_path(self):
        """Test loading env file with absolute path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, "test.yaml")
            with open(env_file, "w") as f:
                yaml.dump({"VAR1": "value1"}, f)

            # Use absolute path (workspace doesn't matter)
            env_vars = load_env_file(env_file, "/different/workspace")
            assert env_vars["VAR1"] == "value1"

    def test_load_env_file_converts_to_strings(self):
        """Test that all values are converted to strings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, "test.yaml")
            with open(env_file, "w") as f:
                yaml.dump(
                    {
                        "NUMBER": 42,
                        "FLOAT": 3.14,
                        "BOOL": True,
                    },
                    f,
                )

            env_vars = load_env_file(env_file, tmpdir)
            assert env_vars["NUMBER"] == "42"
            assert env_vars["FLOAT"] == "3.14"
            assert env_vars["BOOL"] == "True"

    def test_load_env_file_empty(self):
        """Test loading an empty env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, "empty.yaml")
            with open(env_file, "w") as f:
                f.write("")

            env_vars = load_env_file(env_file, tmpdir)
            assert env_vars == {}

    def test_load_env_file_not_found(self):
        """Test that FileNotFoundError is raised for missing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(FileNotFoundError):
                load_env_file(os.path.join(tmpdir, "nonexistent.yaml"), tmpdir)

    def test_load_env_file_invalid_format(self):
        """Test that ValueError is raised for non-dict content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, "invalid.yaml")
            with open(env_file, "w") as f:
                f.write("- item1\n- item2\n")  # List, not dict

            with pytest.raises(ValueError, match="key-value pairs"):
                load_env_file(env_file, tmpdir)


class TestActivationScript:
    """Tests for workspace activation script generation."""

    def test_activation_script_default(self):
        """Test default activation script (no env file)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            script, env_vars = get_activation_script(tmpdir)

            tt_metal = f"{tmpdir}/tt-metal"
            assert f'TT_METAL_HOME="{tt_metal}"' in script
            assert f'PYTHONPATH="{tt_metal}"' in script
            assert f'source "{tt_metal}/python_env/bin/activate"' in script
            assert f'cd "{tmpdir}"' in script

            # Check env_vars dict
            assert env_vars["TT_METAL_HOME"] == tt_metal
            assert env_vars["PYTHONPATH"] == tt_metal

    def test_activation_script_default_paths(self):
        """Test that default paths are correctly constructed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            script, env_vars = get_activation_script(tmpdir)

            assert f"{tmpdir}/tt-metal" in script
            assert f"{tmpdir}/.tt-metal-cache" in script
            assert f"{tmpdir}/tt-metal/python_env" in script

            # Check env_vars dict
            assert env_vars["TT_METAL_CACHE"] == f"{tmpdir}/.tt-metal-cache"

    def test_activation_script_with_env_file(self):
        """Test activation script using env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tt_metal_dir = os.path.join(tmpdir, "custom-tt-metal")
            env_file = os.path.join(tmpdir, ".tt-env.yaml")
            with open(env_file, "w") as f:
                yaml.dump(
                    {
                        "TT_METAL_HOME": tt_metal_dir,
                        "CUSTOM_VAR": "custom_value",
                    },
                    f,
                )

            script, env_vars = get_activation_script(tmpdir, env_file)

            # Should have custom env vars
            assert f'TT_METAL_HOME="{tt_metal_dir}"' in script
            assert 'CUSTOM_VAR="custom_value"' in script

            # Should NOT have default env vars
            assert ".tt-metal-cache" not in script

            # Should use TT_METAL_HOME/python_env (not workspace default)
            assert f'source "{tt_metal_dir}/python_env/bin/activate"' in script
            assert f'cd "{tmpdir}"' in script

            # Check env_vars dict
            assert env_vars["TT_METAL_HOME"] == tt_metal_dir
            assert env_vars["CUSTOM_VAR"] == "custom_value"

    def test_activation_script_env_file_relative(self):
        """Test activation script with relative env file path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, "envs.yaml")
            with open(env_file, "w") as f:
                yaml.dump({"MY_VAR": "my_value"}, f)

            # Use relative path
            script, env_vars = get_activation_script(tmpdir, "envs.yaml")
            assert 'MY_VAR="my_value"' in script
            assert env_vars["MY_VAR"] == "my_value"

    def test_activation_script_inherited_env(self):
        """Test activation script with inherited env vars."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tt_metal_dir = os.path.join(tmpdir, "tt-metal")
            inherited = {
                "TT_METAL_HOME": tt_metal_dir,
                "CUSTOM_VAR": "inherited_value",
            }
            script, env_vars = get_activation_script(tmpdir, inherited_env=inherited)

            # Should use inherited values
            assert f'TT_METAL_HOME="{tt_metal_dir}"' in script
            assert 'CUSTOM_VAR="inherited_value"' in script

            # Check env_vars dict
            assert env_vars == inherited

    def test_activation_script_env_file_python_env_dir(self):
        """Test that PYTHON_ENV_DIR in env file is used for activation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tt_metal_dir = os.path.join(tmpdir, "tt-metal")
            python_env_dir = os.path.join(tmpdir, "custom_python_env")

            env_file = os.path.join(tmpdir, ".tt-env.yaml")
            with open(env_file, "w") as f:
                yaml.dump(
                    {
                        "TT_METAL_HOME": tt_metal_dir,
                        "PYTHON_ENV_DIR": python_env_dir,
                    },
                    f,
                )

            script, env_vars = get_activation_script(tmpdir, env_file)

            # Should activate the custom python env
            assert f'source "{python_env_dir}/bin/activate"' in script
            # Should NOT use the default workspace python env
            assert f"{tmpdir}/tt-metal/python_env" not in script

    def test_activation_script_inherited_python_env_dir(self):
        """Test that PYTHON_ENV_DIR in inherited env is used for activation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tt_metal_dir = os.path.join(tmpdir, "tt-metal")
            python_env_dir = os.path.join(tmpdir, "custom_python_env")

            inherited = {
                "TT_METAL_HOME": tt_metal_dir,
                "PYTHON_ENV_DIR": python_env_dir,
            }
            script, env_vars = get_activation_script(tmpdir, inherited_env=inherited)

            # Should activate the custom python env
            assert f'source "{python_env_dir}/bin/activate"' in script
            # Should NOT use the default workspace python env
            assert f"{tmpdir}/tt-metal/python_env" not in script

    def test_activation_script_virtual_env_fallback(self):
        """Test that VIRTUAL_ENV is used as fallback when no PYTHON_ENV_DIR."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tt_metal_dir = os.path.join(tmpdir, "tt-metal")
            venv_dir = os.path.join(tmpdir, "activated_venv")

            inherited = {
                "TT_METAL_HOME": tt_metal_dir,
                "VIRTUAL_ENV": venv_dir,
            }
            script, env_vars = get_activation_script(tmpdir, inherited_env=inherited)

            # Should use VIRTUAL_ENV for activation
            assert f'source "{venv_dir}/bin/activate"' in script
            # VIRTUAL_ENV should NOT be exported (we activate it, not export it)
            assert "export VIRTUAL_ENV=" not in script

    def test_activation_script_python_env_dir_over_virtual_env(self):
        """Test that PYTHON_ENV_DIR takes priority over VIRTUAL_ENV."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tt_metal_dir = os.path.join(tmpdir, "tt-metal")
            python_env_dir = os.path.join(tmpdir, "explicit_python_env")
            venv_dir = os.path.join(tmpdir, "activated_venv")

            inherited = {
                "TT_METAL_HOME": tt_metal_dir,
                "PYTHON_ENV_DIR": python_env_dir,
                "VIRTUAL_ENV": venv_dir,
            }
            script, env_vars = get_activation_script(tmpdir, inherited_env=inherited)

            # Should use PYTHON_ENV_DIR, not VIRTUAL_ENV
            assert f'source "{python_env_dir}/bin/activate"' in script
            assert venv_dir not in script

    def test_activation_script_validate_missing_env(self):
        """Test that validate=True raises FileNotFoundError for missing python env."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # tt_metal_dir exists but python_env inside it does not
            tt_metal_dir = os.path.join(tmpdir, "tt-metal")
            os.makedirs(tt_metal_dir)

            inherited = {
                "TT_METAL_HOME": tt_metal_dir,
            }
            with pytest.raises(FileNotFoundError) as exc_info:
                get_activation_script(tmpdir, inherited_env=inherited, validate=True)

            assert f"{tt_metal_dir}/python_env" in str(exc_info.value)

    def test_activation_script_validate_existing_env(self):
        """Test that validate=True succeeds when python env exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake python_env structure
            tt_metal_dir = os.path.join(tmpdir, "tt-metal")
            python_env_bin = os.path.join(tt_metal_dir, "python_env", "bin")
            os.makedirs(python_env_bin)
            activate_file = os.path.join(python_env_bin, "activate")
            with open(activate_file, "w") as f:
                f.write("# fake activate script")

            inherited = {
                "TT_METAL_HOME": tt_metal_dir,
            }
            # Should not raise
            script, env_vars = get_activation_script(tmpdir, inherited_env=inherited, validate=True)
            assert f'source "{tt_metal_dir}/python_env/bin/activate"' in script


class TestCleanDeviceGate:
    """Tests for the clean-device gate + graceful termination (wedge avoidance)."""

    def test_wedge_risk_truth_table(self):
        """KILLED/TIMEOUT and signal-crashes are wedge-risk; clean/app-error exits aren't."""
        from tt_device_mcp.server import _is_wedge_risk_exit

        assert _is_wedge_risk_exit(JobStatus.KILLED, None) is True
        assert _is_wedge_risk_exit(JobStatus.KILLED, -9) is True
        assert _is_wedge_risk_exit(JobStatus.TIMEOUT, None) is True
        assert _is_wedge_risk_exit(JobStatus.FAILED, -9) is True  # SIGKILL
        assert _is_wedge_risk_exit(JobStatus.FAILED, -11) is True  # SIGSEGV
        # NOT wedge-risk: clean completion, or an application error with a normal exit code
        assert _is_wedge_risk_exit(JobStatus.COMPLETED, 0) is False
        assert _is_wedge_risk_exit(JobStatus.FAILED, 1) is False
        assert _is_wedge_risk_exit(JobStatus.FAILED, 42) is False

    def test_mark_and_clear_dirty(self):
        """Marking sets the flag+reason; clearing resets both."""
        import tt_device_mcp.server as srv

        srv._clear_device_dirty(verified=True)
        assert srv.fsm.state is ServerState.HEALTHY
        srv._mark_device_dirty("job 1 ended killed")
        assert srv.fsm.state is not ServerState.HEALTHY
        assert "killed" in srv.fsm.record.detail
        srv._clear_device_dirty(verified=True)
        assert srv.fsm.state is ServerState.HEALTHY
        assert srv.fsm.record.detail == ""

    @pytest.mark.asyncio
    async def test_clean_gate_noop_when_clean(self, monkeypatch):
        """With the snapshot check disabled and the device not dirty, the pre-job
        gate returns immediately without touching the device or raising."""
        import tt_device_mcp.server as srv

        monkeypatch.setenv("TT_DEVICE_MCP_HEALTH_CHECK", "0")
        srv._clear_device_dirty(verified=True)
        await srv._ensure_device_clean_for_next_job(None)
        assert srv.fsm.state is ServerState.HEALTHY

    @pytest.mark.asyncio
    async def test_verify_fabric_health_skips_when_unconfigured(self, monkeypatch):
        """No fabric command configured => skipped (ok=None), never a reset trigger."""
        import tt_device_mcp.server as srv

        monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
        ok, detail = await srv.health_monitor.verify_fabric_health(timeout_sec=5)
        assert ok is None and "not set" in detail

    @pytest.mark.asyncio
    async def test_verify_fabric_health_passes_on_exit_zero(self, monkeypatch):
        """Exit 0 from the configured command => fabric healthy."""
        import tt_device_mcp.server as srv

        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "echo links-ok; exit 0")
        ok, detail = await srv.health_monitor.verify_fabric_health(timeout_sec=10)
        assert ok is True

    @pytest.mark.asyncio
    async def test_a_timed_out_fabric_check_keeps_what_it_printed(self, monkeypatch):
        """A wedge is diagnosable ONLY from what the check printed before it hung.

        The timed-out check is the one we most need to read, and it is exactly the one that
        used to be discarded: a real wedge left nothing but "timed out after 600s", naming
        neither the failing link nor the stage it hung in.
        """
        import tt_device_mcp.server as srv

        srv.health_monitor.last_fabric_output = ""
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "echo probing-link-27-25; sleep 30")
        ok, detail = await srv.health_monitor.verify_fabric_health(timeout_sec=2)

        assert ok is False
        assert "timed out" in detail
        assert (
            "probing-link-27-25" in srv.health_monitor.last_fabric_output
        ), "the check's output was thrown away — the wedge is undiagnosable"
        assert "probing-link-27-25" in detail, "the last line must reach the gate's verdict"

    @pytest.mark.asyncio
    async def test_a_timed_out_fabric_check_leaves_no_process_mapping_the_chips(self, monkeypatch):
        """The validator maps every chip's BARs. Orphaning it across a reset is how a host dies.

        The check is spawned via `bash -c` under setsid, so killing only the process leaves the
        validator itself alive, still holding the mesh we are about to reset.
        """
        import os

        import tt_device_mcp.server as srv

        marker = "/tmp/blxfix_fabric_orphan_probe"
        if os.path.exists(marker):
            os.remove(marker)  # a prior run's marker would make this always-fail
        # `&&`, not `;`: the timeout now stops the check with SIGINT (so a real validator unwinds and
        # releases the mesh), which would leave a `; touch` to still run. `&&` short-circuits on the
        # interrupted sleep's non-zero exit, so the marker appears only if the grandchild ran to
        # completion — i.e. was NOT reaped.
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", f"(sleep 30 && touch {marker}) & echo $!; wait")
        ok, _ = await srv.health_monitor.verify_fabric_health(timeout_sec=2)
        assert ok is False

        # The grandchild must be gone: _terminate_process_group signals the whole session.
        await asyncio.sleep(0.3)
        assert srv.health_monitor.fabric_check_proc is None, "a stale handle survives the timeout"
        assert not os.path.exists(marker)

    @pytest.mark.asyncio
    async def test_verify_fabric_health_fails_on_nonzero(self, monkeypatch):
        """Non-zero exit => fabric unhealthy (caller should reset)."""
        import tt_device_mcp.server as srv

        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "echo bad-link >&2; exit 3")
        ok, detail = await srv.health_monitor.verify_fabric_health(timeout_sec=10)
        assert ok is False and "exited 3" in detail

    @pytest.mark.asyncio
    async def test_fabric_cannot_check_is_recorded_as_skipped_not_failed(self, monkeypatch):
        """Exit 77 = 'could not check', not a failure — recent history must say skipped, not
        failed, so a check that never got a verdict does not read as a broken fabric."""
        import tt_device_mcp.server as srv

        logged = []
        monkeypatch.setattr(srv, "write_action_log", lambda owner, cmd, rt, status, ec: logged.append(status))
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", f"exit {FABRIC_CHECK_CANNOT_CHECK_RC}")
        ok, _ = await srv.health_monitor.verify_fabric_health(timeout_sec=10)
        assert ok is None  # skipped => never a reset trigger
        assert logged == ["skipped"]

    @pytest.mark.asyncio
    async def test_post_job_gate_resets_when_fabric_unhealthy(self, monkeypatch, tmp_path):
        """A FAILED job (kill-mid-CCL) forces the fabric pass, so the post-job gate
        catches an unhealthy fabric on a chip-healthy snapshot and resets + verifies."""
        import tt_device_mcp.server as srv

        (tmp_path / "0").write_text("")
        (tmp_path / "1").write_text("")
        monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
        monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
        # snapshot healthy, fabric unhealthy
        monkeypatch.setattr(srv.health_monitor, "verify_device_health", lambda n, **k: (True, f"all {n} chips ok"))
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "exit 7")

        reset_calls = {"n": 0}

        async def fake_reset(indices, log):
            reset_calls["n"] += 1
            log("fake reset ran")
            return True

        patch_recovery(monkeypatch, "_reset_and_verify_device", fake_reset)
        srv._clear_device_dirty()
        await srv._verify_device_after_job(None, job_failed=True)
        assert reset_calls["n"] == 1  # fabric failure triggered a reset in the gap

    @pytest.mark.asyncio
    async def test_post_job_gate_skips_fabric_on_a_clean_exit(self, monkeypatch, tmp_path):
        """A CLEAN job does not pay for the ~45s fabric pass: with an unhealthy fabric cmd
        that WOULD fail if run, a clean exit resets nothing — the next job's failure forces
        the check instead. last_fabric_check is stale, proving the clean exit (not freshness)
        is what skips it. On base (post-job run_fabric=True) this reset."""
        import tt_device_mcp.server as srv

        (tmp_path / "0").write_text("")
        (tmp_path / "1").write_text("")
        monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
        monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
        monkeypatch.setattr(srv.health_monitor, "verify_device_health", lambda n, **k: (True, f"all {n} chips ok"))
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "exit 7")  # would fail IF run
        srv.last_fabric_check_monotonic = 0.0  # stale — not the reason it skips

        reset_calls = {"n": 0}

        async def fake_reset(indices, log):
            reset_calls["n"] += 1
            return True

        patch_recovery(monkeypatch, "_reset_and_verify_device", fake_reset)
        srv._clear_device_dirty()
        await srv._verify_device_after_job(None, job_failed=False)
        assert reset_calls["n"] == 0, "a clean exit must not run the fabric pass or reset on it"

    @pytest.mark.asyncio
    async def test_post_job_gate_no_reset_when_all_healthy(self, monkeypatch, tmp_path):
        """A healthy mesh between runs: snapshot OK + fabric OK => no reset."""
        import tt_device_mcp.server as srv

        (tmp_path / "0").write_text("")
        monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
        monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
        monkeypatch.setattr(srv.health_monitor, "verify_device_health", lambda n, **k: (True, "ok"))
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "exit 0")

        reset_calls = {"n": 0}

        async def fake_reset(indices, log):
            reset_calls["n"] += 1
            return True

        patch_recovery(monkeypatch, "_reset_and_verify_device", fake_reset)
        srv._clear_device_dirty()
        await srv._verify_device_after_job(None)
        assert reset_calls["n"] == 0  # nothing wrong => no reset

    @pytest.mark.asyncio
    async def test_pre_job_gate_acts_only_when_dirty(self, monkeypatch, tmp_path):
        """The pre-job gate is a no-op (zero cost) on a clean device, and resets only
        when a prior wedge-risk exit left it dirty with a drained queue."""
        import tt_device_mcp.server as srv

        (tmp_path / "0").write_text("")
        monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
        monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=[], complete=True))
        monkeypatch.setattr(srv.health_monitor, "verify_device_health", lambda n, **k: (True, "ok"))

        gate_calls = {"n": 0}

        async def fake_gate(job_log_file, *, phase, run_fabric):
            gate_calls["n"] += 1
            assert run_fabric is False  # pre-job never runs the expensive fabric check
            srv._clear_device_dirty()

        monkeypatch.setattr(srv, "_device_health_gate", fake_gate)

        srv._clear_device_dirty()
        await srv._ensure_device_clean_for_next_job(None)
        assert gate_calls["n"] == 0  # clean -> no work, no cost

        srv._mark_device_dirty("job ended killed")
        await srv._ensure_device_clean_for_next_job(None)
        assert gate_calls["n"] == 1  # dirty -> reset before the inheriting run

    @pytest.mark.asyncio
    async def test_graceful_terminate_on_sigterm(self):
        """A well-behaved process exits on SIGTERM (no SIGKILL needed)."""
        from tt_device_mcp.server import _terminate_process_group

        proc = await asyncio.create_subprocess_shell(
            "sleep 300",
            preexec_fn=os.setsid,
            executable="/bin/bash",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.1)
        await _terminate_process_group(proc.pid, grace_sec=5)
        await asyncio.wait_for(proc.wait(), timeout=2)
        assert proc.returncode is not None  # terminated within grace

    @pytest.mark.asyncio
    async def test_graceful_escalates_to_sigkill(self):
        """A process that ignores SIGTERM is escalated to SIGKILL after the grace."""
        from tt_device_mcp.server import _terminate_process_group

        # Parent ignores SIGTERM; only SIGKILL (uncatchable) can stop it.
        proc = await asyncio.create_subprocess_shell(
            "trap '' TERM; sleep 300",
            preexec_fn=os.setsid,
            executable="/bin/bash",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.2)
        await _terminate_process_group(proc.pid, grace_sec=1)
        await asyncio.wait_for(proc.wait(), timeout=3)
        assert proc.returncode is not None  # SIGKILL reaped it

    @pytest.mark.asyncio
    async def test_graceful_terminate_already_dead(self):
        """Terminating an already-exited process group is a safe no-op."""
        from tt_device_mcp.server import _terminate_process_group

        proc = await asyncio.create_subprocess_shell("exit 0", preexec_fn=os.setsid, executable="/bin/bash")
        await proc.wait()
        # Should not raise.
        await _terminate_process_group(proc.pid, grace_sec=1)


class TestStatistics:
    """Tests for statistics tracking."""

    def test_percentile_empty(self):
        """Test percentile with empty list."""
        assert percentile([], 50) == 0.0

    def test_percentile_single(self):
        """Test percentile with single value."""
        assert percentile([10.0], 50) == 10.0
        assert percentile([10.0], 95) == 10.0

    def test_percentile_multiple(self):
        """Test percentile with multiple values."""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert percentile(values, 50) == 3.0
        assert percentile(values, 0) == 1.0
        assert percentile(values, 100) == 5.0

    def test_percentile_interpolation(self):
        """Test percentile interpolation."""
        values = [1.0, 2.0, 3.0, 4.0]
        # P50 should interpolate between 2 and 3
        p50 = percentile(values, 50)
        assert 2.0 <= p50 <= 3.0

    def test_get_machine_name_simple(self):
        """Test machine name extraction."""
        # Just verify function runs without error
        name = get_machine_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_stats_creation(self):
        """Test Stats creation with defaults."""
        s = Stats()
        assert s.machine is not None
        assert s.session_start is not None
        assert s.session_end is None
        assert s.busy_sec == 0.0
        assert s.idle_sec == 0.0
        assert s.jobs_completed == 0
        assert s.jobs_failed == 0
        assert s.jobs_killed == 0
        assert s.jobs_timeout == 0
        assert s.wait_times == []

    def test_stats_job_counts(self):
        """Test recording job completions."""
        s = Stats()
        s.record_job_completion(JobStatus.COMPLETED, 10.0)
        s.record_job_completion(JobStatus.COMPLETED, 20.0)
        s.record_job_completion(JobStatus.FAILED, 5.0)
        s.record_job_completion(JobStatus.KILLED, None)
        s.record_job_completion(JobStatus.TIMEOUT, 30.0)

        assert s.jobs_completed == 2
        assert s.jobs_failed == 1
        assert s.jobs_killed == 1
        assert s.jobs_timeout == 1
        assert s.total_jobs == 5
        assert s.wait_times == [10.0, 20.0, 5.0, 30.0]

    def test_stats_wait_percentiles(self):
        """Test wait time percentile calculations."""
        s = Stats()
        # Add some wait times
        for i in range(1, 101):
            s.wait_times.append(float(i))

        # P50 should be around 50, P95 around 95
        assert 49 <= s.wait_p50 <= 51
        assert 94 <= s.wait_p95 <= 96
        assert s.wait_max == 100.0
        assert s.wait_total == 5050.0  # Sum 1..100

    def test_stats_utilization(self):
        """Test utilization calculation returns percentage."""
        s = Stats()
        s.busy_sec = 60.0
        s.idle_sec = 40.0

        assert s.utilization == 60.0

    def test_stats_utilization_zero(self):
        """Test utilization with no time tracked."""
        s = Stats()
        assert s.utilization == 0.0

    def test_stats_to_dict(self):
        """Test stats JSON serialization."""
        s = Stats()
        s.jobs_completed = 10
        s.jobs_failed = 2
        s.busy_sec = 100.0
        s.idle_sec = 50.0
        s.wait_times = [1.0, 2.0, 3.0]

        d = s.to_dict()

        assert "machine" in d
        assert "session_start" in d
        assert "last_update" in d
        assert d["jobs"]["completed"] == 10
        assert d["jobs"]["failed"] == 2
        assert d["jobs"]["total"] == 12
        assert "utilization" in d["device"]
        assert "p50" in d["waits"]
        assert "p95" in d["waits"]
