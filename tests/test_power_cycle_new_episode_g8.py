# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A new episode after a verified power cycle takes one power cycle at once (spec 04 I9, I27).

Fake-clock replay of a measured Galaxy incident: a tray dropped, the broker power-cycled at 17:45:30
and the next boot passed the full startup check, fabric included. At 18:04 the tray dropped again and
took every chip off the bus. The 1800 s power-cycle interval counted the 17:45 cycle as a failed one
and held the cycle off; the broker repeated the mesh reset three times and the sweep, and the host
died at 18:17. Only a cycle no later boot verified may gate; a host death is never a cycle; the
backstop still caps every cycle.
"""

import json
import time as _time
from types import SimpleNamespace

import pytest

from tests.test_device_safety import _ledger_dir
from tests.test_ladder_v2 import _wire_failed_tray_down_sweep
from tests.test_offbus_power_cycle import _gated, _ladder_ran, _pass
from tt_device_mcp import server as srv
from tt_device_mcp.health.recovery import base as recovery_base
from tt_device_mcp.health.recovery import offbus_power_cycle as opc

DAY = 1_760_486_400.0  # a fixed midnight; only differences matter


def _t(hms: str) -> float:
    h, m, s = (int(x) for x in hms.split(":"))
    return DAY + h * 3600 + m * 60 + s


G7_CYCLE = _t("17:45:30")  # the broker's power cycle for the first drop, recorded on boot-1
G7_VERIFIED = _t("17:51:10")  # boot-2's startup gate: full check, fabric included, passed
G8_RESET_FAILED = _t("18:04:51")  # the new drop: the mesh reset exited 1, every chip off the bus
G8_CYCLE_ASKED = _t("18:07:00")  # the climb asks for the power cycle, 1290 s after G7's
G8_HOST_DIED = _t("18:17:39")


@pytest.fixture
def g8(monkeypatch, tmp_path):
    """boot-2, with G7's cycle in the ledger under boot-1 and the clock at G8_CYCLE_ASKED."""
    d = _ledger_dir(monkeypatch, tmp_path)
    boot = {"id": "boot-2"}
    clock = {"now": G8_CYCLE_ASKED}
    monkeypatch.setattr(srv, "_current_boot_id", lambda: boot["id"])
    monkeypatch.setattr(recovery_base, "POWER_CYCLE_MIN_INTERVAL_SEC", 1800)
    monkeypatch.setattr(srv.recovery_mechanism, "_power_cycle_backstop_paged", 0.0)
    monkeypatch.setattr(recovery_base, "time", SimpleNamespace(time=lambda: clock["now"], monotonic=_time.monotonic))
    (d / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        json.dumps({"action": "power-cycle", "boot_id": "boot-1", "at_epoch": G7_CYCLE}) + "\n"
    )
    return SimpleNamespace(dir=d, boot=boot, clock=clock)


def _verify_g7():
    # A verified release ends the I65 backoff streak and stamps the proof, as _clear_device_dirty does.
    srv.recovery_mechanism.note_mesh_verified(now_epoch=G7_VERIFIED)
    srv.recovery_mechanism.record_verified_release("gate/startup", now_epoch=G7_VERIFIED)


def _allowed(action="power-cycle"):
    return srv.recovery_mechanism.auto_recovery_allowed(action, tenant_active=False)


def test_g8_a_cycle_a_later_boot_verified_does_not_hold_off_the_next_episodes_cycle(g8):
    """(a) The measured case: inside the 1800 s window, G7's verified cycle gates nothing."""
    allowed, why = _allowed()
    assert allowed is False and "boot-loop guard" in why, "unverified, G7's cycle still gates (the loop guard)"
    _verify_g7()
    assert _allowed() == (True, "")
    g8.clock["now"] = _t("18:12:39")  # after the broker restart: the proof is on disk, not in memory
    assert _allowed() == (True, "")


def test_g8_a_host_death_is_never_counted_as_a_cycle(g8):
    """(b) A host death writes no ledger row: it neither gates the next cycle nor spends the per-boot
    cap, and it is no proof that an unverified cycle healed the box."""
    _verify_g7()
    before = (g8.dir / recovery_base.AUTO_RECOVERY_LEDGER).read_text()
    g8.boot["id"] = "boot-3"  # the host died at 18:17:39 and came back; no broker action recorded it
    g8.clock["now"] = G8_HOST_DIED + 300
    assert (g8.dir / recovery_base.AUTO_RECOVERY_LEDGER).read_text() == before
    assert _allowed() == (True, ""), "the death is not a cycle; the verified G7 cycle still gates nothing"

    # The same death after a cycle no boot verified: still gated, the death proves nothing.
    (g8.dir / recovery_base.AUTO_RECOVERY_VERIFIED).unlink()
    g8.clock["now"] = G7_CYCLE + 1500
    allowed, why = _allowed()
    assert allowed is False and "boot-loop guard" in why


