# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures for tt-device-mcp tests."""

import asyncio
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Add src directory to path for tests
src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

import tt_device_mcp.device_holders as device_holders
import tt_device_mcp.server as srv
from tt_device_mcp import privileges
from tt_device_mcp.fsm import ServerFsm
from tt_device_mcp.health import evidence as health
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.core import HealthState
from tt_device_mcp.health.monitors import eth, heartbeat, pci
from tt_device_mcp.health.recovery import RecoveryDeps
from tt_device_mcp.health.recovery import galaxy as recovery_galaxy
from tt_device_mcp.server import (
    _ensure_async_primitives,
    _reset_for_testing,
    get_job_queue,
    jobs,
)

# The broker's boot flow, construction only. It moved off import so that importing the module can
# never touch the device (resolving the platform means a tt-smi snapshot); the suite still needs
# the subsystem it builds, because srv.health_monitor / srv.recovery_mechanism / the two platform
# aliases are the seams almost every test monkeypatches. probe_platform stays off: a test that
# cares about the platform declares TT_DEVICE_MCP_RESET_MODE or drives resolve_platform itself.
srv.boot_broker()


@pytest.fixture(autouse=True)
def isolate_process_globals():
    """Restore os.environ and sys.argv around every test.

    Several commands write these themselves — `cmd_daemon_start` assigns
    os.environ["TT_DEVICE_MCP_SOCKET"] directly and `start-fg` reassigns sys.argv — and monkeypatch
    cannot undo a write to a name it did not set. Leaked, they steer later tests: a socket path in
    a deleted tmp dir, or an argv the next parser reads. Closed here rather than per test, because
    the leak is a property of the code under test, not of any one caller.
    """
    env, argv = dict(os.environ), list(sys.argv)
    yield
    os.environ.clear()
    os.environ.update(env)
    sys.argv[:] = argv


def pytest_addoption(parser):
    parser.addoption(
        "--no-device-tests",
        action="store_true",
        default=False,
        help="skip every @pytest.mark.device test even on a host that has /dev/tenstorrent",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "device: needs real Tenstorrent hardware; skipped without /dev/tenstorrent (spec 09 I8)",
    )


# The real /dev/tenstorrent, hardcoded here rather than read from srv.TT_DEV_DIR, because the
# fixture below sandboxes that module attribute out from under every unmarked test — asking the
# module would make the marker's own gate answer "no device" on a box that has one.
_REAL_TT_DEV_DIR = "/dev/tenstorrent"


def _real_device_present() -> bool:
    """Whether this host has chips a device-marked test could actually run against.

    Numeric entries only, matching `_present_chip_indices()`. A live host's /dev/tenstorrent
    also holds `by-id`, a directory — counting it would unseal and run every device-marked test
    on a box whose chips are all off the bus, where the broker itself sees none.
    """
    try:
        return any(e.name.isdigit() for e in os.scandir(_REAL_TT_DEV_DIR))
    except OSError:
        return False


@pytest.fixture(autouse=True)
def device_marked(request) -> bool:
    """Whether this test declared `@pytest.mark.device` — and may therefore reach real silicon.

    Autouse and requested by name from `isolate_device_state`, so the skip decision is made
    before any seal is lifted: a marked test on a device-free host must skip, not run unsealed.
    """
    if request.node.get_closest_marker("device") is None:
        return False
    if request.config.getoption("--no-device-tests"):
        pytest.skip("device tests disabled by --no-device-tests")
    if not _real_device_present():
        pytest.skip(f"no chips at {_REAL_TT_DEV_DIR} on this host")
    return True


@pytest.fixture(scope="session")
def device_probe_cache_root(tmp_path_factory):
    """One tt-metal cache dir for every device test in the run — see _redirect_probe_caches."""
    return tmp_path_factory.mktemp("device-probe-cache")


