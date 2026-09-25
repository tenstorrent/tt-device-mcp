# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Broker logic against real silicon — validation level 2, spec 09 I8.

Every test here carries the `device` marker, so it skips unless this host has chips at
/dev/tenstorrent. `isolate_device_state` lifts its hardware seals for them (real sysfs, real
PCI, real /dev, real /proc identity, real probe resolution) while keeping the durable-state
seals on: no test here writes the running broker's journal or FSM record.

Two things this file may never do, enforced by conftest rather than by care: reboot or
power-cycle the host (`_fire_host_reboot`/`_fire_ubb_reset` stay AssertionError tripwires,
both rungs pinned off), and spawn anything from `_NEVER_SPAWN`. `tt-smi -r` is the ceiling.

Where a test would otherwise perturb someone else's work it is scoped rather than weakened:
the reclaim test injects its own `rescan` so real signals land only on the child it spawned,
and the reset test consults the broker's own reset gate first and skips when a tenant holds
the device — resetting over a tenant is the invariant under test (04 I6/I7), not a cost to pay.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import tt_device_mcp.device_holders as device_holders
import tt_device_mcp.server as srv
from tt_device_mcp.device_holders import (
    MIN_TENANT_UID,
    HolderScan,
    enumerate_device_holders,
    evaluate_reset_gate,
    reclaim_foreign_holders,
)
from tt_device_mcp.health.monitors import eth, fabric, heartbeat, hostpci

pytestmark = pytest.mark.device


# A child that opens a real device node and then waits, so a scan has something true to find.
# Its own session (start_new_session) matters: reclaim structurally excludes this process's
# process group, and a plain subprocess would inherit ours and be skipped as "self".
_HOLDER_SRC = (
    "import os,sys,time\n"
    "fd=os.open(sys.argv[1], os.O_RDWR)\n"
    "sys.stdout.write('open\\n'); sys.stdout.flush()\n"
    "time.sleep(300)\n"
)


def _first_chip_node() -> Path:
    indices = sorted(srv._present_chip_indices(), key=int)
    assert indices, "device marker gate admitted a host with no chips"
    return Path(srv.TT_DEV_DIR) / indices[0]


def _tenant_uid() -> int:
    """A uid to run a holder process as that the broker will treat as a real tenant.

    Never root: both the reset gate and reclaim exclude uids below MIN_TENANT_UID as
    infrastructure that survives a board reset, so a root-owned holder is invisible to exactly
    the code these tests exist to exercise — under plain `sudo pytest` they passed while
    proving nothing. Under sudo the invoking user's uid is borrowed from SUDO_UID.
    """
    if os.geteuid() != 0:
        return os.getuid()
    sudo_uid = os.environ.get("SUDO_UID", "")
    if sudo_uid.isdigit() and int(sudo_uid) >= MIN_TENANT_UID:
        return int(sudo_uid)
    pytest.skip("running as root with no tenant SUDO_UID to borrow; cannot make a real tenant holder")


def _only_our_child(pid: int):
    """A real holder scan, narrowed to one pid — the scoping seam the reclaim tests inject.

    Real /proc every call, so a holder that has since died genuinely drops out of the result;
    a lambda returning a fixed list would report a killed process as a survivor forever.
    """

    def rescan() -> HolderScan:
        scan = enumerate_device_holders()
        return HolderScan(holders=[h for h in scan.holders if h.pid == pid], complete=True)

    return rescan


@pytest.fixture
def device_holder_child():
    """A real, separately-sessioned, tenant-owned process holding an open fd to chip 0."""
    node = _first_chip_node()
    uid = _tenant_uid()
    # `user=` only when we are not already that uid: passing it needs privileges we do not have
    # in the unprivileged run, and there it would be a no-op anyway.
    as_tenant = {"user": uid} if uid != os.geteuid() else {}
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, str(node)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **as_tenant,
    )
    # Wait for the fd to be open before yielding: a scan that races the open sees nothing and
    # the test would report "no holder found" as a broker bug rather than as its own race.
    ready = proc.stdout.readline()
    if ready.strip() != "open":
        proc.kill()
        proc.wait(timeout=10)
        pytest.fail(f"holder child never opened {node}: {proc.stderr.read()[:400]}")
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


