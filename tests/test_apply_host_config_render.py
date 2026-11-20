# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The systemd unit `apply-host-config.sh` renders, exercised through its `--print-unit`
seam so no root and no host paths are involved.

The load-bearing assertion is the eth-heartbeat wiring: an operator arms the passive
eth-core heartbeat pre-read per host by setting TTDEV_ETH_HEARTBEAT_CMD in /etc/default,
and that value has to reach the broker's unit as TT_DEVICE_MCP_ETH_HEARTBEAT_CMD (the env
var server.py keys on) or the detector can never be turned on without a code change. Its
default is UNSET — no line — so wiring it changes nothing until a host opts in; the inert
case is pinned alongside so the wiring cannot start rendering a line by accident.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests.deploy_helpers import _APPLY_SCRIPT, _GALAXY, _REPO, _one, _render, _statements
from tt_device_mcp.health.recovery import base as recovery_base

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "apply-host-config.sh"


def test_eth_heartbeat_unset_renders_no_line(tmp_path):
    # The default: nothing set AND no staged reader under ROOT. The broker sees no
    # TT_DEVICE_MCP_ETH_HEARTBEAT_CMD, so verify_eth_heartbeat stays inert and the health gate is
    # unchanged. ROOT is sandboxed here because the docstring's "no host paths" promise is what makes
    # this case meaningful: against a real install the staged-reader auto-wire below fires instead,
    # which is the intended behaviour rather than an unset host.
    unit = _render(tmp_path, dict(_GALAXY, TTDEV_ROOT=str(tmp_path)))
    assert "TT_DEVICE_MCP_ETH_HEARTBEAT_CMD" not in unit


def test_eth_heartbeat_set_reaches_the_unit(tmp_path):
    # A host that opted in: the /etc/default value must reach the unit under the name the
    # server reads, in the [Service] section (before ExecStart) so it is in the broker's env.
    # Deliberately NOT the retired eth-heartbeat-check.sh's own path (see the migration test
    # below) — this proves an arbitrary operator override survives, a distinct property.
    cmd = "/opt/tt-device-broker/my-custom-eth-check.sh"
    unit = _render(tmp_path, _GALAXY, eth_cmd=cmd)
    line = f"Environment=TT_DEVICE_MCP_ETH_HEARTBEAT_CMD={cmd}"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_eth_heartbeat_pointed_at_the_retired_wrapper_is_scrubbed(tmp_path):
    # Migration (see apply-host-config.sh): a host that had this
    # auto-wired to the now-deleted eth-heartbeat-check.sh before it was retired must not keep
    # rendering it forever — arming TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN=1 there would run a
    # frozen, unmanaged copy of retired code. The render seam cannot touch the real
    # /etc/default (RENDER_ONLY), but the unit itself must still come out clean, exactly as if
    # the value had never been set.
    cmd = "/opt/tt-device-broker/eth-heartbeat-check.sh"  # $ROOT/eth-heartbeat-check.sh, exactly
    unit = _render(tmp_path, _GALAXY, eth_cmd=cmd)
    assert "TT_DEVICE_MCP_ETH_HEARTBEAT_CMD" not in unit


def test_the_render_seam_logs_to_stderr_only_and_writes_no_log_file(tmp_path):
    # The scrub above is the one status line the render seam can emit. It goes through the shared
    # helper (spec 06 I10) but, the seam writing no host path, to stderr only — even with a log dir
    # exported into the environment.
    conf = tmp_path / "default"
    conf.write_text("".join(f"{k}={v}\n" for k, v in _GALAXY.items()))
    logs = tmp_path / "logs"
    r = subprocess.run(
        ["bash", str(_APPLY_SCRIPT), "--print-unit", str(_REPO)],
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TTDEV_ETC_DEFAULT": str(conf),
            "TTDEV_UNIT_PATH": str(tmp_path / "absent.service"),
            "TTDEV_ETH_HEARTBEAT_CMD": "/opt/tt-device-broker/eth-heartbeat-check.sh",
            "TTDEV_LOG_DIR": str(logs),
        },
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert re.search(r"^\S+ \S+ UTC \| apply-host-config \| clearing TTDEV_ETH_HEARTBEAT_CMD", r.stderr, re.M), r.stderr
    assert not logs.exists(), "the render seam writes no log file"


