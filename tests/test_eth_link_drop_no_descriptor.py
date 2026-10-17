# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A link drop on a host with no cabling descriptor fails the full check (spec 03 I28).

With no descriptor the validator builds its mesh from the links it discovers, so a link that went
down is left out and the traffic over the rest passes. The eth read's count below the high-water
mark was the only measurement of the loss, and it was a skip: the post-reset check released the
device short a link. Now such a pass fails the check, the device stays held, and the hold takes the
ladder and then a power cycle. A healthy read still passes.
"""

from datetime import datetime, timedelta

import pytest

from tests import test_dispatch_failure_reset as dfr
from tests.test_dispatch_failure_reset import mesh  # noqa: F401  (fixture)
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as monitor_mod
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitor import FABRIC_LINKS_UNSEEN
from tt_device_mcp.health.monitors import eth, fabric
from tt_device_mcp.health.post_reset import FABRIC_DEBT

LINKS = 12


def _wire(monkeypatch, tmp_path, st, descriptor: str = ""):
    """The broker's own eth read and fabric pass on stand-ins: the eth probe measures ``LINKS`` up
    links, one fewer while ``st['bad']``; the validator (``descriptor`` empty: none configured)
    passes on the links it discovers, as a descriptor-less run does with a link down."""
    m = srv.health_monitor
    hdir = tmp_path / "health"
    hdir.mkdir()
    st.setdefault("events", [])
    monkeypatch.setattr(monitor_mod, "health_dir", lambda: hdir)
    monkeypatch.setattr(monitor_mod, "health_event", lambda ev, **f: st["events"].append(ev))
    monkeypatch.setattr(m, "last_eth_link_drop", "")
    m.eth_link_drop(LINKS)  # the high-water mark the self-test seeded

    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.setattr(eth, "build", lambda: (["probe"], {}))

    async def eth_check(argv, env, *, timeout_sec, track, cwd=None, reap_sec=0.0):
        n = LINKS - 1 if st.get("bad") else LINKS
        return 0, f"eth-links: measured={n} down=0 unreadable=0\nall {n} active-eth core heartbeat(s) advancing"

    monkeypatch.setattr(eth, "check", eth_check)

    async def fabric_check(argv, env, **kw):
        assert "--cabling-descriptor-path" not in argv or descriptor
        return 0, "all links ok\n"

    for attr, value in (
        ("topology_hw_fault", ""),
        ("topology_lift_pending", False),
        ("_fabric_check_cmd", lambda: ""),
        ("_begin_action_row", lambda *a, **k: None),
        ("_write_action_log", lambda *a, **k: None),
    ):
        monkeypatch.setattr(m, attr, value)
    monkeypatch.setattr(fabric, "build_command", lambda descriptor=True: (["run_cluster_validation"], {}))
    monkeypatch.setattr(fabric, "check", fabric_check)
    monkeypatch.setattr(fabric, "_validator_paths", lambda: ("bin", "root", descriptor))
    return m


# --- the monitor's own pass ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad, healthy", [(True, False), (False, True)])
async def test_a_descriptorless_full_check_fails_on_a_link_drop_and_passes_a_full_count(
    monkeypatch, tmp_path, bad, healthy
):
    """The full check (fabric asked, as every post-reset check is): a drop fails the fabric verdict,
    so the pass is unhealthy and settles no post-reset debt; a full count passes (no false hold)."""
    st = {"bad": bad}
    m = _wire(monkeypatch, tmp_path, st)

    async def snapshot_ok(expected):
        return True, "ok"

    monkeypatch.setattr(m, "_verify_device", snapshot_ok)
    monkeypatch.setattr(FABRIC_DEBT, "owed", "glx_reset")
    state = await m.update(phase="post-reset", run_fabric=True, force_fabric=True, indices=[0, 1], expected=2)
    obs = {o.monitor: o for o in state.observations}
    assert state.healthy is healthy
    if bad:
        assert obs["eth_heartbeat"].verdict is Verdict.SKIPPED
        assert obs["fabric"].verdict is Verdict.UNHEALTHY and FABRIC_LINKS_UNSEEN in obs["fabric"].detail
        assert f"{LINKS - 1} of {LINKS}" in obs["fabric"].detail
        assert m.last_fabric_ok is False and FABRIC_DEBT.owed == "glx_reset", "the debt stays owed"
        assert "fabric_links_unseen" in st["events"]
    else:
        assert obs["eth_heartbeat"].verdict is Verdict.HEALTHY and obs["fabric"].verdict is Verdict.HEALTHY
        assert FABRIC_DEBT.owed == "", "a full pass settles the debt"


@pytest.mark.asyncio
async def test_a_descriptor_run_judges_the_links_itself(monkeypatch, tmp_path):
    """With a descriptor the validator compares the links and names what is missing, so a clean
    descriptor run is not overruled by the count (that path is the descriptor's own, I45/I71)."""
    st = {"bad": True}
    m = _wire(monkeypatch, tmp_path, st, descriptor="/x/d")
    await m.verify_eth_heartbeat()
    assert m.last_eth_link_drop
    ok, _ = await m._verify_fabric_health_body(60)
    assert ok is True


def test_an_operator_verification_pass_is_not_adopted_over_a_drop(monkeypatch, tmp_path):
    """The operator verification job's pass (spec 01 I21) is the same traffic pass: not adopted."""
    m = _wire(monkeypatch, tmp_path, {})
    monkeypatch.setattr(m, "last_eth_link_drop", f"{LINKS - 1} of {LINKS} eth link(s) up")
    monkeypatch.setattr(m, "last_fabric_ok", None)
    assert FABRIC_LINKS_UNSEEN in m.adopt_fabric_pass("all links ok")
    assert m.last_fabric_ok is None, "nothing settled"


def test_a_new_link_mark_is_journaled(monkeypatch, tmp_path):
    """The mark comes down only through a new file (an operator's delete, a rewritten one): logged."""
    st = {}
    m = _wire(monkeypatch, tmp_path, st)
    assert "eth_link_baseline_seeded" in st["events"]
    st["events"].clear()
    (tmp_path / "health" / m.ETH_LINK_BASELINE_FILE).unlink()
    assert m.eth_link_drop(LINKS - 1) == ""
    assert st["events"] == ["eth_link_baseline_seeded"]


# --- through the broker: the post-reset check, the ladder, the power cycle -------------------------


@pytest.fixture
def nodesc_mesh(monkeypatch, tmp_path, mesh):  # noqa: F811
    """The dispatch-failure mesh fixture on a descriptor-less host whose reset leaves a link down
    (``reset_heals`` False) or brings it back. The recovery verify runs the monitor's own eth read
    and fabric pass; read-only, the mesh reads healthy, as a lost link does."""
    m = _wire(monkeypatch, tmp_path, mesh)
    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    mesh["fabric"] = []

    async def verify(expected, log, run_fabric=True, **kw):
        mesh["verifies"].append(run_fabric)
        snapshot = {"ok": True, "detail": f"all {expected} chips"}
        if not run_fabric:
            return True, {"snapshot": snapshot}
        eok, edetail = await m.verify_eth_heartbeat()
        ok, detail = await m._verify_fabric_health_body(60) if eok is not False else (None, "")
        mesh["fabric"].append((ok, detail))
        return eok is not False and ok is not False, {"snapshot": snapshot, "fabric": {"ok": ok, "detail": detail}}

    for r in (srv.galaxy_recovery, srv.per_target_recovery):
        monkeypatch.setattr(r, "_verify_device", verify)
    dfr.fsm_dirty(srv, "gate/post-job: eth links down")
    monkeypatch.setattr(srv, "device_hold_episode_since", datetime.now().isoformat())
    return mesh


async def _gate_then_relift():
    await dfr._gate()
    assert srv.fsm.state is ServerState.RECOVERING
    await srv._attempt_idle_relift()


@pytest.mark.asyncio
async def test_a_post_reset_link_drop_with_no_descriptor_fails_and_never_releases(nodesc_mesh):
    """Goal of the full check after every reset: the reset runs, its check runs the fabric pass, the
    pass misses the down link, and the count fails the check. The device stays held."""
    nodesc_mesh["reset_heals"] = False
    await _gate_then_relift()
    assert nodesc_mesh["resets"], "the hold takes the reset rung"
    assert nodesc_mesh["verifies"][-1] is True, "the post-reset check runs the fabric pass"
    ok, detail = nodesc_mesh["fabric"][-1]
    assert ok is False and FABRIC_LINKS_UNSEEN in detail
    assert srv.fsm.state is not ServerState.HEALTHY, "a failed post-reset check never releases"
    assert srv.device_hold_episode_since, "the episode stays open for the bounded recovery"


@pytest.mark.asyncio
async def test_a_post_reset_check_that_finds_every_link_releases(nodesc_mesh):
    """No false hold: the reset brought the link back, the count is full, the pass releases."""
    await _gate_then_relift()
    assert nodesc_mesh["resets"]
    assert nodesc_mesh["fabric"][-1][0] is True
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize("reboot, rung", [("0", "power-cycle"), ("1", "reboot")])
async def test_the_held_drop_runs_the_ladder_at_the_10_min_mark_then_the_host_rung(
    monkeypatch, nodesc_mesh, reboot, rung
):
    """At the 10-min hold ceiling the watchdog runs the present-mesh reset; its full post-reset check
    still fails on the count, and the same window requests the host rung (a power cycle where the
    reboot rung is off). Never a release without a passing full check."""
    from tt_device_mcp.health.recovery import galaxy

    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_REBOOT", reboot)
    monkeypatch.setenv("TT_DEVICE_MCP_AUTO_POWER_CYCLE", "1")
    cycles = []

    async def power_cycle(log, reason):
        cycles.append(reason)

    monkeypatch.setattr(srv, "_auto_power_cycle_host", power_cycle)
    nodesc_mesh["reset_heals"] = False
    since = datetime.now() - timedelta(seconds=galaxy._stuck_hold_ceiling_sec() + 1)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    await srv._force_escalate_stuck_hold()
    assert nodesc_mesh["resets"] and all(r == ["tt-smi", "-glx_reset"] for r in nodesc_mesh["resets"])
    ok, detail = nodesc_mesh["fabric"][-1]
    assert ok is False and FABRIC_LINKS_UNSEEN in detail, "the full post-reset check fails on the count"
    fired = "reboot" if nodesc_mesh["reboots"] else "power-cycle" if cycles else None
    assert fired == rung, f"the window requests the {rung} rung once the ladder's check fails"
    assert srv.fsm.state is not ServerState.HEALTHY