def _seal_real_hardware(monkeypatch, tmp_path_factory):
    """Hide the host's real hardware from a test that did not declare it needs hardware.

    Every patch here answers the same question — can this test see or reach real silicon —
    so they lift together under the `device` marker and nowhere else. Split out from the
    fixture body for exactly that reason: the seals that remain there are about durable
    broker state and the destructive rungs, which no marker ever lifts.
    """
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", tmp_path_factory.mktemp("sysfs"))
    # A host running this suite may have the real pinned fabric validator installed at the
    # default TTDEV_VALIDATOR_ROOT (see health.monitors.fabric.build_command) — point it at an
    # empty dir so an unset TT_DEVICE_MCP_FABRIC_CHECK_CMD reliably resolves to "no built-in
    # validator either", never to a real binary that would push traffic across a live mesh.
    # TTDEV_FABRIC_BIN/_RUNTIME_ROOT/_DESCRIPTOR each override VROOT-derived resolution outright,
    # so clearing VROOT alone is not a real sandbox while any of them could be ambient (a leaked
    # setenv from another test, a host-level env file) — clear all three, and the override
    # command itself, so every test starts from the same "nothing configured or installed" floor.
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(tmp_path_factory.mktemp("no-fabric-validator")))
    monkeypatch.delenv("TTDEV_FABRIC_BIN", raising=False)
    monkeypatch.delenv("TTDEV_FABRIC_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("TTDEV_FABRIC_DESCRIPTOR", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
    # Same sandboxing for the eth-heartbeat probe (health.monitors.eth) — a host running this
    # suite may have a REAL tt-metal python_env somewhere that can `import ttexalens` (this box
    # does, at a dev checkout entirely outside the broker's own candidate paths); the point isn't
    # that today's candidates happen to miss it, it's that no test may ever spawn the real probe
    # or touch a device. TTDEV_VALIDATOR_ROOT is already sandboxed above, which also clears the
    # validator's own python_env candidate; TT_DEVICE_MCP_ETH_HEARTBEAT_CMD, TTDEV_ETH_CHECK_PYTHON,
    # TTDEV_ETH_VENV and TTDEV_ETH_CHECK_PROBE are the other ways a real, working command could
    # reach eth.build(), so every one of them starts unset. The self-block (TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN) is the
    # primary gate — unset, eth.build() returns None before ever resolving a python — but it is
    # cleared too, both for a clean default and so a test that DOES set it starts from a known
    # floor. The last candidate, DEFAULT_ETH_VENV_PYTHON, is a hardcoded path with no env override,
    # so it is patched to a path that cannot exist rather than trusted to stay absent on every box
    # this suite ever runs on.
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_PYTHON", raising=False)
    monkeypatch.delenv("TTDEV_ETH_VENV", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_PROBE", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_TIMEOUT", raising=False)
    # BOTH arming spellings start unset, not just the legacy one: a host whose broker self-test
    # armed the rung exports TTDEV_ETH_CHECK_ARMED=1 into every child — including this suite —
    # and a leaked armed flag flips every disarmed-skip test into the armed path.
    monkeypatch.delenv("TTDEV_ETH_CHECK_ARMED", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", raising=False)
    monkeypatch.setattr(eth, "DEFAULT_ETH_VENV_PYTHON", str(tmp_path_factory.mktemp("no-eth-venv") / "bin" / "python"))
    # eth._probe_path()'s two remaining candidates, same rationale: TTDEV_ROOT defaults to
    # /opt/tt-device-broker, and this box's real broker install genuinely has a real
    # eth-heartbeat-probe.py staged there — sandboxed to an empty dir so that candidate misses.
    # eth._REPO_ROOT defaults to THIS repo's own checkout (an editable install), which genuinely
    # has deploy/tt-device-eth-heartbeat-probe.py too — patched to an empty dir for the same
    # reason DEFAULT_ETH_VENV_PYTHON is: a test must never resolve a real, executable probe path
    # by construction, not by this box happening not to have one where a candidate looks.
    monkeypatch.setenv("TTDEV_ROOT", str(tmp_path_factory.mktemp("no-eth-install-root")))
    monkeypatch.setattr(eth, "_REPO_ROOT", tmp_path_factory.mktemp("no-eth-repo-checkout"))
    # PCI_DEVICES_DIR defaults to the REAL /sys/bus/pci/devices, read at import time (so an env
    # var set here would arrive too late) — every test that reaches chip_node_present() without
    # patching this itself would otherwise read this box's own live PCI topology. A default here
    # does not disturb the ~14 tests that already patch it to their own scenario-specific dir:
    # their own monkeypatch.setattr runs later, in the test body, and simply overrides this one.
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_path_factory.mktemp("no-pci-devices"))
    # TT_DEV_DIR defaults to the REAL /dev/tenstorrent and is read fresh by _present_chip_indices()
    # on every call — on a build host with actual hardware (unlike CI, which has none) a test that
    # never sets this itself silently probes the real device count instead of the scenario it
    # declares, and its assertions then depend on which machine happens to run them. Sandboxed to
    # an empty dir here for the same reason PCI_DEVICES_DIR is above; the ~40+ tests that already
    # patch it to their own scenario-specific dir are unaffected — their own monkeypatch.setattr
    # runs later, in the test body, and simply overrides this default.
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path_factory.mktemp("no-dev-tenstorrent")))
    # reclaim_foreign_holders' default identity check reads the REAL /proc/<pid>/stat for
    # whatever pid a test's fake HolderScan names — a fake pid that happens to exist as a real
    # process on the host running the suite (kernel threads like 100/200 are common, and 4242 has
    # shown up too) makes a test that forgot to inject its own `read_starttime` signal for a
    # reason unrelated to what it asserts, exactly like TT_DEV_DIR above. Pinned to a function
    # that returns None — "unreadable identity" is reclaim's own fail-safe meaning "never signal
    # this pid" — rather than a fixed non-None value, so a test that omits the seam doesn't
    # silently keep working with a deterministic-but-wrong identity: it gets an empty `signalled`
    # list and fails its own assertions immediately, on every host, the same way every time. A
    # test that means to exercise real signalling injects `read_starttime` itself, the same as
    # `kill`/`sleep`/`rescan`.
    monkeypatch.setattr(device_holders, "_read_proc_starttime", lambda pid: None)
    # tt-kmd's holder record defaults to the REAL /proc/driver/tenstorrent, which exists on a
    # broker host and not on CI — unsandboxed, enumerate_device_holders() would take the driver
    # branch on one and the fd-walk branch on the other, so a test's verdict would depend on which
    # machine ran it. Pointed at a path that cannot exist, which makes _driver_holder_pids decline
    # and every unmarked test exercise the walk, exactly as before this route existed. A test about
    # the driver route stages its own directory and points this at it.
    monkeypatch.setattr(device_holders, "DRIVER_PROC_DIR", str(tmp_path_factory.mktemp("no-tt-driver-proc") / "absent"))
    # The reset argv/mode env vars are read live (never cached), so an operator's own shell/CI
    # environment leaks straight into whatever argv a test builds — cleared here for a
    # deterministic floor; a test exercising a declared mode/override sets it itself afterward.
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_ARGS", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_RESET_MODE", raising=False)
    # Read live too: a broker host declares its chip count here (this box says 8), which would
    # override every test's own /dev-enumeration fixture and drag multi-chip requirements into
    # tests that construct single-chip hosts.
    monkeypatch.delenv("TT_DEVICE_MCP_EXPECTED_CHIPS", raising=False)

    # The cold rung needs ipmitool AND an openable /dev/ipmi*, so without this the rung's state
    # would depend on what the machine running the suite happens to have — green on a box with a
    # BMC, red on CI without one. Pinned present: the default-armed shape the ladder tests assert.
    # A test about a host WITHOUT it latches its own record.
    # tt-smi is a device tool, so an unmarked test must not see one: whether it is on PATH is a
    # property of the machine running the suite, and preflight verdicts keyed on it answered one
    # way on a developer box and another in CI. Sealed absent here and lifted under the marker,
    # like every other hardware seal. A test that needs it present patches `srv.shutil.which`.
    _host_which = shutil.which

    def _sealed_which(name, *args, **kwargs):
        return None if name == "tt-smi" else _host_which(name, *args, **kwargs)

    monkeypatch.setattr(srv.shutil, "which", _sealed_which)
    # The privilege half of the ladder's arming (spec 04 I17). Unpinned, every rung's state would
    # follow whether the suite happened to run as root on a host with setpci and a reachable BMC.
    # Pinned by latching a fully privileged record rather than by patching the accessors, so the
    # derived capabilities and the boot line both read it and cannot describe different hosts. A
    # test about an unprivileged daemon latches its own.
    monkeypatch.setattr(privileges, "_LATCHED", dict.fromkeys(privileges._PROBES, True))
    # The dead-chip sampler asks systemd whether a reset scope is live, and returns early
    # when one is — isolating chips out of a running reset is what leaves a box short a
    # tray. Unstubbed, that question reaches the real host: the sampler tests then pass or
    # fail on whether this machine happened to be resetting a device while they ran, which
    # is how three of them failed on a developer box mid-incident and nowhere else. A test
    # that means to exercise the guard patches this itself.
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: False)


