# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The deploy scripts must not silently swallow a failed `install` of a REQUIRED artifact.

Doctrine: a missing REQUIRED capability is a hard failure, not a silent skip. The broker's
fabric-check validator is required — a host with no fabric-check falls back to a full board
reset after every abnormal exit — so the installer stages it fatally (`set -e`), and
auto-update refuses to mark the host current if any deploy-script stage fails (otherwise the
host runs a stale script while installed.sha lies that it is current). The eth-heartbeat
pre-read is OPTIONAL, so its staging stays non-fatal — but loud, never `2>/dev/null || true`,
so an operator arming it later can see the artifact never landed.

These pin the fail-closed shape of those specific `install` statements; on base every one of
them carried `2>/dev/null || true` and these assertions fail.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.deploy_helpers import _statements
from tests.test_deploy_autoupdate_no_restart import _base_clone, _commit, _git, _installed, _lap, box  # noqa: F401 - fixture

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_INSTALLER = _DEPLOY / "install-tt-device-broker.sh"
_AUTOUPDATE = _DEPLOY / "tt-device-autoupdate.sh"


def _install_stmts(script: Path, dest: str) -> list[str]:
    """Every `install -m 0755` statement that stages into `dest`."""
    return [s for s in _statements(script) if "install -m 0755" in s and dest in s]


def _install_stmt(script: Path, dest: str) -> str:
    """The single `install -m 0755` statement that stages into `dest`."""
    hits = _install_stmts(script, dest)
    assert len(hits) == 1, f"expected exactly one install of {dest} in {script.name}, got {hits}"
    return hits[0]


def _required_stages() -> list[tuple[str, str]]:
    """The full apply's REQUIRED_STAGES list: (source under deploy/, name under $ROOT)."""
    text = _AUTOUPDATE.read_text()
    body = text[text.index("REQUIRED_STAGES=(") : text.index("\n)\n", text.index("REQUIRED_STAGES=("))]
    return [tuple(x.split(":", 1)) for x in body.split()[1:]]


def _restart_path_state_write(stmts: list[str]) -> int:
    """The installed.sha write of an apply that restarts: the last one, just before the restart.
    The no-restart apply (spec 08 I29) writes it earlier, but stages and installs nothing."""
    return max(i for i, s in enumerate(stmts) if '> "$STATE"' in s or '>"$STATE"' in s)


def test_installer_fabric_check_stage_is_fatal_not_swallowed():
    # Required validator: bare `install` under `set -e` aborts loudly. No `|| true` to
    # swallow it, and no `2>/dev/null` to hide why it failed.
    stmt = _install_stmt(_INSTALLER, '"$R/fabric-check.sh"')
    assert "|| true" not in stmt
    assert "2>/dev/null" not in stmt


def test_installer_eth_heartbeat_stage_is_loud_but_not_fatal():
    # Optional pre-read: not fatal, but not silent either — a failed stage warns on stderr.
    # Only the ttexalens probe itself is still staged; its resolution/classification logic
    # moved in-package (health.monitors.eth) and the wrapper script it used to need is retired.
    stmt = _install_stmt(_INSTALLER, '"$R/eth-heartbeat-probe.py"')
    assert "2>/dev/null || true" not in stmt
    assert "|| true" not in stmt
    assert ">&2" in stmt, "optional stage of the eth-heartbeat probe must be loud (warn to stderr)"
    assert "exit" not in stmt, "optional stage of the eth-heartbeat probe must not be fatal"


def test_autoupdate_required_stages_fail_closed():
    # Every deploy-script stage in auto-update is required: a failed copy must abort before
    # installed.sha marks the host current, never `|| true` past it.
    assert {dest for _, dest in _required_stages()} == {
        "reconcile.sh",
        "client-setup.sh",
        "autoupdate.sh",
        "fabric-check.sh",
        "eth-heartbeat-probe.py",
    }
    # The full apply stages the list in one loop; the no-restart apply (spec 08 I29) stages the
    # updater alone. Each copy is required, so each one fails closed.
    loop = _install_stmts(_AUTOUPDATE, '"$ROOT/${stage#*:}"')
    single = _install_stmts(_AUTOUPDATE, '"$ROOT/autoupdate.sh"')
    assert len(loop) == 1 and len(single) == 1, (loop, single)
    for stmt in loop + single:
        assert "|| true" not in stmt
        assert "exit 1" in stmt, f"a failed stage must exit non-zero: {stmt}"


def test_autoupdate_staged_sources_exist_in_deploy():
    # The shape assertions above pin HOW each artifact stages, not THAT it can: a stage
    # whose source left the tree fails on every host, fail-closed, forever — autoupdate
    # never marks current again. Pin the sources to the tree so retiring a deploy script
    # without dropping its stage breaks here, not on the fleet.
    for src, _ in _required_stages():
        assert (_DEPLOY / src).is_file(), f"autoupdate stages deploy/{src} but the tree does not carry it"
    for stmt in _statements(_AUTOUPDATE):
        for src in re.findall(r'"\$clone/(deploy/[^"$]+)"', stmt):
            assert (_DEPLOY.parent / src).is_file(), (
                f"autoupdate stages {src} but the tree does not carry it; "
                "every stage of a missing source wedges auto-update fleet-wide"
            )
    # The fail-closed contract only holds if the staging block precedes the installed.sha
    # write; if the order flipped, an aborted stage would still have marked the host current.
    stmts = _statements(_AUTOUPDATE)
    stage_idx = next(i for i, s in enumerate(stmts) if "install -m 0755" in s and '"$ROOT/${stage#*:}"' in s)
    sha_idx = _restart_path_state_write(stmts)
    assert stage_idx < sha_idx


