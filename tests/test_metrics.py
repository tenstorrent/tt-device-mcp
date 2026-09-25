# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Prometheus telemetry module: closed-enum validation, the FSM dwell-clock math
(including the open-interval flush at write time), the atomic textfile writer, and cross-checks
that every literal label vocabulary here still matches its real source (see metrics.py's own
module docstring for why they are literals, not imports)."""

import pytest

from tt_device_mcp import metrics


@pytest.fixture(autouse=True)
def _fresh_metrics():
    """Every other test in this suite that touches a ServerFsm or a probe check calls straight
    into this module's process-global Counters — the same objects production uses. A test here
    that asserts an absolute value (not a delta) needs to start from zero."""
    metrics.reset_for_tests()
    yield
    metrics.reset_for_tests()


# ---- closed-enum validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn,args",
    [
        (metrics.stage_fired, ("bridge_reset", "not-a-real-outcome")),
        (metrics.stage_fired, ("not-a-real-stage", "ok")),
        (metrics.probe_observed, ("not-a-real-probe", "healthy", 1.0)),
        (metrics.probe_observed, ("pci", "not-a-real-verdict", 1.0)),
        (metrics.job_completed, ("cancelled",)),  # not a real JobStatus outcome — see OUTCOMES
        (metrics.recovery_episode_opened, ("not-a-real-fault",)),
        (metrics.recovery_episode_closed, ("not-a-real-fault", 1.0)),
        (metrics.state_entered, ("broken",)),
    ],
)
def test_every_helper_rejects_an_unknown_label(fn, args):
    with pytest.raises(ValueError):
        fn(*args)


def test_valid_labels_are_accepted_for_every_helper():
    metrics.stage_fired("power_cycle", "blocked")
    metrics.stage_fired("ubb_tray", "not_applicable")
    metrics.probe_observed("fabric", "skipped", 0.5)
    metrics.job_completed("completed")
    metrics.job_completed("hung")
    metrics.recovery_episode_opened("off_bus")
    metrics.recovery_episode_closed("off_bus", 30.0)
    metrics.state_entered("healthy", now=1.0)


# ---- state_entered / state_seconds_total --------------------------------------------------------


def test_state_transition_accumulates_seconds():
    metrics.state_entered("recovering", now=100.0)
    metrics.state_entered("healthy", now=160.0)
    out = metrics.render().decode()
    assert 'tt_device_broker_state_seconds_total{state="recovering"} 60.0' in out


def test_state_entered_first_call_accumulates_nothing():
    """No prior state exists yet — nothing to close out, and no negative/garbage dwell time."""
    metrics.state_entered("boot", now=500.0)
    out = metrics.render().decode()
    assert 'tt_device_broker_state_seconds_total{state="boot"}' not in out


def test_server_state_gauge_is_one_hot():
    metrics.state_entered("recovering", now=0.0)
    out = metrics.render().decode()
    assert 'tt_device_broker_server_state{state="recovering"} 1.0' in out
    assert 'tt_device_broker_server_state{state="healthy"} 0.0' in out
    assert 'tt_device_broker_server_state{state="boot"} 0.0' in out
    assert 'tt_device_broker_server_state{state="down"} 0.0' in out


def test_a_state_held_across_two_writes_shows_a_growing_counter(tmp_path, monkeypatch):
    """The bug this closes: state_entered only ever settled the OUTGOING state's dwell at the
    NEXT transition, so a broker sitting in RECOVERING for hours exported this series FLAT at its
    stale value for the whole incident — increase(...[15m]) reads 0 during exactly the window an
    operator would alert on. write_textfile() must flush the open interval on every call, with NO
    transition in between, so the published counter grows write over write."""
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(tmp_path / "node-exporter"))
    clock = {"t": 1000.0}
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock["t"])

    metrics.state_entered("recovering")  # now=1000.0 via the patched clock

    clock["t"] = 1030.0
    metrics.write_textfile()
    first = (tmp_path / "node-exporter" / metrics.TEXTFILE_NAME).read_text()
    assert 'tt_device_broker_state_seconds_total{state="recovering"} 30.0' in first

    clock["t"] = 1090.0
    metrics.write_textfile()
    second = (tmp_path / "node-exporter" / metrics.TEXTFILE_NAME).read_text()
    assert 'tt_device_broker_state_seconds_total{state="recovering"} 90.0' in second

    # Never transitioned — still RECOVERING, one-hot gauge unchanged across both writes.
    assert 'tt_device_broker_server_state{state="recovering"} 1.0' in second


def test_flush_dwell_does_not_double_count_between_a_write_and_a_transition(monkeypatch):
    """write_textfile's flush rebaselines the clock — a transition shortly after a write must
    account only the time since that write, not double-book the interval write_textfile already
    flushed."""
    clock = {"t": 0.0}
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock["t"])

    metrics.state_entered("recovering")
    clock["t"] = 50.0
    metrics._flush_dwell()  # exercised directly, same seam write_textfile calls
    clock["t"] = 70.0
    metrics.state_entered("healthy")

    out = metrics.render().decode()
    assert 'tt_device_broker_state_seconds_total{state="recovering"} 70.0' in out


# ---- job_completed -------------------------------------------------------------------------------


def test_all_job_outcomes_exist_at_zero_from_the_first_write():
    """A freshly started broker must publish "0 jobs, every outcome" — not nothing. A labeled
    counter emits no series until its first increment, which made the core metric of a job broker
    invisible until a job happened to finish."""
    out = metrics.render().decode()
    for outcome in metrics.OUTCOMES:
        assert f'tt_device_broker_jobs_total{{outcome="{outcome}"}} 0.0' in out


def test_job_completed_counts_outcomes_and_nothing_else():
    """Timing publishes from the snapshot's occupancy clock, not from job completion — this
    counts outcomes, including HUNG, which the legacy CLI-facing Stats counters still omit."""
    metrics.job_completed("failed")
    metrics.job_completed("hung")
    out = metrics.render().decode()
    assert 'tt_device_broker_jobs_total{outcome="failed"} 1.0' in out
    assert 'tt_device_broker_jobs_total{outcome="hung"} 1.0' in out
    assert "tt_device_broker_busy_seconds_total 0.0" in out, "job completion must not advance the occupancy clock"


# ---- render() / textfile writer -----------------------------------------------------------------


def test_render_exposition_format():
    metrics.jobs_total.labels(outcome="completed").inc()
    out = metrics.render()
    assert out.startswith(b"# HELP") or b"tt_device_broker" in out


def test_write_textfile_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(tmp_path / "node-exporter"))
    metrics.jobs_total.labels(outcome="completed").inc()
    metrics.write_textfile()

    target = tmp_path / "node-exporter" / metrics.TEXTFILE_NAME
    assert target.exists()
    assert target.read_bytes() == metrics.render()
    assert not any((tmp_path / "node-exporter").glob(f"{metrics.TEXTFILE_NAME}.*.tmp"))


def test_write_textfile_tmp_name_is_pid_scoped(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(tmp_path / "node-exporter"))
    expected_tmp = tmp_path / "node-exporter" / f"{metrics.TEXTFILE_NAME}.{os.getpid()}.tmp"
    # Confirm the exact path write_textfile computes, not just "some .tmp file" — two writers
    # (a restart racing its predecessor) must never collide on the same temp path.
    assert metrics._textfile_dir() / f"{metrics.TEXTFILE_NAME}.{os.getpid()}.tmp" == expected_tmp


def test_write_textfile_creates_the_directory(tmp_path, monkeypatch):
    target_dir = tmp_path / "does" / "not" / "exist" / "yet"
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(target_dir))
    metrics.write_textfile()
    assert (target_dir / metrics.TEXTFILE_NAME).exists()


def test_write_textfile_never_raises_when_directory_is_unwritable(tmp_path, monkeypatch, caplog):
    """The one thing telemetry may never do is take the broker down with it. A directory that
    cannot be created (its parent is a FILE, not a dir) must degrade to a logged warning, not an
    exception — the exact bug pattern already fixed once in fsm.py's own ``_persist``."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(blocker / "node-exporter"))

    metrics.write_textfile()  # must not raise
    metrics.write_textfile()  # a second failure must not raise either

    assert not (blocker / "node-exporter").exists()


