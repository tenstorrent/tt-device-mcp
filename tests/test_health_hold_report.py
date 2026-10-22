# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""/health while the FSM is RECOVERING or DOWN (spec 03 I72, spec 04 I75).

Each hold kind reports its fault class, its start, the next rung the broker has armed and when it
fires, and per UBB tray (by PCI bus group, never chip index) the chips on the bus and with a live
ARC heartbeat, labelled not verified. The report is read-only: it moves no rung clock, calls no
chip healthy and admits no job."""

from datetime import datetime

import pytest

from tests.conftest import GALAXY_BUS_IDS
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import hold_report as hr
from tt_device_mcp.health.monitors.heartbeat import ALL_ONES
from tt_device_mcp.health.monitors.pci import SAMPLE_INTERVAL_SEC
from tt_device_mcp.health.recovery import offbus_power_cycle as opc

T0 = 1_800_000_000.0
START = T0 - 60  # the hold began a minute before the read
_BOUND_RUNG = "full ladder"  # spec 04 I76; the fixture opts in no host rung
EXPECTED = 32
TABLES = {"tt-galaxy-bh": {1: 0x00, 2: 0x40, 3: 0xC0, 4: 0x80}}
TRAY_CHIPS = {
    g: [i for i, a in enumerate(GALAXY_BUS_IDS) if int(a[5:7], 16) & 0xF0 == g] for g in (0, 0x40, 0x80, 0xC0)
}


def _cool(**kw) -> dict:
    """A governor cooldown record with the I65 keys /health already reads."""
    return {"cause": "host boot", "interval_sec": 1800, "why": "boot-loop guard", **kw}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


@pytest.fixture
def held(monkeypatch, galaxy_trays):
    """A 32-chip Blackhole Galaxy held DOWN since START; ``held['off']`` are the chips off the bus,
    ``held['dead']`` those reading all-ones, ``held['cooldown']`` the governor's wait."""
    st = {"off": set(), "dead": set(), "state": ServerState.DOWN}
    m = srv.recovery_mechanism
    monkeypatch.setattr(type(srv.fsm), "state", property(lambda self: st["state"]))
    monkeypatch.setattr(srv, "_device_hold_state", lambda now=None: ("off_bus", _iso(START)))
    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.fromtimestamp(START).isoformat())
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv, "device_hold_offbus_tenant_wait", False)
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "heartbeat_supported", lambda refresh=False: True)
    monkeypatch.setattr(srv.health_monitor, "_health_check_enabled", lambda: True)
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: EXPECTED)
    monkeypatch.setattr(srv, "_ubb_bus_id_tables", lambda: TABLES)
    monkeypatch.setattr(m, "power_cycle_cooldown_state", None)
    monkeypatch.setattr(m, "power_cycle_retry", None)
    monkeypatch.setattr(m, "host_action_requested", None)
    monkeypatch.setattr(opc, "_pending", {})
    monkeypatch.delenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_HOLD_HANDOFF_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_OFFBUS_HOLD_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FORCE_ESCALATE", raising=False)

    def beats():
        out = {str(i): 100 for i in range(EXPECTED) if i not in st["off"]}
        out.update({str(i): ALL_ONES for i in st["dead"]})
        return out

    monkeypatch.setattr(srv, "read_heartbeats", beats)
    monkeypatch.setattr(srv, "arc_not_ready_chips", lambda b: set())
    monkeypatch.setattr(srv, "_live_offbus_state", lambda: {})
    # Like the real one: every KMD node, a chip reading all-ones included.
    monkeypatch.setattr(
        srv, "chip_pci_bdfs", lambda: {i: a for i, a in enumerate(GALAXY_BUS_IDS) if i not in st["off"]}
    )
    # Anything that would move a rung or touch a chip fails the test outright.
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: pytest.fail("the report spawned a rung"))
    st["m"] = m
    feeds = dict(hr._deadline_feeds)
    yield st
    hr._deadline_feeds.clear()
    hr._deadline_feeds.update(feeds)  # the broker's own I76 feed stays registered for later tests


def _payload():
    return srv._health_payload(now=datetime.fromtimestamp(T0))


def _episode() -> str:
    return srv.device_hold_episode_since


def _deny(m, until: float, **kw) -> None:
    """What the governor's refusal sets (base._deny_power_cycle): the record and this episode's retry."""
    m.power_cycle_cooldown_state = _cool(until=until, **{"last": START - 1500, **kw})
    m.power_cycle_retry = (until, _episode())


def _rows(p):
    return {r["bus_group"]: r for r in p["trays_present"]["trays"]}


# ---- each hold kind ------------------------------------------------------------------------------


def test_class_a_whole_tray_reports_its_tray_by_bus_and_the_ceiling_window(held):
    held["off"] = set(TRAY_CHIPS[0x40])
    p = _payload()
    assert p["hold_class"] == "A"
    assert p["hold_started_at"] == _iso(START)
    assert (p["next_rung"], p["next_rung_at"]) == (_BOUND_RUNG, _iso(START + 600))
    assert p["next_rung_source"] == "hold bound (600s)"
    rows = _rows(p)
    assert rows["0x40"]["tray"] == 2 and rows["0x40"]["chips_present"] == [] and rows["0x40"]["arc_alive"] == []
    assert rows["0x00"]["chips_present"] == TRAY_CHIPS[0x00] == rows["0x00"]["arc_alive"]
    assert all(r["verified"] is False and r["read_at"] == _iso(T0) for r in rows.values())
    assert p["trays_present"]["verified"] is False and "not verified" in p["trays_present"]["label"]


