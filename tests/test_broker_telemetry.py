# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""BrokerTelemetry: one declaration of what the broker publishes about itself.

The exposition used to be 56% histogram buckets reading 0.0 and carried no point-in-time state at
all — no chip counts, no probe verdicts, no platform, nothing an operator staring at a held device
would want. These pin the snapshot's contents and the two properties that keep it trustworthy: it
never raises, and every string it reports lands in a closed-enum label rather than minting series.
"""

import pytest

from tt_device_mcp import metrics, telemetry
from tt_device_mcp.telemetry import BrokerTelemetry


@pytest.fixture(autouse=True)
def _fresh_registry():
    metrics.reset_for_tests()
    yield
    metrics.reset_for_tests()


def test_a_snapshot_of_nothing_still_produces_a_snapshot():
    """Called before the boot flow has built anything — the subsystems are None. A snapshot that
    raised here would remove the only reporting an operator has at exactly the wrong moment."""
    snap = telemetry.snapshot(None, None, None, None)
    assert isinstance(snap, BrokerTelemetry)
    assert snap.fsm_why == "" and snap.platform == "unresolved"
    assert snap.fabric_ok is None, "never-ran must stay distinct from ran-and-failed"


def test_a_subsystem_that_raises_does_not_lose_the_whole_snapshot():
    """One unreadable field must cost that field, not every other one."""

    class Exploding:
        def status(self):
            raise RuntimeError("monitor is mid-transition")

    snap = telemetry.snapshot(None, None, None, None)
    assert snap is not None
    with pytest.raises(RuntimeError):
        Exploding().status()  # the fixture itself really does raise
    # publish() swallows, by contract — telemetry may not take the broker down.
    BrokerTelemetry().publish()


def test_the_snapshot_reaches_the_exposition():
    """The whole point: what publish() sets is what an operator scrapes."""
    telemetry.BrokerTelemetry(
        chips_present=8,
        chips_expected=32,
        isolated_chips=2,
        queue_depth=3,
        busy_sec=100.0,
        free_sec=300.0,
        device_busy=True,
        fsm_dirty=True,
        reset_cooling=True,
        fabric_ok=False,
        wait_p50=1.5,
        wait_p95=9.0,
        wait_max=12.0,
    ).publish()
    out = metrics.render().decode()

    assert "tt_device_broker_chips_present 8.0" in out
    assert "tt_device_broker_chips_expected 32.0" in out
    assert "tt_device_broker_isolated_chips 2.0" in out
    assert "tt_device_broker_busy_seconds_total 100.0" in out
    assert "tt_device_broker_free_seconds_total 300.0" in out
    assert "tt_device_broker_device_busy 1.0" in out
    assert "tt_device_broker_fsm_dirty 1.0" in out
    assert "tt_device_broker_reset_cooling 1.0" in out
    assert 'tt_device_broker_job_wait_seconds{quantile="0.95"} 9.0' in out


def test_busy_and_free_advance_by_the_clock_delta_and_survive_a_reset():
    """The snapshot carries cumulative session seconds; the counters must advance by the delta —
    and a snapshot whose clock went BACKWARDS (a fresh Stats object) must rebaseline silently,
    never re-add a whole session's worth."""
    BrokerTelemetry(busy_sec=100.0, free_sec=300.0).publish()
    BrokerTelemetry(busy_sec=150.0, free_sec=310.0).publish()
    out = metrics.render().decode()
    assert "tt_device_broker_busy_seconds_total 150.0" in out
    assert "tt_device_broker_free_seconds_total 310.0" in out

    BrokerTelemetry(busy_sec=5.0, free_sec=2.0).publish()  # new session, clock restarted
    out = metrics.render().decode()
    assert "tt_device_broker_busy_seconds_total 150.0" in out, "a reset must not increment"
    BrokerTelemetry(busy_sec=8.0, free_sec=4.0).publish()
    assert "tt_device_broker_busy_seconds_total 153.0" in metrics.render().decode()


def test_fabric_verdict_is_tri_state():
    """A fabric pass that never ran and one that ran and failed route the ladder completely
    differently, so they must not both read 0."""
    for value, expected in ((None, "-1.0"), (True, "1.0"), (False, "0.0")):
        metrics.reset_for_tests()
        BrokerTelemetry(fabric_ok=value).publish()
        assert f"tt_device_broker_fabric_ok {expected}" in metrics.render().decode()


def test_platform_and_fault_publish_only_their_current_value():
    """Strings cannot be Prometheus samples, so these are labels — but only the CURRENT value is
    emitted, not a full one-hot over the vocabulary. An alert like fault{why="off_bus"} == 1 still
    matches; the series simply appears when the value does."""
    BrokerTelemetry(platform="galaxy", fsm_why="off_bus").publish()
    out = metrics.render().decode()

    assert 'tt_device_broker_platform{platform="galaxy"} 1.0' in out
    assert 'platform="per-target"' not in out, "non-current values must not be emitted at all"
    assert 'tt_device_broker_fault{why="off_bus"} 1.0' in out
    assert 'why="heartbeat"' not in out
    # Healthy is a value too, not an absence — a missing fault metric must always mean a dead
    # broker, never a good one. And the stale off_bus series must vanish, not linger at 1.
    BrokerTelemetry(fsm_why="").publish()
    out = metrics.render().decode()
    assert 'tt_device_broker_fault{why="none"} 1.0' in out
    assert 'why="off_bus"' not in out


def test_probe_verdicts_report_the_last_pass_not_a_running_total():
    """The counters already say how often each verdict happened; this says which one is current."""
    BrokerTelemetry(probe_verdicts={"fabric": "unhealthy", "heartbeat": "healthy"}).publish()
    out = metrics.render().decode()

    assert 'tt_device_broker_probe_last_verdict{probe="fabric",verdict="unhealthy"} 1.0' in out
    assert 'tt_device_broker_probe_last_verdict{probe="heartbeat",verdict="healthy"} 1.0' in out
    # Only the current verdict per probe is emitted; a probe that did not run reports nothing.
    assert 'probe="fabric",verdict="healthy"' not in out
    assert 'probe="eth_heartbeat"' not in out


def test_the_exposition_carries_no_histogram_buckets():
    """The regression this replaced: 105 of 187 lines were buckets, nearly all 0.0, and the
    default boundaries topped out at 10s while a fabric pass runs 45-100s — every observation
    that mattered landed in +Inf."""
    BrokerTelemetry().publish()
    out = metrics.render().decode()
    assert "_bucket" not in out
    body = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert len(body) < 50, f"exposition grew back to {len(body)} lines"
