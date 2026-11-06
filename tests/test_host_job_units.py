# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 09 I10: no test reaches the host's own broker job and exec units.

On a broker host the live broker runs each job in a ttdev-job-<id>.scope. A test's job can share
that id: a kill test then read the live scope as its own and asked systemd to SIGINT it. Here a
`systemctl` stand-in on PATH plays that host, reporting ttdev-job-323.scope live. Through the real
query paths, no test sees it, and no action on it is spawned.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from tt_device_mcp import server as srv

from .conftest import _spawn_is_forbidden

LIVE = "ttdev-job-323.scope"


@pytest.fixture
def host_broker(tmp_path, monkeypatch):
    """A `systemctl` on PATH that reports LIVE active and logs every call it gets."""
    calls = tmp_path / "calls"
    fake = tmp_path / "bin" / "systemctl"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        f'case "$1" in list-units) echo "{LIVE} loaded active running";; show) echo active;; esac\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return calls


def test_the_stand_in_answers_for_units_that_are_not_the_brokers(host_broker):
    """The control: the fake host is reachable, so the reads below are answered by the guard."""
    run = subprocess.run(["systemctl", "is-active", "tt-device-broker"], capture_output=True, text=True)
    assert run.returncode == 0
    assert host_broker.read_text().strip() == "is-active tt-device-broker"


def test_a_live_host_job_scope_reads_as_none_through_the_real_queries(host_broker):
    assert srv._scope_active(srv.job_scope_unit("323")) is False
    assert srv.list_active_job_scopes() == {}
    show = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", LIVE], capture_output=True, text=True)
    assert show.stdout == ""
    assert not host_broker.exists()


async def test_a_live_host_job_scope_reads_as_none_through_asyncio(host_broker):
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "is-active", "--quiet", LIVE, stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    assert (proc.returncode, out) == (3, b"")
    assert not host_broker.exists()


async def test_killing_a_tests_job_never_takes_the_host_scope_path(host_broker):
    """The incident: job 323 is killed while the host runs ttdev-job-323.scope."""
    assert await srv._terminate_job("323", None) == "nothing to stop (no live scope or pid)"
    assert not host_broker.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["systemctl", "kill", "--signal=SIGINT", LIVE],
        ["systemctl", "stop", LIVE],
        ["systemctl", "freeze", LIVE],
        ["systemctl", "reset-failed", "ttdev-exec-1-2.scope"],
        ["systemctl", "stop", "ttdev-job-*.scope"],
        ["systemd-run", "--scope", "--unit=ttdev-job-9.scope", "--", "true"],
    ],
)
def test_an_action_on_a_broker_unit_is_refused_marker_or_not(argv, host_broker):
    assert _spawn_is_forbidden(argv, allow_device_spawns=True)
    assert _spawn_is_forbidden(argv, allow_device_spawns=False)
    with pytest.raises(AssertionError, match="forbidden command"):
        subprocess.run(argv)
    assert not host_broker.exists()


def test_a_glob_over_job_scopes_counts_as_naming_them(host_broker):
    run = subprocess.run(["systemctl", "list-units", "--no-legend", "*.scope"], capture_output=True, text=True)
    assert run.stdout == ""
    assert not host_broker.exists()