def test_class_a_inside_the_cooldown_reports_the_cycle_at_the_cooldown_end(held):
    held["off"] = set(TRAY_CHIPS[0x40])
    _deny(held["m"], START + 300, last=START - 1500)
    p = _payload()
    assert p["hold_class"] == "A"
    assert (p["next_rung"], p["next_rung_at"]) == ("power cycle", _iso(START + 300))
    assert p["next_rung_source"] == "power_cycle_cooldown"
    assert p["power_cycle_cooldown"]["next_deadline"] == _iso(START + 300), "the I65 field is unchanged"


def test_class_b1_isolated_chips_report_the_off_bus_one_shot(held, monkeypatch):
    """The one-shot's own wait (one sampler interval on a tenant-free box, the tenant bound after a
    tenant deferred it), not the off-bus ceiling."""
    held["off"] = {3, 4}
    monkeypatch.setattr(srv, "isolated_chips", {"3", "4"})
    p = _payload()
    assert p["hold_class"] == "B1"
    assert (p["next_rung"], p["next_rung_source"]) == ("forced ladder", "off-bus one-shot")
    assert p["next_rung_at"] == _iso(START + SAMPLE_INTERVAL_SEC) and p["next_rung_due"] is True
    monkeypatch.setattr(srv, "device_hold_offbus_tenant_wait", True)
    p = _payload()
    assert p["next_rung_at"] == _iso(START + 120) and p["next_rung_due"] is False
    assert _rows(p)["0x00"]["chips_present"] == [0, 1, 2, 5, 6, 7]


def test_class_b1_after_its_one_shot_reports_the_ceiling(held, monkeypatch):
    held["off"] = {3}
    monkeypatch.setattr(srv, "isolated_chips", {"3"})
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    p = _payload()
    assert p["hold_class"] == "B1" and p["next_rung_source"] == "hold bound (600s)"


def test_class_b2_part_of_one_tray(held):
    held["off"] = set(TRAY_CHIPS[0x80][:5])
    p = _payload()
    assert p["hold_class"] == "B2"
    assert _rows(p)["0x80"]["chips_present"] == TRAY_CHIPS[0x80][5:]


def test_class_c_all_off_lists_every_tray_empty(held):
    held["off"] = set(range(EXPECTED))
    p = _payload()
    assert p["hold_class"] == "C"
    rows = _rows(p)
    assert set(rows) == {"0x00", "0x40", "0x80", "0xc0"}
    assert all(r["chips_present"] == [] and r["chips_expected"] == 8 for r in rows.values())


def test_class_d_present_mesh_with_a_dead_arc(held):
    """A present mesh is class D. A chip reading all-ones counts off the bus (B1) instead, and is
    neither present nor ARC-alive in its tray's row."""
    p = _payload()
    assert p["hold_class"] == "D"
    assert all(r["chips_present"] == r["arc_alive"] and len(r["chips_present"]) == 8 for r in _rows(p).values())
    held["dead"] = {9}
    p = _payload()
    assert p["hold_class"] == "B1"
    assert 9 not in sum((r["chips_present"] for r in _rows(p).values()), [])


def test_class_d_arc_not_ready_chip_is_present_but_not_alive(held, monkeypatch):
    """ARC not ready: on the bus (so the mesh is present, class D), but not ARC-alive."""
    held["off"] = {9}  # missing from the heartbeat read...
    monkeypatch.setattr(srv, "arc_not_ready_chips", lambda b: {"9"})
    monkeypatch.setattr(srv, "chip_pci_bdfs", lambda: dict(enumerate(GALAXY_BUS_IDS)))  # ...still on the bus
    p = _payload()
    assert p["hold_class"] == "D"
    row = next(r for r in _rows(p).values() if 9 in r["chips_present"])
    assert 9 not in row["arc_alive"]


def test_fabric_77_only_hold_names_the_10_min_bound(held, monkeypatch):
    """Below the bound a 77-only hold is never forced; at the bound it runs the ladder (spec 04 I76)."""
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: True)
    p = _payload()
    assert p["hold_class"] == "D"
    assert (p["next_rung_source"], p["next_rung_at"], p["next_rung_due"]) == (
        "hold bound (600s)",
        _iso(START + 600),
        False,
    )
    assert _fires(START + 599, monkeypatch) == [] and _fires(START + 600, monkeypatch) == ["ceiling_window"]


def test_force_escalate_off_names_the_10_min_bound(held, monkeypatch):
    """The kill-switch holds every forced run back below the bound, never past it (spec 04 I76): an
    off-bus one-shot due after one sampler tick fires at the 10-min mark instead."""
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    held["off"] = {3, 4}
    monkeypatch.setattr(srv, "isolated_chips", {"3", "4"})
    p = _payload()
    assert (p["next_rung_source"], p["next_rung_at"]) == ("off-bus one-shot", _iso(START + 600))
    assert _fires(START + 599, monkeypatch) == [] and _fires(START + 600, monkeypatch) == ["offbus_one_shot"]


def test_ac_needed_reports_the_recheck(held):
    held["off"] = set(TRAY_CHIPS[0x40])
    _deny(held["m"], T0 + 240, last=T0 - 60, ac_power_needed={"attempts": 2})
    p = _payload()
    assert p["hold_class"] == "AC-needed"
    assert (p["next_rung"], p["next_rung_at"]) == ("AC-power recheck of the bus", _iso(T0 + 240))


