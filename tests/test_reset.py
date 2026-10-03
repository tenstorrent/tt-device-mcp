# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the device-reset route. tt-smi and the device dir are mocked, so this
never resets real hardware; it exercises the gate→exec→result flow and the
verbose step/command/returncode payload the CLI prints."""

import asyncio
import os
import subprocess
import sys
import threading
import time

import pytest
from starlette.testclient import TestClient

import tt_device_mcp.server as srv
from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tt_device_mcp.device_holders import DeviceHolder, HolderScan
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.recovery import _declared_reset_mode, reset_mode_known, select_recovery
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery.galaxy import _is_galaxy as _galaxy_is_galaxy


def _is_galaxy(deps):
    """``_is_galaxy`` reshaped for these tests: the board-types cache stays on ``srv`` (see
    ``health_deps``), the matching itself lives in ``health.recovery.galaxy``."""
    return _galaxy_is_galaxy(deps.board_types_provider(), deps.glx_board_types_provider())


def test_smi_args_ok_allows_readonly_rejects_reset():
    # bare = interactive dashboard; read-only flags + positional values allowed
    assert srv.smi_args_ok([])
    assert srv.smi_args_ok(["-ls"])
    assert srv.smi_args_ok(["-s"])
    assert srv.smi_args_ok(["-f", "/tmp/snap.json"])  # filename positional is fine
    # anything that could reset/reconfigure the device is rejected
    assert not srv.smi_args_ok(["-r"])
    assert not srv.smi_args_ok(["-r", "0,1"])
    assert not srv.smi_args_ok(["-glx_reset"])
    assert not srv.smi_args_ok(["-glx_reset_tray", "1"])
    assert not srv.smi_args_ok(["-c"])  # config write


def test_reset_argv_is_machine_type_aware(monkeypatch, health_deps):
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    # Default (loudbox, n150/n300, anything non-galaxy): per-target -r.
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0", "1", "2"]) == [
        "tt-smi",
        "-r",
        "0,1,2",
    ]
    # 6U Galaxy: reset all ASICs, no targets.
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0", "1"]) == ["tt-smi", "-glx_reset"]
    # Explicit override wins over mode.
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_ARGS", "tt-smi -glx_reset_auto")
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0"]) == ["tt-smi", "-glx_reset_auto"]


# --- runtime guard: a multi-chip reset must never fire on a guessed -r -----------------------
#
# The mode is derived from tt-smi, so it is unknown only where tt-smi could not be read. There
# `-r` is a guess that no-ops on a Galaxy, and doctrine forbids that reset landing silently.


async def _drive_reset(monkeypatch, indices, *, expected):
    """Drive _reset_and_verify_device with the reset seam stubbed hermetic, returning the ordered
    list of health-event names it emitted."""
    events = []
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", str(expected))

    async def no_foreign(log):
        return False

    async def quiesced_reset(argv, log):
        return 0, ""

    async def ok_verify(exp, log, **k):
        return True, {"snapshot": {"detail": "ok"}}

    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", no_foreign)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", quiesced_reset)
    patch_recovery(monkeypatch, "_verify_device", ok_verify)
    patch_health_event(monkeypatch, lambda k, **kw: events.append(k))

    await srv.galaxy_recovery._reset_and_verify_device(indices, lambda m: None)
    return events


@pytest.mark.asyncio
async def test_multichip_reset_with_unknown_mode_is_loud(monkeypatch):
    """expected>1, nothing declared, and tt-smi unreadable (the conftest default) — so `-r` is a
    guess that cannot recover a Galaxy. It must journal reset_mode_unknown BEFORE it fires."""
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    events = await _drive_reset(monkeypatch, ["0", "1"], expected=2)
    assert "reset_mode_unknown" in events, "a multi-chip -r no-op reset fired silently"
    assert events.index("reset_mode_unknown") < events.index(
        "reset_begin"
    ), "the warning must precede the reset it warns about"


@pytest.mark.asyncio
async def test_multichip_reset_is_silent_when_tt_smi_names_the_board(monkeypatch):
    """Nothing declared, but tt-smi identified a non-Galaxy board — `-r` is then derived, not
    guessed, so there is nothing to warn about."""
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    monkeypatch.setattr(srv.health_monitor, "_glx_board_types", lambda: ("tt-galaxy-wh", "tt-galaxy-bh"))
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["n300 L"])
    events = await _drive_reset(monkeypatch, ["0", "1"], expected=2)
    assert "reset_mode_unknown" not in events, "a derived reset mode must not warn"


@pytest.mark.asyncio
async def test_multichip_reset_with_declared_mode_is_silent(monkeypatch):
    """A declared reset mode is the operator's explicit choice — galaxy here — so there is nothing
    to warn about; the guard must stay quiet."""
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    events = await _drive_reset(monkeypatch, ["0", "1"], expected=2)
    assert "reset_mode_unknown" not in events, "a declared reset mode must not warn"


@pytest.mark.asyncio
async def test_singlechip_reset_with_undeclared_mode_is_silent(monkeypatch):
    """On a single-chip host per-target `-r` is exactly right, so an undeclared mode is no defect —
    the guard is scoped to multi-chip hosts and must not warn here."""
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    events = await _drive_reset(monkeypatch, ["0"], expected=1)
    assert "reset_mode_unknown" not in events, "a single-chip host needs no reset mode"


def _fake_tt_smi(returncode, stdout="", stderr="", *, snapshot_chips=2):
    """Fake tt-smi for the reset route. Handles the reset (``-r``) call with the
    given returncode, and the post-reset health snapshot (``-s``) by emitting a
    healthy snapshot JSON on STDOUT with ``snapshot_chips`` chips so a successful
    reset verifies."""

    def run(argv, capture_output=True, text=True, timeout=None, **kwargs):
        assert argv[0] == "tt-smi"
        if argv[1] == "-s":
            import json

            return subprocess.CompletedProcess(argv, 0, json.dumps(_snapshot(snapshot_chips)), "")
        assert argv[1] == "-r"
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


def _fake_scoped_reset(monkeypatch, rc, output=""):
    """Stand in for the scoped reset. The real one execs `systemd-run --scope` so the
    reset outlives a broker restart; that is covered in test_device_safety.py. Here we
    only care about how the route reports its result, so intercept at that seam."""
    calls = []

    async def run(argv, log, owner="[broker]health-gate", on_output=None):
        calls.append(list(argv))
        if output and on_output is not None:
            on_output(output)
        return rc, output

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    return calls


def _client(monkeypatch, dev_dir, holders=None):
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(dev_dir))
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: HolderScan(holders=holders or [], complete=True))
    return TestClient(srv.build_asgi_app(srv.create_mcp_server()))


def test_reset_success_reports_steps_and_command(monkeypatch, tmp_path):
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0))
    calls = _fake_scoped_reset(monkeypatch, 0, "Resetting...\nRe-initializing boards\n")
    d = _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False}).json()

    assert d["status"] == "reset_complete"
    assert d["returncode"] == 0
    assert d["command"] == "tt-smi -r 0,1" or d["command"] == "tt-smi -r 1,0"
    assert set(d["devices"]) == {"0", "1"}
    assert "Resetting" in d["stdout"]
    assert any(s.startswith("exec: tt-smi -r") for s in d["steps"])
    assert calls and calls[0][0] == "tt-smi"


def test_reset_failure_surfaces_returncode_and_output(monkeypatch, tmp_path):
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(1))
    _fake_scoped_reset(monkeypatch, 1, "tt-smi: reset failed")
    d = _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False}).json()

    assert d["status"] == "reset_failed"
    assert d["returncode"] == 1
    # The scoped reset merges stderr into stdout: one ordered stream is what you want
    # when reading a reset's tail.
    assert "reset failed" in d["stdout"]


def _intercept_scoped_subprocess(monkeypatch, rc=0, output="Resetting...\n"):
    """Intercept the reset at the systemd-run/create_subprocess_exec seam, NOT at
    _run_reset_scoped — so the real scoped runner runs, including the action-log row it
    writes. (_fake_scoped_reset stubs the whole runner out and hides that row.)"""

    async def fake_exec(*argv, **kwargs):
        class P:
            returncode = rc

            async def communicate(self):
                return output.encode(), b""

        return P()

    async def pollers(active, log):
        return []

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)


def test_streaming_reset_quiesces_pollers_and_flags_in_flight(monkeypatch, tmp_path):
    """The streaming operator reset used to exec tt-smi directly, skipping the poller quiesce that
    every other reset gets via _reset_with_quiesce. The reset takes every chip off the bus, and a
    poller (tt-telemetry / metrics exporter) reading an off-bus endpoint is the MMIO stall that
    reboots the host — so it must quiesce pollers around the reset and set reset_in_flight, then
    rescan after."""
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    events = []

    async def pollers(active, log):
        events.append(("pollers", active))
        return ["telemetry"] if not active else []

    async def fake_exec(*argv, **kwargs):
        events.append(("exec", srv.recovery_mechanism.reset_in_flight))

        class _Out:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class P:
            returncode = 0
            stdout = _Out()

            async def communicate(self):
                return b"", None

            async def wait(self):
                return 0

        return P()

    async def fake_to_thread(fn, *a, **k):
        events.append(("rescan_write",))  # the only to_thread on this path is the pci rescan
        return None

    async def no_sleep(_):
        return None

    async def ok_verify(expected, log, **k):
        return True, {"snapshot": {"detail": "ok"}}

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(srv.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    patch_recovery(monkeypatch, "_verify_device", ok_verify)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    srv.recovery_mechanism.reset_in_flight = False

    resp = _client(monkeypatch, tmp_path).post("/api/tt_device_reset_stream", json={"force": True})
    assert resp.status_code == 200

    assert ("pollers", False) in events, "streaming reset never quiesced the MMIO pollers"
    i_false = events.index(("pollers", False))
    i_exec = next(n for n, e in enumerate(events) if e[0] == "exec")
    assert i_false < i_exec, "pollers must be quiesced BEFORE the reset subprocess runs"
    assert events[i_exec] == ("exec", True), "reset_in_flight must be set while the reset runs"
    i_true = next((n for n, e in enumerate(events) if e == ("pollers", True)), None)
    assert i_true is not None and i_true > i_exec, "pollers must be restored after the reset"
    assert ("rescan_write",) in events, "the post-reset PCI rescan must run"
    assert srv.recovery_mechanism.reset_in_flight is False, "reset_in_flight must be cleared after the reset"
    rows = [row for row in srv._recent_jobs(20) if row["command"].startswith("tt-smi -r")]
    assert len(rows) == 1
    assert rows[0]["owner"] == "unknown"


def test_explicit_reset_leaves_one_row_attributed_to_the_caller(monkeypatch, tmp_path):
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0))
    _intercept_scoped_subprocess(monkeypatch, rc=0)

    d = _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False}).json()
    assert d["status"] == "reset_complete"

    rows = [r for r in srv._recent_jobs(20) if r["command"].startswith("tt-smi -r")]
    assert len(rows) == 1, f"an explicit reset must leave one row, got {[(r['owner'], r['command']) for r in rows]}"
    assert rows[0]["owner"] == "unknown"
    assert rows[0]["owner"] != "[broker]health-gate"


def test_a_reset_row_is_owned_by_the_caller_not_by_what_they_posted(monkeypatch, tmp_path):
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0))
    _intercept_scoped_subprocess(monkeypatch, rc=0)

    d = _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False, "owner": "smarton"}).json()
    assert d["status"] == "reset_complete"

    rows = [r for r in srv._recent_jobs(20) if r["command"].startswith("tt-smi -r")]
    assert len(rows) == 1
    assert rows[0]["owner"] != "smarton", "a posted owner must not name the reset's owner"
    # TestClient carries no SO_PEERCRED, so there is no identity to derive and the row says so
    # rather than believing the body.
    assert rows[0]["owner"] == "unknown"


def test_reset_no_devices_does_not_call_tt_smi(monkeypatch, tmp_path):
    called = {"n": 0}

    def run(*a, **k):
        called["n"] += 1
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(srv.subprocess, "run", run)
    d = _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False}).json()

    assert d["status"] == "no_devices"
    assert called["n"] == 0  # never shells out when there are no device nodes


# --- privsep host: an HTTP reset has no peer identity, so it must not skip the gate ---
#
# On a privsep host every job is scoped to its SO_PEERCRED submitter. An HTTP reset carries no
# such identity, and the gate used to skip entirely for it — silently resetting the whole board
# over whatever foreign tenant held it. With no caller identity every real tenant is foreign, so
# the un-scoped reset must fail closed (force still overrides). Off privsep it stays legacy skip.


def test_privsep_http_reset_refuses_over_a_foreign_holder(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_DEVICE_MCP_PRIVSEP", "1")
    (tmp_path / "0").write_text("")
    holders = [DeviceHolder(pid=4242, uid=2000)]  # a real tenant; the HTTP caller has no identity
    _fake_scoped_reset(monkeypatch, 0, "Resetting...\n")  # would run if the gate were skipped
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0))
    d = _client(monkeypatch, tmp_path, holders=holders).post("/api/tt_device_reset", json={"force": False}).json()
    assert d["status"] == "refused", "an anonymous HTTP reset nuked a foreign tenant on a privsep host"
    assert d["foreign_holders"] and d["foreign_holders"][0]["uid"] == 2000


def test_privsep_http_reset_allows_a_provably_idle_device(monkeypatch, tmp_path):
    # Fail closed means refuse only when a foreign holder can't be ruled out. A complete scan with
    # no tenant holder rules one out, so the anonymous reset still proceeds.
    monkeypatch.setenv("TT_DEVICE_MCP_PRIVSEP", "1")
    (tmp_path / "0").write_text("")
    _fake_scoped_reset(monkeypatch, 0, "Resetting...\n")
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0))
    d = _client(monkeypatch, tmp_path, holders=[]).post("/api/tt_device_reset", json={"force": False}).json()
    assert d["status"] != "refused"


def test_non_privsep_http_reset_keeps_the_legacy_skip(monkeypatch, tmp_path):
    # Off privsep the HTTP path is the legacy single-tenant developer CLI; the gate stays skipped,
    # so this change must not start refusing resets on non-privsep hosts.
    monkeypatch.delenv("TT_DEVICE_MCP_PRIVSEP", raising=False)
    (tmp_path / "0").write_text("")
    holders = [DeviceHolder(pid=4242, uid=2000)]
    _fake_scoped_reset(monkeypatch, 0, "Resetting...\n")
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0))
    d = _client(monkeypatch, tmp_path, holders=holders).post("/api/tt_device_reset", json={"force": False}).json()
    assert d["status"] != "refused"


def test_privsep_streaming_reset_refuses_over_a_foreign_holder(monkeypatch, tmp_path):
    """The streaming HTTP reset shares the gate; over a foreign holder on a privsep host it must
    emit the '::status::refused' sentinel, not stream a board reset over the tenant's run."""
    monkeypatch.setenv("TT_DEVICE_MCP_PRIVSEP", "1")
    (tmp_path / "0").write_text("")
    holders = [DeviceHolder(pid=4242, uid=2000)]

    # Mock the reset body so a base run (gate skipped) stays hermetic instead of touching hardware.
    async def fake_exec(*argv, **kwargs):
        class _Out:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class P:
            returncode = 0
            stdout = _Out()

            async def communicate(self):
                return b"", None

            async def wait(self):
                return 0

        return P()

    async def pollers(active, log):
        return []

    async def no_sleep(_):
        return None

    async def fake_to_thread(fn, *a, **k):
        return None

    async def ok_verify(expected, log, **k):
        return True, {"snapshot": {"detail": "ok"}}

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    patch_recovery(monkeypatch, "_verify_device", ok_verify)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    srv.recovery_mechanism.reset_in_flight = False

    resp = _client(monkeypatch, tmp_path, holders=holders).post("/api/tt_device_reset_stream", json={"force": False})
    assert resp.status_code == 200
    assert "::status::refused" in resp.text, "the streaming reset skipped the gate for an HTTP caller"
    assert "gate REFUSED" in resp.text


