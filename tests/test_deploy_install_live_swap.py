# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I26/I27: an install never takes the venv or the validator away from a live broker.

The installer pieces run against a temp root with stub python/pip/tt-smi/git/cmake on PATH. Every
stub, and every rm/mv/ln the scripts run, first and last checks a fake live consumer: each path a
running broker resolves (``$R/venv/bin/python``, ``validator/current/<binary>``) that existed at
the start must still exist. A step that removes one logs MISSING.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.deploy_helpers import _statements

# Fork-heavy deploy-script laps: one xdist worker runs them all (conftest: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

_REPO = Path(__file__).resolve().parent.parent
_INSTALLER = _REPO / "deploy" / "install-tt-device-broker.sh"
_VENV_LIB = _REPO / "deploy" / "broker-venv.sh"
_VALIDATOR = _REPO / "deploy" / "install-fabric-validator.sh"
_APPLY = _REPO / "deploy" / "apply-host-config.sh"

_BINS = (
    "build/tools/scaleout/run_cluster_validation",
    "build/programming_examples/distributed/distributed_program_dispatch",
    "build/programming_examples/metal_example_add_2_integers_in_compute",
)

_HOOK = """check_live() {
    local p
    for p in $LIVE_PATHS; do [ -x "$p" ] || echo "MISSING $p at $*" >>"$CHECK_LOG"; done
    echo "step $*" >>"$CHECK_LOG"
}
"""

# Venv stubs: bin/python imports succeed once pip installed the repo; tt-smi --help exits TTSMI_HELP_RC.
_VENV_PY = """#!/bin/bash
. "$HOOK"; check_live "venv-python $*"
[ -e "$(dirname "$0")/../.installed" ]
"""
_VENV_PIP = """#!/bin/bash
. "$HOOK"; check_live "pip $*"
d="$(dirname "$0")/.."
case "$1" in
    install) case "$*" in *--upgrade*) ;; *) touch "$d/.installed" ;; esac ;;
    wheel) while [ $# -gt 1 ]; do [ "$1" = -w ] && mkdir -p "$2"; shift; done ;;
esac
check_live "after pip $*"
"""
_VENV_TTSMI = """#!/bin/bash
. "$HOOK"; check_live "tt-smi $*"
exit "${TTSMI_HELP_RC:-0}"
"""


def _wrap(bindir: Path, name: str) -> None:
    real = shutil.which(name)
    (bindir / name).write_text(
        f'#!/bin/bash\n. "$HOOK"; check_live "{name} $*"\n{real} "$@"; rc=$?\ncheck_live "after {name} $*"\nexit $rc\n'
    )
    (bindir / name).chmod(0o700)


def _stub(bindir: Path, name: str, body: str) -> None:
    (bindir / name).write_text(body)
    (bindir / name).chmod(0o700)


def _env(tmp: Path, bindir: Path, live: list[str]) -> dict:
    hook = tmp / "hook.sh"
    hook.write_text(_HOOK)
    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}:{os.environ['PATH']}",
        HOOK=str(hook),
        CHECK_LOG=str(tmp / "check.log"),
        LIVE_PATHS=" ".join(live),
    )
    return env


def _log(tmp: Path) -> list[str]:
    p = tmp / "check.log"
    return p.read_text().splitlines() if p.exists() else []


def _missing(tmp: Path) -> list[str]:
    return [line for line in _log(tmp) if line.startswith("MISSING")]


# ---------------------------------------------------------------- broker venv (I26)


def _venv_bindir(tmp: Path) -> Path:
    bindir = tmp / "stubs"
    bindir.mkdir()
    _stub(
        bindir,
        "python3",
        '#!/bin/bash\n. "$HOOK"; check_live "python3 $*"\n'
        '[ "$1 $2" = "-m venv" ] || exit 99\n'
        f'mkdir -p "$3/bin"; printf "home = /usr/bin\\n" >"$3/pyvenv.cfg"\n'
        f"cat >\"$3/bin/python\" <<'EOF'\n{_VENV_PY}EOF\n"
        f"cat >\"$3/bin/pip\" <<'EOF'\n{_VENV_PIP}EOF\n"
        f"cat >\"$3/bin/tt-smi\" <<'EOF'\n{_VENV_TTSMI}EOF\n"
        'chmod 755 "$3"/bin/*\ncheck_live "after python3 $*"\n',
    )
    for name in ("rm", "mv", "ln"):
        _wrap(bindir, name)
    return bindir