def test_missed_power_cycle_retry_is_named(held):
    held["off"] = set(TRAY_CHIPS[0x40])
    _deny(held["m"], T0 + 30, last=T0 - 900, missed=1)
    p = _payload()
    assert p["next_rung"] == "power cycle (retry of a missed request)"
    assert p["next_rung_source"] == "power_cycle_missed_retry" and p["next_rung_at"] == _iso(T0 + 30)


def test_a_later_ceiling_window_is_reported_after_the_first(held, monkeypatch):
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    held["off"] = set(TRAY_CHIPS[0x40])
    p = srv._health_payload(now=datetime.fromtimestamp(START + 700))
    assert p["next_rung_at"] == _iso(START + 1200) and p["next_rung_source"] == "hold ceiling window 2"


def test_the_broker_registers_its_hold_bound_feed(held, monkeypatch):
    """Spec 04 I75/I76: the broker's own bound is the registered feed, and it follows the ceiling."""
    assert hr._deadline_feeds.get("hold_bound") is srv._hold_bound_feed
    monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 1800)
    held["off"] = {3}
    p = _payload()
    assert (p["next_rung_at"], p["next_rung_source"]) == (_iso(START + 1800), "hold bound (1800s)")


def test_the_hold_bound_candidate_names_the_cooldown_only_for_its_host_rung():
    """The ladder runs at the mark even inside the cooldown; only the host rung waits for its end."""
    c = hr.hold_bound_candidate(START, T0, START + 1800)
    assert c.at == START + 600 and c.source == "hold bound (600s)"
    assert c.rung == f"full ladder, then power cycle at the cooldown's end ({_iso(START + 1800)})"
    assert hr.hold_bound_candidate(START, T0, None).rung == "full ladder, then power cycle"
    assert hr.hold_bound_candidate(START, T0, START + 300).rung == "full ladder, then power cycle", "ends first"
    assert hr.hold_bound_candidate(START, T0, START + 1800, host_rung=None).rung == "full ladder"


def test_a_broken_feed_reports_nothing(held):
    hr.register_deadline_feed("hold_bound", lambda *a: 1 / 0)
    held["off"] = set(TRAY_CHIPS[0x40])
    assert _payload()["next_rung_source"] == "hold ceiling window 1", "the chain still names the mark"


def test_recovering_reports_too_and_healthy_does_not(held):
    held["state"] = ServerState.RECOVERING
    assert _payload()["hold_class"] == "D"
    held["state"] = ServerState.HEALTHY
    p = _payload()
    assert not {"hold_class", "hold_started_at", "next_rung", "next_rung_at", "trays_present"} & set(p)


def test_no_baseline_and_no_tables_report_unknown(held, monkeypatch):
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 0)
    p = _payload()
    assert p["hold_class"] is None and p["trays_present"] is None
    assert p["hold_started_at"] == _iso(START), "the start and the next rung need no chip read"
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: EXPECTED)
    monkeypatch.setattr(srv, "_ubb_bus_id_tables", lambda: None)
    p = _payload()
    assert p["hold_class"] == "D" and p["trays_present"] is None


def test_the_report_is_read_only(held):
    """No rung clock moves, the hold stays, the FSM keeps its state, no job is admitted."""
    held["off"] = set(TRAY_CHIPS[0x40])
    jobs_before = dict(srv.jobs)
    before = (srv.device_hold_escalate_bucket, srv.device_hold_offbus_escalated, srv.device_hold_episode_since)
    p = _payload()
    assert p["held"] is True and p["status"] == "degraded"
    assert (srv.device_hold_escalate_bucket, srv.device_hold_offbus_escalated, srv.device_hold_episode_since) == before
    assert srv.fsm.state is ServerState.DOWN and dict(srv.jobs) == jobs_before
    assert "healthy" not in str(p["trays_present"]).lower()


# ---- review findings: the report mirrors the watchdog's clocks and guards -------------------------


def _fires(t: float, monkeypatch) -> list:
    """The trigger kinds _check_hold_deadline asks a forced spawn for at epoch ``t``. The spawn
    declines, so no latch moves and the call repeats."""
    asked: list = []

    def spawn():
        # The real spawn's first guard: the kill-switch declines it below the 10-min bound (I76).
        if srv._force_escalate_enabled() or t - START >= srv._stuck_hold_ceiling_sec():
            asked.append(srv.device_hold_escalation_kind)
        return False

    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", spawn)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 10**6)  # keep the deadline alert quiet
    srv._check_hold_deadline(now=datetime.fromtimestamp(t))
    return asked


def _gated(monkeypatch, **pending) -> None:
    """An off-bus whole-tray hold the gate refuses, its one-shot spent, the ladder's safe rungs run."""
    monkeypatch.setattr(srv, "isolated_chips", {str(i) for i in TRAY_CHIPS[0x40]})
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)
    opc._pending.update(episode=_episode(), ladder_ran=True, spawned=False, **pending)


