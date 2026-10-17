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
    monkeypatch.setattr(m, "progress_retry", None)
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
    yield st
    hr.unregister_deadline_feed("hold_bound")


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
    assert (p["next_rung"], p["next_rung_at"]) == ("forced ladder", _iso(START + 600))
    assert p["next_rung_source"] == "hold ceiling window 1"
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
    assert p["hold_class"] == "B1" and p["next_rung_source"] == "hold ceiling window 1"


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


def test_fabric_77_only_hold_names_no_forced_rung(held, monkeypatch):
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: True)
    p = _payload()
    assert p["hold_class"] == "D"
    assert p["next_rung"] is None and p["next_rung_at"] is None


def test_force_escalate_off_names_no_forced_rung(held, monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_FORCE_ESCALATE", "0")
    held["off"] = set(TRAY_CHIPS[0x40])
    assert _payload()["next_rung"] is None


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


def test_the_hold_bound_feed_wins_when_earlier(held, monkeypatch):
    """The hook the universal hold bound feeds: with a 30-min ceiling, its 10-min mark is next."""
    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_SEC", "1800")
    held["off"] = {3}
    assert _payload()["next_rung_at"] == _iso(START + 1800), "no feed: no 10-min mark is claimed"
    hr.register_deadline_feed("hold_bound", hr.hold_bound_candidate)
    p = _payload()
    assert (p["next_rung_at"], p["next_rung"]) == (_iso(START + 600), "full ladder, then power cycle")


def test_the_hold_bound_inside_the_cooldown_moves_to_its_end():
    c = hr.hold_bound_candidate(START, T0, START + 1800)
    assert c.at == START + 1800 and "after cooldown" in c.source
    assert hr.hold_bound_candidate(START, T0, None).at == START + 600
    assert hr.hold_bound_candidate(START, T0, START + 300).at == START + 600, "cooldown ends before the mark"


def test_a_broken_feed_reports_nothing(held):
    hr.register_deadline_feed("hold_bound", lambda *a: 1 / 0)
    held["off"] = set(TRAY_CHIPS[0x40])
    assert _payload()["next_rung_source"] == "hold ceiling window 1"


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
        if srv._force_escalate_enabled():  # the real spawn's first guard: the kill-switch declines it
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
        ({"ipmi_at": datetime.fromtimestamp(START + 30)}, True, START + 330, "BMC IPMI hand-off bound"),
        ({"tray_walk_at": datetime.fromtimestamp(START + 20)}, True, START + 320, "failed tray walk hand-off bound"),
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
    assert _payload()["next_rung_source"] == "hold ceiling window 1"
    assert opc.watchdog_due_at(_episode()) is None and opc.watchdog_due_at("other") is None


def test_the_i39_progress_retry_is_named(held, monkeypatch):
    """F1: a failed mesh reset that brought chips back retries early, not at the ceiling."""
    held["off"] = {3, 4, 5, 6}
    held["m"].progress_retry = (_episode(), srv.time.monotonic() + 45)
    p = _payload()
    assert p["next_rung_source"] == "progress retry"
    assert abs(datetime.fromisoformat(p["next_rung_at"]).timestamp() - (T0 + 45)) <= 2
    held["m"].progress_retry = (_episode(), srv.time.monotonic() - 1)
    p = _payload()
    assert p["next_rung_at"] == _iso(T0) and p["next_rung_due"] is True
    assert _fires(T0, monkeypatch) == ["progress_retry"]
    held["m"].progress_retry = ("another episode", srv.time.monotonic() - 1)
    assert _payload()["next_rung_source"] == "hold ceiling window 1"


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
    through the forced spawn (kill-switch on), and never on a 77-only hold."""
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
    assert p["next_rung_source"] != "power_cycle_cooldown"
    if case in ("force_off", "only_77"):
        assert p["next_rung"] is None
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
