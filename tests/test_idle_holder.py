# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Idle foreign holder detector (spec 03 I44): a tenant process outside every broker scope that holds a
device fd with its CPU time flat past the threshold is named once — a health event and an alert with
its user and pid — and never killed or signalled."""

import os

import pytest

from tests.conftest import patch_health_event
from tt_device_mcp import alert, device_holders
from tt_device_mcp import server as srv
from tt_device_mcp.device_holders import DeviceHolder, HolderScan, read_proc_cpu

TENANT_UID = srv.MIN_TENANT_UID + 7


@pytest.fixture
def watch(monkeypatch):
    """Fake /proc CPU reads and record events, alerts and any signal. Returns a namespace whose
    ``cpu`` dict maps pid -> (ticks, starttime)."""
    monkeypatch.setattr(srv, "_idle_holders", {})
    monkeypatch.delenv("TT_DEVICE_MCP_IDLE_HOLDER_SEC", raising=False)
    monkeypatch.setattr(device_holders, "username_for_uid", lambda uid: f"user{uid}")
    cpu: dict = {}
    monkeypatch.setattr(srv, "read_proc_cpu", lambda pid: cpu.get(pid))
    monkeypatch.setattr(srv, "descends_from", lambda pid, ancestor: False)
    events, alerts, signals = [], [], []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(alert, "alert_argv", lambda: ["/bin/true"])
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: alerts.append(event) or object())
    monkeypatch.setattr(os, "kill", lambda *a: signals.append(a))

    class NS:
        pass

    ns = NS()
    ns.cpu, ns.events, ns.alerts, ns.signals = cpu, events, alerts, signals
    return ns


def _scan(*holders):
    return HolderScan(holders=list(holders), complete=True)


def test_a_flat_holder_past_the_threshold_is_named_once_and_never_signalled(watch):
    h = DeviceHolder(pid=4242, uid=TENANT_UID)
    watch.cpu[4242] = (100, "555")

    assert srv._watch_idle_foreign_holders(_scan(h), 0.0) == []
    assert srv._watch_idle_foreign_holders(_scan(h), 899.0) == [], "inside 15 min: nothing yet"
    named = srv._watch_idle_foreign_holders(_scan(h), 900.0)
    srv._watch_idle_foreign_holders(_scan(h), 2000.0)

    assert named == [{"pid": 4242, "user": f"user{TENANT_UID}", "idle_sec": 900}]
    kinds = [k for k, _ in watch.events]
    assert kinds == ["idle_foreign_holder"], "one event for the holder's lifetime"
    assert watch.events[0][1]["pid"] == 4242 and watch.events[0][1]["user"] == f"user{TENANT_UID}"
    assert len(watch.alerts) == 1, "one alert"
    assert watch.alerts[0]["kind"] == "idle_foreign_holder"
    assert (watch.alerts[0]["pid"], watch.alerts[0]["user"]) == (4242, f"user{TENANT_UID}")
    assert watch.signals == [], "never killed or signalled"


def test_cpu_time_moving_restarts_the_clock(watch):
    h = DeviceHolder(pid=4242, uid=TENANT_UID)
    watch.cpu[4242] = (100, "555")
    srv._watch_idle_foreign_holders(_scan(h), 0.0)
    watch.cpu[4242] = (101, "555")
    srv._watch_idle_foreign_holders(_scan(h), 600.0)
    assert srv._watch_idle_foreign_holders(_scan(h), 1400.0) == [], "busy at 600 s: only 800 s flat"
    assert srv._watch_idle_foreign_holders(_scan(h), 1500.0) != []


@pytest.mark.parametrize(
    "holder,why",
    [
        (DeviceHolder(pid=4242, uid=TENANT_UID, scoped=True), "inside a broker scope (its own job)"),
        (DeviceHolder(pid=4242, uid=0), "not a tenant (a system service)"),
    ],
)
def test_broker_jobs_and_system_holders_are_never_counted(watch, holder, why):
    watch.cpu[4242] = (100, "555")
    for t in (0.0, 900.0, 5000.0):
        assert srv._watch_idle_foreign_holders(_scan(holder), t) == [], why
    assert watch.events == [] and watch.alerts == []


def test_a_child_of_the_broker_is_never_counted(watch, monkeypatch):
    monkeypatch.setattr(srv, "descends_from", lambda pid, ancestor: ancestor == os.getpid())
    h = DeviceHolder(pid=4242, uid=TENANT_UID)
    watch.cpu[4242] = (100, "555")
    for t in (0.0, 900.0, 5000.0):
        assert srv._watch_idle_foreign_holders(_scan(h), t) == []


def test_a_reused_pid_is_a_new_process(watch):
    h = DeviceHolder(pid=4242, uid=TENANT_UID)
    watch.cpu[4242] = (100, "555")
    srv._watch_idle_foreign_holders(_scan(h), 0.0)
    watch.cpu[4242] = (100, "777")  # same pid and ticks, new starttime
    assert srv._watch_idle_foreign_holders(_scan(h), 900.0) == [], "the new process starts its own clock"
    assert srv._watch_idle_foreign_holders(_scan(h), 1800.0) != []


def test_a_busy_alert_hook_is_retried_without_a_second_event(watch, monkeypatch):
    h = DeviceHolder(pid=4242, uid=TENANT_UID)
    watch.cpu[4242] = (100, "555")
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: None)  # hook still busy
    srv._watch_idle_foreign_holders(_scan(h), 0.0)
    srv._watch_idle_foreign_holders(_scan(h), 900.0)
    monkeypatch.setattr(alert, "send_alert", lambda event, log=None: watch.alerts.append(event) or object())
    srv._watch_idle_foreign_holders(_scan(h), 960.0)
    srv._watch_idle_foreign_holders(_scan(h), 1020.0)

    assert [k for k, _ in watch.events] == ["idle_foreign_holder"]
    assert len(watch.alerts) == 1, "sent once the hook is free, then never again"


def test_a_gone_holder_is_forgotten(watch):
    h = DeviceHolder(pid=4242, uid=TENANT_UID)
    watch.cpu[4242] = (100, "555")
    srv._watch_idle_foreign_holders(_scan(h), 0.0)
    srv._watch_idle_foreign_holders(_scan(), 100.0)
    assert srv._idle_holders == {}


@pytest.mark.parametrize("raw,expected", [("60", 60.0), ("", 900.0), ("junk", 900.0), ("0", 0.0)])
def test_threshold_is_configurable(monkeypatch, raw, expected):
    monkeypatch.setenv("TT_DEVICE_MCP_IDLE_HOLDER_SEC", raw)
    assert srv._idle_holder_sec() == expected


def test_threshold_zero_turns_the_detector_off(watch, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_IDLE_HOLDER_SEC", "0")
    h = DeviceHolder(pid=4242, uid=TENANT_UID)
    watch.cpu[4242] = (100, "555")
    for t in (0.0, 900.0, 5000.0):
        assert srv._watch_idle_foreign_holders(_scan(h), t) == []
    assert watch.events == []


def test_read_proc_cpu_reads_this_process_and_none_for_a_gone_pid():
    got = read_proc_cpu(os.getpid())
    assert got is not None
    ticks, start = got
    assert ticks >= 0 and start.isdigit()
    assert read_proc_cpu(2**22 + 1) is None, "above pid_max: never a live process"


@pytest.mark.asyncio
async def test_the_sampler_spawn_is_rate_limited(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_IDLE_HOLDER_SEC", raising=False)
    monkeypatch.setattr(srv, "_idle_holder_task", None)
    monkeypatch.setattr(srv, "_idle_holder_last_scan", 0.0)
    passes = []

    async def one_pass():
        passes.append(1)

    monkeypatch.setattr(srv, "_idle_holder_watch_pass", one_pass)
    srv._maybe_spawn_idle_holder_watch()
    await srv._idle_holder_task
    srv._maybe_spawn_idle_holder_watch()  # inside IDLE_HOLDER_SCAN_SEC: no second scan
    assert passes == [1]