@pytest.mark.parametrize(
    "pending, cmd, at, source",
    [
        ({"gated": True}, True, START + 300, "gated off-bus grace"),
        (
            {"handoff": {"deadline": START + 1200, "why": "w", "latched": "", "operator_seen": "none"}},
            False,
            START + 1200,
            "gated off-bus operator hand-off bound",
        ),
    ],
)
def test_the_off_bus_forced_pass_bounds_are_named_when_they_fire(held, monkeypatch, pending, cmd, at, source):
    """F1: the watchdog forces a pass at each of these bounds, before the ceiling window (the hand-off
    one outlasts window 1 and is reported once that window is spent)."""
    held["off"] = set(TRAY_CHIPS[0x40])
    _gated(monkeypatch, **pending)
    if cmd:
        monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/bin/true")
    if at > START + 600:
        monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
        monkeypatch.setattr(srv, "_stuck_hold_ceiling_sec", lambda: 1800)
    p = _payload()
    assert (p["next_rung"], p["next_rung_source"], p["next_rung_at"]) == ("forced ladder", source, _iso(at))
    assert not _fires(at - 1, monkeypatch) and _fires(at, monkeypatch), "the watchdog fires at the reported time"
    opc.mark_spawned(_episode())
    assert srv._health_payload(now=datetime.fromtimestamp(T0))["next_rung_source"] != source, "spent: not again"


def test_the_off_bus_pass_waits_for_the_ladder_to_have_run(held, monkeypatch):
    held["off"] = set(TRAY_CHIPS[0x40])
    _gated(monkeypatch, gated=True)
    opc._pending["ladder_ran"] = False
    monkeypatch.setenv("TT_DEVICE_MCP_POWER_CYCLE_CMD", "/bin/true")
    assert _payload()["next_rung_source"] == "hold bound (600s)"
    assert opc.watchdog_due_at(_episode()) is None and opc.watchdog_due_at("other") is None


def test_a_declined_window_stays_reported_as_due(held, monkeypatch):
    """F2a: window 1 is due at 600 s; its spawn was declined, so at 700 s it is still window 1 (the
    watchdog retries it every tick), never window 2 at 1200 s."""
    held["off"] = set(TRAY_CHIPS[0x40])
    p = srv._health_payload(now=datetime.fromtimestamp(START + 700))
    assert (p["next_rung_source"], p["next_rung_at"], p["next_rung_due"]) == (
        "hold ceiling window 1",
        _iso(START + 600),
        True,
    )
    assert _fires(START + 700, monkeypatch) == ["ceiling_window"]
    assert _fires(START + 599, monkeypatch) == [] and _fires(START + 600, monkeypatch) == ["ceiling_window"]


@pytest.mark.parametrize("failed", [False, True])
def test_the_bound_host_rung_follows_the_bound_ladder(held, monkeypatch, failed):
    """Spec 04 I76: after the bound's ladder the host rung is next, at the end of the post-ladder check
    bound, or at once when that full check failed; reported where the watchdog asks it."""
    held["off"] = {3, 4, 5, 6}
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_hold_bound_episode", _episode())
    monkeypatch.setattr(srv, "_hold_bound_ladder_age", 620.0)
    monkeypatch.setattr(srv, "_hold_bound_ladder_recovered", True)
    monkeypatch.setattr(srv, "_hold_bound_cycle_age", None)
    monkeypatch.setattr(srv, "_hold_bound_rung_pending", False)
    monkeypatch.setattr(srv, "_hold_bound_check_failed_age", 700.0 if failed else None)
    now = START + 720
    p = srv._health_payload(now=datetime.fromtimestamp(now))
    at = now if failed else START + 620 + srv._HOLD_BOUND_CHECK_SEC
    assert (p["next_rung"], p["next_rung_source"], p["next_rung_at"]) == (
        "power cycle",
        "hold bound host rung",
        _iso(at),
    )
    assert _fires(now, monkeypatch) == (["hold_bound_cycle"] if failed else [])
    assert _fires(at, monkeypatch) == ["hold_bound_cycle"]


def test_no_episode_clock_names_no_forced_rung(held, monkeypatch):
    """F2b: the watchdog keys on the episode clock and fires nothing without it, so the report names
    no forced rung (the start may still come from the held stamp)."""
    held["off"] = set(TRAY_CHIPS[0x40])
    monkeypatch.setattr(srv, "isolated_chips", {"9"})
    monkeypatch.setattr(srv, "device_hold_episode_since", "")
    monkeypatch.setattr(srv, "device_held_since", datetime.fromtimestamp(START).isoformat())
    _deny(held["m"], T0 + 30)
    p = _payload()
    assert p["next_rung"] is None and p["next_rung_at"] is None and p["next_rung_due"] is False
    assert p["hold_started_at"] == _iso(START)
    assert _fires(START + 3600, monkeypatch) == []


@pytest.mark.parametrize("case", ["force_off", "only_77", "other_episode", "no_retry"])
def test_the_cooldown_retry_is_named_only_when_the_watchdog_asks_it(held, monkeypatch, case):
    """F2c: the watchdog asks a refused power cycle again only in the episode it was refused in,
    and below the 10-min bound only through the forced spawn (kill-switch on) and never on a 77-only
    hold; from the bound on it asks it then (spec 04 I76)."""
    held["off"] = set(TRAY_CHIPS[0x40])
    _deny(held["m"], START + 300, last=START - 1500)
    if case == "force_off":
        monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    elif case == "only_77":
        monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: True)
    elif case == "other_episode":
        held["m"].power_cycle_retry = (START + 300, "2020-01-01T00:00:00")
    else:
        held["m"].power_cycle_retry = None
    p = _payload()
    if case in ("force_off", "only_77"):
        assert (p["next_rung_source"], p["next_rung_at"]) == ("power_cycle_cooldown", _iso(START + 600))
        assert _fires(START + 599, monkeypatch) == []
        assert _fires(START + 600, monkeypatch)[0] == "power_cycle_cooldown", "the watchdog's first ask"
    else:
        assert p["next_rung_source"] != "power_cycle_cooldown"
    assert "power_cycle_cooldown" not in _fires(START + 300, monkeypatch)
    assert p["power_cycle_cooldown"]["next_deadline"] == _iso(START + 300), "the cooldown end still shows"


