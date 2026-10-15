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

import re
from pathlib import Path

from tests.deploy_helpers import _statements

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_INSTALLER = _DEPLOY / "install-tt-device-broker.sh"
_AUTOUPDATE = _DEPLOY / "tt-device-autoupdate.sh"


def _install_stmt(script: Path, dest: str) -> str:
    """The single `install -m 0755` statement that stages into `dest`."""
    hits = [s for s in _statements(script) if "install -m 0755" in s and dest in s]
    assert len(hits) == 1, f"expected exactly one install of {dest} in {script.name}, got {hits}"
    return hits[0]


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
    for dest in (
        '"$ROOT/reconcile.sh"',
        '"$ROOT/client-setup.sh"',
        '"$ROOT/autoupdate.sh"',
        '"$ROOT/fabric-check.sh"',
        '"$ROOT/eth-heartbeat-probe.py"',
        '"$ROOT/aiclk-ceiling.py"',
    ):
        stmt = _install_stmt(_AUTOUPDATE, dest)
        assert "|| true" not in stmt
        assert "exit 1" in stmt, f"a failed stage of {dest} must exit non-zero"


def test_autoupdate_staged_sources_exist_in_deploy():
    # The shape assertions above pin HOW each artifact stages, not THAT it can: a stage
    # whose source left the tree fails on every host, fail-closed, forever — autoupdate
    # never marks current again. Pin the sources to the tree so retiring a deploy script
    # without dropping its stage breaks here, not on the fleet.
    for stmt in _statements(_AUTOUPDATE):
        for src in re.findall(r'"\$clone/(deploy/[^"]+)"', stmt):
            assert (_DEPLOY.parent / src).is_file(), (
                f"autoupdate stages {src} but the tree does not carry it; "
                "every stage of a missing source wedges auto-update fleet-wide"
            )
    # The fail-closed contract only holds if the staging block precedes the installed.sha
    # write; if the order flipped, an aborted stage would still have marked the host current.
    stmts = _statements(_AUTOUPDATE)
    stage_idx = next(i for i, s in enumerate(stmts) if "install -m 0755" in s and '"$ROOT/fabric-check.sh"' in s)
    sha_idx = next(i for i, s in enumerate(stmts) if '> "$STATE"' in s or '>"$STATE"' in s)
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
    sha_idx = next(i for i, s in enumerate(stmts) if '> "$STATE"' in s or '>"$STATE"' in s)
    assert apply_idx < sha_idx