def _installer_venv_block() -> str:
    """The installer's own venv step, verbatim: from its banner to the next one."""
    text = _INSTALLER.read_text()
    start = text.index('echo "==> build venv')
    return text[start : text.index('echo "==> CLI on PATH"', start)]


def _old_venv(r: Path, name: str) -> Path:
    v = r / name
    (v / "bin").mkdir(parents=True)
    for exe in ("python", "tt-smi"):
        (v / "bin" / exe).write_text("#!/bin/sh\n")
        (v / "bin" / exe).chmod(0o755)
    return v


def _run_venv(tmp: Path, script: str, live: list[str], **extra) -> subprocess.CompletedProcess:
    r, src = tmp / "root", tmp / "src"
    r.mkdir(exist_ok=True)
    src.mkdir(exist_ok=True)
    env = _env(tmp, tmp / "stubs" if (tmp / "stubs").exists() else _venv_bindir(tmp), live)
    env.update(extra)
    body = f'set -euo pipefail\nR="{r}"; REPO="{_REPO}"\n. "{_VENV_LIB}"\n{script}'
    return subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=60)


def _live_venv_paths(r: Path) -> list[str]:
    return [str(r / "venv" / "bin" / "python"), str(r / "venv" / "bin" / "tt-smi")]


def test_installer_venv_step_never_removes_the_live_venv(tmp_path):
    """The installer's venv step, run as it stands, keeps an upgraded host's live venv whole."""
    r = tmp_path / "root"
    _old_venv(r, "venv.old")
    os.symlink("venv.old", r / "venv")
    block = _installer_venv_block().replace('"$REPO"', '"$SRC"')
    res = _run_venv(tmp_path, f'SRC="{tmp_path / "src"}"\n{block}', _live_venv_paths(r))
    assert res.returncode == 0, res.stderr
    assert _missing(tmp_path) == []
    assert os.readlink(r / "venv") == "venv.old", "the step builds beside the live venv; it does not flip it"


def test_fresh_install_builds_verifies_and_links_a_versioned_venv(tmp_path):
    res = _run_venv(tmp_path, 'n="$(venv_build "$R" "$REPO")"; venv_flip "$R" "$n"; echo "$n"', [])
    assert res.returncode == 0, res.stderr
    r = tmp_path / "root"
    new = Path(res.stdout.strip().splitlines()[-1])
    assert new.parent == r and new.name.startswith("venv.")
    assert os.readlink(r / "venv") == new.name, "a relative link, so the unit's $R/venv paths resolve through it"
    assert os.access(r / "venv" / "bin" / "python", os.X_OK)
    steps = _log(tmp_path)
    verify = [s for s in steps if s.startswith("step venv-python -c import") or s.startswith("step tt-smi --help")]
    assert any("tt_device_mcp.server" in s for s in verify)
    assert any("tt_smi" in s for s in verify)
    assert any(s.startswith("step tt-smi --help") for s in verify)


def test_upgrade_keeps_the_live_venv_until_prune(tmp_path):
    r = tmp_path / "root"
    _old_venv(r, "venv.old")
    os.symlink("venv.old", r / "venv")
    _old_venv(r, "eth-venv")
    res = _run_venv(tmp_path, 'n="$(venv_build "$R" "$REPO")"; venv_flip "$R" "$n"; echo "$n"', _live_venv_paths(r))
    assert res.returncode == 0, res.stderr
    assert _missing(tmp_path) == []
    new = Path(res.stdout.strip().splitlines()[-1]).name
    assert os.readlink(r / "venv") == new
    assert (r / "venv.old").is_dir(), "the old venv stays until the restarted broker answers /health"
    res = _run_venv(tmp_path, 'venv_prune "$R"', _live_venv_paths(r))
    assert res.returncode == 0, res.stderr
    assert not (r / "venv.old").exists()
    assert (r / new).is_dir() and (r / "eth-venv").is_dir()
    assert _missing(tmp_path) == []


