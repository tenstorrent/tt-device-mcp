# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The auto-update's two busy gates, which are not the same gate.

A running JOB must not hold an update back: it lives in its own scope and is re-adopted
across the restart (test_readopt), so waiting for idle buys no safety and leaves a host
stale on the fix it is waiting for. An in-flight DEVICE OP must: re-adoption covers a job,
not a tt-smi that KillMode=control-group SIGTERMs mid-reset, which is a half-reset mesh and
a chassis power cycle.
"""

from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"


def test_the_idle_gate_is_off_by_default():
    assert 'MAX_DEFER="${TTDEV_MAX_DEFER_SEC:-0}"' in _SCRIPT.read_text()


def test_a_host_can_still_ask_for_an_idle_window():
    # The bounded wait stays reachable for a host that sets the knob; only the default moved.
    text = _SCRIPT.read_text()
    assert '[ "$waited" -lt "$MAX_DEFER" ]' in text
    assert '[ "$MAX_DEFER" -gt 0 ]' in text


def test_an_in_flight_device_op_still_bars_the_update():
    # Unconditional, and before the idle gate: no knob may turn this one off.
    text = _SCRIPT.read_text()
    op_bar = text.index("device op in flight")
    assert "exit 0" in text[op_bar : op_bar + 200]
    assert text.index("ttdev-reset-") < text.index("MAX_DEFER=")