def test_seam_preserves_the_existing_render(tmp_path):
    # Characterization: the render seam must not disturb the other Environment= lines the
    # unit has always carried, or a dry-run test would be validating a different unit than
    # the one the installer writes.
    unit = _render(tmp_path, _GALAXY)
    assert "Environment=TT_DEVICE_MCP_RESET_MODE=galaxy" in unit
    assert "Environment=TT_DEVICE_MCP_FABRIC_CHECK_CMD=/opt/tt-device-broker/fabric-check.sh" in unit
    assert "Environment=TTDEV_FABRIC_DESCRIPTOR=" in unit
    assert "Type=notify" in unit and "WatchdogSec=300" in unit


def test_reset_floor_knobs_unset_render_no_line(tmp_path):
    # Default: neither reset-path knob set. server.py sees no floor and relift OFF, so the
    # recovery path is byte-identical to a host that never had this wiring. Pinned so the
    # seam cannot start rendering a live floor by accident.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC" not in unit
    assert "TT_DEVICE_MCP_SELFHEAL_RELIFT" not in unit


def test_reset_floor_knobs_set_reach_the_unit(tmp_path):
    # A host that opts into the floor: the /etc/default values must reach the unit under the
    # names server.py reads, before ExecStart so they are in the broker's env. Without the
    # wiring these keys are inert (nothing sources /etc/default into the broker process).
    opted = dict(_GALAXY, TTDEV_RESET_MIN_DEAD_FRAC="0.5", TTDEV_SELFHEAL_RELIFT="1")
    unit = _render(tmp_path, opted)
    floor = "Environment=TT_DEVICE_MCP_RESET_MIN_DEAD_FRAC=0.5"
    relift = "Environment=TT_DEVICE_MCP_SELFHEAL_RELIFT=1"
    assert floor in unit
    assert relift in unit
    assert unit.index(floor) < unit.index("ExecStart=")
    assert unit.index(relift) < unit.index("ExecStart=")


def test_expected_chips_unset_renders_no_line(tmp_path):
    # A host nothing can identify (no galaxy reset mode, board type or DMI product): no per-host
    # count, and expected() falls back to the high-water mark. Pinned so the seam never guesses.
    # A Galaxy derives 32 instead (spec 08 I28, tests/test_deploy_expected_chips.py).
    unit = _render(tmp_path, {k: v for k, v in _GALAXY.items() if k != "TTDEV_RESET_MODE"})
    assert "TT_DEVICE_MCP_EXPECTED_CHIPS" not in unit


def test_a_galaxy_reset_mode_alone_renders_32(tmp_path):
    unit = _render(tmp_path, _GALAXY)
    assert "Environment=TT_DEVICE_MCP_EXPECTED_CHIPS=32\n" in unit


def test_expected_chips_set_reaches_the_unit(tmp_path):
    # A host that pins its mesh: the /etc/default count must reach the unit under the name
    # expected_chip_count() reads, before ExecStart so it is in the broker's env — otherwise the
    # bar is derived from a high-water mark that a degraded broker-start can bake too low.
    opted = dict(_GALAXY, TTDEV_EXPECTED_CHIPS="32")
    unit = _render(tmp_path, opted)
    line = "Environment=TT_DEVICE_MCP_EXPECTED_CHIPS=32"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_cold_rung_knobs_unset_render_no_line(tmp_path):
    # Default: neither cold rung armed. server.py's _auto_reboot_allowed / _auto_power_cycle_allowed
    # both read "0", so the cascade tops out at the rung below them exactly as before this wiring — no
    # host gains an automatic destructive rung. Pinned so the seam cannot start arming one by accident.
    unit = _render(tmp_path, _GALAXY)
    assert "TT_DEVICE_MCP_AUTO_REBOOT" not in unit
    assert "TT_DEVICE_MCP_AUTO_POWER_CYCLE" not in unit