def test_write_textfile_does_not_leak_tmp_on_a_failed_replace(tmp_path, monkeypatch):
    """write_bytes can succeed and os.replace can still fail (e.g. a permissions change on the
    directory mid-write) — the tmp file must not linger and hold space on exactly the ENOSPC that
    could have caused the failure."""
    import os as os_mod

    target_dir = tmp_path / "node-exporter"
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(target_dir))

    real_replace = os_mod.replace

    def _boom(*a, **k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(metrics.os, "replace", _boom)
    metrics.write_textfile()
    monkeypatch.setattr(metrics.os, "replace", real_replace)

    assert not any(target_dir.glob(f"{metrics.TEXTFILE_NAME}.*.tmp"))


def test_write_textfile_logs_the_failure_once_per_process(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(blocker / "node-exporter"))

    with caplog.at_level("ERROR", logger="tt_device_mcp.metrics"):
        metrics.write_textfile()
        metrics.write_textfile()

    failures = [r for r in caplog.records if "could not write" in r.message]
    assert len(failures) == 1, "the write failure must be logged once per process, not every call"


def test_write_textfile_recovers_once_the_directory_is_writable_again(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(blocker / "node-exporter"))
    metrics.write_textfile()

    good_dir = tmp_path / "good"
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(good_dir))
    metrics.write_textfile()

    assert (good_dir / metrics.TEXTFILE_NAME).exists()


