# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I25: a fresh install builds the pinned fabric validator before the broker's first start.

A broker started before the validator exists gets a 77 from its startup fabric pass and holds the
device fabric-unverified until the out-of-band build lands. The installer therefore runs the build
unit in the foreground (joining the run apply-host-config kicked) before it enables and restarts the
broker, and a failed or incomplete build fails the install without starting the broker.
"""

from pathlib import Path

from tests.deploy_helpers import _one, _statements

_INSTALLER = Path(__file__).resolve().parent.parent / "deploy" / "install-tt-device-broker.sh"


def _index(stmts: list[str], *needles: str) -> int:
    hits = [i for i, s in enumerate(stmts) if not s.lstrip().startswith("#") and all(n in s for n in needles)]
    assert len(hits) == 1, f"expected one statement with {needles}, got {len(hits)}"
    return hits[0]


def test_validator_build_runs_before_the_broker_starts():
    stmts = _statements(_INSTALLER)
    apply = _index(stmts, "bash", "apply-host-config.sh")
    build = _index(stmts, "systemctl start tt-device-fabric-validator.service")
    restart = _index(stmts, "systemctl restart tt-device-broker")
    enable = _index(stmts, "systemctl enable tt-device-broker")
    assert apply < build < enable < restart


def test_validator_build_is_blocking_and_failclosed():
    stmt = _one(_INSTALLER, "systemctl start tt-device-fabric-validator.service")
    assert "--no-block" not in stmt, "the installer must wait for the build, not kick it"
    assert "|| true" not in stmt
    assert "exit 1" in stmt and "NOT started" in stmt


def test_incomplete_build_fails_the_install():
    stmts = [s for s in _statements(_INSTALLER) if s.lstrip().startswith("validator_built") and "exit 1" in s]
    assert stmts, "a build that leaves current incomplete must fail the install"


def test_built_check_covers_all_three_pinned_binaries():
    text = _INSTALLER.read_text()
    for binary in (
        "build/tools/scaleout/run_cluster_validation",
        "build/programming_examples/distributed/distributed_program_dispatch",
        "build/programming_examples/metal_example_add_2_integers_in_compute",
    ):
        assert binary in text
    assert "if ! validator_built; then" in text, "an already-built host (upgrade) must not wait"