# --- device-health verification (reset exit-0 is necessary but not sufficient) ---


def _snapshot(n, *, silent=()):
    """A tt-smi snapshot dict with ``n`` chips; indices in ``silent`` have a wedged
    ARC (no board_id / no board_type / empty telemetry)."""
    devs = []
    for i in range(n):
        if i in silent:
            devs.append({"board_info": {}, "telemetry": {}})
        else:
            devs.append(
                {
                    "board_info": {"board_id": f"0100{i:04d}", "board_type": "n300 L" if i % 2 == 0 else "n300 R"},
                    "telemetry": {"asic_temperature": 45},
                }
            )
    return {"device_info": devs}


def _fake_smi_health(snapshot=None, rc=0, raise_timeout=False):
    """Fake tt-smi for the snapshot path: emits ``snapshot`` JSON on STDOUT."""

    def run(argv, capture_output=True, text=True, timeout=None, **kwargs):
        assert argv[0] == "tt-smi" and argv[1] == "-s" and "--snapshot_no_tty" in argv
        if raise_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        import json

        out = json.dumps(snapshot) if snapshot is not None else ""
        return subprocess.CompletedProcess(argv, rc, out, "snapshot boom" if rc else "")

    return run


def test_verify_health_passes_when_all_chips_enumerate(monkeypatch):
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi_health(_snapshot(32)))
    ok, detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert ok and "all 32 chips" in detail