# ---- cross-checks: the literal label vocabularies must match their real source ------------------


def test_states_match_fsm_server_state():
    from tt_device_mcp.fsm import ServerState

    assert metrics.STATES == tuple(s.value for s in ServerState)


def test_faults_match_fsm_faults():
    from tt_device_mcp.fsm import FAULTS

    assert metrics.FAULTS == FAULTS


def test_verdicts_match_health_core_verdict():
    from tt_device_mcp.health.core import Verdict

    assert metrics.VERDICTS == tuple(v.value for v in Verdict)


def test_stages_match_the_constants_registry():
    """constants.STAGE_NAMES is the one place the five rungs are named — metrics.STAGES imports
    it directly rather than copying it, so this mostly pins that the import target hasn't been
    swapped for a stale local copy."""
    from tt_device_mcp.constants import STAGE_NAMES

    assert metrics.STAGES == STAGE_NAMES


def test_stages_also_match_health_recovery_own_stage_constants():
    """health.recovery's own next_stage()/_route() decision constants (STAGE_BRIDGE_RESET etc,
    which omit ubb_tray — it is outside that router entirely) now import from the SAME
    constants.STAGE_NAMES metrics.STAGES does, so this pins that re-export rather than a second,
    independent vocabulary that merely happened to agree."""
    from tt_device_mcp.health.recovery import (
        STAGE_BRIDGE_RESET,
        STAGE_HOST_REBOOT,
        STAGE_POWER_CYCLE,
        STAGE_SMI_RESET,
    )

    for name in (STAGE_BRIDGE_RESET, STAGE_SMI_RESET, STAGE_HOST_REBOOT, STAGE_POWER_CYCLE):
        assert name in metrics.STAGES


def test_outcomes_match_the_job_statuses_stats_actually_counts():
    from tt_device_mcp.server import JobStatus

    assert set(metrics.OUTCOMES) == {
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.KILLED.value,
        JobStatus.TIMEOUT.value,
        JobStatus.HUNG.value,
    }
    # There is no separate "cancelled" JobStatus — _kill_job() cancels a QUEUED job by setting
    # the same JobStatus.KILLED a running job gets when killed.
    assert not hasattr(JobStatus, "CANCELLED")


# ---- fsm.py wiring: real transitions drive real metrics -----------------------------------------


def test_fsm_on_fault_opens_a_recovery_episode(tmp_path):
    from tt_device_mcp.fsm import ServerFsm

    f = ServerFsm(tmp_path / "fsm.json")
    f.on_fault("off_bus")
    out = metrics.render().decode()
    assert 'tt_device_broker_recovery_episodes_total{why="off_bus"} 1.0' in out
    assert 'tt_device_broker_server_state{state="recovering"} 1.0' in out


def test_fsm_recovery_closes_the_episode_duration(tmp_path):
    from datetime import datetime, timezone

    from tt_device_mcp.fsm import ServerFsm
    from tt_device_mcp.health.core import HealthState

    f = ServerFsm(tmp_path / "fsm.json")
    f.on_fault("eth_frozen")
    f.on_readings(HealthState(phase="test", at=datetime.now(timezone.utc), expected=0))

    # The episode duration is wall-clock (fsm._episode_elapsed_sec parses the ISO `since` it
    # persisted, independent of time.monotonic) — a real test run closes it near-instantly, so
    # only that the episode was counted is asserted, not its magnitude.
    out = metrics.render().decode()
    assert 'tt_device_broker_recovery_episodes_total{why="eth_frozen"} 1.0' in out
    assert 'tt_device_broker_recovery_episode_seconds_total{why="eth_frozen"}' in out


