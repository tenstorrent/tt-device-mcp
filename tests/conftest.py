# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures for tt-device-mcp tests."""

import asyncio
import errno
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

# Add src directory to path for tests
src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

import tt_device_mcp.device_holders as device_holders
import tt_device_mcp.job_reap as job_reap
import tt_device_mcp.server as srv
from tt_device_mcp import pre_reset_scan, privileges
from tt_device_mcp.fsm import ServerFsm
from tt_device_mcp.health import bmc_capture, post_reset
from tt_device_mcp.health import evidence as health
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.core import HealthState
from tt_device_mcp.health.monitor import HealthMonitor
from tt_device_mcp.health.monitors import eth, heartbeat, pci
from tt_device_mcp.health.recovery import RecoveryDeps, offbus_power_cycle, pcie_guard
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import galaxy as recovery_galaxy
from tt_device_mcp.health.recovery.stages import power_cycle as power_cycle_stage
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
def old_host_boot(monkeypatch):
    """Pin this host's boot time to a day ago around every test: the power-cycle cooldown counts from
    the host's boot (spec 04 I65), and a suite run soon after the runner's own reboot must not see
    every power cycle in cooldown. A test that needs another boot patches ``srv._boot_btime_id``."""
    monkeypatch.setattr(srv, "_boot_btime_id", lambda: str(int(time.time()) - 86400))


@pytest.fixture(autouse=True)
def quick_post_kill_settle(monkeypatch):
    """The post-kill wait and settle (spec 03 I89) take no wall time unless a test sets them: a
    32-chip kill test would otherwise sleep the settle, and wait out a real holder on a device host.
    The job record behind the debt never outlives a test."""
    monkeypatch.setattr(srv, "POST_KILL_REAP_WAIT_SEC", 0)
    monkeypatch.setattr(srv, "POST_KILL_SETTLE_SEC", 0)
    monkeypatch.setattr(srv, "POST_KILL_POLL_SEC", 0.01)
    monkeypatch.setattr(srv, "_kill_reset_job", {})


@pytest.fixture(autouse=True)
def reset_post_reset_fabric_debt():
    """Clear the process-wide post-reset fabric debt (spec 04 I32) around every test: a reset one
    test drove must not hold a release in the next."""
    from tt_device_mcp.health.post_reset import FABRIC_DEBT

    FABRIC_DEBT.owed = ""
    FABRIC_DEBT.pass_monotonic = 0.0
    yield
    FABRIC_DEBT.owed = ""
    FABRIC_DEBT.pass_monotonic = 0.0


@pytest.fixture(autouse=True)
def reset_removed_chip_records():
    """Clear the record of chips the broker removed (spec 04 I69) around every test: a chip one test
    isolated must not read as removed, and so withhold the rescan, in the next."""
    from tt_device_mcp.health.monitors import pci

    pci.REMOVED_CHIPS.clear()
    yield
    pci.REMOVED_CHIPS.clear()


@pytest.fixture(autouse=True)
def reset_pci_writer():
    """Clear the one in-flight PCI writer and its PCI-wedged latch (spec 04 I76) around every test."""
    try:
        from tt_device_mcp.health.pci_writer import PCI_WRITER
    except ImportError:
        yield
        return
    PCI_WRITER.reset_for_tests()
    yield
    PCI_WRITER.reset_for_tests()


@pytest.fixture(autouse=True)
def reset_foreign_holders_seen():
    """Forget the foreign holders a gate recorded: one test's must not reach the next test's
    foreign-holder verify event. Only when the server module is already loaded."""
    yield
    srv = sys.modules.get("tt_device_mcp.server")
    if srv is not None:
        srv._foreign_holders_seen = ("", [])
        srv._foreign_holders_logged.clear()


@pytest.fixture(autouse=True)
def reset_broker_internal_pids():
    """Clear the broker-internal pid registry (spec 04 I59) around every test: a probe one test
    spawned through run_probe must not discount a holder in the next."""
    from tt_device_mcp import device_holders

    device_holders._clear_broker_internal()
    yield
    device_holders._clear_broker_internal()


@pytest.fixture(autouse=True)
def reset_host_action_state():
    """Clear the process-wide mechanism's pending host action (spec 04 I34) around every test:
    escalate() refuses every rung under it, so one a test left behind would stop the next test's
    ladder."""

    def clear():
        m = srv.recovery_mechanism
        if m is None:
            return
        m.host_action_requested = None
        m._host_action_reset_refused = None
        # The last mesh reset's per-process record (spec 04 I93): one test's reset does not space the next's sweep.
        m.last_mesh_reset_run = None

    clear()
    yield
    clear()


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


# Spec 09 I9: a test may signal only processes this pytest process spawned. The broker's kill
# paths (the dead-chip valve's holder kill, the survivor reap's pgid backstop, foreign-holder
# reclaim) act on whatever the host's /proc shows, and a test that reached one unstubbed on a
# device host SIGKILLed real jobs holding the real device. Every child inherits this token, so a
# child that left the session or was re-parented still reads as ours; each process (an xdist
# worker included) gets its own value.
_SPAWN_TOKEN_ENV = "TT_DEVICE_MCP_TEST_SPAWN_TOKEN"
_SPAWN_TOKEN = f"{os.getpid()}-{time.time_ns()}"
os.environ[_SPAWN_TOKEN_ENV] = _SPAWN_TOKEN
_real_kill, _real_killpg = os.kill, os.killpg


def _proc_stat_fields(pid: int) -> "list[str] | None":
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    return data[data.rindex(")") + 2 :].split()


# A live process's environ reads empty while it execs: the new image's environment is not set up
# yet. A setsid child that is exec'ing its command, with its shell gone, then shows neither ancestry
# nor token (seen under load in a PID namespace). Such a read is taken again, for at most this long.
_TOKEN_REREAD_SEC = 2.0
_PF_KTHREAD = 0x00200000