def test_cold_rung_knobs_set_reach_the_unit(tmp_path):
    # A host that arms the top of the cascade: the /etc/default opt-in must reach the unit under the
    # name server.py reads, before ExecStart so it is in the broker's env — otherwise the cold
    # rung is unreachable in the deployed config and an all-off-bus mesh holds with no recovery path.
    unit = _render(tmp_path, dict(_GALAXY, TTDEV_AUTO_POWER_CYCLE="1"))
    power = "Environment=TT_DEVICE_MCP_AUTO_POWER_CYCLE=1"
    assert power in unit
    assert unit.index(power) < unit.index("ExecStart=")


def test_a_stale_reboot_setting_renders_no_line(tmp_path):
    # Spec 04 I71: the warm host reboot rung is gone, with no switch left. A TTDEV_AUTO_REBOOT still
    # in an old /etc/default file renders nothing into the unit.
    unit = _render(tmp_path, dict(_GALAXY, TTDEV_AUTO_REBOOT="1", TTDEV_AUTO_POWER_CYCLE="1"))
    assert "AUTO_REBOOT" not in unit
    assert "POST_REBOOT_VERIFY" not in unit


@pytest.mark.parametrize(
    "name",
    [
        # Default: the pre-job dispatch probe is opted out. server.py's _prejob_dispatch_enabled() reads
        # "0", so the gate never runs the probe and admits exactly as before this wiring. Pinned so the
        # seam cannot start arming an untimed probe by accident — an untimed probe that runs slow reads as
        # a false wedge and holds the box, the reboot-class failure the opt-in default exists to prevent.
        "TT_DEVICE_MCP_PREJOB_DISPATCH",
        # Default: no line, so server.py's built-in poller list (no clock-cap keeper) rules.
        "TT_DEVICE_MCP_POLLER_SERVICES",
        "TTDEV_ETH_CHECK_PYTHON",
    ],
)
def test_an_unset_knob_renders_no_line(tmp_path, name):
    unit = _render(tmp_path, _GALAXY)
    assert name not in unit


def test_prejob_dispatch_set_reaches_the_unit(tmp_path):
    # A host that has timed the probe and opts in: the /etc/default value must reach the unit under the
    # name _prejob_dispatch_enabled() reads, before ExecStart so it is in the broker's env — otherwise
    # the only way to arm it is to hand-edit the unit, which the next auto-update re-render silently
    # wipes, and clause G1 ("observed running in a real job log") can never durably hold.
    opted = dict(_GALAXY, TTDEV_PREJOB_DISPATCH="1")
    unit = _render(tmp_path, opted)
    line = "Environment=TT_DEVICE_MCP_PREJOB_DISPATCH=1"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_poller_services_set_reaches_the_unit(tmp_path):
    # A host with yet another device poller overrides the default list via /etc/default; the list
    # must reach the unit verbatim, before ExecStart, or a reset races that poller.
    pollers = "tt-telemetry.service,tt-metrics-exporter.service,tt-fmax-cap.service,site-poller.service"
    unit = _render(tmp_path, dict(_GALAXY, TTDEV_POLLER_SERVICES=pollers))
    line = f"Environment=TT_DEVICE_MCP_POLLER_SERVICES={pollers}"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_eth_check_python_reaches_the_unit(tmp_path):
    # The reader resolves ttexalens from this and calls it the per-box mechanism, but nothing rendered
    # it — so no host could satisfy it and the rung self-tested to OFF everywhere. Documented and
    # unreachable is the same bug as unwired.
    pinned = dict(_GALAXY, TTDEV_ETH_CHECK_PYTHON="/opt/tt/python_env/bin/python")
    unit = _render(tmp_path, pinned)
    line = "Environment=TTDEV_ETH_CHECK_PYTHON=/opt/tt/python_env/bin/python"
    assert line in unit
    assert unit.index(line) < unit.index("ExecStart=")