def test_verify_health_fails_on_short_chip_count(monkeypatch):
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi_health(_snapshot(24)))
    ok, detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert not ok and "expected 32" in detail


def test_verify_health_passes_on_over_count_from_stale_expected(monkeypatch):
    """A reset that recovers the full mesh from a stale, degraded high-water mark
    (expected baked at the survivor count) enumerates MORE chips than expected — a
    recovery, not a drop. Scoring it unhealthy would escalate a healthy box to reboot."""
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi_health(_snapshot(32)))
    ok, detail = srv.health_monitor.verify_device_health(31, timeout_sec=5)
    assert ok and "all 32 chips" in detail


def test_verify_health_fails_on_wedged_arc(monkeypatch):
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi_health(_snapshot(32, silent=(7,))))
    ok, detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert not ok and "[7]" in detail


def test_verify_health_fails_on_snapshot_error(monkeypatch):
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi_health(rc=1))
    ok, detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert not ok and "exited 1" in detail


def test_verify_health_fails_on_snapshot_timeout(monkeypatch):
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi_health(raise_timeout=True))
    ok, detail = srv.health_monitor.verify_device_health(32, timeout_sec=5)
    assert not ok and "timed out" in detail


def test_reset_tool_reports_health(monkeypatch, tmp_path):
    """A successful reset whose chips come back reports health_ok=True; a reset that
    exits 0 but leaves chips dropped is downgraded to status=reset_unhealthy."""
    (tmp_path / "0").write_text("")
    (tmp_path / "1").write_text("")

    def dispatch(healthy: bool):
        def run(argv, capture_output=True, text=True, timeout=None, **kwargs):
            if argv[:2] == ["tt-smi", "-s"]:
                import json

                return subprocess.CompletedProcess(argv, 0, json.dumps(_snapshot(2 if healthy else 1)), "")
            raise AssertionError(f"unexpected argv {argv}")

        return run

    _fake_scoped_reset(monkeypatch, 0, "Re-initialized boards\n")

    monkeypatch.setattr(srv.subprocess, "run", dispatch(healthy=True))
    d = _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False}).json()
    assert d["status"] == "reset_complete" and d["health_ok"] is True

    monkeypatch.setattr(srv.subprocess, "run", dispatch(healthy=False))
    d = _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False}).json()
    assert d["status"] == "reset_unhealthy" and d["health_ok"] is False


# --- an operator reset proves the fabric before it reopens the door (spec 04 I13) -------------
#
# Heartbeat + snapshot score a wedged eth core as fine, so on a mesh the operator's reset is
# verified the way the broker verifies its own: eth read and fabric pass, re-checked on a 77.


def _mesh_with_fabric_check(monkeypatch, tmp_path, chips=2):
    for i in range(chips):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv.fabric, "build_command", lambda: (["fabric-check"], {}))
    _fake_scoped_reset(monkeypatch, 0, "Re-initialized boards\n")


class _Calls(list):
    def __init__(self):
        super().__init__()
        self.expected = []


def _verify_seam(monkeypatch, fabric_ok):
    """Fake the probe pass: heartbeat + snapshot pass, the fabric (when asked for) returns
    ``fabric_ok``. Records the ``run_fabric`` each pass asked for, and on ``.expected`` the chip
    count it verified against."""
    calls = _Calls()

    async def verify(expected, log, *, run_fabric=True, phase="verify_device"):
        calls.append(run_fabric)
        calls.expected.append(expected)
        ev = {"snapshot": {"ok": True, "detail": f"all {expected} chips"}}
        if not run_fabric:
            return True, ev
        ev["fabric"] = {"ok": fabric_ok, "detail": f"fabric rc={ {True: 0, False: 1, None: 77}[fabric_ok]}"}
        return fabric_ok is not False, ev

    patch_recovery(monkeypatch, "_verify_device", verify)
    return calls


def _reset_tool(monkeypatch, tmp_path):
    return _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": False}).json()


def _reset_stream(monkeypatch, tmp_path):
    resp = _client(monkeypatch, tmp_path).post("/api/tt_device_reset_stream", json={"force": False})
    assert resp.status_code == 200
    return resp.text


def test_an_operator_reset_on_a_mesh_releases_only_on_a_fabric_pass(monkeypatch, tmp_path):
    _mesh_with_fabric_check(monkeypatch, tmp_path)
    calls = _verify_seam(monkeypatch, fabric_ok=True)
    fsm_dirty(srv, "job 7 wedged")
    monkeypatch.setattr(srv, "device_fault_reported", "job 7 waiting for active ethernet core")

    d = _reset_tool(monkeypatch, tmp_path)

    assert calls == [True], "the operator reset on a mesh did not run the fabric pass"
    assert d["status"] == "reset_complete" and d["health_ok"] is True
    assert srv.fsm.state is srv.ServerState.HEALTHY
    assert srv.device_fault_reported == "", "a reset proven by the fabric pass retires the reported fault"


def test_an_operator_reset_whose_fabric_fails_is_not_released(monkeypatch, tmp_path):
    _mesh_with_fabric_check(monkeypatch, tmp_path)
    _verify_seam(monkeypatch, fabric_ok=False)

    d = _reset_tool(monkeypatch, tmp_path)

    assert d["status"] == "reset_unhealthy" and d["health_ok"] is False
    assert srv.fsm.state is not srv.ServerState.HEALTHY, "a failed fabric pass reopened the door"
    assert srv.fsm.record.dirty, "the next gate must owe this mesh a real recovery"
    assert srv.fsm.record.why == "operator_reset_unhealthy", (
        f"got why={srv.fsm.record.why!r}: a failed operator-reset verify must say so, not pass as a "
        "read-only probe's finding"
    )


def test_the_reset_tools_documented_duration_matches_its_timeouts(monkeypatch):
    """The tool's docstring states the worst case an operator reset on a mesh can take. Recompute it
    from the timeouts the code actually uses, so a changed timeout cannot leave the doc stale."""
    import inspect
    import math
    import re

    from tt_device_mcp import constants
    from tt_device_mcp.health import monitor as monitor_mod
    from tt_device_mcp.health.monitors import heartbeat

    def default(fn, name):
        return inspect.signature(fn).parameters[name].default

    one_pass = math.ceil(
        heartbeat.HEARTBEAT_SETTLE_SEC
        + default(monitor_mod.HealthMonitor.verify_device_health, "timeout_sec")
        + default(monitor_mod.HealthMonitor.verify_eth_heartbeat, "timeout_sec")
        + constants.FABRIC_CHECK_TIMEOUT_SEC
    )
    # The shipped defaults, read from the source: conftest zeroes the live retry sleep.
    src = inspect.getsource(recovery_pkg)
    retries = int(re.search(r'"TT_DEVICE_MCP_POST_RESET_FABRIC_RETRIES", "(\d+)"', src).group(1))
    sleep = float(re.search(r'"TT_DEVICE_MCP_POST_RESET_FABRIC_SLEEP_SEC", "([\d.]+)"', src).group(1))
    rescan = 3  # reset_with_quiesce's settle after the PCI rescan
    worst = constants.DEVICE_RESET_TIMEOUT_SEC + rescan + (1 + retries) * one_pass + retries * sleep

    tools = asyncio.run(srv.create_mcp_server().list_tools())
    doc = next(t.description for t in tools if t.name == "tt_device_reset")
    assert f"about {round(worst / 60)} minutes" in doc, (worst, doc)
    assert f"reset {constants.DEVICE_RESET_TIMEOUT_SEC}s" in doc
    assert f"up to {one_pass}s each" in doc
    assert f"the {sleep:.0f}s wait" in doc


def test_an_operator_reset_whose_fabric_cannot_verify_holds_fabric_unverified(monkeypatch, tmp_path):
    _mesh_with_fabric_check(monkeypatch, tmp_path)
    calls = _verify_seam(monkeypatch, fabric_ok=None)
    monkeypatch.setattr(srv, "device_fault_reported", "job 7 waiting for active ethernet core")

    d = _reset_tool(monkeypatch, tmp_path)

    assert calls == [True] * (1 + recovery_pkg.POST_RESET_FABRIC_RETRIES), "a 77 was not re-checked"
    assert d["status"] == "reset_unverified" and d["health_ok"] is False
    assert srv.fsm.state is not srv.ServerState.HEALTHY
    assert srv.fsm.record.why == "fabric_unverified" and not srv.fsm.record.dirty
    assert srv.device_fault_reported, "a reset no pass proved retired the runtime's own fault report"