def _carries_spawn_token(pid: int) -> bool:
    """pid's environ holds this run's token. An empty read of a live user process cannot tell and
    is read again until the bound; one still empty then, a zombie or a kernel thread is not ours."""
    deadline = time.monotonic() + _TOKEN_REREAD_SEC
    while True:
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read()
        except OSError:
            return False
        if raw:
            return f"{_SPAWN_TOKEN_ENV}={_SPAWN_TOKEN}".encode() in raw.split(b"\0")
        fields = _proc_stat_fields(pid)
        if fields is None or fields[0] in ("Z", "X") or int(fields[6]) & _PF_KTHREAD:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _spawned_by_this_run(pid: int) -> bool:
    """pid is this process, one of its descendants, or a process that inherited its token."""
    me, cur = os.getpid(), pid
    for _ in range(4096):
        if cur == me:
            return True
        fields = _proc_stat_fields(cur) if cur and cur > 1 else None
        if fields is None:
            break
        cur = int(fields[1])
    return _carries_spawn_token(pid)


# include/linux/sched.h: do_exit sets it on each thread before it drops the mm (empty environ).
_PF_EXITING = 0x4


def _thread_stat_fields(pid: int, tid: str) -> "list[str] | None":
    try:
        with open(f"/proc/{pid}/task/{tid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    return data[data.rindex(")") + 2 :].split()


def _exited(pid: int) -> bool:
    """pid has exited or is exiting (no thread of it can act on a signal any more), or is gone.
    Once do_exit drops a process's mm its /proc environ is empty and, once its parent exits
    first, its ppid is init, so it no longer reads as this run's; a SIGKILL to a killed job's
    group can meet its `sleep` there, mid-exit (PF_EXITING) or a zombie, and init can reap it
    during these reads. A leader that left its live threads behind also reads 'Z', so every
    thread counts, listed again after the reads: only a live thread makes a new one."""
    try:
        tids = os.listdir(f"/proc/{pid}/task")
        for tid in tids:
            fields = _thread_stat_fields(pid, tid)
            if fields is not None and fields[0] not in ("Z", "X") and not int(fields[6]) & _PF_EXITING:
                return False
        return bool(tids) and set(os.listdir(f"/proc/{pid}/task")) <= set(tids)
    except FileNotFoundError:
        return True
    except (OSError, ValueError, IndexError):
        return False


def _group_members(pgid: int) -> list:
    members = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            fields = _proc_stat_fields(int(name))
            if fields is not None and int(fields[2]) == pgid:
                members.append(int(name))
    return members


class _SignalGuard:
    """Refuses (EPERM, nothing delivered) a signal aimed at a process this run did not spawn, and
    records it so the test fails. Signal 0 delivers nothing and passes; so does a pid that does not
    exist (the call raises ESRCH as it would anyway)."""

    def __init__(self):
        self.refused: list = []
        self.owned = _spawned_by_this_run

    def _foreign(self, pids: list) -> list:
        """The pids that exist and are not this run's. A process (or an ancestor) that exits during
        the ppid walk breaks it, so a miss is read again; one that is gone, exiting or exited by
        then is skipped."""
        return [
            p
            for p in pids
            if not self.owned(p) and os.path.exists(f"/proc/{p}") and not self.owned(p) and not _exited(p)
        ]

    def _refuse(self, what: str, sig: int, members: list):
        self.refused.append(f"{what}(signal {int(sig)}) reaches pid(s) {members[:8]} this test did not spawn")
        raise PermissionError(errno.EPERM, f"spec 09 I9: refused {what}: not spawned by this test run")

    def kill(self, pid: int, sig: int):
        if sig == 0:
            return _real_kill(pid, sig)
        if pid == -1:
            return self._refuse(f"os.kill({pid})", sig, ["every process"])
        if pid <= 0:
            return self.killpg(os.getpgid(0) if pid == 0 else -pid, sig, what=f"os.kill({pid})")
        if self._foreign([pid]):
            return self._refuse(f"os.kill({pid})", sig, [pid])
        return _real_kill(pid, sig)

    def killpg(self, pgid: int, sig: int, what: str = ""):
        what = what or f"os.killpg({pgid})"
        if sig == 0:
            return _real_killpg(pgid, sig)
        members = _group_members(os.getpgid(0) if pgid == 0 else pgid)
        foreign = self._foreign(members)
        if foreign:
            return self._refuse(what, sig, foreign)
        return _real_killpg(pgid, sig)


@pytest.fixture(autouse=True)
def signal_guard(monkeypatch):
    """Spec 09 I9: no test signals a process it did not spawn. A refused signal fails the test."""
    guard = _SignalGuard()
    monkeypatch.setattr(os, "kill", guard.kill)
    monkeypatch.setattr(os, "killpg", guard.killpg)
    yield guard
    if guard.refused:
        pytest.fail("spec 09 I9: this test signalled processes it did not spawn: " + "; ".join(guard.refused))


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
    config.addinivalue_line("markers", "xdist_group(name): under -n, tests of one group run on one worker")
    # Under -n, the short fork-heavy deploy-script tests (marked xdist_group("forks")) share one
    # worker, so they no longer start their shell processes on every worker at once. Set here, not in
    # addopts: a run without xdist (-p no:xdist) would refuse --dist. An explicit --dist is kept.
    passed = list(config.invocation_params.args) + os.environ.get("PYTEST_ADDOPTS", "").split()
    if getattr(config.option, "dist", None) == "load" and not any(a.startswith("--dist") for a in passed):
        config.option.dist = "loadgroup"


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


