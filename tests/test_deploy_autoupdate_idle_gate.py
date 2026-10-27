# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The auto-update's two busy gates (spec 08 I7, I20, I24).

A running JOB holds an update back (spec 08 I24): it is re-adopted across a restart, but the
restart still drops its client's socket (a client may kill the job for it, which owes a kill reset)
and holds the queue for the new broker's startup checks until it ends. So a busy lap drains at once:
it holds dispatch, the running job finishes, and the update applies at that job boundary, bounded
by the broker's hard job limit counted from the job's start. An in-flight DEVICE OP bars the update
outright: re-adoption covers a job, not a tt-smi that KillMode=control-group SIGTERMs mid-reset,
which is a half-reset mesh and a chassis power cycle.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.deploy_helpers import LAP_STAND_INS

# Fork-heavy deploy-script laps: one xdist worker runs them all (conftest: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"


def test_the_idle_gate_is_always_on_with_no_setting():
    # spec 08 I24: no setting turns the gate off or sets its bound; the old defer knobs are gone.
    text = _SCRIPT.read_text()
    assert 'if [ "$busy" -ne 0 ]; then' in text
    assert "\nJOB_CAP=1560\n" in text
    for knob in ("TTDEV_MAX_DEFER_SEC", "TTDEV_DRAIN_AFTER_SEC", "TTDEV_DRAIN_TIMEOUT_SEC", "TTDEV_DRAIN_LONG_SEC"):
        assert knob not in text, knob
    assert "applying anyway" not in text and "idle gate off" not in text


def test_the_job_cap_is_the_brokers_hard_limit_plus_its_kill_grace():
    from tt_device_mcp import constants, server

    assert server.MAX_TIMEOUT_SEC + constants.GRACEFUL_KILL_GRACE_SEC == 1560, "JOB_CAP in the script drifted"


def test_an_in_flight_device_op_still_bars_the_update():
    # Unconditional, and before the idle gate: no knob may turn this one off.
    text = _SCRIPT.read_text()
    op_bar = text.index("device op in flight")
    assert "exit 0" in text[op_bar : op_bar + 200]
    assert text.index("ttdev-reset-") < text.index("JOB_CAP=")


# --- spec 08 I18: the dispatch hold, and what counts as busy --------------------------------------
# The gate runs for real, cut from the script between the hold and the "applying" line, under
# stand-ins for systemd, pgrep, /proc/uptime and the /proc scan. A stub `systemctl restart` stands
# in for the restart that ends an applying lap and records whether the hold was still there.

_UPTIME = 100_000


def _clock(text):
    """The script's clock seam (au_now, au_time) as it defines them: the lap reads the real clock."""
    i = text.index("au_now() {")
    return text[i : text.index("\n", text.index("au_time() {", i)) + 1]


def _gate(
    tmp_path,
    *,
    scopes="",
    queued=0,
    stand_ins="",
    lap_stand_ins=True,
    env=None,
    hold_dir=True,
    main_pid=4242,
    install=None,
    job_age=None,
    broker_state="active",
):
    """`install`: also run the last look before the restart (spec 08 I24), with this shell text
    standing in for the install between the gate and it; the restart then follows the STATE write.
    `job_age`: seconds a running job scope has run, from systemd's start record (None: unreadable).
    `lap_stand_ins=False`: the real mv and rm, for the tests that count the script's own processes."""
    text = _SCRIPT.read_text()
    start = text.index("# Dispatch hold (spec 08 I18")
    end = text.index('echo "autoupdate: applying', start)
    gate = text[start:end]
    if install is not None:
        look = text.index("# Last look before the restart (spec 08 I24)")
        state_write = text.index('echo "$remote_sha" > "$STATE"', look)
        gate += 'echo "autoupdate: applying"\n' + install + "\n" + text[look:state_write]
    hold = tmp_path / "run" / "dispatch-hold"
    if hold_dir:
        hold.parent.mkdir()
    assert gate.count("HOLD=/run/tt-device-broker/dispatch-hold\n") == 1
    gate = gate.replace("HOLD=/run/tt-device-broker/dispatch-hold\n", f"HOLD={hold}\n")
    uptime = tmp_path / "uptime"
    uptime.write_text(f"{_UPTIME}.42 123.45\n")
    assert gate.count("UPTIME=/proc/uptime\n") == 1
    gate = gate.replace("UPTIME=/proc/uptime\n", f"UPTIME={uptime}\n")
    # No real /proc holders, and no scan of the host's processes on every poll of a long drain.
    # nullglob for this one expansion only: an empty stand-in /proc has nothing to scan, as an empty
    # /proc would (not its unmatched pattern). Every other glob in the cut runs as in production.
    assert gate.count("for p in /proc/[0-9]*; do") == 1
    gate = gate.replace(
        "for p in /proc/[0-9]*; do",
        f'shopt -s nullglob; procs=({tmp_path}/proc/[0-9]*); shopt -u nullglob; for p in "${{procs[@]}}"; do',
    )
    enter = "" if job_age is None else str((_UPTIME - job_age) * 1_000_000)
    trace = tmp_path / "trace"
    logs = tmp_path / "logs"
    (logs / "queued").mkdir(parents=True)
    for i in range(queued):
        (logs / "queued" / f"{i}.json").write_text("{}")
    root = tmp_path / "root"
    root.mkdir()
    # The stand-ins read their files with the read builtin, never $(cat ...): a lap polls every 5 s
    # for up to the job cap, and each fork on a poll is hundreds per test.
    prelude = f"""set -u
ROOT={root}; remote_sha=abc123; INHIBIT={tmp_path / "device-op.lock"}; STATE={root / "installed.sha"}
{_clock(text)}on_restart() {{ :; }}
seen() {{ local h; if [ -e "$HOLD" ]; then read -r h < "$HOLD"; echo "$1 held $h" >> {trace}; else echo "$1 free" >> {trace}; fi; }}
sleep() {{ seen "sleep $*"; }}
pgrep() {{ seen pgrep; }}
ls() {{ :; }}  # no real /proc holders
job_scopes() {{ [ -n "{scopes}" ] && echo "{scopes}"; }}
recovery_gate() {{ recovery_why=""; return 1; }}  # a healthy broker: spec 08 I23 has its own tests
broker_preempt() {{ :; }}  # no preemptable job runs (or no answer)
broker_names_preempt_job() {{ :; }}  # no answer
systemctl() {{
    case "$*" in
        *list-units*ttdev-job-*) seen scopes; job_scopes ;;
        *ActiveEnterTimestampMonotonic*) echo "{enter}" ;;
        is-active*) echo {broker_state} ;;
        show*) echo {main_pid} ;;
        restart*) on_restart; seen restart ;;
    esac
}}
{LAP_STAND_INS if lap_stand_ins else ""}{stand_ins}
"""
    tail = 'echo "autoupdate: applying"\n' if install is None else 'echo "$remote_sha" > "$STATE"\n'
    script = prelude + gate + tail + "systemctl restart tt-device-broker\n"
    env = {
        "PATH": "/usr/bin:/bin",
        "TTDEV_HOLD_SETTLE_SEC": "7",
        "TTDEV_LOG_DIR": str(logs),
        **(env or {}),
    }
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    lines = trace.read_text().splitlines() if trace.exists() else []
    return done, lines, hold


