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
import subprocess
import time
from pathlib import Path

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


def _gate(
    tmp_path,
    *,
    scopes="",
    queued=0,
    stand_ins="",
    env=None,
    hold_dir=True,
    main_pid=4242,
    install=None,
    job_age=None,
    broker_state="active",
):
    """`install`: also run the last look before the restart (spec 08 I24), with this shell text
    standing in for the install between the gate and it; the restart then follows the STATE write.
    `job_age`: seconds a running job scope has run, from systemd's start record (None: unreadable)."""
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
    assert gate.count("for p in /proc/[0-9]*; do") == 1
    gate = gate.replace("for p in /proc/[0-9]*; do", f"for p in {tmp_path}/proc/[0-9]*; do")
    enter = "" if job_age is None else str((_UPTIME - job_age) * 1_000_000)
    trace = tmp_path / "trace"
    logs = tmp_path / "logs"
    (logs / "queued").mkdir(parents=True)
    for i in range(queued):
        (logs / "queued" / f"{i}.json").write_text("{}")
    root = tmp_path / "root"
    root.mkdir()
    prelude = f"""set -u
ROOT={root}; remote_sha=abc123; INHIBIT={tmp_path / "device-op.lock"}; STATE={root / "installed.sha"}
on_restart() {{ :; }}
seen() {{ if [ -e "$HOLD" ]; then echo "$1 held $(cat "$HOLD")" >> {trace}; else echo "$1 free" >> {trace}; fi; }}
sleep() {{ seen "sleep $*"; }}
pgrep() {{ seen pgrep; }}
ls() {{ :; }}  # no real /proc holders
job_scopes() {{ [ -n "{scopes}" ] && echo "{scopes}"; }}
systemctl() {{
    case "$*" in
        *list-units*ttdev-job-*) seen scopes; job_scopes ;;
        *ActiveEnterTimestampMonotonic*) echo "{enter}" ;;
        is-active*) echo {broker_state} ;;
        show*) echo {main_pid} ;;
        restart*) on_restart; seen restart ;;
    esac
}}
{stand_ins}
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
    boundary, and it waits, persisted, for the new broker."""
    done, trace, hold = _gate(tmp_path, queued=1)
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


# --- spec 08 I20/I24: a busy box drains at once, and applies at the job boundary -------------------
# A model broker stands behind pgrep: one job runs for `job_ticks` polls, and whenever no job runs
# and no hold stands it dispatches the next queued one at once, as a queue of back-to-back jobs does.
# With `scope`, the running job shows as a ttdev-job scope (privsep, or a re-adopted job) instead of
# a broker child. The restart stub asks the model once more first: a job dispatched between the
# last idle count and the restart, or one still running, is a job the restart cuts off. A fake clock
# moves only when the lap sleeps.

_T0 = 1_700_000_000


def _broker(tmp_path, *, running, queue, job_ticks=3, on_sleep="", scope=False):
    m = tmp_path / "model"
    m.mkdir()
    (m / "running").write_text(str(running))
    (m / "queue").write_text(str(queue))
    (m / "t").write_text("0")
    trace = tmp_path / "trace"
    child = "" if scope else '[ "$r" -gt 0 ] && echo 777'
    scopes = (
        '\njob_scopes() { [ "$(cat $M/running)" -gt 0 ] && echo "ttdev-job-428.scope loaded active running"; }'
        if scope
        else ""
    )
    return f"""
M={m}
# One poll: the job advances, the count reads the broker, and only then does the broker dispatch
# again, as it does a second after a job ends.
pgrep() {{
    seen pgrep
    local r q; r=$(cat $M/running); q=$(cat $M/queue)
    if [ "$r" -gt 0 ]; then r=$((r-1)); fi
    {child}
    if [ "$r" -eq 0 ] && [ "$q" -gt 0 ] && [ ! -e "$HOLD" ]; then
        q=$((q-1)); r={job_ticks}; echo dispatch >> {trace}
    fi
    echo $r > $M/running; echo $q > $M/queue
    return 0
}}{scopes}
on_restart() {{
    local r q; r=$(cat $M/running); q=$(cat $M/queue)
    if [ "$r" -eq 0 ] && [ "$q" -gt 0 ] && [ ! -e "$HOLD" ]; then echo dispatch >> {trace}; r=1; fi
    if [ "$r" -gt 0 ]; then echo "job cut by restart" >> {trace}; fi
}}
date() {{ echo $(( {_T0} + $(cat $M/t) )); }}
sleep() {{ seen "sleep $*"; echo $(( $(cat $M/t) + ${{1%.*}} )) > $M/t; {on_sleep} }}
"""