def test_the_unit_never_orders_after_the_vendor_hugepages_service(tmp_path):
    # tenstorrent-hugepages.service is After=multi-user.target and the broker is ordered before it
    # (WantedBy=), so an After= on it is a boot ordering cycle: systemd deletes the broker's (or
    # a serving unit's) start job to break it. The broker waits for the pool in-process instead.
    unit = _render(tmp_path, _GALAXY)
    deps = [ln for ln in unit.splitlines() if ln.split("=", 1)[0] in ("After", "Wants", "Requires", "BindsTo")]
    assert not [ln for ln in deps if "tenstorrent-hugepages" in ln]
    assert "Wants=dev-hugepages\\x2d1G.mount" in deps


def test_the_unit_lifts_the_memlock_limit_jobs_inherit(tmp_path):
    # Jobs run in a systemd-run --scope that keeps the broker's rlimits; tt-metal pins host memory,
    # which systemd's 8 MiB default LimitMEMLOCK would refuse.
    unit = _render(tmp_path, _GALAXY)
    service = unit.split("[Service]", 1)[1].split("[Install]", 1)[0]
    assert "LimitMEMLOCK=infinity" in service.splitlines()
    assert "TTDEV_ETH_CHECK_PYTHON" not in unit


# --- readable logs and state (spec 06 I9/I10, spec 08) -------------------------------------------


def test_the_rendered_unit_states_umask_0022(tmp_path):
    # systemd's own default, written down so a drop-in cannot narrow it unnoticed — the backstop under
    # the broker's open_readable/readable_mkdir helpers (spec 06 I9).
    unit = _render(tmp_path, _GALAXY)
    lines = [ln.strip() for ln in unit.splitlines()]
    assert "UMask=0022" in lines
    assert lines.index("UMask=0022") < lines.index(next(ln for ln in lines if ln.startswith("ExecStart=")))


def test_the_rendered_unit_sets_no_standard_output_or_standard_error(tmp_path):
    # stdout/stderr reach the journal by systemd's default as a second copy; the file is server.log,
    # written by the broker's own handler (spec 06 I10).
    unit = _render(tmp_path, _GALAXY)
    for ln in unit.splitlines():
        assert not ln.strip().startswith(("StandardOutput=", "StandardError=")), ln


def _code(script: Path) -> list[str]:
    return [s for s in _statements(script) if s.strip() and not s.lstrip().startswith("#")]


def test_the_apply_creates_all_three_state_directories_with_install_d_m_0755_the_parent_included():
    # `install -d -m` applies the mode to the leaf only, so every level is named — never `mkdir -p`,
    # whose mode is the operator's umask (the measured 0750 defect, spec 06 Design decisions).
    stmts = _code(_SCRIPT)
    for path in ("/var/log/tt-device-broker", "/var/lib/tt-device-broker", "/var/lib/tt-device-broker/health"):
        assert [s for s in stmts if s.strip() == f"install -d -m 0755 {path}"], f"no install -d of {path}"
        assert not [s for s in stmts if "mkdir" in s and path in s], f"no mkdir of {path}"


def test_the_apply_creates_the_install_root_and_its_validator_dir_with_install_d_m_0755():
    # The validator's build.log is readable only if the path above it is traversable (spec 08).
    _one(_SCRIPT, 'install -d -m 0755 "$ROOT"')
    _one(_SCRIPT, 'install -d -m 0755 "$ROOT/validator"')