def test_a_single_chip_operator_reset_keeps_the_light_verify(monkeypatch, tmp_path):
    _mesh_with_fabric_check(monkeypatch, tmp_path, chips=1)
    calls = _verify_seam(monkeypatch, fabric_ok=False)
    fsm_dirty(srv, "job 7 wedged")

    monkeypatch.setattr(srv, "device_fault_reported", "job 7 hit a device timeout")

    d = _reset_tool(monkeypatch, tmp_path)

    assert calls == [False], "a single chip has no fabric to pass"
    assert d["status"] == "reset_complete" and d["health_ok"] is True
    assert srv.fsm.state is srv.ServerState.HEALTHY
    assert srv.device_fault_reported == "", "on a single chip the light verify is the whole proof"


def test_a_mesh_with_no_fabric_check_keeps_the_light_verify(monkeypatch, tmp_path):
    """Holding for a fabric verdict a host can never produce would strand it after every reset."""
    _mesh_with_fabric_check(monkeypatch, tmp_path)
    monkeypatch.setattr(srv.fabric, "build_command", lambda: None)
    calls = _verify_seam(monkeypatch, fabric_ok=None)

    d = _reset_tool(monkeypatch, tmp_path)

    assert calls == [False]
    assert d["status"] == "reset_complete"
    assert any("no fabric check installed" in s for s in d["steps"])


def test_the_light_verify_checks_the_hosts_chip_count_not_the_survivors(monkeypatch, tmp_path):
    """A mesh back two chips short must not verify 2-of-2 (spec 03 I11)."""
    _mesh_with_fabric_check(monkeypatch, tmp_path)
    monkeypatch.setattr(srv.fabric, "build_command", lambda: None)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "4")
    calls = _verify_seam(monkeypatch, fabric_ok=None)

    _reset_tool(monkeypatch, tmp_path)

    assert calls.expected == [4]


def test_a_blind_stream_verify_does_not_clear_the_reported_fault(monkeypatch, tmp_path):
    """The stream used to retire the runtime's fault report on heartbeat + snapshot alone."""
    _mesh_with_fabric_check(monkeypatch, tmp_path)
    _verify_seam(monkeypatch, fabric_ok=None)
    monkeypatch.setattr(srv, "device_fault_reported", "job 7 waiting for active ethernet core")

    text = _reset_stream(monkeypatch, tmp_path)

    assert "::status::reset_unverified" in text
    assert "fabric rc=77" in text, "the verify's progress must stream"
    assert srv.device_fault_reported, "a blind verify retired the runtime's own fault report"
    assert srv.fsm.record.why == "fabric_unverified"


@pytest.mark.parametrize("fabric_ok,status", [(True, "reset_complete"), (False, "reset_unhealthy")])
def test_the_stream_settles_an_operator_reset_like_the_tool(monkeypatch, tmp_path, fabric_ok, status):
    _mesh_with_fabric_check(monkeypatch, tmp_path)
    calls = _verify_seam(monkeypatch, fabric_ok=fabric_ok)
    monkeypatch.setattr(srv, "device_fault_reported", "job 7 waiting for active ethernet core")

    text = _reset_stream(monkeypatch, tmp_path)

    assert calls == [True]
    assert f"::status::{status}" in text
    assert (srv.fsm.state is srv.ServerState.HEALTHY) is fabric_ok
    assert (srv.device_fault_reported == "") is fabric_ok
    if not fabric_ok:
        assert srv.fsm.record.why == "operator_reset_unhealthy"


# --- the streaming reset is a reset like any other ----------------------------
#
# It ran as a bare child of the broker: outside the op lock, outside a scope. The dead-chip
# sampler reads a live reset as 32 chips leaving the bus and skips only when a reset scope
# says one is in flight — so an operator's own reset was diagnosed as a total blackout and
# the device was held against them. Observed: five all_chips_blackout events and a
# device_held, mid-reset, on a box that was fine.

RESET_CMD = ["tt-smi", "-" + "glx_reset"]


def test_a_reset_scope_argv_is_scoped_and_outlives_us():
    unit, scoped = srv.recovery_mechanism._reset_scope_argv(list(RESET_CMD))
    assert unit.startswith(recovery_base.RESET_SCOPE_PREFIX), "the sampler recognises a reset by this name"
    assert scoped[:2] == ["systemd-run", "--scope"]
    assert f"--unit={unit}" in scoped
    assert "--property=KillMode=mixed" in scoped, "a restart would stop it mid-32-ASICs"
    assert scoped[-2:] == RESET_CMD
    unit2, _ = srv.recovery_mechanism._reset_scope_argv(list(RESET_CMD))
    assert unit != unit2, "each reset needs its own unit"


# Captured before conftest's autouse fixture pins the guard to False: this is the one test
# that means to exercise the real thing.
_REAL_RESET_SCOPE_ACTIVE = srv.recovery_mechanism.scope_active


def test_the_sampler_recognises_a_streaming_resets_scope(monkeypatch):
    """The whole point: the unit the streaming path creates is one _reset_scope_active sees."""
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", _REAL_RESET_SCOPE_ACTIVE)
    unit, _ = srv.recovery_mechanism._reset_scope_argv(list(RESET_CMD))
    seen = {}

    class R:
        stdout = unit + " loaded active running /usr/bin/tt-smi\n"

    def fake_run(argv, **kw):
        seen["pattern"] = argv[-1]
        return R()

    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    assert srv.recovery_mechanism.scope_active() == unit, "the sampler cannot see the operator's reset"
    assert seen["pattern"].startswith(recovery_base.RESET_SCOPE_PREFIX)


def _local_reset_mechanism(tmp_path):
    async def pollers(_active, _log):
        return []

    return recovery_base.RecoveryMechanism(
        current_boot_id=lambda: "boot",
        boot_btime_id=lambda: "0",
        scoped_reset_backend=lambda: False,
        set_device_pollers=pollers,
        set_device_op_detail=lambda _detail: None,
        begin_action_row=lambda *_a: None,
        write_action_log=lambda *_a: None,
        device_hold_episode_since=lambda: "",
        local_reset_dir=lambda: tmp_path,
    )


@pytest.mark.asyncio
async def test_a_reset_without_systemd_runs_detached_and_returns_its_output(tmp_path):
    """Removing the systemd-only gate must execute the reset, not turn a missing scope into a
    successful no-op."""
    mechanism = _local_reset_mechanism(tmp_path)
    rc, output = await mechanism.run_scoped(
        [sys.executable, "-c", "print('local reset complete')"],
        lambda _message: None,
    )
    assert rc == 0
    assert output == "local reset complete"
    assert mechanism.scope_active() is None


@pytest.mark.asyncio
async def test_a_restarted_daemon_adopts_the_local_reset_lock(tmp_path):
    """A second daemon must see the kernel-owned lock from the first daemon's child, or its startup
    probe can race the reset and launch another one."""
    release = tmp_path / "release"
    command = [
        sys.executable,
        "-c",
        f"import pathlib,time; p=pathlib.Path({str(release)!r});\nwhile not p.exists(): time.sleep(.01)",
    ]
    first = _local_reset_mechanism(tmp_path)
    restarted = _local_reset_mechanism(tmp_path)
    task = asyncio.create_task(first.run_scoped(command, lambda _message: None))
    try:
        for _ in range(100):
            if restarted.scope_active() == recovery_base.LOCAL_RESET_NAME:
                break
            await asyncio.sleep(0.01)
        assert restarted.scope_active() == recovery_base.LOCAL_RESET_NAME
    finally:
        release.write_text("")
        await task
    assert restarted.scope_active() is None


@pytest.mark.asyncio
async def test_cancelling_the_waiter_does_not_kill_a_local_reset(tmp_path):
    """The reset child, not the daemon task, owns the lock; cancelling the waiter must not expose
    the device to a concurrent reset while the first reset still runs."""
    release = tmp_path / "release"
    command = [
        sys.executable,
        "-c",
        f"import pathlib,time; p=pathlib.Path({str(release)!r});\nwhile not p.exists(): time.sleep(.01)",
    ]
    mechanism = _local_reset_mechanism(tmp_path)
    observer = _local_reset_mechanism(tmp_path)
    task = asyncio.create_task(mechanism.run_scoped(command, lambda _message: None))
    try:
        for _ in range(100):
            if observer.scope_active() == recovery_base.LOCAL_RESET_NAME:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert observer.scope_active() == recovery_base.LOCAL_RESET_NAME
    finally:
        release.write_text("")
    for _ in range(100):
        if observer.scope_active() is None:
            break
        await asyncio.sleep(0.01)
    assert observer.scope_active() is None