class _FlatTmpDirs:
    """A fresh empty dir per call, made in one session dir without listing it.

    `tmp_path_factory.mktemp()` lists its whole base dir to pick the next number, so its cost grows
    with every dir the run has made. The isolation below makes about 20 dirs per test; through
    `mktemp` that made each test slower than the last (about 1 s each by the end of a CI run, and
    the job hit its time limit). `tempfile.mkdtemp` picks a random name instead: constant cost.
    """

    def __init__(self, root: Path):
        self._root = root

    def mktemp(self, basename: str) -> Path:
        return Path(tempfile.mkdtemp(prefix=f"{basename}-", dir=self._root))


@pytest.fixture(scope="session")
def isolation_tmp_root(tmp_path_factory):
    """The one dir every test's isolation dirs go in; pytest's basetemp retention cleans it."""
    return tmp_path_factory.mktemp("isolation")


@pytest.fixture(scope="session")
def device_probe_cache_root(tmp_path_factory):
    """One tt-metal cache dir for every device test in the run — see _redirect_probe_caches."""
    return tmp_path_factory.mktemp("device-probe-cache")


def _seal_real_hardware(monkeypatch, tmp_dirs):
    """Hide the host's real hardware from a test that did not declare it needs hardware.

    Every patch here answers the same question — can this test see or reach real silicon —
    so they lift together under the `device` marker and nowhere else. Split out from the
    fixture body for exactly that reason: the seals that remain there are about durable
    broker state and the destructive rungs, which no marker ever lifts.
    """
    monkeypatch.setattr(pci, "SYSFS_CLASS_DIR", tmp_dirs.mktemp("sysfs"))
    # A host running this suite may have the real pinned fabric validator installed at the
    # default TTDEV_VALIDATOR_ROOT (see health.monitors.fabric.build_command) — point it at an
    # empty dir so an unset TT_DEVICE_MCP_FABRIC_CHECK_CMD reliably resolves to "no built-in
    # validator either", never to a real binary that would push traffic across a live mesh.
    # TTDEV_FABRIC_BIN/_RUNTIME_ROOT/_DESCRIPTOR each override VROOT-derived resolution outright,
    # so clearing VROOT alone is not a real sandbox while any of them could be ambient (a leaked
    # setenv from another test, a host-level env file) — clear all three, and the override
    # command itself, so every test starts from the same "nothing configured or installed" floor.
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(tmp_dirs.mktemp("no-fabric-validator")))
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
    monkeypatch.setattr(eth, "DEFAULT_ETH_VENV_PYTHON", str(tmp_dirs.mktemp("no-eth-venv") / "bin" / "python"))
    # eth._probe_path()'s two remaining candidates, same rationale: TTDEV_ROOT defaults to
    # /opt/tt-device-broker, and this box's real broker install genuinely has a real
    # eth-heartbeat-probe.py staged there — sandboxed to an empty dir so that candidate misses.
    # eth._REPO_ROOT defaults to THIS repo's own checkout (an editable install), which genuinely
    # has deploy/tt-device-eth-heartbeat-probe.py too — patched to an empty dir for the same
    # reason DEFAULT_ETH_VENV_PYTHON is: a test must never resolve a real, executable probe path
    # by construction, not by this box happening not to have one where a candidate looks.
    monkeypatch.setenv("TTDEV_ROOT", str(tmp_dirs.mktemp("no-eth-install-root")))
    monkeypatch.setattr(eth, "_REPO_ROOT", tmp_dirs.mktemp("no-eth-repo-checkout"))
    # PCI_DEVICES_DIR defaults to the REAL /sys/bus/pci/devices, read at import time (so an env
    # var set here would arrive too late) — every test that reaches chip_node_present() without
    # patching this itself would otherwise read this box's own live PCI topology. A default here
    # does not disturb the ~14 tests that already patch it to their own scenario-specific dir:
    # their own monkeypatch.setattr runs later, in the test body, and simply overrides this one.
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", tmp_dirs.mktemp("no-pci-devices"))
    # pcie_guard reads sysfs under SYS_ROOT (the between-jobs check records the PCI topology):
    # an empty root, for the same reason.
    monkeypatch.setattr(pcie_guard, "SYS_ROOT", tmp_dirs.mktemp("no-sys-root"))
    # TT_DEV_DIR defaults to the REAL /dev/tenstorrent and is read fresh by _present_chip_indices()
    # on every call — on a build host with actual hardware (unlike CI, which has none) a test that
    # never sets this itself silently probes the real device count instead of the scenario it
    # declares, and its assertions then depend on which machine happens to run them. Sandboxed to
    # an empty dir here for the same reason PCI_DEVICES_DIR is above; the ~40+ tests that already
    # patch it to their own scenario-specific dir are unaffected — their own monkeypatch.setattr
    # runs later, in the test body, and simply overrides this default.
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_dirs.mktemp("no-dev-tenstorrent")))
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
    monkeypatch.setattr(device_holders, "DRIVER_PROC_DIR", str(tmp_dirs.mktemp("no-tt-driver-proc") / "absent"))
    # The leftover fence (01 I16) reads the REAL /proc/<pid>/cgroup of whatever holder a scan names
    # — on a broker host a live job's pid sits in a real ttdev-job-<id>.scope this suite never
    # started, and would read as a reaped job's leftover that refuses the test's reset. Pointed at
    # an empty dir; a test about the fence stages its own. Its per-process state starts empty.
    monkeypatch.setattr(job_reap, "LEFTOVER_PROC_DIR", str(tmp_dirs.mktemp("no-leftover-proc")))
    # The tenant rule reads a holder's REAL /proc/<pid>/cgroup at scan time for the same reason: on a
    # broker host a live job's pid would read as scoped. Empty dir; a test about scopes stages its own.
    monkeypatch.setattr(device_holders, "SCOPE_PROC_DIR", str(tmp_dirs.mktemp("no-scope-proc")))
    monkeypatch.delenv(device_holders.TENANT_UIDS_ENV, raising=False)
    # The fd walk reads the REAL /proc and matches each fd against DEVICE_DIR, which defaults to the
    # REAL /dev/tenstorrent: on a device host, any real job or service of a uid >= 1000 that has the
    # device open reads as a live tenant, so a test that runs a job, a rung or a hold through the
    # holder gate waits on it or sees its rung blocked, depending on what the host runs right then.
    # Pointed at a path that cannot exist, like DRIVER_PROC_DIR; a test about holders stages its own.
    monkeypatch.setattr(device_holders, "DEVICE_DIR", "/nonexistent/tt-device-mcp-test/dev-tenstorrent")
    monkeypatch.setattr(srv, "reaped_survivors", {})
    monkeypatch.setattr(srv, "_leftover_fence_detail", "")
    monkeypatch.setattr(srv, "_leftover_unkillable_since", None)
    monkeypatch.setattr(srv, "_leftover_unkillable_mono", None)
    monkeypatch.setattr(srv, "_leftover_deadline_paged", False)
    monkeypatch.setattr(srv, "_leftover_rung_last_try", 0.0)
    monkeypatch.setattr(srv, "_leftover_rung_stage", "")
    monkeypatch.setattr(srv, "_leftover_rung_detail", "")
    monkeypatch.setattr(srv, "_leftover_rung_task", None)
    monkeypatch.setattr(srv, "_dead_chip_stuck", {})
    monkeypatch.setattr(srv, "_dead_chip_stuck_seen", None)
    monkeypatch.setattr(job_reap, "_outstanding_reads", {})
    monkeypatch.setattr(srv, "_dead_chip_page_pending", None)
    monkeypatch.setattr(srv, "_leftover_dead_chip", False)
    monkeypatch.setattr(recovery_base, "_reenumerate_withheld_journaled", {})
    # The pollers a quiesce stopped and owes a restore. None: read again from the per-test health dir.
    monkeypatch.setattr(srv, "_pollers_to_restore", None)
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
    # Same hole for a job's own scope: _terminate_job asks systemd whether ttdev-job-<id>.scope
    # is active. On a broker host whose live job shares a test job's id, the real answer is yes
    # and the kill path goes to `systemctl kill` on the host's job. A test about scopes patches it.
    monkeypatch.setattr(srv, "_scope_active", lambda scope: False)

    # The pre-reset holder scan (spec 04 I92) walks /proc and signals the readers it finds. Unstubbed,
    # a unit test's reset would scan this machine's real processes. Every reset here sees a clear
    # scan; the scan's own tests drive it through its seams.
    async def _clear_scan(**_kw):
        return pre_reset_scan.PreResetScan()

    monkeypatch.setattr(srv, "clear_device_readers", _clear_scan)