def test_the_apply_creates_the_directories_before_the_first_unit_start():
    stmts = _code(_SCRIPT)
    last_mkdir = max(i for i, s in enumerate(stmts) if "install -d -m 0755" in s and "tt-device-broker" in s)
    first_start = min(i for i, s in enumerate(stmts) if "systemctl" in s and ("enable --now" in s or " start " in s))
    assert last_mkdir < first_start


def test_the_directory_creation_and_the_sweep_sit_under_the_render_only_seam():
    # `--print-unit` touches no host path: the install -d lines and the sweep must be inside the
    # `if [ -z "$RENDER_ONLY" ]` block that precedes the unit render.
    stmts = _code(_SCRIPT)
    guards = [i for i, s in enumerate(stmts) if s.strip() == 'if [ -z "$RENDER_ONLY" ]; then']
    render = next(i for i, s in enumerate(stmts) if 'cat > "$unit_dest"' in s)
    for needle in (
        "install -d -m 0755 /var/log/tt-device-broker",
        'install -d -m 0755 "$ROOT/validator"',
        "sweep_to_mode",
    ):
        for i, s in enumerate(stmts):
            if needle not in s:
                continue
            guard = max((g for g in guards if g < i), default=None)
            assert guard is not None, f"{s!r} has no RENDER_ONLY guard before it"
            end = next(j for j, t in enumerate(stmts) if j > guard and t.strip() == "fi")
            assert i < end < render, f"{s!r} is outside the RENDER_ONLY guard that precedes the render"


def test_the_apply_stages_log_sh_beside_the_scripts_that_source_it():
    # Staged by apply-host-config.sh as well as by the installer, like fabric-check.sh and the Slurm
    # hooks: auto-update never re-runs the installer, only this script (spec 08 I8).
    stmt = _one(_SCRIPT, "install -m 0755", '"$DEPLOY/tt-device-log.sh"', '"$ROOT/log.sh"')
    assert "|| true" not in stmt


