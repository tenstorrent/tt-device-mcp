# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I18: the vendor CPLD dump comes before recovery, once per fault episode.

A fake DIAG script and a fake ipmitool stand in for the vendor's: the fake ipmitool keeps each
UBB CPLD's debug index in a file, and the fake DIAG moves that index the way the vendor's does,
so the save/restore is checked against a register that really changed."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import shutil
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
if os.environ.get("FAKE_IPMI_HANG"):
    open(os.path.join(os.environ["FAKE_DIAG_PIDS"], "ipmi"), "w").write(str(os.getpid()))
    import time; time.sleep(60)
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
pids = os.environ.get("FAKE_DIAG_PIDS")
if pids and op == "0x00" and os.path.exists(os.path.join(pids, "diag")):
    # A restore: is the DIAG that moved the index still running?
    diag = open(os.path.join(pids, "diag")).read().strip()
    try:
        alive = open("/proc/%s/stat" % diag).read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        alive = False
    open(os.path.join(pids, "restores"), "a").write("%s alive=%s\\n" % (bus, alive))
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


# --- The bound holds on the root side (review #249, finding 2) -------------------------------------

# Like sudo: the command runs as root, out of the broker's reach. Here it runs in its own session,
# so the broker's killpg of sudo's group misses it, as EPERM does on a real host.
FAKE_SUDO = """#!{py}
import subprocess, sys
a = sys.argv[1:]
if a[:1] == ["-n"]:
    a = a[1:]
rc = subprocess.Popen(a, start_new_session=True).wait()
sys.exit(rc if rc >= 0 else 128 - rc)
"""

# A DIAG that hangs silently with a child of its own, as one stuck on a dead BMC would.
HUNG_DIAG = """#!/bin/bash
echo $$ > "$FAKE_DIAG_PIDS/diag"
sleep 300 &
echo $! > "$FAKE_DIAG_PIDS/child"
echo "This Script: V1.03.01"
wait
"""

# A DIAG that keeps writing for a while, then ends.
CHATTY_DIAG = """#!/bin/bash
echo $$ > "$FAKE_DIAG_PIDS/diag"
for i in $(seq 1 30); do echo "line $i"; sleep 0.05; done
"""


def _alive(pid: int) -> bool:
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _pid(fake, name) -> int:
    return int((fake["pids"] / name).read_text())


@pytest.fixture
def as_user(fake, monkeypatch):
    """The broker as a normal user: privileged commands go through a fake sudo."""
    if not os.path.isdir("/proc") or shutil.which("timeout") is None:
        pytest.skip("needs Linux /proc and coreutils timeout")
    sudo = fake["tmp"] / "fake-sudo"
    sudo.write_text(FAKE_SUDO.format(py=sys.executable))
    sudo.chmod(0o755)
    pids = fake["tmp"] / "pids"
    pids.mkdir()
    monkeypatch.setenv("FAKE_DIAG_PIDS", str(pids))
    monkeypatch.setattr(cpld_dump, "SUDO", (str(sudo),))
    monkeypatch.setattr(cpld_dump.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cpld_dump, "KILL_GRACE_SEC", 1.0)
    fake["pids"] = pids
    return fake


def _use_diag(fake, monkeypatch, body):
    diag = fake["tmp"] / "diag-variant.sh"
    diag.write_text(body)
    monkeypatch.setenv("TT_DEVICE_MCP_CPLD_DUMP_SCRIPT", str(diag))


def test_privileged_commands_carry_a_root_side_kill(fake, monkeypatch):
    monkeypatch.setattr(cpld_dump, "SUDO", ("sudo", "-n"))
    monkeypatch.setattr(cpld_dump.os, "geteuid", lambda: 1000)
    assert cpld_dump._privileged(["bash", "/s.sh", "DIAG"], 40.0) == [
        "sudo", "-n", "timeout", "-s", "KILL", "40", "bash", "/s.sh", "DIAG"
    ]
    assert cpld_dump._privileged(["ipmitool", "raw"], 5.0)[:6] == ["sudo", "-n", "timeout", "-s", "KILL", "5"]
    monkeypatch.setattr(cpld_dump.os, "geteuid", lambda: 0)
    assert cpld_dump._privileged(["bash", "/s.sh", "DIAG"], 0.5) == ["timeout", "-s", "KILL", "1", "bash", "/s.sh", "DIAG"]


