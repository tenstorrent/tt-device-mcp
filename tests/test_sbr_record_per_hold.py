# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I98: one SBR round per chip per hold.

Replay of one below-floor hold with one chip off the bus: the post-job gate reset the chip through its
bridge (three tries) and it stayed off. The off-bus escalation and the climb's last-chance sweep each
ran their own three-try round on the same chip, because the hold's SBR record only kept chips the rung
gave no reason for, and a reset chip still off the bus is tagged as such. These pin: every bridge reset
enters the record, a later path in the same hold gives the chip no second round and goes on to its next
rung, a new hold starts with an empty record, and only a just-passed full check with fabric lifts the
hold. No test reaches a real reset, setpci or power cycle."""

from datetime import datetime, timedelta

import pytest

from tests.conftest import patch_recovery
from tests.test_below_floor_offbus_ladder import GONE, _gate, _rig
from tests.test_hold_episode_carry import _GENERIC_REHOLD, _PASS, _isolate
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import NO_WINDOW_REMOVED_REASON, OUTCOME_WAITING
from tt_device_mcp.health.recovery import base as recovery_base


def _reset_still_off(monkeypatch, order):
    """The SBR rung as the real one ends for a chip reset on every try and still off the bus: a bridge
    write reached it and it is tagged with the no-window reason. As the real rung, it writes no bridge
    reset to a chip in ``exclude`` (already spent this hold)."""

    async def sbr(log, exclude=frozenset()):
        targets = (set(srv.isolated_chips) | set(pci.removed_chip_ids())) - set(exclude)
        order.append(f"sbr {sorted(targets, key=int)}")
        srv.galaxy_recovery.last_bridge_reset_fired = set(targets)
        srv.galaxy_recovery.last_bridge_reset_reasons = {c: {"reason": NO_WINDOW_REMOVED_REASON} for c in targets}
        return False

    patch_recovery(monkeypatch, "_recover_isolated_chips", sbr)
    sweep = srv.galaxy_recovery._issue_all_resets_back_to_back

    async def marked_sweep(*args, **kwargs):
        order.append("sweep")
        return await sweep(*args, **kwargs)

    monkeypatch.setattr(srv.galaxy_recovery, "_issue_all_resets_back_to_back", marked_sweep)


async def _escalation(logs):
    return await srv.galaxy_recovery._escalate_offbus_stuck_hold([GONE], 32, logs.append, force=True)


def _sbrs(order):
    """The SBR rounds before the last-chance sweep."""
    before = order[: order.index("sweep")] if "sweep" in order else order
    return [o for o in before if o.startswith("sbr")]




def _before_sweep(events: list) -> list:
    """The events before the last-chance sweep that runs before the power cycle (spec 04 I58)."""
    return events[: events.index("last_chance_reset_sweep")] if "last_chance_reset_sweep" in events else events

@pytest.mark.asyncio
async def test_a_chip_the_gate_reset_is_not_reset_again_by_the_escalation_or_the_sweep(
    monkeypatch, tmp_path, clear_job_state
):
    """(a) The gate's round counts: the escalation runs no second SBR round on the chip. The last-chance
    sweep right before the power cycle still writes it (the full ladder, whatever failed earlier in the
    hold; here no chip is isolated or removed, so it has no bridge to write), and the hold climbs to the power cycle with the hold kept."""
    order, events, counters = _rig(monkeypatch, tmp_path, opt_in=True, held_min=3)
    _reset_still_off(monkeypatch, order)
    assert await _gate() == OUTCOME_WAITING
    assert _sbrs(order) == [f"sbr ['{GONE}']"], "the gate's one round"
    order.clear()
    logs = []
    await _escalation(logs)
    assert _sbrs(order) == [], "no second round in the escalation"
    assert "sbr_skipped_spent" in events
    srv.recovery_mechanism.note_rung_failed(srv.device_hold_episode_since, "mesh_reset", {GONE}, {GONE}, rc=1)
    order.clear()
    logs.clear()
    await srv.galaxy_recovery.escalate("offbus-forced", [GONE], 32, logs.append)
    assert _sbrs(order) == [], "no second round before the sweep"
    sweep = events[events.index("last_chance_reset_sweep") :]
    assert "sbr_skipped_spent" not in sweep, "the sweep never skips a chip's SBR as spent"
    assert "last_chance_sweep_repeats_failed_reset" in sweep
    assert not any("no SBR has run" in m for m in logs), "the sweep reads the gate's round"
    assert order[-1] == "power cycle"
    assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0


@pytest.mark.asyncio
async def test_the_record_starts_empty_for_each_new_hold(monkeypatch, tmp_path, clear_job_state):
    """(b) The record is keyed by the hold: after the gate's round, a new hold's escalation SBRs the chip
    again."""
    order, events, _counters = _rig(monkeypatch, tmp_path, opt_in=True, held_min=3)
    _reset_still_off(monkeypatch, order)
    await _gate()
    assert _sbrs(order) == [f"sbr ['{GONE}']"]
    srv.device_hold_episode_since = (datetime.now() - timedelta(minutes=1)).isoformat()  # a new hold
    order.clear()
    await _escalation([])
    assert _sbrs(order) == [f"sbr ['{GONE}']"], "a new hold gets its own round"
    assert "sbr_skipped_spent" not in _before_sweep(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, False])
async def test_after_a_skipped_round_only_the_full_check_with_fabric_lifts(
    monkeypatch, tmp_path, clear_job_state, verdict
):
    """(c) A skipped round is not a recovery: the escalation goes on to its next rung, and the hold lifts
    only on a just-passed full check with fabric; a failed one keeps it."""
    order, events, counters = _rig(monkeypatch, tmp_path, opt_in=True, held_min=3, verdicts=[verdict])
    _reset_still_off(monkeypatch, order)
    await _gate()
    order.clear()
    await _escalation([])
    assert _sbrs(order) == [] and "sbr_skipped_spent" in events
    assert "check without fabric" not in order
    if verdict:
        assert "full check" in order
        assert counters["dirty_cleared"] == 1, "lifted once, on the just-passed full check with fabric"
    else:
        assert counters["dirty_cleared"] == 0 and counters["fault_cleared"] == 0, "a failed check keeps the hold"


def _seen_back(monkeypatch, chips):
    """A rescan or mesh reset brought ``chips`` back: the broker's own re-read after it (I81, I97 b)."""
    monkeypatch.setattr(recovery_base, "returned_chips", lambda ids: sorted(set(ids) & set(chips), key=int))
    recovery_base.drop_returned_isolated(srv.isolated_chips, "reset")