# --- topology and probe resolution, read-only ---------------------------------------------------


def test_the_chip_indices_are_the_real_device_nodes():
    """`_present_chip_indices` against real /dev — and `by-id`, a real subdirectory on a live
    host, must not be counted as a chip."""
    real = Path(srv.TT_DEV_DIR)
    assert real.is_dir() and str(real) == "/dev/tenstorrent"
    expected = sorted(p.name for p in real.iterdir() if p.name.isdigit())
    assert sorted(srv._present_chip_indices()) == expected
    assert expected, "the marker gate admitted a host with no chips"
    assert "by-id" not in srv._present_chip_indices()


def test_the_host_pci_probe_reads_the_real_bus():
    """The cheapest probe, against the real `/sys/bus/pci/devices` — spec 03 I9.

    It must find every chip by PCI vendor id rather than through /dev or /sys/class/tenstorrent,
    because a chip whose driver never bound has no entry under either — the exact fault it exists
    to name would make the chip look absent instead of unusable.
    """
    functions = hostpci.tt_functions()
    assert functions, "no Tenstorrent function found on a host the marker admitted"
    assert len(functions) >= len(srv._present_chip_indices()), (
        f"the bus shows fewer functions ({functions}) than /dev shows chips — /dev cannot exceed "
        "the bus, so one of the two reads is wrong"
    )

    ok, detail, evidence = hostpci.host_pci_verdict()

    assert ok is True, f"real hardware failed the host-PCI probe: {detail}"
    assert set(evidence["devices"]) == set(functions)
    for bdf, rec in evidence["devices"].items():
        assert rec["driver"] == "tenstorrent", f"{bdf} is bound to {rec['driver']}"
        assert rec.get("link_speed"), f"{bdf} reported no link speed"
    assert evidence["iommu"], "the IOMMU mode was not recorded"


def test_the_real_firmware_bundle_versions_are_readable():
    """The floors read sysfs, so they must actually resolve on a real host.

    Asserted as "every chip reports one, and they agree" rather than against a fixed version:
    which bundle this box runs is host state, but a chip that reports none — or a mesh running
    two — is a fact worth failing on.
    """
    versions = hostpci.fw_bundle_versions()

    assert versions, "no chip reported a firmware bundle version"
    assert set(versions) == set(srv._present_chip_indices()), (
        f"firmware versions and present chips disagree: {sorted(versions)} vs " f"{sorted(srv._present_chip_indices())}"
    )
    assert len(set(versions.values())) == 1, f"chips are running different firmware bundles: {versions}"


def test_the_heartbeat_probe_is_supported_on_real_sysfs():
    """Unsealed, /sys/class/tenstorrent is populated, so the probe is present rather than absent.

    The mirror of what I1 guarantees for every unmarked test: sealed, this is False and the
    probe is simply skipped.
    """
    assert heartbeat.heartbeat_supported(refresh=True) is True


def test_the_expected_chip_count_is_at_least_what_is_present():
    """`expected()` is a high-water mark, never the survivor count — a mesh that lost a tray
    must not silently lower its own bar."""
    present = len(srv._present_chip_indices())
    assert srv.health_monitor.expected(present) >= present


def test_the_real_fabric_validator_resolves_to_an_executable():
    """With TTDEV_VALIDATOR_ROOT unsealed, build_command finds the installed validator.

    Resolution only — the traffic pass itself is exercised through the gate below, where it
    runs under the same quiesce/lock discipline production gives it.
    """
    built = fabric.build_command()
    if built is None:
        pytest.skip("no fabric validator installed on this host")
    argv, env = built
    assert argv
    binary = argv[2] if argv[:2] == ["/bin/bash", "-c"] else argv[0]
    assert Path(binary.split()[0]).exists(), f"resolved a validator that is not there: {binary}"


def test_the_real_eth_heartbeat_probe_resolves_when_armed(monkeypatch):
    """The built-in probe resolves against the real install once its opt-in is set.

    Unarmed, `build()` returns None before resolving anything — that gate is the primary one
    and is proved device-free elsewhere; what needs real hardware is that the resolution
    behind it finds the probe and interpreter actually staged on a broker host.
    """
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "1")
    built = eth.build()
    if built is None:
        pytest.skip(f"no eth-heartbeat probe resolvable on this host: {eth.build_reason()}")
    argv, env = built
    assert argv
    assert Path(argv[0]).exists(), f"resolved an interpreter that is not there: {argv[0]}"