def test_the_hold_is_taken_before_anything_is_counted(tmp_path):
    done, trace, hold = _gate(tmp_path)
    assert done.returncode == 0, done.stderr
    assert trace[0].startswith("sleep 7 held "), f"the settle did not run under the hold: {trace}"
    pid, epoch = trace[0].split()[-2:]
    assert int(pid) > 0 and abs(int(epoch) - time.time()) < 60, trace[0]
    counted = [t for t in trace if t.split()[0] in ("pgrep", "scopes")]
    assert counted and all(" held " in t for t in counted), f"something was counted without the hold: {trace}"
    assert trace[-1].startswith("restart held "), trace


def test_the_hold_appears_whole(tmp_path):
    """The broker removes a hold it cannot parse, so the hold is never seen empty or half-written:
    it is written under another name and renamed into place."""
    done, trace, hold = _gate(tmp_path, stand_ins='mv() { seen "mv $*"; command mv "$@"; }')
    assert done.returncode == 0, done.stderr
    assert trace[0].startswith(f"mv -f {hold}.") and trace[0].endswith(f" {hold} free"), trace
    assert trace[1].startswith("sleep 7 held "), f"the rename did not put the hold in place: {trace}"
    assert not list(hold.parent.glob("dispatch-hold.*")), "the lap left a temporary hold file behind"


def test_a_queued_job_alone_applies_at_once_under_the_hold(tmp_path):
    """A queued spec is not running work: under the hold it cannot start, so this is a job
    boundary, and it waits, persisted, for the new broker.

    Nothing here sleeps, so the lap's clock stands still while each poll takes over a second of
    real time: a slow or loaded host must not read as a drain. (On the real clock, a count that
    crossed a second boundary under a loaded full-suite run printed "drained after 1s".)"""
    clock = '\nau_now() { printf -v "$1" %s 1700000000; }\npgrep() { seen pgrep; command sleep 1.05; }\n'
    done, trace, hold = _gate(tmp_path, queued=1, stand_ins=clock)
    assert done.returncode == 0, done.stderr
    assert "queued=1" in done.stderr and "drained after 0s" in done.stderr, done.stderr
    assert "applying" in done.stdout
    assert trace[-1].startswith("restart held "), trace
    assert (tmp_path / "logs" / "queued" / "0.json").exists()