def test_a_hung_diag_behind_sudo_is_gone_when_the_dump_returns(as_user, monkeypatch):
    _use_diag(as_user, monkeypatch, HUNG_DIAG)
    monkeypatch.setenv("TT_DEVICE_MCP_CPLD_DUMP_CAP_SEC", "1")
    t0 = time.monotonic()
    rec = cpld_dump.take_dump("off_bus", "ep-hung", lambda _s: None)
    assert time.monotonic() - t0 < 8
    diag, child = _pid(as_user, "diag"), _pid(as_user, "child")
    assert not _alive(diag) and not _alive(child), "the root-side DIAG outlived the cap"
    assert rec["rc"] is None and rec["summary"].startswith("timed out")
    restores = (as_user["pids"] / "restores").read_text().splitlines()
    assert len(restores) == 4 and all(r.endswith("alive=False") for r in restores), restores
    assert all(v["restored"] for v in rec["index_0x50"].values())


def test_a_hung_ipmitool_behind_sudo_is_killed_at_its_bound(as_user, monkeypatch):
    monkeypatch.setenv("FAKE_IPMI_HANG", "1")
    monkeypatch.setattr(cpld_dump, "IPMI_CALL_SEC", 1.0)
    t0 = time.monotonic()
    assert cpld_dump._read_index("0x0D", "0x80") is None
    assert time.monotonic() - t0 < 5
    assert not _alive(_pid(as_user, "ipmi")), "the root-side ipmitool outlived its bound"


def test_a_slow_psu_read_behind_sudo_is_killed_at_its_bound(as_user, monkeypatch):
    monkeypatch.setenv("FAKE_IPMI_HANG", "1")
    monkeypatch.setattr(cpld_dump, "PSU_SDR_SEC", 1.0)
    t0 = time.monotonic()
    rec = cpld_dump._read_psu(as_user["tmp"] / "psu.txt")
    assert time.monotonic() - t0 < 5 and rec.get("psu_error")
    assert not _alive(_pid(as_user, "ipmi")), "the root-side sensor read outlived its bound"


# --- An error mid-DIAG still ends it and restores the index (review #249, finding 3) ----------------


class _FullDisk:
    def __init__(self, f):
        self._f = f

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()

    def fileno(self):
        return self._f.fileno()

    def flush(self):
        pass

    def write(self, _b):
        raise OSError(errno.ENOSPC, "No space left on device")


def test_a_failed_output_write_ends_the_diag_and_restores_the_index(as_user, monkeypatch):
    _use_diag(as_user, monkeypatch, CHATTY_DIAG)
    real_open = open
    monkeypatch.setattr(
        cpld_dump,
        "open",
        lambda p, m="r", *a, **k: _FullDisk(real_open(p, m)) if m == "wb" else real_open(p, m, *a, **k),
        raising=False,
    )
    rec = cpld_dump.take_dump("chip_dead", "ep-enospc", lambda _s: None)
    assert not _alive(_pid(as_user, "diag"))
    restores = (as_user["pids"] / "restores").read_text().splitlines()
    assert len(restores) == 4 and all(r.endswith("alive=False") for r in restores), restores
    assert all(v["restored"] for v in rec["index_0x50"].values())
    (ev,) = _events("cpld_dump")
    assert "ENOSPC" in ev["error"] or "No space" in ev["error"]
    assert ev["rc"] is None and ev["summary"].startswith("DIAG aborted")
    assert _events("cpld_dump_skipped") == []


def test_a_failed_pipe_read_ends_the_diag_and_restores_the_index(as_user, monkeypatch):
    _use_diag(as_user, monkeypatch, CHATTY_DIAG)

    class _BrokenSelect:
        @staticmethod
        def select(*_a):
            raise OSError(errno.EBADF, "select failed")

    monkeypatch.setattr(cpld_dump, "select", _BrokenSelect)
    rec = cpld_dump.take_dump("chip_dead", "ep-select", lambda _s: None)
    assert not _alive(_pid(as_user, "diag")), "a DIAG whose pipe is closed dies of SIGPIPE"
    assert all(v["restored"] for v in rec["index_0x50"].values())
    assert "select failed" in rec["error"]