@pytest.mark.asyncio
async def test_a_daemon_without_systemd_resets_through_the_local_backend(tmp_path):
    """A container needs no declared authority to reset: `tt-smi -r` is an ioctl on a device node
    its submitter already holds (spec 04 I17), so lacking a PID-1 scope picks the other backend
    rather than cancelling the reset. The tenant rules (I6/I7) still gate it."""
    mechanism = _local_reset_mechanism(tmp_path)
    rc, _output = await mechanism.run_scoped(
        [sys.executable, "-c", "raise SystemExit(0)"],
        lambda _message: None,
    )
    assert rc == 0


def test_a_streaming_local_reset_is_visible_to_a_restarted_daemon(monkeypatch, tmp_path):
    """The operator reset route must use the same cross-process lock as automatic recovery; an
    untracked direct child lets a restarted daemon probe and reset the device concurrently."""
    (tmp_path / "0").touch()
    release = tmp_path / "release"
    reset_script = tmp_path / "reset"
    reset_script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, time\n"
        f"release = pathlib.Path({str(release)!r})\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)\n"
        "print('local operator reset complete')\n"
    )
    reset_script.chmod(0o755)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_ARGS", str(reset_script))
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", str(tmp_path / "device-op.lock"))
    monkeypatch.setattr(srv, "_scoped_reset_backend", lambda: False)
    monkeypatch.setattr(srv.recovery_mechanism, "_local_reset_dir", lambda: tmp_path)

    async def no_pollers(_active, _log):
        return []

    async def healthy_after_reset(*_args, **_kwargs):
        return True, {"snapshot": {"detail": "ok"}}

    monkeypatch.setattr(srv, "_set_device_pollers", no_pollers)
    monkeypatch.setattr(srv.fsm, "observe", healthy_after_reset)
    response = {}
    client = _client(monkeypatch, tmp_path)
    request = threading.Thread(
        target=lambda: response.setdefault("value", client.post("/api/tt_device_reset_stream", json={"force": False}))
    )
    request.start()
    observer = _local_reset_mechanism(tmp_path)
    try:
        for _ in range(200):
            if observer.scope_active() == recovery_base.LOCAL_RESET_NAME:
                break
            time.sleep(0.01)
        assert observer.scope_active() == recovery_base.LOCAL_RESET_NAME
    finally:
        release.write_text("")
        request.join(timeout=5)
    assert not request.is_alive()
    assert "local operator reset complete" in response["value"].text


# --- a slow reset is not a failed reset ---------------------------------------
#
# The scope is deliberately never killed, so our timer expiring says only that we stopped
# watching. Calling that a failure armed the failed-reset cooldown — suppressing the NEXT
# reset — and held the device over a reset that had worked. Seen three times in one evening:
# reset_timeout at exactly 180.0s, then 32/32 chips back with their counters restarted.


@pytest.mark.asyncio
async def test_a_reset_that_overruns_is_waited_out_not_failed(monkeypatch):
    events = []
    monkeypatch.setattr(recovery_base, "health_event", lambda k, **kw: events.append(k))
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_OVERRUN_SEC", 0.05)
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_TIMEOUT_SEC", 10)

    class SlowProc:
        returncode = 0

        def __init__(self):
            self.calls = 0

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.3)  # blows the overrun line ...
            return (b"Re-initialized 32 boards after reset. Exiting...", None)

    async def fake_exec(*a, **k):
        return SlowProc()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)

    rc, out = await srv.recovery_mechanism.run_scoped(list(RESET_CMD), lambda m: None)

    assert rc == 0, "a reset that merely ran long was reported as a failure"
    assert "reset_overran" in events, "the overrun went unrecorded"
    assert "reset_timeout" not in events, "a slow reset was called a timeout"


@pytest.mark.asyncio
async def test_a_reset_that_never_ends_is_still_a_failure(monkeypatch):
    events = []
    monkeypatch.setattr(recovery_base, "health_event", lambda k, **kw: events.append(k))
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_OVERRUN_SEC", 0.05)
    monkeypatch.setattr(recovery_base, "DEVICE_RESET_TIMEOUT_SEC", 0.2)

    class DeadProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(30)

    async def fake_exec(*a, **k):
        return DeadProc()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)

    rc, out = await srv.recovery_mechanism.run_scoped(list(RESET_CMD), lambda m: None)

    assert rc is None, "a stuck reset must still fail"
    assert "reset_timeout" in events


# --- a reset must not wedge the mesh on its way in ----------------------------
#
# Both reset paths hard-SIGKILLed the running job to take the device. A hard kill mid-CCL is
# the classic way the mesh gets wedged — it is why every other path SIGTERMs first — so the
# reset was partly a cure for damage the reset itself did.


@pytest.mark.asyncio
async def test_a_reset_stops_the_running_job_gracefully(monkeypatch):
    """SIGINT, grace, and only then SIGKILL — the job gets its chance to release the chip."""
    signals = []

    def fake_killpg(pid, sig):
        signals.append(sig)
        if sig == 0:  # the liveness probe
            raise ProcessLookupError  # gone after the graceful signal

    monkeypatch.setattr(srv.os, "killpg", fake_killpg)

    await srv._terminate_process_group(4242, grace_sec=0.2)

    assert (
        signals[0] == __import__("signal").SIGINT
    ), "the job was hard-killed mid-CCL, which is how the mesh gets wedged"
    assert __import__("signal").SIGKILL not in signals, "escalated despite the job exiting"


@pytest.mark.asyncio
async def test_a_job_that_ignores_the_graceful_signals_is_still_killed(monkeypatch):
    """The grace is a chance, not a veto: a truly hung job is still reaped."""
    signals = []
    monkeypatch.setattr(srv.os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(srv, "SIGTERM_GRACE_SEC", 0.1)  # keep the SIGTERM rung fast under test

    await srv._terminate_process_group(4242, grace_sec=0.1)

    import signal as _s

    # A Python job only unwinds (and releases the mesh) on SIGINT, so the ladder leads with it.
    assert signals[0] == _s.SIGINT
    assert _s.SIGKILL in signals, "a job that ignored SIGINT/SIGTERM would hold the device forever"


@pytest.mark.asyncio
async def test_terminate_job_signals_the_scope_for_a_privsep_job(monkeypatch):
    """A privsep job runs in its own systemd scope, and the pid the broker holds is the
    systemd-run wrapper, not the reparented payload. killpg on that pid leaves the real job
    running mid-CCL/fabric and wedges the eth (a timed-out job reaped by killpg exited code
    None on a dead fabric). _terminate_job must signal the SCOPE, never the process group."""
    scope_kills = []
    pgroup_kills = []

    async def fake_scope(scope, grace_sec=0.0):
        scope_kills.append((scope, grace_sec))

    async def fake_pgroup(pid, grace_sec=0.0):
        pgroup_kills.append(pid)

    monkeypatch.setattr(srv, "_scope_active", lambda scope: True)
    monkeypatch.setattr(srv, "_terminate_scope", fake_scope)
    monkeypatch.setattr(srv, "_terminate_process_group", fake_pgroup)

    await srv._terminate_job("77", 4242, grace_sec=1.5)

    assert scope_kills == [(srv.job_scope_unit("77"), 1.5)]
    assert not pgroup_kills, "a scoped job must NOT be reaped by killpg — that is the eth wedge"


@pytest.mark.asyncio
async def test_terminate_job_falls_back_to_killpg_without_a_scope(monkeypatch):
    """A non-privsep job has no scope; there is nothing for systemd to signal, so terminate
    its process group instead."""
    scope_kills = []
    pgroup_kills = []

    async def fake_scope(scope, grace_sec=0.0):
        scope_kills.append(scope)

    async def fake_pgroup(pid, grace_sec=0.0):
        pgroup_kills.append((pid, grace_sec))

    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)
    monkeypatch.setattr(srv, "_terminate_scope", fake_scope)
    monkeypatch.setattr(srv, "_terminate_process_group", fake_pgroup)

    await srv._terminate_job("77", 4242, grace_sec=1.5)

    assert pgroup_kills == [(4242, 1.5)]
    assert not scope_kills


# --- every kill path must scope-route a LIVE privsep job -----------------------
#
# _terminate_job (above) routes correctly, but the callers bypassed it: _kill_job
# scope-routed only RE-ADOPTED jobs and both reset-quiesce paths killpg'd the
# broker-held pid outright. For a live privsep job that pid is the systemd-run
# wrapper, so the payload ran on mid-CCL and wedged the eth -- the gap that let the
# fleet incident through.