def test_reinstall_of_the_same_commit_never_builds_over_the_live_venv(tmp_path):
    r = tmp_path / "root"
    res = _run_venv(tmp_path, 'n="$(venv_build "$R" "$REPO")"; venv_flip "$R" "$n"; echo "$n"', [])
    first = Path(res.stdout.strip().splitlines()[-1]).name
    res = _run_venv(tmp_path, 'n="$(venv_build "$R" "$REPO")"; echo "$n"', _live_venv_paths(r))
    assert res.returncode == 0, res.stderr
    second = Path(res.stdout.strip().splitlines()[-1]).name
    assert second != first and os.readlink(r / "venv") == first
    assert _missing(tmp_path) == []


def test_a_venv_that_fails_verification_is_discarded_and_the_live_one_kept(tmp_path):
    r = tmp_path / "root"
    _old_venv(r, "venv.old")
    os.symlink("venv.old", r / "venv")
    res = _run_venv(tmp_path, 'venv_build "$R" "$REPO" && echo built', _live_venv_paths(r), TTSMI_HELP_RC="1")
    assert res.returncode != 0 and "built" not in res.stdout
    assert "tt-smi --help failed" in res.stderr
    assert sorted(p.name for p in r.iterdir()) == ["venv", "venv.old"]
    assert os.readlink(r / "venv") == "venv.old"
    assert _missing(tmp_path) == []


def test_migration_from_a_real_dir_venv_has_only_the_two_rename_gap(tmp_path):
    r = tmp_path / "root"
    _old_venv(r, "venv")
    (r / "venv" / "marker").write_text("legacy")
    res = _run_venv(tmp_path, 'n="$(venv_build "$R" "$REPO")"; venv_flip "$R" "$n"; echo "$n"', _live_venv_paths(r))
    assert res.returncode == 0, res.stderr
    new = Path(res.stdout.strip().splitlines()[-1]).name
    assert (r / "venv").is_symlink() and os.readlink(r / "venv") == new
    assert (r / "venv.legacy" / "marker").read_text() == "legacy"
    # The live paths are absent only after the rename aside and before the link's rename in.
    steps = _log(tmp_path)
    gaps = [i for i, s in enumerate(steps) if s.startswith("MISSING")]
    assert gaps, "the consumer sees the rename-aside gap"
    for i in gaps:
        after = next(s for s in steps[i:] if s.startswith("step "))
        assert (
            after.startswith("step after mv -T") and after.endswith("venv.legacy") or after.startswith("step mv -Tf")
        ), after
    nxt = [s for s in steps if s.startswith("step mv")]
    assert nxt[-1].startswith("step mv -Tf") and nxt[-1].endswith(f"{r}/venv")
    res = _run_venv(tmp_path, 'venv_prune "$R"', _live_venv_paths(r))
    assert not (r / "venv.legacy").exists() and (r / new).is_dir()


def test_installer_flips_before_host_config_and_prunes_after_health():
    stmts = [s for s in _statements(_INSTALLER) if not s.lstrip().startswith("#")]

    def idx(*needles):
        hits = [i for i, s in enumerate(stmts) if all(n in s for n in needles)]
        assert len(hits) == 1, (needles, hits)
        return hits[0]

    assert not [s for s in stmts if 'rm -rf "$R/venv"' in s], "the live venv is never removed in place"
    build = idx("venv_build")
    flip = idx("venv_flip")
    apply = idx("bash", "apply-host-config.sh")
    restart = idx("systemctl restart tt-device-broker")
    health_fail = idx("BROKER_UP", "exit 1")
    prune = idx("venv_prune")
    assert build < flip < apply < restart < health_fail < prune


# ---------------------------------------------------------------- fabric validator (I27)


