# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 03 I81: a missing fabric validator is its own no-verdict class.

A fresh install whose broker started before the pinned validator was built held its mesh
fabric-unverified (a 77), and the hold was read as a generic present-mesh hold: the idle relift
escalated it to a glx_reset, whose post-reset check then waited 60 s for eth training that could not
help. A validator that is not on disk measures nothing, so it owes no reset, no link fault and no
eth-training wait. It is shown on /health, the broker asks for the build, and only a full check that
passes once the build lands lifts the hold. The 10-min bound (spec 04 I76) still applies.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import patch_health_event, patch_recovery
from tests.test_post_reset_gate import FOK, NO_TRAINED_LINK_77, _replay_post_reset_verify, mesh  # noqa: F401 (fixture)
from tests.test_startup_fabric_hugepages import _deadline, _hold_77, _sleeps
from tt_device_mcp import server as srv
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health.monitors import fabric

_NOT_BUILT = "pinned validator not installed yet ('/opt/x/run_cluster_validation')"
_WRAPPER = "/opt/tt-device-broker/deploy/tt-device-fabric-check.sh"


def _not_built(monkeypatch, missing=_NOT_BUILT):
    monkeypatch.setattr(fabric, "validator_missing", lambda: missing)


# --- which fabric checks the stat read speaks for -----------------------------------------------


@pytest.mark.parametrize(
    "override,speaks",
    [(None, True), (_WRAPPER, True), ("/usr/local/bin/my-own-fabric-check", False)],
)
def test_the_not_built_read_speaks_only_for_the_pinned_validator(monkeypatch, override, speaks):
    _not_built(monkeypatch)
    if override is None:
        monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", override)
    assert fabric.validator_not_built() == (_NOT_BUILT if speaks else None)


def test_a_built_validator_reads_built(monkeypatch):
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
    monkeypatch.setattr(fabric, "validator_missing", lambda: None)
    assert fabric.validator_not_built() is None


# --- post-reset: no eth-training wait, never a pass, never a failed reset -----------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("mesh")
async def test_a_post_reset_77_with_the_validator_not_built_takes_no_eth_training_wait(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", _WRAPPER)
    _not_built(monkeypatch)
    healthy, spent, sleeps, fabric_asked = await _replay_post_reset_verify(monkeypatch, [(5.0, NO_TRAINED_LINK_77)])
    assert healthy is None, "UNKNOWN: not a recovery (goal 11) and not a failed reset (no host rung)"
    assert sleeps == [], "no 60 s eth-training wait for a validator that is not on disk"
    assert fabric_asked == [True], "the one full check ran, fabric included (goal 8); no retry"
    assert spent == 5.0


@pytest.mark.asyncio
@pytest.mark.usefixtures("mesh")
async def test_the_post_reset_log_names_the_missing_validator_not_a_trained_link(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", _WRAPPER)
    _not_built(monkeypatch)

    async def verify(expected, log, run_fabric=True, **_):
        return True, dict(NO_TRAINED_LINK_77)

    patch_recovery(monkeypatch, "_verify_device", verify)
    lines = []
    await srv.galaxy_recovery._verify_device_after_reset(32, lines.append)
    assert any("fabric validator not built" in m for m in lines)
    assert not any("no trained link" in m for m in lines)


@pytest.mark.asyncio
@pytest.mark.usefixtures("mesh")
async def test_an_operators_own_fabric_check_keeps_the_eth_training_wait(monkeypatch):
    """The stat read says nothing about a command that does not run the pinned validator."""
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "/usr/local/bin/my-own-fabric-check")
    _not_built(monkeypatch)
    healthy, _, sleeps, _ = await _replay_post_reset_verify(monkeypatch, [(40.0, NO_TRAINED_LINK_77), (45.0, FOK)])
    assert healthy is True and sleeps == [60.0]


# --- /health shows it ----------------------------------------------------------------------------


@pytest.mark.parametrize("missing", [_NOT_BUILT, None])
def test_health_shows_whether_the_validator_is_built(monkeypatch, missing):
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "device_held_since", "")
    _not_built(monkeypatch, missing)
    assert srv._health_payload()["fabric_validator"] == {"built": missing is None, "missing": missing}


# --- the broker asks for the build, once per episode, bounded -----------------------------------


def _spawns(monkeypatch, *, root=True, rc=0):
    calls = []

    class _Proc:
        returncode = rc

        async def wait(self):
            return rc

        def kill(self):
            pass

    async def fake_exec(program, *args, **kwargs):
        calls.append((program, *args))
        return _Proc()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(srv.privileges, "has_systemd", lambda: root)
    monkeypatch.setattr(srv.privileges, "is_root", lambda: root)
    monkeypatch.setattr(srv, "_validator_build_requested_for", None)
    return calls


