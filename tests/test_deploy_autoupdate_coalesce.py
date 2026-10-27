# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I21, burst coalescing: a new sha applies once its branch has been quiet for APPLY_QUIET
(600 s), or once the oldest sha not yet applied has waited APPLY_BOUND (3600 s), whichever comes
first. The 1800 s interval, the idle gate, the drain and the restart-needed check stay as they were.

Every restart reruns the full startup check, fabric pass included. A branch that moves every few
minutes for hours used to restart every box once per interval for the whole burst; with coalescing
it is one restart per bound at most, plus one onto the last sha once the burst ends. The laps run
the script's own lines under a fake clock, one lap a minute, like the reconcile timer.
"""

import re

import pytest

import tt_device_mcp.server as srv
from tests.test_deploy_autoupdate_no_restart import _installed, _push, _restarts, box  # noqa: F401 - fixture
from tests.test_deploy_autoupdate_no_restart import _lap as _full_lap
from tests.test_deploy_autoupdate_rate_limit import _SCRIPT, _lap
from tests.test_restart_drain import _restart_with_a_held_job
from tests.test_startup_fabric_hugepages import _shortfalls, _startup, _systemctl  # noqa: F401 - fixtures
from tt_device_mcp.fsm import ServerState

# Fork-heavy deploy-script laps: one xdist worker runs them all (pyproject: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

QUIET, BOUND, INTERVAL, LAP = 600, 3600, 1800, 60
T0 = 1_700_000_000


def _simulate(tmp_path, moves, *, until, quiet=None, start=T0, skip=False, step=0):
    """Laps every 60 s from `start` to `until` while the branch head moves at the times in `moves`
    (sha `s<n>` after the n-th move). The box applied `s0` long before. Returns the restarts as
    (time, sha) and, for every sha, when it was first seen. Once the last sha is installed and one
    more lap found it current, it stops: with the head at rest every later lap is that same exit.
    `skip`: after a lap that defers and says when it applies, the next lap run is the first one at
    or past that time or the next move, whichever comes first; the laps between are the same deferral.
    A lap reached at the time a deferral named, with no move since, must not defer for that reason
    again. The lone-push test runs every lap, and so checks that a deferral applies when it says.
    `step` (with `skip`): a deferring lap jumps straight to the last move lap within `step` seconds
    and before the time it named. The laps it passes would each see a newer head and defer again:
    the record keeps its first-seen time, and the last move sets the quiet clock as it would have.
    With coalescing on, `step` stays at or under the quiet, so no quiet ends between the moves it
    passes; every passed sha is counted seen at its own move lap, as a lap run there would have."""
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    (root / "installed.sha").write_text("s0\n")
    (root / "last-apply").write_text(f"{start - 4 * 3600}\n")
    restarts, seen, settled, owed = [], {}, False, None
    now = start
    while now <= until:
        sha = f"s{sum(m <= now for m in moves)}"
        seen.setdefault(sha, now)
        done, lines, _ = _lap(tmp_path, now=now, sha=sha, quiet=quiet)
        if owed and owed[0] == now:
            assert owed[1] not in done.stderr, f"{now}: deferred past the time it named: {done.stderr}"
        restarts = [(int(t), s) for t, s in (x.split() for x in lines)]
        if settled:
            assert not done.stderr and not (root / "pending-head").exists(), done.stderr
            break
        settled = now >= max(moves) and (root / "installed.sha").read_text().split() == [sha]
        then = now + LAP
        said = re.search(r"applies in (\d+)s", done.stderr) if skip else None
        owed = None
        if said:
            due = _on_lap(now + int(said.group(1)), start)
            then = max(then, min([due] + [_on_lap(m, start) for m in moves if m > now]))
            jump = [m for m in moves if now < _on_lap(m, start) <= min(now + step, due)]
            if jump:
                then = max(then, _on_lap(jump[-1], start))
                for k, m in enumerate(moves[:-1], 1):  # each sha a lap run at its move lap saw
                    if now < _on_lap(m, start) < then and _on_lap(m, start) < _on_lap(moves[k], start):
                        seen.setdefault(f"s{k}", _on_lap(m, start))
            if then == due and not any(now < m <= due for m in moves):
                owed = (due, "coalescing: head" if "coalescing: head" in done.stderr else "rate-limited:")
        now = then
    return restarts, seen


def _on_lap(t, start):
    """The first lap at or past `t`."""
    return start + -(-(t - start) // LAP) * LAP


def test_the_defaults_are_code_defaults():
    text = _SCRIPT.read_text()
    assert 'APPLY_QUIET="${TTDEV_APPLY_QUIET_SEC:-600}"' in text
    assert 'APPLY_BOUND="${TTDEV_APPLY_BOUND_SEC:-3600}"' in text
    assert QUIET < INTERVAL < BOUND, "a burst must coalesce past the interval, and a lone push wait less"


def test_a_push_every_5_min_for_150_min_is_at_most_3_restarts(tmp_path):
    moves = [T0 + m * 60 for m in range(0, 151, 5)]
    end = moves[-1] + QUIET + BOUND
    restarts, _ = _simulate(tmp_path, moves, until=end, skip=True)
    assert len(restarts) <= 3, restarts
    assert restarts[-1][1] == f"s{len(moves)}", "the last push of the burst applies"
    assert restarts[-1][0] <= moves[-1] + QUIET + LAP, "once the burst ends, its last sha waits only the quiet"
    times = [t for t, _ in restarts]
    assert all(b - a >= INTERVAL for a, b in zip(times, times[1:])), restarts
    # The same burst with coalescing off: one restart per interval, which is what it replaces.
    (tmp_path / "off").mkdir()
    # Coalescing off, a move between the laps changes nothing: each one defers until the interval ends.
    before, _ = _simulate(tmp_path / "off", moves, until=end, quiet=0, skip=True, step=INTERVAL)
    assert len(before) >= 5 > len(restarts), (before, restarts)


def test_a_lone_push_after_an_hour_of_quiet_waits_only_the_quiet_period(tmp_path):
    moves = [T0 + 3600]
    restarts, _ = _simulate(tmp_path, moves, until=T0 + 3600 + 2 * QUIET)
    (tmp_path / "off").mkdir()
    before, _ = _simulate(tmp_path / "off", moves, until=T0 + 3600 + 2 * QUIET, quiet=0, skip=True)
    assert before == [(T0 + 3600, "s1")], before
    assert restarts == [(T0 + 3600 + QUIET, "s1")], restarts


@pytest.mark.parametrize(
    "gaps_min",
    [[1] * 150, [5] * 40, [9] * 25, [11] * 20, [2, 9, 3, 9, 1, 12, 9, 9, 4, 30, 9, 9, 9, 9, 9, 9, 9, 2]],
    ids=["every-1-min", "every-5-min", "every-9-min", "every-11-min", "irregular"],
)
def test_no_sha_waits_longer_than_the_bound_plus_a_lap(tmp_path, gaps_min):
    moves, t = [], T0
    for g in gaps_min:
        moves.append(t)
        t += g * 60
    end = moves[-1] + QUIET + BOUND
    # A push a minute: a deferring lap passes up to 9 moves at once (see _simulate's `step`).
    step = QUIET - LAP if max(gaps_min) == 1 else 0
    restarts, seen = _simulate(tmp_path, moves, until=end, skip=True, step=step)
    applied_at = {}
    for t_apply, sha in restarts:  # a sha is served by the first restart onto it or a newer one
        n = int(sha[1:])
        for k in range(1, n + 1):
            applied_at.setdefault(f"s{k}", t_apply)
    for sha, first in seen.items():
        if sha == "s0":
            continue
        assert sha in applied_at, (sha, restarts)
        # The spec bound is bound + interval + the idle wait; here every box is idle, and the
        # interval ends before the bound (it counts from the apply before the sha was seen).
        assert applied_at[sha] - first <= BOUND + LAP <= BOUND + INTERVAL, (sha, first, restarts)


def test_a_coalescing_lap_says_when_it_applies_and_logs_its_deferral(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "installed.sha").write_text("a1\n")
    done, restarts, root = _lap(tmp_path, now=T0, sha="b2", quiet=None)
    assert restarts == [], done.stderr
    assert "coalescing: head b2 seen 0s ago (quiet 600s)" in done.stderr, done.stderr
    assert "applies in 600s if the branch stays put" in done.stderr, done.stderr
    (logged,) = (tmp_path / "logs" / "autoupdate.log").read_text().splitlines()
    assert "decision=deferred" in logged and "new=b2" in logged and "reason=burst coalescing:" in logged, logged
    assert "apply_by=" in logged, logged
    assert (root / "pending-head").read_text().split() == [str(T0), "b2", str(T0)]
    # A newer head restarts the quiet but keeps the oldest sha's clock.
    _lap(tmp_path, now=T0 + 300, sha="c3", quiet=None)
    assert (root / "pending-head").read_text().split() == [str(T0), "c3", str(T0 + 300)]


def test_an_apply_or_a_branch_back_at_the_installed_sha_clears_the_record(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "installed.sha").write_text("a1\n")
    _, _, root = _lap(tmp_path, now=T0, sha="b2", quiet=None)
    assert (root / "pending-head").exists()
    _lap(tmp_path, now=T0 + 60, sha="a1", quiet=None)  # the push was reverted by a force push
    assert not (root / "pending-head").exists()
    _lap(tmp_path, now=T0 + 120, sha="b2", quiet=None)
    _, restarts, _ = _lap(tmp_path, now=T0 + 120 + QUIET, sha="b2", quiet=None)
    assert restarts == [f"{T0 + 120 + QUIET} b2"], "the bound started again with the new sha"
    assert not (root / "pending-head").exists(), "an apply ends the record"


@pytest.mark.parametrize("record", ["garbage", "x b2 y", f"{T0 + 9999} b2 {T0 + 9999}"])
def test_a_bad_or_future_record_counts_from_now(tmp_path, record):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "installed.sha").write_text("a1\n")
    (tmp_path / "root" / "pending-head").write_text(record + "\n")
    done, restarts, root = _lap(tmp_path, now=T0, sha="b2", quiet=None)
    assert restarts == [] and "applies in 600s" in done.stderr, done.stderr
    assert (root / "pending-head").read_text().split() == [str(T0), "b2", str(T0)]


@pytest.mark.parametrize(
    "broken",
    [dict(active="failed"), dict(health=""), dict(health='{"held":true,"fsm_state":"down"}')],
    ids=["failed", "silent", "held"],
)
def test_a_broken_broker_is_not_coalesced(tmp_path, broken):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "installed.sha").write_text("a1\n")
    done, restarts, _ = _lap(tmp_path, now=T0, sha="b2", quiet=None, **broken)
    assert restarts == [f"{T0} b2"], done.stderr
    assert "coalescing skipped: the broker" in done.stderr, done.stderr


def test_quiet_0_turns_coalescing_off(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "installed.sha").write_text("a1\n")
    _, restarts, root = _lap(tmp_path, now=T0, sha="b2", quiet=0)
    assert restarts == [f"{T0} b2"]
    assert not (root / "pending-head").exists()


# --- the gates after it are unchanged: a coalesced lap holds nothing, then the lap runs as before ---


def test_coalescing_comes_before_the_rate_limit_and_every_gate_that_holds():
    text = _SCRIPT.read_text()
    gate = text.index("# Burst coalescing (spec 08 I21)")
    assert text.index("broker_broken() {") < gate < text.index('if [ "$MIN_APPLY" -gt 0 ]')
    assert gate < text.index("INHIBIT=/run/") < text.index("# Dispatch hold (spec 08 I18")
    # It only defers: no rung, reset or check is named in it, and the apply is still a full restart.
    block = text[gate : text.index('if [ "$MIN_APPLY" -gt 0 ]')]
    for word in ("reset", "fabric_check", "STARTUP_", "health-gate", "tt-smi", "systemctl restart"):
        assert word not in block, word
    assert "systemctl restart tt-device-broker" in text[text.index("restart_needed() {") :]


_DEFAULTS = "unset TTDEV_APPLY_QUIET_SEC TTDEV_APPLY_BOUND_SEC\n"


def test_a_docs_only_push_coalesces_then_applies_with_no_restart(box):  # noqa: F811
    new = _push(box, {"README.md": "readme 2\n"})
    done, lines, events = _full_lap(box, new, now=T0, stand_ins=_DEFAULTS)
    assert done.returncode == 0 and "coalescing" in done.stderr, done.stderr
    assert lines == [] and events == [], "a coalesced lap holds, drains and installs nothing"
    assert _installed(box) == box["installed"]
    done, lines, events = _full_lap(box, new, now=T0 + QUIET, stand_ins=_DEFAULTS)
    assert done.returncode == 0, done.stderr
    assert _restarts(lines) == [] and "broker not restarted" in done.stderr, done.stderr
    assert [e["kind"] for e in events] == ["autoupdate_applied_no_restart"]
    assert _installed(box) == new


def test_a_code_push_coalesces_then_drains_a_running_job_and_restarts(box):  # noqa: F811
    new = _push(box, {"src/tt_device_mcp/server.py": "x = 2\n"})
    done, lines, _ = _full_lap(box, new, now=T0, job_polls=5, stand_ins=_DEFAULTS)
    assert done.returncode == 0 and "coalescing" in done.stderr and lines == [], (done.stderr, lines)
    done, lines, events = _full_lap(box, new, now=T0 + QUIET, job_polls=2, stand_ins=_DEFAULTS)
    assert done.returncode == 0, done.stderr
    assert "restart needed: changes src/tt_device_mcp/server.py" in done.stderr, done.stderr
    assert lines.count(f"sleep {box['installed']}") >= 2, "the drain waited for the running job"
    assert _restarts(lines) == [f"restart {new}"], lines
    assert lines.index(f"sleep {box['installed']}") < lines.index(f"restart {new}")
    assert [e["kind"] for e in events] == ["autoupdate_apply"]


# --- the restart a coalesced apply ends in is an ordinary start: the full check, held until it passes ---


@pytest.mark.asyncio
@pytest.mark.usefixtures("_startup")
async def test_the_start_after_a_coalesced_apply_runs_the_forced_fabric_pass_before_the_queue_opens(
    monkeypatch, tmp_path
):
    job, calls = await _restart_with_a_held_job(
        monkeypatch, tmp_path, lambda: srv.fsm.on_readings(srv._healthy_reading())
    )
    assert calls == [("startup", True, True, None)], "one full gate pass, fabric forced, after the hugepages"
    assert srv.fsm.state is ServerState.HEALTHY and not srv._device_unavailable_for_tenant()
    assert job.status is srv.JobStatus.QUEUED


@pytest.mark.asyncio
@pytest.mark.usefixtures("_startup")
async def test_the_start_after_a_coalesced_apply_stays_held_on_fewer_links_than_the_cabling(monkeypatch, tmp_path):
    # The fabric check fails when it finds fewer links than the cabling descriptor expects
    # (tests/test_below_floor_offbus_ladder.py); here the startup pass reports that failure.
    job, calls = await _restart_with_a_held_job(
        monkeypatch,
        tmp_path,
        lambda: srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="fabric check: 76 of 77 cabled links up", dirty=True),
    )
    assert calls == [("startup", True, True, None)]
    assert srv.fsm.state is ServerState.RECOVERING, "a failed startup check never releases the device"
    assert srv._device_unavailable_for_tenant()
    assert job.status is srv.JobStatus.QUEUED