def _validator_bindir(tmp: Path) -> Path:
    bindir = tmp / "vstubs"
    bindir.mkdir()
    _stub(
        bindir,
        "git",
        '#!/bin/bash\n. "$HOOK"; check_live "git $*"\n'
        '[ "$1" = -C ] && { d="$2"; shift 2; }\n'
        'case "$1" in\n'
        '  init) mkdir -p "$d/.git" ;;\n'
        '  checkout) mkdir -p "$d/cmake" "$d/tt_metal/hw"; : >"$d/cmake/tc.cmake";\n'
        '            printf "set(ARCHS\\n    blackhole\\n)\\n" >"$d/tt_metal/hw/CMakeLists.txt" ;;\n'
        "esac\n"
        'check_live "after git $*"\n',
    )
    bins = " ".join(_BINS)
    _stub(
        bindir,
        "cmake",
        '#!/bin/bash\n. "$HOOK"; check_live "cmake $*"\n'
        'if [ "$1" = --build ]; then\n'
        '  p="$(dirname "$2")"\n'
        f'  for b in {bins}; do mkdir -p "$p/$(dirname $b)"; printf \'#!/bin/sh\\n\' >"$p/$b"; chmod 755 "$p/$b"; done\n'
        "else\n"
        '  while [ $# -gt 0 ]; do [ "$1" = -B ] && mkdir -p "$2"; shift; done\n'
        "fi\n"
        'check_live "after cmake $*"\n',
    )
    _stub(bindir, "ninja", "#!/bin/sh\n")
    for name in ("rm", "mv", "ln"):
        _wrap(bindir, name)
    return bindir


def _pin(tmp: Path, sha: str) -> Path:
    pin = tmp / f"pin-{sha}"
    pin.write_text(
        "TTDEV_VALIDATOR_REPO=https://example.invalid/tt-metal.git\n"
        f"TTDEV_VALIDATOR_SHA={sha}\n"
        "TTDEV_VALIDATOR_TOOLCHAIN=cmake/tc.cmake\n"
        "TTDEV_VALIDATOR_TARGET=a\nTTDEV_DISPATCH_TARGET=b\nTTDEV_PREJOB_DISPATCH_TARGET=c\n"
        f"TTDEV_VALIDATOR_BIN_REL={_BINS[0]}\nTTDEV_DISPATCH_BIN_REL={_BINS[1]}\n"
        f"TTDEV_PREJOB_DISPATCH_BIN_REL={_BINS[2]}\n"
    )
    return pin


def _prefix(root: Path, name: str, bins=_BINS) -> Path:
    p = root / name
    (p / ".git").mkdir(parents=True)
    for b in bins:
        (p / b).parent.mkdir(parents=True, exist_ok=True)
        (p / b).write_text("#!/bin/sh\n")
        (p / b).chmod(0o755)
    return p


def _live_bins(root: Path, bins=_BINS) -> list[str]:
    return [str(root / "current" / b) for b in bins]


def _run_validator(tmp: Path, sha: str, live: list[str], root: str | None = None) -> subprocess.CompletedProcess:
    bindir = tmp / "vstubs" if (tmp / "vstubs").exists() else _validator_bindir(tmp)
    env = _env(tmp, bindir, live)
    env["TTDEV_VALIDATOR_ROOT"] = root or str(tmp / "validator")
    return subprocess.run(
        ["bash", str(_VALIDATOR), str(_pin(tmp, sha))], env=env, capture_output=True, text=True, timeout=60
    )


def _built(p: Path) -> bool:
    return all(os.access(p / b, os.X_OK) for b in _BINS)


def test_validator_fresh_build_publishes_current(tmp_path):
    res = _run_validator(tmp_path, "aaa", [])
    assert res.returncode == 0, res.stderr
    root = tmp_path / "validator"
    assert os.readlink(root / "current") == str(root / "aaa") and _built(root / "current")


def test_validator_upgrade_keeps_the_old_pin_live_until_the_flip(tmp_path):
    root = tmp_path / "validator"
    _prefix(root, "old")
    os.symlink(root / "old", root / "current")
    res = _run_validator(tmp_path, "new", _live_bins(root))
    assert res.returncode == 0, res.stderr
    assert _missing(tmp_path) == []
    assert os.readlink(root / "current") == str(root / "new") and _built(root / "new")
    assert _built(root / "old"), "the old pin stays for a one-line revert"