def _redirect_probe_caches(monkeypatch, tmp_dirs, device_marked, session_root):
    """Keep the fabric and eth probes off the real /var/cache, marker or not.

    Both default to a hardcoded real /var/cache/tt-device-broker path and mkdir it, so this is
    a redirect rather than a delenv. A device test gets one session-scoped dir instead of a
    fresh per-test one: these hold a compiled tt-metal cache, and a per-test dir would make
    every device test pay the build again.
    """
    if device_marked:
        fabric, eth_cache = session_root / "fabric", session_root / "eth"
    else:
        fabric = tmp_dirs.mktemp("no-fabric-cache")
        eth_cache = tmp_dirs.mktemp("no-eth-cache")
    monkeypatch.setenv("TTDEV_FABRIC_CACHE", str(fabric))
    monkeypatch.setenv("TTDEV_ETH_CHECK_CACHE", str(eth_cache))


@pytest.fixture(autouse=True)
def isolate_device_state(monkeypatch, isolation_tmp_root, device_marked, device_probe_cache_root):
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
    tmp_dirs = _FlatTmpDirs(isolation_tmp_root)
    if not device_marked:
        _seal_real_hardware(monkeypatch, tmp_dirs)
    _redirect_probe_caches(monkeypatch, tmp_dirs, device_marked, device_probe_cache_root)
    health_tmp = tmp_dirs.mktemp("health")
    monkeypatch.setattr(health, "HEALTH_DIR", health_tmp)
    # Spec 04 I91: a mesh reset's BMC preflight runs `ipmitool mc info`; ipmitool is never spawned
    # by the suite, so every test sees a healthy BMC unless it patches its own answer.
    from tt_device_mcp.health.recovery import ipmi_guard

    monkeypatch.setattr(
        ipmi_guard,
        "_run_mc_info",
        lambda timeout: subprocess.CompletedProcess(
            ["ipmitool", "mc", "info"], 0, "Firmware Revision : 0.05\nAux Firmware Rev Info : 0x23 0x00 0x00 0x00\n", ""
        ),
    )
    monkeypatch.setattr(heartbeat, "_heartbeat_supported", None)
    # raising=False so a base tree without the re-arm latch (a fails-on-base stash) still sets up.
    monkeypatch.setattr(heartbeat, "_heartbeat_absent_journaled", False, raising=False)
    # eth.resolve_python() caches its answer per process; every test starts with it empty, so
    # one test's resolved stub python never answers for the next one's.
    monkeypatch.setattr(eth, "_python_cache", None, raising=False)
    # Prometheus's textfile dir defaults to the REAL /var/lib/prometheus/node-exporter and is read
    # fresh on every write_textfile() call (never cached), so any path through the stats
    # persistence loop that reaches it in a test would mkdir a real host path.
    monkeypatch.setenv("TT_DEVICE_MCP_TEXTFILE_DIR", str(tmp_dirs.mktemp("no-textfile-dir")))
    # The device-op flock defaults to /run/tt-device-broker/device-op.flock, the file the host's own
    # broker holds during its resets. A test that took it there would stall that broker's device op.
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_FLOCK", str(tmp_dirs.mktemp("flock") / "device-op.flock"))
    monkeypatch.setenv("TT_DEVICE_MCP_DISPATCH_HOLD", str(tmp_dirs.mktemp("hold") / "dispatch-hold"))
    monkeypatch.setenv("TT_DEVICE_MCP_JOB_SPOOL_DIR", str(tmp_dirs.mktemp("spool") / "spool"))
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
    # A poller restart one test deferred must not fire on the next test's close to HEALTHY.
    monkeypatch.setattr(srv, "_pollers_owed", None)
    monkeypatch.setattr(srv, "_pollers_owed_loop", None)

    # No board types seen yet: the conservative default, and the machine type reads as unknown.
    # Nothing here can shell out — the cache is only ever filled by a health snapshot — so the
    # reset mode cannot depend on which machine CI lands on. Tests that want a machine type set
    # `_board_types` and `_glx_board_types` themselves. Both now live on the health_monitor
    # singleton, not on the server module itself (see health/monitor.py).
    monkeypatch.setattr(srv.health_monitor, "_board_types", None)
    monkeypatch.setattr(srv.health_monitor, "_glx_cache", None)

    monkeypatch.setattr(srv, "device_fault_reported", "", raising=False)
    # Many tests assign srv.isolated_chips directly; a leaked chip makes a later hold not 77-only.
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_offbus_hold_chips", set())
    # Spec 03 I66: the lost/recovering registry is process-wide; each test starts with none, and
    # publishes into its own tmp file, never /run.
    monkeypatch.setenv("TT_DEVICE_MCP_CHIP_STATE_FILE", str(tmp_dirs.mktemp("chipstate") / "chip-state.json"))
    from tt_device_mcp.health import chip_state

    chip_state.reset()
    # The dead-chip valve's per-chip strikes: a strike left by an earlier test on the same worker
    # marks a chip lost on its first all-ones sample in the next one.
    monkeypatch.setattr(srv.sampler, "dead_chip_strikes", {})
    monkeypatch.setattr(srv, "_last_offbus_state", {})
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", 0.0)
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_failed", False)
    # Whether the last reset EXITED non-zero. Left set it would steer the NEXT test's gate
    # escalation to the cold rung over a reset that never hard-failed. raising=False so a base tree
    # without the flag (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_exit_nonzero", False, raising=False)
    # The exit code of the last launched reset. Left set, a test whose stub reset never sets it would
    # record a failed reset in the boot's record (spec 04 I58) and skip the next test's reset.
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_rc", None, raising=False)
    for flag in ("last_reset_driver_wait_failed", "last_reset_tray_repower", "last_reset_rescan_withheld"):
        monkeypatch.setattr(srv.recovery_mechanism, flag, False, raising=False)
    # A host reboot/power cycle a test requested (with a stub fire) must not suppress the NEXT
    # test's forced escalation. raising=False so a base tree without the marker still sets up.
    monkeypatch.setattr(srv.recovery_mechanism, "host_action_requested", None, raising=False)
    # A failed-launch page a test sent must not leak into the NEXT test.
    if srv.galaxy_recovery is not None:
        monkeypatch.setattr(srv.galaxy_recovery, "_cycle_launch_paged", None, raising=False)
        monkeypatch.setattr(srv.galaxy_recovery, "_sbr_hold", None, raising=False)
    monkeypatch.setattr(srv, "_host_action_suppressed_logged", None, raising=False)
    monkeypatch.setattr(srv, "last_fabric_check_monotonic", 0.0)
    # When the last job's device work ended, the anchor the inter-job cooldown measures from.
    # Left set, a prior test's value would make the next runner test wait (or not) unexpectedly.
    # raising=False so a base tree without the cooldown (a fails-on-base stash) still sets up.
    monkeypatch.setattr(srv, "last_job_end_monotonic", 0.0, raising=False)
    # The eth self-test retry's latches. Left set by a test that ran the self-test, any later test
    # that ticks the idle sampler on a HEALTHY device would start a real retry task. raising=False
    # so a base tree without the retry (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv, "eth_rearm_retryable", False, raising=False)
    monkeypatch.setattr(srv, "_last_eth_rearm_monotonic", 0.0, raising=False)
    monkeypatch.setattr(srv, "_eth_rearm_task", None, raising=False)
    monkeypatch.setattr(srv, "eth_rearm_no_reader", False, raising=False)
    monkeypatch.setattr(srv, "eth_check_armed_late", None, raising=False)
    monkeypatch.setattr(srv, "_eth_selftest_runs", 0, raising=False)
    # The owed startup fabric pass (spec 03 I39): left set, a later test's 77-only hold that
    # happened to share its `since` would arm a relift it never asked for.
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", None, raising=False)
    monkeypatch.setattr(srv, "_startup_prereq_task", None, raising=False)
    # The startup gate task (spec 03 I75): left pending, a later test's startup hold would never escalate.
    monkeypatch.setattr(srv, "_startup_verify_task", None, raising=False)
    monkeypatch.setattr(srv, "_startup_verify_ready_at", None, raising=False)
    monkeypatch.setattr(srv, "_startup_verify_started_wall", None, raising=False)
    monkeypatch.setattr(srv, "_startup_verify_ready_wall", None, raising=False)
    monkeypatch.setattr(srv, "_startup_verify_deferral_logged", None, raising=False)
    # The 10-min hold bound's bookkeeping (spec 04 I76) is keyed by the episode's start stamp: two tests
    # that open a hold at the same fixed stamp would share one ladder record.
    for name, value in (
        ("_hold_bound_episode", ""),
        ("_hold_bound_ladder_age", None),
        ("_hold_bound_ladder_recovered", False),
        # Why the last host-rung request skipped the ladder: left set, a later test's request at the same
        # (empty) episode would name a prior test's skip instead of its own cause.
        ("_hold_bound_skip", ""),
        ("_hold_bound_early", False),
        ("_hold_bound_granted_age", None),
        ("_hold_bound_cycle_age", None),
        ("_hold_bound_check_failed_age", None),
        ("_hold_bound_unarmed_paged", ""),
        ("_hold_bound_rung_pending", False),
        ("_hold_bound_denied_why", ""),
        ("_hold_bound_tenant_paged", ""),
        ("_hold_bound_tenant_blocker", None),
        # The holders the dead-chip valve refused to kill (spec 03 I84), which the bound routes.
        ("_kill_refused_pids", {}),
        ("_kill_refused_job_pid", None),
        ("_kill_refused_since", ""),
        ("_pci_wedged_asked", None),
    ):
        monkeypatch.setattr(srv, name, value, raising=False)
    monkeypatch.setattr(srv, "post_job_gate_pending", False, raising=False)
    # The per-owner burst-cap ledger. Left populated, one test's submissions would count against
    # the next test's cap. Fresh dict so state never bleeds; raising=False so a base tree without
    # the cap (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv, "owner_submit_times", {}, raising=False)
    # The idle all-gone debouncer: left at 1 from a prior test, the next sampler test would
    # confirm a sysfs blackout on its FIRST empty sample instead of its second. raising=False so
    # a base tree without the counter (a fails-on-base stash) still sets up cleanly.
    monkeypatch.setattr(srv.sampler, "all_chips_gone_strikes", 0)
    # The short-count debouncer and the dispatch recheck's clock (spec 03 I31/I32), for the same
    # reason. raising=False so a base tree without them (a fails-on-base stash) still sets up.
    monkeypatch.setattr(srv.sampler, "short_count_strikes", 0, raising=False)
    # The hung-chip poller stop's per-episode latch and its previous heartbeats, and the reset scope
    # the sampler saw still running (spec 03 I66), for the same reason.
    monkeypatch.setattr(srv.sampler, "pollers_stopped_for", frozenset(), raising=False)
    monkeypatch.setattr(srv.sampler, "pollers_deferred_for", frozenset(), raising=False)
    monkeypatch.setattr(srv.sampler, "prev_beats", {}, raising=False)
    monkeypatch.setattr(srv.sampler, "stall_strikes", {}, raising=False)
    monkeypatch.setattr(srv.sampler, "hung_held", frozenset(), raising=False)
    monkeypatch.setattr(srv.sampler, "scope_seen", False, raising=False)
    # The valve runs in flight and the chips they read all-ones or removed (spec 03 I94).
    monkeypatch.setattr(srv.sampler, "valve_runs", [], raising=False)
    monkeypatch.setattr(srv.sampler, "last_sample_all_ones", [], raising=False)
    monkeypatch.setattr(srv.sampler, "valve_detaching", set(), raising=False)
    monkeypatch.setattr(srv.sampler, "valve_removed", set(), raising=False)
    monkeypatch.setattr(srv, "_last_dispatch_recheck_at", None, raising=False)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    # A link drop an eth test measured on the singleton would turn the next test's 77 hold into a fault.
    monkeypatch.setattr(srv.health_monitor, "last_eth_link_drop", "", raising=False)
    monkeypatch.setattr(srv, "_fabric_ok_retire_monotonic", 0.0)
    monkeypatch.setattr(srv, "_fabric_ok_retire_streak", 0)
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0.0)  # no real eth-training wait in tests
    # Spec 04 I94: a chip a stubbed bridge reset brings back has a live ARC unless the test says not
    # (tests/test_bridge_reset_arc_dead.py reads real fake-sysfs heartbeats).
    monkeypatch.setattr(recovery_pkg, "arc_live_chips", lambda ids: sorted({str(i) for i in ids}, key=int))
    monkeypatch.setattr(
        recovery_galaxy, "_settle_before_host_rung_sec", lambda: 0
    )  # no real pre-host-rung settle in tests

    async def _no_gap_sleep(seconds):
        return None

    # No real wait for the spaced second mesh reset (spec 04 I93); its tests move a fake clock instead.
    monkeypatch.setattr(recovery_galaxy, "_mesh_reset_gap_sleep", _no_gap_sleep)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_op_detail", "")
    monkeypatch.setattr(srv, "device_op_owner", "[broker]", raising=False)
    # A runner test cancelled mid-job never reaches the runner's own cleanup, so the dispatched
    # job's id stays current; a re-adopt test can leave its scope behind the same way. Either one
    # makes the next test's device look busy (_broker_work_in_flight refuses every step).
    monkeypatch.setattr(srv, "current_job_id", None)
    monkeypatch.setattr(srv, "current_process", None)
    monkeypatch.setattr(srv, "readopted_scopes", {})
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
    # A window given back on a tenant deferral keeps the next forced spawn checking for the tenant
    # first; a leaked flag would make the next test's escalation stand down on its own stub scan.
    monkeypatch.setattr(srv, "device_hold_tenant_deferred", False, raising=False)
    monkeypatch.setattr(srv.galaxy_recovery, "tenant_deferred", False, raising=False)
    monkeypatch.setattr(srv, "device_hold_offbus_prior_bucket", None, raising=False)
    # A carried-over hold one startup test armed must not tag the next test's device_held.
    monkeypatch.setattr(srv, "_carried_hold", None, raising=False)
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

    # The suite must never actually power-cycle the box it runs on. The host rung defaults ON
    # (a ladder nobody armed is a ladder that ends in a permanent hold), so this pins it OFF
    # explicitly — deleting the var would select the armed default and point every gate test at
    # the real thing. The fire below stays a loud tripwire so a test that DOES reach it fails instead
    # of taking CI (or a dev box) down. A test exercising that path re-patches the fire itself.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "0")
    # Arm the automatic mesh reset explicitly; only AUTO_MESH_RESET=0 is the outage hold (spec 04
    # I28). A test of the hold sets it to 0.
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_MESH_RESET", "1")

    # The bounded off-bus hold (spec 04 I27) runs a host-supplied power-cycle command: unset here, a
    # loud tripwire if reached anyway, and its per-process episode tracking starts empty per test.
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", raising=False)

    def _no_offbus_power_cycle_in_tests(*a, **k):
        raise AssertionError("a test reached the real off-bus power cycle; fire_command must be mocked")

    monkeypatch.setattr(offbus_power_cycle, "fire_command", _no_offbus_power_cycle_in_tests)
    # The OS-first power cycle (spec 04 I88) reads the installed shutdown hook: on a box that has it,
    # a test reaching the real fire would queue a reboot. Here it reads as not installed, and every
    # step it would spawn is a loud tripwire; the I88 tests re-patch both.
    monkeypatch.setattr(power_cycle_stage, "installed_shutdown_hook", lambda: "")

    def _no_os_first_step_in_tests(*a, **k):
        raise AssertionError("a test reached a real OS-first power-cycle step; _run_step must be mocked")

    monkeypatch.setattr(power_cycle_stage, "_run_step", _no_os_first_step_in_tests)
    monkeypatch.setattr(offbus_power_cycle, "_pending", {})

    # The tray-down prelude (spec 04 I19) rescans the bus and reads the BMC: never the real sysfs
    # or ipmitool from the suite. Its reads answer "not run in tests", so every gate/ladder test
    # still passes through the prelude unchanged. Its onset latch lives on the module-global
    # GalaxyRecovery, so every test starts with none.
    def _no_rescan_in_tests():
        raise OSError("a test reached the real PCI rescan; _pci_rescan must be mocked")

    def _no_bmc_reads_in_tests(argv, **k):
        return subprocess.CompletedProcess(argv, 0, stdout="not run in tests\n", stderr="")

    monkeypatch.setattr(recovery_galaxy, "_pci_rescan", _no_rescan_in_tests)
    monkeypatch.setattr(bmc_capture, "_RUN", _no_bmc_reads_in_tests)
    if getattr(srv, "galaxy_recovery", None) is not None:
        srv.galaxy_recovery.tray_down_episode_end()

    # The full check's tt-smi snapshot runs tracked, in its own session (spec 03 I86). The suite's
    # snapshot fakes stand in for `subprocess.run`, so here the runner delegates to it; the I86 tests
    # of the tracked runner itself restore the real one (REAL_RUN_SNAPSHOT).
    monkeypatch.setattr(
        HealthMonitor,
        "_run_snapshot",
        lambda self, argv, timeout_sec: subprocess.run(argv, capture_output=True, text=True, timeout=timeout_sec),
        raising=False,
    )

    _install_spawn_tripwire(monkeypatch, allow_device_spawns=device_marked)


