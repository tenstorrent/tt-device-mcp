# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A reset that came back with fewer eth links than the mark is not recovered (spec 03 I64, 04 I57).

The fabric traffic pass tests the links it finds, so it can pass on a mesh whose reset left a link
down. While a reset owes its fabric pass (04 I32), the built-in eth read's link count is held to the
mark: a short count fails the pass, is retried once for training, then climbs the ladder. A known
deficit (accepted by the operator, or by a broker power cycle that came back with it) releases,
loudly, and never loops resets.
"""

import json

import pytest

import tt_device_mcp.server as srv
from tests.conftest import patch_recovery
from tests.test_device_safety import _arm_stuck_hold, _setup_selfheal_hold
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import eth_deficit, post_reset
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health import recovery as recovery_pkg
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.evidence import health_dir
from tt_device_mcp.health.monitors import eth
from tt_device_mcp.health.post_reset import FABRIC_DEBT

MARK = 12
_REAL_CLEAR_DIRTY = srv._clear_device_dirty
# What every reset path writes into the debt (spec 04 I32): the operator and ladder `tt-smi -r`, a
# galaxy reset, a bridge chip reset, a UBB tray re-power, and a broker start (after a boot, a
# power cycle or a restart; the startup pass).
RESET_PATHS = ["tt-smi -r 0", "tt-smi -glx_reset", "bridge reset 0000:41:00.0", "ubb tray 2 re-power", "broker start"]


def _probe(monkeypatch, links: int) -> None:
    """The built-in eth read, every core advancing, measuring ``links`` — no spawn."""
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.setattr(eth, "build", lambda: (["probe"], {}))

    async def _check(argv, env, *, timeout_sec, track, cwd=None, reap_sec=0.0):
        return 0, f"eth-links: measured={links} down=0 unreadable=0\nall {links} active-eth core heartbeat(s) advancing"

    monkeypatch.setattr(eth, "check", _check)


@pytest.fixture
def mon(monkeypatch):
    """The health monitor on a 32-chip mesh with a link mark of 12: snapshot and fabric pass stubbed
    healthy, every journal event captured."""
    m = srv.health_monitor
    m.eth_link_drop(MARK)
    events = []
    monkeypatch.setattr(monitor_mod, "health_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(post_reset, "health_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(monitor_mod, "_read_boot_id", lambda: "boot-b")

    async def _snapshot_ok(expected):
        return True, "ok"

    async def _fabric():
        FABRIC_DEBT.note_fabric(True)  # what the real healthy verdict does (monitor.verify_fabric_health)
        return True, "all links healthy"

    monkeypatch.setattr(m, "_verify_device", _snapshot_ok)
    monkeypatch.setattr(m, "verify_fabric_health", _fabric)
    m.events = events
    yield m
    m.last_fabric_ok = None


async def _pass(m, expected=32):
    state = await m.update(phase="verify_device", run_fabric=True, indices=list(range(expected)), expected=expected)
    return state.of("fabric"), state.as_evidence()["fabric"]


def _kinds(m):
    return [k for k, _ in m.events]


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", RESET_PATHS)
async def test_a_reset_that_came_back_short_of_links_fails_its_fabric_pass(mon, monkeypatch, reset):
    FABRIC_DEBT.note_reset(reset)
    _probe(monkeypatch, MARK - 1)
    obs, ev = await _pass(mon)
    assert obs.verdict is Verdict.UNHEALTHY
    assert f"{MARK - 1} of {MARK} baseline eth links" in obs.detail
    assert ev["link_drop"] is True and ev["links"] == MARK - 1 and ev["high_water"] == MARK
    assert FABRIC_DEBT.owed == reset, "the debt the fabric pass settled is owed again"
    assert mon.last_fabric_ok is False
    pend = eth_deficit.read_pending()
    assert pend["links"] == MARK - 1 and pend["boot_id"] == "boot-b" and not pend["reset_spent"]
    assert "eth_link_deficit_held" in _kinds(mon)


@pytest.mark.asyncio
async def test_a_pass_with_no_reset_owed_is_unchanged(mon, monkeypatch):
    """The between-jobs path adds nothing: with no reset owed, a short count is the I28 skip only."""
    _probe(monkeypatch, MARK - 1)
    obs, _ = await _pass(mon)
    assert obs.verdict is Verdict.HEALTHY
    assert eth_deficit.read_pending() is None


@pytest.mark.asyncio
async def test_a_single_chip_is_unaffected(mon, monkeypatch):
    FABRIC_DEBT.note_reset("tt-smi -r 0")
    _probe(monkeypatch, MARK - 1)
    obs, _ = await _pass(mon, expected=1)
    assert obs.verdict is Verdict.HEALTHY


@pytest.mark.asyncio
async def test_a_host_with_no_built_in_count_stays_unchecked(mon, monkeypatch):
    """An operator eth override reports no link count: the traffic pass's verdict stands."""
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    _probe(monkeypatch, MARK - 1)
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "true")
    obs, _ = await _pass(mon)
    assert obs.verdict is Verdict.HEALTHY and mon.last_eth_links is None


