# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I31, per-host apply stagger: each host adds a fixed delay, a hash of its hostname into
TTDEV_APPLY_STAGGER_SEC (default 2700 s), to the burst-coalescing deadlines, so boxes that follow
one branch do not all restart in the same minute. A window of 0 turns it off. A broken broker is
never staggered."""

import subprocess

import pytest

from tests.test_deploy_autoupdate_rate_limit import _SCRIPT, _cut, _lap

T0 = 1_700_000_000
WINDOW = 2700


def _fnv1a(name):
    h = 2166136261
    for b in name.encode():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _stagger(names, window=WINDOW):
    """au_stagger from the script itself, once per name, in one bash."""
    fn = _cut(_SCRIPT.read_text(), "au_stagger() {", "stagger=0\n")
    body = fn + "".join(f"au_stagger s '{n}' {window}; echo \"$s\"\n" for n in names)
    done = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=30, check=True)
    return [int(x) for x in done.stdout.split()]


_HOSTS = ["box-a", "box-b", "galaxy-01", "galaxy-02", "n300-dev", "a.example.com", "x", ""]


def test_the_default_window_is_2700s():
    assert 'APPLY_STAGGER="${TTDEV_APPLY_STAGGER_SEC:-2700}"' in _SCRIPT.read_text()


def test_the_stagger_is_deterministic_and_inside_the_window():
    first, again = _stagger(_HOSTS), _stagger(_HOSTS)
    assert first == again
    assert first == [_fnv1a(n) % WINDOW for n in _HOSTS], "FNV-1a of the hostname, mod the window"
    assert all(0 <= s < WINDOW for s in first), first
    assert len(set(first[:-1])) == len(_HOSTS) - 1, "distinct hosts spread across the window"
    assert _stagger(["box-a"], window=1) == [0]


def _box(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "installed.sha").write_text("a1\n")


def test_a_host_applies_exactly_its_stagger_after_the_branch_settles(tmp_path):
    host = "galaxy-01"
    (d,) = _stagger([host])
    assert d > 60, d
    _box(tmp_path)
    done, restarts, _ = _lap(tmp_path, now=T0, sha="b2", stagger=WINDOW, host=host)
    assert restarts == [], done.stderr
    assert f"host stagger {d}s; applies in {d}s" in done.stderr, done.stderr
    _, restarts, _ = _lap(tmp_path, now=T0 + d - 1, sha="b2", stagger=WINDOW, host=host)
    assert restarts == []
    _, restarts, _ = _lap(tmp_path, now=T0 + d, sha="b2", stagger=WINDOW, host=host)
    assert restarts == [f"{T0 + d} b2"]


def test_the_stagger_adds_to_the_quiet_period(tmp_path):
    host = "box-b"
    (d,) = _stagger([host])
    _box(tmp_path)
    _lap(tmp_path, now=T0, sha="b2", quiet=600, stagger=WINDOW, host=host)
    _, restarts, _ = _lap(tmp_path, now=T0 + 600 + d - 1, sha="b2", quiet=600, stagger=WINDOW, host=host)
    assert restarts == []
    _, restarts, _ = _lap(tmp_path, now=T0 + 600 + d, sha="b2", quiet=600, stagger=WINDOW, host=host)
    assert restarts == [f"{T0 + 600 + d} b2"]


def test_a_window_of_0_turns_the_stagger_off(tmp_path):
    _box(tmp_path)
    done, restarts, root = _lap(tmp_path, now=T0, sha="b2", quiet=0, stagger=0, host="galaxy-01")
    assert restarts == [f"{T0} b2"], done.stderr
    assert not (root / "pending-head").exists()


@pytest.mark.parametrize(
    "broken",
    [dict(active="failed"), dict(health='{"held":true,"fsm_state":"down"}')],
    ids=["failed", "held"],
)
def test_a_broken_broker_is_not_staggered(tmp_path, broken):
    _box(tmp_path)
    done, restarts, _ = _lap(tmp_path, now=T0, sha="b2", quiet=0, stagger=WINDOW, host="galaxy-01", **broken)
    assert restarts == [f"{T0} b2"], done.stderr