# The tracked snapshot runner the fixture above swaps out (spec 03 I86).
REAL_RUN_SNAPSHOT = getattr(HealthMonitor, "_run_snapshot", None)


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
# The units a broker runs its jobs and direct execs in (spec 09 I10). On a broker host the live
# broker's ttdev-job-<id>.scope can share its id with a test's job, so no test may reach them:
# a query reads none live, an action or a systemd-run in that namespace is refused, marker or not.
_BROKER_UNIT_SAMPLES = ("ttdev-job-1.scope", "ttdev-exec-1-1.scope")
_READ_ONLY_SYSTEMCTL_VERBS = ("is-active", "is-failed", "is-enabled", "status", "show", "list-units", "cat")


def _names_broker_unit(parts) -> bool:
    """Whether a systemctl/systemd-run argv names a broker job or exec unit, a glob included."""
    if not any(os.path.basename(p) in ("systemctl", "systemd-run") for p in parts):
        return False
    for tok in parts:
        if tok.startswith("-"):
            if "=" not in tok:
                continue
            tok = tok.split("=", 1)[1]
        if "ttdev-job-" in tok or "ttdev-exec-" in tok:
            return True
        if any(ch in tok for ch in "*?[") and any(fnmatch.fnmatchcase(u, tok) for u in _BROKER_UNIT_SAMPLES):
            return True
    return False