def test_validator_same_pin_repair_builds_beside_the_live_prefix(tmp_path):
    root = tmp_path / "validator"
    present = _BINS[:2]
    _prefix(root, "aaa", bins=present)  # the pre-job dispatch binary is missing
    os.symlink(root / "aaa", root / "current")
    res = _run_validator(tmp_path, "aaa", _live_bins(root, present))
    assert res.returncode == 0, res.stderr
    assert _missing(tmp_path) == [], "a live broker never loses a validator binary it had"
    assert os.readlink(root / "current") == str(root / "aaa.build") and _built(root / "current")
    assert not [s for s in _log(tmp_path) if f"{root}/aaa/build" in s and s.startswith("step rm")]

    # Within the hour after the flip the old slot may still serve a check that started before it.
    res = _run_validator(tmp_path, "aaa", _live_bins(root))
    assert res.returncode == 0, res.stderr
    assert (root / "aaa").is_dir()
    # An hour on, it is a stale repair source and goes; current is not re-flipped.
    old = time.time() - 3700
    os.utime(root / "current", (old, old), follow_symlinks=False)
    res = _run_validator(tmp_path, "aaa", _live_bins(root))
    assert res.returncode == 0, res.stderr
    assert not (root / "aaa").exists()
    assert os.readlink(root / "current") == str(root / "aaa.build") and _built(root / "current")
    assert _missing(tmp_path) == []


def test_validator_second_repair_returns_to_the_first_slot(tmp_path):
    root = tmp_path / "validator"
    _prefix(root, "aaa.build", bins=_BINS[1:])
    os.symlink(root / "aaa.build", root / "current")
    res = _run_validator(tmp_path, "aaa", _live_bins(root, _BINS[1:]))
    assert res.returncode == 0, res.stderr
    assert _missing(tmp_path) == []
    assert os.readlink(root / "current") == str(root / "aaa")


def test_validator_already_built_pin_does_not_refresh_the_link(tmp_path):
    root = tmp_path / "validator"
    _prefix(root, "aaa")
    os.symlink(root / "aaa", root / "current")
    old = time.time() - 100
    os.utime(root / "current", (old, old), follow_symlinks=False)
    res = _run_validator(tmp_path, "aaa", _live_bins(root))
    assert res.returncode == 0, res.stderr
    assert abs(os.lstat(root / "current").st_mtime - old) < 1, "the link's mtime dates its last real flip"
    assert not [s for s in _log(tmp_path) if s.startswith("step cmake")]


def _repair_under_respelled_root(tmp: Path, spelled: str, root: Path) -> None:
    # `current` was written by a run that spelled the root as `root`; this run spells it `spelled`.
    present = _BINS[:2]
    _prefix(root, "aaa", bins=present)
    os.symlink(root / "aaa", root / "current")
    res = _run_validator(tmp, "aaa", _live_bins(root, present), root=spelled)
    assert res.returncode == 0, res.stderr
    assert _missing(tmp) == [], "a live broker never loses a validator binary it had"
    assert not [s for s in _log(tmp) if s.startswith("step rm") and "/aaa/build" in s], "live slot wiped"
    assert all((root / "aaa" / b).exists() for b in present)
    assert os.path.realpath(root / "current") == os.path.realpath(root / "aaa.build")
    assert _built(root / "current")


def test_validator_repair_with_trailing_slash_root_builds_beside_the_live_prefix(tmp_path):
    root = tmp_path / "validator"
    _repair_under_respelled_root(tmp_path, f"{root}/", root)


def test_validator_repair_with_symlinked_parent_root_builds_beside_the_live_prefix(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    _repair_under_respelled_root(tmp_path, str(tmp_path / "alias" / "validator"), tmp_path / "real" / "validator")


def test_validator_refuses_when_current_resolves_inside_the_build_slot(tmp_path):
    root = tmp_path / "validator"
    _prefix(root, "aaa", bins=_BINS[:2])
    os.symlink(root / "aaa" / "build", root / "current")  # not a slot root: the live slot is unknown
    res = _run_validator(tmp_path, "aaa", [str(root / "aaa" / b) for b in _BINS[:2]])
    assert res.returncode != 0 and "refusing" in res.stderr
    assert not [s for s in _log(tmp_path) if s.startswith(("step rm", "step cmake", "step git"))]
    assert all((root / "aaa" / b).exists() for b in _BINS[:2])


def test_host_config_counts_either_build_slot_as_built():
    text = _APPLY.read_text()
    assert '"/opt/tt-device-broker/validator/$pinned_sha/$pinned_bin"' in text
    assert '"/opt/tt-device-broker/validator/$pinned_sha.build/$pinned_bin"' in text
