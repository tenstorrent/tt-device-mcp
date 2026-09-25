# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The per-user install must prove the daemon serves before declaring DONE.

Doctrine: a missing REQUIRED capability is a hard failure, not a silent skip. On a reserved
box the standalone socket daemon IS the product — nothing else this script installs is usable
without it. `daemon start` already polls /health internally and returns 0 only once the daemon
answers (or it deferred to a live system broker); a non-zero means the install can't serve.

On base `daemon start` and the trailing `daemon status` both carried `|| true` and DONE printed
unconditionally, so a dead daemon reported success and exited 0. These pin the fail-closed
shape: a failed start aborts the install before DONE, and the final status verdict is not
swallowed.
"""

from pathlib import Path

from tests.deploy_helpers import _statements

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "install-user.sh"


def test_daemon_start_failure_aborts_before_done():
    # The daemon is the product; a failed start must abort, not be swallowed and reported DONE.
    stmts = _statements(_SCRIPT)
    # The quoted invocation, not the comment / crontab grep / @reboot echo that also say it.
    starts = [s for s in stmts if '"$BINDIR/tt-device-mcp" daemon start' in s]
    assert len(starts) == 1, f"expected one daemon-start invocation, got {starts}"
    assert "|| true" not in starts[0], "a failed daemon start must abort, not be swallowed by || true"
    start_idx = stmts.index(starts[0])
    done_idx = next(i for i, s in enumerate(stmts) if "DONE" in s and "standalone" in s)
    aborts = [i for i, s in enumerate(stmts) if s.strip() == "exit 1" and start_idx <= i < done_idx]
    assert aborts, "a failed daemon start must exit non-zero before the install prints DONE"


def test_daemon_status_verdict_not_swallowed():
    # The trailing readiness echo must not hide a non-zero behind `|| true`.
    stmts = _statements(_SCRIPT)
    stat = [s for s in stmts if '"$BINDIR/tt-device-mcp" daemon status' in s]
    assert len(stat) == 1, f"expected one daemon-status invocation, got {stat}"
    assert "|| true" not in stat[0], "the final status verdict must not be swallowed by || true"
