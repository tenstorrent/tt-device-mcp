# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I21: the apply rate limit.

Every apply restarts the broker, and every start runs the full startup check, fabric traffic pass
included. With TTDEV_MIN_APPLY_INTERVAL_SEC set, a burst of pushes becomes one restart onto its
newest sha. The laps below run the script's own lines, cut from it, under a fake clock and a stub
`systemctl restart` that records each restart.
"""

import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"


def _cut(text, start, end):
    i = text.index(start)
    return text[i : text.index(end, i)]


def _lap(tmp_path, *, now, sha, interval=None):
    """One lap at fake time `now` that has just resolved `sha`: the already-current check and the
    rate limit, then (as if every gate passed) the tail that marks the sha current and restarts."""
    text = _SCRIPT.read_text()
    head = _cut(text, 'installed_sha="$(cat "$STATE"', "INHIBIT=/run/")
    tail = text[text.index('echo "$remote_sha" > "$STATE"') :]
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    restarts = tmp_path / "restarts"
    prelude = f"""set -u
ROOT={root}; STATE={root}/installed.sha; VENV={tmp_path}/venv; BR=main; src_desc=test
remote_sha={sha}
date() {{ echo {now}; }}
chmod() {{ :; }}
systemctl() {{ case "$*" in restart*) echo "{now} $(cat "$STATE")" >> {restarts} ;; esac; }}
"""
    env = {"PATH": "/usr/bin:/bin"}
    if interval is not None:
        env["TTDEV_MIN_APPLY_INTERVAL_SEC"] = str(interval)
    done = subprocess.run(["bash", "-c", prelude + head + tail], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    lines = restarts.read_text().splitlines() if restarts.exists() else []
    return done, lines, root


def test_the_rate_limit_is_off_by_default(tmp_path):
    assert 'MIN_APPLY="${TTDEV_MIN_APPLY_INTERVAL_SEC:-0}"' in _SCRIPT.read_text()
    _lap(tmp_path, now=1000, sha="a1")
    _, restarts, _ = _lap(tmp_path, now=1001, sha="b2")
    assert restarts == ["1000 a1", "1001 b2"]


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
