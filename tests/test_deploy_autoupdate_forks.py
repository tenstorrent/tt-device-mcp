# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""The autoupdate script's clock and its process count (spec 08 I18/I21/I24).

A drain polls every 5 s for up to the job cap, and a box runs a lap every minute: each process a
poll or a lap starts is paid on every one. The script reads its clock (au_now, au_time) and
matches the broker's /health answer in the shell, so a poll forks for what it asks systemd and the
kernel, not for `date`, `grep`, `wc` or `cat`. The other autoupdate tests replace that clock; the
tests here run it for real, and bound the forks of one drain poll and of one lap.
"""

import re
import shutil
import subprocess
import time

import pytest

from tests.test_deploy_autoupdate_idle_gate import _drain, _gate
from tests.test_deploy_autoupdate_rate_limit import _lap

# Fork-heavy deploy-script laps: one xdist worker runs them all (pyproject: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

# Forks the script itself starts, measured with stand-in commands (each a $(...) subshell; on a box
# an external command, one fork each): 5 a poll and 7 a lap, where they were 13 and 19 before the
# shell read the clock and matched /health itself. A lap that finds its sha already installed, the
# lap of nearly every minute, starts none.
_POLL_FORKS = 5
_LAP_FORKS = 7


def _strace_works():
    if shutil.which("strace") is None:
        return False
    probe = subprocess.run(["strace", "-f", "-qq", "-o", "/dev/null", "true"], capture_output=True)
    return probe.returncode == 0


needs_strace = pytest.mark.skipif(not _strace_works(), reason="needs a working strace (ptrace allowed)")


@pytest.fixture
def forks(monkeypatch, tmp_path):
    """Run every `bash` the helpers start under strace; forks() is the count of the last one."""
    log = tmp_path / "forks.strace"
    real = subprocess.run

    def run(argv, **kw):
        log.unlink(missing_ok=True)
        cmd = ["strace", "-f", "-qq", "-e", "trace=fork,vfork,clone,clone3", "-o", str(log), *argv]
        return real(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)

    def count():
        calls = [
            line
            for line in log.read_text().splitlines()
            if re.search(r"\b(v?fork|clone3?)\(", line) and "CLONE_THREAD" not in line
        ]
        return len(calls)

    return count


@needs_strace
def test_a_drain_poll_forks_a_bounded_number_of_processes(tmp_path, forks):
    """The marginal cost of a poll: two drains that differ only in how long the job runs."""
    counts, polled = {}, {}
    for ticks in (2, 12):
        sub = tmp_path / str(ticks)
        sub.mkdir()
        done, trace, _ = _drain(sub, running=ticks, queue=0, lap_stand_ins=False)
        assert done.returncode == 0, done.stderr
        polled[ticks] = sum(t.startswith("pgrep ") for t in trace)  # one count of the broker's children a poll
        counts[ticks] = forks()
    assert polled[12] - polled[2] == 10, polled
    per_poll = (counts[12] - counts[2]) / 10
    assert per_poll <= _POLL_FORKS, counts


@needs_strace
def test_a_lap_forks_a_bounded_number_of_processes(tmp_path, forks):
    """One lap that finds a new sha and defers it under the rate limit: the decision log, the
    broker check, burst coalescing and the rate limit."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "installed.sha").write_text("s0\n")
    (root / "last-apply").write_text("9990\n")
    done, restarts, _ = _lap(tmp_path, now=10_000, sha="s1", quiet=None, stand_ins=False)
    assert done.returncode == 0 and restarts == [], done.stderr
    assert forks() <= _LAP_FORKS