def _lmode(p: Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


def _gnu_find() -> bool:
    """The deploy scripts target GNU/Linux (spec 09); `-ignore_readdir_race` is GNU find's. An
    executed sweep test skips where find is not GNU, the way a device test skips without silicon."""
    try:
        return subprocess.run(["find", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


_MODES = _SCRIPT.parent / "tt-device-modes.sh"
_CRASH = _SCRIPT.parent / "install-crash-recorder.sh"
_SOURCE_MODES = '. "$(dirname "$(readlink -f "$0")")/tt-device-modes.sh"'
_DIRS = 'sweep_to_mode 0755 "$tree" -type d'
_FILES = f'sweep_to_mode 0644 "$tree" -type f ! -name {recovery_base.LOCAL_RESET_LOCK}'
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _sweep(tree: Path, path: str = _PATH) -> subprocess.CompletedProcess:
    """The apply's two sweep statements, exactly as it carries them, over a stand-in tree."""
    body = f'. "{_MODES}"\ntree="$1"\n{_one(_SCRIPT, _DIRS).strip()}\n{_one(_SCRIPT, _FILES).strip()}\n'
    return subprocess.run(
        ["bash", "-e", "-c", body, "_", str(tree)], env={"PATH": path}, capture_output=True, text=True
    )


def _helper_find() -> str:
    m = re.search(r"^\s*find .*?\{\} \+$", _MODES.read_text(), re.S | re.M)
    assert m, "sweep_to_mode must run one find"
    return m.group(0)


def test_the_migration_sweep_widens_dirs_to_0755_and_files_to_0644_skipping_every_local_reset_lock():
    # Recursive under both trees; the lock is skipped by NAME wherever it sits (a redirected health dir
    # inside either tree keeps its lock private too), and nothing but that name is skipped.
    dirs, files = _one(_SCRIPT, _DIRS), _one(_SCRIPT, _FILES)
    assert "local-reset.lock" not in dirs, "the lock's directory is widened like any other"
    assert files.count("! -name") == 1, "the lock is the only name skipped"
    loop = _one(_SCRIPT, "for tree in /var/log/tt-device-broker /var/lib/tt-device-broker")
    stmts = _code(_SCRIPT)
    source = next(i for i, s in enumerate(stmts) if s.strip() == _SOURCE_MODES)
    assert source < stmts.index(loop) < stmts.index(dirs) < stmts.index(files)


def test_both_sweeps_go_through_the_one_helper_and_neither_script_carries_its_own():
    # Spec 08: the race handling lives once, in tt-device-modes.sh. A second hand-written
    # find ... chmod in either caller is a copy that drifts from it.
    for script in (_SCRIPT, _CRASH):
        assert _SOURCE_MODES in [s.strip() for s in _code(script)], f"{script.name} must source the helper"
        own = [s for s in _code(script) if re.search(r"\bfind\b", s) and "chmod" in s]
        assert own == [], f"{script.name} carries its own sweep: {own}"


def test_the_migration_sweep_follows_no_symlink():
    # -P (never -L / -H): stats/current points at a file that is asserted at its own path.
    find = _helper_find()
    assert find.lstrip().startswith('find -P "$root" '), find
    assert " -L " not in find and " -H " not in find and "-follow" not in find, find


def test_the_migration_sweep_tells_find_a_vanished_entry_is_not_an_error():
    # Spec 08: an entry that vanishes between find's readdir and its own stat would otherwise make
    # find exit 1 before -exec runs — the half of the race the per-path body cannot see. The option
    # sits before the caller's tests, so it applies to the whole traversal.
    assert _helper_find().lstrip().startswith('find -P "$root" -ignore_readdir_race "$@" ! -perm "$mode" ')


def test_the_migration_sweep_touches_nothing_on_a_tree_already_at_mode(tmp_path):
    if not _gnu_find():
        pytest.skip("the sweep is GNU find's (-ignore_readdir_race); the statement anchors hold here")
    # Spec 08: the sweep is idempotent. `! -perm` makes that literal — a lap over an at-mode tree
    # issues no chmod, so it never races the live broker's own creates. A chmod stub on PATH that
    # always fails proves it: any chmod at all would exit non-zero.
    tree = tmp_path / "tree"
    (tree / "health" / "incidents" / "b1").mkdir(parents=True)
    for f in (tree / "server.log", tree / "health" / "fsm.json", tree / "health" / "incidents" / "b1" / "job.log"):
        f.write_text("")
        f.chmod(0o644)
    lock = tree / "health" / "local-reset.lock"
    lock.write_text("")
    lock.chmod(0o600)
    for d in [tree, *(p for p in tree.rglob("*") if p.is_dir())]:
        d.chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "chmod").write_text("#!/bin/sh\necho 'chmod: a sweep over an at-mode tree must not chmod' >&2\nexit 1\n")
    (stubs / "chmod").chmod(0o755)

    r = _sweep(tree, f"{stubs}:{_PATH}")
    assert r.returncode == 0 and r.stderr == "", r.stderr
    assert _lmode(lock) == 0o600


def _sweep_with_chmod_stub(tmp_path: Path, tag: str, on_doomed: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """sweep_to_mode over a two-file tree, with a chmod stub that runs `on_doomed` for one file and
    the real chmod for everything else."""
    tree = tmp_path / tag
    tree.mkdir()
    doomed, survivor = tree / "queued-spec-whose-job-started.json", tree / "survivor.json"
    for f in (doomed, survivor):
        f.write_text("")
        f.chmod(0o600)
    stubs = tmp_path / f"stubs-{tag}"
    stubs.mkdir()
    (stubs / "chmod").write_text(
        f'#!/bin/sh\ncase "$2" in *queued-spec-whose-job-started.json) {on_doomed} ;; esac\nexec /bin/chmod "$@"\n'
    )
    (stubs / "chmod").chmod(0o755)
    r = subprocess.run(
        ["bash", "-e", "-c", f'. "{_MODES}"\nsweep_to_mode 0644 "$1" -type f', "_", str(tree)],
        env={"PATH": f"{stubs}:{_PATH}"},
        capture_output=True,
        text=True,
    )
    return r, doomed, survivor


def test_the_migration_sweep_skips_a_path_the_broker_deleted_under_it_and_still_aborts_on_any_other_chmod_failure(
    tmp_path,
):
    if not _gnu_find():
        pytest.skip("the sweep is GNU find's (-ignore_readdir_race); the statement anchors hold here")
    # The broker is live while the apply runs and deletes short-lived files (a queued spec whose job
    # started, fsm.json's .tmp). A chmod stub that deletes its own target and then fails stands in
    # for the broker winning that race: the sweep returns 0 and still brings the survivor to mode. A
    # stub that fails on a path that still EXISTS stands in for EPERM/EROFS: the sweep returns
    # non-zero, so the apply's set -e aborts as spec 08 requires.
    r, doomed, survivor = _sweep_with_chmod_stub(tmp_path, "raced", 'rm -f "$2"; exit 1')
    assert r.returncode == 0, f"a path gone by its chmod must be skipped, not a failure: {r.stderr}"
    assert not doomed.exists() and _lmode(survivor) == 0o644, "the paths that still exist are brought to mode"

    r, doomed, _ = _sweep_with_chmod_stub(tmp_path, "refused", "echo 'chmod: stub refusal' >&2; exit 1")
    assert r.returncode != 0, "a chmod that fails on a path that exists must still fail the apply"
    assert doomed.exists() and "stub refusal" in r.stderr


def test_the_migration_sweep_run_on_a_tree_widens_everything_but_each_local_reset_lock_and_follows_no_symlink(tmp_path):
    if not _gnu_find():
        pytest.skip("the sweep is GNU find's (-ignore_readdir_race); the statement anchors hold here")
    # Nested 0700 directories, a health/x/local-reset.lock at 0600 with a 0600 sibling beside it, a
    # 0600 file at incident-bundle depth, a lock at the tree root, and a stats/current symlink to a
    # 0600 file OUTSIDE the tree: afterwards every directory is 0755, every regular file 0644, every
    # lock still 0600, and the symlink's target untouched.
    tree = tmp_path / "tree"
    outside = tmp_path / "outside-target"
    for d in ("health/x", "incidents/b1/deep", "stats", "queued"):
        (tree / d).mkdir(parents=True)
    files_0600 = [
        tree / "health" / "x" / "sibling.jsonl",
        tree / "incidents" / "b1" / "deep" / "job.log",
        tree / "queued" / "7.json",
        tree / "server.log",
    ]
    locks = [tree / "health" / "x" / "local-reset.lock", tree / "local-reset.lock"]
    for f in files_0600 + locks + [outside]:
        f.write_text("")
        f.chmod(0o600)
    (tree / "stats" / "current").symlink_to(outside)
    for d in [tree, *(p for p in tree.rglob("*") if p.is_dir() and not p.is_symlink())]:
        d.chmod(0o700)

    r = _sweep(tree)
    assert r.returncode == 0, r.stderr

    seen_dirs = [tree] + [p for p in tree.rglob("*") if p.is_dir() and not p.is_symlink()]
    assert len(seen_dirs) == 8
    for d in seen_dirs:
        assert _lmode(d) == 0o755, d
    for f in files_0600:
        assert _lmode(f) == 0o644, f
    for lock in locks:
        assert _lmode(lock) == 0o600, f"{lock} must stay private wherever it sits"
    assert _lmode(outside) == 0o600, "the symlink was not followed"
    assert (tree / "stats" / "current").is_symlink() and os.readlink(tree / "stats" / "current") == str(outside)