def _argv_parts(argv) -> list:
    if isinstance(argv, (str, bytes)):
        return (argv.decode() if isinstance(argv, bytes) else argv).split()
    return [a.decode() if isinstance(a, bytes) else str(a) for a in argv]


def _is_broker_unit_query(argv) -> bool:
    """A read-only systemctl naming a broker unit: it runs, but never against the host (I10)."""
    parts = _argv_parts(argv)
    return _names_broker_unit(parts) and _spawn_is_forbidden(parts, allow_device_spawns=True) is None


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
    parts = _argv_parts(argv)
    if not parts:
        return None
    broker_unit = _names_broker_unit(parts)
    for tok in parts:
        prog = os.path.basename(tok)
        if prog in _NEVER_SPAWN:
            return f"{prog} would reboot, power-cycle, or escape this test run"
        if prog == "systemd-run" and broker_unit:
            return "systemd-run would start a unit in the host broker's ttdev-job/ttdev-exec namespace"
        if prog in _DEVICE_ONLY_SPAWN and not allow_device_spawns:
            return f"{prog} touches the device; declare @pytest.mark.device or assert on the argv"
        if prog == "systemctl":
            at = parts.index(tok)
            if broker_unit:
                # Fail closed: on a broker unit only a known query verb runs (answered by the
                # stand-in); every other verb, kill/stop or one not listed here, is refused.
                rest = {os.path.basename(p) for p in parts[at + 1 :]}
                if not rest & set(_READ_ONLY_SYSTEMCTL_VERBS) or rest & set(_FORBIDDEN_SYSTEMCTL_VERBS):
                    return "systemctl would act on the host broker's live job or exec unit"
                return None
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

    A read-only systemctl naming a broker job or exec unit (spec 09 I10) is not refused but
    answered here, marker or not: it runs a stand-in that prints nothing and exits 3 (systemctl's
    "inactive"), so every caller reads no such unit live and never the host broker's. A test that
    means a live scope stubs the query (`srv._scope_active`, `srv.list_active_job_scopes`) or
    `subprocess.run` itself. asyncio's subprocess transport spawns through Popen, so it is
    answered the same way.
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
        if _is_broker_unit_query(args):
            k.pop("executable", None)
            a = a[:1] + (None,) + a[2:] if len(a) >= 2 else a
            args = "exit 3" if k.get("shell") else ["/bin/sh", "-c", "exit 3"]
        return real_popen_init(self, args, *a, **k)

    real_exec = asyncio.create_subprocess_exec

    async def guarded_exec(program, *args, **k):
        _refuse([program, *args], "asyncio.create_subprocess_exec")
        return await real_exec(program, *args, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)