def _record_terminators(monkeypatch):
    scope_kills, pgroup_kills = [], []

    async def fake_scope(scope, grace_sec=0.0):
        scope_kills.append(scope)

    async def fake_pgroup(pid, grace_sec=0.0):
        pgroup_kills.append(pid)

    monkeypatch.setattr(srv, "_scope_active", lambda scope: True)  # a live scope
    monkeypatch.setattr(srv, "_terminate_scope", fake_scope)
    monkeypatch.setattr(srv, "_terminate_process_group", fake_pgroup)
    return scope_kills, pgroup_kills


class _FakeWrapperProc:
    """Stands in for current_process: the systemd-run wrapper's pid, never the payload."""

    pid = 4242

    async def wait(self):
        return 0


def test_a_live_privsep_kill_signals_the_scope_not_the_pgroup(monkeypatch):
    scope_kills, pgroup_kills = _record_terminators(monkeypatch)
    monkeypatch.setattr(srv, "readopted_scopes", {})  # NOT re-adopted: a job this broker spawned

    job = srv.Job(
        id="900-1", owner="bjones", workspace="/w", command="pytest", queued_at="", status=srv.JobStatus.RUNNING
    )
    job.pid = 4242
    monkeypatch.setattr(srv, "jobs", {"900-1": job})

    d = (
        TestClient(srv.build_asgi_app(srv.create_mcp_server()))
        .post("/api/tt_device_job_kill", json={"job_id": "900-1", "owner": "bjones"})
        .json()
    )

    assert d["status"] == "killed"
    assert scope_kills == [srv.job_scope_unit("900-1")]
    assert not pgroup_kills, "a live privsep job must be reaped by its scope, not killpg on the wrapper"


def test_reset_device_quiesce_scope_routes_a_live_privsep_job(monkeypatch, tmp_path):
    (tmp_path / "0").write_text("")
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0))
    _fake_scoped_reset(monkeypatch, 0, "ok")
    scope_kills, pgroup_kills = _record_terminators(monkeypatch)
    monkeypatch.setattr(srv, "current_process", _FakeWrapperProc())
    monkeypatch.setattr(srv, "current_job_id", "901-1")

    _client(monkeypatch, tmp_path).post("/api/tt_device_reset", json={"force": True}).json()

    assert scope_kills == [srv.job_scope_unit("901-1")]
    assert not pgroup_kills, "the reset quiesce must scope-route a privsep job, not killpg the wrapper"


def test_reset_stream_quiesce_scope_routes_a_live_privsep_job(monkeypatch, tmp_path):
    (tmp_path / "0").write_text("")
    scope_kills, pgroup_kills = _record_terminators(monkeypatch)
    monkeypatch.setattr(srv, "current_process", _FakeWrapperProc())
    monkeypatch.setattr(srv, "current_job_id", "902-1")

    async def fake_exec(*argv, **kwargs):
        class _Out:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class P:
            returncode = 0
            stdout = _Out()

            async def communicate(self):
                return b"", None

            async def wait(self):
                return 0

        return P()

    async def pollers(active, log):
        return []

    async def ok_verify(expected, log, **k):
        return True, {"snapshot": {"detail": "ok"}}

    async def fake_to_thread(fn, *a, **k):
        # Must run fn: _terminate_job checks _scope_active through to_thread. The only
        # other to_thread here is the /sys pci rescan, which fails closed under OSError.
        return fn(*a, **k)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(srv.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    patch_recovery(monkeypatch, "_verify_device", ok_verify)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: None)
    srv.recovery_mechanism.reset_in_flight = False

    resp = _client(monkeypatch, tmp_path).post("/api/tt_device_reset_stream", json={"force": True})
    assert resp.status_code == 200

    assert scope_kills == [srv.job_scope_unit("902-1")]
    assert not pgroup_kills, "the streaming reset quiesce must scope-route a privsep job"


@pytest.mark.asyncio
async def test_reset_quiesce_restores_pollers_even_if_the_rescan_is_cancelled(monkeypatch):
    """A client disconnect cancels the streaming reset and re-delivers CancelledError at every
    await, including the post-reset rescan. CancelledError is a BaseException the OSError guard
    misses, so without an always-run restore the MMIO pollers are left OFF — a silent telemetry gap
    until the next reset. The restore must run through the cancellation."""
    import asyncio as _aio

    calls = []

    async def pollers(active, log):
        calls.append(active)
        return ["telem"] if not active else []

    async def run(argv, log, owner="[broker]health-gate"):
        return 0, "ok"

    async def cancel(*a, **k):
        raise _aio.CancelledError()

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", run)
    monkeypatch.setattr(srv.asyncio, "to_thread", cancel)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)

    with pytest.raises(_aio.CancelledError):
        await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r", "0"], lambda m: None)

    assert calls and calls[0] is False, "pollers must be quiesced for the reset"
    assert True in calls, "the poller restore must run even when the rescan await is cancelled"


@pytest.mark.asyncio
async def test_reset_quiesce_leaves_pollers_off_when_cancelled_mid_reset(monkeypatch):
    """Cancelled MID-reset (client disconnect ~10s into a ~60s reset): the detached KillMode=mixed
    scope keeps cycling the 32 chips off the bus. Restarting the MMIO pollers now reads dead
    endpoints for the rest of the reset and RAS-reboots the whole host — the reboot the quiesce
    exists to prevent. So on a cancel BEFORE the reset completes, the restore must be SKIPPED: the
    pollers stay quiesced (a benign telemetry gap), never restarted against off-bus chips."""
    import asyncio as _aio

    calls = []

    async def pollers(active, log):
        calls.append(active)
        return ["telem"] if not active else []

    async def cancelled_mid_reset(argv, log, owner="[broker]health-gate"):
        raise _aio.CancelledError()  # the reset never returns — abandoned mid-flight

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", cancelled_mid_reset)

    with pytest.raises(_aio.CancelledError):
        await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r", "0"], lambda m: None)

    assert calls == [False], f"pollers must stay quiesced when cancelled mid-reset, got {calls}"


@pytest.mark.asyncio
async def test_reset_quiesce_clears_in_flight_when_cancelled_mid_reset(monkeypatch):
    """A mid-reset cancel must still CLEAR reset_in_flight, even though the poller restore is skipped.
    The flag gates the dead-chip sampler (it returns early while set). The detached scope finishes on
    its own after the cancel, leaving a healthy box — but a stuck-True flag then blinds the sampler to
    a real chip drop until the next completed reset, so a wedged chip never gets isolated and its
    holder stalls a core into a host reboot. Only the poller RESTORE may be skipped on a cancel; the
    flag must clear, because _reset_scope_active() still defers isolation while the scope cycles."""
    import asyncio as _aio

    async def pollers(active, log):
        return ["telem"] if not active else []

    async def cancelled_mid_reset(argv, log, owner="[broker]health-gate"):
        raise _aio.CancelledError()  # the reset never returns — abandoned mid-flight

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", cancelled_mid_reset)
    srv.recovery_mechanism.reset_in_flight = False

    with pytest.raises(_aio.CancelledError):
        await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r", "0"], lambda m: None)

    assert srv.recovery_mechanism.reset_in_flight is False, "reset_in_flight must be cleared even on a mid-reset cancel"


@pytest.mark.asyncio
async def test_reset_quiesce_leaves_pollers_off_when_the_reset_times_out(monkeypatch):
    """A reset that TIMES OUT returns (None, ...) as a NORMAL return — _run_reset_scoped never kills
    the scope, so it keeps cycling the 32 chips off the bus while the caller sees a plain return and
    cancelled_mid stays False. Restoring the MMIO pollers into that reads dead endpoints and
    RAS-reboots the host, exactly like a mid-reset cancel. So the restore must be gated on the reset
    scope being gone, not only on the cancel flag: with a scope still active, the pollers stay off."""
    calls = []

    async def pollers(active, log):
        calls.append(active)
        return ["telem"] if not active else []

    async def timed_out(argv, log, owner="[broker]health-gate"):
        return None, "reset never finished after 300s"  # normal return, scope left cycling

    async def to_thread(fn, *a, **k):
        # Route the fix's scope probe to the (mocked) checker; swallow the /sys/bus/pci/rescan write
        # so a base re-run of this fails-on-base test never rescans the real PCI bus.
        return fn(*a, **k) if fn is srv.recovery_mechanism.scope_active else None

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", timed_out)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: "tt-reset-abcd.scope")  # still cycling
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)

    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r", "0"], lambda m: None)

    assert rc is None
    assert calls == [False], f"pollers must stay quiesced while the timed-out reset scope cycles, got {calls}"