@pytest.mark.asyncio
async def test_a_partial_round_writes_no_bridge_reset_to_the_spent_chip(monkeypatch, tmp_path, clear_job_state):
    """(d) Per chip: a second chip drops later in the hold while the first, SBR'd and still off, stays
    out. The new chip gets its round; the spent one gets no second bridge write, and the rung reports
    not recovered while it is still off the bus."""
    order, events, _counters = _rig(monkeypatch, tmp_path, opt_in=True, held_min=3)
    _reset_still_off(monkeypatch, order)
    srv.isolated_chips.add(GONE)
    await srv.galaxy_recovery._sbr(lambda m: None)
    assert _sbrs(order) == [f"sbr ['{GONE}']"]
    order.clear()
    srv.isolated_chips.add("5")
    assert await srv.galaxy_recovery._sbr(lambda m: None) is False
    assert _sbrs(order) == ["sbr ['5']"], "the spent chip gets no second round"
    assert "sbr_skipped_spent" in events
    order.clear()
    await srv.galaxy_recovery._sbr(lambda m: None)
    assert _sbrs(order) == [], "both spent now: no round at all"


@pytest.mark.asyncio
async def test_a_chip_the_driver_wait_sbr_healed_gets_a_round_when_it_drops_again(
    monkeypatch, tmp_path, clear_job_state
):
    """(e) The driver-wait SBR (I97 b) fires on an isolated chip and the wait's next rescan brings it
    back. When it drops again later in the hold, it gets its own SBR round, not sbr_skipped_spent."""
    order, events, _counters = _rig(monkeypatch, tmp_path, opt_in=True, held_min=3)
    _reset_still_off(monkeypatch, order)
    srv.isolated_chips.add(GONE)
    srv.recovery_mechanism.sbr_fired_hook([GONE])
    _seen_back(monkeypatch, [GONE])
    assert GONE not in srv.isolated_chips
    srv.isolated_chips.add(GONE)  # it drops again
    await srv.galaxy_recovery._sbr(lambda m: None)
    assert _sbrs(order) == [f"sbr ['{GONE}']"]
    assert "sbr_skipped_spent" not in events


@pytest.mark.asyncio
async def test_a_chip_the_mesh_reset_healed_gets_a_round_when_it_drops_again(monkeypatch, tmp_path, clear_job_state):
    """(f) The gate's round left the chip off; the mesh reset brought it back. A later drop in the same
    hold is a new one: the off-bus escalation gives it an SBR round."""
    order, events, _counters = _rig(monkeypatch, tmp_path, opt_in=True, held_min=3)
    _reset_still_off(monkeypatch, order)
    await _gate()
    assert _sbrs(order) == [f"sbr ['{GONE}']"]
    srv.isolated_chips.add(GONE)
    _seen_back(monkeypatch, [GONE])
    assert GONE not in srv.isolated_chips
    order.clear()
    srv.isolated_chips.add(GONE)  # it drops again
    await _escalation([])
    assert _sbrs(order) == [f"sbr ['{GONE}']"], "a new drop gets its own round"
    assert "sbr_skipped_spent" not in _before_sweep(events)


@pytest.mark.asyncio
async def test_a_hold_carried_after_a_verified_lift_starts_with_an_empty_record(monkeypatch, tmp_path, clear_job_state):
    """(g) The record ends with the hold: a lift on a just-passed full check clears it, so a hold that
    carries the same episode (I82) gives a chip that drops again its own round."""
    order, events, _counters = _rig(monkeypatch, tmp_path, opt_in=True, held_min=3)
    _isolate(monkeypatch, tmp_path)
    _reset_still_off(monkeypatch, order)
    await _gate()
    assert _sbrs(order) == [f"sbr ['{GONE}']"]
    since = srv.device_hold_episode_since
    srv.isolated_chips.discard(GONE)
    monkeypatch.setattr(srv, "device_hold_logged", True)
    monkeypatch.setattr(srv.health_monitor, "fabric_verdict", lambda: dict(_PASS))
    srv._note_tenant_gate_verdict("")
    assert not srv.device_hold_logged and srv._hold_carry is not None
    srv._note_tenant_gate_verdict(_GENERIC_REHOLD)
    assert srv.device_hold_episode_since == since, "the hold carries the released episode"
    order.clear()
    srv.isolated_chips.add(GONE)  # the chip drops again
    await _escalation([])
    assert _sbrs(order) == [f"sbr ['{GONE}']"]
    assert "sbr_skipped_spent" not in _before_sweep(events)