# --- holder scan and reclaim, against real /proc -------------------------------------------------


def test_the_holder_scan_finds_a_real_process_holding_the_device(device_holder_child):
    """The scan reads real /proc fd tables, not a fixture.

    `complete` is deliberately not asserted: as an unprivileged user the scan cannot read every
    process's fds, and reporting that blind spot honestly is the behavior (04 I7 fails closed
    on it). What must hold is that a holder it CAN see is found.
    """
    scan = enumerate_device_holders()
    mine = [h for h in scan.holders if h.pid == device_holder_child.pid]
    assert mine, f"scan missed pid {device_holder_child.pid} holding the device"
    assert mine[0].uid == _tenant_uid()
    assert mine[0].username == srv.username_for_uid(_tenant_uid())


def test_the_driver_holder_record_answers_completely_without_root(device_holder_child):
    """The real `/proc/driver/tenstorrent/<n>/pids`, unprivileged — spec 04 I6.

    This is the thing the fd walk cannot do. On this host the walk reports hundreds of processes
    unreadable, so `scan.complete` is False and the reset gate fails closed on a device that is
    genuinely free: it refuses for lack of privilege, not for anything about the device. The
    driver's own record is world-readable, so the same caller gets a complete answer.
    """
    scan = enumerate_device_holders()

    assert scan.source == "driver", f"the driver record did not answer: {scan.source}"
    assert scan.complete is True, "the driver record answered but not completely"
    assert any(
        h.pid == device_holder_child.pid for h in scan.holders
    ), f"the driver did not report pid {device_holder_child.pid} holding the device: {scan.holders}"
    assert next(h.uid for h in scan.holders if h.pid == device_holder_child.pid) == _tenant_uid()


def test_a_free_device_passes_the_reset_gate_without_root():
    """The consequence: the gate stops refusing a free device for want of CAP_DAC_READ_SEARCH.

    Skipped rather than failed when a real tenant is on the box — that refusal is the invariant
    working, not a regression — and the two reasons are reported apart so a permanent skip cannot
    read as a pass.
    """
    scan = enumerate_device_holders()
    if scan.source != "driver":
        pytest.skip("this tt-kmd does not publish a holder record; the walk is all there is")

    tenants = [h for h in scan.holders if h.uid >= MIN_TENANT_UID]
    if tenants:
        pytest.skip(f"a real tenant holds the device: {[(h.pid, h.username) for h in tenants]}")

    decision = evaluate_reset_gate(os.getuid(), scan)

    assert scan.complete is True
    assert decision.allowed is True, f"a free device was refused unprivileged: {decision.reason}"


def test_the_real_proc_starttime_identifies_a_live_process(device_holder_child):
    """Identity is pid + starttime, and the starttime half must come from real /proc.

    Sealed to a constant None for every unmarked test, so this is the only place the actual
    field-22 parse runs against a real /proc/<pid>/stat.
    """
    st = device_holders._read_proc_starttime(device_holder_child.pid)
    assert st is not None and st.isdigit()
    assert device_holders._read_proc_starttime(device_holder_child.pid) == st


def test_an_absent_pid_has_no_readable_identity():
    """The fail-safe half: no identity means never signal, and it must hold against real /proc."""
    absent = 2**22 - 1  # above every plausible pid_max, so it cannot be a live process
    assert not Path(f"/proc/{absent}").exists()
    assert device_holders._read_proc_starttime(absent) is None


def test_reclaim_really_signals_a_device_holder_it_targeted(device_holder_child):
    """Real os.kill, real /proc identity revalidation, real fd-table re-scan.

    The injected `rescan` still calls `enumerate_device_holders()` — so discovery and the
    post-signal survivor check both read real /proc — and only filters the result to this
    test's own child. That filter is the sole seam: an unscoped reclaim on a broker host
    would SIGKILL other people's live jobs, which is not a thing a test may decide to do.
    """
    res = reclaim_foreign_holders(grace_sec=1.0, rescan=_only_our_child(device_holder_child.pid))

    assert [h.pid for h in res.signalled] == [device_holder_child.pid]
    assert res.survivors == []
    assert device_holder_child.wait(timeout=30) != 0