def test_an_idle_box_applies_and_keeps_the_hold_until_the_restart(tmp_path):
    done, trace, hold = _gate(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "draining" not in done.stderr
    assert [t for t in trace if t.startswith("restart")] and trace[-1].startswith("restart held "), trace
    # Nothing between the gate and the restart lets go of it.
    text = _SCRIPT.read_text()
    apply = text[text.index('echo "autoupdate: applying') : text.index("systemctl restart tt-device-broker")]
    assert "$HOLD" not in apply


def test_the_cut_runs_with_the_production_glob_options(tmp_path):
    # A test-only shell option over the real cut hides regressions in its globs (an unguarded
    # queued/*.json loop counts its unmatched pattern as a job under bash's defaults, not under
    # nullglob), so the simulation leaves bash's glob options as production has them.
    # Read before the cut (the prelude's options) and after it (what it leaves behind).
    show = "shopt nullglob failglob dotglob extglob >&2"
    done, _, _ = _gate(tmp_path, stand_ins=show, install=show)
    assert done.returncode == 0, done.stderr
    for opt in ("nullglob", "failglob", "dotglob", "extglob"):
        assert len(re.findall(rf"^{opt}\s+off$", done.stderr, re.M)) == 2, done.stderr


# --- spec 08 I20/I24: a busy box drains at once, and applies at the job boundary -------------------
# A model broker stands behind pgrep: one job runs for `job_ticks` polls, and whenever no job runs
# and no hold stands it dispatches the next queued one at once, as a queue of back-to-back jobs does.
# With `scope`, the running job shows as a ttdev-job scope (privsep, or a re-adopted job) instead of
# a broker child. The restart stub asks the model once more first: a job dispatched between the
# last idle count and the restart, or one still running, is a job the restart cuts off. A fake clock
# moves only when the lap sleeps.

_T0 = 1_700_000_000


def _broker(tmp_path, *, running, queue, job_ticks=3, on_sleep="", scope=False, jump=None):
    """`jump=(a, b)`: a sleep that ends past a s and short of b s ends at b s instead. A long bound
    then costs a dozen polls, not hundreds, and its exact time in the log still shows any early
    let-go."""
    m = tmp_path / "model"
    m.mkdir()
    (m / "running").write_text(str(running))
    (m / "queue").write_text(str(queue))
    (m / "t").write_text("0")
    trace = tmp_path / "trace"
    leap = f"if [ $v -gt {jump[0]} ] && [ $v -lt {jump[1]} ]; then v={jump[1]}; fi; " if jump else ""
    child = "" if scope else '[ "$r" -gt 0 ] && echo 777'
    scopes = (
        '\njob_scopes() { local r; read -r r < $M/running; [ "$r" -gt 0 ] && echo "ttdev-job-428.scope loaded active running"; }'
        if scope
        else ""
    )
    return f"""
M={m}
# One poll: the job advances, the count reads the broker, and only then does the broker dispatch
# again, as it does a second after a job ends.
pgrep() {{
    seen pgrep
    local r q; read -r r < $M/running; read -r q < $M/queue
    if [ "$r" -gt 0 ]; then r=$((r-1)); fi
    {child}
    if [ "$r" -eq 0 ] && [ "$q" -gt 0 ] && [ ! -e "$HOLD" ]; then
        q=$((q-1)); r={job_ticks}; echo dispatch >> {trace}
    fi
    echo $r > $M/running; echo $q > $M/queue
    return 0
}}{scopes}
on_restart() {{
    local r q; read -r r < $M/running; read -r q < $M/queue
    if [ "$r" -eq 0 ] && [ "$q" -gt 0 ] && [ ! -e "$HOLD" ]; then echo dispatch >> {trace}; r=1; fi
    if [ "$r" -gt 0 ]; then echo "job cut by restart" >> {trace}; fi
}}
au_now() {{ local v; read -r v < $M/t; printf -v "$1" %s $(( {_T0} + v )); }}
sleep() {{ local v; seen "sleep $*"; read -r v < $M/t; v=$(( v + ${{1%.*}} )); {leap}echo $v > $M/t; {on_sleep} }}
"""


def _drain(tmp_path, *, running, queue, job_ticks=3, on_sleep="", scope=False, env=None, jump=None, **kw):
    """The gate against the model broker; its queued jobs are persisted specs too."""
    stand_ins = _broker(
        tmp_path, running=running, queue=queue, job_ticks=job_ticks, on_sleep=on_sleep, scope=scope, jump=jump
    )
    env = {"TTDEV_HOLD_SETTLE_SEC": "0", **(env or {})}
    return _gate(tmp_path, stand_ins=stand_ins + kw.pop("stand_ins", ""), queued=queue, env=env, **kw)


def _polls_held(trace):
    polls = [t for t in trace if t.startswith("pgrep")]
    return polls and all(" held " in t for t in polls)


def test_a_running_job_scope_holds_dispatch_and_the_update_applies_when_it_ends(tmp_path):
    """A ttdev-job scope runs (a long serving job) when an update is pending. No restart
    until the scope ends, no new job starts meanwhile, and the restart follows the job's end in the
    same lap, one 5 s poll later at most."""
    done, trace, hold = _drain(tmp_path, running=4, queue=2, scope=True, job_age=300)
    assert done.returncode == 0, done.stderr
    assert "scopes=1" in done.stderr and "draining" in done.stderr, done.stderr
    assert "(bound 1260s: job cap 1560s, youngest job 300s in)" in done.stderr, done.stderr
    assert "drained after 10s" in done.stderr, done.stderr
    assert "dispatch" not in trace, f"a queued job started under the drain: {trace}"
    assert "job cut by restart" not in trace, trace
    restarts = [i for i, t in enumerate(trace) if t.startswith("restart")]
    assert restarts == [len(trace) - 1] and trace[-1].startswith("restart held "), trace
    assert _polls_held(trace), f"the drain let go of dispatch: {trace}"
    # The poll that saw the scope gone is the last thing before the restart: no sleep in between.
    assert not trace[-2].startswith("sleep"), trace
    assert (tmp_path / "model" / "queue").read_text().strip() == "2", "the queued jobs wait for the new broker"

    # Without the drain, the same lap with no hold to take is the old race, and the model catches it.
    control = tmp_path / "control"
    control.mkdir()
    done, trace, _ = _drain(control, running=4, queue=2, scope=True, hold_dir=False)
    assert "dispatch" in trace, f"the model no longer shows the back-to-back dispatch it guards: {trace}"


def test_a_busy_box_drains_at_once_and_applies_at_the_job_boundary(tmp_path):
    done, trace, hold = _drain(tmp_path, running=4, queue=2)
    assert done.returncode == 0, done.stderr
    assert "deferring" not in done.stderr and "drained after 10s" in done.stderr, done.stderr
    assert "dispatch" not in trace and "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace
    assert len([t for t in trace if t.startswith("pgrep")]) == 4 and _polls_held(trace), trace
    assert not hold.exists(), "the hold outlived the restart"


def test_old_defer_settings_do_not_turn_the_drain_off(tmp_path):
    """Boxes may still carry long TTDEV_MAX_DEFER_SEC (or the old drain knobs) in their defaults:
    they are read by nothing, and the lap still drains at once and applies at the job boundary."""
    old = {
        "TTDEV_MAX_DEFER_SEC": "86400",
        "TTDEV_DRAIN_AFTER_SEC": "99999",
        "TTDEV_DRAIN_TIMEOUT_SEC": "1",
        "TTDEV_DRAIN_LONG_SEC": "1",
    }
    for name, max_defer in (("unset", None), ("zero", "0"), ("long", None)):
        case = tmp_path / name
        case.mkdir()
        env = old if name == "long" else ({} if max_defer is None else {"TTDEV_MAX_DEFER_SEC": max_defer})
        done, trace, _ = _drain(case, running=10, queue=1, scope=True, env=env)
        assert done.returncode == 0, done.stderr
        assert "drained after 40s" in done.stderr, f"{name}: {done.stderr}"
        assert "job cut by restart" not in trace and "dispatch" not in trace, f"{name}: {trace}"
        assert trace[-1].startswith("restart held "), f"{name}: {trace}"


def test_a_back_to_back_queue_cannot_slip_a_job_into_the_restart(tmp_path):
    """The idle check used to race dispatch: a job dispatched a second after the count read
    children=0 was cut off by the restart (rc 120). Under the drain's hold it never starts."""
    done, trace, hold = _drain(tmp_path, running=2, queue=5, job_ticks=1)
    assert done.returncode == 0, done.stderr
    assert "draining" in done.stderr, done.stderr
    assert "dispatch" not in trace and "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace

    # The same lap with no hold to take is the old race, and the model catches it.
    control = tmp_path / "control"
    control.mkdir()
    done, trace, _ = _drain(control, running=2, queue=5, job_ticks=1, hold_dir=False)
    assert "job cut by restart" in trace, f"the model no longer shows the race it guards: {trace}"


def _no_restart(done, trace, hold):
    """The lap restarted nothing, released dispatch and left the update for the next lap."""
    assert done.returncode == 0, done.stderr
    assert "a running broker job always holds the update" in done.stderr, done.stderr
    assert "dispatch released, the next lap retries" in done.stderr
    assert "job-limit kill" not in done.stderr, "the log claims a kill bound a no-timeout job lacks"
    assert "applying" not in done.stdout and "no longer holds the update" not in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), f"a running job got a restart: {trace}"
    assert "job cut by restart" not in trace, trace
    assert not hold.exists(), "a lap that restarts nothing kept dispatch held"


