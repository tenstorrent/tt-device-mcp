# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The auto-update's two busy gates (spec 08 I7, I24).

A running JOB holds an update back (spec 08 I24): it is re-adopted across a restart, but the
restart still drops its client's socket and holds the queue for the new broker's startup checks
until it ends, so the update waits for a job boundary (defer, then drain) and never restarts the
broker under it. An in-flight DEVICE OP bars the update outright: re-adoption covers a job, not a
tt-smi that KillMode=control-group SIGTERMs mid-reset, which is a half-reset mesh and a chassis
power cycle.
"""

import os
import subprocess
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"


def test_the_idle_gate_is_on_by_default():
    # spec 08 I24: unset or 0 takes the default bound; no value turns the gate off.
    text = _SCRIPT.read_text()
    assert '[ "$MAX_DEFER" -gt 0 ] 2>/dev/null || MAX_DEFER=3600' in text
    assert 'if [ "$busy" -ne 0 ]; then' in text
    assert "applying anyway" not in text and "idle gate off" not in text


def test_an_in_flight_device_op_still_bars_the_update():
    # Unconditional, and before the idle gate: no knob may turn this one off.
    text = _SCRIPT.read_text()
    op_bar = text.index("device op in flight")
    assert "exit 0" in text[op_bar : op_bar + 200]
    assert text.index("ttdev-reset-") < text.index("MAX_DEFER=")


# --- spec 08 I18: the dispatch hold, and what counts as busy --------------------------------------
# The gate runs for real, cut from the script between the hold and the "applying" line, under
# stand-ins for systemd, pgrep and the /proc scan. A stub `systemctl restart` stands in for the
# restart that ends an applying lap and records whether the hold was still there.


def _gate(
    tmp_path,
    *,
    scopes="",
    queued=0,
    max_defer=600,
    stand_ins="",
    pending=None,
    env=None,
    hold_dir=True,
    main_pid=4242,
    install=None,
):
    """`install`: also run the last look before the restart (spec 08 I24), with this shell text
    standing in for the install between the gate and it; the restart then follows the STATE write."""
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
    # No real /proc holders, and no scan of the host's processes on every poll of a long drain.
    assert gate.count("for p in /proc/[0-9]*; do") == 1
    gate = gate.replace("for p in /proc/[0-9]*; do", f"for p in {tmp_path}/proc/[0-9]*; do")
    trace = tmp_path / "trace"
    logs = tmp_path / "logs"
    (logs / "queued").mkdir(parents=True)
    for i in range(queued):
        (logs / "queued" / f"{i}.json").write_text("{}")
    root = tmp_path / "root"
    root.mkdir()
    if pending is not None:
        (root / "pending.defer").write_text(pending)
    prelude = f"""set -u
ROOT={root}; remote_sha=abc123; INHIBIT={tmp_path / "device-op.lock"}; STATE={root / "installed.sha"}
on_restart() {{ :; }}
seen() {{ if [ -e "$HOLD" ]; then echo "$1 held $(cat "$HOLD")" >> {trace}; else echo "$1 free" >> {trace}; fi; }}
sleep() {{ seen "sleep $*"; }}
pgrep() {{ seen pgrep; }}
ls() {{ :; }}  # no real /proc holders
systemctl() {{
    case "$*" in
        *list-units*ttdev-job-*) seen scopes; [ -n "{scopes}" ] && echo "{scopes}" ;;
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
        **({} if max_defer is None else {"TTDEV_MAX_DEFER_SEC": str(max_defer)}),
        "TTDEV_HOLD_SETTLE_SEC": "7",
        "TTDEV_LOG_DIR": str(logs),
        **(env or {}),
    }
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    lines = trace.read_text().splitlines() if trace.exists() else []
    return done, lines, hold


def test_the_hold_is_taken_before_anything_is_counted(tmp_path):
    done, trace, hold = _gate(tmp_path, max_defer=0)
    assert done.returncode == 0, done.stderr
    assert trace[0].startswith("sleep 7 held "), f"the settle did not run under the hold: {trace}"
    pid, epoch = trace[0].split()[-2:]
    assert int(pid) > 0 and abs(int(epoch) - time.time()) < 60, trace[0]
    counted = [t for t in trace if t.split()[0] in ("pgrep", "scopes")]
    assert counted and all(" held " in t for t in counted), f"something was counted without the hold: {trace}"
    assert not hold.exists(), "the hold outlived the lap"


def test_the_hold_appears_whole(tmp_path):
    """The broker removes a hold it cannot parse, so the hold is never seen empty or half-written:
    it is written under another name and renamed into place."""
    done, trace, hold = _gate(tmp_path, max_defer=0, stand_ins='mv() { seen "mv $*"; command mv "$@"; }')
    assert done.returncode == 0, done.stderr
    assert trace[0].startswith(f"mv -f {hold}.") and trace[0].endswith(f" {hold} free"), trace
    assert trace[1].startswith("sleep 7 held "), f"the rename did not put the hold in place: {trace}"
    assert not hold.exists() and list(hold.parent.iterdir()) == [], "the lap left a hold file behind"