@pytest.mark.asyncio
async def test_the_build_is_requested_once_per_hold_episode_without_blocking(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    calls = _spawns(monkeypatch)
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))

    assert await srv._request_validator_build(_NOT_BUILT) is True
    assert await srv._request_validator_build(_NOT_BUILT) is False, "asked once per episode"
    assert calls == [("systemctl", "start", "--no-block", "tt-device-fabric-validator.service")]
    assert [k for k, _ in events] == ["fabric_validator_build_requested"]
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY, "the request lifts nothing"


@pytest.mark.asyncio
async def test_no_build_request_without_systemd_or_root(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    calls = _spawns(monkeypatch, root=False)
    assert await srv._request_validator_build(_NOT_BUILT) is False
    assert calls == []


@pytest.mark.asyncio
async def test_a_hung_build_request_is_bounded(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    killed = []

    class _Proc:
        async def wait(self):
            import asyncio

            await asyncio.sleep(3600)

        def kill(self):
            killed.append(True)

    async def fake_exec(program, *args, **kwargs):
        return _Proc()

    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(srv.privileges, "has_systemd", lambda: True)
    monkeypatch.setattr(srv.privileges, "is_root", lambda: True)
    monkeypatch.setattr(srv, "_validator_build_requested_for", None)
    monkeypatch.setattr(srv, "VALIDATOR_BUILD_REQUEST_TIMEOUT_SEC", 0.05)
    assert await srv._request_validator_build(_NOT_BUILT) is False
    assert killed == [True]


# --- the hold: no reset, no lift until a full check passes after the build lands ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize("passes", [True, False])
async def test_the_watch_requests_the_build_then_lifts_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, passes
):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", raising=False)
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", None)
    calls = _spawns(monkeypatch)
    landed = {"v": False}
    monkeypatch.setattr(fabric, "validator_missing", lambda: None if landed["v"] else _NOT_BUILT)
    slept, hooks = _sleeps(monkeypatch)
    hooks.append(lambda n: landed.update(v=n >= 2))

    await srv._await_startup_prerequisites(_NOT_BUILT)

    assert calls == [("systemctl", "start", "--no-block", "tt-device-fabric-validator.service")]
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY, "still held while the build runs"
    assert srv._startup_fabric_owed()

    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        f = {"ok": True, "detail": "fabric healthy"} if passes else {"ok": None, "detail": "could not run (77)"}
        return True, {"snapshot": {"ok": True}, "fabric": f}

    patch_recovery(monkeypatch, "_verify_device", verify)
    rungs = []

    async def escalate(rung, *a, **k):
        rungs.append(rung)

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    await srv._attempt_idle_relift()

    assert seen == [True], "the owed pass is the full check, fabric included"
    assert rungs == [], "never a reset"
    assert (srv.fsm.state is ServerState.HEALTHY) is passes, "only a passing check lifts (goal 11)"


@pytest.mark.asyncio
async def test_an_idle_relift_on_a_not_built_validator_never_resets_or_lifts(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    _not_built(monkeypatch)

    async def verify(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    rungs = []

    async def escalate(rung, *a, **k):
        rungs.append(rung)

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    await srv._attempt_idle_relift()

    assert rungs == []
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY and not srv.fsm.record.dirty
    assert srv._fabric_77_only_hold(), "still the 77-only class, never the generic present-mesh hold"
    assert not srv._idle_relift_armed()[2], "never the generic stuck-hold escalation"
    assert srv._fabric_77_no_reset_cause(), "no host rung for it either (spec 04 I29)"
    assert srv._fabric_77_validator_crash() == "", "not read as a validator crash (no reset)"


def test_the_10_min_bound_still_forces_a_not_built_hold(monkeypatch, tmp_path, clear_job_state):
    """Goal 10 is unchanged: a validator still missing at the 10-min mark gets the I76 ladder."""
    _hold_77(monkeypatch, tmp_path)
    _not_built(monkeypatch)
    fired, since = _deadline(monkeypatch)
    srv._check_hold_deadline(now=since + timedelta(seconds=srv._stuck_hold_ceiling_sec() - 1))
    assert fired == []
    srv._check_hold_deadline(now=since + timedelta(seconds=srv._stuck_hold_ceiling_sec() + 1))
    assert fired == [True]