def _redirect_probe_caches(monkeypatch, tmp_path_factory, device_marked, session_root):
    """Keep the fabric and eth probes off the real /var/cache, marker or not.

    Both default to a hardcoded real /var/cache/tt-device-broker path and mkdir it, so this is
    a redirect rather than a delenv. A device test gets one session-scoped dir instead of a
    fresh per-test one: these hold a compiled tt-metal cache, and a per-test dir would make
    every device test pay the build again.
    """
    if device_marked:
        fabric, eth_cache = session_root / "fabric", session_root / "eth"
    else:
        fabric = tmp_path_factory.mktemp("no-fabric-cache")
        eth_cache = tmp_path_factory.mktemp("no-eth-cache")
    monkeypatch.setenv("TTDEV_FABRIC_CACHE", str(fabric))
    monkeypatch.setenv("TTDEV_ETH_CHECK_CACHE", str(eth_cache))


@pytest.fixture(autouse=True)
def isolate_device_state(monkeypatch, tmp_path_factory, device_marked, device_probe_cache_root):
    """Point the health probe and journal at empty temp dirs for every test.

    Without this the suite reads the host's real /sys/class/tenstorrent, so on a
    Galaxy the tests see 32 live chips while their fixtures declare 2, the chip
    counts disagree, and the gate correctly calls it unhealthy — a pass/fail that
    depends on which machine CI lands on. An empty sysfs also means
    heartbeat_supported() is False, so the probe is simply absent unless a test
    populates the dir itself.
    """
    # Everything that exists only to hide the host's real hardware (spec 09 I8): lifted for a
    # device-marked test, which needs the silicon it declared. The durable-state and
    # destructive-rung seals below are NOT part of this and run either way.
    if not device_marked:
        _seal_real_hardware(monkeypatch, tmp_path_factory)
    _redirect_probe_caches(monkeypatch, tmp_path_factory, device_marked, device_probe_cache_root)
    health_tmp = tmp_path_factory.mktemp("health")
    monkeypatch.setattr(health, "HEALTH_DIR", health_tmp)
    monkeypatch.setattr(heartbeat, "_heartbeat_supported", None)
    # raising=False so a base tree without the re-arm latch (a fails-on-base stash) still sets up.
    monkeypatch.setattr(heartbeat, "_heartbeat_absent_journaled", False, raising=False)
    # eth.resolve_python() caches its answer per process; every test starts with it empty, so
    # one test's resolved stub python never answers for the next one's.
    monkeypatch.setattr(eth, "_python_cache", {}, raising=False)
    # Prometheus's textfile dir defaults to the REAL /var/lib/prometheus/node-exporter and is read
    # fresh on every write_textfile() call (never cached), so any path through the stats
    # persistence loop that reaches it in a test would mkdir a real host path.
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(tmp_path_factory.mktemp("no-textfile-dir")))
    # The gate's history lives in module globals — whether a reset just failed, when the
    # fabric was last proved. A test that leaves those set silently changes what the NEXT
    # test's gate decides to do, which is a debugging session nobody wants.
    # A fresh FSM per test, pointed at this test's own empty dir: srv.fsm is a module singleton
    # that loads and persists a durable record, so without this every test would share the one
    # instance (and the one on-disk file) the module created at import time. Reconciled straight
    # to HEALTHY, matching production's own non-privsep startup path (on_readings, not
    # boot_merge — there is no reboot/re-adoption concern here for boot_merge to reconcile) — a
    # test that wants a degraded device says so itself (srv.fsm.on_fault(...) etc.); nothing here
    # defaults to BOOT, which no caller outside boot_merge may ever observe.
    fresh_fsm = ServerFsm(health_tmp / "fsm.json")
    fresh_fsm.on_readings(HealthState(phase="test", at=datetime.now(), expected=0))
    monkeypatch.setattr(srv, "fsm", fresh_fsm)

    # No board types seen yet: the conservative default, and the machine type reads as unknown.
    # Nothing here can shell out — the cache is only ever filled by a health snapshot — so the
    # reset mode cannot depend on which machine CI lands on. Tests that want a machine type set
    # `_board_types` and `_glx_board_types` themselves. Both now live on the health_monitor
    # singleton, not on the server module itself (see health/monitor.py).
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_glx_cache", None)

    monkeypatch.setattr(srv, "device_fault_reported", "", raising=False)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", 0.0)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_failed", False)
    # Whether the last reset EXITED non-zero. Left set it would steer the NEXT test's gate
    # escalation to the cold rung over a reset that never hard-failed. raising=False so a base tree
    # without the flag (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_exit_nonzero", False, raising=False)
    monkeypatch.setattr(srv, "last_fabric_check_monotonic", 0.0)
    # When the last job's device work ended, the anchor the inter-job cooldown measures from.
    # Left set, a prior test's value would make the next runner test wait (or not) unexpectedly.
    # raising=False so a base tree without the cooldown (a fails-on-base stash) still sets up.
    monkeypatch.setattr(srv, "last_job_end_monotonic", 0.0, raising=False)
    # The per-owner burst-cap ledger. Left populated, one test's submissions would count against
    # the next test's cap. Fresh dict so state never bleeds; raising=False so a base tree without
    # the cap (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv, "owner_submit_times", {}, raising=False)
    # The idle all-gone debouncer: left at 1 from a prior test, the next sampler test would
    # confirm a sysfs blackout on its FIRST empty sample instead of its second. raising=False so
    # a base tree without the counter (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv.sampler, "all_chips_gone_strikes", 0)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    monkeypatch.setattr(srv, "_fabric_ok_retire_monotonic", 0.0)
    monkeypatch.setattr(srv, "_fabric_ok_retire_streak", 0)
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0.0)  # no real eth-training wait in tests
    monkeypatch.setattr(
        recovery_galaxy, "_settle_before_host_rung_sec", lambda: 0
    )  # no real pre-host-rung settle in tests
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_op_detail", "")
    monkeypatch.setattr(srv, "device_op_owner", "[broker]", raising=False)
    # The device-op mutex is created once and binds to the creating test's event loop; a later
    # test on its own loop acquiring the stale lock raises "bound to a different event loop".
    # Fresh per test, so anything running under _device_op (the startup probe included) is safe.
    monkeypatch.setattr(srv, "device_op_lock", None)
    # Same rationale, same fix, for the external-step reservation: a leaked "held" flag or an
    # asyncio.Event bound to a prior test's event loop would either wedge job_runner in the next
    # test (awaiting a free event nothing ever sets) or raise the same cross-loop error.
    # raising=False so a base tree without the reservation (a fails-on-base stash) still sets up.
    monkeypatch.setattr(srv, "external_step_active", "", raising=False)
    monkeypatch.setattr(srv, "external_step_free_event", None, raising=False)
    # The refcounted holder map/token counter backing external_step_active — a leaked holder
    # entry from a prior test (its own reservation never released, e.g. an assertion failure
    # before the gate task ever ran) would otherwise make the NEXT test's device look reserved
    # by a step that finished a full test ago. raising=False so a base tree without them (a
    # fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv, "_external_step_holders", {}, raising=False)
    monkeypatch.setattr(srv, "_external_step_token_seq", 0, raising=False)
    # A test that leaves a gate task running (one exercising the no-longer-cancelling deadline)
    # must not leak it into the next test's set — that set is also what a test inspects to find
    # "the" gate task, so a stale entry from a prior test would make that lookup ambiguous.
    monkeypatch.setattr(srv, "_step_background_tasks", set(), raising=False)
    # The tenant-gate hold latch persists across jobs by design; reset it so one runner
    # test tripping it does not silence the next test's expected held event. raising=False
    # so a tree without the attribute yet (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv, "device_hold_logged", False, raising=False)
    # The idle escalation's and the per-tray BMC reset's once-per-episode latches are per-episode
    # state on the fresh srv.fsm just constructed above, so they are already unarmed here — no
    # separate reset needed.
    # The reset-in-flight flag gates the dead-chip sampler; the per-tray reset walk holds it across the
    # walk. A test that leaves it set (e.g. a fails-on-base stash without the finally) would blind the
    # next test's sampler, so reset it. raising=False so a base tree still sets up cleanly.
    monkeypatch.setattr(srv.recovery_mechanism, "reset_in_flight", False, raising=False)
    # The once-per-process dedup for no-verdict skip journaling is another persistent global: a
    # test that trips a fabric/eth-heartbeat skip leaves its key set, which would then suppress the
    # NEXT test's expected skip event. raising=False so a fails-on-base tree still sets up cleanly.
    monkeypatch.setattr(srv.health_monitor, "_skip_events_journaled", set(), raising=False)
    # Same shape for the systemd-notify error dedup: a test that trips a broken $NOTIFY_SOCKET
    # leaves its per-kind key set, which would suppress the NEXT test's expected notify warning.
    monkeypatch.setattr(srv, "_sd_notify_errors_logged", set(), raising=False)

    # The suite must never actually reboot or power-cycle the box it runs on. Both rungs default ON
    # (a ladder nobody armed is a ladder that ends in a permanent hold), so this pins them OFF
    # explicitly — deleting the vars would now select the armed default and point every gate test at
    # the real thing. The fires below stay loud tripwires so a test that DOES reach one fails instead
    # of taking CI (or a dev box) down. A test exercising either path re-patches the fire itself.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", "0")
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    # Same for the per-tray BMC reset: clear its opt-in so no test reaches the fire by accident, and
    # make the real fire a loud tripwire so a test that DOES reach it fails rather than issuing real
    # reset ioctls / ipmitool. A test exercising the tray-reset path re-patches _fire_ubb_reset itself.
    monkeypatch.delenv("TT_DEVICE_MCP_AUTO_UBB_RESET", raising=False)

    def _no_reboot_in_tests():
        raise AssertionError("a test reached the real host reboot; _fire_host_reboot must be mocked")

    monkeypatch.setattr(recovery_galaxy, "_fire_host_reboot", _no_reboot_in_tests)

    def _no_ubb_reset_in_tests(*a, **k):
        raise AssertionError("a test reached the real per-tray BMC reset; _fire_ubb_reset must be mocked")

    monkeypatch.setattr(recovery_galaxy, "_fire_ubb_reset", _no_ubb_reset_in_tests, raising=False)

    _install_spawn_tripwire(monkeypatch, allow_device_spawns=device_marked)