def test_reclaim_leaves_a_process_it_cannot_identify_alone(device_holder_child):
    """An unreadable starttime means never signal — proved with a real, live, killable process.

    The seam that makes it survive is `read_starttime`, not the process: the child genuinely
    holds the device and genuinely could be killed, and the only reason it lives is the
    fail-safe. Its own uid is a real tenant uid, so the uid floor is not what spares it.
    """
    res = reclaim_foreign_holders(
        grace_sec=0.1,
        rescan=_only_our_child(device_holder_child.pid),
        read_starttime=lambda pid: None,
    )
    assert res.signalled == []
    assert device_holder_child.poll() is None, "a holder with no readable identity was signalled"


def test_the_reset_gate_refuses_over_a_real_holder(device_holder_child):
    """04 I6/I7 against a real scan: an anonymous caller owns no holder, so the live child is
    foreign and the reset is refused."""
    scan = enumerate_device_holders()
    decision = evaluate_reset_gate(None, scan)
    assert decision.allowed is False
    assert any(h.pid == device_holder_child.pid for h in decision.foreign_holders)


# --- the gate, driving real probes and a real reset ----------------------------------------------


def test_the_step_verdict_agrees_with_the_real_holder_scan():
    """pre-step's verdict against real hardware and real /proc.

    Asserted as agreement with an independently computed scan rather than as a fixed ok=True:
    whether this box is free right now is host state, but the verdict matching the same
    predicates the queue's own admission reads is the invariant (02 I11).
    """
    degraded = srv._device_degraded_for_tenant()
    scan = enumerate_device_holders()
    foreign = [h for h in scan.holders if h.uid >= srv.MIN_TENANT_UID]

    verdict = srv._slurm_step_verdict(require_free=True)

    assert verdict["ok"] is (not degraded and not foreign and scan.complete)
    assert {h["pid"] for h in verdict["holders"]} == {h.pid for h in foreign}
    if degraded:
        assert verdict["reason"]


async def test_the_read_only_gate_probes_real_hardware_and_never_resets():
    """with_recover=False is pre-step's whole contract, proved against real silicon.

    A dirty device is exactly the case where the acting gate WOULD reset, so this is the
    discriminating test rather than a no-op one: the probe runs on real chips, the verdict
    comes back, and the reset clock must not have moved.
    """
    srv._mark_device_dirty(None, why="probe_unhealthy")
    before = srv.recovery_mechanism.last_reset_monotonic

    await srv.device_health_gate(None, phase="pre-step", run_fabric=False, with_recover=False)

    assert srv.recovery_mechanism.last_reset_monotonic == before
    assert srv._device_degraded_for_tenant(), "a read-only gate cleared the dirty flag"


async def test_a_real_probe_pass_reports_a_verdict_for_every_present_chip():
    """One real `update()` pass: real heartbeat, real PCI, no traffic pass."""
    present = srv._present_chip_indices()
    state = await srv.health_monitor.update(phase="device-test", run_fabric=False)

    assert state.expected >= len(present)
    assert state.observations, "a real probe pass produced no observations"
    assert state.of("heartbeat") is not None or state.of("pci") is not None