@needs_strace
def test_an_already_current_lap_starts_no_process(tmp_path, forks):
    """No coalescing record: the lap exits with no process. A record, or a dangling link left in its
    place, is still removed."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "installed.sha").write_text("s1\n")
    done, restarts, _ = _lap(tmp_path, now=10_000, sha="s1", quiet=None, stand_ins=False)
    assert done.returncode == 0 and restarts == [] and not done.stderr, done.stderr
    assert forks() == 0
    record = root / "pending-head"
    for make in (lambda: record.write_text("9000 s1 9000\n"), lambda: record.symlink_to(root / "gone")):
        make()
        done, restarts, _ = _lap(tmp_path, now=10_000, sha="s1", quiet=None, stand_ins=False)
        assert done.returncode == 0 and restarts == [], done.stderr
        assert not record.exists() and not record.is_symlink(), "the record of an applied sha outlived it"


def test_a_coalescing_record_is_rewritten_only_when_it_changes(tmp_path):
    """A lap that would write the record's exact bytes leaves the file as it is; a new head, or a
    record that is a link, is written whole as before."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "installed.sha").write_text("s0\n")
    (root / "last-apply").write_text("1000\n")
    record = root / "pending-head"
    _lap(tmp_path, now=10_000, sha="s1", quiet=None, stand_ins=False)
    assert record.read_text() == "10000 s1 10000\n"
    inode = record.stat().st_ino
    _lap(tmp_path, now=10_060, sha="s1", quiet=None, stand_ins=False)
    assert record.read_text() == "10000 s1 10000\n" and record.stat().st_ino == inode
    _lap(tmp_path, now=10_120, sha="s2", quiet=None, stand_ins=False)
    assert record.read_text() == "10000 s2 10120\n" and record.stat().st_ino != inode
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(record.read_text())
    record.unlink()
    record.symlink_to(elsewhere)
    _lap(tmp_path, now=10_180, sha="s2", quiet=None, stand_ins=False)
    assert not record.is_symlink() and record.read_text() == "10000 s2 10120\n"
    assert not list(root.glob("pending-head.*")), "a temp record was left behind"


def test_a_drain_on_the_real_clock(tmp_path):
    """No stand-in clock: the script's own au_now times the drain and stamps the dispatch hold."""
    once = tmp_path / "once"
    # One poll sees a broker child (a job), the next sees none.
    pgrep = f"\npgrep() {{ seen pgrep; [ -e {once} ] && return 0; : > {once}; echo 777; }}\n"
    t0 = int(time.time())
    done, trace, _ = _gate(tmp_path, stand_ins=pgrep, lap_stand_ins=False, env={"TTDEV_HOLD_SETTLE_SEC": "0"})
    t1 = int(time.time())
    assert done.returncode == 0, done.stderr
    m = re.search(r"drained after (\d+)s", done.stderr)
    assert m and int(m.group(1)) <= t1 - t0, done.stderr
    held = [t for t in trace if t.startswith("restart held ")]
    assert held, trace
    stamp = int(held[-1].split()[-1])
    assert t0 <= stamp <= t1, (t0, held[-1], t1)


def test_a_lap_on_the_real_clock(tmp_path):
    """No stand-in clock: a rate-limited lap reads the real time for its wait and its apply_by, and
    stamps its decision with the local time, as `date '+%Y-%m-%dT%H:%M:%S%z'` would."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "installed.sha").write_text("s0\n")
    last = int(time.time()) - 60
    (root / "last-apply").write_text(f"{last}\n")
    t0 = int(time.time())
    done, restarts, _ = _lap(tmp_path, now=None, sha="s1", stand_ins=False)
    t1 = int(time.time())
    assert restarts == [], done.stderr
    line = (tmp_path / "logs" / "autoupdate.log").read_text().splitlines()[-1]
    m = re.search(r"last apply (\d+)s ago \(min 1800s\); apply_by=(\S+)", line)
    assert m and t0 - last <= int(m.group(1)) <= t1 - last, line

    def epoch(stamp):
        out = subprocess.run(["date", "-d", stamp, "+%s"], capture_output=True, text=True, check=True)
        return int(out.stdout)

    assert epoch(m.group(2)) == last + 1800, line
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} decision=deferred ", line), line
    assert t0 <= epoch(line.split()[0]) <= t1, line