# Programs no test may ever hand to the kernel, marker or not: they power-cycle the box, reboot
# it, or write PCI config space directly. A test that composes one asserts on the argv instead
# (spec 09 I3).
_NEVER_SPAWN = ("setpci", "ipmitool", "reboot", "shutdown", "poweroff")
# Spawnable only under the `device` marker (spec 09 I2/I8). tt-smi is both the chip enumerator
# and the ladder's warm rung (`tt-smi -r`) — the one destructive step a device test may perform,
# and the ceiling of what a pytest run may do to the box it runs on. systemd-run is how that
# reset is actually issued: a transient `--scope ... --collect` unit, so a broker restart cannot
# kill the reset partway. Permitting the wrapper is safe because the check below is token-wise
# over the whole argv — a systemd-run wrapping anything in _NEVER_SPAWN is still refused, under
# the marker or not — and `--collect` means the unit is reaped when it exits rather than leaked.
_DEVICE_ONLY_SPAWN = ("tt-smi", "systemd-run")
# systemctl is both a read-only query (scope liveness, which several gate paths genuinely need) and
# a way to stop the host's services. Only the mutating verbs are refused.
_FORBIDDEN_SYSTEMCTL_VERBS = ("stop", "start", "restart", "reload", "enable", "disable", "mask", "kill")