@pytest.mark.asyncio
async def test_reset_quiesce_restores_pollers_when_a_failed_launch_leaves_no_scope(monkeypatch):
    """rc None covers a timeout (scope cycling — skip restore) AND a launch failure (no scope ever
    started). A launch failure must still restore the pollers, or a reset that never ran leaves the
    box with telemetry off. With no reset scope active, the restore runs even though rc is None —
    proving the timeout gate is scoped to a genuinely-cycling reset, not to rc None alone."""
    calls = []

    async def pollers(active, log):
        calls.append(active)
        return ["telem"] if not active else []

    async def failed_launch(argv, log, owner="[broker]health-gate"):
        return None, "reset could not be launched: [Errno 2] No such file or directory"

    async def to_thread(fn, *a, **k):
        # Route the fix's scope probe to the (mocked) checker; swallow the /sys/bus/pci/rescan write
        # so a unit test never rescans the real PCI bus on a shared galaxy.
        return fn(*a, **k) if fn is srv.recovery_mechanism.scope_active else None

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    monkeypatch.setattr(srv.recovery_mechanism, "run_scoped", failed_launch)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)  # nothing ever started
    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)

    rc, _ = await srv.recovery_mechanism.reset_with_quiesce(["tt-smi", "-r", "0"], lambda m: None)

    assert rc is None
    assert True in calls, "a launch failure starts no scope, so the pollers must be restored"


# --- the machine type: taken from the health snapshot, never probed for ------------------------


def _glx(monkeypatch):
    monkeypatch.setattr(srv.health_monitor, "_glx_board_types", lambda: ("tt-galaxy-wh", "tt-galaxy-bh"))


def test_the_machine_type_never_shells_out(monkeypatch, health_deps):
    """reset_argv runs on the event loop from three coroutines, and a tt-smi call is a full UMD
    device init. One blocking here starves the watchdog ping, systemd kills the broker as
    unresponsive, and KillMode=control-group takes the in-flight tt-smi with it — a reset stopped
    partway through 32 ASICs. So the verdict comes from what a health snapshot already cached."""
    _glx(monkeypatch)
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["n300 L", "n300 R"])
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: pytest.fail(f"machine-type derivation shelled out: {a}"))
    assert _is_galaxy(health_deps) is False
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0", "1"]) == ["tt-smi", "-r", "0,1"]
    assert reset_mode_known(health_deps) is True


def test_the_health_snapshot_fills_the_cache(monkeypatch):
    """The gate's own tt-smi read is the only one the broker makes; the board types ride along on
    it so the reset argv needs no second device init — least of all at a device just declared
    unhealthy."""
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.subprocess, "run", _fake_tt_smi(0, snapshot_chips=2))
    srv.health_monitor.verify_device_health(2)
    assert srv.health_monitor._board_types, "verify_device_health must record the board types it already parsed"


def test_a_wedged_chip_leaves_the_cache_open(monkeypatch):
    """A chip whose ARC is not answering reports no board type at all, and that snapshot is the one
    the gate takes just before a reset. It must not become this process's answer for the machine
    type — the snapshot after the reset is the one that knows."""
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.subprocess, "run", _fake_smi_health(_snapshot(2, silent=(1,))))
    srv.health_monitor.verify_device_health(2)
    assert srv.health_monitor._board_types is None, "a snapshot with a silent chip identified nothing"


def test_an_unidentifiable_board_is_unknown_not_non_galaxy(monkeypatch, health_deps):
    """tt-smi prints "N/A" whenever a board-id or ARC read fails — i.e. exactly on a degraded
    Galaxy. Reading that as "not a Galaxy" hands it the reset that cannot recover it, and marks the
    mode known so the loud guard never fires. There is no backstop: -glx_reset never consults
    tt-smi's own Galaxy check, so a wrong verdict resets trays on hardware that cannot take one."""
    _glx(monkeypatch)
    for types in (["N/A"], ["N/A", "tt-galaxy-wh"], ["tt-galaxy-wh", "N/A"], ["n300 L", "N/A"]):
        monkeypatch.setattr(srv.health_monitor, "_board_types", types)
        assert _is_galaxy(health_deps) is None, types
        assert reset_mode_known(health_deps) is False, types


def test_a_degraded_snapshot_does_not_pin_the_machine_type_to_unknown(monkeypatch, health_deps):
    """The gate's first snapshot can land while an ARC read is failing, which is when tt-smi prints
    "N/A" for every board. Caching that would hold the machine type unknown for the life of the
    process — including after a reset restores the boards — leaving a Galaxy on the loud guard and,
    once an operator declares per-target to get past it, on the reset that cannot recover it."""
    _glx(monkeypatch)
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)

    srv.health_monitor._bank_board_types_once(["N/A", "N/A"])
    assert srv.health_monitor._board_types is None, "an unidentified snapshot is not a read of the machine type"
    srv.health_monitor._bank_board_types_once(["tt-galaxy-wh", "N/A"])
    assert srv.health_monitor._board_types is None, "one unidentified board is enough to prove nothing"

    srv.health_monitor._bank_board_types_once(["tt-galaxy-wh L", "tt-galaxy-wh R"])
    assert _is_galaxy(health_deps) is True, "the snapshot after a recovery must still get to fill the cache"


def test_an_identified_snapshot_is_not_overwritten(monkeypatch, health_deps):
    """The boards cannot change under a running broker, so a later short read is a worse read."""
    _glx(monkeypatch)
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    srv.health_monitor._bank_board_types_once(["tt-galaxy-wh"])
    for later in ([], ["n300 L"], ["N/A"]):
        srv.health_monitor._bank_board_types_once(later)
        assert _is_galaxy(health_deps) is True, later


# --- _bank_bus_ids_once guard battery (I16: no map, no fire) -----------------------------------
#
# The bus-id cache is load-bearing: the tray rung reads it positionally, so a wrong or short list
# names the wrong tray. The invariants live in _bank_bus_ids_once itself; these pin each guard so
# a refactor that drops one cannot ship silently.


def test_bank_bus_ids_once_rejects_a_snapshot_shorter_than_the_mesh(monkeypatch):
    """A partial read positionally attributes one chip's bus to another's id. Refuse rather than
    freeze it — a later full snapshot has to get to fill the cache."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    srv.health_monitor._bank_bus_ids_once(["0000:01:00.0"] * 24, expected_count=32)
    assert srv.health_monitor._bus_ids is None


def test_bank_bus_ids_once_rejects_a_boot_probe_that_did_not_ask_the_question(monkeypatch):
    """expected_count == 0 is the boot platform probe (identify boards without counting chips).
    A snapshot from that call must never fill the map, since a degraded mesh at boot returns a
    short list keyed positionally by enumerate()."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    srv.health_monitor._bank_bus_ids_once(["0000:01:00.0"] * 32, expected_count=0)
    assert srv.health_monitor._bus_ids is None


def test_bank_bus_ids_once_rejects_a_chip_enumerated_without_a_bus_id(monkeypatch):
    """A snapshot the caller has already length-checked can still carry an empty string for a chip
    whose bus id tt-smi did not read. Positional caching would silently drop that chip from its
    tray's reset."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    holes = [f"0000:{i:02x}:00.0" for i in range(1, 33)]
    holes[10] = ""
    srv.health_monitor._bank_bus_ids_once(holes, expected_count=32)
    assert srv.health_monitor._bus_ids is None


def test_bank_bus_ids_once_rejects_an_empty_list(monkeypatch):
    """An empty snapshot is not a read of the bus map. The cache stays open for the next call."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    srv.health_monitor._bank_bus_ids_once([], expected_count=0)
    srv.health_monitor._bank_bus_ids_once([], expected_count=32)
    assert srv.health_monitor._bus_ids is None


def test_bank_bus_ids_once_first_full_read_stands(monkeypatch):
    """PCI topology does not move under a running broker (spec 04 I16), so once a full snapshot
    has filled the map a later — possibly short — read must not overwrite it. The method's name
    encodes this: it banks once, not on every call."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    good = [f"0000:{i:02x}:00.0" for i in range(1, 33)]
    srv.health_monitor._bank_bus_ids_once(good, expected_count=32)
    assert srv.health_monitor._bus_ids == good
    later = [f"0000:{i + 0x40:02x}:00.0" for i in range(1, 33)]
    srv.health_monitor._bank_bus_ids_once(later, expected_count=32)
    assert srv.health_monitor._bus_ids == good


def test_bank_bus_ids_once_stays_open_after_a_rejected_snapshot(monkeypatch):
    """A rejected snapshot must not pin the cache to "unknown". The next full read gets to fill
    it — the same rule _bank_board_types_once follows for its unidentified branch."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", None, raising=False)
    srv.health_monitor._bank_bus_ids_once(["0000:01:00.0"] * 24, expected_count=32)
    assert srv.health_monitor._bus_ids is None
    good = [f"0000:{i:02x}:00.0" for i in range(1, 33)]
    srv.health_monitor._bank_bus_ids_once(good, expected_count=32)
    assert srv.health_monitor._bus_ids == good