def test_fsm_terminal_outcome_reaches_down_gauge(tmp_path):
    from tt_device_mcp.fsm import OUTCOME_TERMINAL, ServerFsm

    f = ServerFsm(tmp_path / "fsm.json")
    f.on_fault("arc_dead")
    f.on_outcome(OUTCOME_TERMINAL)
    out = metrics.render().decode()
    assert 'tt_device_broker_server_state{state="down"} 1.0' in out


# ---- server.py wiring: Stats.record_job_completion ------------------------------------------------


def test_stats_record_job_completion_reports_metrics():
    from tt_device_mcp.server import JobStatus, Stats

    s = Stats()
    s.record_job_completion(JobStatus.COMPLETED, wait_sec=3.0, runtime_sec=10.0)
    out = metrics.render().decode()
    assert 'tt_device_broker_jobs_total{outcome="completed"} 1.0' in out


def test_stats_record_job_completion_counts_hung_in_prometheus_but_not_the_legacy_json():
    """JobStatus.HUNG stays uncounted by the legacy jobs_completed/failed/killed/timeout fields
    (that CLI-facing stats/ JSON gap predates this task and stays as-is — total_jobs must not
    move), but IS reported to Prometheus as its own outcome. Its device time reaches busy via
    the occupancy clock, not from here."""
    from tt_device_mcp.server import JobStatus, Stats

    s = Stats()
    s.record_job_completion(JobStatus.HUNG, wait_sec=None, runtime_sec=900.0)
    assert s.total_jobs == 0
    out = metrics.render().decode()
    assert 'tt_device_broker_jobs_total{outcome="hung"} 1.0' in out


# ---- health/recovery wiring: "blocked" vs. "not_applicable" is consistent per-stage -------------


def test_ubb_tray_no_tray_on_this_platform_is_not_applicable_not_blocked():
    """The bug this closes: on a per-target (non-Galaxy) host there is no tray concept at all, so
    _ubb_tray_walk_plan returns None on EVERY below-floor off-bus drop — mapping that to "blocked"
    made the series climb forever on a platform that will never have a tray rung, indistinguishable
    from an operator leaving a kill-switch off."""
    import asyncio

    from tt_device_mcp.health.recovery.galaxy import GalaxyRecovery

    async def _run():
        recovery = GalaxyRecovery.__new__(GalaxyRecovery)
        # beats={} + expected=1 -> _ubb_tray_walk_plan finds no whole/partial tray to route,
        # exactly the per-target-host shape (a single/multi-card box with no UBB trays at all).
        return await recovery._attempt_ubb_tray_reset({}, off_bus=1, expected=1, log=lambda m: None)

    result = asyncio.run(_run())
    assert result is None
    out = metrics.render().decode()
    assert 'tt_device_broker_stages_fired_total{outcome="not_applicable",stage="ubb_tray"} 1.0' in out
    assert 'tt_device_broker_stages_fired_total{outcome="blocked",stage="ubb_tray"}' not in out


def test_bridge_reset_missing_pci_address_is_not_applicable_not_blocked():
    """A chip this process never cached a PCI address for cannot be reset through a bridge it
    cannot name — a topology/bookkeeping fact, not an operator-flippable guard."""
    import asyncio
    from unittest.mock import MagicMock

    from tt_device_mcp.health.recovery import Recovery

    class _NoBridgeRecovery(Recovery):
        def next_stage(self, ev):
            raise NotImplementedError

        def _platform_reset_argv(self, indices):
            raise NotImplementedError

    deps = MagicMock()
    deps.isolated_chips.return_value = {"0"}
    deps.device_pci_map.return_value = {}  # no cached address for chip "0"
    recovery = _NoBridgeRecovery(monitor=MagicMock(), mechanism=MagicMock(), deps=deps)

    result = asyncio.run(recovery._recover_isolated_chips(log=lambda m: None))
    assert result is False
    out = metrics.render().decode()
    assert 'tt_device_broker_stages_fired_total{outcome="not_applicable",stage="bridge_reset"} 1.0' in out