def test_a_queued_job_defers_an_opted_in_update_and_drops_the_hold(tmp_path):
    done, trace, hold = _gate(tmp_path, queued=1)
    assert done.returncode == 0, done.stderr
    assert "deferring" in done.stderr and "queued=1" in done.stderr, done.stderr
    assert "applying" not in done.stdout + done.stderr
    assert not any(t.startswith("restart") for t in trace)
    assert not hold.exists(), "a deferring lap left dispatch held"
    assert (tmp_path / "root" / "pending.defer").exists()
    # And the defer drops the hold itself, before its exit, not only through the trap.
    text = _SCRIPT.read_text()
    defer = text[text.index('echo "autoupdate: deferring') :]
    assert defer.index('rm -f "$HOLD"') < defer.index("exit 0")


def test_an_idle_box_applies_and_keeps_the_hold_until_the_restart(tmp_path):
    done, trace, hold = _gate(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "deferring" not in done.stderr
    assert [t for t in trace if t.startswith("restart")] and trace[-1].startswith("restart held "), trace
    assert not hold.exists(), "the hold outlived the restart"
    # Nothing between the gate and the restart lets go of it.
    text = _SCRIPT.read_text()
    apply = text[text.index('echo "autoupdate: applying') : text.index("systemctl restart tt-device-broker")]
    assert "$HOLD" not in apply


def test_a_running_job_scope_counts_as_busy(tmp_path):
    done, trace, hold = _gate(tmp_path, scopes="ttdev-job-42.scope loaded active running")
    assert done.returncode == 0, done.stderr
    assert "deferring" in done.stderr and "scopes=1" in done.stderr, done.stderr
    assert not hold.exists()


# --- spec 08 I20: draining a box that is never idle ------------------------------------------------
# A model broker stands behind pgrep: one job runs for `job_ticks` polls, and whenever no job runs
# and no hold stands it dispatches the next queued one at once, as a queue of back-to-back jobs does.
# The restart stub asks it once more first: a job dispatched between the last idle count and the
# restart is the one the restart cuts off. A fake clock moves only when the lap sleeps.

_T0 = 1_700_000_000


def _broker(tmp_path, *, running, queue, job_ticks=3, on_sleep=""):
    m = tmp_path / "model"
    m.mkdir()
    (m / "running").write_text(str(running))
    (m / "queue").write_text(str(queue))
    (m / "t").write_text("0")
    trace = tmp_path / "trace"
    return f"""
M={m}
# One poll: the job advances, the count reads the broker, and only then does the broker dispatch
# again, as it does a second after a job ends.
pgrep() {{
    seen pgrep
    local r q; r=$(cat $M/running); q=$(cat $M/queue)
    if [ "$r" -gt 0 ]; then r=$((r-1)); fi
    [ "$r" -gt 0 ] && echo 777
    if [ "$r" -eq 0 ] && [ "$q" -gt 0 ] && [ ! -e "$HOLD" ]; then
        q=$((q-1)); r={job_ticks}; echo dispatch >> {trace}
    fi
    echo $r > $M/running; echo $q > $M/queue
    return 0
}}
on_restart() {{
    local r q; r=$(cat $M/running); q=$(cat $M/queue)
    if [ "$r" -eq 0 ] && [ "$q" -gt 0 ] && [ ! -e "$HOLD" ]; then echo dispatch >> {trace}; r=1; fi
    if [ "$r" -gt 0 ]; then echo "job cut by restart" >> {trace}; fi
}}
date() {{ echo $(( {_T0} + $(cat $M/t) )); }}
sleep() {{ seen "sleep $*"; echo $(( $(cat $M/t) + ${{1%.*}} )) > $M/t; {on_sleep} }}
"""


def _drain(tmp_path, *, running, queue, job_ticks=3, on_sleep="", env=None, **kw):
    """The gate against the model broker; its queued jobs are persisted specs too."""
    stand_ins = _broker(tmp_path, running=running, queue=queue, job_ticks=job_ticks, on_sleep=on_sleep)
    env = {"TTDEV_HOLD_SETTLE_SEC": "0", **(env or {})}
    return _gate(tmp_path, stand_ins=stand_ins + kw.pop("stand_ins", ""), queued=queue, env=env, **kw)


def _since(seconds_ago):
    return f"oldsha {_T0 - seconds_ago}\n"


def test_a_busy_box_defers_until_the_drain_is_due(tmp_path):
    done, trace, hold = _drain(tmp_path, running=5, queue=2, pending=_since(100))
    assert done.returncode == 0, done.stderr
    assert "deferring" in done.stderr and "drain in 200s" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace)
    assert not hold.exists(), "a lap that is not draining left dispatch held"
    assert (tmp_path / "root" / "pending.defer").read_text().split() == ["abc123", str(_T0 - 100), str(_T0 + 200)]


