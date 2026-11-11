# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I18: the vendor CPLD dump comes before recovery, once per fault episode.

A fake DIAG script and a fake ipmitool stand in for the vendor's: the fake ipmitool keeps each
UBB CPLD's debug index in a file, and the fake DIAG moves that index the way the vendor's does,
so the save/restore is checked against a register that really changed."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time

import pytest

from tt_device_mcp.fsm import ServerFsm, ServerState
from tt_device_mcp.health import cpld_dump
from tt_device_mcp.health import evidence as health
from tt_device_mcp.health.recovery import base as recovery_base

FAKE_IPMI = """#!{py}
import os, sys
regs = os.environ["FAKE_CPLD_REGS"]
a = sys.argv[1:]
if a[:3] == ["sdr", "elist", "full"]:
    if os.environ.get("FAKE_SDR_FAIL"):
        print("Error: no BMC", file=sys.stderr)
        sys.exit(1)
    print("PSU0_VIN         | 70h | ok  | 10.1 | 207 Volts")
    print("PSU0_POUT        | 72h | ok  | 10.1 | 1840 Watts")
    print("P12V_UBB0        | 80h | ok  |  7.1 | 12.06 Volts")
    print("CPU0_TEMP        | 01h | ok  |  3.1 | 45 degrees C")
    sys.exit(0)
assert a[:3] == ["raw", "0x06", "0x52"], a
bus, op, reg = a[3], a[5], a[6]
path = os.path.join(regs, bus)
if os.environ.get("FAKE_CPLD_ABSENT") == bus:
    sys.exit(1)
if op == "0x01":
    print(" " + (open(path).read() if os.path.exists(path) else "00"))
elif os.environ.get("FAKE_CPLD_REFUSE") != bus + ":" + a[7]:
    open(path, "w").write(a[7].replace("0x", "").zfill(2))
"""