def test_a_job_past_its_cap_never_gets_a_restart(tmp_path):
    """A running ttdev-job scope holds the update at any age. The wait under the hold is bounded
    by the broker's hard job limit (1500 s + 60 s kill grace) from the scope's start; past it the
    lap releases dispatch and retries on the next lap until the job ends, never a restart (a
    capped job by the broker's limit kill; a no-timeout-except-on-contention job only by itself
    or on contention)."""
    # Already past the cap: no wait under the hold, and no restart.
    past = tmp_path / "past"
    past.mkdir()
    done, trace, hold = _drain(past, running=10_000, queue=1, scope=True, job_age=1600)
    _no_restart(done, trace, hold)
    assert "(bound 0s:" in done.stderr and "past the job cap (1560s) after 0s" in done.stderr, done.stderr
    assert not [t for t in trace if t.startswith("sleep 5")], f"a lap past the cap held dispatch: {trace}"
    assert "dispatch" not in trace, trace

    # Far past the cap, many laps in a row: none restarts.
    for lap in range(3):
        again = tmp_path / f"again{lap}"
        again.mkdir()
        _no_restart(*_drain(again, running=10_000, queue=1, scope=True, job_age=7200 + 60 * lap))

    # 60 s short of the cap: the lap holds dispatch those 60 s, then lets go, still no restart.
    near = tmp_path / "near"
    near.mkdir()
    done, trace, hold = _drain(near, running=10_000, queue=1, scope=True, job_age=1500)
    _no_restart(done, trace, hold)
    assert "(bound 60s:" in done.stderr and "past the job cap (1560s) after 60s" in done.stderr, done.stderr
    assert int((near / "model" / "t").read_text()) == 60
    assert _polls_held(trace), "the drain let go of dispatch before the cap"

    # No readable start counts as just started: the whole cap, from the drain's start.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    done, trace, hold = _drain(fresh, running=10_000, queue=1, scope=True, jump=(0, 1500))
    _no_restart(done, trace, hold)
    assert "past the job cap (1560s) after 1560s" in done.stderr, done.stderr
    assert _polls_held(trace), "the drain let go of dispatch before the cap"

    # A broker child past the cap (no scope: a per-user broker's job) holds it the same way.
    child = tmp_path / "child"
    child.mkdir()
    done, trace, hold = _drain(child, running=10_000, queue=1, jump=(0, 1500))
    _no_restart(done, trace, hold)
    assert "past the job cap (1560s) after 1560s" in done.stderr, done.stderr

    # Once the job has ended, the next lap finds the box idle and applies.
    after = tmp_path / "after"
    after.mkdir()
    done, trace, _ = _drain(after, running=0, queue=1, scope=True)
    assert done.returncode == 0 and "applying" in done.stdout, done.stderr
    assert trace[-1].startswith("restart held ") and "dispatch" not in trace, trace


def _holder(tmp_path, cgroup, jump=(0, 1500)):
    """One device holder (uid 1000) in `cgroup`, and no broker child or listed job scope."""
    proc = tmp_path / "proc" / "4321"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text(f"0::{cgroup}\n")
    stand_ins = """
ls() { [ "$2" = "%s/fd" ] && echo "lrwx------ 1 u u 64 Oct 21 13:00 3 -> /dev/tenstorrent/0"; return 0; }
stat() { echo 1000; }
""" % proc
    return _drain(tmp_path, running=0, queue=1, stand_ins=stand_ins, jump=jump)


def test_a_re_adopted_job_never_gets_a_restart(tmp_path):
    """A re-adopted job is no child of the broker: its ttdev-job scope counts as busy, and so do its
    device holders, read from their cgroup, even when the scope list cannot be read."""
    # The scope as systemd lists it, with no broker child: what a restart re-adopts.
    listed = tmp_path / "listed"
    listed.mkdir()
    done, trace, hold = _drain(listed, running=10_000, queue=1, scope=True, job_age=2400)
    assert "children=0" in done.stderr and "scopes=1" in done.stderr, done.stderr
    _no_restart(done, trace, hold)

    # Only its holder is seen, inside the job's scope cgroup: still a broker job.
    unlisted = tmp_path / "unlisted"
    unlisted.mkdir()
    done, trace, hold = _holder(unlisted, "/system.slice/ttdev-job-029.scope")
    assert "holders=1 job_holders=1 scopes=0" in done.stderr, done.stderr
    _no_restart(done, trace, hold)


def test_a_device_holder_outside_any_broker_job_stops_holding_the_update_at_the_cap(tmp_path):
    """A tenant that opened the device on its own is no broker job: no broker kill ends it, so it
    holds the update for the bound only, and the restart does not touch it."""
    done, trace, _ = _holder(tmp_path, "/user.slice/user-1000.slice/session-3.scope")
    assert done.returncode == 0, done.stderr
    assert "a device holder outside any broker job is past the job cap (1560s) after 1560s" in done.stderr
    assert "applying" in done.stdout and trace[-1].startswith("restart held "), trace


def test_a_device_op_in_flight_holds_a_drained_apply(tmp_path):
    """The broker may start a device op while dispatch is held; the apply waits it out."""
    inhibit = tmp_path / "device-op.lock"
    inhibit.write_text(f"{os.getpid()} health_gate\n")
    count = tmp_path / "sleeps"
    on_sleep = f'echo x >> {count}; [ "$(wc -l < {count})" -ge 3 ] && rm -f {inhibit};'
    done, trace, hold = _drain(tmp_path, running=0, queue=1, on_sleep=on_sleep)
    assert done.returncode == 0, done.stderr
    assert "draining" in done.stderr, done.stderr
    assert trace[-1].startswith("restart held "), trace
    # The settle's sleep, then drain polls until the op ended on the third sleep.
    assert len(count.read_text().splitlines()) == 3, "the apply did not wait for the device op"
    assert "dispatch" not in trace


def test_a_device_op_in_flight_still_bars_the_restart_past_the_cap(tmp_path):
    """The cap ends the wait for a job, never for a device op: a reset in flight at the cap
    restarts nothing, drops the hold, and leaves the update for the next lap."""
    inhibit = tmp_path / "device-op.lock"
    inhibit.write_text(f"{os.getpid()} tt_device_reset\n")
    done, trace, hold = _drain(tmp_path, running=10_000, queue=1, scope=True, job_age=1600)
    assert done.returncode == 0, done.stderr
    assert "device op is still in flight at the job cap" in done.stderr, done.stderr
    assert "applying" not in done.stdout + done.stderr
    assert not any(t.startswith("restart") for t in trace), trace
    assert not hold.exists(), "a lap that restarts nothing kept dispatch held"

    # A reset scope counts the same while the broker runs.
    scope = tmp_path / "scope"
    scope.mkdir()
    done, trace, hold = _drain(scope, running=10_000, queue=1, scope=True, job_age=1600, stand_ins=_RESET_SCOPE)
    assert "device op is still in flight at the job cap" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace

    # The broker's own op bars it with the broker reading down too: a live pid is a live op.
    down = tmp_path / "down"
    down.mkdir()
    (down / "device-op.lock").write_text(f"{os.getpid()} tt_device_reset\n")
    done, trace, hold = _drain(down, running=10_000, queue=1, scope=True, job_age=1600, broker_state="failed")
    assert "device op is still in flight at the job cap" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace


# A ttdev-reset scope systemd lists on every look: one the broker timed out and left running.
_RESET_SCOPE = """
_sc=$(declare -f systemctl | sed '1d')
eval "systemctl_job() $_sc"
systemctl() { case "$*" in *list-units*ttdev-reset-*) echo "ttdev-reset-7.scope loaded active running" ;; *) systemctl_job "$@" ;; esac; }
"""


def test_a_down_broker_with_only_a_reset_scope_left_is_restarted(tmp_path):
    """A down broker runs no op of its own, and a reset scope is its own unit, which the restart does
    not stop: the new broker's startup waits it out before its queue opens. So the lap applies, as
    the reconcile it would otherwise block would start the broker over the same scope."""
    done, trace, hold = _drain(
        tmp_path, running=10_000, queue=1, scope=True, job_age=1600, broker_state="failed", stand_ins=_RESET_SCOPE
    )
    assert done.returncode == 0, done.stderr
    assert "the broker is down after 0s; applying" in done.stderr, done.stderr
    assert trace[-1].startswith("restart held "), trace


def test_a_down_broker_ends_the_drain(tmp_path):
    """A stopped or failed broker protects no job by waiting, and the drain blocks the reconcile
    that would start it: the lap applies, and its restart starts the broker (the scope is
    re-adopted, as it would be by the reconcile's own start)."""
    done, trace, hold = _drain(tmp_path, running=10_000, queue=1, scope=True, broker_state="failed")
    assert done.returncode == 0, done.stderr
    assert "the broker is down after 0s; applying" in done.stderr, done.stderr
    assert trace[-1].startswith("restart held "), trace


def test_the_drain_refreshes_the_hold(tmp_path):
    """The broker drops a hold past DISPATCH_HOLD_MAX_SEC; a drain that is still polling must not
    lose it, so every poll rewrites it, and the apply starts on a fresh one."""
    done, trace, hold = _drain(tmp_path, running=6, queue=1)
    assert done.returncode == 0, done.stderr
    epochs = [int(t.split()[-1]) for t in trace if t.startswith("pgrep held ")]
    assert epochs[0] == _T0 and epochs[-1] > epochs[0], trace
    assert epochs == sorted(epochs)
    assert int(trace[-1].split()[-1]) == epochs[-1], "the apply did not start on a fresh hold"


def test_the_apply_ends_in_a_full_broker_restart():
    """The update's restart is an ordinary stop and start, so the new broker runs its whole startup
    check (spec 03 I4) before its queue opens: no reload, no kill, no knob that skips it. That the
    check runs, fabric included, after the hugepage pool fills, and passes before the queue opens:
    tests/test_restart_drain.py::test_a_job_held_across_an_update_waits_for_the_startup_fabric_pass
    and ::test_a_failed_startup_pass_after_an_update_keeps_the_queue_held."""
    text = _SCRIPT.read_text()
    tail = text[text.index('echo "autoupdate: applying') :]
    assert tail.count("systemctl restart tt-device-broker") == 1
    for shortcut in ("reload", "kill -HUP", "SKIP", "--no-block"):
        assert shortcut not in tail, shortcut


# --- spec 08 I24: the last look before the restart -------------------------------------------------


def test_a_job_running_at_the_restart_point_is_waited_out_to_its_cap(tmp_path):
    """The gate saw an idle box, but a job is running by the time the install is done: the lap
    waits for it under the hold, as a drain does, and applies at its end."""
    done, trace, hold = _drain(tmp_path, running=0, queue=0, install="echo 30 > $M/running")
    assert done.returncode == 0, done.stderr
    assert "restart point: busy" in done.stderr and "restart point: drained after 140s" in done.stderr
    assert "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace
    assert (tmp_path / "root" / "installed.sha").read_text().strip() == "abc123"
    looks = [t for t in trace if t.startswith("pgrep")][1:]
    assert looks and all(" held " in t for t in looks), f"the last look let go of dispatch: {trace}"


def _op_at_the_restart_point(tmp_path, *, op_polls):
    """The install starts a device op with no job running; the op ends after `op_polls` polls of
    the last look, and the trace records the moment it does."""
    inhibit = tmp_path / "device-op.lock"
    start_op = f"echo {os.getpid()} tt_device_reset > {inhibit}"
    count = tmp_path / "op-polls"
    trace = tmp_path / "trace"
    on_sleep = (
        f"[ -e {inhibit} ] && echo x >> {count}; "
        f'[ -e {inhibit} ] && [ "$(wc -l < {count})" -ge {op_polls} ] && rm -f {inhibit} && echo "op ended" >> {trace};'
    )
    return _drain(tmp_path, running=0, queue=0, scope=True, install=start_op, on_sleep=on_sleep)


def test_a_device_op_at_the_restart_point_blocks_the_restart_until_it_ends(tmp_path):
    """A device op the last look finds with no job running is waited out under the hold (never
    restarted over, I7); once it ends the broker is idle and the update applies."""
    done, trace, hold = _op_at_the_restart_point(tmp_path, op_polls=4)
    assert done.returncode == 0, done.stderr
    assert "waited out under the hold for at most 660s" in done.stderr, done.stderr
    assert "not restarting" not in done.stderr, done.stderr
    assert "op ended" in trace and trace[-1].startswith("restart held "), trace
    assert trace.index("op ended") < len(trace) - 1, "the broker restarted over a device op"
    assert (tmp_path / "root" / "installed.sha").read_text().strip() == "abc123"
    looks = [t for t in trace if t.startswith("pgrep")]
    assert looks and all(" held " in t for t in looks), f"the op wait let go of dispatch: {trace}"


def test_a_device_op_at_the_restart_point_blocks_the_restart(tmp_path):
    """A job past its cap and a device op at the last look: no restart, installed.sha stays old."""
    inhibit = tmp_path / "device-op.lock"
    start_op = f"echo 10000 > $M/running; echo {os.getpid()} tt_device_reset > {inhibit}"
    done, trace, hold = _drain(tmp_path, running=0, queue=0, scope=True, job_age=1600, install=start_op)
    assert done.returncode == 0, done.stderr
    assert "applying" in done.stdout and "not restarting, not marking current" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace
    assert not (tmp_path / "root" / "installed.sha").exists(), "a restart that did not run was marked current"


def _never_ending_op(tmp_path, *, lock, install=True, running=0, job_age=None, jump=None):
    """A device op that never ends: the broker's own (a live-pid device-op lock) when `lock`, else
    only a ttdev-reset scope systemd keeps listing. `install`: it shows at the restart point."""
    inhibit = tmp_path / "device-op.lock"
    start = f"echo {os.getpid()} tt_device_reset > {inhibit}" if lock else ":"
    stand_ins = "" if lock else _RESET_SCOPE
    if running:
        start += "; echo %d > $M/running" % running
    if not install:
        if lock:
            inhibit.write_text(f"{os.getpid()} tt_device_reset\n")
        return _drain(tmp_path, running=running, queue=1, stand_ins=stand_ins, scope=True, job_age=job_age, jump=jump)
    return _drain(
        tmp_path, running=0, queue=0, stand_ins=stand_ins, scope=True, job_age=job_age, install=start, jump=jump
    )