def _reset_server_state():
    """Drop the broker's process-wide job and hold state (see init_async_primitives)."""
    _reset_for_testing()
    srv.job_runner_task = None
    srv.stats_task = None
    srv.runner_job_id = None
    jobs.clear()


@pytest.fixture(autouse=True)
def init_async_primitives():
    """Give every test fresh broker job and hold state, and fresh async primitives.

    The queue, lock, runner and stats tasks, the jobs dict and the hold/action rows, hold-episode
    latches, the hold-escalation buckets, latches and kind, the spec 04 I76 hold-bound latches, the
    shared recovery mechanism's class-A ladder anchor and kill-reset debt are module globals. Left
    over, they steer the next test: a queue bound to the previous test's closed loop fails the next
    lifespan's job runner (which then marks the device dirty and holds it), a hold row one test
    opened shows up in another's recent-jobs list, and a bound host-rung latch one test set skips
    the next test's ladder. Everything _reset_for_testing names is reset around every test (spec 09
    I7); a new global of this kind goes there too. Tasks a test started die with its own loop."""
    _reset_server_state()
    _ensure_async_primitives()
    yield
    _reset_server_state()


@pytest.fixture
def clear_job_state(init_async_primitives):
    """Drain the job queue before and after a runner test (the autouse reset does the rest)."""
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
    ``srv._auto_power_cycle_enabled`` still takes effect through a Recovery (or HealthMonitor) built
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
        auto_power_cycle_enabled=lambda: srv._auto_power_cycle_enabled(),
        board_types_provider=lambda: srv.health_monitor._board_types,
        glx_board_types_provider=lambda: srv.health_monitor._glx_board_types(),
        chip_buses_provider=lambda: srv.health_monitor._chip_buses,
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
        clear_device_reported_fault=lambda why: srv._clear_device_reported_fault(why),
        clear_device_dirty=lambda **kw: srv._clear_device_dirty(**kw),
        note_fabric_77_reset_passes=lambda n: srv._note_fabric_77_reset_passes(n),
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
    # The tray map keys on {chip id: bus} (I16). This fixture's host numbers its chips in PCI order;
    # tests/test_ubb_tray_map.py pins the real Blackhole kernel order, where 0xCX comes before 0x8X.
    monkeypatch.setattr(srv.health_monitor, "_chip_buses", dict(enumerate(GALAXY_BUS_IDS)), raising=False)
    monkeypatch.setattr(srv.health_monitor, "_board_types", ["tt-galaxy-bh"] * len(GALAXY_BUS_IDS))
    stub_device_pollers(monkeypatch)
    return srv.galaxy_recovery._tray_map_now()