def _only_touches_device_pollers(parts, verb_at: int) -> bool:
    """Whether a mutating systemctl names nothing but the broker's own declared poller units.

    A real reset must land on a quiesced bus, and quiescing IS `systemctl stop` against
    DEVICE_POLLER_SERVICES — so a device test that refuses it can only test a reset in a
    configuration production never runs, which on real silicon is the more dangerous one.
    Read from the broker's own constant rather than a literal here, so the exception cannot
    drift wider than the set the broker actually manages: notably it excludes
    tt-device-broker.service, which a test stopping would pull the host out from under itself.
    """
    units = {tok for tok in parts[verb_at + 1 :] if not tok.startswith("-")}
    return bool(units) and units <= set(srv.DEVICE_POLLER_SERVICES)


def _spawn_is_forbidden(argv, allow_device_spawns: bool = False) -> "str | None":
    """The reason this spawn must not reach the kernel, or None when it is harmless."""
    if isinstance(argv, (str, bytes)):
        parts = (argv.decode() if isinstance(argv, bytes) else argv).split()
    else:
        parts = [a.decode() if isinstance(a, bytes) else str(a) for a in argv]
    if not parts:
        return None
    for tok in parts:
        prog = os.path.basename(tok)
        if prog in _NEVER_SPAWN:
            return f"{prog} would reboot, power-cycle, or escape this test run"
        if prog in _DEVICE_ONLY_SPAWN and not allow_device_spawns:
            return f"{prog} touches the device; declare @pytest.mark.device or assert on the argv"
        if prog == "systemctl":
            at = parts.index(tok)
            for offset, p in enumerate(parts[at + 1 :]):
                v = os.path.basename(p)
                if v in _FORBIDDEN_SYSTEMCTL_VERBS:
                    if allow_device_spawns and _only_touches_device_pollers(parts, at + 1 + offset):
                        return None
                    return f"systemctl {v} would change host service state"
            return None
    return None