async def test_the_warm_reset_rung_really_resets_and_reproves_the_mesh():
    """The ladder's warm rung end to end: a real `tt-smi -r`, then a real verify.

    Driven at `_reset_and_verify_device` rather than through the gate on purpose. The gate
    only resets when a probe returns UNHEALTHY, and a healthy device stays healthy however
    dirty the flag says it is — marking it dirty makes the gate *prove* the mesh, which is
    correct behavior and never reaches a reset. Manufacturing a real unhealthy verdict means
    wedging the device, which is worse than resetting it. So the decision half is left to the
    device-free tests that already cover it, and what runs here is the half that cannot be
    faked: the reset command reaching silicon and the mesh coming back.

    The one destructive test in the file, and the reason it is safe to have: the rungs above
    this one are pinned off and their fires are AssertionError tripwires, so an escalation
    fails this test loudly instead of rebooting the box.

    Two independent things must hold, guarded separately because they used to be conflated: the
    gate must *permit* the reset (policy), and this process must be able to *perform* it
    (capability — `tt-smi -r` and `/sys/bus/pci/rescan` both need root). While the holder scan
    could only be completed by root, one guard accidentally covered both; now that the driver's
    record completes unprivileged, the gate permits a reset this process cannot actually run.
    """
    if srv.galaxy_recovery is None:
        pytest.skip("no recovery object on this host — boot_broker did not construct one")
    if os.geteuid() != 0:
        pytest.skip("performing a reset needs root (tt-smi -r, /sys/bus/pci/rescan) — rerun under sudo")
    # The reset resolves tt-smi from PATH, and root's default PATH does not carry the broker's
    # venv. Without this the reset spawns, exits 1 on command-not-found, and reads as a device
    # that would not come back — see spec 09 level 2 for the invocation that matches the unit's.
    if shutil.which("tt-smi") is None:
        pytest.skip("tt-smi not on PATH; add the broker venv's bin as its unit does (spec 09, level 2)")
    scan = enumerate_device_holders()
    decision = evaluate_reset_gate(os.getuid(), scan)
    if not decision.allowed:
        pytest.skip(f"the reset gate refuses, which is the invariant working: {decision.reason}")

    indices = srv._present_chip_indices()
    before = srv.recovery_mechanism.last_reset_monotonic
    lines: list[str] = []

    # The lock and the quiesce are the reset's own safety contract, not test ceremony: it must
    # land on a bus with no poller on it and nothing able to restart the broker mid-command.
    async with srv._device_op("device-test reset", owner="[pytest]"):
        recovered = await srv.galaxy_recovery._reset_and_verify_device(indices, lines.append)

    assert srv.recovery_mechanism.last_reset_monotonic > before, f"no reset ran: {lines}"
    assert recovered, f"the mesh did not verify healthy after a real reset: {lines}"
    assert sorted(srv._present_chip_indices()) == sorted(indices), "chips did not re-enumerate"


async def test_the_gate_runs_the_real_fabric_traffic_pass(tmp_path):
    """The traffic pass on a live mesh, under the gate's own quiesce and lock discipline.

    `with_recover=True` is required, not incidental: the gate's `full` term is conjoined with
    it, so the read-only arm never reaches a traffic pass however hard `force_fabric` asks —
    the fabric pass must not run on a submitter's clock (03 I12). On a device that probes
    healthy the recover arm resets nothing, so this stays a probe-only test in practice.

    Slow by nature: it compiles the validator and then pushes traffic across every link.
    """
    built = fabric.build_command()
    if built is None:
        pytest.skip("no fabric validator installed on this host")
    # The validator writes into its own install tree (`generated/`), so an unprivileged run gets
    # rc=-6 and "no verdict" — which the gate correctly reports as fabric-unverified rather than
    # unhealthy. Guarded on the tree actually being writable, not on being root, so a deployment
    # that group-writes it still runs this: the point is the traffic pass, not the uid.
    metal_home = built[1].get("TT_METAL_HOME", "")
    if not metal_home or not os.access(metal_home, os.W_OK):
        pytest.skip(f"validator tree not writable, so the pass cannot reach a verdict: {metal_home}")

    # `last_fabric_check_monotonic` rather than `health_monitor.status()`: the gate probes
    # through `fsm.observe`, which does not necessarily refresh the monitor singleton, so a
    # stale reading left by an earlier test in this file reads exactly like a fresh pass. This
    # clock is reset per test by conftest and advanced only when the pass reached a verdict.
    log_file = tmp_path / "gate.log"
    assert srv.last_fabric_check_monotonic == 0.0, "conftest did not reset the fabric clock"

    await srv.device_health_gate(log_file, phase="post-step", run_fabric=True, force_fabric=True, with_recover=True)

    written = log_file.read_text() if log_file.exists() else "(no gate log)"
    assert srv.last_fabric_check_monotonic > 0.0, f"the traffic pass reached no verdict:\n{written}"
    assert "fabric" in written.lower(), f"the gate log never mentions the fabric pass:\n{written}"