def test_the_cooldown_retry_fires_when_reported(held, monkeypatch):
    held["off"] = set(TRAY_CHIPS[0x40])
    _deny(held["m"], START + 300, last=START - 1500)
    assert _payload()["next_rung_at"] == _iso(START + 300)
    assert "power_cycle_cooldown" not in _fires(START + 299, monkeypatch)
    assert "power_cycle_cooldown" in _fires(START + 300, monkeypatch)


def test_a_pending_host_action_is_reported_and_left_alone(held, monkeypatch):
    """F2c: while a requested host action is pending every forced spawn declines; the report names
    the request and its window end, and reading it neither drops nor journals the request."""
    held["off"] = set(TRAY_CHIPS[0x40])
    _deny(held["m"], T0 + 30)
    monkeypatch.setattr(srv, "host_action_pending_sec", lambda: 900.0)
    req = ("power-cycle", srv.time.monotonic() - 100)
    held["m"].host_action_requested = req
    p = _payload()
    assert p["next_rung_source"] == "host_action_pending" and "power-cycle" in p["next_rung"]
    assert abs(datetime.fromisoformat(p["next_rung_at"]).timestamp() - (T0 + 800)) <= 2
    held["m"].host_action_requested = ("reboot", srv.time.monotonic() - 1000)  # expired
    expired = held["m"].host_action_requested
    events: list = []
    monkeypatch.setattr(srv, "health_event", lambda *a, **k: events.append(a))
    assert _payload()["next_rung_source"] == "power_cycle_cooldown", "past the window the retry is next"
    assert held["m"].host_action_requested is expired and not events, "the report expires nothing"


def test_an_all_ones_chip_is_in_no_tray_row(held):
    """F3: chip_pci_bdfs lists an all-ones chip's KMD node; the hold counts it off the bus, so its
    tray row does not list it, and the classifier still sees the ladder's input."""
    held["dead"] = {9}
    p = _payload()
    assert 9 in srv.chip_pci_bdfs(), "the fixture lists it like the real enumeration"
    assert p["hold_class"] == "B1"
    rows = _rows(p)
    assert all(9 not in r["chips_present"] and 9 not in r["arc_alive"] for r in rows.values())
    assert sum(len(r["chips_present"]) for r in rows.values()) == EXPECTED - 1


# ---- the pure helpers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "off, whole, ac, want",
    [
        (0, False, False, "D"),
        (1, False, False, "B1"),
        (3, False, False, "B1"),
        (4, False, False, "B2"),
        (8, True, False, "A"),
        (16, True, False, "A"),
        (9, False, False, "B2"),
        (32, False, False, "C"),
        (8, True, True, "AC-needed"),
    ],
)
def test_classify_hold(off, whole, ac, want):
    assert hr.classify_hold(expected=EXPECTED, off_bus=off, whole_trays=whole, ac_power_needed=ac) == want
    assert want in hr.HOLD_CLASSES


def test_classify_hold_with_no_baseline():
    assert hr.classify_hold(expected=0, off_bus=0, whole_trays=False) is None


def test_trays_group_by_bus_never_by_index():
    """Blackhole kernel order: chips 16-23 on 0xC1-0xC8 (tray 3), 24-31 on 0x81-0x88 (tray 4)."""
    present = {**{i: 0xC1 + i - 16 for i in range(16, 24)}, **{i: 0x81 + i - 24 for i in range(24, 32)}, 40: 0x21}
    rows = {
        r["bus_group"]: r
        for r in hr.trays_present(present, {str(i) for i in range(16, 32)}, {0xC0: 3, 0x80: 4}, "t", 8)
    }
    assert rows["0xc0"]["tray"] == 3 and rows["0xc0"]["chips_present"] == list(range(16, 24))
    assert rows["0x80"]["tray"] == 4 and rows["0x80"]["chips_present"] == list(range(24, 32))
    assert rows["0x20"]["tray"] is None and rows["0x20"]["chips_present"] == [40] and rows["0x20"]["arc_alive"] == []


def test_cooldown_candidate_and_next_rung():
    assert hr.cooldown_candidate(None, T0) == hr.RungCandidate(T0, "power cycle", "power_cycle_cooldown")
    assert hr.cooldown_candidate({"missed": 1}, T0).source == "power_cycle_missed_retry"
    a = hr.RungCandidate(T0 + 5, "x", "a")
    b = hr.RungCandidate(T0 + 1, "y", "b")
    assert hr.next_rung([None, a, b]) is b and hr.next_rung([]) is None


# ---- rungs rollout-3 added beside the watchdog chain (spec 04 I73, I70) ---------------------------


