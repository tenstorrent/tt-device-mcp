# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I21: the apply rate limit.

Every apply restarts the broker, and every start runs the full startup check, fabric traffic pass
included. The limit is on by default (TTDEV_MIN_APPLY_INTERVAL_SEC, 1800 s; 0 turns it off), so a
burst of pushes becomes one restart onto its newest sha. The laps below run the script's own lines, cut from it, under a fake clock and a stub
`systemctl restart` that records each restart. The limit is for a healthy broker only: one that is
down, restarting itself, holding the device or failing its health check applies a new sha at once.
A broker in its startup check is booting, not broken, and stays limited for a bounded time.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from tt_device_mcp import server as srv
from tt_device_mcp.constants import FABRIC_CHECK_TIMEOUT_SEC
from tt_device_mcp.fsm import ServerFsm

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"


def _cut(text, start, end):
    i = text.index(start)
    return text[i : text.index(end, i)]


_HEALTHY = '{"status":"ok","held":false,"fsm_state":"healthy","fsm_why":null}'


def _lap(tmp_path, *, now, sha, interval=None, quiet=0, active="active", nrestarts="0", health=_HEALTHY):
    """One lap at fake time `now` that has just resolved `sha`: the already-current check, burst
    coalescing and the rate limit, then (as if every gate passed) the tail that marks the sha
    current and restarts. `active`, `nrestarts` and `health` are what systemd and the broker's
    /health report. `quiet` is TTDEV_APPLY_QUIET_SEC: 0 (coalescing off) for the rate-limit tests
    here, None for the script's default (tests/test_deploy_autoupdate_coalesce.py)."""
    quoted_health = health.replace("'", "'\\''")  # a /health body may carry single quotes
    text = _SCRIPT.read_text()
    head = _cut(text, 'installed_sha="$(cat "$STATE"', "INHIBIT=/run/")
    tail = text[text.index('echo "$remote_sha" > "$STATE"') :]
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    restarts = tmp_path / "restarts"
    prelude = f"""set -u
ROOT={root}; STATE={root}/installed.sha; VENV={tmp_path}/venv; BR=main; src_desc=test
idle=true; drained_s=0; TT_DEVICE_MCP_HEALTH_DIR={root}; RECOVERY_PENDING={root}/recovery.defer
remote_sha={sha}
date() {{ echo {now}; }}
chmod() {{ :; }}
systemctl() {{
    case "$*" in
        restart*) echo "{now} $(cat "$STATE")" >> {restarts} ;;
        is-active*) echo '{active}'; [ '{active}' = active ] ;;
        show*NRestarts*) echo '{nrestarts}' ;;
    esac
}}
curl() {{ printf '%s' '{quoted_health}'; }}
logger() {{ :; }}
"""
    env = {"PATH": "/usr/bin:/bin", "TTDEV_LOG_DIR": str(tmp_path / "logs")}
    if interval is not None:
        env["TTDEV_MIN_APPLY_INTERVAL_SEC"] = str(interval)
    if quiet is not None:
        env["TTDEV_APPLY_QUIET_SEC"] = str(quiet)
    done = subprocess.run(["bash", "-c", prelude + head + tail], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    lines = restarts.read_text().splitlines() if restarts.exists() else []
    return done, lines, root


def test_the_rate_limit_is_on_by_default_at_1800s(tmp_path):
    assert 'MIN_APPLY="${TTDEV_MIN_APPLY_INTERVAL_SEC:-1800}"' in _SCRIPT.read_text()
    _lap(tmp_path, now=1000, sha="a1")
    done, restarts, _ = _lap(tmp_path, now=1060, sha="b2")
    assert restarts == ["1000 a1"], done.stderr
    assert "(min 1800s); b2 applies in 1740s" in done.stderr, done.stderr
    _, restarts, _ = _lap(tmp_path, now=2800, sha="b2")
    assert restarts == ["1000 a1", "2800 b2"]


def test_an_interval_of_0_turns_the_rate_limit_off(tmp_path):
    _lap(tmp_path, now=1000, sha="a1", interval=0)
    _, restarts, _ = _lap(tmp_path, now=1001, sha="b2", interval=0)
    assert restarts == ["1000 a1", "1001 b2"]


def test_a_push_burst_under_the_default_is_paced_to_one_restart_per_interval(tmp_path):
    # Nine pushes over about two hours, a lap a minute, no interval configured: one restart per
    # 1800 s at most, each onto the newest sha, and the last push still applies.
    t0 = 1_700_000_000
    pushes = [t0 + m * 60 for m in (0, 8, 15, 31, 47, 60, 79, 95, 128)]
    sha = None
    for now in range(t0, pushes[-1] + 1800 + 120, 60):
        sha = f"s{sum(p <= now for p in pushes)}"
        _, restarts, _ = _lap(tmp_path, now=now, sha=sha)
    assert restarts[-1].endswith(f" {sha}"), restarts
    # The first push applies at once; the rest wait out the interval: 6 restarts, not 9.
    assert len(restarts) <= 1 + -(-(pushes[-1] - t0) // 1800) == 6, restarts
    times = [int(r.split()[0]) for r in restarts]
    assert all(b - a >= 1800 for a, b in zip(times, times[1:])), restarts


def test_a_burst_of_pushes_becomes_one_restart_onto_the_newest_sha(tmp_path):
    t0 = 1_700_000_000
    _lap(tmp_path, now=t0, sha="a1", interval=3600)
    # Five pushes over the next hour, a lap a minute: none of them restarts the broker.
    for i, sha in enumerate(["b2", "c3", "d4", "e5", "f6"]):
        done, restarts, _ = _lap(tmp_path, now=t0 + 60 + i * 600, sha=sha, interval=3600)
        assert "rate-limited" in done.stderr, done.stderr
        assert restarts == [f"{t0} a1"], restarts
    # Once the interval has passed, the newest sha applies, once.
    done, restarts, _ = _lap(tmp_path, now=t0 + 3600, sha="f6", interval=3600)
    assert restarts == [f"{t0} a1", f"{t0 + 3600} f6"], done.stderr
    _, restarts, _ = _lap(tmp_path, now=t0 + 3660, sha="f6", interval=3600)
    assert len(restarts) == 2, "an already-current sha restarted again"


def test_the_wait_names_the_time_left(tmp_path):
    _lap(tmp_path, now=5000, sha="a1", interval=3600)
    done, _, _ = _lap(tmp_path, now=5600, sha="b2", interval=3600)
    assert "last apply 600s ago (min 3600s); b2 applies in 3000s" in done.stderr, done.stderr


def test_a_rate_limited_lap_logs_its_deferral(tmp_path):
    """Spec 08 I22: the rate limit's deferral is a decision like any other, logged with its gate and
    its deadline; the apply that set the stamp is logged as applied."""
    _lap(tmp_path, now=5000, sha="a1", interval=3600)
    _lap(tmp_path, now=5600, sha="b2", interval=3600)
    log = (tmp_path / "logs" / "autoupdate.log").read_text().splitlines()
    assert len(log) == 2 and "decision=applied" in log[0] and "new=a1" in log[0], log
    assert "decision=deferred" in log[1] and "new=b2" in log[1], log
    assert "reason=apply rate limit: last apply 600s ago (min 3600s); apply_by=" in log[1], log


def test_no_stamp_a_bad_stamp_or_a_future_stamp_bars_nothing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for n, stamp in enumerate([None, "garbage", "99999999999"]):
        if stamp is None:
            (root / "last-apply").unlink(missing_ok=True)
        else:
            (root / "last-apply").write_text(stamp + "\n")
        _, restarts, _ = _lap(tmp_path, now=2000 + n, sha=f"s{n}", interval=3600)
        assert restarts[-1] == f"{2000 + n} s{n}", (stamp, restarts)
        (root / "installed.sha").unlink()


def test_a_rate_limited_lap_holds_nothing_and_skips_no_check():
    # Before the device-op bar and the dispatch hold: a lap that will not apply takes no hold.
    text = _SCRIPT.read_text()
    limit = text.index("# Apply rate limit (spec 08 I21)")
    assert limit < text.index("INHIBIT=/run/") < text.index("HOLD=/run/")
    # The apply is still a full broker restart, so the startup check (fabric pass included) runs.
    assert "systemctl restart tt-device-broker" in text[limit:]


def test_the_stamp_is_written_only_by_an_apply_that_restarts():
    # Every failed step exits before the stamp, so a failed apply is retried on the next lap
    # instead of waiting out the interval.
    text = _SCRIPT.read_text()
    stamp = text.index('date +%s > "$LAST_APPLY"')
    assert text.rindex("exit 1", 0, len(text)) < stamp
    assert text.index("systemctl restart tt-device-broker", stamp) - stamp < 120


# The throttle must never delay a fix to a broken broker. Each lap below comes
# 60 s after an apply, well inside the interval.
_BROKEN = {
    "failed": dict(active="failed"),
    "activating": dict(active="activating"),
    "inactive": dict(active="inactive"),
    "crash-looping": dict(nrestarts="3"),
    "silent": dict(health=""),
    "held": dict(health='{"status":"degraded","held":true,"held_reason":"fabric","fsm_state":"down"}'),
    "recovering": dict(health='{"status":"ok","held":false,"fsm_state":"recovering","fsm_why":"heartbeat"}'),
    "down": dict(health='{"status":"ok","held": false,"fsm_state": "down"}'),
}


@pytest.mark.parametrize("state", sorted(_BROKEN))
def test_a_broken_broker_applies_a_new_sha_inside_the_interval(tmp_path, state):
    _lap(tmp_path, now=10_000, sha="a1", interval=3600)
    done, restarts, _ = _lap(tmp_path, now=10_060, sha="b2", interval=3600, **_BROKEN[state])
    assert restarts == ["10000 a1", "10060 b2"], done.stderr
    assert "rate limit skipped: the broker" in done.stderr and "b2 applies now" in done.stderr, done.stderr


@pytest.mark.parametrize("state", sorted(_BROKEN))
def test_a_broken_broker_applies_at_once_under_the_default_interval(tmp_path, state):
    # The default is on, so a fix for a broken broker must not wait for it either.
    _lap(tmp_path, now=10_000, sha="a1")
    done, restarts, _ = _lap(tmp_path, now=10_060, sha="b2", **_BROKEN[state])
    assert restarts == ["10000 a1", "10060 b2"], done.stderr
    assert "rate limit skipped: the broker" in done.stderr and "b2 applies now" in done.stderr, done.stderr


@pytest.mark.parametrize(
    "broker",
    [
        dict(),
        dict(health='{"status": "ok", "held": false, "fsm_state": "healthy"}'),
    ],
    ids=["healthy", "spaced-json"],
)
def test_a_healthy_broker_stays_rate_limited(tmp_path, broker):
    _lap(tmp_path, now=10_000, sha="a1", interval=3600)
    done, restarts, _ = _lap(tmp_path, now=10_060, sha="b2", interval=3600, **broker)
    assert restarts == ["10000 a1"], done.stderr
    assert "rate-limited" in done.stderr, done.stderr


def _startup_health(tmp_path, monkeypatch, *, op=False, fault=None, dirty=False):
    """The /health body the broker itself serves right after a start: the real _health_payload()
    once fsm.boot_merge() has opened RECOVERING(startup_unverified). `op` is the startup gate
    running (a broker device op); `fault` is a failed startup check holding for its fault; `dirty` is a
    fault found during the startup check (a dead chip, say), which keeps the why and marks the episode
    dirty: held for that fault, not for the check."""
    f = ServerFsm(tmp_path / "fsm.json")
    monkeypatch.setattr(srv, "fsm", f)
    f.boot_merge(open_episode=None)
    if op:
        monkeypatch.setattr(srv, "device_op_active", "health-gate")
    if fault:
        f.on_fault(fault, detail="startup gate failed", dirty=False)
    if dirty:
        f.on_fault("heartbeat", detail="chip 3 heartbeat frozen")
    return json.dumps(srv._health_payload())


def _startup_bound():
    return int(re.search(r"^STARTUP_BOUND=(\d+)$", _SCRIPT.read_text(), re.M).group(1))


def test_the_startup_bound_covers_the_hugepage_wait_and_the_fabric_check():
    assert _startup_bound() >= srv.STARTUP_HUGEPAGES_WAIT_SEC + FABRIC_CHECK_TIMEOUT_SEC


@pytest.mark.parametrize("op", [False, True], ids=["held-for-the-startup-check", "startup-gate-running"])
def test_a_booting_broker_stays_rate_limited(tmp_path, monkeypatch, op):
    # Every start is RECOVERING(startup_unverified) until its startup check passes; within the
    # bound that is a broker booting, and a push then waits out the interval.
    health = _startup_health(tmp_path, monkeypatch, op=op)
    assert '"fsm_state": "recovering"' in health and '"fsm_why": "startup_unverified"' in health
    _lap(tmp_path, now=10_000, sha="a1", interval=3600)
    for t in (10_060, 10_000 + _startup_bound()):
        done, restarts, _ = _lap(tmp_path, now=t, sha="b2", interval=3600, health=health)
        assert restarts == ["10000 a1"], done.stderr
        assert "rate-limited" in done.stderr, done.stderr


@pytest.mark.parametrize(
    "case",
    ["past-the-bound", "startup-check-failed", "startup-episode-dirty"],
)
def test_a_startup_check_past_its_bound_or_held_applies_at_once(tmp_path, monkeypatch, case):
    health = _startup_health(
        tmp_path,
        monkeypatch,
        fault="fabric_unverified" if case == "startup-check-failed" else None,
        dirty=case == "startup-episode-dirty",
    )
    if case == "startup-episode-dirty":
        assert '"fsm_why": "startup_unverified"' in health and '"held": true' in health
    _lap(tmp_path, now=10_000, sha="a1", interval=3600)
    t = 10_001 + _startup_bound() if case == "past-the-bound" else 10_060
    done, restarts, _ = _lap(tmp_path, now=t, sha="b2", interval=3600, health=health)
    assert restarts == ["10000 a1", f"{t} b2"], done.stderr
    assert "rate limit skipped: the broker" in done.stderr and "b2 applies now" in done.stderr, done.stderr
    # The same under the default interval, which is longer than the bound.
    assert _startup_bound() < 1800
    (tmp_path / "default").mkdir()
    _lap(tmp_path / "default", now=10_000, sha="a1")
    done, restarts, _ = _lap(tmp_path / "default", now=t, sha="b2", health=health)
    assert restarts == ["10000 a1", f"{t} b2"], done.stderr


def test_the_crash_loop_baseline_is_the_count_after_the_last_apply(tmp_path):
    # systemd restarts counted before the last apply are not a crash loop of the applied sha.
    _lap(tmp_path, now=10_000, sha="a1", interval=3600, nrestarts="2")
    assert (tmp_path / "root" / "last-apply-nrestarts").read_text().strip() == "2"
    _, restarts, _ = _lap(tmp_path, now=10_060, sha="b2", interval=3600, nrestarts="2")
    assert restarts == ["10000 a1"]
    _, restarts, _ = _lap(tmp_path, now=10_120, sha="b2", interval=3600, nrestarts="3")
    assert restarts == ["10000 a1", "10120 b2"]


def test_a_missing_stamp_is_silent_and_a_leading_zero_stamp_is_decimal(tmp_path):
    # No "No such file" noise before the first stamp, and "0900" is 900, not octal.
    done, _, root = _lap(tmp_path, now=1000, sha="a1", interval=3600)
    assert "No such file" not in done.stderr, done.stderr
    (root / "last-apply").write_text("0900\n")
    done, restarts, _ = _lap(tmp_path, now=1000, sha="b2", interval=3600)
    assert "last apply 100s ago" in done.stderr, done.stderr
    assert restarts == ["1000 a1"]