def test_g8_the_loop_bound_stands_for_an_unverified_cycle(g8):
    """The new episode gets one cycle, not a loop: the G8 cycle itself is unverified until a later
    boot passes the full check, so a third request inside 1800 s of it is refused."""
    _verify_g7()
    assert _allowed() == (True, "")
    assert srv.recovery_mechanism.record_auto_recovery("power-cycle", "G8", now_epoch=G8_CYCLE_ASKED)
    g8.boot["id"] = "boot-3"  # came back, never verified
    g8.clock["now"] = G8_CYCLE_ASKED + 900
    allowed, why = _allowed()
    assert allowed is False and "boot-loop guard" in why
    g8.clock["now"] = G8_CYCLE_ASKED + 1800
    assert _allowed() == (True, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False], ids=["verified", "unverified"])
async def test_g8_the_ladder_takes_the_power_cycle_instead_of_holding_for_the_interval(g8, monkeypatch, verified):
    """(c) End to end on the climb after a failed reset (the governor real, the clock fake): the new
    episode after a verified cycle fires the power cycle now. In the incident the denial left
    the hold to repeat the mesh reset and the sweep for the rest of the interval."""
    _events, cycles = _wire_failed_tray_down_sweep(monkeypatch, lambda: {})
    g = srv.galaxy_recovery
    monkeypatch.delattr(g.mechanism, "auto_recovery_allowed")  # the real governor reads the ledger
    if verified:
        _verify_g7()
    offbus = {str(i) for i in range(32)}
    await g._climb_or_hand_off(32, len(offbus), "2026-10-14T18:04:51", lambda m: None)
    assert cycles["n"] == (1 if verified else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, fires",
    [("verified", True), ("unverified", False), ("legacy row without boot id", False), ("same boot", False)],
)
async def test_g8_the_off_bus_hold_interval_skips_a_cycle_a_later_boot_verified(
    monkeypatch, tmp_path, clear_job_state, case, fires
):
    """(a) on the bounded off-bus hold (I27), with an attempt spacing set: it counted every attempt. An attempt a
    later boot verified healthy no longer holds the next episode's cycle; any attempt it cannot prove
    healed (no stamp, no boot id, or a stamp from its own boot) still gates and pages."""
    hold_since, rec = _gated(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_OFFBUS_POWER_CYCLE_MIN_INTERVAL_SEC", "3600")
    _ledger_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(opc, "health_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "_current_boot_id", lambda: "boot-2")
    now = opc.now_epoch()
    cycled = now - 1290
    attempt = {"at_epoch": cycled, "episode": "earlier", "reason": "G7", "boot_id": "boot-1"}
    if case == "legacy row without boot id":
        del attempt["boot_id"]
    opc.save_state({"attempts": [attempt]})
    (tmp_path / recovery_base.AUTO_RECOVERY_LEDGER).write_text(
        json.dumps({"action": "power-cycle", "boot_id": "boot-1", "at_epoch": cycled - 3600}) + "\n"
    )
    if case != "unverified":
        stamp_boot = "boot-1" if case == "same boot" else "boot-2"
        (tmp_path / recovery_base.AUTO_RECOVERY_VERIFIED).write_text(
            json.dumps({"boot_id": stamp_boot, "at_epoch": cycled + 340})
        )
    _ladder_ran(hold_since)
    out = await _pass(hold_since)
    if fires:
        assert out is True and len(rec["fires"]) == 1
        assert rec["fires"][0]["state"]["attempts"][0]["boot_id"] == "boot-2", "the new attempt names its boot"
    else:
        assert out is False and rec["fires"] == [], "held to a stated deadline, no fall-through to the ladder"
        assert [a["kind"] for a in rec["alerts"]] == ["offbus_power_cycle_rate_limited"]


def test_last_attempt_within_skips_only_what_healed_says():
    now = 100_000.0
    state = {"attempts": [{"at_epoch": now - 60, "boot_id": "a"}, {"at_epoch": now - 120, "boot_id": "b"}]}
    assert opc.last_attempt_within(state, now, 600) == now - 60
    assert opc.last_attempt_within(state, now, 600, healed=lambda r: r["boot_id"] == "a") == now - 120
    assert opc.last_attempt_within(state, now, 600, healed=lambda r: True) is None
    assert opc.last_attempt_within({"attempts": [{"at_epoch": "?"}]}, now, 600, healed=lambda r: True) == now