def _lap_seconds(tmp_path):
    return int((tmp_path / "model" / "t").read_text())


def _last_hold_epoch(trace):
    return max(int(t.split()[-1]) for t in trace if " held " in t)


def test_an_op_that_never_ends_at_the_restart_point_is_never_restarted_over(tmp_path):
    """The broker's own op (live-pid lock, in its cgroup: a restart SIGTERMs it) or a reset scope
    the broker timed out and left running, never ending at the restart point. The wait ends at
    OP_CAP, the lap restarts nothing and leaves installed.sha on the old sha, so the next lap
    redoes the install and restarts once the broker is idle (spec 08 I7, I24). The hold is never
    rewritten past the bound."""
    for name, lock in (("lock", True), ("scope", False)):
        d = tmp_path / name
        d.mkdir()
        done, trace, hold = _never_ending_op(d, lock=lock, jump=(0, 600))
        assert done.returncode == 0, (name, done.stderr)
        assert "never restarted over (I7)" in done.stderr, (name, done.stderr)
        assert "not restarting, not marking current" in done.stderr, (name, done.stderr)
        assert not any(t.startswith("restart") for t in trace), (name, trace)
        assert not (d / "root" / "installed.sha").exists(), (name, "a restart that did not run was marked current")
        assert _lap_seconds(d) == 660, (name, "the op wait is not bounded by OP_CAP")
        assert _last_hold_epoch(trace) - _T0 <= 660, (name, "the hold was rewritten past the bound")


def test_an_op_with_no_job_holds_dispatch_for_op_cap_not_the_job_cap(tmp_path):
    """With no job running, a never-ending op keeps the hold for OP_CAP (under the broker's
    DISPATCH_HOLD_MAX_SEC of 900 s), never for the job cap of 1560 s, before the install and after;
    either way nothing restarts over it."""
    for name, kw in (
        ("drain-lock", {"lock": True, "install": False}),
        ("drain-scope", {"lock": False, "install": False}),
        ("restart-point-lock", {"lock": True}),
        ("restart-point-scope", {"lock": False}),
    ):
        d = tmp_path / name
        d.mkdir()
        done, trace, hold = _never_ending_op(d, **kw, jump=(0, 600))
        assert done.returncode == 0, (name, done.stderr)
        assert _lap_seconds(d) == 660, (name, _lap_seconds(d), done.stderr)
        assert not any(t.startswith("restart") for t in trace), (name, trace)
        if kw.get("install") is False:
            assert not hold.exists(), (name, "a drain that restarts nothing kept dispatch held")


def test_a_running_job_holds_the_update_past_every_cap_and_an_idle_broker_applies(tmp_path):
    """A running broker job always holds the update, past the job cap and past OP_CAP, with a
    device op in flight beside it too: no cap lets the lap restart over it, before the install or
    after. The wait under the hold ends at the job cap (never the job cap plus OP_CAP) and the next
    lap retries. Once the job and the op are gone, the next lap finds the broker idle and applies."""
    for name, kw, secs in (
        ("drain-lock-past", {"lock": True, "install": False, "job_age": 1600}, 0),
        ("drain-scope-near", {"lock": False, "install": False, "job_age": 1500}, 60),
        ("restart-point-lock-past", {"lock": True, "job_age": 1600}, 0),
        ("restart-point-scope-near", {"lock": False, "job_age": 1500}, 60),
    ):
        d = tmp_path / name
        d.mkdir()
        done, trace, hold = _never_ending_op(d, running=10_000, **kw)
        assert done.returncode == 0, (name, done.stderr)
        assert "device op is still in flight at the job cap" in done.stderr, (name, done.stderr)
        assert not any(t.startswith("restart") for t in trace), (name, trace)
        assert "job cut by restart" not in trace, (name, trace)
        assert not (d / "root" / "installed.sha").exists(), (name, "a restart that did not run was marked current")
        assert _lap_seconds(d) == secs, (name, _lap_seconds(d), done.stderr)

    idle = tmp_path / "idle"
    idle.mkdir()
    done, trace, _ = _drain(idle, running=0, queue=1, scope=True, install="")
    assert done.returncode == 0 and "applying" in done.stdout, done.stderr
    assert trace[-1].startswith("restart held ") and "dispatch" not in trace, trace
    assert (idle / "root" / "installed.sha").read_text().strip() == "abc123"


def test_the_op_cap_is_the_reset_timeout_plus_its_kill_grace():
    from tt_device_mcp import constants

    assert "\nOP_CAP=660\n" in _SCRIPT.read_text()
    assert constants.DEVICE_RESET_TIMEOUT_SEC + constants.GRACEFUL_KILL_GRACE_SEC == 660, "OP_CAP drifted"


def test_a_job_that_ends_at_the_restart_point_is_waited_out(tmp_path):
    done, trace, hold = _drain(tmp_path, running=0, queue=0, install="echo 4 > $M/running")
    assert done.returncode == 0, done.stderr
    assert "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace
    assert (tmp_path / "root" / "installed.sha").read_text().strip() == "abc123"


def test_a_job_past_its_cap_at_the_restart_point_gets_no_restart(tmp_path):
    """A job scope past the cap found by the last look: no restart, installed.sha stays old."""
    install = "echo 10000 > $M/running"
    done, trace, hold = _drain(tmp_path, running=0, queue=0, scope=True, job_age=1600, install=install)
    assert done.returncode == 0, done.stderr
    assert "restart point: the running job is past the job cap" in done.stderr, done.stderr
    assert "not restarting, not marking current" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace
    assert not (tmp_path / "root" / "installed.sha").exists(), "a restart that did not run was marked current"


def test_a_stopped_broker_has_no_children_to_count(tmp_path):
    """MainPID 0 is a stopped broker, and `pgrep -P 0` lists init and kthreadd."""
    init_kids = "pgrep() { seen pgrep; echo 1; echo 2; }\n"
    done, trace, hold = _gate(tmp_path, main_pid=0, stand_ins=init_kids)
    assert done.returncode == 0, done.stderr
    assert "draining" not in done.stderr and not any(t.startswith("pgrep") for t in trace), trace
    assert trace[-1].startswith("restart held "), trace

    running = tmp_path / "running"
    running.mkdir()
    done, _, _ = _gate(running, stand_ins=init_kids, scopes="ttdev-job-1.scope loaded active running", job_age=1600)
    assert "draining" in done.stderr and "children=2" in done.stderr, done.stderr


