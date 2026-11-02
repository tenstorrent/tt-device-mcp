# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I98 with I69: a chip whose SBR round this hold found no bridge to write to is spent (no
second round), but no bridge write ever reached it. A removed one is still un-reset, so it withholds
every rescan of a later round in the same hold, as it did before the per-chip record."""

import pytest

from tests.test_removed_chip_rescan import FakeBus, _kinds, rung  # noqa: F401
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.recovery import galaxy

HOLD = "2026-01-01T00:00:00"


def _spend_14_with_no_bridge(tmp_path, monkeypatch, rung):  # noqa: F811
    """Chip 14 removed with its bridge gone: its round is spent with no bridge write."""
    monkeypatch.setattr(galaxy, "health_event", lambda kind, **f: rung.append((kind, f)))
    monkeypatch.setattr(srv, "device_hold_episode_since", HOLD)
    bus = FakeBus(tmp_path, monkeypatch)
    bus.remove("14")
    (bus.devices / "0000:40:01.2").unlink()
    srv.isolated_chips = {"14"}
    srv.galaxy_recovery._sbr_hold = None
    return bus


@pytest.mark.asyncio
async def test_probe_no_bridge_spent_chip_must_still_withhold_rescan(tmp_path, monkeypatch, rung):  # noqa: F811
    """Chip 14 is removed and has no usable bridge: its round is recorded spent with no bridge write.
    Chip 13 drops later in the hold. I69: 14 was never reset, so no rescan may re-enumerate it."""
    bus = _spend_14_with_no_bridge(tmp_path, monkeypatch, rung)
    assert await srv.galaxy_recovery._sbr(lambda m: None) is False
    assert srv.galaxy_recovery.last_bridge_reset_reasons.get("14") == {"reason": "no_bridge"}
    assert "14" in srv.galaxy_recovery._sbr_done(HOLD), "no_bridge counts as spent"
    bus.trace.clear()
    bus.remove("13")
    srv.isolated_chips = {"13", "14"}
    await srv.galaxy_recovery._sbr(lambda m: None)
    assert ("rescan",) not in bus.trace, f"rescan re-enumerates never-reset chip 14: {bus.trace}"


@pytest.mark.asyncio
async def test_a_no_bridge_spent_chip_withholds_the_rescan_and_gets_no_second_round(
    tmp_path, monkeypatch, rung  # noqa: F811
):
    """Chip 13 drops later (removed too). The round may reset 13 through its bridge, but runs no rescan
    while never-reset 14 is removed; 14 gets no second try (I98) and is named un-reset; both stay
    isolated and the rung is not recovered, so the hold climbs to the next rung."""
    bus = _spend_14_with_no_bridge(tmp_path, monkeypatch, rung)
    await srv.galaxy_recovery._sbr(lambda m: None)
    bus.trace.clear()
    rung.clear()
    bus.remove("13")
    srv.isolated_chips = {"13", "14"}
    assert await srv.galaxy_recovery._sbr(lambda m: None) is False
    assert ("rescan",) not in bus.trace, bus.trace
    assert ("sbr", "0000:40:01.2") not in bus.trace, "14 has no bridge and is spent"
    assert srv.galaxy_recovery.last_bridge_reset_reasons.get("14") == {"reason": "sbr_spent_this_hold"}
    withheld = _kinds(rung, "pci_rescan_withheld")
    assert withheld and "14" in withheld[0]["unreset"], rung
    assert srv.isolated_chips == {"13", "14"}
    assert pci.removed_chip("14") is not None


def test_a_chip_a_bridge_write_reaches_leaves_the_not_reset_set(tmp_path, monkeypatch, rung):  # noqa: F811
    """The record keeps the no-bridge chips apart until a bridge write reaches one (the driver-wait
    SBR, say): from then on it was reset this hold and no longer withholds the rescan."""
    _spend_14_with_no_bridge(tmp_path, monkeypatch, rung)
    srv.galaxy_recovery._note_sbr_fired({"14"}, unreset={"14"})
    assert srv.galaxy_recovery._sbr_unreset(HOLD) == {"14"}
    srv.galaxy_recovery._note_sbr_fired({"14"})
    assert srv.galaxy_recovery._sbr_done(HOLD) == {"14"}
    assert srv.galaxy_recovery._sbr_unreset(HOLD) == frozenset()


def test_a_chip_back_by_an_outside_path_leaves_the_record(tmp_path, monkeypatch, rung):  # noqa: F811
    """A removed chip that comes back by a path the broker did not run (an outside rescan, a tray
    re-power) is dropped from the removal records on read; its back-on-bus count moves too, so when it
    drops again in the same hold it is not read as spent and gets its own round."""
    bus = _spend_14_with_no_bridge(tmp_path, monkeypatch, rung)
    srv.galaxy_recovery._note_sbr_fired({"14"})
    assert srv.galaxy_recovery._sbr_done(HOLD) == {"14"}
    bus.enumerate("14")
    assert "14" not in pci.removed_chip_ids()
    assert srv.galaxy_recovery._sbr_done(HOLD) == frozenset()