FAKE_DIAG = """#!/bin/bash
VERSION=V1.03.01
echo "This Script: $VERSION"
[ "$1" = DIAG ] || exit 2
"{ipmi}" raw 0x06 0x52 0x0D 0x80 0x00 0x50 0x3c
"{ipmi}" raw 0x06 0x52 0x11 0x80 0x00 0x50 0x07
{py} -c 'import fcntl,os,sys; fd=os.open(os.environ["TT_DEVICE_MCP_CPLD_LOCK"],os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); print("lock: free")
except OSError:
    print("lock: held")'
printf 'raw\\tbytes \\x01 kept\\n'
sleep "${{FAKE_DIAG_SLEEP:-0}}"
echo "[[ (local) DIAG Summary Begin ]]"
echo "  3 FAILED signals in 620 TEST items:"
exit "${{FAKE_DIAG_RC:-0}}"
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    regs = tmp_path / "regs"
    regs.mkdir()
    for bus in ("0x0D", "0x0F", "0x11", "0x13"):
        (regs / bus).write_text("02")
    ipmi = tmp_path / "fake-bmc-tool"
    ipmi.write_text(FAKE_IPMI.format(py=sys.executable))
    ipmi.chmod(0o755)
    diag = tmp_path / "fake-diag.sh"
    diag.write_text(FAKE_DIAG.format(ipmi=ipmi, py=sys.executable))
    lock = tmp_path / "tt-cpld.lock"
    monkeypatch.setenv("FAKE_CPLD_REGS", str(regs))
    monkeypatch.setenv("TT_DEVICE_MCP_CPLD_DUMP", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_CPLD_DUMP_SCRIPT", str(diag))
    monkeypatch.setenv("TT_DEVICE_MCP_CPLD_LOCK", str(lock))
    monkeypatch.setenv("TT_DEVICE_MCP_CPLD_DUMP_LOCK_WAIT_SEC", "0.5")
    monkeypatch.setattr(cpld_dump, "IPMITOOL", str(ipmi))
    monkeypatch.setattr(cpld_dump, "SUDO", ())
    monkeypatch.setattr(cpld_dump, "HOST_SCRIPT", str(tmp_path / "no-host-copy.sh"))
    monkeypatch.setattr(cpld_dump, "PINNED_SCRIPT", str(tmp_path / "no-pinned-copy.sh"))
    return {"regs": regs, "diag": diag, "lock": lock, "tmp": tmp_path}


def _events(kind):
    return [e for e in health.read_health_events() if e.get("kind") == kind]


def _mechanism(episode="2026-11-10T10:00:00", order=None):
    async def pollers(active, _log):
        if order is not None and not active:
            order.append(("reset", len(_events("cpld_dump"))))
        return []

    m = recovery_base.RecoveryMechanism(
        current_boot_id=lambda: "boot",
        boot_btime_id=lambda: "0",
        scoped_reset_backend=lambda: False,
        set_device_pollers=pollers,
        set_device_op_detail=lambda _d: None,
        begin_action_row=lambda *_a: None,
        write_action_log=lambda *_a: None,
        device_hold_episode_since=lambda: "",
        local_reset_dir=lambda: health.health_dir(),
    )
    m.cpld_episode = lambda: episode
    return m


@pytest.mark.asyncio
async def test_the_dump_comes_before_the_reset(fake):
    order = []
    m = _mechanism(order=order)
    rc, _ = await m.reset_with_quiesce([sys.executable, "-c", "pass"], lambda _s: None)
    assert rc == 0
    assert order == [("reset", 1)], "the reset quiesced the pollers before the dump was journaled"


@pytest.mark.asyncio
async def test_the_host_reboot_waits_for_the_dump(fake):
    seen = []
    m = _mechanism()
    await m._fire_recovery_escalation(
        "reboot",
        row_owner="[broker]reboot-request",
        label="host reboot",
        detail="test",
        fire=lambda: seen.append(len(_events("cpld_dump"))),
        log=lambda _s: None,
        reason="test",
    )
    assert seen == [1]


@pytest.mark.asyncio
async def test_a_failing_dump_still_lets_recovery_run(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DIAG_RC", "1")
    m = _mechanism()
    rc, _ = await m.reset_with_quiesce([sys.executable, "-c", "pass"], lambda _s: None)
    assert rc == 0
    (ev,) = _events("cpld_dump")
    assert ev["rc"] == 1


@pytest.mark.asyncio
async def test_a_raising_dump_still_lets_recovery_run(fake, monkeypatch):
    def boom(*_a):
        raise RuntimeError("dump exploded")

    monkeypatch.setattr(cpld_dump, "take_dump", boom)
    lines = []
    rc, _ = await _mechanism().reset_with_quiesce([sys.executable, "-c", "pass"], lines.append)
    assert rc == 0
    assert any("recovery proceeds" in line for line in lines)


@pytest.mark.asyncio
async def test_a_hung_dump_is_killed_at_the_cap_and_recovery_runs(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DIAG_SLEEP", "30")
    monkeypatch.setenv("TT_DEVICE_MCP_CPLD_DUMP_CAP_SEC", "1")
    t0 = time.monotonic()
    rc, _ = await _mechanism().reset_with_quiesce([sys.executable, "-c", "pass"], lambda _s: None)
    assert rc == 0
    assert time.monotonic() - t0 < 10
    (ev,) = _events("cpld_dump")
    assert ev["rc"] is None and ev["summary"].startswith("timed out")
    assert all(v["restored"] for v in ev["index_0x50"].values()), "a killed dump still restores the index"


@pytest.mark.asyncio
async def test_one_dump_per_episode(fake):
    m = _mechanism()
    for _ in range(3):
        await m.cpld_dump_before_recovery("reset", lambda _s: None)
    assert len(_events("cpld_dump")) == 1
    # A restarted broker (a fresh mechanism) in the same episode does not dump it again.
    await _mechanism().cpld_dump_before_recovery("reset", lambda _s: None)
    assert len(_events("cpld_dump")) == 1
    await _mechanism(episode="2026-11-10T11:00:00").cpld_dump_before_recovery("reset", lambda _s: None)
    assert len(_events("cpld_dump")) == 2


@pytest.mark.asyncio
async def test_no_episode_no_dump(fake):
    await _mechanism(episode="").cpld_dump_before_recovery("reset", lambda _s: None)
    assert _events("cpld_dump") == [] and _events("cpld_dump_skipped") == []


@pytest.mark.asyncio
async def test_a_degraded_fault_starts_the_dump(fake, tmp_path, health_deps):
    fsm = ServerFsm(tmp_path / "fsm.json")
    fsm.boot(health_deps)
    assert fsm.mechanism.cpld_episode() == ""
    fsm.on_fault("off_bus", detail="tray 3 dropped")
    assert fsm.state is ServerState.RECOVERING
    task = fsm.mechanism._cpld_task
    assert task is not None and fsm.mechanism._cpld_task_episode == fsm.mechanism.cpld_episode()
    await task
    (ev,) = _events("cpld_dump")
    assert ev["trigger"] == "off_bus"
    # The first rung of the same episode reuses that dump.
    await fsm.mechanism.cpld_dump_before_recovery("reset", lambda _s: None)
    assert len(_events("cpld_dump")) == 1


@pytest.mark.asyncio
async def test_a_hold_fault_does_not_start_the_dump(fake, tmp_path, health_deps):
    fsm = ServerFsm(tmp_path / "fsm.json")
    fsm.boot(health_deps)
    fsm.on_fault("foreign_holder", detail="someone else's process", dirty=False)
    assert fsm.mechanism._cpld_task is None


def test_the_event_names_path_summary_and_script(fake):
    rec = cpld_dump.take_dump("off_bus", "ep", lambda _s: None)
    (ev,) = _events("cpld_dump")
    assert ev["path"] == rec["path"] and os.path.isfile(ev["path"])
    assert ev["summary"] == "3/620 failed"
    assert ev["script"] == str(fake["diag"]) and ev["which"] == "override"
    assert ev["version"] == "V1.03.01"
    assert ev["sha256"] == hashlib.sha256(fake["diag"].read_bytes()).hexdigest()
    assert ev["episode"] == "ep" and ev["trigger"] == "off_bus"


def test_raw_stdout_is_saved_unchanged(fake):
    rec = cpld_dump.take_dump("t", "ep", lambda _s: None)
    raw = open(rec["path"], "rb").read()
    assert b"raw\tbytes \x01 kept\n" in raw
    assert raw.startswith(b"This Script: V1.03.01\n")


def test_progress_lines_while_it_runs(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DIAG_SLEEP", "1.5")
    monkeypatch.setattr(cpld_dump, "PROGRESS_SEC", 0.5)
    lines = []
    cpld_dump.take_dump("t", "ep", lines.append)
    assert sum("lines, at:" in line for line in lines) >= 2


def test_host_script_wins(fake, monkeypatch):
    host = fake["tmp"] / "host-copy.sh"
    host.write_text(fake["diag"].read_text())
    pinned = fake["tmp"] / "pinned.sh"
    pinned.write_text(fake["diag"].read_text())
    monkeypatch.delenv("TT_DEVICE_MCP_CPLD_DUMP_SCRIPT")
    monkeypatch.setattr(cpld_dump, "HOST_SCRIPT", str(host))
    monkeypatch.setattr(cpld_dump, "PINNED_SCRIPT", str(pinned))
    monkeypatch.setattr(cpld_dump, "PINNED_SHA256", hashlib.sha256(pinned.read_bytes()).hexdigest())
    assert cpld_dump.resolve_script()[:2] == (str(host), "host")
    host.unlink()
    assert cpld_dump.resolve_script()[:2] == (str(pinned), "pinned")


def test_a_pinned_copy_with_the_wrong_sha_never_runs(fake, monkeypatch):
    pinned = fake["tmp"] / "pinned.sh"
    pinned.write_text(fake["diag"].read_text() + "# edited\n")
    monkeypatch.delenv("TT_DEVICE_MCP_CPLD_DUMP_SCRIPT")
    monkeypatch.setattr(cpld_dump, "PINNED_SCRIPT", str(pinned))
    rec = cpld_dump.take_dump("t", "ep", lambda _s: None)
    assert "expected " + cpld_dump.PINNED_SHA256 in rec["skipped"]
    assert _events("cpld_dump") == [] and len(_events("cpld_dump_skipped")) == 1


def test_the_lock_is_held_while_the_dump_runs(fake):
    rec = cpld_dump.take_dump("t", "ep", lambda _s: None)
    assert b"lock: held" in open(rec["path"], "rb").read()
    fd = os.open(fake["lock"], os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released afterwards
    os.close(fd)


def test_a_busy_lock_skips_with_a_reason(fake):
    fd = cpld_dump._open_lock(str(fake["lock"]))
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rec = cpld_dump.take_dump("t", "ep", lambda _s: None)
    finally:
        os.close(fd)
    assert "still held" in rec["skipped"]
    (ev,) = _events("cpld_dump_skipped")
    assert ev["lock_wait_sec"] >= 0.5


def test_the_lock_opens_an_existing_file_without_o_creat(fake, monkeypatch):
    """fs.protected_regular refuses O_CREAT on another user's file in sticky /run/lock: an
    existing lock file must be opened without it."""
    fake["lock"].write_text("")
    real_open = os.open
    flags_seen = []

    def spy(path, flags, *a):
        flags_seen.append(flags)
        return real_open(path, flags, *a)

    monkeypatch.setattr(cpld_dump.os, "open", spy)
    os.close(cpld_dump._open_lock(str(fake["lock"])))
    assert flags_seen and not any(f & os.O_CREAT for f in flags_seen)
    fake["lock"].unlink()
    flags_seen.clear()
    os.close(cpld_dump._open_lock(str(fake["lock"])))
    assert oct(fake["lock"].stat().st_mode & 0o777) == oct(0o644)


@pytest.mark.parametrize("exists", [True, False])
def test_the_lock_file_is_never_world_writable(fake, monkeypatch, exists):
    """The lock carries no data: it is opened read-only and created 0644 even under umask 0."""
    if exists:
        fake["lock"].write_text("")
        fake["lock"].chmod(0o644)
    real_open = os.open
    flags_seen = []

    def spy(path, flags, *a):
        flags_seen.append(flags)
        return real_open(path, flags, *a)

    monkeypatch.setattr(cpld_dump.os, "open", spy)
    old = os.umask(0)
    try:
        fd = cpld_dump._open_lock(str(fake["lock"]))
    finally:
        os.umask(old)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    assert flags_seen and all(f & os.O_ACCMODE == os.O_RDONLY for f in flags_seen)
    assert not fake["lock"].stat().st_mode & 0o022


def test_the_debug_index_is_restored_and_verified(fake):
    rec = cpld_dump.take_dump("t", "ep", lambda _s: None)
    for bus in ("0x0D", "0x0F", "0x11", "0x13"):
        assert (fake["regs"] / bus).read_text() == "02"
        assert rec["index_0x50"][bus] == {"before": "0x02", "after": "0x02", "restored": True}


def test_an_index_that_does_not_restore_is_named(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CPLD_REFUSE", "0x11:0x02")  # takes the DIAG's select, refuses the restore
    monkeypatch.setenv("FAKE_CPLD_ABSENT", "0x13")
    rec = cpld_dump.take_dump("t", "ep", lambda _s: None)
    assert rec["index_0x50"]["0x11"] == {"before": "0x02", "after": "0x07", "restored": False}
    assert rec["index_0x50"]["0x13"]["before"] is None
    assert "NOT restored on 0x11" in rec["summary"] and "0x13" not in rec["summary"]
    assert json.loads(json.dumps(_events("cpld_dump")[0]))["index_0x50"]["0x11"]["restored"] is False


def test_the_deploy_pin_matches_the_module():
    pin = {}
    for line in open(os.path.join(os.path.dirname(__file__), "..", "deploy", "cpld-dump.pin")):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            pin[k] = v
    assert pin["TTDEV_CPLD_DUMP_SHA256"] == cpld_dump.PINNED_SHA256
    assert pin["TTDEV_CPLD_DUMP_PATH"] == cpld_dump.PINNED_SCRIPT


def test_psu_readings_saved_beside_the_dump(fake):
    rec = cpld_dump.take_dump("chip_dead", "ep-psu", lambda _m: None)
    assert rec["psu_path"].endswith("_psu.txt")
    raw = open(rec["psu_path"]).read()
    assert "CPU0_TEMP" in raw  # the whole listing is kept raw
    assert [l.split("|")[0].strip() for l in rec["psu"]] == ["PSU0_VIN", "PSU0_POUT", "P12V_UBB0"]
    assert "psu_error" not in rec
    assert _events("cpld_dump")[-1]["psu_path"] == rec["psu_path"]


def test_psu_read_failure_keeps_the_dump(fake, monkeypatch):
    monkeypatch.setenv("FAKE_SDR_FAIL", "1")
    rec = cpld_dump.take_dump("chip_dead", "ep-psu-fail", lambda _m: None)
    assert rec["summary"] == "3/620 failed"
    assert rec["psu_error"].startswith("rc 1") and "no BMC" in rec["psu_error"]
    assert rec["psu"] == []