def test_a_class_a_hold_in_the_cooldown_has_no_second_10_min_trigger(held, monkeypatch):
    """Spec 04 I73/I76: a whole tray whose cycle the cooldown refuses gets its full ladder only in the
    10-min hold bound's run. No separate class-A start exists: with the first ceiling window spent, the
    report names the next window, and no class-A kind fires 10 min after the last cycle or boot."""
    held["off"] = set(TRAY_CHIPS[0x40])
    monkeypatch.setattr(held["m"], "whole_tray_off", list(TRAY_CHIPS[0x40]))
    monkeypatch.setattr(held["m"], "_class_a_ladder_anchor", None)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    _deny(held["m"], START + 400 + 1800, last=START + 400)
    assert _payload()["next_rung_source"] == "hold ceiling window 2"
    assert "class_a_ladder" not in _fires(START + 1000, monkeypatch)


def test_the_unkillable_leftover_host_rung_is_named(held, monkeypatch):
    """Spec 04 I70: the sampler's host rung for an unkillable leftover, at its budget's end, then one
    retry interval after each try; never moved to the 10-min floor (it is no forced spawn)."""
    held["off"] = set(TRAY_CHIPS[0x40])
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    mono = srv.time.monotonic()
    monkeypatch.setattr(srv, "_leftover_unkillable_mono", mono - 100)
    monkeypatch.setattr(srv, "_leftover_rung_last_try", 0.0)
    monkeypatch.setattr(srv, "_leftover_rung_stage", "")
    p = _payload()
    assert (p["next_rung"], p["next_rung_source"]) == ("power cycle", "unkillable leftover budget")
    assert abs(datetime.fromisoformat(p["next_rung_at"]).timestamp() - (T0 + 500)) <= 2
    monkeypatch.setattr(srv, "_leftover_unkillable_mono", mono - 700)
    monkeypatch.setattr(srv, "_leftover_rung_last_try", mono - 20)
    monkeypatch.setattr(srv, "_leftover_rung_stage", "held")
    p = _payload()
    assert abs(datetime.fromisoformat(p["next_rung_at"]).timestamp() - (T0 + 40)) <= 2
    monkeypatch.setattr(srv, "_leftover_rung_stage", "no-host-rung")
    assert _payload()["next_rung_source"] != "unkillable leftover budget"
    monkeypatch.setattr(srv, "_leftover_rung_stage", "")
    monkeypatch.setattr(srv, "_leftover_unkillable_mono", None)
    assert _payload()["next_rung_source"] != "unkillable leftover budget"


# ---- one routing for the report and the watchdog (spec 04 I75) -----------------------------------

_ROUTE_RUNG = {srv.ROUTE_LADDER: "forced ladder", srv.ROUTE_HOST_RUNG: "power cycle"}


def _acts_at(t: float, monkeypatch, read_at: float):
    """What the watchdog really does at epoch ``t``: the first kind its tick spawns, routed by the
    forced run's own routing (the monotonic clock moved on by ``t - read_at``). None when no spawn
    acts: nothing fires, or the run returns on a pending post-ladder check or a startup stand-down."""
    kinds = _fires(t, monkeypatch)
    if not kinds:
        return None
    route = srv._forced_run_route(kinds[0], t - START, srv.time.monotonic() + (t - read_at))
    return _ROUTE_RUNG.get(route.action)


def _first_act(read_at: float, until: float, monkeypatch, step: float = 10.0):
    t = read_at
    while t <= until:
        act = _acts_at(t, monkeypatch, read_at)
        if act is not None:
            return t, act
        t += step
    return None


def _report(read_at: float):
    p = srv._health_payload(now=datetime.fromtimestamp(read_at))
    return p["next_rung"], p["next_rung_source"], p["next_rung_at"]


def _bound_ladder_ran(monkeypatch, lad: float, recovered: bool) -> None:
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    monkeypatch.setattr(srv, "_hold_bound_episode", _episode())
    monkeypatch.setattr(srv, "_hold_bound_ladder_age", lad)
    monkeypatch.setattr(srv, "_hold_bound_ladder_recovered", recovered)
    monkeypatch.setattr(srv, "_hold_bound_cycle_age", None)
    monkeypatch.setattr(srv, "_hold_bound_rung_pending", False)
    monkeypatch.setattr(srv, "_hold_bound_check_failed_age", None)


@pytest.mark.parametrize(
    "recovered, at, source",
    [(True, 1300, "hold bound host rung"), (False, 1200, "hold bound host rung at hold ceiling window 2")],
)
def test_the_report_names_the_rung_the_forced_run_routes_to(held, monkeypatch, recovered, at, source):
    """A bound ladder ran at 1000 s. Window 2 (1200 s) is then no ladder: while its post-ladder check
    is pending the run returns, so the host rung follows at the check's end (1300 s); when that ladder
    did not bring the mesh back, window 2 itself goes to the host rung. The report answers what the
    watchdog's own routing does, at the same time."""
    held["off"] = {3, 4, 5, 6}
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    _bound_ladder_ran(monkeypatch, 1000.0, recovered)
    read_at = START + 1100
    assert _report(read_at) == ("power cycle", source, _iso(START + at))
    assert _first_act(read_at, START + 1900, monkeypatch) == (START + at, "power cycle")


