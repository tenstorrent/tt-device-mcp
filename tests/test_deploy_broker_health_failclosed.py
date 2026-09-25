# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The installer must prove the broker actually serves before declaring success.

Doctrine: a missing REQUIRED capability is a hard failure, not a silent skip. `systemctl
enable --now tt-device-broker` returns once the unit launched, not once the socket serves —
the broker can still be binding, or can have started and then crashed. On a locked host a
dead broker is a total device outage (bare-metal denied, broker gone).

On base the readiness probe was a fixed `sleep 2` then a single `curl /health` whose only
failure handling was `|| echo "(broker not responding ...)"` — the install then reported
DONE and exited 0 over a dead broker. These pin the fail-closed shape: the /health probe is
polled in a retry loop, and a broker that never answers aborts the install with a journal hint.
"""

from pathlib import Path

from tests.deploy_helpers import _statements

_INSTALLER = Path(__file__).resolve().parent.parent / "deploy" / "install-tt-device-broker.sh"


def test_broker_health_failure_aborts_with_journal_hint():
    # The statement that names the broker journal is the readiness verdict. On base it was a
    # soft `|| echo`; fail-closed means it aborts and still points at the journal.
    stmts = _statements(_INSTALLER)
    hits = [s for s in stmts if "journalctl -u tt-device-broker" in s]
    assert len(hits) == 1, f"expected one broker-journal statement, got {hits}"
    stmt = hits[0]
    assert "exit 1" in stmt, "a broker that never answers /health must abort the install, not warn"
    assert "|| echo" not in stmt, "the /health verdict must not be swallowed by a soft echo"


def test_broker_health_is_polled_in_a_loop():
    # A single-shot curl races the broker's startup; readiness must be retried until it serves.
    stmts = _statements(_INSTALLER)
    loop_idx = next((i for i, s in enumerate(stmts) if "for " in s and "seq " in s), None)
    assert loop_idx is not None, "broker /health must be polled in a retry loop, not once"
    done_idx = next(i for i, s in enumerate(stmts) if s.strip() == "done" and i > loop_idx)
    probe = [i for i, s in enumerate(stmts) if "curl" in s and "/health" in s and loop_idx < i < done_idx]
    assert probe, "the /health probe must run inside the retry loop"