@pytest.mark.asyncio
async def test_the_full_count_after_a_reset_passes_and_clears_any_deficit(mon, monkeypatch):
    eth_deficit.write_accepted(MARK - 2, MARK, source="operator")
    eth_deficit.write_pending(MARK - 2, MARK, "boot-a")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    _probe(monkeypatch, MARK)
    obs, _ = await _pass(mon)
    assert obs.verdict is Verdict.HEALTHY and FABRIC_DEBT.owed == ""
    assert eth_deficit.read_accepted() is None and eth_deficit.read_pending() is None
    assert "eth_link_deficit_cleared" in _kinds(mon)


@pytest.mark.asyncio
async def test_a_count_at_the_accepted_deficit_passes_loudly(mon, monkeypatch):
    eth_deficit.write_accepted(MARK - 2, MARK, source="operator", ports=["chip 3 ch 9"])
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    _probe(monkeypatch, MARK - 2)
    obs, _ = await _pass(mon)
    assert obs.verdict is Verdict.HEALTHY and FABRIC_DEBT.owed == ""
    kinds = _kinds(mon)
    assert "eth_link_deficit_accepted" in kinds and "eth_link_deficit_held" not in kinds


@pytest.mark.asyncio
async def test_a_loss_beyond_the_accepted_deficit_is_held(mon, monkeypatch):
    eth_deficit.write_accepted(MARK - 2, MARK, source="operator")
    FABRIC_DEBT.note_reset("tt-smi -glx_reset")
    _probe(monkeypatch, MARK - 3)
    obs, ev = await _pass(mon)
    assert obs.verdict is Verdict.UNHEALTHY and ev["link_drop"]
    assert FABRIC_DEBT.owed
    assert eth_deficit.read_accepted()["links"] == MARK - 2, "the record is never lowered by a loss"


def _ledger(*records):
    (health_dir() / "auto_recovery.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


@pytest.mark.asyncio
async def test_a_deficit_a_broker_power_cycle_came_back_with_is_accepted(mon, monkeypatch):
    eth_deficit.write_pending(MARK - 1, MARK, "boot-a", reset_spent=True)
    _ledger({"action": "reboot", "boot_id": "boot-0"}, {"action": "power-cycle", "boot_id": "boot-a"})
    FABRIC_DEBT.note_reset("broker start")
    _probe(monkeypatch, MARK - 1)
    obs, _ = await _pass(mon)
    assert obs.verdict is Verdict.HEALTHY and FABRIC_DEBT.owed == ""
    acc = eth_deficit.read_accepted()
    assert acc["links"] == MARK - 1 and acc["source"] == "power_cycle"
    assert eth_deficit.read_pending() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ledger",
    [
        [{"action": "reboot", "boot_id": "boot-a"}],  # a warm reboot is no power cycle
        [{"action": "power-cycle", "boot_id": "boot-0"}],  # a cycle that ended another boot
        [],  # a manual reboot: nothing the broker fired
    ],
)
async def test_a_deficit_with_no_broker_power_cycle_since_stays_held(mon, monkeypatch, ledger):
    eth_deficit.write_pending(MARK - 1, MARK, "boot-a", reset_spent=True)
    _ledger(*ledger)
    FABRIC_DEBT.note_reset("broker start")
    _probe(monkeypatch, MARK - 1)
    obs, _ = await _pass(mon)
    assert obs.verdict is Verdict.UNHEALTHY and eth_deficit.read_accepted() is None
    pend = eth_deficit.read_pending()
    assert pend["boot_id"] == "boot-b", "re-stamped with this boot, the one a later power cycle ends"
    assert pend["reset_spent"], "the same deficit keeps its spent reset across the reboot"


# --- the ladder: retried once for training, then one reset rung, then the host rungs ---------------

DROP = {
    "snapshot": {"ok": True},
    "fabric": {"ok": False, "detail": "fabric passed on 11 of 12 baseline eth links", "links": 11, "link_drop": True},
}
FOK = {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "fabric links healthy"}}


def _relift_with_post_reset_verifies(monkeypatch, tmp_path, script):
    """A measured-fabric-failure hold whose first relift resets (spec 03 I56). Verifies before the
    reset read healthy; after it, each verify returns the next ``script`` entry (a DROP also records
    the pending deficit, as the monitor does)."""
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRY_SLEEP_SEC", 0)
    _setup_selfheal_hold(monkeypatch, tmp_path, n_present=32)
    _arm_stuck_hold(monkeypatch, held_minutes_ago=0.05)
    g = srv.galaxy_recovery
    monkeypatch.delattr(g, "_reset_and_verify_device")
    monkeypatch.setattr(srv, "_clear_device_dirty", _REAL_CLEAR_DIRTY)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "true")
    srv.health_monitor.last_fabric_ok = False
    seen = {"resets": 0, "post": 0, "climbs": []}

    async def glx_reset(argv, log):
        seen["resets"] += 1
        FABRIC_DEBT.note_reset(" ".join(argv))
        return 0, ""

    async def verify(expected, log, run_fabric=True, **_):
        if not seen["resets"]:
            return True, dict(FOK)
        ev = script[min(seen["post"], len(script) - 1)]
        seen["post"] += 1
        if ev is DROP:
            eth_deficit.write_pending(11, 12, "boot-a", reset_spent=eth_deficit.reset_spent())
            return False, dict(ev)
        FABRIC_DEBT.note_fabric(True)
        return True, dict(ev)

    async def eth_unreadable(timeout_sec=60.0):
        return None, "eth read timed out"

    async def climb(expected, off_bus_before, log, *, swept=False):
        seen["climbs"].append(swept)

    monkeypatch.setattr(g.mechanism, "reset_with_quiesce", glx_reset)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_unreadable)
    monkeypatch.setattr(g, "_climb_to_host_recovery_after_failed_reset", climb)
    patch_recovery(monkeypatch, "_verify_device", verify)
    return g, seen