def test_a_class_a_hold_inside_the_check_window_reports_the_host_rung(held, monkeypatch):
    """A whole tray off while the bound ladder's check (900 s + 300 s) is pending: no class-A start
    fires 10 min after the last cycle (1000 s), and the report names the act that comes first."""
    held["off"] = set(TRAY_CHIPS[0x40])
    monkeypatch.setattr(held["m"], "whole_tray_off", list(TRAY_CHIPS[0x40]))
    monkeypatch.setattr(held["m"], "_class_a_ladder_anchor", None)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    _deny(held["m"], START + 400 + 1800, last=START + 400)
    _bound_ladder_ran(monkeypatch, 900.0, True)
    read_at = START + 950
    assert "class_a_ladder" not in _fires(START + 1000, monkeypatch)
    rung, source, at = _report(read_at)
    assert _first_act(read_at, START + 2400, monkeypatch) == (datetime.fromisoformat(at).timestamp(), rung)


def test_a_startup_verify_stand_down_is_reported_with_its_bounded_end(held, monkeypatch):
    """Spec 03 I75: a ceiling window due while the startup gate still owns the hold stands down; the
    report names the gate first and the window's ladder at the stand-down's bounded end, when the
    watchdog's run stops standing down."""
    held["state"] = ServerState.RECOVERING
    srv.fsm.on_fault("startup_unverified", detail="broker start", dirty=False)
    monkeypatch.setattr(srv, "jobs", {})

    class _Pending:
        def done(self):
            return False

    monkeypatch.setattr(srv, "_startup_verify_task", _Pending())
    monkeypatch.setattr(srv, "_startup_verify_ready_at", srv.time.monotonic() - 30)
    read_at = START + 620
    assert srv._forced_run_route("ceiling_window", 620).action == srv.ROUTE_STAND_DOWN
    rung, source, at = _report(read_at)
    end = read_at + srv.STARTUP_VERIFY_DEFER_MAX_SEC - 30
    assert (rung, source) == (
        "startup health check, then forced ladder",
        "startup gate stand-down (hold ceiling window 1)",
    )
    assert abs(datetime.fromisoformat(at).timestamp() - end) <= 2
    t, act = _first_act(read_at, end + 60, monkeypatch, step=5.0)
    assert act == "forced ladder" and abs(t - end) <= 5


# ---- spec 04 I76 as the /health deadline feed (spec 04 I75) ---------------------------------------


def _class(held, monkeypatch, cls: str) -> None:
    if cls == "A":
        held["off"] = set(TRAY_CHIPS[0x40])
    elif cls == "B1":
        held["off"] = {3}
        monkeypatch.setattr(srv, "isolated_chips", {"3"})
        monkeypatch.setattr(srv, "device_hold_offbus_escalated", True)  # its one-shot spent
    else:
        monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: True)


@pytest.mark.parametrize("cls", ["A", "B1", "D"])
def test_the_bound_mark_is_reported_for_each_fault_class(held, monkeypatch, cls):
    """The bound's ladder, then its host rung, at hold start + 600 s; the watchdog fires it then."""
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    _class(held, monkeypatch, cls)
    p = _payload()
    assert p["hold_class"] == cls
    assert (p["next_rung"], p["next_rung_source"], p["next_rung_at"]) == (
        "full ladder, then power cycle",
        "hold bound (600s)",
        _iso(START + 600),
    )
    assert "ceiling_window" not in _fires(START + 599, monkeypatch)
    assert "ceiling_window" in _fires(START + 600, monkeypatch)


@pytest.mark.parametrize("latch", ["fabric_known_fault", "topology_hardware_fault"])
def test_a_latched_hold_reports_the_same_mark(held, monkeypatch, latch):
    """Spec 03 I42/I46 latches change what the check after the ladder sees, never when the bound fires."""
    monkeypatch.setattr(srv.health_monitor, latch, "1 port missing vs the cabling descriptor", raising=False)
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: True)
    p = _payload()
    assert (p["next_rung_source"], p["next_rung_at"]) == ("hold bound (600s)", _iso(START + 600))
    assert _fires(START + 600, monkeypatch) == ["ceiling_window"]


def test_a_cooldown_hold_reports_the_ladder_at_the_mark_and_the_host_rung_at_its_end(held, monkeypatch):
    """Spec 04 I65: inside a boot/post-cycle cooldown the ladder still runs at the mark;
    only the host rung waits. Once the bound's ladder ran, the watchdog's own chain names what is next."""
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    held["off"] = {3, 4, 5, 6}
    end = START + 1800
    held["m"].power_cycle_cooldown_state = _cool(until=end, last=START - 100)
    p = _payload()
    assert (p["next_rung_source"], p["next_rung_at"]) == ("hold bound (600s)", _iso(START + 600))
    assert p["next_rung"] == f"full ladder, then power cycle at the cooldown's end ({_iso(end)})"
    assert _fires(START + 600, monkeypatch) == ["ceiling_window"]
    # The bound's ladder ran and did not heal; its host rung was denied by the cooldown.
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    _bound_ladder_ran(monkeypatch, 600.0, False)
    monkeypatch.setattr(srv, "_hold_bound_cycle_age", 600.0)
    _deny(held["m"], end, last=START - 100)
    assert srv._hold_bound_feed(START, START + 700, end) is None, "spent: the chain names what is next"
    assert _report(START + 700)[1] != "hold bound (600s)"


def test_after_the_bound_ladder_the_chain_names_the_host_rung(held, monkeypatch):
    held["off"] = {3, 4, 5, 6}
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    _bound_ladder_ran(monkeypatch, 600.0, True)
    assert srv._hold_bound_feed(START, START + 700, None) is None
    assert _report(START + 700)[1] == "hold bound host rung"