def test_nothing_slow_runs_between_the_last_busy_look_and_the_restart():
    text = _SCRIPT.read_text()
    last = text.index("Last look before the restart")
    assert text.index('chmod -R a+rX "$VENV"') < last
    tail = text[last : text.index("systemctl restart tt-device-broker")]
    assert "chmod" not in tail


def test_the_obsolete_pending_defer_marker_is_removed_once():
    assert 'rm -f "$ROOT/pending.defer"' in _SCRIPT.read_text()


# --- the autoupdate_apply event: a restart the update caused, traceable from the journal ----------


def _apply_event(tmp_path, health_dir, idle="true", drained_s=0, restart_rc=0):
    """The script from its restart on, with a stub restart that exits `restart_rc`, under the
    broker's own journal-dir variable."""
    text = _SCRIPT.read_text()
    block = text[text.index("systemctl restart tt-device-broker\n") :]
    script = (
        f"set -u\ninstalled_sha=old111; remote_sha=new222; src_desc=test; idle={idle}; drained_s={drained_s}\n"
        f"LAST_APPLY_NRESTARTS={tmp_path}/last-apply.nrestarts\n"
        f"RECOVERY_PENDING={tmp_path}/recovery.defer\nau_log() {{ :; }}\n"
        f"systemctl() {{ return {restart_rc}; }}\n" + _clock(text) + block
    )
    env = {"PATH": "/usr/bin:/bin", "TT_DEVICE_MCP_HEALTH_DIR": str(health_dir)}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)


def _events(health):
    import json

    return [json.loads(line) for line in (health / "health_events.jsonl").read_text().splitlines()]


def test_apply_appends_one_autoupdate_apply_event_in_the_broker_schema(tmp_path):
    health = tmp_path / "health"
    health.mkdir()
    (health / "health_events.jsonl").write_text('{"kind": "x"}\n')
    done = _apply_event(tmp_path, health, idle="false", drained_s=37)
    assert done.returncode == 0, done.stderr
    recs = _events(health)
    assert len(recs) == 2
    rec = recs[1]
    assert rec["kind"] == "autoupdate_apply"
    assert (rec["from_sha"], rec["to_sha"], rec["idle"], rec["drained_s"]) == ("old111", "new222", False, 37)
    assert rec["restart_rc"] == 0 and "broker restarted at new222" in done.stderr, done.stderr
    assert isinstance(rec["ts"], float) and rec["iso"].endswith("Z") and len(rec["iso"]) == 20


def test_a_failed_restart_is_recorded_as_failed(tmp_path):
    """No `set -e`: a failed restart still reaches the event, which must not read as a clean apply."""
    health = tmp_path / "health"
    health.mkdir()
    done = _apply_event(tmp_path, health, restart_rc=5)
    (rec,) = _events(health)
    assert rec["kind"] == "autoupdate_apply" and rec["restart_rc"] == 5, rec
    assert "broker restart at new222 failed (rc=5" in done.stderr, done.stderr
    assert "broker restarted at" not in done.stderr, done.stderr


def test_the_event_goes_to_the_brokers_journal_dir():
    """The broker's journal override (spec 06), not a second deploy-only name for the same dir."""
    statements = [s for s in _SCRIPT.read_text().splitlines() if s.startswith("EVENTS=")]
    assert statements == ['EVENTS="${TT_DEVICE_MCP_HEALTH_DIR:-/var/lib/tt-device-broker/health}/health_events.jsonl"']


def test_a_failed_event_write_only_warns(tmp_path):
    done = _apply_event(tmp_path, tmp_path / "missing")
    assert done.returncode == 0
    assert "warning: could not append autoupdate_apply" in done.stderr


def _recorded_lap(tmp_path, **kw):
    """The real gate and last look against the model broker; the stub restart records what the
    event would carry."""
    rec = tmp_path / "lap"
    on_restart = f'\non_restart() {{ echo "idle=$idle drained_s=$drained_s" > {rec}; }}\n'
    done, trace, _ = _drain(tmp_path, stand_ins=on_restart, **kw)
    assert done.returncode == 0, done.stderr
    assert trace[-1].startswith("restart held "), trace
    return rec.read_text().strip()


def test_an_idle_lap_reports_idle_and_no_drain(tmp_path):
    assert _recorded_lap(tmp_path, running=0, queue=0, install="") == "idle=true drained_s=0"


def test_a_lap_that_drains_reports_busy_and_its_drain_time(tmp_path):
    assert _recorded_lap(tmp_path, running=4, queue=2, install="") == "idle=false drained_s=10"


def test_a_wait_at_the_restart_point_adds_to_the_drain_time(tmp_path):
    lap = _recorded_lap(tmp_path, running=0, queue=0, install="echo 30 > $M/running")
    assert lap == "idle=false drained_s=140"


# --- spec 01 I20, spec 08 I20: the hold preempts a no-timeout-except-on-contention job ------------
# The broker counts a live hold as a waiting job for a preemptable job past its limit: the preempt
# grace (60 s) then SIGTERM, SIGKILL 10 s later. The model broker ends the job that long after its
# limit, as the broker does with nothing else queued; the lap keeps the hold through it.


def _preemptable(limit_left, ends_after, running=1):
    """broker_preempt reports one running preemptable job `limit_left` s before its limit; the model
    job ends `ends_after` s into the drain (the broker's preemption)."""
    stub = f'\nbroker_preempt() {{ echo "{running} 990 {limit_left}"; }}\n'
    end = f"read -r t < $M/t; [ $t -ge {ends_after} ] && echo 0 > $M/running;"
    return stub, end


def test_a_preemptable_job_past_its_limit_is_drained_by_the_hold(tmp_path):
    """Nothing queued, the job past its limit and past the job cap: today's lap would let go at
    once and retry forever. It now keeps the hold for the preempt bound (60 + 10 + 30 s), the
    broker preempts the job, and the update applies at that boundary."""
    stub, end = _preemptable(limit_left=0, ends_after=70)
    done, trace, hold = _drain(
        tmp_path, running=10_000, queue=0, scope=True, job_age=1600, stand_ins=stub, on_sleep=end
    )
    assert done.returncode == 0, done.stderr
    assert "(bound 100s: job cap 1560s, youngest job 1600s in; preemptable job 990" in done.stderr, done.stderr
    assert "drained after 70s" in done.stderr, done.stderr
    assert _polls_held(trace), "the drain let go of dispatch before the preemption"
    assert trace[-1].startswith("restart held "), trace