def _install_spawn_tripwire(monkeypatch, allow_device_spawns: bool = False):
    """Refuse, loudly, any spawn that would reach real hardware or host services.

    The seam is the process-spawn layer rather than each caller, because the callers are spread
    across the gate, the recovery stages, and the pollers, and a new one must not be able to
    slip past by being written after this fixture. A test that means to exercise such a path
    stubs the function that composes the argv and asserts on the argv itself.

    `allow_device_spawns` moves _DEVICE_ONLY_SPAWN from refused to permitted for a
    device-marked test. _NEVER_SPAWN and the mutating systemctl verbs are unaffected by it.
    """

    def _refuse(argv, kind):
        reason = _spawn_is_forbidden(argv, allow_device_spawns)
        if reason:
            shown = argv if isinstance(argv, str) else " ".join(str(a) for a in argv)
            raise AssertionError(
                f"a test spawned a forbidden command via {kind}: {shown[:200]}\n"
                f"  {reason}. Stub the function that builds this argv and assert on the argv."
            )

    real_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self, args, *a, **k):
        _refuse(args, "subprocess.Popen")
        return real_popen_init(self, args, *a, **k)

    real_exec = asyncio.create_subprocess_exec

    async def guarded_exec(program, *args, **k):
        _refuse([program, *args], "asyncio.create_subprocess_exec")
        return await real_exec(program, *args, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)


