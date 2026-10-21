# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 09 I7: the autouse per-test reset leaves no broker job, hold, hold-bound or escalation state
behind, so no test's result depends on the order it runs in or on the xdist worker it lands on."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import _reset_server_state
from tt_device_mcp import device_holders as dh
from tt_device_mcp import server as srv
from tt_device_mcp.health import chip_state

REPO = Path(__file__).resolve().parent.parent

# Each pair failed with its first test run first: the first leaves state the second reads.
LEAKING_PAIRS = [
    # The I76 bound's host-rung latch and the escalation kind ('hold_bound_cycle').
    "tests/test_hold_bound.py::test_a_stuck_hold_event_past_the_bound_never_reads_escalated_false",
    "tests/test_hold_bound.py::test_a_latched_i42_hold_still_escalates_and_its_latch_stands",
    # The shared recovery mechanism's class-A 10-min ladder anchor.
    "tests/test_power_cycle_ac_needed.py::test_the_watchdog_starts_the_class_a_ladder_at_the_10_min_mark",
    "tests/test_power_cycle_ac_needed.py::test_the_full_ladder_runs_10_min_after_the_last_cycle_or_boot_then_the_cycle",
    # chip_state's configured present/expected chip sources.
    "tests/test_chip_state.py::test_the_published_state_is_atomic_and_names_every_expected_chip",
    "tests/test_chip_state.py::test_a_re_created_node_is_read_at_once_and_a_valid_read_clears_lost",
]

# Every global the reset owns, with the value a fresh broker process starts with.
FRESH = {
    "device_op_stage_started_at": "",
    "device_held_since": "",
    "device_hold_escalated_monotonic": 0.0,
    "device_hold_deadline_bucket": 0,
    "device_hold_alert_next_bucket": 0,
    "device_hold_alert_gap": 0,
    "device_hold_escalate_bucket": 0,
    "device_hold_offbus_escalated": False,
    "device_hold_offbus_tenant_wait": False,
    "device_hold_offbus_idle_early": False,
    "device_hold_offbus_prior_bucket": None,
    "device_hold_tenant_deferred": False,
    "device_hold_job_bound_escalated": False,
    "device_hold_escalation_trigger": "",
    "device_hold_escalation_kind": "",
    "_forced_escalation_task": None,
    "_host_action_suppressed_logged": None,
    "exec_scope_seq": 0,
    "_idle_holder_task": None,
    "_idle_holder_last_scan": 0.0,
    "_hold_bound_episode": "",
    "_hold_bound_ladder_age": None,
    "_hold_bound_ladder_recovered": False,
    "_hold_bound_cycle_age": None,
    "_hold_bound_check_failed_age": None,
    "_hold_bound_unarmed_paged": "",
    "_hold_bound_rung_pending": False,
    "_hold_bound_denied_why": "",
    "_hold_bound_tenant_paged": "",
    "_hold_bound_tenant_blocker": None,
}
FRESH_SETS = ["reset_killed_job_ids", "request_killed_job_ids", "preempt_forced_job_ids", "_client_gone_kills"]


def test_the_per_test_reset_clears_every_hold_bound_and_escalation_global(monkeypatch):
    """A test that leaves a hold-bound latch, an escalation kind or a class-A ladder anchor set does
    not hand it to the next test."""
    for name, fresh in FRESH.items():
        monkeypatch.setattr(srv, name, "left over" if isinstance(fresh, str) else 7)
    for name in FRESH_SETS:
        getattr(srv, name).add("job-1")
    srv._idle_holders["1234"] = {"left": "over"}
    m = srv.recovery_mechanism
    monkeypatch.setattr(m, "_class_a_ladder_anchor", 1.0)
    monkeypatch.setattr(m, "_class_a_ladder_spawned", 1.0)
    chip_state.configure(present=lambda: ["0"], expected=lambda: 4)

    _reset_server_state()

    assert {name: getattr(srv, name) for name in FRESH} == FRESH
    assert {name: getattr(srv, name) for name in FRESH_SETS} == {name: set() for name in FRESH_SETS}
    assert srv._idle_holders == {}
    assert (m._class_a_ladder_anchor, m._class_a_ladder_spawned) == (None, None)
    chip_state.reset()
    assert (chip_state._present, chip_state._expected) == (chip_state._sysfs_indices, chip_state._no_baseline)


class _HostHoldsTheRealDevice:
    """``os`` as the holder walk sees it on a device host where this process has the real
    /dev/tenstorrent/0 open as fd 9: no test may open the real node to set that up."""

    def __getattr__(self, name):
        return getattr(os, name)

    def listdir(self, path):
        return ["9"] if path == f"/proc/{os.getpid()}/fd" else os.listdir(path)

    def readlink(self, path):
        return "/dev/tenstorrent/0" if path == f"/proc/{os.getpid()}/fd/9" else os.readlink(path)


def test_a_real_device_holder_on_the_host_is_not_a_holder_in_a_test(monkeypatch):
    """Spec 09 I1 (holder scan): a real job holding the host's own /dev/tenstorrent does not read as
    a tenant in an unmarked test. Unsealed, it kept a runner test's job waiting on the holder and
    blocked a hold-bound test's host rung, only while the host ran device work."""
    monkeypatch.setattr(dh, "os", _HostHoldsTheRealDevice())
    monkeypatch.setattr(dh, "_iter_pids", lambda: [os.getpid()])
    monkeypatch.setattr(srv, "_present_chip_indices", lambda: ["0"])

    assert dh.enumerate_device_holders().holders == []
    assert srv._tenant_holder_reason() == ""


def test_the_leaking_pairs_pass_with_the_leaking_test_first():
    """The order that failed before the reset covered these globals: each pair's first test runs first,
    in one process, and both pass."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH")])))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:xdist", "-p", "no:cacheprovider"]
        + ["--no-device-tests", "-rA", *LEAKING_PAIRS],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    passed = [line.split(" ", 1)[1] for line in run.stdout.splitlines() if line.startswith("PASSED ")]
    assert run.returncode == 0 and passed == LEAKING_PAIRS, run.stdout[-3000:]