def test_autoupdate_host_config_apply_fails_closed():
    # apply-host-config propagates unit/sudoers/reconcile changes; skipping it while the new
    # python package is staged leaves a split-version host. On base the apply was `|| echo ...`
    # and execution fell through to `> "$STATE"` + restart, marking a half-configured host
    # current. The apply must abort (exit non-zero) instead, so installed.sha stays on the
    # last-known-good and the next reconcile retries.
    stmts = _statements(_AUTOUPDATE)
    apply = [s for s in stmts if "apply-host-config.sh" in s and '"$clone"' in s]
    assert len(apply) == 1, f"expected one apply-host-config invocation, got {apply}"
    assert "exit 1" in apply[0], "a failed host-config apply must abort, not fall through to installed.sha"
    assert "|| echo" not in apply[0], "a failed apply must not be swallowed with a bare warning"


def test_autoupdate_host_config_apply_precedes_installed_sha():
    # Same ordering guarantee as the staging block: the apply must run (and its failure abort)
    # before the installed.sha write, or an aborted apply would still mark the host current.
    stmts = _statements(_AUTOUPDATE)
    apply_idx = next(i for i, s in enumerate(stmts) if "apply-host-config.sh" in s and '"$clone"' in s)
    sha_idx = _restart_path_state_write(stmts)
    assert apply_idx < sha_idx


# The staging block of an updater from before the AICLK ceiling's removal, verbatim. A host on
# that version runs this block from its own installed copy against the new tree, so every file it
# names must still be in deploy/, or the upgrade stops before installed.sha moves.
_OLD_STAGING = r"""
    install -m 0755 "$clone/deploy/tt-device-reconcile.sh"    "$ROOT/reconcile.sh" \
        || { echo "autoupdate: staging reconcile.sh failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-client-setup.sh" "$ROOT/client-setup.sh" \
        || { echo "autoupdate: staging client-setup.sh failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-autoupdate.sh"   "$ROOT/autoupdate.sh" \
        || { echo "autoupdate: staging autoupdate.sh failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-fabric-check.sh" "$ROOT/fabric-check.sh" \
        || { echo "autoupdate: staging fabric-check.sh failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-eth-heartbeat-probe.py" "$ROOT/eth-heartbeat-probe.py" \
        || { echo "autoupdate: staging eth-heartbeat-probe.py failed (required); not marking current" >&2; exit 1; }
    install -m 0755 "$clone/deploy/tt-device-aiclk-ceiling.py" "$ROOT/aiclk-ceiling.py" \
        || { echo "autoupdate: staging aiclk-ceiling.py failed (required); not marking current" >&2; exit 1; }
"""


def test_older_updater_stages_this_tree(tmp_path):
    clone = _DEPLOY.parent
    r = subprocess.run(
        ["bash", "-c", _OLD_STAGING],
        env={"PATH": os.environ["PATH"], "clone": str(clone), "ROOT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.xdist_group("forks")
@pytest.mark.parametrize(
    "missing, how",
    [(f"deploy/{src}", "rm") for src, _ in _required_stages()]
    + [("deploy/apply-host-config.sh", "rm"), ("deploy/apply-host-config.sh", "chmod -x")],
)
def test_a_tree_missing_a_required_source_changes_nothing(box, missing, how):  # noqa: F811
    # The full apply checks every source it stages or runs before the venv install. Found only at
    # its copy, a missing source left the new venv and the scripts staged before it beside the old
    # broker and the old installed.sha until a later lap finished the apply.
    root = box["tmp"] / "root"
    staged = {dest: f"old {dest}\n" for _, dest in _required_stages()}
    for dest, body in staged.items():
        (root / dest).write_text(body)
    if how == "rm":
        (box["clone"] / missing).unlink()
    else:
        (box["clone"] / missing).chmod(0o644)
    new = _commit(box["clone"], {"src/tt_device_mcp/server.py": "x = 2\n"})  # a change that restarts
    _git(box["clone"], "reset", "-q", "--hard", box["installed"])

    done, lines, _ = _lap(box, new, stand_ins='install() { command install "$@"; }\n')

    assert done.returncode == 1, done.stderr
    assert f"autoupdate: {missing} missing from the pulled tree" in done.stderr
    assert "nothing changed, not marking current" in done.stderr
    assert "pip" not in lines and not any(x.startswith(("restart", "host-config")) for x in lines), lines
    assert _installed(box) == box["installed"]
    assert {dest: (root / dest).read_text() for dest in staged} == staged