@pytest.mark.asyncio
async def test_a_short_count_that_trains_back_on_the_retry_releases(monkeypatch, tmp_path, clear_job_state):
    _, seen = _relift_with_post_reset_verifies(monkeypatch, tmp_path, [DROP, FOK])
    await srv._attempt_idle_relift()
    assert seen["resets"] == 1 and seen["post"] == 2, "the short count is re-checked once after the training wait"
    assert srv.fsm.state is ServerState.HEALTHY and FABRIC_DEBT.owed == ""
    assert not eth_deficit.reset_spent()


@pytest.mark.asyncio
async def test_a_persistent_short_count_holds_and_climbs_without_another_reset(monkeypatch, tmp_path, clear_job_state):
    g, seen = _relift_with_post_reset_verifies(monkeypatch, tmp_path, [DROP])
    await srv._attempt_idle_relift()
    assert seen["resets"] == 1 and seen["post"] == 2, "one reset, its verify and one training retry"
    assert srv.fsm.state is not ServerState.HEALTHY and FABRIC_DEBT.owed
    assert eth_deficit.reset_spent(), "the deficit's one reset rung is spent"
    assert seen["climbs"] == [False], "the failed reset climbs as any failed reset does"
    # The hold-deadline escalation and the next idle escalation reset no more: straight to the climb.
    await g._escalate_stuck_hold(list(range(32)), 32, lambda m: None, force=True)
    assert seen["resets"] == 1, "no reset loop on a known deficit"
    assert seen["climbs"] == [False, True], "the climb skips the last-chance sweep of more resets"


@pytest.mark.asyncio
async def test_the_gate_reset_rung_is_not_fired_again_on_a_spent_deficit(monkeypatch, tmp_path, clear_job_state):
    g, seen = _relift_with_post_reset_verifies(monkeypatch, tmp_path, [DROP])
    eth_deficit.write_pending(11, 12, "boot-a", reset_spent=True)
    outcome = await g._fire_gate_rung(
        "post-job", galaxy_stage_smi_reset(), list(range(32)), 32, lambda m: None, ev=None, beats=None
    )
    assert outcome == recovery_pkg.OUTCOME_WAITING and seen["resets"] == 0


def galaxy_stage_smi_reset():
    from tt_device_mcp.health.recovery import STAGE_SMI_RESET

    return STAGE_SMI_RESET


# --- the operator's command --------------------------------------------------------------------


def _cli(monkeypatch, capsys, *argv):
    from tt_device_mcp import cli

    monkeypatch.setattr("sys.argv", ["tt-device-mcp", *argv])
    monkeypatch.setattr(cli, "health_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("tt_device_mcp.health.evidence.health_event", lambda *a, **k: None)
    rc = cli.main()
    return rc, capsys.readouterr()


def test_the_operator_accepts_shows_and_clears_a_deficit(monkeypatch, capsys):
    srv.health_monitor.eth_link_drop(MARK)
    eth_deficit.write_pending(MARK - 1, MARK, "boot-a")
    rc, out = _cli(monkeypatch, capsys, "eth-links", "accept", "--ports", "chip 3 ch 9")
    assert rc == 0 and "11 of 12" in out.out
    acc = eth_deficit.read_accepted()
    assert acc == {**acc, "links": 11, "high_water": 12, "source": "operator", "ports": ["chip 3 ch 9"]}
    assert eth_deficit.read_pending() is None
    rc, out = _cli(monkeypatch, capsys, "eth-links", "show")
    assert rc == 0 and '"links": 11' in out.out
    assert srv._health_payload()["eth_link_deficit"]["links"] == 11
    rc, out = _cli(monkeypatch, capsys, "eth-links", "clear")
    assert rc == 0 and eth_deficit.read_accepted() is None
    assert srv._health_payload()["eth_link_deficit"] is None


def test_the_operator_cannot_accept_the_full_mark_or_nothing(monkeypatch, capsys):
    srv.health_monitor.eth_link_drop(MARK)
    assert _cli(monkeypatch, capsys, "eth-links", "accept")[0] == 2, "no pending deficit and no --links"
    assert _cli(monkeypatch, capsys, "eth-links", "accept", "--links", str(MARK))[0] == 2
    assert eth_deficit.read_accepted() is None