def test_a_due_drain_keeps_the_hold_and_applies_at_the_job_boundary(tmp_path):
    done, trace, hold = _drain(tmp_path, running=4, queue=2, pending=_since(400))
    assert done.returncode == 0, done.stderr
    assert "draining" in done.stderr and "drained after 10s" in done.stderr, done.stderr
    assert "dispatch" not in trace, f"a queued job started under the drain: {trace}"
    assert "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace
    polls = [t for t in trace if t.startswith("pgrep")]
    assert len(polls) == 4 and all(" held " in t for t in polls), f"the drain let go of dispatch: {trace}"
    assert (tmp_path / "model" / "queue").read_text().strip() == "2", "the queued jobs wait for the new broker"
    assert not (tmp_path / "root" / "pending.defer").exists()
    assert not hold.exists(), "the hold outlived the restart"


def test_a_back_to_back_queue_cannot_slip_a_job_into_the_restart(tmp_path):
    """The idle check used to race dispatch: a job dispatched a second after the count read
    children=0 was cut off by the restart (rc 120). Under the drain's hold it never starts."""
    done, trace, hold = _drain(tmp_path, running=2, queue=5, job_ticks=1, pending=_since(400))
    assert done.returncode == 0, done.stderr
    assert "draining" in done.stderr, done.stderr
    assert "dispatch" not in trace and "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace

    # The same lap with no hold to take is the old race, and the model catches it.
    control = tmp_path / "control"
    control.mkdir()
    done, trace, _ = _drain(control, running=2, queue=5, job_ticks=1, pending=_since(400), hold_dir=False)
    assert "job cut by restart" in trace, f"the model no longer shows the race it guards: {trace}"


def test_a_drain_that_outlives_its_bound_lets_the_job_run_and_backs_off(tmp_path):
    done, trace, hold = _drain(
        tmp_path,
        running=10_000,
        queue=1,
        pending=_since(400),
        env={"TTDEV_DRAIN_TIMEOUT_SEC": "20"},
        stand_ins='kill() { seen "kill $*"; }\n',
    )
    assert done.returncode == 0, done.stderr
    assert "drain timed out after 20s" in done.stderr, done.stderr
    assert "applying" not in done.stdout + done.stderr
    assert not any(t.startswith(("restart", "kill")) for t in trace), f"the running job was touched: {trace}"
    assert not hold.exists(), "a drain that gave up kept dispatch held"
    assert int((tmp_path / "model" / "running").read_text()) > 0
    since, next_drain = (tmp_path / "root" / "pending.defer").read_text().split()[1:]
    assert since == str(_T0 - 400), "the wait is still clocked from the first pending update"
    assert next_drain == str(_T0 + 20 + 300), "the next drain is DRAIN_AFTER past this one"


def test_a_backed_off_drain_waits_its_turn(tmp_path):
    pending = f"oldsha {_T0 - 400} {_T0 + 60}\n"
    done, trace, hold = _drain(tmp_path, running=5, queue=1, pending=pending)
    assert done.returncode == 0, done.stderr
    assert "deferring" in done.stderr and "drain in 60s" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace) and not hold.exists()


def test_a_device_op_in_flight_holds_a_drained_apply(tmp_path):
    """The broker may start a device op while dispatch is held; the apply waits it out."""
    inhibit = tmp_path / "device-op.lock"
    inhibit.write_text(f"{os.getpid()} health_gate\n")
    count = tmp_path / "sleeps"
    on_sleep = f'echo x >> {count}; [ "$(wc -l < {count})" -ge 3 ] && rm -f {inhibit};'
    done, trace, hold = _drain(tmp_path, running=0, queue=1, on_sleep=on_sleep, pending=_since(400))
    assert done.returncode == 0, done.stderr
    assert "draining" in done.stderr, done.stderr
    assert trace[-1].startswith("restart held "), trace
    # The settle's sleep, then drain polls until the op ended on the third sleep.
    assert len(count.read_text().splitlines()) == 3, "the apply did not wait for the device op"
    assert "dispatch" not in trace


def test_the_drain_refreshes_the_hold(tmp_path):
    """The broker drops a hold past DISPATCH_HOLD_MAX_SEC; a drain that is still polling must not
    lose it, so every poll rewrites it, and the apply starts on a fresh one."""
    done, trace, hold = _drain(tmp_path, running=6, queue=1, pending=_since(400))
    assert done.returncode == 0, done.stderr
    epochs = [int(t.split()[-1]) for t in trace if t.startswith("pgrep held ")]
    assert epochs[0] == _T0 and epochs[-1] > epochs[0], trace
    assert epochs == sorted(epochs)
    assert int(trace[-1].split()[-1]) == epochs[-1], "the apply did not start on a fresh hold"