def test_a_padded_board_type_is_still_matched(monkeypatch, health_deps):
    """The suffix comes off the trimmed value: strip it first and "tt-galaxy-wh L " keeps its " L",
    matches no Galaxy name, and yields a confident False — a per-target reset on a 6U."""
    _glx(monkeypatch)
    for types in (["tt-galaxy-wh L "], [" tt-galaxy-wh"], ["tt-galaxy-wh R\n"]):
        monkeypatch.setattr(srv.health_monitor, "_board_types", types)
        assert _is_galaxy(health_deps) is True, types
    monkeypatch.setattr(srv.health_monitor, "_board_types", [" N/A "])
    assert _is_galaxy(health_deps) is None, "padding must not hide an unidentified board either"


def test_boards_must_agree(monkeypatch, health_deps):
    """A mixed snapshot proves nothing about which reset fits, so it is unknown rather than a
    majority guess — tt-smi's own -r path likewise requires every targeted chip to be Galaxy."""
    _glx(monkeypatch)
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["tt-galaxy-wh", "n300 L"])
    assert _is_galaxy(health_deps) is None


def test_a_galaxy_is_recognised_through_the_snapshot_suffix(monkeypatch, health_deps):
    """tt-smi appends " L"/" R" to its log copy only and compares the unsuffixed value."""
    _glx(monkeypatch)
    for types in (["tt-galaxy-wh"], ["tt-galaxy-wh L"], ["tt-galaxy-wh L", "tt-galaxy-wh R"]):
        monkeypatch.setattr(srv.health_monitor, "_board_types", types)
        assert _is_galaxy(health_deps) is True, types


def test_the_galaxy_list_comes_from_tt_smi_not_from_here():
    """No board name is duplicated in the broker. tt-smi is a hard dependency, so this is not
    conditional on the environment."""
    from tt_smi.constants import GLX_BOARD_TYPES

    assert srv.health_monitor._glx_board_types() == tuple(GLX_BOARD_TYPES)
    assert GLX_BOARD_TYPES, "tt-smi must name at least one Galaxy board type"


def test_reset_argv_is_derived_with_nothing_declared(monkeypatch, health_deps):
    """Requiring the operator to declare this is what took multi-chip hosts out of service."""
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    _glx(monkeypatch)

    monkeypatch.setattr(srv.health_monitor, "_board_types", ["tt-galaxy-wh"])
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0", "1"]) == ["tt-smi", "-glx_reset"]

    monkeypatch.setattr(srv.health_monitor, "_board_types", ["n300 L", "n300 R"])
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0", "1"]) == ["tt-smi", "-r", "0,1"]


def test_a_reset_mode_naming_no_known_mode_does_not_override_the_derivation(monkeypatch, health_deps):
    """`galaxy_6u`, a typo, or a stale value from when declaring it was mandatory used to read as
    per-target — silently overriding a correct derivation and marking the mode known."""
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy_6u")
    _glx(monkeypatch)
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["tt-galaxy-wh"])
    assert _declared_reset_mode(health_deps.journal_skip_once) is None, "an unrecognised value is not a declaration"
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0"]) == [
        "tt-smi",
        "-glx_reset",
    ], "derivation must still win"


def test_a_declared_mode_still_wins_over_the_derivation(monkeypatch, health_deps):
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", srv.RESET_MODE_TARGET)
    _glx(monkeypatch)
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["tt-galaxy-wh"])
    assert select_recovery(None, srv.recovery_mechanism, health_deps).reset_argv(["0", "1"]) == ["tt-smi", "-r", "0,1"]


class TestCpldTooOldBanner:
    """tt-smi's own warning that `-r` is the wrong reset for this host (spec 04 I6 note).

    On a Galaxy whose CPLD firmware predates v1.16, `tt-smi -r` does not merely fail: the chips
    re-enumerate and every register read then returns 0xffffffff until a `-glx_reset` runs.
    tt-smi says so on stdout before it tries, and that output already reached the broker and was
    being discarded — the one warning the host gives about a reset that strands it went nowhere.
    """

    REAL = "Warning: CPLD FW v1.16 or higher is required to use tt-smi -r on this system\n"

    def test_the_banner_is_logged_and_journalled(self, monkeypatch):
        events = []
        lines = []
        monkeypatch.setattr(recovery_pkg, "health_event", lambda kind, **f: events.append((kind, f)))

        assert recovery_pkg._journal_cpld_too_old(["tt-smi", "-r", "0"], self.REAL, lines.append) is True

        assert [k for k, _ in events] == ["reset_cpld_too_old"]
        assert events[0][1]["host_at_risk"] is True
        assert events[0][1]["argv"] == ["tt-smi", "-r", "0"]
        assert lines and "glx_reset" in lines[0], lines
        assert "CPLD" in lines[0]

    def test_ordinary_reset_output_is_silent(self, monkeypatch):
        """No event on a normal reset: an operator watching for this must not learn to ignore it."""
        events = []
        monkeypatch.setattr(recovery_pkg, "health_event", lambda kind, **f: events.append((kind, f)))

        assert recovery_pkg._journal_cpld_too_old(["tt-smi", "-r"], "Resetting UMD logical IDs: [0]\n", print) is False
        assert recovery_pkg._journal_cpld_too_old(["tt-smi", "-r"], "", print) is False
        assert recovery_pkg._journal_cpld_too_old(["tt-smi", "-r"], None, print) is False
        assert events == []

    def test_a_reworded_or_wrapped_banner_still_trips(self):
        """Matched on the version and the flag, not the whole sentence.

        A missed match silently restores the old behaviour of discarding the warning, so the
        pattern is deliberately looser than the exact string tt-smi happens to print today.
        """
        assert recovery_pkg._journal_cpld_too_old(["tt-smi"], "cpld fw 1.16 needed for tt-smi -r", print) is True
        wrapped = "CPLD FW v1.16 or higher\nis required to use tt-smi -r"
        assert recovery_pkg._journal_cpld_too_old(["tt-smi"], wrapped, print) is True

    def test_the_check_itself_mutates_nothing(self, monkeypatch):
        """It reads and records; the CALLER latches. Keeps it safe to call from anywhere."""
        monkeypatch.setattr(recovery_pkg, "health_event", lambda kind, **f: None)
        monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)

        recovery_pkg._journal_cpld_too_old(["tt-smi", "-r"], self.REAL, print)

        assert os.environ.get("TT_DEVICE_MCP_RESET_MODE") is None

    def test_the_latch_makes_the_reset_mode_known(self, health_deps, monkeypatch, tmp_path):
        """Once tt-smi has said this host is a Galaxy, the mode is no longer unknown.

        The loud reset_mode_unknown warning exists for a host nothing could identify. Repeating it
        after the host itself answered would be reporting an open question that is closed.
        """
        mech = _local_reset_mechanism(tmp_path)
        monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
        monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
        monkeypatch.setattr(health_deps, "board_types_provider", lambda: None)

        assert reset_mode_known(health_deps, mech) is False
        mech.cpld_forces_galaxy = True
        assert reset_mode_known(health_deps, mech) is True
        # Still unknown to a caller with no mechanism to consult, which is the honest answer.
        assert reset_mode_known(health_deps) is False

    def test_the_latch_selects_the_galaxy_ladder(self, health_deps, monkeypatch, tmp_path):
        """The point of the whole change: the next rung stops repeating the `-r` tt-smi disowned."""
        from tt_device_mcp.health.recovery.galaxy import GalaxyRecovery
        from tt_device_mcp.health.recovery.per_target import PerTargetRecovery

        mech = _local_reset_mechanism(tmp_path)
        monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
        monkeypatch.setattr(health_deps, "board_types_provider", lambda: None)
        monitor = object()

        assert isinstance(select_recovery(monitor, mech, health_deps), PerTargetRecovery)
        mech.cpld_forces_galaxy = True
        assert isinstance(select_recovery(monitor, mech, health_deps), GalaxyRecovery)

    def test_an_operator_declaration_still_outranks_the_latch(self, health_deps, monkeypatch, tmp_path):
        """Evidence, not an override.

        An operator may be deliberately holding a host on the per-target ladder; the banner must
        inform that choice, not silently reverse it.
        """
        from tt_device_mcp.health.recovery.per_target import PerTargetRecovery

        mech = _local_reset_mechanism(tmp_path)
        mech.cpld_forces_galaxy = True
        monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "per-target")

        assert isinstance(select_recovery(object(), mech, health_deps), PerTargetRecovery)