def test_an_error_before_the_diag_restores_only_what_was_read(fake, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("mkdir exploded")

    monkeypatch.setattr(cpld_dump, "_note_dump", boom)
    writes = []
    real = cpld_dump._restore_index
    monkeypatch.setattr(cpld_dump, "_restore_index", lambda b, a, v: writes.append(b) or real(b, a, v))
    rec = cpld_dump.take_dump("t", "ep-early", lambda _s: None)
    assert "mkdir exploded" in rec["skipped"]
    assert writes == ["0x0D", "0x0F", "0x11", "0x13"]
    assert all(v["restored"] for v in rec["index_0x50"].values())
    fd = os.open(fake["lock"], os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the lock was released
    os.close(fd)


# --- The dump and the episode's incidents are linked both ways (review #249, finding 1) -----------


@pytest.fixture
def incidents(monkeypatch):
    monkeypatch.setattr(cpld_dump, "current_episode", lambda: "")
    monkeypatch.setattr(health, "_kernel_evidence", lambda *a, **k: "(kernel)")
    monkeypatch.setattr(health.pci, "chip_snapshot", lambda: [])
    monkeypatch.setattr(health, "_prune_incidents", lambda: None)


def _link(folder):
    return json.loads((folder / cpld_dump.LINK_FILE).read_text())


@pytest.mark.asyncio
async def test_a_chip_dead_incident_and_its_dump_are_linked(fake, incidents, tmp_path, health_deps):
    fsm = ServerFsm(tmp_path / "fsm.json")
    fsm.boot(health_deps)
    fsm.on_fault("heartbeat", detail="chip 3 fell off the bus")
    task = fsm.mechanism._cpld_task
    incident = await asyncio.to_thread(health.capture_incident, "chip_dead", trace=[])
    await task
    (ev,) = _events("cpld_dump")
    assert ev["incidents"] == [str(incident)]
    assert _link(incident)["path"] == ev["path"] and _link(incident)["episode"] == ev["episode"]


@pytest.mark.asyncio
async def test_an_incident_after_the_dump_links_to_it(fake, incidents, tmp_path, health_deps):
    fsm = ServerFsm(tmp_path / "fsm.json")
    fsm.boot(health_deps)
    fsm.on_fault("off_bus", detail="tray 3 dropped")
    await fsm.mechanism._cpld_task
    (ev,) = _events("cpld_dump")
    incident = health.capture_incident("unhealthy", job={"id": "j7"})
    assert _link(incident)["path"] == ev["path"] and _link(incident)["psu_path"] == ev["psu_path"]
    (linked,) = _events("cpld_dump_linked")
    assert linked["incident"] == str(incident) and linked["path"] == ev["path"]


def test_an_incident_just_before_the_episode_opened_is_linked(fake, incidents):
    incident = health.capture_incident("unhealthy")
    episode = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 30))
    rec = cpld_dump.take_dump("probe_unhealthy", episode, lambda _s: None)
    assert rec["incidents"] == [str(incident)] and _link(incident)["path"] == rec["path"]


def test_no_link_outside_the_episode(fake, incidents, monkeypatch):
    old = health.health_dir() / health.INCIDENTS_DIR / "20200101T000000Z_unhealthy_none"
    old.mkdir(parents=True)
    rec = cpld_dump.take_dump("off_bus", "2026-11-10T10:00:00Z", lambda _s: None)
    assert rec["incidents"] == [] and not (old / cpld_dump.LINK_FILE).exists()
    # No open episode: a later incident is not linked to the last dump.
    monkeypatch.setattr(cpld_dump, "current_episode", lambda: "")
    assert not (health.capture_incident("dirty") / cpld_dump.LINK_FILE).exists()
    # Another episode open: not linked either.
    monkeypatch.setattr(cpld_dump, "current_episode", lambda: "2026-11-10T11:00:00Z")
    assert not (health.capture_incident("dirty") / cpld_dump.LINK_FILE).exists()
    assert _events("cpld_dump_linked") == []