def _drain(tmp_path, *, running, queue, job_ticks=3, on_sleep="", scope=False, env=None, **kw):
    """The gate against the model broker; its queued jobs are persisted specs too."""
    stand_ins = _broker(tmp_path, running=running, queue=queue, job_ticks=job_ticks, on_sleep=on_sleep, scope=scope)
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
        done, trace, _ = _drain(case, running=40, queue=1, scope=True, env=env)
        assert done.returncode == 0, done.stderr
        assert "drained after 190s" in done.stderr, f"{name}: {done.stderr}"
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


def test_a_job_past_its_cap_does_not_delay_the_update_past_the_cap(tmp_path):
    """The wait is bounded by the broker's hard job limit (1500 s + 60 s kill grace), counted
    from the job scope's start: a re-adopted job keeps its age across restarts."""
    # Already past the cap: no wait at all.
    past = tmp_path / "past"
    past.mkdir()
    done, trace, _ = _drain(past, running=10_000, queue=1, scope=True, job_age=1600)
    assert done.returncode == 0, done.stderr
    assert "(bound 0s:" in done.stderr and "past the job cap (1560s) after 0s" in done.stderr, done.stderr
    assert not [t for t in trace if t.startswith("sleep 5")], f"a job past its cap delayed the update: {trace}"
    assert trace[-1].startswith("restart held ") and "dispatch" not in trace, trace

    # 60 s short of the cap: the update waits those 60 s and no longer.
    near = tmp_path / "near"
    near.mkdir()
    done, trace, _ = _drain(near, running=10_000, queue=1, scope=True, job_age=1500)
    assert "(bound 60s:" in done.stderr and "past the job cap (1560s) after 60s" in done.stderr, done.stderr
    assert int((near / "model" / "t").read_text()) == 60
    assert trace[-1].startswith("restart held "), trace

    # No readable start counts as just started: the whole cap, from the drain's start.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    done, trace, _ = _drain(fresh, running=10_000, queue=1, scope=True)
    assert "past the job cap (1560s) after 1560s" in done.stderr, done.stderr
    assert _polls_held(trace), "the drain let go of dispatch before the cap"


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

    # A reset scope counts the same, even with the broker down.
    scope = tmp_path / "scope"
    scope.mkdir()
    reset_scope = """
_sc=$(declare -f systemctl | sed '1d')
eval "systemctl_job() $_sc"
systemctl() { case "$*" in *list-units*ttdev-reset-*) echo "ttdev-reset-7.scope loaded active running" ;; *) systemctl_job "$@" ;; esac; }
"""
    done, trace, hold = _drain(
        scope, running=10_000, queue=1, scope=True, job_age=1600, broker_state="failed", stand_ins=reset_scope
    )
    assert "device op is still in flight at the job cap" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace


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


def test_a_device_op_at_the_restart_point_blocks_the_restart(tmp_path):
    inhibit = tmp_path / "device-op.lock"
    start_op = f"echo 10000 > $M/running; echo {os.getpid()} tt_device_reset > {inhibit}"
    done, trace, hold = _drain(tmp_path, running=0, queue=0, scope=True, job_age=1600, install=start_op)
    assert done.returncode == 0, done.stderr
    assert "applying" in done.stdout and "not restarting, not marking current" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace
    assert not (tmp_path / "root" / "installed.sha").exists(), "a restart that did not run was marked current"


def test_a_job_that_ends_at_the_restart_point_is_waited_out(tmp_path):
    done, trace, hold = _drain(tmp_path, running=0, queue=0, install="echo 4 > $M/running")
    assert done.returncode == 0, done.stderr
    assert "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace
    assert (tmp_path / "root" / "installed.sha").read_text().strip() == "abc123"


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


def test_a_device_holder_alone_is_named_as_such_past_the_cap():
    text = _SCRIPT.read_text()
    assert '"$holders" -gt 0' in text and "a device holder is" in text
    assert "the running job is" in text


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
        f"systemctl() {{ return {restart_rc}; }}\n" + block
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