def stub_device_pollers(monkeypatch, stopped=()) -> list:
    """Replace the poller stop/start (a real `systemctl stop`, which the spawn tripwire refuses) with
    a recorder: the per-tray walk quiesces the pollers across its re-powers. ``stopped`` is what the
    stop reports it touched (empty: nothing to restore). Returns the list of `active` flags it was
    called with."""
    calls: list = []

    async def pollers(active, log):
        calls.append(active)
        return list(stopped)

    monkeypatch.setattr(srv, "_set_device_pollers", pollers)
    return calls


def patch_health_event(monkeypatch, fn) -> None:
    """Patch ``health_event`` everywhere the recovery ladder can call it.

    ``server.py``, ``health.recovery`` (the platform-invariant verify/reset loop), and
    ``health.recovery.galaxy`` (the escalation ladder) each hold their OWN
    ``from health.evidence import health_event`` binding — an import copies the reference, so
    patching one module's copy never touches another's. A test that wants to see (or silence) an
    event any of these bodies emits has to patch all three, and ``pcie_guard`` (the tray re-power
    envelope and the tray rung's disable, which the walk reaches), and ``base`` (the outage hold)."""
    monkeypatch.setattr(srv, "health_event", fn)
    monkeypatch.setattr(srv, "health_event_detached", fn)
    monkeypatch.setattr(srv, "health_event_flushed", lambda kind, timeout, **f: fn(kind, **f) or True)
    monkeypatch.setattr(recovery_pkg, "health_event", fn)
    monkeypatch.setattr(recovery_galaxy, "health_event", fn)
    monkeypatch.setattr(pcie_guard, "health_event", fn)
    monkeypatch.setattr(recovery_base, "health_event", fn)
    monkeypatch.setattr(post_reset, "health_event", fn)


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


@pytest.fixture
def armed_power_cycle(monkeypatch):
    """The host rung (the BMC power cycle, spec 04 I71) armed with a recording stub fire: the list it
    returns gets one entry per power cycle the broker fires. Nothing real is spawned."""
    from tt_device_mcp import privileges

    fired = []
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    monkeypatch.setattr(privileges, "can_ipmi", lambda: True)
    monkeypatch.setattr(srv, "_fire_power_cycle", lambda: fired.append("power-cycle"))
    return fired