@pytest.fixture(autouse=True)
def init_async_primitives():
    """Initialize async primitives before any test runs."""
    _ensure_async_primitives()


@pytest.fixture
def clear_job_state(init_async_primitives):
    """Clear jobs dict and queue before and after each test."""
    # Reset global queue and lock to None so they get recreated in current event loop
    _reset_for_testing()

    # Re-initialize async primitives in current event loop
    _ensure_async_primitives()
    queue = get_job_queue()

    # Clear before test
    jobs.clear()
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    yield

    # Clear after test
    jobs.clear()
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break


@pytest.fixture
def health_deps():
    """A RecoveryDeps wired to tt_device_mcp.server's OWN module globals/functions via lambdas —
    the same late-bound pattern server.py uses for its own module-level ``recovery_mechanism`` and
    ``_recovery_deps`` — so a test that monkeypatches e.g. ``srv.health_monitor._board_types`` or
    ``srv._auto_reboot_enabled`` still takes effect through a Recovery (or HealthMonitor) built
    from this fixture."""
    return RecoveryDeps(
        current_boot_id=lambda: srv._current_boot_id(),
        boot_btime_id=lambda: srv._boot_btime_id(),
        scoped_reset_backend=lambda: srv._scoped_reset_backend(),
        set_device_pollers=lambda active, log: srv._set_device_pollers(active, log),
        set_device_op_detail=lambda detail: srv._set_device_op_detail(detail),
        begin_action_row=lambda *a, **k: srv._begin_action_row(*a, **k),
        write_action_log=lambda *a, **k: srv.write_action_log(*a, **k),
        device_hold_episode_since=lambda: srv.device_hold_episode_since,
        auto_reboot_enabled=lambda: srv._auto_reboot_enabled(),
        auto_power_cycle_enabled=lambda: srv._auto_power_cycle_enabled(),
        board_types_provider=lambda: srv.health_monitor._board_types,
        glx_board_types_provider=lambda: srv.health_monitor._glx_board_types(),
        bus_ids_provider=lambda: srv.health_monitor._bus_ids,
        journal_skip_once=lambda kind, reason, **f: srv.health_monitor._journal_skip_once(kind, reason, **f),
        terminate_process_group=lambda pid: srv._terminate_process_group(pid),
        logger=lambda: srv.logger,
        present_chip_indices=lambda: srv._present_chip_indices(),
        enumerate_device_holders=lambda: srv.enumerate_device_holders(),
        job_running=lambda: any(j.status == srv.JobStatus.RUNNING for j in srv.jobs.values()),
        isolated_chips=lambda: srv.isolated_chips,
        device_pci_map=lambda: srv.device_pci_map,
        device_hold_episode_reason=lambda: srv.device_hold_episode_reason,
        device_hold_episode_escalated=lambda: srv.fsm.latch("escalated"),
        hold_escalation_latched=lambda: srv._hold_escalation_latched(),
        set_device_hold_episode_escalated=srv._set_hold_escalated,
        device_hold_episode_ubb_reset_fired=lambda: srv.fsm.latch("ubb_reset_fired"),
        set_device_hold_episode_ubb_reset_fired=lambda v: srv.fsm.set_latch("ubb_reset_fired", v),
        clear_device_reported_fault=lambda why: srv._clear_device_reported_fault(why),
        clear_device_dirty=lambda **kw: srv._clear_device_dirty(**kw),
        device_degraded=lambda: srv._recovery_degraded(),
        auto_power_cycle_host=lambda log, reason: srv._auto_power_cycle_host(log, reason),
        emit_all_off_bus_power_cycle_required=lambda log, off_bus, expected, context: (
            srv._emit_all_off_bus_power_cycle_required(log, off_bus, expected, context=context)
        ),
        host_escalation_kwargs=lambda: srv._host_escalation_kwargs(),
        read_heartbeats=lambda: srv.read_heartbeats(),
        heartbeat_supported=lambda: srv.heartbeat_supported(),
        heartbeat_verdict=lambda expected: srv.heartbeat_verdict(expected),
        gone_chip_confirm_settle_sec=lambda: srv.GONE_CHIP_CONFIRM_SETTLE_SEC,
    )


