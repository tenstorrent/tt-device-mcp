# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A fabric check cut off by a broker stop is journaled, never lost (spec 03 I50).

systemd stops the broker by signalling its whole cgroup, the running validator with it, and
SIGKILLs what is left at the stop timeout. Python runs no cleanup on that SIGTERM, so the broker
marks a check in flight on disk; the next startup journals `fabric_check_interrupted` once and
clears the mark. Nothing is taken from the cut-off run: the startup gate runs its own full pass.
"""

import asyncio
import json

import pytest

from tt_device_mcp.health import evidence
from tt_device_mcp.health import monitor as mon


def _events(kind):
    return [e for e in evidence.read_health_events() if e.get("kind") == kind]


def _monitor(monkeypatch, body):
    m = mon.HealthMonitor.__new__(mon.HealthMonitor)
    monkeypatch.setattr(m, "_fabric_check_cmd", lambda: "fabric-check.sh", raising=False)
    monkeypatch.setattr(m, "_verify_fabric_health_body", body, raising=False)
    return m


def test_a_check_in_flight_is_marked_and_cleared(monkeypatch):
    seen = {}

    async def body(timeout_sec):
        seen["mark"] = json.loads((evidence.health_dir() / mon.FABRIC_INFLIGHT_FILE).read_text())
        return True, "fabric links healthy"

    m = _monitor(monkeypatch, body)
    assert asyncio.run(m.verify_fabric_health(5)) == (True, "fabric links healthy")
    assert seen["mark"]["cmd"] == "fabric-check.sh"
    assert not (evidence.health_dir() / mon.FABRIC_INFLIGHT_FILE).exists()
    assert mon.journal_interrupted_fabric_check() is None
    assert _events("fabric_check_interrupted") == []


def test_a_mark_left_by_a_stopped_broker_is_journaled_once_at_startup():
    # What a SIGTERM/SIGKILL mid-check leaves: the mark, with no finally run to remove it.
    mon._fabric_inflight_begin("fabric-check.sh")
    ev = mon.journal_interrupted_fabric_check()
    assert ev and ev["how"] == "broker_stopped" and ev["cmd"] == "fabric-check.sh"
    assert len(_events("fabric_check_interrupted")) == 1
    assert mon.journal_interrupted_fabric_check() is None, "the same interruption was journaled twice"
    assert len(_events("fabric_check_interrupted")) == 1


def test_an_unreadable_mark_is_still_journaled():
    (evidence.health_dir() / mon.FABRIC_INFLIGHT_FILE).write_text("{not json")
    assert mon.journal_interrupted_fabric_check() is not None
    assert len(_events("fabric_check_interrupted")) == 1


def test_a_cancelled_check_is_journaled_at_once(monkeypatch):
    async def body(timeout_sec):
        raise asyncio.CancelledError

    m = _monitor(monkeypatch, body)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(m.verify_fabric_health(5))
    ev = _events("fabric_check_interrupted")
    assert len(ev) == 1 and ev[0]["how"] == "cancelled"
    assert mon.journal_interrupted_fabric_check() is None


def test_startup_journals_the_interruption_before_anything_else(monkeypatch):
    import tt_device_mcp.server as srv

    mon._fabric_inflight_begin("fabric-check.sh")
    monkeypatch.setattr(srv, "_startup_tasks_done", False)
    monkeypatch.setattr(srv, "should_privsep", lambda: False)
    monkeypatch.setattr(srv, "log_rung_inventory", lambda: None)

    async def no_probe():
        return None

    monkeypatch.setattr(srv, "_probe_on_per_user_start", no_probe)
    asyncio.run(srv.run_startup_tasks())
    assert len(_events("fabric_check_interrupted")) == 1
    assert not (evidence.health_dir() / mon.FABRIC_INFLIGHT_FILE).exists()
