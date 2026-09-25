# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A second broker the system unit does not manage must be LOUD, not silent.

Doctrine: no silent degrade. A `tt_device_mcp.server` running outside the
tt-device-broker.service cgroup is a stray — a legacy per-user daemon from before
`daemon start` learned to defer to the system broker, or a hand-started one. It
fragments the device queue and drifts to an old version unseen (a 45-day-old
port-8333 daemon coexisting with the system broker is the exact shape this catches).

reconcile runs every minute on every broker host, so it is the place to surface it.
The detection is cgroup-based (precise: the unit's own children stay in its cgroup)
and log-only: killing another user's daemon from a root reconcile is not safe, so the
guarantee is VISIBILITY, not removal. These pin that shape.
"""

from pathlib import Path

_RECONCILE = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-reconcile.sh"


def test_reconcile_detects_a_stray_broker():
    body = _RECONCILE.read_text()
    # enumerates from LISTENERS (ss), not pgrep — a client / self-match does not bind a socket.
    assert "ss -lnpx" in body and "ss -tlnp" in body
    # ...keeps only broker daemons...
    assert "tt_device_mcp\\.server" in body or "tt_device_mcp.server" in body
    # ...and classifies "stray" by cgroup membership, not a fragile name/port match.
    assert "/proc/$pid/cgroup" in body
    assert "tt-device-broker\\.service" in body or "tt-device-broker.service" in body
    # ...and says STRAY loudly to the journal.
    assert "STRAY broker" in body


def test_reconcile_does_not_kill_the_stray():
    """Log-only: a root reconcile must not stop a possibly-deliberate per-user daemon."""
    body = _RECONCILE.read_text()
    # No kill/pkill/systemctl-stop COMMAND anywhere in the stray-handling block. Check for a kill
    # invocation at statement position (start of a logical line), so the word in a message is fine.
    lines = body.splitlines()
    block_start = next(i for i, line in enumerate(lines) if "1b)" in line)
    block_end = next(i for i, line in enumerate(lines) if line.startswith("# 2)"))
    for line in lines[block_start:block_end]:
        s = line.strip()
        assert not (
            s.startswith("kill ") or s.startswith("kill\t") or s.startswith("pkill") or "systemctl stop" in s
        ), f"reconcile must not stop a stray broker: {line!r}"


def test_reconcile_dedups_a_persistent_stray():
    """A persistent stray logs on change, not every one-minute lap — else it floods the journal."""
    body = _RECONCILE.read_text()
    assert "stray_brokers" in body  # a state file remembers the last-seen pid set