def fsm_dirty(
    owner, detail: str, *, why: str = "job_killed", job: dict | None = None, dirty: bool | None = None
) -> None:
    """Put ``owner.fsm`` (``srv`` or a test's own server module reference) into a RECOVERING
    episode the way ``_mark_device_dirty`` (a job/probe finding, owing the next gate a reset) or
    ``_hold_device_unverified``/``_hold_device_fabric_unverified`` (the gate's own affirmative
    hold, NOT owing a reset) do in production — the test-side replacement for the old
    ``monkeypatch.setattr(srv, "device_dirty", True)`` + ``device_dirty_reason``/
    ``device_unverified_why`` pair.

    ``dirty`` defaults to inferred from ``why``: the hold-flavored values (self-heal, the fabric
    relift, and the generic-escalate class) are placed by the gate's own hold functions and so
    default to ``False``; every other value is a plain dirty mark and defaults to ``True``. Pass it
    explicitly to override."""
    if dirty is None:
        hold_whys = owner.SELFHEAL_WHYS | {owner.FABRIC_RELIFT_WHY} | owner.GENERIC_ESCALATE_WHYS
        dirty = why not in hold_whys
    owner.fsm.on_fault(why, detail=detail, job=job or {}, dirty=dirty)


def fsm_healthy(owner) -> None:
    """Fold a verified-healthy reading into ``owner.fsm`` — the test-side replacement for
    ``monkeypatch.setattr(srv, "device_dirty", False)`` / ``_clear_device_dirty(verified=True)``."""
    owner.fsm.on_readings(HealthState(phase="test", at=datetime.now(), expected=0))


# The PCI bus ids a 32-chip Blackhole Galaxy reports: four groups of eight, one group per UBB
# tray, the low nibble counting the chips within the group. Tray IDENTITY is derived from these
# (spec 04 I16) and is NOT the chip index divided by eight — bus group 0x80 is tray 4 and 0xc0 is
# tray 3, so the two orderings disagree for half the mesh.
GALAXY_BUS_IDS = [f"0000:{group + n:02x}:00.0" for group in (0x00, 0x40, 0x80, 0xC0) for n in range(1, 9)]


@pytest.fixture
def galaxy_trays(monkeypatch):
    """Put the monitor in the state one healthy Blackhole Galaxy snapshot leaves it in, and hand
    back the resulting tray map.

    Every tray-rung test needs this: the map is read from caches the gate fills on its way past,
    and a broker that has never seen a healthy snapshot declines the rung outright rather than
    derive a tray from the chip index."""
    monkeypatch.setattr(srv.health_monitor, "_bus_ids", list(GALAXY_BUS_IDS), raising=False)
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["tt-galaxy-bh"] * len(GALAXY_BUS_IDS))
    return srv.galaxy_recovery._tray_map_now()


def patch_health_event(monkeypatch, fn) -> None:
    """Patch ``health_event`` everywhere the recovery ladder can call it.

    ``server.py``, ``health.recovery`` (the platform-invariant verify/reset loop), and
    ``health.recovery.galaxy`` (the escalation ladder) each hold their OWN
    ``from health.evidence import health_event`` binding — an import copies the reference, so
    patching one module's copy never touches another's. A test that wants to see (or silence) an
    event any of these bodies emits has to patch all three."""
    monkeypatch.setattr(srv, "health_event", fn)
    monkeypatch.setattr(recovery_pkg, "health_event", fn)
    monkeypatch.setattr(recovery_galaxy, "health_event", fn)


def patch_recovery(monkeypatch, name: str, fn) -> None:
    """Patch a platform-invariant Recovery method (``_verify_device``, ``_reset_and_verify_device``,
    ``_recover_isolated_chips``, ``_verify_device_after_reset``) on BOTH persistent instances
    (``srv.galaxy_recovery`` and ``srv.per_target_recovery``).

    A test cannot know in advance which of the two ``select_recovery`` hands the gate/idle-relift
    for its own env/board-type setup, and these fakes are plain callables with no ``self`` — which
    only run unbound (matching the fake's signature) when the attribute lives on the INSTANCE, not
    the class. Patching both instances the same way is what makes the fake take effect regardless of
    which platform this test's setup resolves to, without changing every fake's signature to accept
    (and ignore) ``self``."""
    monkeypatch.setattr(srv.galaxy_recovery, name, fn)
    monkeypatch.setattr(srv.per_target_recovery, name, fn)
