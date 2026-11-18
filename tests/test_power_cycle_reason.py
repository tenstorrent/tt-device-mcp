# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Every host power cycle records why it was requested (spec 04 I113).

One record per request (trigger, fault class, the chips with address, tray and root port, the last failed check, the
rungs the hold ran, the hold's start and age) is required by every path that asks for the cycle: a plain string raises
before anything runs. It reaches the request event, ``power_cycle_os_first``, a state file written and fsynced
before the reboot, one WARNING line before the reboot, ``power_cycle_confirmed`` after the boot, and the health
status. No test runs a real step or reads a chip: ``_run_step`` is a stub, and the root port comes from a stubbed
sysfs lookup.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.conftest import GALAXY_BUS_IDS, patch_health_event, power_cycle_reason
from tests.test_classac_power_cycle_direct import EXPECTED, _beats_without, _gate, _off, rig  # noqa: F401 - the fixture
from tests.test_power_cycle_os_first import BOOT_NOW, CYCLE, _steps, booted, escalation, os_first  # noqa: F401
from tt_device_mcp import cli
from tt_device_mcp import server as srv
from tt_device_mcp.health import bmc_capture
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy
from tt_device_mcp.health.recovery import offbus_power_cycle as opc
from tt_device_mcp.health.recovery import power_cycle_reason as pcr
from tt_device_mcp.health.recovery.power_cycle_reason import PowerCycleReason
from tt_device_mcp.health.recovery.stages import power_cycle as pc

_REAL_FIRE_COMMAND = opc.fire_command  # the suite-wide tripwire replaces it per test
PREFIX = "host power cycle:"
ROOT_PORT = "0000:00:01.1"


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    """The state file's directory, and a root port for every address (sysfs is not read)."""
    monkeypatch.setattr(pcr, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(bmc_capture, "upstream_bridge", lambda bdf: ROOT_PORT)
    return tmp_path


def _record(**kw):
    kw.setdefault("trigger", "idle_escalation")
    kw.setdefault("fault_class", "partial_offbus")
    kw.setdefault("chips", [{"chip": "3", "bdf": "0000:41:00.0", "tray": 1, "root_port": ROOT_PORT}])
    kw.setdefault("failed_check", "post-reset-ladder check")
    kw.setdefault("failed_result", "1/32 chip(s) off the bus, 0 ARC not ready")
    kw.setdefault("rungs", [pcr.rung_row(1_000.0, "glx_reset", "failed")])
    kw.setdefault("hold_since", "2026-11-17T10:00:00")
    kw.setdefault("hold_age_sec", 612)
    return PowerCycleReason("idle escalation: 1/32 chip(s) off the bus after the reset ladder", **kw)


# ---- the record ---------------------------------------------------------------------------------


@pytest.mark.parametrize("plain", ["idle escalation: 1/32 chip(s) off the bus", "", None, {"reason": "x"}])
def test_a_request_without_the_record_is_refused(plain):
    with pytest.raises(TypeError, match="PowerCycleReason"):
        PowerCycleReason.require(plain)
    assert PowerCycleReason.require(_record()).trigger == "idle_escalation"


@pytest.mark.parametrize("missing", ["trigger", "fault_class"])
def test_the_record_needs_its_trigger_and_fault_class(missing):
    kw = {"trigger": "tray_down", "fault_class": "whole_tray", missing: ""}
    with pytest.raises(ValueError):
        PowerCycleReason("text", **kw)


def test_the_record_keeps_the_one_line_text_and_every_field():
    r = _record()
    assert isinstance(r, str) and str(r).startswith("idle escalation: 1/32 chip(s)")
    d = r.as_dict()
    assert d["trigger"] == "idle_escalation" and d["fault_class"] == "partial_offbus"
    assert d["chips"] == [{"chip": "3", "bdf": "0000:41:00.0", "tray": 1, "root_port": ROOT_PORT}]
    assert d["failed_check"] == "post-reset-ladder check" and d["failed_result"].startswith("1/32 chip(s)")
    assert d["rungs"][0]["rung"] == "glx_reset" and d["rungs"][0]["status"] == "failed"
    assert d["hold_since"] == "2026-11-17T10:00:00" and d["hold_age_sec"] == 612
    assert d["reason"] == str(r) and d["requested_at"].endswith("Z")
    json.dumps(d)


def test_the_warning_line_has_the_stable_prefix_and_every_field():
    line = _record().line()
    assert line.startswith(PREFIX) and "\n" not in line
    for part in (
        "trigger=idle_escalation",
        "class=partial_offbus",
        "3 (0000:41:00.0, tray 1, root port 0000:00:01.1)",
        "last_check=post-reset-ladder check",
        "result=1/32 chip(s) off the bus, 0 ARC not ready",
        "rungs=[glx_reset=failed]",
        "hold_since=2026-11-17T10:00:00",
        "hold_age=612s",
        "reason=idle escalation",
    ):
        assert part in line, part


def test_the_line_says_unknown_where_a_field_is_not_known():
    bare = PowerCycleReason("x", trigger="hold_deadline", fault_class="unknown")
    line = bare.line()
    assert line.startswith(PREFIX) and "last_check=unknown" in line and "hold_age=unknown" in line


def test_the_chips_carry_tray_and_root_port_from_memory_and_sysfs(state_dir):
    rows = pcr.build_chips({"10": "0000:42:00.0", "2": None}, {1: [2], 2: [10]})
    assert rows == [
        {"chip": "2", "bdf": None, "tray": 1, "root_port": None},
        {"chip": "10", "bdf": "0000:42:00.0", "tray": 2, "root_port": ROOT_PORT},
    ]


def test_a_root_port_that_cannot_be_read_is_none_never_an_error(monkeypatch):
    def boom(bdf):
        raise OSError("no sysfs")

    monkeypatch.setattr(bmc_capture, "upstream_bridge", boom)
    assert pcr.build_chips({"0": "0000:01:00.0"})[0]["root_port"] is None


def test_completed_fills_only_what_the_record_lacks(state_dir):
    since = (datetime.now() - timedelta(seconds=700)).isoformat(timespec="seconds")
    bare = PowerCycleReason("x", trigger="hold_deadline", fault_class="unknown")
    rung = pcr.rung_row(1.0, "tt-smi -r", "ok")
    done = bare.completed(
        fault_class="whole_tray", bdfs={"0": "0000:01:00.0"}, tray_map={1: [0]}, hold_since=since, rungs=[rung]
    )
    assert done.fault_class == "whole_tray" and done.trigger == "hold_deadline"
    assert done.chips == [{"chip": "0", "bdf": "0000:01:00.0", "tray": 1, "root_port": ROOT_PORT}]
    assert done.hold_since == since and 690 <= done.hold_age_sec <= 720 and done.rungs == [rung]
    kept = _record().completed(fault_class="other", bdfs={"9": "x"}, hold_since="2020-01-01T00:00:00", rungs=[rung])
    assert kept.fault_class == "partial_offbus" and kept.chips[0]["chip"] == "3"
    assert kept.hold_since == "2026-11-17T10:00:00" and kept.hold_age_sec == 612
    assert kept.rungs[0]["rung"] == "glx_reset"


# ---- the state file -----------------------------------------------------------------------------


def test_the_state_file_is_fsynced_and_read_back(state_dir, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(pcr.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    r = _record()
    at = pcr.write_state(r, unit="ttdev-power-cycle-1")
    assert synced and (state_dir / pcr.POWER_CYCLE_REASON_FILE).exists()
    assert not list(state_dir.glob("*.tmp"))
    row = pcr.read_state()
    assert row["outcome"] == "requested" and row["unit"] == "ttdev-power-cycle-1"
    assert row["requested_epoch"] == round(at, 3)
    assert row["trigger"] == "idle_escalation" and row["chips"][0]["root_port"] == ROOT_PORT
    last = pcr.last_power_cycle()
    assert last["reason"] == str(r) and last["outcome"] == "requested" and last["hold_age_sec"] == 612


def test_a_state_file_that_cannot_be_written_never_stops_the_cycle(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(pcr, "health_dir", lambda: tmp_path / "missing" / "dir")
    monkeypatch.setattr(pcr.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with caplog.at_level(logging.ERROR, logger="tt-device-mcp"):
        assert pcr.write_state(_record()) > 0
    assert "NOT written" in caplog.text and pcr.last_power_cycle() is None


def test_the_boot_reads_only_the_record_of_its_own_request(state_dir):
    at = pcr.write_state(_record())
    assert pcr.record_for_boot(at - 5, at + 5)["trigger"] == "idle_escalation"
    assert pcr.record_for_boot(at + 1, at + 9) is None, "a record from before the window is another request's"
    pcr.mark_outcome("confirmed", at + 100)
    assert pcr.read_state()["outcome"] == "requested", "another request's outcome is left alone"
    pcr.mark_outcome("confirmed", at, shutdown_sec=12)
    assert pcr.last_power_cycle()["outcome"] == "confirmed"


def test_no_record_no_last_power_cycle(state_dir):
    assert pcr.last_power_cycle() is None and pcr.record_for_boot(0, 9e12) is None
    (state_dir / pcr.POWER_CYCLE_REASON_FILE).write_text("not json")
    assert pcr.last_power_cycle() is None


# ---- the OS-first fire --------------------------------------------------------------------------


def test_the_os_first_fire_refuses_a_plain_string_before_anything_runs(os_first, state_dir, monkeypatch):
    monkeypatch.setattr(opc, "fire_command", _REAL_FIRE_COMMAND)
    for fire in (
        lambda: pc.fire_os_first(CYCLE, "auto BMC power cycle"),
        lambda: pc._fire_power_cycle("auto BMC power cycle"),
        lambda: opc.fire_command(CYCLE, "3/32 off the bus", 5),
        lambda: opc.fire_and_record(CYCLE, "3/32 off the bus", {}),
    ):
        with pytest.raises(TypeError, match="PowerCycleReason"):
            fire()
    assert os_first["calls"] == [] and os_first["events"] == []
    assert not os_first["req"].exists() and pcr.last_power_cycle() is None


def test_the_record_is_logged_and_on_disk_before_the_unit_and_the_reboot(os_first, state_dir, monkeypatch, caplog):
    seen = {}
    inner = pc._run_step

    def step(argv, timeout=pc.STEP_TIMEOUT_SEC):
        key = "reboot" if "reboot" in argv else "unit" if argv[0] == "systemd-run" else "other"
        if key != "other":
            seen[key] = ((state_dir / pcr.POWER_CYCLE_REASON_FILE).exists(), PREFIX in caplog.text)
        return inner(argv, timeout)

    monkeypatch.setattr(pc, "_run_step", step)
    with caplog.at_level(logging.WARNING, logger="tt-device-mcp"):
        assert pc.fire_os_first(CYCLE, _record()) == "os_first"
    assert seen == {"unit": (True, True), "reboot": (True, True)}
    (line,) = [r for r in caplog.records if r.getMessage().startswith(PREFIX)]
    assert line.levelno == logging.WARNING and "trigger=idle_escalation" in line.getMessage()
    assert pcr.read_state()["outcome"] == "requested"
    assert pcr.read_state()["unit"].startswith("tt-device-mcp-")


def test_the_os_first_event_carries_the_record_not_the_generic_text(os_first, state_dir):
    pc.fire_os_first(CYCLE, _record())
    (event,) = [k for n, k in os_first["events"] if n == "power_cycle_os_first"]
    assert event["phase"] == "reboot_requested" and event["reason"].startswith("idle escalation")
    assert "auto BMC power cycle" not in json.dumps(event)
    rec = event["power_cycle_reason"]
    assert rec["trigger"] == "idle_escalation" and rec["chips"][0]["tray"] == 1 and rec["hold_age_sec"] == 612
    assert rec["failed_check"] and rec["rungs"]
    assert os_first["req"].read_text().splitlines()[1].startswith("idle escalation")


def test_the_default_bmc_cycle_passes_the_callers_record_through(os_first, state_dir):
    pc._fire_power_cycle(_record(trigger="hold_deadline"))
    (event,) = [k for n, k in os_first["events"] if n == "power_cycle_os_first"]
    assert event["power_cycle_reason"]["trigger"] == "hold_deadline" and "auto BMC" not in event["reason"]


def test_the_bmc_fallback_that_stays_pending_keeps_the_record(os_first, state_dir):
    os_first["rc"].update(reboot=1, stop=1, **{"is-active": 0})
    assert pc.fire_os_first(CYCLE, _record()) == "fallback_pending"
    (event,) = [k for n, k in os_first["events"] if n == "power_cycle_os_first"]
    assert event["phase"] == "fallback_pending" and event["power_cycle_reason"]["trigger"] == "idle_escalation"
    assert pcr.read_state()["outcome"] == "requested", "the fallback may still fire: the request stands"


def test_a_cycle_that_did_not_launch_says_so_in_the_record(os_first, state_dir):
    os_first["rc"].update(**{"systemd-run": 1})
    with pytest.raises(pc.PowerCycleNotLaunched):
        pc.fire_os_first(CYCLE, _record())
    (event,) = [k for n, k in os_first["events"] if n == "power_cycle_os_first"]
    assert event["phase"] == "not_launched" and event["power_cycle_reason"]["trigger"] == "idle_escalation"
    assert pcr.last_power_cycle()["outcome"] == "not_launched"


# ---- the escalation's request event -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_event_carries_the_record_on_every_escalation(escalation, state_dir):
    mech = escalation["mech"]
    assert await srv._auto_power_cycle_host(lambda _l: None, _record()) is True
    (event,) = [k for n, k in escalation["hevents"] if n == "auto_power_cycle_request"]
    assert event["reason"].startswith("idle escalation") and event["power_cycle_reason"]["trigger"] == "idle_escalation"
    assert [r["reason"] for r in mech.read_auto_recovery_ledger()][0].startswith("idle escalation")
    assert _steps(escalation) == ["preflight", "systemd-run", "reboot"]


@pytest.mark.asyncio
async def test_an_escalation_without_the_record_is_refused_before_it_is_recorded(escalation):
    with pytest.raises(TypeError):
        await srv._auto_power_cycle_host(lambda _l: None, "idle escalation: 1/32 chip(s) off the bus")
    with pytest.raises(TypeError):
        await escalation["mech"]._fire_recovery_escalation(
            "power-cycle", row_owner="x", label="x", detail="x", fire=lambda: None, log=lambda _m: None, reason="plain"
        )
    assert escalation["mech"].read_auto_recovery_ledger() == [] and escalation["calls"] == []


@pytest.mark.asyncio
async def test_the_server_completes_the_record_from_memory_without_a_chip_read(escalation, state_dir, monkeypatch):
    """A caller that names only its trigger still sends the chips with address, tray and root port, and the rungs."""
    mech = escalation["mech"]
    monkeypatch.setattr(srv, "_offbus_one_shot_chips", lambda: {"0"})
    monkeypatch.setattr(srv, "device_pci_map", {"0": "0000:01:00.0", "1": "0000:02:00.0"})
    monkeypatch.setattr(srv, "health_monitor", SimpleNamespace(expected=lambda present: 32))
    monkeypatch.setattr(srv, "galaxy_recovery", None)
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-01-01T00:00:00")
    await srv._auto_power_cycle_host(lambda _l: None, power_cycle_reason("hold bound", trigger="hold_deadline"))
    (event,) = [k for n, k in escalation["hevents"] if n == "auto_power_cycle_request"]
    rec = event["power_cycle_reason"]
    assert rec["chips"][0]["bdf"] == "0000:01:00.0" and rec["chips"][0]["root_port"] == ROOT_PORT
    assert rec["hold_since"] == "2026-01-01T00:00:00" and rec["hold_age_sec"] > 0
    assert pcr.read_state()["chips"] == rec["chips"]
    assert mech is srv.recovery_mechanism


def test_the_rungs_the_hold_ran_are_listed_newest_last(escalation):
    mech = escalation["mech"]
    mech.rung_history.clear()
    mech._write_action_log("[broker]", "tt-smi -r 0", 1.0, "reset", 0)
    mech._write_action_log("[broker]", "glx_reset", 2.0, "reset", 1)
    rungs = mech.rungs_since("1970-01-01T00:00:00")
    assert [r["rung"] for r in rungs][-2:] == ["tt-smi -r 0", "glx_reset"]
    assert mech.rungs_since("2999-01-01T00:00:00") == []


# ---- each path that requests a cycle ------------------------------------------------------------


@pytest.fixture
def captured(rig, monkeypatch):  # noqa: F811 - the imported fixture
    """The records the galaxy recovery sends to the broker's power cycle, with its chip addresses and trays known."""
    got = []

    async def cycle(log, reason):
        got.append(reason)

    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle)
    monkeypatch.setattr(bmc_capture, "upstream_bridge", lambda bdf: ROOT_PORT)
    monkeypatch.setattr(rig["g"], "_chip_buses_now", lambda: dict(enumerate(GALAXY_BUS_IDS)))
    rig["got"] = got
    return rig


@pytest.mark.asyncio
async def test_a_tray_down_cycle_names_the_trays_chips_and_the_check(captured):
    off = _off(captured["map"], {3: 8})
    captured["beats"] = _beats_without(off)
    await _gate(captured, galaxy.STAGE_SMI_RESET)
    (rec,) = captured["got"]
    assert isinstance(rec, PowerCycleReason) and rec.trigger == "tray_down"
    assert rec.fault_class == galaxy.FAULT_CLASS_WHOLE_TRAY
    assert {int(c["chip"]) for c in rec.chips} == off and {c["tray"] for c in rec.chips} == {3}
    assert all(c["root_port"] == ROOT_PORT for c in rec.chips)
    assert rec.failed_check and f"{len(off)}/{EXPECTED}" in rec.failed_result


@pytest.mark.asyncio
async def test_an_all_chips_off_cycle_is_recorded_as_such(captured):
    captured["g"].mechanism.mesh_reset_failed_since_release = True
    captured["beats"] = {}
    await _gate(captured, galaxy.STAGE_SMI_RESET)
    (rec,) = captured["got"]
    assert rec.trigger == "all_chips_off" and rec.fault_class == galaxy.FAULT_CLASS_ALL_OFF
    assert len(rec.chips) == EXPECTED


def test_the_galaxy_record_lists_the_rungs_and_the_hold_age(captured, monkeypatch):
    g = captured["g"]
    since = (datetime.now() - timedelta(seconds=900)).isoformat(timespec="seconds")
    monkeypatch.setattr(g.deps, "device_hold_episode_since", lambda: since)
    g.mechanism.rung_history.clear()
    g.mechanism._write_action_log("[broker]", "glx_reset", 3.0, "reset", 1)
    rec = g._power_cycle_reason(
        "idle escalation: 1/32 chip(s) off", trigger="idle_escalation", chips=[0], failed_check="c", failed_result="r"
    )
    assert rec.hold_since == since and 880 <= rec.hold_age_sec <= 930
    assert [r["rung"] for r in rec.rungs] == ["glx_reset"] and rec.chips[0]["bdf"] == GALAXY_BUS_IDS[0]


@pytest.mark.asyncio
async def test_the_idle_escalation_cause_is_a_record_of_the_bus_as_it_reads_now(captured, monkeypatch):
    g = captured["g"]
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(g.deps, "read_heartbeats", lambda: {str(i): 100 for i in range(1, EXPECTED)})
    rec = await g._host_rung_cause_now(EXPECTED, "idle escalation", "fallback text")
    assert rec.trigger == "idle_escalation" and rec.failed_check == "post-reset-ladder check"
    assert [c["chip"] for c in rec.chips] == ["0"] and rec.failed_result.startswith("1/32 chip(s) off the bus")
    monkeypatch.setattr(g.deps, "read_heartbeats", lambda: 1 / 0)
    rec = await g._host_rung_cause_now(EXPECTED, "idle escalation", "fallback text")
    assert str(rec) == "fallback text" and rec.failed_result == "the heartbeats could not be read"
    monkeypatch.setattr(g.mechanism, "scope_active", lambda: "scope")
    rec = await g._host_rung_cause_now(EXPECTED, "idle escalation", "fallback text", trigger="fabric_unverified")
    assert rec.trigger == "fabric_unverified" and "still cycling" in rec.failed_result


@pytest.mark.asyncio
async def test_the_hold_deadline_cycle_carries_its_trigger_and_hold_age(monkeypatch, clear_job_state):
    got = []

    async def cycle(log, reason):
        got.append(reason)

    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv, "_auto_power_cycle_host", cycle)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_bound_tenant_blocker", lambda bound: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv.recovery_mechanism, "auto_recovery_allowed", lambda *a, **k: (True, ""))
    await srv._hold_bound_host_rung(640.0, lambda _m: None, sweep=False)
    (rec,) = got
    assert isinstance(rec, PowerCycleReason) and rec.trigger == "hold_deadline"
    assert rec.hold_age_sec == 640 and rec.failed_check and rec.failed_result
    monkeypatch.setattr(srv, "device_hold_escalation_kind", "offbus_one_shot")
    await srv._hold_bound_host_rung(640.0, lambda _m: None, sweep=False)
    assert got[-1].trigger == "offbus_one_shot", "the window that claimed the cycle is the trigger"


# ---- after the boot -----------------------------------------------------------------------------


def _kept(booted, **kw):
    """The state file the previous boot left, stamped between the ledger row and this boot's start."""
    rec = _record(**kw)
    rec.requested_epoch = BOOT_NOW - 250
    row = {**rec.as_dict(), "unit": "u", "outcome": "requested"}
    (booted["tmp"] / pcr.POWER_CYCLE_REASON_FILE).write_text(json.dumps(row) + "\n")
    return rec


def test_the_confirmation_after_the_boot_reads_the_record_back(booted, monkeypatch):
    monkeypatch.setattr(pcr, "health_dir", lambda: booted["tmp"])
    rec = _kept(booted)
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "confirmed"
    (event,) = [k for n, k in booted["hevents"] if n == "power_cycle_confirmed"]
    assert event["reason"] == str(rec)
    assert event["power_cycle_reason"]["trigger"] == "idle_escalation" and event["power_cycle_reason"]["chips"]
    assert event["power_cycle_reason"]["hold_age_sec"] == 612
    assert pcr.last_power_cycle()["outcome"] == "confirmed"


def test_an_unconfirmed_cycle_names_its_reason_too(booted, monkeypatch):
    monkeypatch.setattr(pcr, "health_dir", lambda: booted["tmp"])
    _kept(booted)
    booted["out"]["restart_cause"] = ""
    booted["rc"]["restart_cause"] = 1
    booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW)
    events = [k for n, k in booted["hevents"] if n in ("power_cycle_confirmed", "power_cycle_unconfirmed")]
    assert len(events) == 1 and events[0]["power_cycle_reason"]["trigger"] == "idle_escalation"


def test_a_boot_with_no_record_falls_back_to_the_ledger_reason(booted, monkeypatch):
    monkeypatch.setattr(pcr, "health_dir", lambda: booted["tmp"])
    assert booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW) == "confirmed"
    (event,) = [k for n, k in booted["hevents"] if n == "power_cycle_confirmed"]
    assert event["reason"] and "power_cycle_reason" not in event


def test_a_record_of_another_request_is_not_attached(booted, monkeypatch):
    monkeypatch.setattr(pcr, "health_dir", lambda: booted["tmp"])
    rec = _record()
    rec.requested_epoch = BOOT_NOW + 500  # after this boot started: not this boot's request
    row = {**rec.as_dict(), "outcome": "requested"}
    (booted["tmp"] / pcr.POWER_CYCLE_REASON_FILE).write_text(json.dumps(row) + "\n")
    booted["mech"].confirm_power_cycle_on_boot(now_epoch=BOOT_NOW)
    (event,) = [k for n, k in booted["hevents"] if n == "power_cycle_confirmed"]
    assert "power_cycle_reason" not in event
    assert recovery_base.RecoveryMechanism._reason_fields(None, {}) == {}


# ---- status -------------------------------------------------------------------------------------


def test_the_status_shows_the_last_power_cycle_time_and_reason(state_dir):
    assert cli.last_power_cycle_line(None) == "" and cli.last_power_cycle_line({}) == ""
    pcr.write_state(_record())
    last = pcr.last_power_cycle()
    line = cli.last_power_cycle_line(last)
    assert line.startswith("Last power cycle: ") and last["requested_at"] in line and last["reason"] in line
    assert "[requested]" in line
    lines = cli.overview_lines([], [], [], last_power_cycle=last)
    assert lines[-1] == line and cli.overview_lines([], [], []) == cli.overview_lines([], [], [], last_power_cycle=None)


def test_the_health_payload_carries_the_last_power_cycle(state_dir, monkeypatch):
    pcr.write_state(_record())
    payload = srv._health_payload()
    assert payload["last_power_cycle"]["reason"].startswith("idle escalation")
    assert payload["last_power_cycle"]["trigger"] == "idle_escalation"