@pytest.mark.parametrize("case", ["window_spent", "host_action", "tenant", "rung_pending", "past_mark"])
def test_the_bound_feed_is_silent_when_its_mark_is_not_next(held, monkeypatch, case):
    held["off"] = {3, 4, 5, 6}
    monkeypatch.setattr(srv, "_hold_bound_episode", _episode())
    now = T0
    if case == "window_spent":
        monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    elif case == "host_action":
        monkeypatch.setattr(held["m"], "host_action_requested", ("power_cycle", srv.time.monotonic()))
    elif case == "tenant":
        monkeypatch.setattr(srv, "_hold_bound_tenant_blocker", "pid 4242 (python3)")
    elif case == "rung_pending":
        monkeypatch.setattr(srv, "_hold_bound_rung_pending", True)
    else:
        now = START + 700  # due and unrun: the chain's window 1 names it as due
    assert srv._hold_bound_feed(START, now, None) is None
    if case == "past_mark":
        p = srv._health_payload(now=datetime.fromtimestamp(now))
        assert (p["next_rung_source"], p["next_rung_due"]) == ("hold ceiling window 1", True)


def test_the_bound_feed_names_the_host_rung_the_box_opts_into(held, monkeypatch):
    held["off"] = {3, 4, 5, 6}
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: False)
    assert srv._hold_bound_feed(START, T0, None).rung == "full ladder"


def test_the_bound_feed_yields_to_a_startup_stand_down(held, monkeypatch):
    monkeypatch.setattr(srv, "_forced_run_route", lambda kind, age, mono=None: srv.ForcedRoute(srv.ROUTE_STAND_DOWN))
    held["off"] = {3, 4, 5, 6}
    assert srv._hold_bound_feed(START, T0, None) is None


def test_a_healthy_device_reports_no_bound(held, monkeypatch):
    monkeypatch.setattr(srv, "_auto_power_cycle_enabled", lambda: True)
    held["state"] = ServerState.HEALTHY
    p = _payload()
    assert not {"next_rung", "next_rung_at", "next_rung_source"} & set(p)


def test_the_bound_feed_moves_no_clock(held, monkeypatch):
    """Reporting only (TTR effect 0): reading /health leaves every I76 clock where it was."""
    held["off"] = {3, 4, 5, 6}
    names = (
        "_hold_bound_ladder_age",
        "_hold_bound_cycle_age",
        "_hold_bound_rung_pending",
        "device_hold_escalate_bucket",
    )
    before = {n: getattr(srv, n) for n in names}
    _payload()
    srv._hold_bound_feed(START, T0, None)
    assert {n: getattr(srv, n) for n in names} == before


def test_a_power_cycle_the_governor_still_gates_is_reported_at_the_latest_gate(held, monkeypatch):
    """Spec 04 I75: window 2 routes to the host rung at 1200 s, but the governor's backstop and then
    its post-boot cooldown refuse a power cycle until later. next_rung_at and hold_escalation's
    next_deadline give the later gate's end and the source names both waits, not the window alone."""
    held["off"] = {3, 4, 5, 6}
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 1)
    _bound_ladder_ran(monkeypatch, 1000.0, False)
    backstop, cooldown = START + 1578, START + 1665
    monkeypatch.setattr(
        held["m"],
        "power_cycle_gates",
        lambda now_epoch=None: [(cooldown, "power-cycle cooldown (host boot)"), (backstop, "power-cycle backstop")],
        raising=False,
    )
    p = srv._health_payload(now=datetime.fromtimestamp(START + 1100))
    assert p["next_rung"] == "power cycle"
    assert p["next_rung_at"] == _iso(cooldown)
    assert p["next_rung_source"].startswith("hold bound host rung")
    assert p["next_rung_source"].endswith(
        f", held by the power-cycle backstop until {_iso(backstop)}, "
        f"then the power-cycle cooldown (host boot) until {_iso(cooldown)}"
    )
    assert p["hold_escalation"]["next_deadline"] == _iso(cooldown)


def test_the_governor_gates_move_only_a_power_cycle_step():
    """A ladder or a check keeps its time; a power cycle already past every gate keeps its own."""
    from tt_device_mcp.health import hold_report

    gates = [(500.0, "power-cycle backstop")]
    out = hold_report.gate_power_cycle(
        [
            hold_report.RungCandidate(100.0, "reset ladder", "class A 10-min ladder"),
            hold_report.RungCandidate(200.0, "power cycle", "w"),
            hold_report.RungCandidate(900.0, "power cycle (retry of a missed request)", "r"),
        ],
        gates,
    )
    assert [(c.at, c.rung) for c in out] == [
        (100.0, "reset ladder"),
        (500.0, "power cycle"),
        (900.0, "power cycle (retry of a missed request)"),
    ]
    assert out[1].source.startswith("w, held by the power-cycle backstop until ")
    assert hold_report.gate_power_cycle(out, []) == out


def test_the_power_cycle_gates_read_marks_nothing(held, monkeypatch):
    """The /health read of the governor's waits is read-only: it sets no refusal record or retry,
    and a failed ledger read gives no gates rather than raising."""
    m = held["m"]
    state, retry = m.power_cycle_cooldown_state, m.power_cycle_retry
    m.power_cycle_gates(START + 100)
    assert (m.power_cycle_cooldown_state, m.power_cycle_retry) == (state, retry)

    def _boom():
        raise OSError("ledger unreadable")

    monkeypatch.setattr(m, "read_auto_recovery_ledger", _boom)
    assert m.power_cycle_gates(START + 100) == []
