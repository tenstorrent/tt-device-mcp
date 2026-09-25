# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A shipped sudoers that fails `visudo -c` is a build defect, not a tolerable host state.

apply-host-config.sh installs tt-device-mcp-smi.sudoers — a file that ships WITH the broker.
If it fails `visudo -c` the fault is ours: we shipped garbage. On base the failure path removed
the file (correct — a syntactically invalid file in /etc/sudoers.d makes sudo refuse to run for
the whole host) but then let the `|| { ...; }` block succeed on the trailing `rm`, so `set -e`
never tripped and the script continued. Auto-update then marked a build that silently dropped
the read-only smi grant as applied.

Doctrine: a build defect must be loud, not silent. The fail-closed shape pins both halves — the
protective removal stays (never leave a file that bricks all sudo) AND the path aborts (exit 1)
so a defective build never marks itself current; F2 keeps the host on last-known-good and retries.
"""

from pathlib import Path

from tests.deploy_helpers import _one

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_APPLY = _DEPLOY / "apply-host-config.sh"


def test_invalid_shipped_sudoers_aborts():
    stmt = _one(_APPLY, "visudo -cf", "/etc/sudoers.d/tt-device-mcp-smi")
    assert "|| true" not in stmt, "a shipped sudoers failing visudo -c must not be swallowed"
    assert "exit 1" in stmt, "a shipped sudoers failing visudo -c is a build defect and must abort"


def test_invalid_shipped_sudoers_is_removed_before_aborting():
    # The removal is not optional: leaving a syntactically invalid file in /etc/sudoers.d makes
    # sudo refuse to run for the entire host, so it must precede the abort, not be dropped for it.
    stmt = _one(_APPLY, "visudo -cf", "/etc/sudoers.d/tt-device-mcp-smi")
    rm_idx = stmt.index("rm -f /etc/sudoers.d/tt-device-mcp-smi")
    exit_idx = stmt.index("exit 1")
    assert rm_idx < exit_idx, "the broken sudoers must be removed before the script aborts"