def test_the_apply_ends_in_a_full_broker_restart():
    """The update's restart is an ordinary stop and start, so the new broker runs its whole startup
    check (spec 03 I4) before its queue opens: no reload, no kill, no knob that skips it."""
    text = _SCRIPT.read_text()
    tail = text[text.index('echo "autoupdate: applying') :]
    assert tail.count("systemctl restart tt-device-broker") == 1
    for shortcut in ("reload", "kill -HUP", "SKIP", "--no-block"):
        assert shortcut not in tail, shortcut


# --- spec 08 I24: never a restart under a running tenant job ---------------------------------------


def test_a_busy_box_with_no_max_defer_set_still_defers(tmp_path):
    """The idle gate used to be off unless TTDEV_MAX_DEFER_SEC was set, so a busy lap restarted
    the broker under the running job. Unset or 0, it now takes the default bound and defers."""
    for name, max_defer in (("unset", None), ("zero", 0)):
        case = tmp_path / name
        case.mkdir()
        done, trace, hold = _drain(case, running=5, queue=1, max_defer=max_defer, pending=_since(100))
        assert done.returncode == 0, done.stderr
        assert "deferring" in done.stderr and "waited 100s/3600s" in done.stderr, done.stderr
        assert "applying" not in done.stdout + done.stderr
        assert not any(t.startswith("restart") for t in trace), f"{name}: restarted under a job: {trace}"
        assert not hold.exists()


def test_past_max_defer_the_drain_outwaits_a_long_job_and_applies_at_its_end(tmp_path):
    # A job that runs about 500 s: longer than the short bound, well under the long one.
    done, trace, hold = _drain(tmp_path, running=101, queue=2, max_defer=None, pending=_since(4000))
    assert done.returncode == 0, done.stderr
    assert "(bound 1800s)" in done.stderr and "drained after 495s" in done.stderr, done.stderr
    assert "dispatch" not in trace and "job cut by restart" not in trace, trace
    assert trace[-1].startswith("restart held "), trace
    assert not (tmp_path / "root" / "pending.defer").exists()

    # Before MAX_DEFER the same job outlives the short bound: the drain backs off, no restart.
    early = tmp_path / "early"
    early.mkdir()
    done, trace, _ = _drain(early, running=101, queue=2, max_defer=None, pending=_since(400))
    assert "(bound 300s)" in done.stderr and "drain timed out after 300s" in done.stderr, done.stderr
    assert not any(t.startswith("restart") for t in trace), trace


def test_a_job_past_the_long_bound_is_left_alone_and_the_drain_backs_off(tmp_path):
    done, trace, hold = _drain(
        tmp_path,
        running=10_000,
        queue=1,
        max_defer=None,
        pending=_since(4000),
        stand_ins='kill() { seen "kill $*"; }\n',
    )
    assert done.returncode == 0, done.stderr
    assert "drain timed out after 1800s" in done.stderr, done.stderr
    assert "applying" not in done.stdout + done.stderr
    assert not any(t.startswith(("restart", "kill")) for t in trace), f"the running job was touched: {trace}"
    assert not hold.exists()
    since, next_drain = (tmp_path / "root" / "pending.defer").read_text().split()[1:]
    assert since == str(_T0 - 4000) and next_drain == str(_T0 + 1800 + 300)


def test_a_job_running_at_the_restart_point_blocks_the_restart(tmp_path):
    """The gate saw an idle box, but a job is running by the time the install is done: the lap
    waits for it up to DRAIN_TIMEOUT, then leaves installed.sha alone and restarts nothing."""
    start_job = "echo 10000 > $M/running"
    done, trace, hold = _drain(tmp_path, running=0, queue=0, install=start_job)
    assert done.returncode == 0, done.stderr
    assert "applying" in done.stdout and "a job is running at the restart point after 300s" in done.stderr
    assert not any(t.startswith("restart") for t in trace), trace
    assert not (tmp_path / "root" / "installed.sha").exists(), "a restart that did not run was marked current"
    assert (tmp_path / "root" / "pending.defer").read_text().split()[2] == str(_T0 + 300 + 300)
    assert not hold.exists()
    looks = [t for t in trace if t.startswith("pgrep")][1:]
    assert looks and all(" held " in t for t in looks), f"the last look let go of dispatch: {trace}"


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
    assert "deferring" not in done.stderr and not any(t.startswith("pgrep") for t in trace), trace
    assert trace[-1].startswith("restart held "), trace

    running = tmp_path / "running"
    running.mkdir()
    done, _, _ = _gate(running, stand_ins=init_kids)
    assert "deferring" in done.stderr and "children=2" in done.stderr, done.stderr
