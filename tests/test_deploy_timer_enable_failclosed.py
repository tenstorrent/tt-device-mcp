# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Enabling a REQUIRED systemd timer must be verified, not fired-and-forgotten.

Doctrine: a missing REQUIRED capability is a hard failure, not a silent skip. Two timers
are required for a healthy host:

  - tt-device-fabric-validator.timer keeps the authoritative fabric validator built and
    refreshed; without it the fabric check reports CANNOT CHECK and every abnormal exit
    falls back to a full board reset.
  - tt-device-reconcile.timer wires agents and re-applies config on drift; without it a
    host silently stops picking up new users and unit/sudoers changes.

`systemctl enable --now` can exit 0 while the unit stays masked/unarmed, so enabling alone
is not proof the timer runs. On base both enable calls carried `>/dev/null 2>&1 || true`,
swallowing a failed enable and never confirming the timer armed. These pin the fail-closed
shape: the enable aborts on failure, and a follow-up `is-active` check aborts if the timer
did not actually come up.
"""

from pathlib import Path

from tests.deploy_helpers import _one, _statements

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_INSTALLER = _DEPLOY / "install-tt-device-broker.sh"
_APPLY = _DEPLOY / "apply-host-config.sh"


def _assert_enable_failclosed(script: Path, timer: str):
    stmt = _one(script, "systemctl enable --now", timer)
    assert "|| true" not in stmt, f"a failed enable of {timer} must not be swallowed"
    assert "exit 1" in stmt, f"a failed enable of {timer} must abort"


def _assert_is_active_verified(script: Path, timer: str):
    stmt = _one(script, "systemctl is-active", timer)
    assert "|| true" not in stmt, f"the is-active check of {timer} must not be swallowed"
    assert "exit 1" in stmt, f"an unarmed {timer} must abort"


def test_fabric_validator_timer_enable_is_verified():
    _assert_enable_failclosed(_APPLY, "tt-device-fabric-validator.timer")
    _assert_is_active_verified(_APPLY, "tt-device-fabric-validator.timer")


def test_reconcile_timer_enable_is_verified():
    _assert_enable_failclosed(_INSTALLER, "tt-device-reconcile.timer")
    _assert_is_active_verified(_INSTALLER, "tt-device-reconcile.timer")


def test_is_active_check_follows_the_enable():
    # The verification only proves anything if it runs AFTER the enable; if the order flipped
    # it would read a stale state from a prior run rather than confirming this enable armed.
    for script, timer in ((_APPLY, "tt-device-fabric-validator.timer"), (_INSTALLER, "tt-device-reconcile.timer")):
        stmts = _statements(script)
        enable_idx = next(i for i, s in enumerate(stmts) if "systemctl enable --now" in s and timer in s)
        active_idx = next(i for i, s in enumerate(stmts) if "systemctl is-active" in s and timer in s)
        assert enable_idx < active_idx, f"is-active of {timer} must follow its enable"