def test_a_preemptable_job_before_its_limit_keeps_the_hold_until_the_limit_and_the_preemption(tmp_path):
    stub, end = _preemptable(limit_left=300, ends_after=370)
    done, trace, hold = _drain(
        tmp_path, running=10_000, queue=0, scope=True, job_age=1500, stand_ins=stub, on_sleep=end, jump=(10, 290)
    )
    assert done.returncode == 0, done.stderr
    assert "(bound 400s:" in done.stderr and "drained after 370s" in done.stderr, done.stderr
    assert _polls_held(trace)
    assert trace[-1].startswith("restart held "), trace


@pytest.mark.parametrize("limit_left", [1461, 1480, 5000])
def test_a_limit_too_far_off_for_the_job_cap_is_never_held_through(tmp_path, limit_left):
    """limit + 100 s past the job cap: held through, the hold would preempt the job at its limit
    and still let go at the cap, a kill on a lap that cannot apply. The lap lets go at once, before
    the limit, so the hold never preempts the job; the model broker SIGTERMs it only under a live
    hold a minute past its limit, as the broker does."""
    stub, _ = _preemptable(limit_left=limit_left, ends_after=10**9)
    kill = f'read -r t < $M/t; [ -e "$HOLD" ] && [ $t -ge {limit_left + 60} ] && echo "hold-driven kill" >> $M/kills;'
    done, trace, hold = _drain(tmp_path, running=10_000, queue=0, scope=True, job_age=10, stand_ins=stub, on_sleep=kill)
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "model" / "kills").exists(), "the hold preempted a job on a lap that deferred"
    assert "is not held through; dispatch released" in done.stderr, done.stderr
    assert _lap_seconds(tmp_path) < 60, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace
    assert not hold.exists(), "a lap that restarts nothing kept dispatch held"


def test_a_limit_that_just_fits_the_job_cap_is_held_through(tmp_path):
    stub, end = _preemptable(limit_left=1460, ends_after=1530)
    done, trace, hold = _drain(
        tmp_path, running=10_000, queue=0, scope=True, job_age=10, stand_ins=stub, on_sleep=end, jump=(0, 1400)
    )
    assert done.returncode == 0, done.stderr
    assert "(bound 1560s:" in done.stderr and "drained after 1530s" in done.stderr, done.stderr
    assert _polls_held(trace) and trace[-1].startswith("restart held "), trace


def _preempted_then_gate(tmp_path, *, gate_ends, seen_by, jump=None):
    """The only running job, past its limit, is preempted 70 s in; then the broker's post-job gate
    (a kill reset, the full check) runs as its child under the device-op lock until `gate_ends`.
    `seen_by`: how the drain can tell the job ended, its scope or /health."""
    lock = tmp_path / "device-op.lock"
    unit = "990" if seen_by == "scope" else "428"
    names = 'local r; read -r r < $M/running; [ "$r" -gt 0 ] && echo 1 || echo 0' if seen_by == "health" else ":"
    stub = f"""
broker_preempt() {{ echo "1 990 0"; }}
broker_names_preempt_job() {{ {names}; }}
job_scopes() {{ local r; read -r r < $M/running; [ "$r" -gt 0 ] && echo "ttdev-job-{unit}.scope loaded active running"; }}
pgrep() {{ seen pgrep; [ -e $M/gate ] && echo 778; return 0; }}
"""
    on_sleep = (
        f"read -r t < $M/t; read -r r < $M/running; if [ $t -ge 70 ] && [ $r -gt 0 ]; then echo 0 > $M/running;"
        f" touch $M/gate; echo {os.getpid()} post_job_gate > {lock}; fi;"
        f" if [ $t -ge {gate_ends} ]; then rm -f $M/gate {lock}; fi;"
    )
    return _drain(
        tmp_path, running=10_000, queue=0, scope=True, job_age=1600, stand_ins=stub, on_sleep=on_sleep, jump=jump
    )


@pytest.mark.parametrize("seen_by", ["health", "scope"])
def test_a_preempted_job_whose_post_job_gate_runs_long_still_applies_under_the_same_hold(tmp_path, seen_by):
    """The hold preempted the job; its post-job gate (op lock live, a broker child) runs to 300 s,
    past limit + 70 + 30 s. The lap keeps the hold through it and applies at the job boundary, never
    letting go of dispatch in between (a standby resubmitted then would be killed on every lap)."""
    done, trace, hold = _preempted_then_gate(tmp_path, gate_ends=300, seen_by=seen_by, jump=(75, 290))
    assert done.returncode == 0, done.stderr
    assert "the preemptable job 990 ended after 70s" in done.stderr, done.stderr
    assert "drained after 300s" in done.stderr, done.stderr
    assert "dispatch released" not in done.stderr, done.stderr
    assert _polls_held(trace), "the drain let go of dispatch during the post-job gate"
    assert trace[-1].startswith("restart held "), trace


def test_a_post_job_gate_that_never_ends_lets_go_at_the_gate_cap(tmp_path):
    done, trace, hold = _preempted_then_gate(tmp_path, gate_ends=10**9, seen_by="health", jump=(75, 950))
    assert done.returncode == 0, done.stderr
    assert "post-job gate is still running 936s after the preempted job ended" in done.stderr, done.stderr
    assert 70 + 936 <= _lap_seconds(tmp_path) < 70 + 936 + 5, done.stderr  # 5 s polls
    assert not any(t.startswith("restart") for t in trace), trace
    assert not hold.exists(), "a lap that restarts nothing kept dispatch held"


def test_the_gate_cap_is_a_kill_reset_plus_a_full_check_on_32_chips():
    from tt_device_mcp import constants

    assert "\nGATE_CAP=936\n" in _SCRIPT.read_text()
    full = constants.FABRIC_CHECK_TIMEOUT_SEC + constants.FULL_CHECK_PER_CHIP_SEC * 32
    assert constants.DEVICE_RESET_TIMEOUT_SEC + constants.GRACEFUL_KILL_GRACE_SEC + full == 936, "GATE_CAP drifted"


@pytest.mark.parametrize("report", ["", "2 990 0"])
def test_a_job_past_the_cap_still_defers_unless_the_only_running_job_is_preemptable(tmp_path, report):
    """No preemptable job (or a second job running beside it): the job cap's bound, as before."""
    stub = f'\nbroker_preempt() {{ [ -n "{report}" ] && echo "{report}"; }}\n'
    done, trace, hold = _drain(tmp_path, running=10_000, queue=1, scope=True, job_age=1600, stand_ins=stub)
    _no_restart(done, trace, hold)
    assert "(bound 0s: job cap 1560s, youngest job 1600s in)" in done.stderr, done.stderr
    assert "past the job cap (1560s) after 0s" in done.stderr, done.stderr
